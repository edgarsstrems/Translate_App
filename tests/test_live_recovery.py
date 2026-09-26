import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np

from church_translator.audio import ChunkRecorder
from church_translator.backlog import DurableQueue
from church_translator.config import load_config
from church_translator.engine import TranslationEngine, TranslationItem
from church_translator.glossary import Glossary
from church_translator.reliability import GeminiRetryLater, gemini_retry_delay
from church_translator.services import Translator


class LiveRecoveryTests(unittest.TestCase):
    def test_catalog_keeps_only_stable_text_flash_lite_models(self):
        service = Translator(replace(load_config(), gemini_api_key=None), Glossary())
        service._genai_client = MagicMock()
        def model(name, actions=('generateContent',)):
            value = MagicMock()
            value.name = 'models/' + name
            value.supported_actions = list(actions)
            return value
        service._genai_client.models.list.return_value = [
            model('gemini-3.8-flash-lite'), model('gemini-3.5-flash-lite'),
            model('gemini-3.1-flash-lite-preview'), model('gemini-3.1-flash-lite-image'),
            model('gemini-3.1-flash-lite', ('countTokens',)),
            model('gemini-2.5-flash-lite'), model('gemini-3.7-flash')]
        self.assertEqual(service.refresh_available_models(),
                         ['gemini-3.5-flash-lite', 'gemini-3.8-flash-lite'])

    def test_catalog_failure_uses_verified_fallback_order(self):
        service = Translator(replace(load_config(), gemini_api_key=None), Glossary())
        service._genai_client = MagicMock()
        service._genai_client.models.list.side_effect = TimeoutError('catalog timeout')
        self.assertEqual(service.refresh_available_models(),
                         ['gemini-3.5-flash-lite', 'gemini-3.1-flash-lite'])

    def test_successful_calls_are_paced_for_fifteen_rpm(self):
        service = Translator(replace(load_config(), gemini_api_key=None), Glossary())
        service._call_gemini_api = MagicMock(return_value='translated')
        service._request_translation('one', str)
        with self.assertRaises(GeminiRetryLater) as caught:
            service._request_translation('two', str)
        self.assertTrue(caught.exception.pacing)
        self.assertGreater(caught.exception.delay, 4)
        service._call_gemini_api.assert_called_once()

    def test_gemini3_uses_supported_thinking_and_no_hidden_retry(self):
        service = Translator(replace(load_config(), gemini_api_key=None), Glossary())
        service._genai_client = MagicMock()
        service._genai_client.models.generate_content.return_value.text = 'Hello'
        service._call_gemini_api('gemini-3.5-flash-lite', 'Translate this.')
        kwargs = service._genai_client.models.generate_content.call_args.kwargs
        config = kwargs['config']
        self.assertEqual(config.thinking_config.thinking_level.value, 'MINIMAL')
        self.assertIsNone(config.thinking_config.thinking_budget)
        self.assertEqual(config.http_options.timeout, 10000)
        self.assertEqual(config.http_options.retry_options.attempts, 1)
        self.assertIn('CHRISTIAN THEOLOGY', config.system_instruction)

    def test_pause_noise_and_quiet_speech_are_preserved_without_repeating_samples(self):
        chunks = []
        recorder = ChunkRecorder(0, 5, 0, 2.5, .45, .004, .025,
                                 lambda seq, audio, *args: chunks.append(audio), lambda _: None)
        rng = np.random.default_rng(12)
        noise = rng.normal(0, .00004, 16000 * 30).astype('float32')
        recorder._consume(noise[:16000 * 10])
        speech = (np.sin(np.arange(32000) * .1) * .04).astype('float32')
        recorder._consume(speech)
        recorder._consume(noise)
        recorder.stop()
        combined = np.concatenate(chunks)
        # Every source sample is present once, in order; there is no overlap that
        # can make Whisper hear a boundary word repeatedly.
        expected = np.concatenate([noise[:16000 * 10], speech, noise])
        np.testing.assert_array_equal(combined[-expected.size:], expected)

    def test_cooldown_never_clears_when_all_models_throttled(self):
        service = Translator(replace(load_config(), gemini_api_key=None), Glossary())
        service._call_gemini_api = MagicMock(side_effect=RuntimeError('429 Please retry in 707.6ms.'))
        with self.assertRaises(GeminiRetryLater):
            service._request_translation('text', str)
        self.assertEqual(service._call_gemini_api.call_count, 2)
        with self.assertRaises(GeminiRetryLater):
            service._request_translation('text', str)
        self.assertEqual(service._call_gemini_api.call_count, 2)

    def test_successful_failover_stays_on_working_model(self):
        service = Translator(replace(load_config(), gemini_api_key=None), Glossary())
        service._call_gemini_api = MagicMock(side_effect=[RuntimeError('503 unavailable'), 'One', 'Two'])
        self.assertEqual(service._request_translation('text', str), 'One')
        service._last_call_time -= 4.2
        self.assertEqual(service._request_translation('text', str), 'Two')
        calls = service._call_gemini_api.call_args_list
        self.assertNotEqual(calls[0].args[0], calls[1].args[0])
        self.assertEqual(calls[1].args[0], calls[2].args[0])

    def test_retry_delay_and_daily_quota_are_respected(self):
        self.assertGreaterEqual(gemini_retry_delay(RuntimeError('429 Please retry in 20.5s.')), 20.5)
        self.assertEqual(gemini_retry_delay(RuntimeError('429 PerDay quota exceeded')), 86400)

    def test_engine_recovers_same_pending_request_and_stop_interrupts_wait(self):
        engine = TranslationEngine.__new__(TranslationEngine)
        engine._stop = threading.Event()
        engine.on_status = MagicMock()
        op = MagicMock(side_effect=[GeminiRetryLater(.001), 'translated'])
        self.assertEqual(engine._with_gemini_recovery(op), 'translated')
        engine._stop.set()
        with self.assertRaisesRegex(RuntimeError, 'pending text retained'):
            engine._with_gemini_recovery(MagicMock(side_effect=GeminiRetryLater(86400)))

    def test_backlog_coalescing_survives_reopen_without_loss_or_duplication(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'work.sqlite'
            q = DurableQueue(path, TranslationItem)
            for i, text in enumerate(['first', 'second', 'third', 'fourth']):
                q.put(TranslationItem(0, transcript=text, sequence_id=i))
            q.get()
            merged = q.coalesce_pending(TranslationEngine._combine_translation)
            self.assertEqual(merged.transcript, 'first\nsecond\nthird')
            q.close()
            q = DurableQueue(path, TranslationItem)
            self.assertEqual(q.get().transcript, merged.transcript)
            q.ack()
            self.assertEqual(q.get().transcript, 'fourth')
            q.ack()
            q.close()
