"""Deterministic timing model with measured PCM filter durations, no APIs.

Provider/synthesis times are explicit injected assumptions. This models queue
sustainability, not measured church latency or older-PC synthesis throughput.
The separate shared-mixer verifier exercises real durable queues and workers.
"""
import argparse
import json
import math
from pathlib import Path
import statistics
import sys
import time
import os
from unittest.mock import patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from church_translator.playback_policy import DurationController, stretch_pcm


def summary(values):
    ordered = sorted(values)
    return {'average': statistics.mean(values), 'p95': ordered[math.ceil(.95 * len(values)) - 1],
            'maximum': max(values), 'last': values[-1]}


def run_service(seconds, calibrated, *, enabled, source_seconds=6, slow_yoruba=False):
    with patch.dict(os.environ, {'PLAYBACK_ADAPTIVE_ENABLED': str(enabled).lower()}):
        controls = {lang: DurationController() for lang in ('en', 'yo')}
    capture_start = stt_end = translation_end = 0.
    voice_end = {'en': 0., 'yo': 0.}
    playback_end = dict(voice_end)
    rows = {lang: {'transcript': [], 'audio_ready': [], 'playback': [], 'backlog': [],
                   'tts': [], 'rates': [], 'duration_warning': False} for lang in controls}
    seq = 0
    while capture_start < seconds:
        captured = capture_start + source_seconds
        # Nonzero provider times, occasional bounded transport delay, shared STT
        # and translation, independent CPU/network voice workers.
        stt_end = max(captured, stt_end) + 1.8 + (2.5 if seq % 113 == 100 else 0)
        translation_end = max(stt_end, translation_end) + 1.0
        for lang, control in controls.items():
            synthesis = (7.2 if slow_yoruba else 3.2) if lang == 'yo' else 1.8 + .3 * math.sin(seq)
            ready = max(translation_end, voice_end[lang]) + synthesis
            voice_end[lang] = ready
            pending = max(0, playback_end[lang] - ready)
            natural_duration = source_seconds * (1.059 if lang == 'en' else 1.04)
            decision = control.decide(natural_duration, source_seconds, pending, now=ready)
            speed = round(decision['playback_speed'], 3)
            # Filter duration measured from actual PCM, including the protected
            # tail; scale its speech portion to each modeled sentence length.
            filter_duration = natural_duration if speed == 1 else natural_duration / speed + calibrated[str(speed)]['tail_seconds']
            start = max(ready, playback_end[lang]) + .05
            playback_end[lang] = start + filter_duration
            row = rows[lang]
            for key, value in (('transcript', stt_end - capture_start), ('audio_ready', ready - capture_start),
                               ('playback', start - capture_start), ('backlog', pending), ('tts', synthesis), ('rates', speed)):
                row[key].append(value)
            row['duration_warning'] |= decision['duration_unsustainable']
        capture_start = captured + (1.0 if seq % 14 == 13 else 0)
        seq += 1
    return {lang: {key: summary(value) if isinstance(value, list) else value for key, value in row.items()}
            for lang, row in rows.items()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('report', type=Path)
    args = parser.parse_args()
    wall, cpu = time.perf_counter(), time.process_time()
    calibrated = {}
    rate = 16000
    samples = (.2 * np.sin(2 * np.pi * 440 * np.arange(6 * rate) / rate)).astype(np.float32)[:, None]
    for speed in (1.025, 1.04, 1.05, 1.06, 1.065, 1.07, 1.075, 1.08, 1.085, 1.09, 1.095, 1.10, 1.125, 1.15):
        began = time.perf_counter()
        output = stretch_pcm(samples, rate, speed)
        duration = len(output) / rate
        calibrated[str(speed)] = {'output_seconds': duration, 'tail_seconds': duration - 6 / speed,
                                   'processing_seconds': time.perf_counter() - began}
    # The controller may choose a ratio-derived target between the step values.
    class Calibration(dict):
        def __missing__(self, key):
            speed = float(key)
            output = stretch_pcm(samples, rate, speed)
            self[key] = {'output_seconds': len(output) / rate, 'tail_seconds': len(output) / rate - 6 / speed}
            return self[key]
    calibrated = Calibration(calibrated)
    scenarios = []
    for seconds in (300, 1800, 3600, 7200):
        for source_seconds, slow in ((6, False), (10, False), (6, True)):
            before = run_service(seconds, calibrated, enabled=False, source_seconds=source_seconds, slow_yoruba=slow)
            after = run_service(seconds, calibrated, enabled=True, source_seconds=source_seconds, slow_yoruba=slow)
            assert before['en']['transcript'] == after['en']['transcript']
            assert before['en']['audio_ready'] == after['en']['audio_ready']
            assert after['en']['backlog']['maximum'] < before['en']['backlog']['maximum']
            if slow:
                normal = run_service(seconds, calibrated, enabled=True, source_seconds=source_seconds)
                assert after['en'] == normal['en'], 'Slow Yoruba affected English'
            scenarios.append({'seconds': seconds, 'source_segment_seconds': source_seconds,
                'slow_yoruba': slow, 'before': before, 'after': after})
    report = {'ok': True, 'measurement': 'deterministic queue timing model with measured PCM filter durations',
        'provider_calls': 0, 'physical_audio_verified': False,
        'assumptions': {'stt_seconds': 1.8, 'translation_seconds': 1., 'english_tts_seconds': '1.8 +/- 0.3',
            'yoruba_tts_seconds': 3.2, 'slow_yoruba_tts_seconds': 7.2, 'output_latency_seconds': .05,
            'english_generated_source_ratio': 1.059, 'yoruba_generated_source_ratio': 1.04,
            'source_pause_seconds': '1 second after each 14 segments', 'injected_stt_delay': '2.5s every 113 segments'},
        'calibrated_pcm': calibrated, 'scenarios': scenarios,
        'wall_seconds': time.perf_counter() - wall, 'process_cpu_seconds': time.process_time() - cpu,
        'limits': 'Provider times and source cadence are assumptions. No local Yoruba model, live APIs, ASR accuracy or physical audio measured.'}
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps({k: report[k] for k in ('ok', 'wall_seconds', 'process_cpu_seconds', 'measurement')}))


if __name__ == '__main__':
    main()
