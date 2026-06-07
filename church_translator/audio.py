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
    ) -> None:
        self.device_index = device_index
        self.chunk_seconds = chunk_seconds
        self.min_chunk_seconds = max(1.0, min(min_chunk_seconds, chunk_seconds))
        self.early_flush_silence_seconds = max(0.0, early_flush_silence_seconds)
        self.silence_rms_threshold = silence_rms_threshold
        self.silence_peak_threshold = silence_peak_threshold
        self.overlap_seconds = max(0.0, min(overlap_seconds, chunk_seconds - 0.25))
        self.on_chunk = on_chunk
        self.on_error = on_error
        self.on_level = on_level
        self.on_started = on_started
        self._buffer: list[np.ndarray] = []
        self._frames_needed = int(SAMPLE_RATE * chunk_seconds)
        self._min_frames = int(SAMPLE_RATE * self.min_chunk_seconds)
        self._early_flush_silence_frames = int(SAMPLE_RATE * self.early_flush_silence_seconds)
        self._overlap_frames = int(SAMPLE_RATE * self.overlap_seconds)
        self._hop_frames = max(1, self._frames_needed - self._overlap_frames)
        self._frames_collected = 0
        self._silent_frames = 0
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
        with self._lock:
            stream = self._stream
            self._stream = None
        if stream:
            stream.stop()
            stream.close()

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
            ready: list[tuple[int, np.ndarray, float]] = []
            with self._lock:
                self._buffer.append(mono)
                self._frames_collected += frames
                if rms < self.silence_rms_threshold and peak < self.silence_peak_threshold:
                    self._silent_frames += frames
                else:
                    self._silent_frames = 0
                if (
                    self._overlap_frames == 0
                    and self._early_flush_silence_frames > 0
                    and self._frames_collected >= self._min_frames
                    and self._silent_frames >= self._early_flush_silence_frames
                    and self._frames_collected < self._frames_needed
                ):
                    combined = np.concatenate(self._buffer)
                    ready.append((self._chunk_index, combined.copy(), 0.0))
                    self._buffer = []
                    self._frames_collected = 0
                    self._silent_frames = 0
                    self._chunk_index += 1
                while self._frames_collected >= self._frames_needed:
                    combined = np.concatenate(self._buffer)
                    chunk = combined[: self._frames_needed].copy()
                    remaining = combined[self._hop_frames :]
                    self._buffer = [remaining] if remaining.size else []
                    self._frames_collected = int(remaining.size)
                    self._silent_frames = min(self._silent_frames, self._frames_collected)
                    leading_context_seconds = self.overlap_seconds if self._chunk_index > 0 else 0.0
                    ready.append((self._chunk_index, chunk, leading_context_seconds))
                    self._chunk_index += 1
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
        while not self._stop.is_set():
            item = self._queue.get()
            if item is None:
                break
            try:
                data, samplerate = sf.read(io.BytesIO(item), dtype="float32", always_2d=True)
                volume = max(0.0, min(1.0, self.volume_getter()))
                data = data * volume
                with sd.OutputStream(
                    samplerate=samplerate,
                    device=self.output_device_index,
                    channels=data.shape[1],
                    dtype="float32",
                ) as stream:
                    stream.write(data)
            except Exception as exc:
                self.on_error(f"{self.name} audio playback failed: {exc}")
