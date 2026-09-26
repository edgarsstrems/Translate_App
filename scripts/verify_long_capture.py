"""Accelerated two-hour segmentation soak with streaming sample hashes."""
import hashlib
import json
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from church_translator.audio import ChunkRecorder

expected, actual = hashlib.sha256(), hashlib.sha256()
count = frames = maximum = 0


def emit(seq, audio, captured_at, context):
    global count, frames, maximum
    assert seq == count
    assert len(audio) <= 80000
    actual.update(audio[audio != 0].tobytes())
    count += 1
    frames += len(audio)
    maximum = max(maximum, len(audio))


errors = []
rec = ChunkRecorder(0, 5, 0, 2.5, .45, .004, .025, emit, errors.append)
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
    assert rec._active_frames <= 80000
rec.stop()
assert not errors, errors
assert expected.digest() == actual.digest()
report = {'simulated_seconds': 7200, 'segments': count, 'maximum_segment_seconds': maximum / 16000,
          'all_nonzero_samples_preserved_in_order': True, 'hash': actual.hexdigest(),
          'wall_seconds': round(time.monotonic() - start, 2), 'errors': errors}
Path(sys.argv[1]).write_text(json.dumps(report, indent=2), encoding='utf-8')
print(json.dumps(report))
