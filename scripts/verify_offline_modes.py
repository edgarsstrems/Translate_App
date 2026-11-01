"""Compare exact segmentation and upload normalization on local audio; no APIs."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.compare_sermon_context import decode_audio
from church_translator.audio import ChunkRecorder, SAMPLE_RATE
from church_translator.config import get_speed_profile_settings
from church_translator.engine import TranslationEngine
from church_translator.speech_gate import audio_evidence


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('audio', type=Path)
    parser.add_argument('report', type=Path)
    parser.add_argument('--start', type=float, default=0)
    parser.add_argument('--seconds', type=float, default=48)
    args = parser.parse_args()
    original = decode_audio(str(args.audio), args.start, args.seconds)
    if not len(original):
        raise ValueError('Recording excerpt is empty')
    results = []
    for case, audio in [('original_recording', original), ('quiet_recording', original * .01),
                        ('digital_silence', np.zeros_like(original))]:
        for mode in ('balanced', 'dynamic', 'low_latency'):
            chunks, metadata, errors = [], [], []
            profile = get_speed_profile_settings(mode)
            def emit(seq, chunk, *_):
                chunks.append(chunk)
                metadata.append({**rec.last_capture, 'reason': rec.last_boundary})
            rec = ChunkRecorder(0, profile['chunk_seconds'], 0, profile['min_chunk_seconds'],
                                profile['early_flush_silence_seconds'], .004, .025, emit, errors.append,
                                dynamic_mode=mode == 'dynamic')
            for offset in range(0, len(audio), 1600):
                block = audio[offset:offset + 1600]
                rec._callback(block[:, None], len(block), None, None)
            rec.stop()
            np.testing.assert_array_equal(np.concatenate(chunks), audio)
            assert not errors
            cursor = 0
            for row in metadata:
                assert row['start_sample'] == cursor
                cursor = row['end_sample']
            gains, peaks, clipped = [], [], 0
            for chunk in chunks:
                evidence = audio_evidence(chunk)
                processed, gain = (TranslationEngine._prepare_whisper_audio(None, chunk,
                    float(np.sqrt(np.mean(chunk * chunk))), float(np.max(np.abs(chunk))))
                    if evidence['allow_gain'] else (chunk, 1.0))
                gains.append(gain)
                peaks.append(float(np.max(np.abs(processed))))
                clipped += int(np.count_nonzero(np.abs(processed) >= 1))
            results.append({'case': case, 'mode': mode, 'samples': len(audio), 'segments': len(chunks),
                            'maximum_segment_seconds': max(map(len, chunks)) / SAMPLE_RATE,
                            'sha256': hashlib.sha256(audio.tobytes()).hexdigest(),
                            'exact_all_sample_coverage': True, 'overlap_samples': 0,
                            'maximum_gain': max(gains), 'maximum_processed_peak': max(peaks),
                            'processed_clipped_samples': clipped,
                            'pause_boundaries': sum(r['reason'] == 'pause' for r in metadata)})
    result = {'ok': True, 'paid_requests': 0, 'recording_seconds': len(original) / SAMPLE_RATE,
              'rows': results, 'wer': None, 'recognition_omissions': None,
              'limitation': 'Offline signal/segmentation only; no human verified reference or recognition accuracy claim.'}
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps(result))


if __name__ == '__main__':
    main()
