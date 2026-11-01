"""Deterministic offline two-language PCM mixer verification; no providers/devices."""
import argparse
import io
import json
from pathlib import Path
import sys
import tempfile
import time
from unittest.mock import patch

import numpy as np
import soundfile as sf

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from church_translator.audio_mixer import MixedAudioPlayer


def fixture(seconds, value, rate=16000, channels=1, *, compressed=False):
    audio = io.BytesIO()
    samples = np.full((round(seconds * rate), channels), value, np.float32)
    sf.write(audio, samples, rate, format='FLAC' if compressed else 'WAV', subtype='PCM_16')
    return audio.getvalue()


class MixerSimulation:
    """Finite-buffer virtual DAC, cooperatively driven disk/decoder workers.

    Running worker steps between writes makes readiness deterministic instead
    of racing virtual time against the OS's real thread scheduler.
    """
    def __init__(self, capture=True, fail_after=None):
        self.now = 100.0
        self.capture, self.fail_after = capture, fail_after
        self.blocks, self.frames, self.writes = [], 0, 0
        self.starts = self.stops = 0
        self.player = None
        self.after_write = lambda: None
        self.check_block = None

    def output(self, samplerate, channels, **kwargs):
        owner = self
        class Stream:
            latency = .02
            def __init__(self): self.end = owner.now
            @property
            def time(self): return owner.now
            def __enter__(self):
                owner.starts += 1
                return self
            def __exit__(self, *args): pass
            def write(self, data):
                if owner.fail_after is not None and owner.writes == owner.fail_after:
                    raise OSError('Simulated output disconnect')
                assert data.shape[1] == channels
                assert np.isfinite(data).all() and np.max(np.abs(data)) <= 1
                if owner.check_block is not None and owner.frames % samplerate == 0:
                    owner.check_block(data, owner.frames)
                if owner.capture: owner.blocks.append(data.copy())
                owner.frames += len(data)
                owner.writes += 1
                self.end = max(self.end, owner.now + self.latency) + len(data) / samplerate
                owner.now = max(owner.now, self.end - .06)
                owner.after_write()
                for lane in owner.player._lanes.values():
                    owner.player._pump_lane(lane)
                return False
            def stop(self):
                owner.now = max(owner.now, self.end)
                owner.stops += 1
        return Stream()

    def drive(self, player):
        self.player = player
        player.finish()
        with patch('church_translator.audio_mixer.sd.OutputStream', self.output), \
             patch('church_translator.audio_mixer.time.monotonic', lambda: self.now):
            for lane in player._lanes.values():
                player._pump_lane(lane)
            player._run()
            for lane in player._lanes.values():
                player._pump_lane(lane)


def two_hour_service(seconds=7200):
    starts, completed, errors = [], [], []
    simulation = MixerSimulation(capture=False)
    checked_blocks = [0]
    def check_unique_audio(data, frame):
        if frame % 96000 == 0:
            return  # Exclude the sentence-edge resampler filter transient.
        sequence = frame // 96000
        expected = .5 * ((.25 + sequence / 4096) + (.125 + sequence / 8192))
        np.testing.assert_allclose(data, expected, atol=1e-5, rtol=0)
        checked_blocks[0] += 1
    simulation.check_block = check_unique_audio
    with tempfile.TemporaryDirectory() as folder:
        player = MixedAudioPlayer('en', 0, lambda: 1, errors.append, Path(folder) / 'playback-en.sqlite',
            on_started=starts.append, on_completed=completed.append,
            languages=('en', 'yo'), samplerate=16000, channels=2)
        for seq in range(seconds // 6):
            for lang, rate, value in (('en', 24000, .25 + seq / 4096), ('yo', 16000, .125 + seq / 8192)):
                # FLAC is a lossless fixture that keeps the long durable test
                # small. The mixer decodes all six seconds of PCM per sentence.
                player.enqueue(fixture(6, value, rate, compressed=True), lang, sequence_id=seq)
        simulation.drive(player)
        assert not errors, errors
        count = seconds // 6
        for lang in ('en', 'yo'):
            first = [d for d in starts if d['language'] == lang]
            last = [d for d in completed if d['language'] == lang]
            assert [d['sequence_id'] for d in first] == list(range(count))
            assert [d['sequence_id'] for d in last] == list(range(count))
            assert all(d['start_frame'] == i * 96000 for i, d in enumerate(first))
            assert all(d['end_frame'] == (i + 1) * 96000 for i, d in enumerate(last))
            assert all(d['gap_seconds'] == 0 for d in first[1:])
        assert player.overlap_frames == seconds * player.samplerate
        assert all(lane.queue.qsize() == 0 for lane in player._lanes.values())
        report = {'ok': True, 'represented_seconds': seconds,
            'sentences_per_language': count, 'completed_sentences': len(completed),
            'output_frames': simulation.frames, 'overlap_frames': player.overlap_frames,
            'maximum_same_language_ready_gap_seconds': max(d['gap_seconds'] or 0 for d in starts),
            'peak_output': player.peak_output, 'stream_starts': simulation.starts, 'stream_drains': simulation.stops,
            'all_queues_drained': True, 'no_lost_duplicate_or_reordered_sentences': True,
            'unique_sentence_pcm_sum_blocks_checked': checked_blocks[0],
            'paid_requests': 0, 'measurement': 'virtual DAC, real durable queues/decoder/resampler/mixing; no physical sound'}
        player.stop()
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('report', type=Path)
    parser.add_argument('--seconds', type=int, choices=(60, 7200), default=7200)
    parser.add_argument('--silent-device', action='store_true', help='Also test muted production mixing on the default output')
    args = parser.parse_args()
    report = two_hour_service(args.seconds)
    report['wall_clock_scheduler'] = wall_clock_scheduler()
    if args.silent_device:
        report['silent_device'] = silent_device()
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report))


def wall_clock_scheduler():
    calls, starts, ends, errors = [], [], [], []
    class Stream:
        latency = .02
        time = None
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def stop(self): pass
        def write(self, data):
            began = time.perf_counter()
            time.sleep(.003)
            calls.append((began, time.perf_counter()))
            return False
    with tempfile.TemporaryDirectory() as folder:
        player = MixedAudioPlayer('en', 0, lambda: 1, errors.append, Path(folder) / 'playback-en.sqlite',
            on_started=starts.append, on_completed=ends.append,
            languages=('en', 'yo'), samplerate=48000, channels=2)
        for seq in range(30):
            for lang, rate in (('en', 24000), ('yo', 16000)):
                player.enqueue(fixture(.12, .25, rate), lang, sequence_id=seq)
        player.finish()
        with patch('church_translator.audio_mixer.sd.OutputStream', Stream):
            player.start()
            player.stop()
        assert not errors, errors
        assert len(starts) == len(ends) == 60
        assert player.pending_at_stop == 0
    steps = [calls[i][0] - calls[i - 1][1] for i in range(1, len(calls))]
    assert max(steps) < .2
    return {'sentences': 60, 'maximum_between_write_work_seconds': max(steps),
            'average_between_write_work_seconds': sum(steps) / len(steps),
            'measurement': 'real wall clock/threads/SQLite/decode/mixing, mock blocking output; not physical sound'}


def silent_device():
    starts, ends, errors = [], [], []
    with tempfile.TemporaryDirectory() as folder:
        player = MixedAudioPlayer('en', None, lambda: 0, errors.append, Path(folder) / 'playback-en.sqlite',
            on_started=starts.append, on_completed=ends.append, languages=('en', 'yo'))
        for seq in range(4):
            for lang, rate in (('en', 24000), ('yo', 16000)):
                player.enqueue(fixture(.25, .125, rate), lang, sequence_id=seq)
        player.start()
        player.stop()
        assert not errors, errors
        assert player.overlap_frames > 0
        assert len(starts) == len(ends) == 8
        assert player.pending_at_stop == 0
        maximum_gap = max(d['avoidable_gap_seconds'] or 0 for d in starts)
        assert maximum_gap < .2
        return {'sentences': 8, 'overlap_frames': player.overlap_frames,
                'samplerate': player.samplerate, 'channels': player.channels,
                'maximum_estimated_avoidable_same_language_gap_seconds': maximum_gap,
                'reported_device_latency_seconds': starts[0]['device_latency'], 'underflows': 0,
                'measurement': 'real device, digital silence, PortAudio estimates; no acoustic loopback or audible-quality proof'}


if __name__ == '__main__':
    main()
