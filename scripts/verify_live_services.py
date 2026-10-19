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
report = {'fixture': source, 'model': cfg.openai_transcription_model}
translator = None
try:
    context = SermonContextManager(report_path.parent / 'diagnostic-context.json')
    context.set_text(source + ' Romiešiem 8:28.', save=False)
    context_logs = []
    translator = Translator(cfg, glossary, status_cb=context_logs.append, sermon_context=context)
    report['gemini_models'] = translator.refresh_available_models()
    translated = translator.translate(source, 'en')
    report['gemini'] = {'ok': translator.last_provider_used == 'gemini', 'translation': translated, 'provider': translator.last_provider_used, 'model': translator._working_model}
    announcement = 'Autobuss izbrauks rīt pulksten deviņos no stāvvietas.'
    unrelated = translator.translate(announcement, 'en')
    report['unrelated_announcement'] = {'source': announcement, 'translation': unrelated,
        'prepared_hint_supplied': bool(context.get_compact_prompt_hint(announcement))}
    report['context_diagnostics'] = [line for line in context_logs if line.startswith('[CONTEXT]')]
except Exception as exc:
    report['gemini'] = {'ok': False, 'error': safe_error(exc)}
try:
    from church_translator.summarizer import SermonSummarizer
    from church_translator.session_manager import TranscriptSegment
    summary = SermonSummarizer(cfg)
    text = summary.generate_summary([TranscriptSegment(0, time.time(), '10:00:00', source, '')], language='lv')
    report['gemini_summary'] = {'ok': bool(text), 'provider': summary.last_provider_used, 'model': summary.last_model_used, 'summary': text}
except Exception as exc:
    report['gemini_summary'] = {'ok': False, 'error': safe_error(exc)}
try:
    import soundfile as sf
    if not cfg.google_application_credentials:
        raise RuntimeError('No configured Google Cloud service account')
    tts = TextToSpeech(cfg)
    report['voice_catalog'] = {language: tts.list_voices(language) for language in ('en', 'lv', 'lt', 'ru')}
    report['multilingual_tts'] = {}
    for language, sample in {'en': 'Hope brings peace.', 'lv': source,
                             'lt': 'Viltis atneša ramybę.', 'ru': 'Надежда приносит мир.'}.items():
        audio = tts.synthesize(sample, language, strict=True)
        data, rate = sf.read(io.BytesIO(audio), dtype='float32')
        report['multilingual_tts'][language] = {'ok': len(data) > 0, 'voice': tts._voices[language].voice_name, 'seconds': len(data) / rate}
        if language == 'lv':
            lv_audio, sr = data, rate
    assert sr == 16000
    stt = OpenAITranscriber(cfg, glossary, lambda _: None)
    chain_start = time.monotonic()
    result = stt.transcribe(lv_audio)
    stt_seconds = time.monotonic() - chain_start
    report['openai'] = {'ok': bool(result.text), 'transcript': result.text, 'fixture_kind': 'synthetic Latvian speech'}
    translation_start = time.monotonic()
    while True:
        try:
            spoken = translator.translate(result.text, 'en')
            break
        except GeminiRetryLater as exc:
            if exc.delay > 10:
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
    stt._client.close()
    tts._client.transport.close()
except Exception as exc:
    report['audio_test_error'] = safe_error(exc)
if translator and translator._genai_client:
    translator._genai_client.close()
report_path.parent.mkdir(parents=True, exist_ok=True)
report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding='utf-8')
print(json.dumps(report, ensure_ascii=True))
