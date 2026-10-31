"""Offline retained-PCM gate and profile comparison. No uploads or provider calls."""
import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from church_translator.audio import ChunkRecorder, SAMPLE_RATE
from church_translator.config import get_speed_profile_settings
from church_translator.speech_gate import audio_evidence
from church_translator.services import OpenAITranscriber
from church_translator.engine import TranslationEngine


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('report', type=Path)
    parser.add_argument('--audit', type=Path, action='append', required=True)
    args = parser.parse_args()
    report = {'api_requests': 0, 'recordings': [], 'wer': None,
              'limitation': 'No checked transcript: signal/gate/sample coverage evidence only, not word accuracy.'}
    for path in args.audit:
        if Path(str(path) + '-wal').exists():
            raise ValueError('Close the source session before reading its immutable audit snapshot.')
        db = sqlite3.connect(path.resolve().as_uri() + '?immutable=1', uri=True)
        try:
            recordings = {}
            for ident, start, end, digest, raw in db.execute(
                    'SELECT capture_id,start_sample,end_sample,sha256,audio FROM capture ORDER BY capture_id,start_sample'):
                assert hashlib.sha256(raw).hexdigest() == digest
                parts = recordings.setdefault(ident, [])
                assert start == sum(map(len, parts)) and end - start == len(raw) // 4
                parts.append(np.frombuffer(raw, dtype='<f4'))
            recordings = {key: np.concatenate(parts) for key, parts in recordings.items()}
            captures = {seq: json.loads(details)['capture'] for seq, details in db.execute(
                "SELECT segment,details FROM events WHERE kind='captured_segment'")}
            results = [(seq, json.loads(details)) for seq, details in db.execute(
                "SELECT segment,details FROM events WHERE kind='recognition'")]
        finally:
            db.close()
        row = {'session': path.parent.name, 'original_segments': [], 'profiles': []}
        for seq, result in results:
            c = captures[seq]
            audio = recordings[c['capture_id']][c['start_sample']:c['end_sample']]
            evidence = audio_evidence(audio)
            completeness = OpenAITranscriber._completeness_signals(audio, result.get('selected_text', ''), evidence)
            processed, gain = TranslationEngine._prepare_whisper_audio(None, audio,
                float(np.sqrt(np.mean(audio * audio))), float(np.max(np.abs(audio))), evidence['allow_gain'])
            row['original_segments'].append({'segment': seq, **evidence,
                'previously_produced_text': bool(result.get('selected_text')),
                'previous_gain': result.get('gain'), 'samples': len(audio),
                'coverage_warning': completeness,
                'original_sha256': hashlib.sha256(audio.tobytes()).hexdigest(),
                'processed_sha256': hashlib.sha256(processed.tobytes()).hexdigest(),
                'replayed_gain': gain, 'processed_peak': float(np.max(np.abs(processed))),
                'processed_clipped_samples': int(np.count_nonzero(np.abs(processed) >= 1)),
                'original_dc_offset': float(np.mean(audio)), 'processed_dc_offset': float(np.mean(processed))})
        for ident, audio in recordings.items():
            row['rms_dbfs'] = audio_evidence(audio)['rms_dbfs']
            row['peak_dbfs'] = audio_evidence(audio)['peak_dbfs']
            for case, source in [('original', audio), ('quiet_1_percent', audio * .01)]:
                for mode in ('balanced', 'dynamic', 'low_latency'):
                    emitted = []
                    settings = get_speed_profile_settings(mode)
                    rec = ChunkRecorder(0, settings['chunk_seconds'], 0, settings['min_chunk_seconds'],
                        settings['early_flush_silence_seconds'], .004, .025,
                        lambda seq, chunk, *_: emitted.append(chunk), lambda error: (_ for _ in ()).throw(RuntimeError(error)),
                        dynamic_mode=mode == 'dynamic', dynamic_soft_seconds=settings.get('dynamic_soft_seconds', 6))
                    for start in range(0, len(source), 1600):
                        rec._consume(source[start:start + 1600])
                    rec.stop()
                    combined = np.concatenate(emitted)
                    np.testing.assert_array_equal(combined, source)
                    gates = [audio_evidence(chunk) for chunk in emitted]
                    row['profiles'].append({'mode': mode, 'case': case, 'segments': len(emitted),
                        'samples': len(source), 'all_samples_equal': True,
                        'sha256': hashlib.sha256(combined.tobytes()).hexdigest(),
                        'maximum_segment_seconds': max(map(len, emitted)) / SAMPLE_RATE,
                        'gated_segments': sum(g['skip_stt'] for g in gates)})
            # Every original segment with strong speech evidence remains eligible
            # even after reducing its level 100x. No transcript-derived labels.
            speech, quiet_preserved = 0, 0
            for seq, result in results:
                c = captures[seq]
                if c['capture_id'] != ident:
                    continue
                source = audio[c['start_sample']:c['end_sample']]
                e = audio_evidence(source)
                if e.get('speech_probability', 0) >= .5 or (not e['skip_stt'] and e['rms_dbfs'] > -50):
                    speech += 1
                    quiet_preserved += not audio_evidence(source * .01)['skip_stt']
            row.update(speech_candidate_segments=speech, quiet_candidates_preserved=quiet_preserved)
            assert speech == quiet_preserved
        report['recordings'].append(row)
    report['ok'] = True
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps({'ok': True, 'recordings': len(report['recordings']), 'api_requests': 0}))


if __name__ == '__main__':
    main()
