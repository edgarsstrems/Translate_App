from __future__ import annotations

import io
import math
import shutil
import queue
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass
from typing import Callable

import numpy as np
import av
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


class DynamicBoundaryPolicy:
    """Energy-based phrase boundaries without an extra recognition request.

    Keep a quiet speaker's syllables above a conservative relative threshold.
    Longer phrases can release sooner; brief pauses in short phrases wait longer.
    Silence is a practical proxy for sentence completion, not grammar detection.
    """
    def __init__(self, rms_threshold, peak_threshold):
        self.rms_threshold = rms_threshold
        self.peak_threshold = peak_threshold
        self.speech_level = max(.002, rms_threshold * 4)
        self.phrase_seconds = 4.0

    def is_speech(self, rms, peak):
        threshold = min(self.rms_threshold, max(.0002, self.speech_level * .12))
        speech = rms >= threshold or peak >= min(self.peak_threshold, threshold * 5)
        if speech:
            self.speech_level = .97 * self.speech_level + .03 * rms
        elif rms >= .0003:
            # A sustained quiet onset gradually lowers the relative threshold.
            self.speech_level = .9 * self.speech_level + .1 * rms
        return speech

    def pause_seconds(self, phrase_duration):
        base = .76 if phrase_duration < 2.5 else (.70 if phrase_duration < 4.5 else .66)
        cadence = max(-.02, min(.04, (self.phrase_seconds - 4.0) * .02))
        return max(.64, min(.80, base + cadence))

    def observe_phrase(self, duration):
        self.phrase_seconds = .8 * self.phrase_seconds + .2 * min(8.0, duration)


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
        dynamic_mode: bool = False,
        dynamic_soft_seconds: float = 6.0,
        on_capture=None,
        capture_audit=None,
        on_stream_ready=None,
        streaming_mode=False,
    ) -> None:
        self.device_index = device_index
        # Hard ceiling of 10.0 seconds maximum chunk duration
        self.chunk_seconds = min(10.0, max(1.5, float(chunk_seconds)))
        self.min_chunk_seconds = max(0.8, min(min_chunk_seconds, self.chunk_seconds))
        # Post-speech pause / sentence boundary detection threshold (default ~0.45s, low latency ~0.30s)
        self.early_flush_silence_seconds = max(0.20, min(1.20, float(early_flush_silence_seconds)))
        self.silence_rms_threshold = silence_rms_threshold
        self.silence_peak_threshold = silence_peak_threshold
        # Logical cores stay an exact partition. Timestamp-capable recognizers
        # may request a shared pre/post context window around those cores.
        self.overlap_seconds = min(.7, max(0.0, float(overlap_seconds)))
        self.overlap_seconds = min(self.overlap_seconds, (self.chunk_seconds - .8) / 2)
        self._context_frames = int(self.overlap_seconds * SAMPLE_RATE)
        self._pending_boundary = None
        self.pre_speech_seconds = max(0.20, min(0.60, float(pre_speech_seconds)))
        self.on_chunk = on_chunk
        self.on_error = on_error
        self.on_level = on_level
        self.on_started = on_started
        self.on_capture = on_capture
        self.on_stream_ready = on_stream_ready
        self.streaming_mode = streaming_mode
        self._journal = capture_audit
        self._journal_thread = None
        self._journal_done = threading.Event()
        self._journal_messages = queue.SimpleQueue()
        self._storage_stopped = False
        self._storage_checked_at = -1
        self._journal_ready = threading.Condition()
        self._journaled_samples = 0
        self._overflow_packet = None
        self._failed_journal_packets = []
        self._capacity_aborts = 0
        self._overruns = 0
        self._capture_started_at = None
        self._input_started = False
        self.capture_id = uuid.uuid4().hex
        self._received_samples = self._consumed_samples = self._emitted_samples = 0
        self._last_adc_end = None
        self._adc_clock_warning_reported = False
        self._dropped_samples = 0
        self.last_capture = None
        self.dynamic_mode = dynamic_mode
        self.dynamic_soft_seconds = min(self.chunk_seconds, max(1.5, dynamic_soft_seconds))
        self._dynamic_policy = DynamicBoundaryPolicy(silence_rms_threshold, silence_peak_threshold)

        # Frame limits
        self._frames_needed = int(SAMPLE_RATE * self.chunk_seconds) - 2 * self._context_frames
        self._min_frames = min(self._frames_needed, int(SAMPLE_RATE * self.min_chunk_seconds))
        self._early_flush_silence_frames = int(SAMPLE_RATE * self.early_flush_silence_seconds)
        self._pre_speech_frames = int(SAMPLE_RATE * self.pre_speech_seconds)
        self._overlap_frames = int(SAMPLE_RATE * self.overlap_seconds)
        self._hop_frames = self._frames_needed

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
        # At most one minute of callback frames in memory; normal processing
        # drains to the durable disk queue independently of every API stage.
        self._raw = queue.Queue(maxsize=600)
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
        actual_rate = self._stream.samplerate
        if isinstance(actual_rate, (int, float)) and abs(actual_rate - SAMPLE_RATE) > .1:
            self._stream.close()
            self._stream = None
            raise RuntimeError(f'Input sample rate mismatch: expected {SAMPLE_RATE}, got {actual_rate}.')
        self._start_workers()
        try:
            self._stream.start()
            self._input_started = True
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
        self._input_started = False
        if stream:
            try:
                stream.stop()
            finally:
                stream.close()
        if self._journal_thread:
            while self._journal_thread.is_alive():
                try:
                    self._raw.put(None, timeout=.1)
                    break
                except queue.Full:
                    continue
            self._journal_thread.join()
            self._capture_thread.join()
            self._journal_thread = None
            self._capture_thread = None
        elif self._capture_thread:
            # A disk/segmentation failure may already have ended the consumer.
            # Never block shutdown trying to append to its full bounded queue.
            while self._capture_thread.is_alive():
                try:
                    self._raw.put(None, timeout=.1)
                    break
                except queue.Full:
                    continue
            self._capture_thread.join()
            self._capture_thread = None
        try:
            with self._lock:
                if self._active_frames:
                    self._emit(self._active_frames, "stop")
        finally:
            if self._journal:
                self._journal.event('capture_stopped', None, self.integrity())
        if self._dropped_samples:
            self.on_error(f'Capture queue capacity exceeded: {self._dropped_samples} callback samples lost. '
                          'Input aborted; previously journaled audio is recoverable. Check storage and restart.')

    def integrity(self):
        return {'capture_id': self.capture_id, 'captured_samples': self._received_samples,
                'journaled_samples': self._journaled_samples, 'segmented_samples': self._emitted_samples,
                'capture_overruns': self._overruns, 'capacity_aborts': self._capacity_aborts,
                'lost_callback_samples': self._dropped_samples}

    def _start_workers(self):
        if self._journal is None:
            from .config import app_data_dir
            from .speech_audit import SpeechAudit
            self._journal = SpeechAudit(app_data_dir() / 'backlog' / self.capture_id / 'speech-audit.sqlite')
        self._journal_thread = threading.Thread(target=self._journal_loop, name='audio-journal', daemon=True)
        self._capture_thread = threading.Thread(target=self._capture_loop, name='audio-segmenter', daemon=True)
        self._journal_thread.start()
        self._capture_thread.start()

    def _journal_loop(self):
        # Only this worker drains callback memory. Segmentation, UI and all
        # provider work may stall while original PCM continues to reach disk.
        packets = []
        try:
            while True:
                try:
                    packet = self._raw.get(timeout=.2)
                except queue.Empty:
                    if self._storage_stopped:
                        break
                    if self._overflow_packet is None:
                        if self._input_started and self._stream is not None and not self._stream.active:
                            self._journal_messages.put('Microphone stream stopped unexpectedly. Previously captured audio is retained; Stop and Start to reconnect.')
                            break
                        continue
                    packet = None
                if packet is None:
                    packet = self._overflow_packet
                    if packet is None:
                        break
                    self._overflow_packet = None
                    final = True
                else:
                    final = False
                packets = [packet]
                # Commit already-waiting callbacks together, without a batching
                # timer. At most two seconds extra PCM sits outside the queue.
                if not final:
                    for _ in range(19):
                        try:
                            extra = self._raw.get_nowait()
                        except queue.Empty:
                            break
                        if extra is None:
                            if self._overflow_packet is not None:
                                packets.append(self._overflow_packet)
                                self._overflow_packet = None
                            final = True
                            break
                        packets.append(extra)
                self._journal.capture_packets(self.capture_id, packets)
                start, mono, details = packets[-1]
                packets = []
                with self._journal_ready:
                    self._journaled_samples = start + len(mono)
                    self._journal_ready.notify_all()
                # Disk work is off the callback. Commit received PCM first, then
                # stop future input with a reserve while draining queued blocks.
                if (start - self._storage_checked_at >= 30 * SAMPLE_RATE or self._storage_checked_at < 0):
                    self._storage_checked_at = start
                    try:
                        free = shutil.disk_usage(self._journal.path.parent).free
                    except OSError as exc:
                        free = None
                        self._journal_messages.put(f'Cannot check audio storage reserve: {exc}')
                    if free is not None and free < 256 * 1024 ** 2 and not self._storage_stopped:
                        self._storage_stopped = True
                        self._journal_messages.put('Audio storage is almost full. Input stopped with a 256 MiB reserve; '
                                                   'received audio is being preserved. Export/recover audio and free disk space.')
                        if self._stream:
                            self._stream.abort()
                    elif free is not None and free < 2 * 1024 ** 3:
                        self._journal_messages.put('Low audio storage: less than 2 GiB free. A two-hour service with queues '
                                                   'and exact STT diagnostics may need this space. Pending speech is never auto-deleted.')
                if final:
                    if self._capacity_aborts:
                        self._journal_messages.put('Capture storage could not keep up: input stopped at the bounded memory limit. '
                                      'The overflow packet was retained; recording after this stop is unavailable. Check storage and restart.')
                    break
        except Exception as exc:
            # Keep the uncommitted batch in bounded memory for explicit retry;
            # the queued tail and overflow reserve also remain intact.
            self._failed_journal_packets = packets
            self._journal_messages.put(f'Original audio storage failed; input stopped: {exc}')
            if self._stream:
                self._stream.abort()
        finally:
            self._journal_done.set()
            with self._journal_ready:
                self._journal_ready.notify_all()

    def update_parameters(
        self,
        chunk_seconds: float,
        min_chunk_seconds: float,
        early_flush_silence_seconds: float,
        silence_rms_threshold: float | None = None,
        silence_peak_threshold: float | None = None,
        dynamic_mode: bool | None = None,
        dynamic_soft_seconds: float = 6.0,
    ) -> None:
        """Dynamically and thread-safely update chunking parameters without dropping buffered speech."""
        with self._lock:
            if dynamic_mode is not None:
                self.dynamic_mode = dynamic_mode
            self.chunk_seconds = min(10.0, max(1.5, float(chunk_seconds)))
            self.overlap_seconds = min(self.overlap_seconds, (self.chunk_seconds - .8) / 2)
            self._context_frames = int(self.overlap_seconds * SAMPLE_RATE)
            self.dynamic_soft_seconds = min(self.chunk_seconds, max(1.5, dynamic_soft_seconds))
            self.min_chunk_seconds = max(0.8, min(float(min_chunk_seconds), self.chunk_seconds))
            self.early_flush_silence_seconds = max(0.20, min(1.20, float(early_flush_silence_seconds)))
            if silence_rms_threshold is not None:
                self.silence_rms_threshold = float(silence_rms_threshold)
            if silence_peak_threshold is not None:
                self.silence_peak_threshold = float(silence_peak_threshold)
            self._frames_needed = int(SAMPLE_RATE * self.chunk_seconds) - 2 * self._context_frames
            self._min_frames = min(self._frames_needed, int(SAMPLE_RATE * self.min_chunk_seconds))
            self._early_flush_silence_frames = int(SAMPLE_RATE * self.early_flush_silence_seconds)
            self._hop_frames = self._frames_needed
            if dynamic_mode is not None:
                self.dynamic_mode = dynamic_mode
            self._dynamic_policy.rms_threshold = self.silence_rms_threshold
            self._dynamic_policy.peak_threshold = self.silence_peak_threshold

    def _callback(self, indata, frames, _time_info, status) -> None:
        # No API, disk I/O, segmentation, or playback on the PortAudio callback.
        mono = np.asarray(indata[:, 0], dtype=np.float32).copy()
        start = self._received_samples
        self._received_samples += mono.size
        if self._capture_started_at is None:
            self._capture_started_at = time.monotonic() - mono.size / SAMPLE_RATE
        adc = getattr(_time_info, 'inputBufferAdcTime', None)
        details = {'frames': frames, 'samples': mono.size, 'status': str(status) if status else '',
                   'sample_rate': SAMPLE_RATE, 'channels': 1, 'channel_selection': 'device_mono',
                   'duration_seconds': mono.size / SAMPLE_RATE,
                   'block_id': f'{self.capture_id}:{start}:{start + mono.size}',
                   'captured_at': self._capture_started_at + start / SAMPLE_RATE,
                   'wall_time': time.time(), 'adc_time': adc, 'adc_gap_seconds': None}
        if status and getattr(status, 'input_overflow', False):
            self._overruns += 1
        # Some Windows host APIs return zero when ADC timing is unavailable.
        # That is not a measured gap (nor evidence of missing callback audio).
        valid_adc = isinstance(adc, (int, float)) and math.isfinite(adc) and adc > 0
        if valid_adc and self._last_adc_end is not None:
            details['adc_gap_seconds'] = adc - self._last_adc_end
        self._last_adc_end = adc + mono.size / SAMPLE_RATE if valid_adc else None
        if self._capture_thread:
            try:
                self._raw.put_nowait((start, mono, details))
            except queue.Full:
                # One reserved packet protects the callback that filled memory.
                # Abort stops further input; never overwrite earlier audio.
                if self._overflow_packet is None:
                    self._overflow_packet = (start, mono, details)
                else:
                    self._dropped_samples += mono.size
                self._capacity_aborts += 1
                raise sd.CallbackAbort
        else:
            # Deterministic entry point for offline continuity tests.
            self._consume(mono, start, details)

    def _capture_loop(self):
        try:
            while True:
                while not self._journal_messages.empty():
                    self.on_error(self._journal_messages.get())
                if self._journal_thread is not None:
                    with self._journal_ready:
                        while self._consumed_samples >= self._journaled_samples and not self._journal_done.is_set():
                            self._journal_ready.wait(.2)
                        if self._consumed_samples >= self._journaled_samples and self._journal_done.is_set():
                            return
                    for start, mono, details in self._journal.packets(self.capture_id, self._consumed_samples):
                        self._consume(mono, start, details)
                    continue
                try:
                    packet = self._raw.get(timeout=1.0)
                except queue.Empty:
                    if self._stream is not None and not self._stream.active:
                        self.on_error("Microphone stream stopped unexpectedly. Reconnect the device, then Stop and Start.")
                        return
                    continue
                if packet is None:
                    return
                start, mono, details = packet
                self._consume(mono, start, details)
        except Exception as exc:
            self.on_error(f"Capture storage/segmentation failed; stop and check disk/device: {exc}")
            # Abort input rather than keep allocating indefinitely after disk failure.
            if self._stream:
                self._stream.abort()
        finally:
            while not self._journal_messages.empty():
                self.on_error(self._journal_messages.get())

    def _consume(self, mono, sample_start=None, details=None):
        start = self._consumed_samples if sample_start is None else sample_start
        self._consume_packet(mono, sample_start, details)
        # This runs only on the segmentation worker, after journal commit and
        # boundary publication. The callback and journal worker never do network
        # work. It advertises a disk-backed range, not another PCM queue.
        if self.on_stream_ready:
            self.on_stream_ready(self.capture_id, start, start + len(mono), details or {})

    def _consume_packet(self, mono, sample_start=None, details=None):
        with self._lock:
            sample_start = self._consumed_samples if sample_start is None else sample_start
            if sample_start != self._consumed_samples:
                raise RuntimeError(f'Capture continuity gap/overlap: expected {self._consumed_samples}, got {sample_start}')
            details = details or {'sample_rate': SAMPLE_RATE}
            if self.on_capture:
                self.on_capture(self.capture_id, sample_start, mono, details)
            if details.get('status') or details.get('frames', mono.size) != mono.size:
                self.on_error(f'Audio input warning (possible hardware gap): {details}')
            gap = details.get('adc_gap_seconds')
            if gap is not None and gap > 2 / SAMPLE_RATE:
                self.on_error(f'Audio ADC timing gap: {gap:.6f}s; possible hardware loss cannot be reconstructed.')
            elif gap is not None and gap < -2 / SAMPLE_RATE and not self._adc_clock_warning_reported:
                self._adc_clock_warning_reported = True
                self.on_error('Audio ADC clock moved backwards or repeated; timing is unreliable. '
                              'This alone does not establish missing audio; callback samples are preserved.')
            self._consumed_samples += mono.size
            if self._capture_thread is None:
                self._received_samples = max(self._received_samples, self._consumed_samples)
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
                if self.dynamic_mode:
                    speech = self._dynamic_policy.is_speech(rms, peak)
                # Exact partition including zeros and very quiet onsets. Energy
                # chooses boundaries only; every sample also has a raw journal.
                self._is_recording_speech = True
                self._active_buffer.append(frame)
                self._active_frames += frame.size
                self._silent_frames = 0 if speech else self._silent_frames + frame.size
                if speech:
                    self._speech_frames_in_chunk += frame.size
                elif self._silent_frames >= int((0.22 if self.dynamic_mode else 0.12) * SAMPLE_RATE):
                    self._last_pause = self._active_frames - self._silent_frames // 2
                if self._pending_boundary is not None:
                    boundary, kind = self._pending_boundary
                    if self._active_frames >= boundary + self._context_frames:
                        self._emit(boundary, kind)
                    continue
                if self._active_frames >= self._frames_needed:
                    boundary = self._active_frames if self.streaming_mode else self._frames_needed
                    kind = "forced"
                    pause_minimum = (int(self.dynamic_soft_seconds * SAMPLE_RATE) if self.dynamic_mode
                                     else max(self._min_frames, int(self.chunk_seconds * .65 * SAMPLE_RATE)))
                    if not self.streaming_mode and pause_minimum <= self._last_pause < boundary:
                        boundary, kind = self._last_pause, "pause"
                    self._emit(boundary, kind)
                elif self._silent_frames >= (int(self._dynamic_policy.pause_seconds(
                        (self._active_frames - self._silent_frames) / SAMPLE_RATE) * SAMPLE_RATE)
                        if self.dynamic_mode else self._early_flush_silence_frames) and self._active_frames >= self._min_frames:
                    self._emit(self._active_frames, "pause")
                elif (self.dynamic_mode and self._active_frames >= int(self.dynamic_soft_seconds * SAMPLE_RATE)
                      and self._silent_frames >= int(.32 * SAMPLE_RATE)):
                    # After the soft target, take a real breath rather than cut
                    # an active word. The hard limit still bounds continuous speech.
                    self._emit(self._active_frames, "pause")
                elif (self.dynamic_mode and self._speech_frames_in_chunk > 0 and
                      self._silent_frames >= int(1.2 * SAMPLE_RATE)):
                    self._emit(self._active_frames, "pause")

    def _emit(self, boundary, kind):
        if self._context_frames and kind != 'stop' and self._active_frames < boundary + self._context_frames:
            self._pending_boundary = (boundary, kind)
            return
        self._pending_boundary = None
        if self.dynamic_mode and kind == "pause" and self._speech_frames_in_chunk:
            self._dynamic_policy.observe_phrase((boundary - self._silent_frames) / SAMPLE_RATE)
        combined = np.concatenate(self._active_buffer)
        chunk, remaining = combined[:boundary].copy(), combined[boundary:].copy()
        self.last_boundary = kind
        self.last_capture = {'capture_id': self.capture_id,
                             'start_sample': self._emitted_samples,
                             'end_sample': self._emitted_samples + len(chunk),
                             'sample_rate': SAMPLE_RATE, 'overlap_samples': 0}
        if self._context_frames:
            request_start = max(0, self._emitted_samples - self._context_frames)
            request_end = self._emitted_samples + len(chunk) + min(self._context_frames, remaining.size)
            self.last_capture.update(request_start_sample=request_start, request_end_sample=request_end,
                request_overlap_samples=(self._emitted_samples - request_start) +
                                        (request_end - self._emitted_samples - len(chunk)))
        self.last_continues_previous = self._continues_previous
        self._continues_previous = kind == "forced"
        # Consume an exact prefix. Never overlap or throw away the remainder.
        self._active_buffer = [remaining] if remaining.size else []
        self._active_frames = remaining.size
        self._is_recording_speech = bool(remaining.size) or kind == "forced"
        self._silent_frames = 0
        self._speech_frames_in_chunk = 0
        self._last_pause = 0
        captured_at = (self._capture_started_at + self._emitted_samples / SAMPLE_RATE
                       if self._capture_started_at is not None else time.monotonic() - combined.size / SAMPLE_RATE)
        # Commit downstream before consuming the prefix. On storage failure,
        # both the active buffer and original callback journal remain available.
        try:
            self.on_chunk(self._chunk_index, chunk, captured_at, 0.0)
        except Exception:
            self._active_buffer = [combined]
            self._active_frames = combined.size
            raise
        self._emitted_samples += len(chunk)
        self._chunk_index += 1



class OrderedAudioPlayer:
    def __init__(self, name, output_device_index, volume_getter, on_error,
                 backlog_path=None, on_status=None, on_started=None, on_completed=None):
        from .backlog import DurableQueue
        from .config import app_data_dir
        import uuid
        self.name = name
        self.output_device_index = output_device_index
        self.volume_getter, self.on_error = volume_getter, on_error
        self.on_status = on_status or (lambda message: None)
        self.on_started = on_started or (lambda details: None)
        self.on_completed = on_completed or (lambda details: None)
        self._queue = DurableQueue(backlog_path or app_data_dir() / "backlog" / f"{uuid.uuid4().hex}.sqlite")
        self._thread = None
        self.volume_getters = {}
        self._duration_lock = threading.Lock()
        self._queued_seconds = 0.0
        self._playing_started_at = None
        self._playing_duration = 0.0
        self._generated_audio_seconds = 0.0
        self._source_seconds = 0.0
        self._last_source_sequence = None
        self._gap_history = deque(maxlen=120)

    def start(self):
        if self._thread is not None:
            raise RuntimeError("Player already started")
        self._thread = threading.Thread(target=self._run, name=f"{self.name}-player", daemon=True)
        self._thread.start()

    def enqueue(self, audio_bytes, language=None, captured_at=0.0, sequence_id=None, source_duration=0.0, timing=None):
        if audio_bytes:
            try:
                info = sf.info(io.BytesIO(audio_bytes))
                duration = info.frames / info.samplerate
            except Exception:
                duration = 0.0  # Decode failure is retained by the playback worker.
            with self._duration_lock:
                inserted = self._queue.put({'audio': audio_bytes, 'language': language or self.name,
                             'captured_at': captured_at, 'sequence_id': sequence_id,
                             'timing': timing or {},
                             'queued_at': time.monotonic(), 'duration': duration},
                             key=f'{sequence_id}:{language or self.name}' if sequence_id is not None else None)
                if not inserted:
                    return
                self._queued_seconds += duration
                self._generated_audio_seconds += duration
                if sequence_id is not None and sequence_id != self._last_source_sequence:
                    self._source_seconds += max(0.0, source_duration)
                    self._last_source_sequence = sequence_id
                pending_seconds = self._queued_seconds
                if self._playing_started_at is not None:
                    pending_seconds = max(0.0, pending_seconds - min(self._playing_duration,
                        max(0.0, time.monotonic() - self._playing_started_at)))
            self.on_status(f'[PLAYBACK QUEUE] output={self.name} audio_pending={pending_seconds:.2f}s '
                           f'jobs={self._queue.qsize()} estimate=wall_clock_remaining preserves_all_content=true')
            if self._source_seconds:
                self.on_status(f'[PLAYBACK LOAD] output={self.name} '
                    f'generated_audio={self._generated_audio_seconds:.2f}s source={self._source_seconds:.2f}s '
                    f'audio_to_source_ratio={self._generated_audio_seconds / self._source_seconds:.3f} '
                    'shared_device_languages_count_together=true automatic_skipping=false')
            if pending_seconds >= 15:
                self.on_status(f'[PLAYBACK BACKLOG] output={self.name} audio_pending={pending_seconds:.1f}s; '
                               'pending generated audio is high; cause=unverified '
                               'includes_current_segment=true preserves_all_content=true')

    def stop(self):
        self._queue.finish()
        if self._thread:
            self._thread.join()
        self.pending_at_stop = self._queue.qsize()
        self._queue.close()

    def _run(self):
        from .reliability import safe_error
        from contextlib import ExitStack
        pending = deque()
        previous_end = None
        previous_completed_at = None

        def complete(drained=False):
            nonlocal previous_completed_at
            # PortAudio's stream clock and reported output latency give a
            # conservative completion estimate. At idle/format change/Stop,
            # stop() is the actual backend drain barrier. Never ack on write.
            clock = stream.time if stream is not None else None
            while pending and (drained or (isinstance(clock, (int, float)) and
                                           pending[0][2] is not None and clock >= pending[0][2])):
                receipt, item, _, details = pending[0]
                self._queue.ack_claim(receipt)
                pending.popleft()
                previous_completed_at = time.monotonic()
                self.on_completed({**details, 'playback_completed_at': previous_completed_at,
                    'completion_measurement': 'backend_drained' if drained else 'portaudio_clock_estimate',
                    'physical_audio_verified': False})
                with self._duration_lock:
                    self._queued_seconds = max(0.0, round(self._queued_seconds - item.get('duration', 0), 9))
                    if not pending:
                        self._playing_started_at = None
                        self._playing_duration = 0.0
        try:
            with ExitStack() as streams:
                stream, format_key, active = None, None, False
                while True:
                    try:
                        claimed = self._queue.claim(block=not active)
                    except queue.Empty:
                        claimed = None
                    if claimed is None:
                        if active:
                            stream.stop()
                            active = False
                            complete(drained=True)
                            continue  # Event-driven wait, with no restart until audio exists.
                        return
                    receipt, item = claimed
                    language = item.get('language', self.name) if isinstance(item, dict) else self.name
                    audio = item['audio'] if isinstance(item, dict) else item
                    decode_start = time.monotonic()
                    data, samplerate = sf.read(io.BytesIO(audio), dtype='float32', always_2d=True)
                    decoded_at = time.monotonic()
                    # Measure only exact digital padding. Quiet speech/breaths
                    # are preserved; do not guess silence from an amplitude gate.
                    nonzero = np.flatnonzero(np.any(data != 0, axis=1))
                    leading = (int(nonzero[0]) if nonzero.size else len(data)) / samplerate
                    trailing = (len(data) - 1 - int(nonzero[-1]) if nonzero.size else 0) / samplerate
                    original_samplerate = samplerate
                    # Cloud and local voices may have different sample rates.
                    # Convert only the playback derivative to the open stream's
                    # rate, avoiding a physical-device reopen between voices.
                    if format_key and samplerate != format_key[0] and data.shape[1] == format_key[1] and data.shape[1] in (1, 2):
                        frame = av.AudioFrame.from_ndarray(np.ascontiguousarray(data.T), format='fltp',
                            layout='mono' if data.shape[1] == 1 else 'stereo')
                        frame.sample_rate = samplerate
                        resampler = av.AudioResampler(format='fltp', layout=frame.layout.name, rate=format_key[0])
                        frames = [*resampler.resample(frame), *resampler.resample(None)]
                        data = np.concatenate([f.to_ndarray().T for f in frames]).astype(np.float32)
                        samplerate = format_key[0]
                    prepared_at = time.monotonic()
                    key = (samplerate, data.shape[1])
                    if stream is None or key != format_key:
                        if active:
                            stream.stop()
                            complete(drained=True)
                        streams.close()
                        stream = streams.enter_context(sd.OutputStream(samplerate=samplerate,
                            device=self.output_device_index, channels=data.shape[1], dtype='float32'))
                        format_key = key
                        active = True
                    elif not active:
                        stream.start()
                        active = True
                    display_name = 'English' if language == 'en' else language.upper()
                    with self._duration_lock:
                        self._playing_started_at = time.monotonic()
                        self._playing_duration = len(data) / samplerate
                    details = None
                    for offset in range(0, len(data), max(1, samplerate // 10)):
                        volume = max(0.0, min(1.0, self.volume_getters.get(language, self.volume_getter)()))
                        submitted_at = time.monotonic()
                        underflow = stream.write((data[offset:offset + max(1, samplerate // 10)] * volume).astype(np.float32))
                        if offset == 0:
                            latency = stream.latency if isinstance(stream.latency, (int, float)) else 0.0
                            start_estimate = max(submitted_at + latency, previous_end or 0)
                            gap = max(0.0, start_estimate - previous_end) if previous_end is not None else None
                            eligible = item.get('queued_at', submitted_at)
                            unavailable = max(0.0, eligible - previous_end) if previous_end is not None else 0.0
                            avoidable = max(0.0, gap - unavailable) if gap is not None else None
                            if gap is not None:
                                self._gap_history.append(gap)
                            details = {'language': language, 'sequence_id': item.get('sequence_id') if isinstance(item, dict) else None,
                                'queue_wait': max(0.0, submitted_at - item.get('queued_at', submitted_at)) if isinstance(item, dict) else 0.0,
                                'capture_to_output_start': (max(0.0, submitted_at + latency - item['captured_at'])
                                    if isinstance(item, dict) and item.get('captured_at', 0) > 0 else None),
                                'device_latency': latency, 'measurement': 'first_successful_write_plus_device_latency',
                                **item.get('timing', {}),
                                'playback_queue_entry_at': item.get('queued_at'),
                                'decode_started_at': decode_start, 'decoded_at': decoded_at,
                                'audio_prepared_at': prepared_at, 'original_sample_rate': original_samplerate,
                                'output_sample_rate': samplerate,
                                'leading_digital_zero_seconds': leading, 'trailing_digital_zero_seconds': trailing,
                                'previous_playback_completed_estimate': previous_end,
                                'previous_playback_completion_observed_at': previous_completed_at,
                                'playback_submission_at': submitted_at, 'playback_start_estimate': start_estimate,
                                'playback_write_returned_at': time.monotonic(),
                                'gap_seconds': gap, 'audio_not_eligible_seconds': unavailable,
                                'avoidable_gap_seconds': avoidable,
                                'gap_rolling_average': sum(self._gap_history) / len(self._gap_history) if self._gap_history else None,
                                'gap_reason': ('first_chunk' if gap is None else
                                    'audio_not_ready_or_shared_order' if unavailable else 'ready_audio_transition'),
                                'audio_ready_to_start': max(0.0, start_estimate - item.get('timing', {}).get('audio_ready_at', eligible)),
                                'physical_audio_verified': False}
                            if gap is not None:
                                self.on_status(f'[PLAYBACK GAP] segment={details["sequence_id"]} language={language} '
                                    f'gap={gap:.4f}s avoidable={avoidable:.4f}s '
                                    f'rolling_average={details["gap_rolling_average"]:.4f}s reason={details["gap_reason"]} '
                                    'measurement=portaudio_estimate physical_audio_verified=false')
                            self.on_status(f'[PLAYBACK] {display_name} started')
                            self.on_status(f'[TIMING] segment={details["sequence_id"]} stage=playback '
                                f'language={language} queue_wait={details["queue_wait"]:.2f}s '
                                f'capture_to_output_start={details["capture_to_output_start"]} '
                                f'device_latency={latency:.3f}s estimate=portaudio_not_physical')
                            self.on_started(details)
                        if underflow:
                            self.on_error(f'{self.name} output underflow; check audio device/CPU load.')
                        complete()
                    if details is None:
                        raise RuntimeError('Decoded audio has no frames')
                    # Keep writing ready same-format chunks into the active
                    # stream. Retain each row beyond the last submitted block
                    # plus reported output latency; idle stop drains the tail.
                    clock = stream.time
                    deadline = (clock + latency + min(len(data), max(1, samplerate // 10)) / samplerate
                                if isinstance(clock, (int, float)) else None)
                    previous_end = start_estimate + len(data) / samplerate
                    details['playback_completed_estimate'] = previous_end
                    pending.append((receipt, item, deadline, details))
        except Exception as exc:
            self.on_error(f"{self.name} playback paused: {safe_error(exc)}. Pending audio retained at {self._queue.path}; current item may be partially played.")
