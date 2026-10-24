"""Accelerated two-hour segmentation soak with streaming sample hashes."""
import hashlib
import json
import sys
import time
import argparse
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from church_translator.audio import ChunkRecorder
from church_translator.config import get_speed_profile_settings

expected, actual = hashlib.sha256(), hashlib.sha256()
count = frames = maximum = 0
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('report', type=Path)
parser.add_argument('--mode', choices=('balanced', 'low_latency', 'dynamic'), default='balanced')
parser.add_argument('--dynamic', action='store_true', help='Compatibility alias for --mode dynamic')
args = parser.parse_args()
profile = get_speed_profile_settings('dynamic' if args.dynamic else args.mode)
dynamic = profile['mode'] == 'dynamic'
max_frames = int(profile['chunk_seconds'] * 16000)


def emit(seq, audio, captured_at, context):
    global count, frames, maximum
    assert seq == count
    assert len(audio) <= max_frames
    actual.update(audio[audio != 0].tobytes())
    count += 1
    frames += len(audio)
    maximum = max(maximum, len(audio))


errors = []
rec = ChunkRecorder(0, profile['chunk_seconds'], 0, profile['min_chunk_seconds'], profile['early_flush_silence_seconds'],
                    .004, .025, emit, errors.append, dynamic_mode=dynamic)
start = time.monotonic()
rng = np.random.default_rng(91)
for i in range(72000):
    # Continuous speech, breaths, long pauses, quiet syllables, variable pacing.
    audio = rng.uniform(.03, .12, 1600).astype('float32')
    if i % 197 < 3 or i % 401 < 17:
        audio[:] = 0
    if i % 83 == 0:
        audio *= .01
    expected.update(audio[audio != 0].tobytes())
    rec._callback(audio[:, None], len(audio), None, None)
    assert rec._active_frames <= max_frames
rec.stop()
assert not errors, errors
assert expected.digest() == actual.digest()
report = {'mode': profile['mode'], 'simulated_seconds': 7200, 'segments': count, 'maximum_segment_seconds': maximum / 16000,
          'all_nonzero_samples_preserved_in_order': True, 'hash': actual.hexdigest(),
          'wall_seconds': round(time.monotonic() - start, 2), 'errors': errors}
args.report.parent.mkdir(parents=True, exist_ok=True)
args.report.write_text(json.dumps(report, indent=2), encoding='utf-8')
print(json.dumps(report))
