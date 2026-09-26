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
    hostapi: str = ""

    @property
    def identity(self) -> str:
        return f"{self.hostapi}|{self.name}|{self.max_input_channels}|{self.max_output_channels}"

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
                hostapi=str(sd.query_hostapis(raw["hostapi"])["name"]),
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
        self.overlap_seconds = 0.0  # Exact partition; no duplicate audio.
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
        self._raw = queue.SimpleQueue()
        self._capture_thread = None
        self._last_pause = 0
        self.last_boundary = "pause"
        self.last_continues_previous = False
        self._continues_previous = False

    def start(self) -> None:
        self._stream = sd.InputStream(
            samplerate=SAMPLE_RATE,
            device=self.device_index,
            channels=CHANNELS,
            dtype="float32",
            callback=self._callback,
            blocksize=int(SAMPLE_RATE * 0.1),
        )
        self._capture_thread = threading.Thread(target=self._capture_loop, name="audio-segmenter", daemon=True)
        self._capture_thread.start()
        try:
            self._stream.start()
        except Exception:
            self.stop()
            raise
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
        # Stop callbacks first: the last callback cannot race the final flush.
        stream, self._stream = self._stream, None
        if stream:
            try:
                stream.stop()
            finally:
                stream.close()
        if self._capture_thread:
            self._raw.put(None)
            self._capture_thread.join()
            self._capture_thread = None
        with self._lock:
            if self._active_frames:
                self._emit(self._active_frames, "stop")

    def _callback(self, indata, frames, _time_info, status) -> None:
        # No API, disk I/O, segmentation, or playback on the PortAudio callback.
        if status:
            self.on_error(f"Audio input warning (possible hardware gap): {status}")
        mono = np.asarray(indata[:, 0], dtype=np.float32).copy()
        if self._capture_thread:
            self._raw.put(mono)
        else:
            # Deterministic entry point for offline continuity tests.
            self._consume(mono)

    def _capture_loop(self):
        try:
            while True:
                try:
                    mono = self._raw.get(timeout=1.0)
                except queue.Empty:
                    if self._stream is not None and not self._stream.active:
                        self.on_error("Microphone stream stopped unexpectedly. Reconnect the device, then Stop and Start.")
                        return
                    continue
                if mono is None:
                    return
                self._consume(mono)
        except Exception as exc:
            self.on_error(f"Capture storage/segmentation failed; stop and check disk/device: {exc}")
            # Abort input rather than keep allocating indefinitely after disk failure.
            if self._stream:
                self._stream.abort()

    def _consume(self, mono):
        with self._lock:
            # 20ms analysis windows, independent of hardware callback size.
            for offset in range(0, mono.size, 320):
                frame = mono[offset:offset + 320]
                rms = float(np.sqrt(np.mean(frame * frame)))
                peak = float(np.max(np.abs(frame)))
                now = time.monotonic()
                if self.on_level and now - self._last_level_report >= 0.1:
                    self._last_level_report = now
                    self.on_level(rms, peak)
                speech = rms >= self.silence_rms_threshold or peak >= self.silence_peak_threshold
                # Any real non-zero input can be a quiet syllable. Keep it. The
                # rolling pre-roll covers the samples immediately before onset;
                # thresholds below choose boundaries only, never whether words live.
                audible = peak > 1e-7
                if not self._is_recording_speech:
                    if not audible:
                        self._pre_speech_buffer.append(frame)
                        self._pre_speech_collected += frame.size
                        while self._pre_speech_collected > self._pre_speech_frames:
                            excess = self._pre_speech_collected - self._pre_speech_frames
                            first = self._pre_speech_buffer[0]
                            take = min(excess, first.size)
                            if take == first.size:
                                self._pre_speech_buffer.pop(0)
                            else:
                                self._pre_speech_buffer[0] = first[take:]
                            self._pre_speech_collected -= take
                        continue
                    self._active_buffer = self._pre_speech_buffer
                    self._active_frames = self._pre_speech_collected
                    self._pre_speech_buffer = []
                    self._pre_speech_collected = 0
                    self._is_recording_speech = True
                self._active_buffer.append(frame)
                self._active_frames += frame.size
                self._silent_frames = 0 if speech else self._silent_frames + frame.size
                if speech:
                    self._speech_frames_in_chunk += frame.size
                elif self._silent_frames >= int(0.12 * SAMPLE_RATE):
                    self._last_pause = self._active_frames - self._silent_frames // 2
                if self._active_frames >= self._frames_needed:
                    boundary = self._frames_needed
                    kind = "forced"
                    if int(self.chunk_seconds * 0.65 * SAMPLE_RATE) <= self._last_pause < boundary:
                        boundary, kind = self._last_pause, "pause"
                    self._emit(boundary, kind)
                elif self._silent_frames >= self._early_flush_silence_frames and self._active_frames >= self._min_frames:
                    self._emit(self._active_frames, "pause")
                elif self._silent_frames >= int(1.2 * SAMPLE_RATE):
                    self._emit(self._active_frames, "pause")

    def _emit(self, boundary, kind):
        combined = np.concatenate(self._active_buffer)
        chunk, remaining = combined[:boundary].copy(), combined[boundary:].copy()
        self.last_boundary = kind
        self.last_continues_previous = self._continues_previous
        self._continues_previous = kind == "forced"
        # Consume an exact prefix. Never overlap or throw away the remainder.
        self._active_buffer = [remaining] if remaining.size else []
        self._active_frames = remaining.size
        self._is_recording_speech = bool(remaining.size) or kind == "forced"
        self._silent_frames = 0
        self._speech_frames_in_chunk = 0
        self._last_pause = 0
        captured_at = time.monotonic() - combined.size / SAMPLE_RATE
        self.on_chunk(self._chunk_index, chunk, captured_at, 0.0)
        self._chunk_index += 1



class OrderedAudioPlayer:
    def __init__(self, name, output_device_index, volume_getter, on_error,
                 backlog_path=None, on_status=None):
        from .backlog import DurableQueue
        from .config import app_data_dir
        import uuid
        self.name = name
        self.output_device_index = output_device_index
        self.volume_getter, self.on_error = volume_getter, on_error
        self.on_status = on_status or (lambda message: None)
        self._queue = DurableQueue(backlog_path or app_data_dir() / "backlog" / f"{uuid.uuid4().hex}.sqlite")
        self._thread = None

    def start(self):
        if self._thread is not None:
            raise RuntimeError("Player already started")
        self._thread = threading.Thread(target=self._run, name=f"{self.name}-player", daemon=True)
        self._thread.start()

    def enqueue(self, audio_bytes):
        if audio_bytes:
            self._queue.put(audio_bytes)

    def stop(self):
        self._queue.finish()
        if self._thread:
            self._thread.join()
        self._queue.close()

    def _run(self):
        from .reliability import safe_error
        try:
            while True:
                item = self._queue.get()
                if item is None:
                    return
                data, samplerate = sf.read(io.BytesIO(item), dtype="float32", always_2d=True)
                # stop()/context exit drains PortAudio before ack, preserving endings.
                # Never replay on write failure: a partial write may already be audible.
                with sd.OutputStream(samplerate=samplerate, device=self.output_device_index,
                                     channels=data.shape[1], dtype="float32") as stream:
                    for offset in range(0, len(data), max(1, samplerate // 10)):
                        volume = max(0.0, min(1.0, self.volume_getter()))
                        underflow = stream.write((data[offset:offset + max(1, samplerate // 10)] * volume).astype(np.float32))
                        if underflow:
                            self.on_error(f"{self.name} output underflow; check audio device/CPU load.")
                self._queue.ack()
                self.on_status(f"{self.name} playback complete; queue {self._queue.qsize()}.")
        except Exception as exc:
            self.on_error(f"{self.name} playback paused: {safe_error(exc)}. Pending audio retained at {self._queue.path}; current item may be partially played.")
