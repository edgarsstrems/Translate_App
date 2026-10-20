"""Small opt-in real-service test using an existing app configuration.

Synthesizes a known Latvian fixture if a Latvian Cloud voice is available, then
transcribes it, translates it and synthesizes English. Does not play audio.
"""
import io
import json
import time
import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from church_translator import config
from church_translator.services import OpenAITranscriber, Translator, TextToSpeech
from church_translator.glossary import load_glossary
from church_translator.reliability import safe_error, GeminiRetryLater
from church_translator.sermon_context import SermonContextManager

root, report_path = Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve()
config.project_root = lambda: root
cfg = replace(config.load_config(), openai_transcription_model='gpt-4o-transcribe', translation_provider='gemini', openai_fallback_enabled=False)
glossary = load_glossary(root)
source = 'Šodien mēs runājam par apustuli Pāvilu. Jēzus Kristus mums dod cerību un mieru.'
report = {'ok': False, 'fixture': source, 'model': cfg.openai_transcription_model,
          'gemini_timeout_seconds': cfg.gemini_timeout_seconds}
if '--resume' in sys.argv and report_path.exists():
    report = json.loads(report_path.read_text(encoding='utf-8'))
    if report.get('ok'):
        print(json.dumps({'ok': True, 'reused_completed_report': str(report_path)}))
        raise SystemExit(0)
    report.pop('audio_test_error', None)


def checkpoint(stage):
    report['last_stage'] = stage
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding='utf-8')
    print(json.dumps({'stage': stage, 'report': str(report_path)}), flush=True)


translator = None
print('Checking Gemini translation with fixed text.', flush=True)
try:
    context = SermonContextManager(report_path.parent / 'diagnostic-context.json')
    context.set_text(source + ' Romiešiem 8:28.', save=False)
    context_logs = []
    translator = Translator(cfg, glossary, status_cb=context_logs.append, sermon_context=context)
    if not report.get('gemini', {}).get('ok'):
        report['gemini_models'] = translator.refresh_available_models()
        translated = translator.translate(source, 'en')
        report['gemini'] = {'ok': translator.last_provider_used == 'gemini', 'translation': translated, 'provider': translator.last_provider_used, 'model': translator._working_model}
    announcement = 'Autobuss izbrauks rīt pulksten deviņos no stāvvietas.'
    if 'unrelated_announcement' not in report:
        unrelated = translator.translate(announcement, 'en')
        report['unrelated_announcement'] = {'source': announcement, 'translation': unrelated,
            'prepared_hint_supplied': bool(context.get_compact_prompt_hint(announcement))}
        report['context_diagnostics'] = [line for line in context_logs if line.startswith('[CONTEXT]')]
except Exception as exc:
    report['gemini'] = {'ok': False, 'error': safe_error(exc)}
checkpoint('gemini_translation')
print('Checking Gemini summary with fixed text.', flush=True)
try:
    from church_translator.summarizer import SermonSummarizer
    from church_translator.session_manager import TranscriptSegment
    if not report.get('gemini_summary', {}).get('ok'):
        summary = SermonSummarizer(cfg)
        text = summary.generate_summary([TranscriptSegment(0, time.time(), '10:00:00', source, '')], language='lv')
        report['gemini_summary'] = {'ok': bool(text), 'provider': summary.last_provider_used, 'model': summary.last_model_used, 'summary': text}
except Exception as exc:
    report['gemini_summary'] = {'ok': False, 'error': safe_error(exc)}
checkpoint('gemini_summary')
print('Checking Cloud voices and synthetic speech recognition.', flush=True)
tts = stt = None
try:
    import soundfile as sf
    if not cfg.google_application_credentials:
        raise RuntimeError('No configured Google Cloud service account')
    tts = TextToSpeech(cfg)
    report.setdefault('voice_catalog', {})
    for language in ('en', 'lv', 'lt', 'ru'):
        if language not in report['voice_catalog']:
            report['voice_catalog'][language] = tts.list_voices(language)
            checkpoint('voice_catalog_' + language)
    report.setdefault('multilingual_tts', {})
    for language, sample in {'en': 'Hope brings peace.', 'lv': source,
                             'lt': 'Viltis atneša ramybę.', 'ru': 'Надежда приносит мир.'}.items():
        if report['multilingual_tts'].get(language, {}).get('ok') and language != 'lv':
            continue
        audio = tts.synthesize(sample, language, strict=True)
        data, rate = sf.read(io.BytesIO(audio), dtype='float32')
        report['multilingual_tts'][language] = {'ok': len(data) > 0, 'voice': tts._voices[language].voice_name, 'seconds': len(data) / rate}
        checkpoint('tts_' + language)
        if language == 'lv':
            # Cloud LINEAR16 defaults to the voice's native rate (often 24 kHz).
            # App microphone STT takes 16 kHz. Resample the synthetic fixture
            # rather than labelling its original samples with the wrong rate.
            import av
            import numpy as np
            report['fixture_native_sample_rate'] = rate
            frames = []
            with av.open(io.BytesIO(audio)) as container:
                resampler = av.AudioResampler(format='flt', layout='mono', rate=16000)
                for frame in container.decode(audio=0):
                    frames.extend(f.to_ndarray().flatten() for f in resampler.resample(frame))
                frames.extend(f.to_ndarray().flatten() for f in resampler.resample(None))
            lv_audio = np.concatenate(frames).astype('float32')
    stt = OpenAITranscriber(cfg, glossary, lambda _: None)
    chain_start = time.monotonic()
    result = stt.transcribe(lv_audio)
    stt_seconds = time.monotonic() - chain_start
    report['openai'] = {'ok': bool(result.text), 'transcript': result.text, 'fixture_kind': 'synthetic Latvian speech'}
    checkpoint('openai_stt')
    translation_start = time.monotonic()
    for attempt in range(2):
        try:
            spoken = translator.translate(result.text, 'en')
            break
        except GeminiRetryLater as exc:
            # One local pacing wait is safe. A provider outage is a failed
            # diagnostic, not permission to generate forever until it succeeds.
            if not exc.pacing or exc.delay > 4.5 or attempt:
                raise
            time.sleep(exc.delay)
    translation_seconds = time.monotonic() - translation_start
    tts_start = time.monotonic()
    final_audio = tts.synthesize(spoken, 'en', strict=True)
    report['serial_chain'] = {'ok': bool(final_audio), 'stt_seconds': round(stt_seconds, 2),
        'translation_seconds': round(translation_seconds, 2),
        'tts_seconds': round(time.monotonic() - tts_start, 2),
        'after_capture_to_audio_ready_seconds': round(time.monotonic() - chain_start, 2),
        'translation': spoken}
except Exception as exc:
    report['audio_test_error'] = safe_error(exc)
finally:
    if stt and stt._client:
        stt._client.close()
    if tts and tts._client:
        tts._client.transport.close()
checkpoint('synthetic_audio_chain')
if translator and translator._genai_client:
    translator._genai_client.close()
report['ok'] = (
    all(report.get(stage, {}).get('ok', False) for stage in ('gemini', 'gemini_summary', 'openai', 'serial_chain'))
    and all(report.get('multilingual_tts', {}).get(language, {}).get('ok', False) for language in ('en', 'lv', 'lt', 'ru'))
    and report.get('unrelated_announcement', {}).get('prepared_hint_supplied') is False
)
report_path.parent.mkdir(parents=True, exist_ok=True)
report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding='utf-8')
print(json.dumps({'ok': report['ok'], 'report': str(report_path),
                  'audio_test_error': report.get('audio_test_error'),
                  'chain': report.get('serial_chain')}, ensure_ascii=True))
raise SystemExit(0 if report['ok'] else 1)
