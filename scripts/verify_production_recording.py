"""Private local input/response audit; no APIs. AI hypotheses are not references.

Optional --local-model is an already installed faster-whisper snapshot, used
offline on the final contextual window only. --references accepts human-checked
segment references; otherwise all accuracy/deletion metrics remain unknown.
"""
import argparse
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import sys
import time
from unittest.mock import patch
import numpy as np
import soundfile as sf

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from church_translator.engine import TranslationEngine
from church_translator.recognition_quality import short_audio_warning
from church_translator.services import OpenAITranscriber
from church_translator.speech_audit import SpeechAudit
from church_translator.speech_gate import audio_evidence
from scripts.compare_sermon_context import error_counts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('audit', type=Path)
    parser.add_argument('report', type=Path)
    parser.add_argument('--local-model', type=Path)
    parser.add_argument('--references', type=Path)
    args = parser.parse_args()
    args.report.parent.mkdir(parents=True, exist_ok=True)
    if Path(str(args.audit) + '-wal').exists():
        raise ValueError('Use a closed session snapshot, not a live WAL database.')
    audit = SpeechAudit.existing(args.audit)
    db = audit._connect()
    try:
        events = [(kind, seq, json.loads(details)) for kind, seq, details in
                  db.execute('SELECT kind,segment,details FROM events ORDER BY id')]
        submissions = [(seq, audio, json.loads(meta), json.loads(response) if response else None)
                       for seq, audio, meta, response in db.execute(
                           'SELECT segment,audio,metadata,response FROM submissions ORDER BY id')]
    finally:
        db.close()
    captures = {seq: details['capture'] for kind, seq, details in events if kind == 'captured_segment'}
    recognition = {seq: details for kind, seq, details in events if kind == 'recognition'}
    report = {'private': True, 'api_requests': 0, 'segments': [], 'word_error_rates': None,
              'measured_missed_speech_seconds': None, 'verified_reference_available': False}
    for seq, capture in captures.items():
        audio = audit.audio_range(capture['capture_id'], capture['start_sample'], capture['end_sample'])
        old = recognition.get(seq, {})
        evidence = audio_evidence(audio)
        row = {'segment': seq, 'capture': capture, 'original_samples': len(audio),
               'source_sha256': hashlib.sha256(audio.tobytes()).hexdigest(), 'speech_evidence': evidence,
               'selected_hypothesis': old.get('selected_text', ''), 'submissions': []}
        row['short_audio_check'] = short_audio_warning(len(audio), row['selected_hypothesis'])
        row['coverage_check'] = OpenAITranscriber._completeness_signals(audio, row['selected_hypothesis'], evidence)
        for submitted_seq, encoded, metadata, response in submissions:
            if submitted_seq != seq:
                continue
            actual, rate = sf.read(io.BytesIO(encoded), dtype='float32')
            assert rate == 16000
            diagnostic = metadata['diagnostics']
            assert hashlib.sha256(encoded).hexdigest() == diagnostic['encoded_sha256']
            assert hashlib.sha256(actual.astype('<f4').tobytes()).hexdigest() == diagnostic['decoded_pcm_sha256']
            assert len(actual) == diagnostic['submitted_samples']
            assert diagnostic['source_samples'] == len(audio)
            processed, gain = TranslationEngine._prepare_whisper_audio(None, audio,
                float(np.sqrt(np.mean(audio * audio))), float(np.max(np.abs(audio))), evidence['allow_gain'])
            if len(processed) < 8000:
                processed = np.pad(processed, (0, 8000 - len(processed)))
            expected = io.BytesIO()
            sf.write(expected, processed, 16000, format='WAV', subtype='PCM_16')
            decoded, _ = sf.read(io.BytesIO(expected.getvalue()), dtype='float32')
            maximum_error = float(np.max(np.abs(decoded - actual)))
            assert maximum_error <= 1 / 32768
            row['submissions'].append({'decoded_samples': len(actual), 'padding_samples': diagnostic['padding_samples'],
                'gain': gain, 'reproduced_pcm16_max_error': maximum_error,
                'clipped_samples': int(np.count_nonzero(np.abs(actual) >= 1)),
                'prompt': metadata.get('request_options', {}).get('prompt'), 'raw_response': response})
        report['segments'].append(row)
    # A private review pack makes the verification gap concrete, without
    # pretending a second recognizer is a human-checked transcript.
    final_seq = max(captures)
    final = captures[final_seq]
    left = max(0, final['start_sample'] - 4 * 16000)
    context = audit.audio_range(final['capture_id'], left, final['end_sample'])
    prefix = args.report.with_suffix('')
    sf.write(str(prefix) + '-tail-context.wav', context, 16000, subtype='FLOAT')
    tail = audit.audio_range(final['capture_id'], final['start_sample'], final['end_sample'])
    sf.write(str(prefix) + '-tail-only.wav', tail, 16000, subtype='FLOAT')
    template = {'verified': False, 'checked_by': None, 'note': 'Check speech against original audio, not a draft or ASR.',
                'segments': [{'segment': row['segment'], 'checked_reference': None,
                              'asr_hypothesis_not_reference': row['selected_hypothesis']}
                             for row in report['segments']]}
    Path(str(prefix) + '-reference-template.json').write_text(json.dumps(template, ensure_ascii=False, indent=2), encoding='utf-8')
    if args.references:
        checked = json.loads(args.references.read_text(encoding='utf-8'))
        if not checked.get('verified') or not checked.get('checked_by'):
            raise ValueError('Explicit human verification and checker are required for accuracy metrics.')
        report['word_error_rates'] = [{**error_counts(case['checked_reference'], recognition[case['segment']]['selected_text']),
                                      'segment': case['segment']} for case in checked['segments'] if case.get('checked_reference')]
        report['verified_reference_available'] = bool(report['word_error_rates'])
    if args.local_model:
        started = time.monotonic()
        try:
            with patch('socket.socket.connect', side_effect=AssertionError('Offline verification attempted network')):
                from faster_whisper import WhisperModel
                model = WhisperModel(str(args.local_model), device='cpu', compute_type='int8', local_files_only=True)
                segments, _ = model.transcribe(context, language='lv', beam_size=3, word_timestamps=True,
                    vad_filter=False, condition_on_previous_text=False, initial_prompt=None)
                report['offline_context_hypothesis'] = [
                    {'text': segment.text, 'start': segment.start, 'end': segment.end,
                     'words': [{'word': word.word, 'start': word.start, 'end': word.end} for word in segment.words or []]}
                    for segment in segments]
                report['offline_context_window'] = {'start_sample': left, 'end_sample': final['end_sample'],
                    'tail_start_relative_seconds': (final['start_sample'] - left) / 16000, 'ground_truth': False}
        except Exception as exc:
            report['offline_model_error'] = type(exc).__name__ + ': ' + str(exc)
        report['offline_model_seconds'] = time.monotonic() - started
    report['ok'] = True
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'ok': True, 'segments': len(report['segments']), 'api_requests': 0,
                      'submitted_files_verified': sum(len(s['submissions']) for s in report['segments']),
                      'reference_verified': report['verified_reference_available'],
                      'offline_model_error': report.get('offline_model_error')}))


if __name__ == '__main__':
    main()
