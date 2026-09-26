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

root, report_path = Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve()
config.project_root = lambda: root
cfg = replace(config.load_config(), openai_transcription_model='gpt-4o-transcribe', translation_provider='gemini')
glossary = load_glossary(root)
source = 'Šodien mēs runājam par apustuli Pāvilu. Jēzus Kristus mums dod cerību un mieru.'
report = {'fixture': source, 'model': cfg.openai_transcription_model}
translator = None
try:
    translator = Translator(cfg, glossary)
    translated = translator.translate(source, 'en')
    report['gemini'] = {'ok': True, 'translation': translated}
except Exception as exc:
    report['gemini'] = {'ok': False, 'error': safe_error(exc)}
try:
    import soundfile as sf
    from google.cloud import texttospeech
    from google.oauth2 import service_account
    if not cfg.google_application_credentials:
        raise RuntimeError('No configured Google Cloud service account')
    credentials = service_account.Credentials.from_service_account_file(cfg.google_application_credentials)
    client = texttospeech.TextToSpeechClient(credentials=credentials)
    tts = TextToSpeech(cfg)
    audio = tts.synthesize(report.get('gemini', {}).get('translation', 'Jesus Christ gives us hope and peace.'), 'en')
    data, rate = sf.read(io.BytesIO(audio))
    report['google_tts'] = {'ok': True, 'audio_seconds': len(data) / rate}
    voices = client.list_voices(language_code='lv-LV', timeout=15, retry=None).voices
    if not voices:
        raise RuntimeError('No Latvian Cloud TTS voice available for synthetic transcription fixture')
    response = client.synthesize_speech(
        input=texttospeech.SynthesisInput(text=source),
        voice=texttospeech.VoiceSelectionParams(language_code='lv-LV', name=voices[0].name),
        audio_config=texttospeech.AudioConfig(audio_encoding=texttospeech.AudioEncoding.LINEAR16, sample_rate_hertz=16000),
        timeout=15, retry=None)
    lv_audio, sr = sf.read(io.BytesIO(response.audio_content), dtype='float32')
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
    final_audio = tts.synthesize(spoken, 'en')
    report['serial_chain'] = {'ok': bool(final_audio), 'stt_seconds': round(stt_seconds, 2),
        'translation_seconds': round(translation_seconds, 2),
        'tts_seconds': round(time.monotonic() - tts_start, 2),
        'after_capture_to_audio_ready_seconds': round(time.monotonic() - chain_start, 2),
        'translation': spoken}
    stt._client.close()
    client.transport.close()
    tts._client.transport.close()
except Exception as exc:
    report['audio_test_error'] = safe_error(exc)
if translator and translator._genai_client:
    translator._genai_client.close()
report_path.parent.mkdir(parents=True, exist_ok=True)
report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding='utf-8')
print(json.dumps(report, ensure_ascii=True))
