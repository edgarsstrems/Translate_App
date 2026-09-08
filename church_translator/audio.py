from __future__ import annotations

import io
import queue
import threading
import time
from dataclasses import dataclass
from typing import Callable

import numpy as np
import sounddevice as sd
import soundfile as sf


SAMPLE_RATE = 16_000
CHANNELS = 1


@dataclass(frozen=True)
class AudioDevice:
    index: int
    name: str
    max_input_channels: int
    max_output_channels: int

    @property
    def label(self) -> str:
        return f"{self.name} [{self.index}]"


@dataclass(frozen=True)
class RecorderInfo:
    device_index: int
    device_name: str
    sample_rate: int
    channels: int
    chunk_seconds: float
    min_chunk_seconds: float
    early_flush_silence_seconds: float
    overlap_seconds: float
    hop_seconds: float
    blocksize: int


def list_audio_devices() -> list[AudioDevice]:
    devices = []
    for index, raw in enumerate(sd.query_devices()):
        devices.append(
            AudioDevice(
                index=index,
                name=str(raw.get("name", f"Device {index}")),
                max_input_channels=int(raw.get("max_input_channels", 0)),
                max_output_channels=int(raw.get("max_output_channels", 0)),
            )
        )
    return devices


def audio_device_name(device_index: int) -> str:
    try:
        raw = sd.query_devices(device_index)
        return str(raw.get("name", f"Device {device_index}"))
    except Exception:
        return f"Device {device_index}"


class ChunkRecorder:
    def __init__(
        self,
        device_index: int,
        chunk_seconds: float,
        overlap_seconds: float,
        min_chunk_seconds: float,
        early_flush_silence_seconds: float,
        silence_rms_threshold: float,
        silence_peak_threshold: float,
        on_chunk: Callable[[int, np.ndarray, float, float], None],
        on_error: Callable[[str], None],
        on_level: Callable[[float, float], None] | None = None,
        on_started: Callable[[RecorderInfo], None] | None = None,
        pre_speech_seconds: float = 0.40,
    ) -> None:
        self.device_index = device_index
        # Hard ceiling of 10.0 seconds maximum chunk duration
        self.chunk_seconds = min(10.0, max(2.0, float(chunk_seconds)))
        self.min_chunk_seconds = max(1.0, min(min_chunk_seconds, self.chunk_seconds))
        # Post-speech pause / sentence boundary detection threshold (default ~0.70s)
        self.early_flush_silence_seconds = max(0.40, min(1.20, float(early_flush_silence_seconds)))
        self.silence_rms_threshold = silence_rms_threshold
        self.silence_peak_threshold = silence_peak_threshold
        self.overlap_seconds = max(0.0, min(overlap_seconds, self.chunk_seconds - 0.25))
        self.pre_speech_seconds = max(0.30, min(0.60, float(pre_speech_seconds)))
        self.on_chunk = on_chunk
        self.on_error = on_error
        self.on_level = on_level
        self.on_started = on_started

        # Frame limits
        self._frames_needed = int(SAMPLE_RATE * self.chunk_seconds)
        self._min_frames = int(SAMPLE_RATE * self.min_chunk_seconds)
        self._early_flush_silence_frames = int(SAMPLE_RATE * self.early_flush_silence_seconds)
        self._pre_speech_frames = int(SAMPLE_RATE * self.pre_speech_seconds)
        self._overlap_frames = int(SAMPLE_RATE * self.overlap_seconds)
        self._hop_frames = max(1, self._frames_needed - self._overlap_frames)

        # Dynamic speech tracking state
        self._is_recording_speech = False
        self._pre_speech_buffer: list[np.ndarray] = []
        self._pre_speech_collected = 0
        self._active_buffer: list[np.ndarray] = []
        self._active_frames = 0
        self._silent_frames = 0
        self._speech_frames_in_chunk = 0

        self._stream: sd.InputStream | None = None
        self._lock = threading.Lock()
        self._chunk_index = 0
        self._last_level_report = 0.0

    def start(self) -> None:
        self._stream = sd.InputStream(
            samplerate=SAMPLE_RATE,
            device=self.device_index,
            channels=CHANNELS,
            dtype="float32",
            callback=self._callback,
            blocksize=int(SAMPLE_RATE * 0.1),
        )
        self._stream.start()
        if self.on_started:
            self.on_started(
                RecorderInfo(
                    device_index=self.device_index,
                    device_name=audio_device_name(self.device_index),
                    sample_rate=SAMPLE_RATE,
                    channels=CHANNELS,
                    chunk_seconds=self.chunk_seconds,
                    min_chunk_seconds=self.min_chunk_seconds,
                    early_flush_silence_seconds=self.early_flush_silence_seconds,
                    overlap_seconds=self.overlap_seconds,
                    hop_seconds=self._hop_frames / SAMPLE_RATE,
                    blocksize=int(SAMPLE_RATE * 0.1),
                )
            )

    def stop(self) -> None:
        ready: list[tuple[int, np.ndarray, float]] = []
        with self._lock:
            stream = self._stream
            self._stream = None
            # If stopping while actively recording speech, flush remaining captured audio
            if self._active_buffer and self._speech_frames_in_chunk >= int(SAMPLE_RATE * 0.20):
                combined = np.concatenate(self._active_buffer)
                ready.append((self._chunk_index, combined.copy(), 0.0))
                self._active_buffer = []
                self._active_frames = 0
                self._is_recording_speech = False
                self._chunk_index += 1
        if stream:
            stream.stop()
            stream.close()
        captured_at = time.monotonic()
        for chunk_index, chunk, leading_context_seconds in ready:
            try:
                self.on_chunk(chunk_index, chunk, captured_at, leading_context_seconds)
            except Exception:
                pass

    def _callback(self, indata, frames, _time_info, status) -> None:
        if status:
            self.on_error(f"Audio input warning: {status}")
        try:
            mono = np.asarray(indata[:, 0], dtype=np.float32).copy()
            now = time.monotonic()
            rms = float(np.sqrt(np.mean(np.square(mono)))) if mono.size else 0.0
            peak = float(np.max(np.abs(mono))) if mono.size else 0.0
            if self.on_level and now - self._last_level_report >= 0.1:
                self._last_level_report = now
                self.on_level(rms, peak)

            is_speech = (rms >= self.silence_rms_threshold or peak >= self.silence_peak_threshold)
            ready: list[tuple[int, np.ndarray, float]] = []

            with self._lock:
                if not self._is_recording_speech:
                    if not is_speech:
                        # Waiting for speech: maintain rolling pre-speech buffer (300-500ms)
                        # Avoid accumulating silence chunks to Whisper.
                        self._pre_speech_buffer.append(mono)
                        self._pre_speech_collected += frames
                        while self._pre_speech_collected > self._pre_speech_frames and self._pre_speech_buffer:
                            dropped = self._pre_speech_buffer.pop(0)
                            self._pre_speech_collected -= len(dropped)
                    else:
                        # Speech onset detected! Transition to active recording.
                        # Prepend the pre-speech buffer so the first consonant/syllable is never clipped.
                        self._is_recording_speech = True
                        self._active_buffer = list(self._pre_speech_buffer)
                        self._active_frames = self._pre_speech_collected
                        self._pre_speech_buffer = []
                        self._pre_speech_collected = 0

                        self._active_buffer.append(mono)
                        self._active_frames += frames
                        self._speech_frames_in_chunk = frames
                        self._silent_frames = 0
                else:
                    # Actively recording a dynamic speech segment
                    self._active_buffer.append(mono)
                    self._active_frames += frames

                    if is_speech:
                        self._speech_frames_in_chunk += frames
                        self._silent_frames = 0
                    else:
                        self._silent_frames += frames

                    # 1. Natural sentence ending detection before 10 seconds:
                    # When speech was spoken, active duration is at least min_chunk_seconds,
                    # and the speaker has paused for >= early_flush_silence_seconds (500-1000ms).
                    has_min_speech = self._speech_frames_in_chunk >= int(SAMPLE_RATE * 0.20)
                    if (
                        self._silent_frames >= self._early_flush_silence_frames
                        and self._active_frames >= self._min_frames
                        and has_min_speech
                        and self._active_frames < self._frames_needed
                    ):
                        combined = np.concatenate(self._active_buffer)
                        ready.append((self._chunk_index, combined.copy(), 0.0))
                        self._active_buffer = []
                        self._active_frames = 0
                        self._silent_frames = 0
                        self._speech_frames_in_chunk = 0
                        self._is_recording_speech = False
                        self._chunk_index += 1

                    # 1b. Safety silence flush: if speech occurred and the speaker paused for >= 1.2s,
                    # flush immediately so the speech isn't delayed or padded with trailing silence.
                    elif (
                        has_min_speech
                        and self._silent_frames >= int(SAMPLE_RATE * 1.20)
                        and self._active_frames >= int(SAMPLE_RATE * 1.0)
                        and self._active_frames < self._frames_needed
                    ):
                        combined = np.concatenate(self._active_buffer)
                        ready.append((self._chunk_index, combined.copy(), 0.0))
                        self._active_buffer = []
                        self._active_frames = 0
                        self._silent_frames = 0
                        self._speech_frames_in_chunk = 0
                        self._is_recording_speech = False
                        self._chunk_index += 1

                    # 1c. Transient noise blip reset: if a noise spike triggered recording but no actual
                    # speech followed (< 0.20s speech) and silence has resumed for >= 1.0s, reset cleanly.
                    elif not has_min_speech and self._silent_frames >= int(SAMPLE_RATE * 1.0):
                        self._active_buffer = []
                        self._active_frames = 0
                        self._silent_frames = 0
                        self._speech_frames_in_chunk = 0
                        self._is_recording_speech = False

                    # 2. Hard 10-second ceiling reached:
                    # If continuous speech or long phrasing reaches 10s without a natural pause,
                    # automatically cut and send chunk to Whisper.
                    elif self._active_frames >= self._frames_needed:
                        combined = np.concatenate(self._active_buffer)
                        chunk = combined[: self._frames_needed].copy()
                        remaining = combined[self._frames_needed :]
                        ready.append((self._chunk_index, chunk, 0.0))
                        self._chunk_index += 1

                        if is_speech or self._silent_frames < self._early_flush_silence_frames:
                            # Speaker is still speaking; carry over remainder to start next chunk seamlessly
                            self._active_buffer = [remaining] if remaining.size else []
                            self._active_frames = int(remaining.size)
                            self._speech_frames_in_chunk = int(remaining.size)
                            self._silent_frames = 0
                            self._is_recording_speech = True
                        else:
                            self._active_buffer = []
                            self._active_frames = 0
                            self._silent_frames = 0
                            self._speech_frames_in_chunk = 0
                            self._is_recording_speech = False

            captured_at = time.monotonic()
            for chunk_index, chunk, leading_context_seconds in ready:
                self.on_chunk(chunk_index, chunk, captured_at, leading_context_seconds)
        except Exception as exc:
            self.on_error(f"Audio capture failed for one chunk: {exc}")



class OrderedAudioPlayer:
    def __init__(
        self,
        name: str,
        output_device_index: int | None,
        volume_getter: Callable[[], float],
        on_error: Callable[[str], None],
    ) -> None:
        self.name = name
        self.output_device_index = output_device_index
        self.volume_getter = volume_getter
        self.on_error = on_error
        self._queue: queue.Queue[bytes | None] = queue.Queue(maxsize=4)
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name=f"{self.name}-player", daemon=True)
        self._thread.start()

    def enqueue(self, audio_bytes: bytes) -> None:
        try:
            self._queue.put_nowait(audio_bytes)
        except queue.Full:
            try:
                self._queue.get_nowait()
                self._queue.put_nowait(audio_bytes)
                self.on_error(f"{self.name} audio playback is behind; dropped oldest speech to stay live.")
            except (queue.Empty, queue.Full):
                self.on_error(f"{self.name} audio queue is full; dropped one generated speech chunk.")

    def stop(self) -> None:
        self._stop.set()
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        if self._thread:
            self._thread.join(timeout=3)
        self._clear_queue()

    def _clear_queue(self) -> None:
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                return

    def _run(self) -> None:
        current_stream = None
        current_samplerate = None
        current_channels = None
        try:
            while not self._stop.is_set():
                item = self._queue.get()
                if item is None:
                    break
                try:
                    data, samplerate = sf.read(io.BytesIO(item), dtype="float32", always_2d=True)
                    channels = data.shape[1]
                    volume = max(0.0, min(1.0, self.volume_getter()))
                    data = (data * volume).astype(np.float32)

                    if (
                        current_stream is None
                        or current_samplerate != samplerate
                        or current_channels != channels
                    ):
                        if current_stream is not None:
                            try:
                                current_stream.stop()
                                current_stream.close()
                            except Exception:
                                pass
                        current_stream = sd.OutputStream(
                            samplerate=samplerate,
                            device=self.output_device_index,
                            channels=channels,
                            dtype="float32",
                        )
                        current_stream.start()
                        current_samplerate = samplerate
                        current_channels = channels

                    current_stream.write(data)
                except Exception as exc:
                    self.on_error(f"{self.name} audio playback failed: {exc}")
                    if current_stream is not None:
                        try:
                            current_stream.stop()
                            current_stream.close()
                        except Exception:
                            pass
                        current_stream = None
        finally:
            if current_stream is not None:
                try:
                    current_stream.stop()
                    current_stream.close()
                except Exception:
                    pass
