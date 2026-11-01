"""Independent durable language FIFOs mixed into one continuous PortAudio stream.

Only bounded, decoded PCM reaches the writer. SQLite, decoding/resampling and
acknowledgements run on lane workers; diagnostics run on a reporting worker.
"""
from collections import deque
from pathlib import Path
import io
import queue
import threading
import time

import av
import numpy as np
import sounddevice as sd
import soundfile as sf

from .backlog import DurableQueue
from .reliability import safe_error


def prepare_pcm(encoded, output_rate, output_channels):
    """Prepare a derivative; retain original bytes unchanged in the durable FIFO."""
    data, source_rate = sf.read(io.BytesIO(encoded), dtype='float32', always_2d=True)
    source_channels = data.shape[1]
    if not len(data) or not np.isfinite(data).all():
        raise ValueError('Empty or non-finite decoded audio')
    # Preserve a mono voice's level on both speakers. Stereo downmix uses the
    # arithmetic mean; multichannel downmix uses FFmpeg's standard layout matrix.
    if source_channels == 1 and output_channels == 2:
        data = np.repeat(data, 2, axis=1)
    elif source_channels == 2 and output_channels == 1:
        data = data.mean(axis=1, keepdims=True)
    channels = data.shape[1]
    if source_rate != output_rate or channels != output_channels:
        layouts = {1: 'mono', 2: 'stereo'}
        if channels in layouts:
            source_layout = layouts[channels]
        else:
            with av.open(io.BytesIO(encoded)) as container:
                layout = container.streams.audio[0].codec_context.layout
                if len(layout.channels) != channels or any(c.name == 'NONE' for c in layout.channels):
                    raise ValueError('Unspecified multichannel layout; original audio retained')
                source_layout = layout.name
        frame = av.AudioFrame.from_ndarray(np.ascontiguousarray(data.T), format='fltp', layout=source_layout)
        frame.sample_rate = source_rate
        converter = av.AudioResampler(format='fltp', layout=layouts[output_channels], rate=output_rate)
        frames = [*converter.resample(frame), *converter.resample(None)]
        if not frames:
            raise ValueError('Resampler returned no audio; original retained')
        data = np.concatenate([frame.to_ndarray().T for frame in frames]).astype(np.float32)
    if not np.isfinite(data).all():
        raise ValueError('Non-finite resampled audio; original retained')
    peak = float(np.max(np.abs(data)))
    normalization = 1 / max(1.0, peak)
    if normalization < 1:
        data *= normalization
    return np.ascontiguousarray(data), source_rate, source_channels, normalization


class _Lane:
    def __init__(self, language, path):
        self.language = language
        self.queue = DurableQueue(path)
        self.ready = deque()
        self.retire = deque()
        self.wake = threading.Event()
        self.exhausted = self.failed = False
        self.last_end_frame = None
        self.last_end_estimate = None
        self.gaps = deque(maxlen=120)
        self.queued_seconds = 0.0
        self.generated_seconds = self.source_seconds = 0.0


class MixedAudioPlayer:
    """One physical output; chronological speech within each language only.

    A fixed 1/N headroom gain keeps full-scale voices within PCM bounds without
    pumping, voice ducking or hard clipping. User volumes remain independent.
    """
    def __init__(self, name, output_device_index, volume_getter, on_error,
                 backlog_path, on_status=None, on_started=None, on_completed=None,
                 *, languages, samplerate=None, channels=None):
        self.name, self.output_device_index = name, output_device_index
        self.volume_getter, self.on_error = volume_getter, on_error
        self.on_status = on_status or (lambda _: None)
        self.on_started = on_started or (lambda _: None)
        self.on_completed = on_completed or (lambda _: None)
        languages = tuple(dict.fromkeys(languages))
        if not languages or name not in languages:
            raise ValueError('Mixer needs its named language lane')
        device = sd.query_devices(output_device_index, 'output') if samplerate is None or channels is None else {}
        self.samplerate = int(samplerate or device['default_samplerate'])
        self.channels = int(channels or min(2, int(device['max_output_channels'])))
        if self.samplerate <= 0 or self.channels not in (1, 2):
            raise ValueError('Output must support mono or stereo PCM')
        self.block_frames = max(1, round(self.samplerate * .020))
        self.headroom_gain = 1.0 / len(languages)
        path = Path(backlog_path)
        self._lanes = {lang: _Lane(lang, path if lang == name else path.with_name(f'playback-{lang}.sqlite'))
                       for lang in languages}
        self.volume_getters = {}
        self._condition = threading.Condition()
        self._reports = queue.SimpleQueue()
        self._report_thread = self._thread = None
        self._workers = []
        self._closing = self._drained = False
        self._abort = threading.Event()
        self._pending = deque()
        self._submitted_frames = 0
        self.overlap_frames = 0
        self.peak_output = 0.0

    def _publish(self, callback, *args):
        if self._report_thread is None:
            callback(*args)
        else:
            self._reports.put((callback, args))

    def _report_loop(self):
        while (job := self._reports.get()) is not None:
            try:
                job[0](*job[1])
            except Exception as exc:
                self.on_error(f'Mixer diagnostic failed; speech queues preserved: {safe_error(exc)}')

    def enqueue(self, audio_bytes, language=None, captured_at=0.0, sequence_id=None, source_duration=0.0, timing=None):
        if not audio_bytes:
            return
        language = language or self.name
        lane = self._lanes[language]
        try:
            info = sf.info(io.BytesIO(audio_bytes))
            duration = info.frames / info.samplerate
        except Exception:
            duration = 0.0
        inserted = lane.queue.put({'audio': audio_bytes, 'language': language,
            'captured_at': captured_at, 'sequence_id': sequence_id, 'queued_at': time.monotonic(),
            'duration': duration, 'source_duration': source_duration, 'timing': timing or {}},
            key=f'{sequence_id}:{language}' if sequence_id is not None else None)
        if not inserted:
            return
        with self._condition:
            lane.queued_seconds += duration
            lane.generated_seconds += duration
            lane.source_seconds += max(0.0, source_duration)
            pending, generated, source = lane.queued_seconds, lane.generated_seconds, lane.source_seconds
        lane.wake.set()
        self._publish(self.on_status, f'[PLAYBACK QUEUE] output=mixer language={language} '
            f'lane_audio_pending={pending:.2f}s jobs={lane.queue.qsize()} '
            'cross_language_wait=false preserves_all_content=true')
        if source:
            self._publish(self.on_status, f'[PLAYBACK LOAD] output=mixer language={language} '
                f'generated_audio={generated:.2f}s source={source:.2f}s audio_to_source_ratio={generated / source:.3f} '
                'per_language=true cross_language_overlap=true automatic_skipping=false')
        if pending >= 15:
            self._publish(self.on_status, f'[PLAYBACK BACKLOG] output=mixer language={language} '
                f'unacknowledged_audio={pending:.1f}s includes_played_portion_of_current_chunk=true '
                'other_languages_do_not_gate=true preserves_all_content=true')

    def start(self):
        if self._thread is not None:
            raise RuntimeError('Mixer already started')
        self._report_thread = threading.Thread(target=self._report_loop, name='mixer-reports', daemon=True)
        self._report_thread.start()
        self._workers = [threading.Thread(target=self._lane_loop, args=(lane,),
                         name=f'mixer-decode-{lang}', daemon=True) for lang, lane in self._lanes.items()]
        for worker in self._workers:
            worker.start()
        self._thread = threading.Thread(target=self._run, name='output-mixer', daemon=True)
        self._thread.start()

    def finish(self):
        with self._condition:
            self._closing = True
            self._condition.notify_all()
        for lane in self._lanes.values():
            lane.queue.finish()
            lane.wake.set()

    def stop(self):
        self.finish()
        if self._thread:
            self._thread.join()
        for worker in self._workers:
            worker.join()
        if not self._workers:
            for lane in self._lanes.values():
                self._pump_lane(lane)
        if self._report_thread:
            self._reports.put(None)
            self._report_thread.join()
        self.pending_at_stop = sum(lane.queue.qsize() for lane in self._lanes.values())
        for lane in self._lanes.values():
            lane.queue.close()

    def _lane_loop(self, lane):
        while not self._abort.is_set():
            lane.wake.clear()
            self._pump_lane(lane)
            if self._drained:
                return
            lane.wake.wait()

    def _pump_lane(self, lane):
        """Disk/decoder worker step, also used by deterministic offline drivers."""
        try:
            while True:
                with self._condition:
                    completed = lane.retire.popleft() if lane.retire else None
                if completed is None:
                    break
                receipt, details, duration = completed
                lane.queue.ack_claim(receipt)
                with self._condition:
                    lane.queued_seconds = max(0.0, lane.queued_seconds - duration)
                self._publish(self.on_completed, details)
            while not self._abort.is_set() and not self._drained:
                with self._condition:
                    if lane.failed or lane.exhausted or len(lane.ready) >= 2:
                        break
                try:
                    claimed = lane.queue.claim(block=False)
                except queue.Empty:
                    break
                if claimed is None:
                    with self._condition:
                        lane.exhausted = True
                        self._condition.notify_all()
                    break
                receipt, item = claimed
                decode_started = time.monotonic()
                data, rate, channels, gain = prepare_pcm(item['audio'], self.samplerate, self.channels)
                chunk = {'receipt': receipt, 'item': item, 'data': data, 'cursor': 0, 'details': None,
                         'decode_started_at': decode_started, 'audio_prepared_at': time.monotonic(),
                         'original_sample_rate': rate, 'original_channels': channels,
                         'normalization_gain': gain}
                with self._condition:
                    lane.ready.append(chunk)
                    self._condition.notify_all()
        except Exception as exc:
            with self._condition:
                lane.failed = lane.exhausted = True
                self._condition.notify_all()
            self._publish(self.on_error, f'{lane.language.upper()} mixer lane paused: {safe_error(exc)}. '
                f'Original/pending audio retained at {lane.queue.path}; other languages continue.')

    def _mix_block(self):
        """Fill each lane consecutively, then sum lanes. No I/O in this method."""
        data = np.zeros((self.block_frames, self.channels), np.float32)
        activity = np.zeros(self.block_frames, np.int8)
        starts, ends = [], []
        with self._condition:
            for lane in self._lanes.values():
                offset = 0
                volume = max(0.0, min(1.0, float(self.volume_getters.get(lane.language, self.volume_getter)())))
                while offset < self.block_frames and lane.ready:
                    chunk = lane.ready[0]
                    if chunk['cursor'] == 0:
                        chunk['start_frame'] = self._submitted_frames + offset
                        chunk['previous_end_frame'] = lane.last_end_frame
                        chunk['previous_end_estimate'] = lane.last_end_estimate
                        starts.append((lane, chunk, offset))
                    available = len(chunk['data']) - chunk['cursor']
                    count = min(self.block_frames - offset, available)
                    data[offset:offset + count] += chunk['data'][chunk['cursor']:chunk['cursor'] + count] * (volume * self.headroom_gain)
                    activity[offset:offset + count] += 1
                    chunk['cursor'] += count
                    offset += count
                    if chunk['cursor'] == len(chunk['data']):
                        lane.ready.popleft()
                        lane.last_end_frame = self._submitted_frames + offset
                        chunk['end_frame'] = lane.last_end_frame
                        ends.append((lane, chunk, offset))
                        lane.wake.set()
            self.overlap_frames += int(np.count_nonzero(activity > 1))
        self.peak_output = max(self.peak_output, float(np.max(np.abs(data))))
        return np.clip(data, -1.0, 1.0), starts, ends

    def _started_details(self, lane, chunk, offset, submitted, returned, block_start, latency):
        item = chunk['item']
        start = block_start + offset / self.samplerate
        previous = chunk['previous_end_frame']
        gap = (chunk['start_frame'] - previous) / self.samplerate if previous is not None else None
        # Prepared PCM is distinct from visible text and provider-ready bytes.
        ready = chunk['audio_prepared_at']
        previous_estimate = (block_start + (previous - self._submitted_frames) / self.samplerate
                             if previous is not None and previous >= self._submitted_frames
                             else chunk['previous_end_estimate'])
        eligible = item.get('queued_at', ready)
        unavailable = max(0.0, ready - previous_estimate) if previous_estimate is not None else 0.0
        avoidable = max(0.0, gap - unavailable) if gap is not None else None
        if gap is not None:
            lane.gaps.append(gap)
        return {**item.get('timing', {}), 'language': lane.language, 'sequence_id': item.get('sequence_id'),
            'playback_queue_entry_at': eligible, 'decode_started_at': chunk['decode_started_at'],
            'audio_prepared_at': ready, 'original_sample_rate': chunk['original_sample_rate'],
            'original_channels': chunk['original_channels'], 'output_sample_rate': self.samplerate,
            'output_channels': self.channels, 'headroom_gain': self.headroom_gain,
            'normalization_gain': chunk['normalization_gain'], 'playback_submission_at': submitted,
            'playback_write_returned_at': returned, 'playback_start_estimate': start,
            'queue_wait': max(0.0, submitted - eligible), 'audio_ready_to_start': max(0.0, start - item.get('timing', {}).get('audio_ready_at', ready)),
            'capture_to_output_start': max(0.0, start - item['captured_at']) if item.get('captured_at', 0) > 0 else None,
            'start_frame': chunk['start_frame'], 'previous_end_frame': previous,
            'previous_playback_completed_estimate': previous_estimate,
            'gap_seconds': gap, 'audio_not_ready_seconds': unavailable, 'avoidable_gap_seconds': avoidable,
            'gap_rolling_average': sum(lane.gaps) / len(lane.gaps) if lane.gaps else None,
            'gap_reason': 'first_chunk' if gap is None else 'audio_not_ready' if unavailable else 'ready_audio_transition',
            'device_latency': latency, 'measurement': 'mixed_pcm_frame_offset_plus_portaudio_latency_estimate',
            'physical_audio_verified': False, 'cross_language_overlap_allowed': True}

    def _retire(self, stream, drained=False):
        clock = stream.time
        while self._pending and (drained or (isinstance(clock, (int, float)) and self._pending[0][2] is not None and clock >= self._pending[0][2])):
            lane, chunk, _ = self._pending.popleft()
            details = {**chunk['details'], 'end_frame': chunk['end_frame'],
                'playback_completed_at': time.monotonic(),
                'completion_measurement': 'backend_drained' if drained else 'portaudio_clock_estimate'}
            with self._condition:
                lane.retire.append((chunk['receipt'], details, chunk['item'].get('duration', 0)))
            lane.wake.set()

    def _run(self):
        try:
            with self._condition:
                self._condition.wait_for(lambda: any(lane.ready for lane in self._lanes.values()) or
                    (self._closing and all(lane.exhausted for lane in self._lanes.values())))
                if not any(lane.ready for lane in self._lanes.values()):
                    return
            with sd.OutputStream(samplerate=self.samplerate, channels=self.channels, dtype='float32',
                                 device=self.output_device_index) as stream:
                previous_block_end = None
                while True:
                    with self._condition:
                        if self._closing and all(lane.exhausted and not lane.ready for lane in self._lanes.values()):
                            break
                    data, starts, ends = self._mix_block()
                    submitted = time.monotonic()
                    underflow = stream.write(data)
                    returned = time.monotonic()
                    latency = stream.latency if isinstance(stream.latency, (int, float)) else 0.0
                    block_start = max(submitted + latency, previous_block_end or 0)
                    previous_block_end = block_start + len(data) / self.samplerate
                    for lane, chunk, offset in starts:
                        chunk['details'] = self._started_details(lane, chunk, offset, submitted, returned, block_start, latency)
                        self._publish(self.on_started, dict(chunk['details']))
                        d = chunk['details']
                        self._publish(self.on_status, f'[MIXER START] language={lane.language} segment={d["sequence_id"]} '
                            f'frame={d["start_frame"]} gap={d["gap_seconds"]} avoidable={d["avoidable_gap_seconds"]} '
                            'measurement=portaudio_estimate cross_language_wait=false physical_audio_verified=false')
                        self._publish(self.on_status, f'[TIMING] segment={d["sequence_id"]} stage=playback '
                            f'language={lane.language} queue_wait={d["queue_wait"]:.4f}s '
                            f'capture_to_output_start={d["capture_to_output_start"]} '
                            'estimate=mixed_pcm_portaudio_not_physical')
                    clock = stream.time
                    for lane, chunk, offset in ends:
                        lane.last_end_estimate = block_start + offset / self.samplerate
                        chunk['details']['playback_completed_estimate'] = lane.last_end_estimate
                        deadline = clock + latency + len(data) / self.samplerate if isinstance(clock, (int, float)) else None
                        self._pending.append((lane, chunk, deadline))
                    self._submitted_frames += len(data)
                    self._retire(stream)
                    if underflow:
                        self._publish(self.on_error, 'Mixed output underflow; check audio device/CPU load. No speech was skipped.')
                stream.stop()
                self._retire(stream, drained=True)
        except Exception as exc:
            self._abort.set()
            self._publish(self.on_error, f'Mixed output paused: {safe_error(exc)}. '
                'Unacknowledged audio retained per language; current chunks may be partially played.')
        finally:
            with self._condition:
                self._drained = True
                self._condition.notify_all()
            for lane in self._lanes.values():
                lane.wake.set()
