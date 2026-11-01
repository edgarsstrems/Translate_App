"""Offline blocking-PortAudio simulation using production durable playback.

Virtual time models finite output buffering, DAC latency and expensive backend
restart. No credentials, private recording or provider is used. The optional
--silent-device check writes digital silence to the default physical output.
"""
import argparse
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import time
from unittest.mock import patch

import numpy as np
import soundfile as sf

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from church_translator.audio import OrderedAudioPlayer


class OutputSimulation:
    def __init__(self, restart_seconds=0.0, fail_write=None):
        self.now = 100.0
        self.restart_seconds = restart_seconds
        self.fail_write = fail_write
        self.streams, self.writes, self.errors = [], [], []
        self.events = []

    def output(self, samplerate, channels, **kwargs):
        owner = self

        class Stream:
            latency = .02
            def __init__(self):
                self.end = owner.now
                self.active = False
                self.starts = self.stops = 0
            @property
            def time(self):
                return owner.now
            def start(self):
                owner.now += owner.restart_seconds
                self.starts += 1
                self.active = True
            def __enter__(self):
                self.start()
                return self
            def __exit__(self, *args):
                if self.active:
                    self.stop()
            def write(self, data):
                assert self.active
                if owner.fail_write == len(owner.writes):
                    raise OSError('simulated disconnect')
                start = max(self.end, owner.now + self.latency)
                self.end = start + len(data) / samplerate
                owner.writes.append((start, self.end, data.copy()))
                # Blocking write returns as finite output buffer space permits.
                owner.now = max(owner.now, self.end - .12)
                owner.events.append('write')
                return False
            def stop(self):
                owner.now = max(owner.now, self.end)
                owner.events.append('drain')
                self.stops += 1
                self.active = False
        stream = Stream()
        self.streams.append(stream)
        return stream


def wav(seconds=.3, rate=16000, padding=0):
    samples = np.full(round(seconds * rate), .125, dtype=np.float32)
    if padding:
        samples = np.pad(samples, (padding, padding))
    output = io.BytesIO()
    sf.write(output, samples, rate, format='WAV', subtype='FLOAT')
    return output.getvalue(), samples


def simulate(player_class=OrderedAudioPlayer, seconds=12, restart_seconds=0.0):
    simulation = OutputSimulation(restart_seconds)
    starts, completions = [], []
    with tempfile.TemporaryDirectory() as folder:
        player = player_class('en', 0, lambda: 1, simulation.errors.append,
            Path(folder) / 'play.sqlite', on_started=starts.append, on_completed=completions.append)
        audio, samples = wav(.3)
        count = round(seconds / .3)
        with patch('church_translator.audio.time.monotonic', lambda: simulation.now), \
             patch('church_translator.audio.sd.OutputStream', simulation.output):
            for seq in range(count):
                player.enqueue(audio, 'en' if seq % 2 == 0 else 'yo', sequence_id=seq)
            player._queue.finish()
            player._run()
        assert not simulation.errors, simulation.errors
        assert [r['sequence_id'] for r in starts] == list(range(count))
        assert [r['sequence_id'] for r in completions] == list(range(count))
        assert player._queue.qsize() == 0
        player._queue.close()
    # Actual simulated DAC boundaries, independently of production estimates.
    gaps = [max(0, simulation.writes[i * 3][0] - simulation.writes[i * 3 - 1][1])
            for i in range(1, count)]
    assert all(np.array_equal(w[2][:, 0], samples[:len(w[2])]) for w in simulation.writes)
    return {'chunks': count, 'source_simulated_seconds': seconds,
            'maximum_ready_gap_seconds': max(gaps, default=0),
            'average_ready_gap_seconds': sum(gaps) / len(gaps) if gaps else 0,
            'stream_starts': sum(s.starts for s in simulation.streams),
            'stream_drains': sum(s.stops for s in simulation.streams),
            'ordered_complete': True, 'unchanged_pcm': True,
            'measurement': 'virtual DAC, mocked providers/backend; not physical sound'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('report', type=Path)
    parser.add_argument('--seconds', type=int, default=7200)
    parser.add_argument('--silent-device', action='store_true')
    args = parser.parse_args()
    # Load the release baseline into an isolated module, without touching disk.
    baseline = subprocess.check_output(['git', 'show', '07180a7:church_translator/audio.py'], text=True, encoding='utf-8')
    module = types.ModuleType('church_translator._playback_baseline')
    sys.modules[module.__name__] = module
    exec(compile(baseline, '<baseline-audio>', 'exec'), module.__dict__)
    report = {'ok': True, 'paid_requests': 0,
        'baseline_restart_stress': simulate(module.OrderedAudioPlayer, restart_seconds=5),
        'fixed_restart_stress': simulate(restart_seconds=5),
        'two_hour_ready_playback': simulate(seconds=args.seconds)}
    report['wall_clock_scheduler'] = wall_clock_scheduler()
    if args.silent_device:
        report['silent_physical_output'] = silent_physical_output()
    assert report['baseline_restart_stress']['maximum_ready_gap_seconds'] >= 5
    assert report['fixed_restart_stress']['maximum_ready_gap_seconds'] < .2
    assert report['two_hour_ready_playback']['maximum_ready_gap_seconds'] < .2
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report))


def wall_clock_scheduler():
    """Measure boundary work with real SQLite, decoding and thread scheduling."""
    writes, errors = [], []
    class Stream:
        latency = 0
        time = None  # No fake device-clock completion acknowledgements.
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def stop(self): pass
        def start(self): raise AssertionError('Ready stream restarted')
        def write(self, data):
            began = time.monotonic()
            time.sleep(.005)
            writes.append((began, time.monotonic()))
            return False
    with tempfile.TemporaryDirectory() as folder:
        player = OrderedAudioPlayer('en', 0, lambda: 1, errors.append, Path(folder) / 'wall.sqlite')
        audio, _ = wav(.3)
        for seq in range(60):
            player.enqueue(audio, sequence_id=seq)
        player._queue.finish()
        with patch('church_translator.audio.sd.OutputStream', return_value=Stream()):
            player.start()
            player._thread.join(10)
        assert not player._thread.is_alive()
        assert not errors, errors
        assert player._queue.qsize() == 0
        player._queue.close()
    gaps = [writes[i][0] - writes[i - 1][1] for i in range(3, len(writes), 3)]
    assert max(gaps) < .2
    return {'chunks': 60, 'maximum_boundary_work_seconds': max(gaps),
            'average_boundary_work_seconds': sum(gaps) / len(gaps),
            'measurement': 'real wall clock, real FIFO/SQLite/decode, mock blocking device; not physical sound'}


def silent_physical_output():
    results = []
    for shared in (False, True):
        starts, completions, errors = [], [], []
        with tempfile.TemporaryDirectory() as folder:
            player = OrderedAudioPlayer('en', None, lambda: 0, errors.append, Path(folder) / 'silent.sqlite',
                on_started=starts.append, on_completed=completions.append)
            for seq in range(8):
                rate = 16000 if shared and seq % 2 else 24000
                audio = io.BytesIO()
                sf.write(audio, np.zeros(rate // 4, np.float32), rate, format='WAV')
                player.enqueue(audio.getvalue(), 'yo' if shared and seq % 2 else 'en', sequence_id=seq)
            player.start()
            player.stop()
            assert player.pending_at_stop == 0
        assert not errors, errors
        assert [r['sequence_id'] for r in starts] == list(range(8))
        assert [r['sequence_id'] for r in completions] == list(range(8))
        gap = max(r['avoidable_gap_seconds'] for r in starts[1:])
        assert gap < .2, gap
        results.append({'configuration': 'English/Yoruba shared' if shared else 'English only',
            'chunks': len(starts), 'maximum_estimated_avoidable_gap_seconds': gap,
            'reported_device_latency_seconds': starts[0]['device_latency'], 'underflows': 0,
            'measurement': 'digital silence, real device, PortAudio estimates; no audible quality/loopback proof'})
    return results


if __name__ == '__main__':
    main()
