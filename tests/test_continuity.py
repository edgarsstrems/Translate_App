import io
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import soundfile as sf

from church_translator.audio import ChunkRecorder, OrderedAudioPlayer, SAMPLE_RATE
from church_translator.backlog import DurableQueue
from church_translator.config import load_config, save_user_settings, load_user_settings
from church_translator.engine import TranslationEngine, EngineSettings, ProcessingItem
from church_translator.services import OpenAITranscriber, TranscriptionResult, Translator, TextToSpeech
from church_translator.glossary import Glossary
from church_translator.reliability import retry_call, safe_error


def recorder_for(chunks):
    return ChunkRecorder(0, 10, 0, 1.2, .7, .004, .025,
                         lambda seq, audio, at, ctx: chunks.append((seq, audio)),
                         lambda error: (_ for _ in ()).throw(AssertionError(error)))


class ContinuityTests(unittest.TestCase):
    def test_forced_boundaries_preserve_every_sample_and_final_syllable(self):
        chunks = []
        rec = recorder_for(chunks)
        rng = np.random.default_rng(42)
        original = rng.uniform(.04, .1, SAMPLE_RATE * 31 + 73).astype('float32')
        for start in range(0, len(original), 733):
            data = original[start:start + 733, None]
            rec._callback(data, len(data), None, None)
        rec.stop()
        np.testing.assert_array_equal(np.concatenate([a for _, a in chunks]), original)
        self.assertEqual([i for i, _ in chunks], list(range(len(chunks))))
        self.assertTrue(all(len(a) <= 10 * SAMPLE_RATE for _, a in chunks))
        self.assertEqual(len(chunks[-1][1]), SAMPLE_RATE + 73)

    def test_recent_breath_boundary_retains_carryover(self):
        chunks = []
        rec = recorder_for(chunks)
        speech = np.full(1600, .1, dtype='float32')
        breath = np.zeros(1600, dtype='float32')
        blocks = [speech] * 80 + [breath] * 2 + [speech] * 29
        for block in blocks:
            rec._callback(block[:, None], len(block), None, None)
        rec.stop()
        self.assertGreater(len(chunks[0][1]), 7 * SAMPLE_RATE)
        self.assertLess(len(chunks[0][1]), 9 * SAMPLE_RATE)
        np.testing.assert_array_equal(np.concatenate([a for _, a in chunks]), np.concatenate(blocks))

    def test_short_quiet_words_and_pauses_are_not_deleted(self):
        chunks = []
        rec = recorder_for(chunks)
        blocks = []
        for amplitude, count in [(0, 20), (.001, 1), (0, 15), (.08, 12), (0, 3), (.02, 20), (0, 30), (.04, 1)]:
            blocks.extend([np.full(1600, amplitude, dtype='float32')] * count)
        for block in blocks:
            rec._callback(block[:, None], len(block), None, None)
        rec.stop()
        original = np.concatenate(blocks)
        result = np.concatenate([a for _, a in chunks])
        np.testing.assert_array_equal(result[result != 0], original[original != 0])

    def test_backlog_keeps_old_and_unacknowledged_work_across_reopen(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'queue.sqlite'
            q = DurableQueue(path, ProcessingItem)
            for i in range(100):
                q.put(ProcessingItem(i, 0, audio=np.full(17, i, dtype='float32')))
            self.assertEqual(q.get().chunk_index, 0)
            q.close()  # crash/failure before acknowledgement
            q = DurableQueue(path, ProcessingItem)
            for i in range(100):
                item = q.get()
                self.assertEqual(item.chunk_index, i)
                np.testing.assert_array_equal(item.audio, np.full(17, i))
                q.ack()
            q.close()
            self.assertFalse(path.exists())

    def test_repeated_sermon_words_reach_openai_result_unchanged(self):
        config = replace(load_config(), openai_api_key='test', chunk_overlap_seconds=1)
        transcriber = OpenAITranscriber(config, Glossary(), lambda _: None)
        transcriber._client = MagicMock()
        repeated = 'Āmen. Āmen. Āmen. Tas Kungs to ir radījis un veidojis.'
        transcriber._client.audio.transcriptions.create.return_value.text = repeated
        result = transcriber.transcribe(np.ones(1600, dtype='float32') * .01)
        self.assertEqual(result.text, repeated)
        kwargs = transcriber._client.audio.transcriptions.create.call_args.kwargs
        self.assertEqual(kwargs['language'], 'lv')
        self.assertEqual(kwargs['response_format'], 'json')
        self.assertNotIn('prompt', kwargs)

    def test_settings_atomic_backup_recovery(self):
        with tempfile.TemporaryDirectory() as folder, patch('church_translator.config.settings_path', return_value=Path(folder) / 'settings.json'):
            first = {'devices': {'input': {'name': 'USB', 'identity': 'WASAPI|USB'}}}
            save_user_settings(first)
            save_user_settings({'devices': {'input': {'name': 'Other'}}})
            (Path(folder) / 'settings.json').write_text('{broken', encoding='utf-8')
            self.assertEqual(load_user_settings(), first)

    def test_retry_is_bounded_and_auth_is_not_retried(self):
        with patch('church_translator.reliability.time.sleep'):
            op = MagicMock(side_effect=[TimeoutError(), 'ok'])
            self.assertEqual(retry_call(op), 'ok')
            self.assertEqual(op.call_count, 2)
            op = MagicMock(side_effect=RuntimeError('401 bad key'))
            with self.assertRaises(RuntimeError):
                retry_call(op)
            self.assertEqual(op.call_count, 1)

    def test_context_is_bounded_even_for_huge_segments(self):
        translator = Translator(replace(load_config(), gemini_api_key=None), Glossary())
        for _ in range(1000):
            translator._remember_joint_context('a' * 10000, {'en': 'b' * 10000})
        self.assertLessEqual(len(translator._history), 8)
        self.assertLessEqual(len(translator._get_sermon_context_prompt(['en'])), 10000)

    def test_pipeline_keeps_capturing_while_all_services_are_busy_and_drains(self):
        with tempfile.TemporaryDirectory() as folder, patch('church_translator.engine.app_data_dir', return_value=Path(folder)), patch('church_translator.engine.ChunkRecorder') as recorder, patch('church_translator.audio.sd.OutputStream') as output:
            output.return_value.__enter__.return_value.write.return_value = False
            config = replace(load_config(), gemini_api_key=None)
            heard, translated, errors = [], [], []
            settings = EngineSettings(0, True, False, None, None, lambda: 1., lambda: 1.)
            engine = TranslationEngine(config, settings, lambda _: None, errors.append, lambda _: None, heard.append, lambda lang, text: translated.append(text))
            gate = threading.Event()
            started = threading.Event()
            def transcribe(audio, ctx):
                started.set()
                gate.wait(5)
                time.sleep(.005)
                return TranscriptionResult(f'Segments {int(audio[0] * 100)}.', False)
            engine._prepare_whisper_audio = lambda a, r, p: (a, 1.)
            engine._transcriber = MagicMock()
            engine._transcriber.transcribe.side_effect = transcribe
            engine._translator = MagicMock()
            engine._translator.translate_joint.side_effect = lambda text, langs, **kw: {'en': text}
            wav = io.BytesIO()
            sf.write(wav, np.zeros(160), 16000, format='WAV')
            engine._tts = MagicMock()
            engine._tts.synthesize.side_effect = lambda *args: (time.sleep(.005) or wav.getvalue())
            recorder.return_value.last_boundary = 'forced'
            recorder.return_value.last_continues_previous = True
            engine.start()
            for i in range(1, 21):
                engine._on_chunk(i, np.full(1600, i / 100, dtype='float32'), 0, 0)
            self.assertTrue(started.wait(2))
            self.assertGreaterEqual(engine._chunks.qsize(), 19)
            gate.set()
            engine.stop()
            self.assertEqual(len(heard), 20)
            # Free-tier catch-up may combine adjacent segments into one request,
            # but every transcript must remain present exactly once and in order.
            spoken_lines = [line for group in translated for line in group.splitlines()]
            self.assertEqual(heard, spoken_lines)
            self.assertEqual(output.return_value.__enter__.return_value.write.call_count,
                             len(translated))
            self.assertEqual(errors, [])
            self.assertFalse(engine._processor_thread.is_alive())
            self.assertFalse(engine.session_dir.exists())

    def test_google_tts_transient_retry_does_not_disable_cloud(self):
        config = replace(load_config(), google_application_credentials='configured.json')
        tts = TextToSpeech(config)
        tts._client = MagicMock()
        response = MagicMock(audio_content=b'wave')
        tts._client.synthesize_speech.side_effect = [TimeoutError(), response]
        with patch('church_translator.reliability.time.sleep'):
            self.assertEqual(tts.synthesize('Hello', 'en'), b'wave')
        self.assertFalse(tts._cloud_tts_disabled)

    def test_playback_failure_retains_current_and_later_audio_without_retry(self):
        with tempfile.TemporaryDirectory() as folder, patch('church_translator.audio.sd.OutputStream') as output:
            output.return_value.__enter__.return_value.write.side_effect = RuntimeError('USB disconnected')
            errors = []
            player = OrderedAudioPlayer('en', 3, lambda: 1., errors.append, Path(folder) / 'play.sqlite')
            wav = io.BytesIO()
            sf.write(wav, np.ones(160) * .1, 16000, format='WAV')
            player.enqueue(wav.getvalue())
            player.enqueue(wav.getvalue())
            player.start()
            player.stop()
            self.assertEqual(output.return_value.__enter__.return_value.write.call_count, 1)
            q = DurableQueue(Path(folder) / 'play.sqlite')
            self.assertEqual(q.qsize(), 2)
            q.close()
            self.assertIn('partially played', errors[0])

    def test_normalization_preserves_waveform_without_clipping(self):
        engine = TranslationEngine.__new__(TranslationEngine)
        wave = (.002 * np.sin(np.arange(16000) * 2 * np.pi * 320 / 16000) + .01).astype('float32')
        audio, gain = engine._prepare_whisper_audio(wave, float(np.sqrt(np.mean(wave * wave))), float(max(wave)))
        self.assertLessEqual(gain, 12)
        self.assertAlmostEqual(float(np.mean(audio)), 0, places=6)
        self.assertLess(float(np.max(np.abs(audio))), .98)
        self.assertGreater(np.corrcoef(audio, wave)[0, 1], .99999)

    def test_very_quiet_speech_is_kept_and_boosted_without_distortion(self):
        chunks = []
        rec = recorder_for(chunks)
        samples = (np.sin(np.arange(SAMPLE_RATE * 2) * 2 * np.pi * 190 / SAMPLE_RATE)
                   * .0002).astype('float32')
        for start in range(0, samples.size, 320):
            block = samples[start:start + 320]
            rec._callback(block[:, None], len(block), None, None)
        rec.stop()
        captured = np.concatenate([audio for _, audio in chunks])
        np.testing.assert_array_equal(captured[-samples.size:], samples)

        engine = TranslationEngine.__new__(TranslationEngine)
        rms = float(np.sqrt(np.mean(samples * samples)))
        boosted, gain = engine._prepare_whisper_audio(samples, rms, float(np.max(np.abs(samples))))
        self.assertAlmostEqual(gain, 12.0)
        self.assertGreater(float(np.sqrt(np.mean(boosted * boosted))), rms * 11.9)
        self.assertLess(float(np.max(np.abs(boosted))), .98)
        self.assertGreater(np.corrcoef(boosted, samples)[0, 1], .99999)

    def test_glossary_is_guidance_not_transcript_replacement_in_translation(self):
        glossary = Glossary({'pie vīna': 'pie Viņa'}, {'Pāvils': {'en': 'Paul'}})
        translator = Translator(replace(load_config(), gemini_api_key='test', translation_provider='gemini'), glossary)
        prompts = []
        translator._call_gemini_api = lambda model, prompt: (prompts.append(prompt) or 'Near the wine.')
        translator.translate('Viņš stāvēja pie vīna.', 'en')
        self.assertIn('Viņš stāvēja pie vīna.', prompts[0])
        self.assertIn('Pāvils', prompts[0])


if __name__ == '__main__':
    unittest.main()
