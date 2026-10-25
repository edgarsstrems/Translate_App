"""Bounded opt-in A/B: identical audio, prepared vocabulary off/on, same model.

Outputs can contain sermon content. Save under build/ or private-audit/, never
publish the report. Human-verified reference text is required for natural ASR WER.
At most three recorded passages of 20 seconds and one synthetic canary, two arms; no paid translation opt-in is
created. --live authorizes configured STT/TTS/Gemini usage for this invocation.
"""
import argparse
from dataclasses import replace
import io
import json
from pathlib import Path
import re
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import av
import numpy as np
from church_translator.audio import ChunkRecorder
from church_translator.config import get_speed_profile_settings, load_config
from church_translator.glossary import Glossary
from church_translator.reliability import GeminiRetryLater, safe_error
from church_translator.sermon_context import SermonContextManager
from church_translator.services import OpenAITranscriber, Translator, TextToSpeech


def error_counts(reference, hypothesis):
    """Word-level Levenshtein alignment; punctuation/case do not count as errors."""
    import unicodedata
    tokens = lambda value: re.findall(r'\w+', unicodedata.normalize('NFC', value).casefold())
    source, result = tokens(reference), tokens(hypothesis)
    previous = [(j, 0, j, 0) for j in range(len(result) + 1)]  # distance, S, I, D
    for i, word in enumerate(source, 1):
        current = [(i, 0, 0, i)]
        for j, other in enumerate(result, 1):
            if word == other:
                current.append(previous[j - 1])
            else:
                d, s, ins, dele = previous[j - 1]
                sub = (d + 1, s + 1, ins, dele)
                d, s, ins, dele = current[-1]
                insert = (d + 1, s, ins + 1, dele)
                d, s, ins, dele = previous[j]
                delete = (d + 1, s, ins, dele + 1)
                current.append(min((sub, insert, delete), key=lambda value: value[0]))
        previous = current
    distance, substitutions, insertions, deletions = previous[-1]
    return {'reference_words': len(source), 'wer': round(distance / max(1, len(source)), 4),
            'substitutions': substitutions, 'insertions': insertions, 'deletions': deletions}


def decode_audio(source, start=0, duration=60):
    frames = []
    with av.open(source) as container:
        if start:
            container.seek(round(start * av.time_base))
        resampler = av.AudioResampler(format='flt', layout='mono', rate=16000)
        for frame in container.decode(audio=0):
            timestamp = frame.time or 0
            if timestamp > start + duration + 1:
                break
            for output in resampler.resample(frame):
                data = output.to_ndarray().flatten()
                when = output.time if output.time is not None else timestamp
                left = max(0, round((start - when) * 16000))
                right = min(len(data), round((start + duration - when) * 16000))
                if right > left:
                    frames.append(data[left:right])
        for output in resampler.resample(None):
            when = output.time or 0
            left = max(0, round((start - when) * 16000))
            right = min(output.samples, round((start + duration - when) * 16000))
            if right > left:
                frames.append(output.to_ndarray().flatten()[left:right])
    if not frames:
        raise ValueError('No audio decoded in the requested interval.')
    return np.concatenate(frames).astype('float32')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('report', type=Path)
    context = parser.add_mutually_exclusive_group(required=True)
    context.add_argument('--docx', type=Path)
    context.add_argument('--context-json', type=Path, help='Local evaluate_sermon.py extraction with paragraphs')
    parser.add_argument('--cases', type=Path, help='JSON list: id, audio, start, seconds, optional checked_reference')
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--no-synthetic', action='store_true', help='Targeted rerun with recorded cases only')
    args = parser.parse_args()
    if not args.live:
        parser.error('--live is required for provider usage')
    if not any(part in {'build', 'private-audit'} for part in args.report.parts):
        parser.error('Write potentially private results under build/ or private-audit/')
    args.report.parent.mkdir(parents=True, exist_ok=True)
    ctx = SermonContextManager(args.report.parent / 'unused-context.json')
    if args.docx:
        text, title = ctx.read_docx(args.docx)
    else:
        document = json.loads(args.context_json.read_text(encoding='utf-8'))
        text, title = '\n\n'.join(p['text'] for p in document['paragraphs']), document.get('title', '')
    ctx.set_text(text, title, save=False)
    cfg = replace(load_config(), auto_return_to_gemini=False)
    cases = json.loads(args.cases.read_text(encoding='utf-8')) if args.cases else []
    if len(cases) > 3:
        parser.error('At most three recorded passages, plus one unrelated synthetic canary')
    if any(not 0 < float(case.get('seconds', 12)) <= 20 for case in cases):
        parser.error('Recorded excerpts must be at most 20 seconds each')
    report = {'ok': False, 'stt_model': cfg.openai_transcription_model, 'prepared_document': ctx.get_stats(),
              'paid_translation_consent': cfg.openai_fallback_enabled, 'cases': [],
              'limits': 'Natural WER only with checked_reference; prepared text and historical recognition are never ground truth.'}
    save = lambda: args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    save()
    # Synthetic unrelated speech verifies that prepared guidance does not invent
    # body/faith wording in an announcement. Its source is known, unlike a draft.
    unrelated = 'Rīt autobuss izbrauks pulksten deviņos. Lūdzu, paņemiet biļeti un sagaidiet šoferi pie stacijas.'
    if not args.no_synthetic:
        tts = TextToSpeech(cfg)
        try:
            synthetic = tts.synthesize(unrelated, 'lv', strict=True)
            cases.append({'id': 'unrelated_synthetic', 'samples': decode_audio(io.BytesIO(synthetic)),
                          'checked_reference': unrelated, 'synthetic': True})
        finally:
            if tts._client:
                tts._client.transport.close()
    for case in cases:
        samples = case.get('samples')
        if samples is None:
            samples = decode_audio(str(Path(case['audio'])), float(case.get('start', 0)), float(case.get('seconds', 12)))
        chunks, boundaries = [], []
        profile = get_speed_profile_settings('dynamic')
        rec = ChunkRecorder(0, profile['chunk_seconds'], 0, profile['min_chunk_seconds'],
            profile['early_flush_silence_seconds'], .004, .025,
            lambda seq, data, *_: (chunks.append(data), boundaries.append((rec.last_boundary, rec.last_continues_previous))),
            lambda error: (_ for _ in ()).throw(RuntimeError(error)),
            dynamic_mode=True, dynamic_soft_seconds=profile['dynamic_soft_seconds'])
        rec._consume(samples)
        rec.stop()
        entry = {'id': case['id'], 'audio_seconds': round(len(samples) / 16000, 3),
                 'chunks_seconds': [round(len(c) / 16000, 3) for c in chunks],
                 'source_kind': 'synthetic' if case.get('synthetic') else 'original_recording',
                 'reference_kind': 'checked_reference' if case.get('checked_reference') else 'unavailable', 'arms': {}}
        report['cases'].append(entry)
        save()
        for enabled in (False, True):
            logs, rows, timing, diagnostics, usage, reconciliations, uncertainties = [], [], [], [], [], [], []
            stt = OpenAITranscriber(cfg, Glossary(), logs.append)
            try:
                previous = ''
                for chunk, (boundary, continuation) in zip(chunks, boundaries):
                    if not np.any(chunk):
                        continue
                    hint = ctx.recognition_guidance(previous) if enabled else None
                    started = time.monotonic()
                    result = stt.transcribe(chunk, previous_context=previous or None, prepared_context=hint)
                    timing.append(round(time.monotonic() - started, 3))
                    rows.append(result.text)
                    uncertainties.append(result.uncertain)
                    diagnostics.append(stt.last_context_diagnostics)
                    usage.append(stt.last_usage)
                    reconciliations.append(stt.last_context_reconciliation)
                    previous = ' '.join(result.text.split()[-15:])
            finally:
                if stt._client:
                    stt._client.close()
            source = ' '.join(rows)
            arm = {'transcript': source, 'segments': rows, 'stt_seconds': timing,
                   'context_requests': diagnostics, 'stt_usage': usage, 'reference_free_checks': reconciliations,
                   'uncertain_segments': uncertainties}
            if case.get('checked_reference'):
                arm['recognition_errors'] = error_counts(case['checked_reference'], source)
            entry['arms']['context_on' if enabled else 'context_off'] = arm
            save()
            translator = Translator(cfg, Glossary(), logs.append, sermon_context=ctx if enabled else None)
            translator.source_uncertain = any(uncertainties)
            try:
                started = time.monotonic()
                # Exactly one Gemini translation chain per arm. Existing bounded
                # quality repair may run; no automatic paid-fallback opt-in.
                arm['english'] = translator._translate_joint_with_gemini(source, ['en'])['en'] if source.strip() else ''
                arm['translation_seconds'] = round(time.monotonic() - started, 3)
                arm['context_diagnostics'] = [line for line in logs if line.startswith('[CONTEXT]')]
            except Exception as exc:
                arm['translation_error'] = safe_error(exc)
            finally:
                if translator._genai_client:
                    translator._genai_client.close()
            save()
        off, on = entry['arms']['context_off'], entry['arms']['context_on']
        entry['recognition_changed'] = off['transcript'] != on['transcript']
        if case.get('checked_reference'):
            entry['wer_delta_on_minus_off'] = round(on['recognition_errors']['wer'] - off['recognition_errors']['wer'], 4)
        save()
        print(json.dumps({'case': case['id'], 'stage': 'compared', 'audio_seconds': entry['audio_seconds']}), flush=True)
    report['ok'] = all(len(c['arms']) == 2 and all('translation_error' not in arm for arm in c['arms'].values()) for c in report['cases'])
    report['stt_audio_seconds_both_arms'] = round(sum(c['audio_seconds'] for c in report['cases']) * 2, 3)
    report['cost'] = 'Actual STT usage recorded per request; no provider invoice or Gemini billed-cost metadata available.'
    save()
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
