"""Opt-in fixed synthetic STT/translation/TTS and silent ordered output check.

Configured services may incur usage. No private data or microphone is read.
Physical output is muted; its timing is estimated from PortAudio, not a listener.
"""
import argparse
from dataclasses import replace
import io
import json
from pathlib import Path
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import av
import numpy as np
import soundfile as sf
from church_translator.audio import OrderedAudioPlayer
from church_translator.config import load_config
from church_translator.glossary import Glossary
from church_translator.services import OpenAITranscriber, Translator, TextToSpeech


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('report', type=Path)
    parser.add_argument('--live', action='store_true')
    args = parser.parse_args()
    if not args.live:
        parser.error('--live is required for configured provider usage')
    args.report = args.report.resolve()
    args.report.parent.mkdir(parents=True, exist_ok=True)
    cfg = load_config()
    # Diagnostic-only voice override; never save or migrate user preferences.
    voices = dict(cfg.voice_settings)
    voices['en'] = {'voice': 'en-US-Chirp3-HD-Iapetus', 'rate': .95, 'pitch': 0, 'gain': 0}
    tts = TextToSpeech(replace(cfg, voice_settings=voices))
    stt = OpenAITranscriber(cfg, Glossary(), lambda _: None)
    translator = Translator(replace(cfg, auto_return_to_gemini=False), Glossary())
    report = {'ok': False, 'fixture_kind': 'fixed synthetic, silent output',
              'paid_translation_consent': cfg.openai_fallback_enabled}
    try:
        fixture = 'Šodien mēs runājam par mieru un cerību. Rīt autobuss izbrauks pulksten deviņos.'
        native = tts.synthesize(fixture, 'lv', strict=True)
        frames = []
        with av.open(io.BytesIO(native)) as container:
            resampler = av.AudioResampler(format='flt', layout='mono', rate=16000)
            for frame in container.decode(audio=0):
                frames.extend(f.to_ndarray().flatten() for f in resampler.resample(frame))
            frames.extend(f.to_ndarray().flatten() for f in resampler.resample(None))
        samples = np.concatenate(frames).astype('float32')
        begin = time.monotonic()
        recognized = stt.transcribe(samples)
        stt_end = time.monotonic()
        translator.source_uncertain = recognized.uncertain
        translated = translator.translate_joint(recognized.text, ['en'])['en']
        translation_end = time.monotonic()
        output = tts.synthesize(translated, 'en', strict=True)
        tts_end = time.monotonic()
        info = sf.info(io.BytesIO(output))
        logs, errors = [], []
        with tempfile.TemporaryDirectory(dir=args.report.parent) as directory:
            player = OrderedAudioPlayer('en', None, lambda: 0., errors.append,
                                        Path(directory) / 'silent.sqlite', logs.append)
            player.enqueue(output, 'en', captured_at=begin - len(samples) / 16000, sequence_id=0)
            player.enqueue(output, 'en', captured_at=begin - len(samples) / 16000, sequence_id=1)
            player.start()
            player.stop()
        assert not errors, errors
        assert sum('[PLAYBACK] English started' == line for line in logs) == 2
        report.update(ok=True, synthetic_source=fixture, transcript=recognized.text,
            uncertain=recognized.uncertain, translation=translated, provider=translator.last_provider_used,
            source_audio_seconds=len(samples) / 16000, stt_seconds=stt_end - begin,
            translation_seconds=translation_end - stt_end, tts_seconds=tts_end - translation_end,
            post_capture_to_speech_ready_seconds=tts_end - begin,
            simulated_capture_start_to_speech_ready_seconds=len(samples) / 16000 + tts_end - begin,
            voice=tts._voices['en'].voice_name, speaking_rate=tts._voices['en'].speaking_rate,
            output_audio_seconds=info.frames / info.samplerate, ordered_silent_segments=2,
            playback_diagnostics=logs, playback_errors=errors,
            limits='One short synthetic sample. Source audio is pre-generated. Silent real-device output verifies stream reuse/drain, not audible quality or natural service latency.')
    finally:
        for client in (stt._client, translator._genai_client, translator._openai_client):
            if client:
                client.close()
        if tts._client:
            tts._client.transport.close()
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({k: v for k, v in report.items() if k not in {'synthetic_source', 'transcript', 'translation', 'playback_diagnostics'}}))


if __name__ == '__main__':
    main()
