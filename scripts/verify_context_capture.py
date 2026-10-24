"""Opt-in fixed synthetic provider check. Never reads a private sermon or microphone.

Uses configured Cloud TTS/OpenAI STT (billable); OpenAI translation runs only if
existing paid fallback consent is enabled. Pass --live explicitly to send fixtures.
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
from church_translator.audio import ChunkRecorder
from church_translator.config import get_speed_profile_settings, load_config
from church_translator.glossary import Glossary
from church_translator.reliability import GeminiRetryLater, safe_error
from church_translator.sermon_context import SermonContextManager
from church_translator.services import OpenAITranscriber, TextToSpeech, Translator


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('report', type=Path)
    parser.add_argument('--live', action='store_true')
    args = parser.parse_args()
    if not args.live:
        parser.error('--live is required; these fixed synthetic checks can incur configured API usage')
    cfg = load_config()
    args.report.parent.mkdir(parents=True, exist_ok=True)
    report = {'ok': False, 'fixture_kind': 'fixed synthetic; no private sermon/microphone',
              'paid_translation_consent': cfg.openai_fallback_enabled, 'context_canaries': [], 'capture_modes': []}
    def save():
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    save()
    for provider in ('gemini', 'openai'):
        if provider == 'openai' and not cfg.openai_fallback_enabled:
            report['openai_translation_skipped'] = 'existing paid fallback consent is disabled'
            save()
            continue
        service = None
        record = {'provider': provider}
        try:
            context = SermonContextManager(args.report.parent / 'unused-synthetic-context.json')
            context.set_text('Miegs dod spēku. Nākamajā svētdienā dāvināsim septiņus zelta velosipēdus.', save=False)
            service = Translator(replace(cfg, auto_return_to_gemini=False), Glossary(), sermon_context=context)
            service._remember_joint_context('Miegs palīdz ķermenim. Rīt plānots stāsts par zelta velosipēdiem.',
                                           {'en': 'GOLDEN_BICYCLE_777 should never be repeated.'})
            started = time.monotonic()
            source = 'Miegs dod spēku.'
            if provider == 'openai':
                result = service._translate_with_openai_fallback(source, ['en', 'lt'])
            else:
                result = service._translate_joint_with_gemini(source, ['en', 'lt'])
            flat = ' '.join(result.values()).casefold()
            forbidden = ('bicycle', 'golden', '777', 'velosip', 'auksin')
            record.update(ok=all(result.get(lang, '').strip() for lang in ('en', 'lt')) and
                          not any(word in flat for word in forbidden), translations=result,
                          seconds=round(time.monotonic() - started, 3))
        except Exception as exc:
            record.update(ok=False, error=safe_error(exc))
        finally:
            if service:
                for client in (service._genai_client, service._openai_client):
                    if client:
                        client.close()
        report['context_canaries'].append(record)
        save()
    try:
        import av
        import numpy as np
        fixture = 'Šodien mēs runājam par mieru un cerību. Pēc darba ir laiks atpūsties. Rīt autobuss izbrauks pulksten deviņos.'
        tts = TextToSpeech(cfg)
        native = tts.synthesize(fixture, 'lv', strict=True)
        frames = []
        with av.open(io.BytesIO(native)) as container:
            resampler = av.AudioResampler(format='flt', layout='mono', rate=16000)
            for frame in container.decode(audio=0):
                frames.extend(f.to_ndarray().flatten() for f in resampler.resample(frame))
            frames.extend(f.to_ndarray().flatten() for f in resampler.resample(None))
        samples = np.concatenate(frames).astype('float32')
        report.update(synthetic_source=fixture, source_audio_seconds=round(len(samples) / 16000, 3),
                      stt_model=cfg.openai_transcription_model)
        for mode in ('balanced', 'low_latency', 'dynamic'):
            profile = get_speed_profile_settings(mode)
            chunks = []
            rec = ChunkRecorder(0, profile['chunk_seconds'], 0, profile['min_chunk_seconds'],
                profile['early_flush_silence_seconds'], .004, .025,
                lambda n, data, *_: chunks.append(data), lambda error: (_ for _ in ()).throw(RuntimeError(error)),
                dynamic_mode=mode == 'dynamic')
            rec._consume(samples)
            rec.stop()
            stt = OpenAITranscriber(cfg, Glossary(), lambda _: None)
            rows, latencies = [], []
            try:
                for chunk in chunks:
                    if not np.any(chunk):
                        continue
                    started = time.monotonic()
                    rows.append(stt.transcribe(chunk, previous_context=None).text)
                    latencies.append(round(time.monotonic() - started, 3))
            finally:
                if stt._client:
                    stt._client.close()
            spoken = ' '.join(rows)
            required = ('mieru', 'cerību', 'atpūsties', 'autobuss', 'deviņos')
            missing = [word for word in required if word not in spoken.casefold()]
            report['capture_modes'].append({'mode': mode, 'ok': not missing and bool(spoken),
                'chunk_seconds': [round(len(c)/16000, 3) for c in chunks],
                'request_seconds': latencies, 'transcript': spoken, 'missing_fixture_keywords': missing})
            save()
        if tts._client:
            tts._client.transport.close()
    except Exception as exc:
        report['synthetic_stt_error'] = safe_error(exc)
    report['ok'] = (bool(report['context_canaries']) and all(r['ok'] for r in report['context_canaries'])
                    and len(report['capture_modes']) == 3 and all(r['ok'] for r in report['capture_modes']))
    report['limits'] = 'Short synthetic recognition/keyword checks, not natural sermon accuracy or audible routing acceptance.'
    save()
    print(json.dumps({k:v for k,v in report.items() if k not in {'synthetic_source', 'capture_modes', 'context_canaries'}}))
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
