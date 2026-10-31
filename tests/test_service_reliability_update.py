"""Audio-backed silence suppression and Gemini recovery/metrics regressions."""
from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

import numpy as np

from church_translator.config import load_config
from church_translator.engine import TranslationEngine, EngineSettings, ProcessingItem
from church_translator.glossary import Glossary
from church_translator.reliability import classify_gemini_error
from church_translator.services import Translator
from church_translator.speech_audit import SpeechAudit
from church_translator.speech_gate import audio_evidence


class ServiceReliabilityUpdateTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        env = patch.dict(os.environ, {'CHURCH_TRANSLATOR_APPDATA': folder.name})
        env.start()
        self.addCleanup(env.stop)

    @staticmethod
    def noise():
        # Equivalent amplitude/distribution to the retained virtual-device floor.
        return np.random.default_rng(100).choice(
            np.array([-1 / 32768, 0, 0, 0, 0, 0, 0, 1 / 32768], 'float32'), 32000)

    def test_stationary_floor_has_no_context_gain_or_recognition(self):
        result = audio_evidence(self.noise())
        self.assertTrue(result['skip_stt'], result)
        self.assertFalse(result['use_context'])
        self.assertFalse(result['allow_gain'])

    def test_quietness_alone_never_discards_speech_or_a_short_tail(self):
        t = np.arange(32000) / 16000
        audio = (1e-5 * np.sin(2 * np.pi * 180 * t) * (.6 + .4 * np.sin(2 * np.pi * 3 * t))).astype('float32')
        self.assertFalse(audio_evidence(audio)['skip_stt'])
        self.assertFalse(audio_evidence(audio[:73])['skip_stt'])

    def test_vad_failure_preserves_ambiguous_audio_unprompted(self):
        with patch('faster_whisper.vad.get_vad_model', side_effect=RuntimeError('missing asset')):
            result = audio_evidence(self.noise())
        self.assertFalse(result['skip_stt'])
        self.assertFalse(result['use_context'])
        self.assertFalse(result['allow_gain'])

    def test_silence_cannot_repeat_translate_or_speak_last_sentence(self):
        cfg = replace(load_config(), gemini_api_key=None)
        with patch.object(Translator, '_init_gemini_client'):
            engine = TranslationEngine(cfg, EngineSettings(0, True, False, None, None, lambda: 0, lambda: 0),
                MagicMock(), MagicMock(), MagicMock(), MagicMock(), MagicMock())
        for queue in (engine._chunks, engine._translations, engine._speech, engine._unrecognized):
            self.addCleanup(queue.close)
        engine._last_spoken_transcript = 'Nē!'
        engine._transcriber = MagicMock()
        engine._transcriber.transcribe.return_value = SimpleNamespace(text='Nē!', uncertain=False)
        for seq in range(4):
            engine._chunks.put(ProcessingItem(seq, 1, audio=self.noise()))
        engine._chunks.finish()
        with patch('church_translator.engine.ChunkRecorder'):
            engine._process_loop()
        engine._transcriber.transcribe.assert_not_called()
        engine.on_transcript.assert_not_called()
        self.assertEqual(engine._translations.qsize(), 0)
        self.assertEqual(engine._speech.qsize(), 0)
        self.assertEqual(engine._unrecognized.qsize(), 4)

    def translator(self):
        cfg = replace(load_config(), gemini_api_key='fixture', openai_api_key='fixture',
                      openai_fallback_enabled=True, auto_return_to_gemini=True)
        with patch.object(Translator, '_init_gemini_client'):
            return Translator(cfg, Glossary())

    def test_recovery_uses_current_translation_instead_of_parallel_probe(self):
        for languages in (['en'], ['en', 'lt']):
            service = self.translator()
            service.circuit_breaker.report_gemini_failure('temporary', cooldown_seconds=0)
            result = json.dumps({l: 'Hope.' for l in languages})
            service._call_gemini_api = MagicMock(return_value=result)
            service._translate_with_openai_fallback = MagicMock()
            self.assertEqual(set(service.translate_joint('Cerība.', languages)), set(languages))
            service._translate_with_openai_fallback.assert_not_called()
            service._call_gemini_api.assert_called_once()
            self.assertIn('Cerība.', service._call_gemini_api.call_args.args[1])

    def test_verified_second_gemini_precedes_openai_after_transient_failure(self):
        service = self.translator()
        service._verified_models.add('gemini-3.1-flash-lite')
        service._call_gemini_api = MagicMock(side_effect=[TimeoutError('timed out'), '{"en":"Hope."}'])
        service._translate_with_openai_fallback = MagicMock()
        self.assertEqual(service.translate('Cerība.', 'en'), 'Hope.')
        self.assertEqual(service._call_gemini_api.call_count, 2)
        service._translate_with_openai_fallback.assert_not_called()

    def test_long_retry_after_header_is_respected(self):
        error = RuntimeError('429 RESOURCE_EXHAUSTED')
        error.response = SimpleNamespace(headers={'Retry-After': '120'})
        self.assertGreaterEqual(classify_gemini_error(error)[2], 120)

    def test_translation_metrics_include_failed_jobs_without_counting_retries_twice(self):
        audit = SpeechAudit(self.root / 'audit.sqlite')
        for seq in (0, 0, 1, 2):
            audit.event('translation_attempt', seq, {})
        audit.event('translated', 0, {'provider': 'gemini'})
        audit.event('translated', 1, {'provider': 'openai'})
        stats = audit.integrity_report()['translation_metrics']
        self.assertEqual(stats['attempted_batches'], 3)
        self.assertEqual(stats['gemini_share_of_completed'], .5)
        self.assertAlmostEqual(stats['gemini_share_of_attempted'], 1 / 3)

    def test_concurrent_audit_writers_do_not_race_schema_initialization(self):
        from concurrent.futures import ThreadPoolExecutor
        audit = SpeechAudit(self.root / 'concurrent.sqlite')
        def write(seq):
            audit.capture('fixture', seq * 320, np.full(320, .1, 'float32'), {})
            audit.event('captured_segment', seq, {'capture': {'capture_id': 'fixture',
                        'start_sample': seq * 320, 'end_sample': (seq + 1) * 320}})
        with ThreadPoolExecutor(max_workers=4) as executor:
            list(executor.map(write, range(40)))
        report = audit.integrity_report()
        self.assertEqual(report['journaled_samples'], 12800)
        self.assertEqual(report['uncovered_ranges'], [])


if __name__ == '__main__':
    unittest.main()
