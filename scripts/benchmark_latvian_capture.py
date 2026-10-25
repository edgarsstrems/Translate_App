"""Private, bounded same-audio STT experiment; AI output is never ground truth.

--live explicitly authorizes configured OpenAI use for the supplied recording.
No translation, TTS, settings edits, or automatic uploads of reports occur.
"""
import argparse
from dataclasses import replace
import hashlib
import io
import json
from pathlib import Path
import sys
import time

import numpy as np
import soundfile as sf

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.compare_sermon_context import decode_audio, error_counts
from church_translator.audio import ChunkRecorder, SAMPLE_RATE
from church_translator.config import load_config
from church_translator.engine import TranslationEngine
from church_translator.sermon_context import SermonContextManager


def windows(audio, strategy):
    if strategy == 'pause12':
        out = []
        rec = ChunkRecorder(0, 10, 0, 4, .65, .004, .025,
                            lambda _, data, *_args: out.append(data), lambda e: None)
        # Experimental only: the shipping policy is not changed by this probe.
        rec._frames_needed = 12 * SAMPLE_RATE
        rec._consume(audio)
        rec.stop()
        cursor = 0
        result = []
        for data in out:
            result.append((cursor, cursor + len(data), data))
            cursor += len(data)
        return result
    seconds = 8 if strategy in {'eight', 'overlap8'} else 24
    size = seconds * SAMPLE_RATE
    overlap = SAMPLE_RATE // 2 if strategy == 'overlap8' else 0
    return [(left, min(len(audio), left + size), audio[left:left + size])
            for left in range(0, len(audio), size - overlap)]


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('audio', type=Path)
    p.add_argument('report', type=Path)
    p.add_argument('--live', action='store_true')
    p.add_argument('--context', type=Path)
    p.add_argument('--cases', type=Path, help='JSON array with id/start/seconds and optional human checked_reference')
    p.add_argument('--max-requests', type=int, default=64)
    args = p.parse_args()
    if not args.live or not any(x in {'build', 'private-audit'} for x in args.report.parts):
        p.error('Use --live and an ignored build/ or private-audit/ report path')
    cfg = load_config()
    from openai import OpenAI
    import openai
    client = OpenAI(api_key=cfg.openai_api_key, timeout=30, max_retries=0)
    ctx = SermonContextManager(args.report.parent / 'unused-context.json')
    if args.context:
        ctx.set_text(args.context.read_text(encoding='utf-8'), save=False)
    from church_translator.services import recognition_prompt
    cases = json.loads(args.cases.read_text(encoding='utf-8')) if args.cases else [
        {'id': 'continuous-colloquial', 'start': 4164, 'seconds': 24},
        {'id': 'quiet-science', 'start': 4197, 'seconds': 24},
        {'id': 'music-control', 'start': 5196, 'seconds': 12}]
    if len(cases) > 6 or any(not 0 < c['seconds'] <= 30 for c in cases):
        p.error('Use at most six excerpts of at most 30 seconds')
    args.report.parent.mkdir(parents=True, exist_ok=True)
    report = {'ok': False, 'reference_status': 'AI draft only unless explicitly human checked',
              'sdk': openai.__version__, 'requests': 0, 'submitted_seconds': 0, 'cases': []}
    save = lambda: args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    # Identical inputs within each arm; one-variable comparisons, no tuning on test references.
    arms = [('eight', cfg.openai_transcription_model, False, False),
            ('pause12', cfg.openai_transcription_model, False, False),
            ('long24', cfg.openai_transcription_model, False, False),
            ('long24', cfg.openai_transcription_model, True, False),
            ('long24', cfg.openai_transcription_model, False, True),
            ('long24', 'gpt-4o-mini-transcribe', False, False),
            ('long24', 'whisper-1', False, False),
            ('overlap8', 'whisper-1', False, False)]
    try:
        for case in cases:
            audio = decode_audio(str(args.audio), case['start'], case['seconds'])
            clip = args.report.parent / (case['id'] + '.wav')
            sf.write(clip, audio, SAMPLE_RATE, subtype='FLOAT')
            entry = {'id': case['id'], 'start': case['start'], 'samples': len(audio),
                     'float32_sha256': hashlib.sha256(audio.tobytes()).hexdigest(),
                     'review_audio': clip.name, 'arms': []}
            report['cases'].append(entry)
            for strategy, model, gain, context in arms:
                arm = {'strategy': strategy, 'model': model, 'gain': gain, 'context': context, 'segments': []}
                entry['arms'].append(arm)
                previous = ''
                for left, right, data in windows(audio, strategy):
                    if report['requests'] >= args.max_requests:
                        raise RuntimeError('Explicit request budget exhausted')
                    original = data
                    factor = 1.0
                    if gain:
                        rms, peak = float(np.sqrt(np.mean(data * data))), float(np.max(np.abs(data)))
                        data, factor = TranslationEngine._prepare_whisper_audio(None, data, rms, peak)
                    stream = io.BytesIO()
                    sf.write(stream, data, SAMPLE_RATE, format='FLAC', subtype='PCM_16')
                    stream.name = 'excerpt.flac'
                    stream.seek(0)
                    options = {'response_format': 'verbose_json', 'timestamp_granularities': ['word', 'segment']} if model == 'whisper-1' else {'response_format': 'json', 'include': ['logprobs']}
                    if context:
                        prompt = recognition_prompt(previous, ctx.recognition_guidance(previous))
                        if prompt:
                            options['prompt'] = prompt
                    report['requests'] += 1
                    report['submitted_seconds'] += len(data) / SAMPLE_RATE
                    save()
                    began = time.monotonic()
                    row = {'start_sample': left, 'end_sample': right, 'gain_factor': factor}
                    try:
                        response = client.audio.transcriptions.create(file=stream, model=model, language='lv', temperature=0, **options)
                        row.update(response.model_dump())
                        previous = response.text
                    except Exception as exc:
                        from church_translator.reliability import safe_error
                        row['error'] = safe_error(exc)
                    row['latency_seconds'] = round(time.monotonic() - began, 3)
                    arm['segments'].append(row)
                    save()
                # Overlapped hypotheses stay separate. Timestamp ownership is a
                # diagnostic candidate, never fuzzy text deletion in production.
                if strategy == 'overlap8':
                    selected = []
                    for i, row in enumerate(arm['segments']):
                        owned_start = row['start_sample'] + (SAMPLE_RATE // 4 if i else 0)
                        owned_end = row['end_sample'] - (SAMPLE_RATE // 4 if i + 1 < len(arm['segments']) else 0)
                        for word in row.get('words', []):
                            middle = row['start_sample'] + (word['start'] + word['end']) * SAMPLE_RATE / 2
                            if owned_start <= middle < owned_end:
                                selected.append(word['word'])
                    arm['provisional_text'] = ' '.join(selected)
                else:
                    arm['provisional_text'] = ' '.join(r.get('text', '') for r in arm['segments'])
                if case.get('checked_reference') and case.get('reference_verified_by'):
                    arm['verified_metrics'] = error_counts(case['checked_reference'], arm['provisional_text'])
                baseline = entry['arms'][0]['provisional_text']
                arm['disagreement_with_baseline_NOT_WER'] = error_counts(baseline, arm['provisional_text'])
                save()
                print(json.dumps({'case': case['id'], 'arm': len(entry['arms']), 'requests': report['requests']}), flush=True)
        report['ok'] = all('error' not in s for c in report['cases'] for a in c['arms'] for s in a['segments'])
    finally:
        save()
        client.close()
    draft = ['# Latvian reference DRAFT - requires human listening and correction', '',
             'These are AI hypotheses, not verified ground truth. Do not score accuracy against them.', '']
    for c in report['cases']:
        draft.extend([f"## {c['id']} - recording offset {c['start']} seconds", '', f"Audio: [{c['review_audio']}]({c['review_audio']})", ''])
        for arm in c['arms']:
            draft.extend([f"### {arm['strategy']} / {arm['model']} / gain={arm['gain']} / context={arm['context']}", '', arm.get('provisional_text', ''), ''])
        whisper = next((a for a in c['arms'] if a['model'] == 'whisper-1' and a['strategy'] == 'long24'), None)
        for row in (whisper or {}).get('segments', []):
            for s in row.get('segments', []):
                draft.append(f"[{c['start'] + s['start']:.2f}-{c['start'] + s['end']:.2f}] {s['text']}")
    args.report.with_name('HUMAN_REVIEW_DRAFT.md').write_text('\n'.join(draft), encoding='utf-8')


if __name__ == '__main__':
    main()
