"""Regressions for the observed slow-Gemini backlog and hidden output state."""
import io
import os
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import numpy as np
import soundfile as sf

from church_translator.config import load_config
from church_translator.engine import EngineSettings, ProcessingItem, TranslationEngine
from church_translator.glossary import Glossary
from church_translator.reliability import GeminiRetryLater
from church_translator.services import Translator
from church_translator.session_manager import SessionManager


class TranslationDeadlineTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        environment = patch.dict(os.environ, {'CHURCH_TRANSLATOR_APPDATA': temporary.name})
        environment.start()
        self.addCleanup(environment.stop)
        self.config = replace(load_config(), gemini_api_key='fixture-gemini',
                              openai_api_key='fixture-openai', openai_fallback_enabled=True,
                              translation_provider='gemini', gemini_timeout_seconds=4.0)

    def translator(self, config=None, logs=None):
        with patch.object(Translator, '_init_gemini_client'):
            return Translator(config or self.config, Glossary(), (logs if logs is not None else []).append)

    def test_actual_sdk_request_uses_configured_deadline_and_single_attempt(self):
        service = self.translator()
        service._genai_client = MagicMock()
        service._genai_client.models.generate_content.return_value = SimpleNamespace(text='Hello.')
        with patch('church_translator.reliability.pace_gemini_request'):
            self.assertEqual(service._call_gemini_api('gemini-3.5-flash-lite', 'Labdien.'), 'Hello.')
        sent = service._genai_client.models.generate_content.call_args.kwargs['config']
        self.assertEqual(sent.http_options.timeout, 4000)
        self.assertEqual(sent.http_options.headers['X-Server-Timeout'], '10')
        self.assertEqual(sent.http_options.retry_options.attempts, 1)
        self.assertTrue(sent.automatic_function_calling.disable)

    def test_legacy_sdk_also_obeys_deadline_without_hidden_retries(self):
        service = self.translator()
        service._legacy_genai = MagicMock()
        service._legacy_genai.GenerativeModel.return_value.generate_content.return_value = SimpleNamespace(text='Hello.')
        with patch('church_translator.reliability.pace_gemini_request'):
            service._call_gemini_api('gemini-3.1-flash-lite', 'Labdien.')
        options = service._legacy_genai.GenerativeModel.return_value.generate_content.call_args.kwargs['request_options']
        self.assertEqual(options, {'timeout': 4.0, 'retry': None})

    def test_sdk_transport_keeps_client_timeout_and_valid_server_deadline_separate(self):
        import httpx
        from google import genai
        from google.genai import types
        requests = []
        def respond(request):
            requests.append(request)
            return httpx.Response(200, json={'candidates': [
                {'content': {'parts': [{'text': 'Hello.'}], 'role': 'model'}, 'finishReason': 'STOP'}]})
        client = genai.Client(api_key='fixture-gemini', http_options=types.HttpOptions(
            client_args={'transport': httpx.MockTransport(respond)}))
        self.addCleanup(client.close)
        service = self.translator()
        service._genai_client = client
        with patch('church_translator.reliability.pace_gemini_request'):
            self.assertEqual(service._call_gemini_api('gemini-3.5-flash-lite', 'Labdien.'), 'Hello.')
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].headers['X-Server-Timeout'], '10')
        self.assertEqual(requests[0].extensions['timeout']['read'], 4.)
        self.assertEqual(requests[0].extensions['timeout']['connect'], 4.)

    def test_recovery_pacing_uses_request_start_instead_of_completion(self):
        service = self.translator()
        clock = [10.]
        def respond(*args):
            clock[0] = 14.
            return 'Hello.'
        service._wait_for_pacing = MagicMock()
        service._call_gemini_api = MagicMock(side_effect=respond)
        with patch('church_translator.services.time.monotonic', side_effect=lambda: clock[0]):
            self.assertTrue(service._probe_gemini_recovery())
        self.assertEqual(service._last_call_time, 10.)

    def test_real_gemini_failure_precedes_single_authorized_fallback(self):
        for languages in (['en'], ['en', 'lt']):
            for failure in (TimeoutError('Request timed out'), RuntimeError('503 Service Unavailable'),
                            RuntimeError('429 Resource Exhausted')):
                with self.subTest(languages=languages, error=str(failure)):
                    events = []
                    service = self.translator()
                    def gemini(*args):
                        events.append('gemini')
                        raise failure
                    def fallback(text, requested, **kwargs):
                        events.append('openai')
                        return {language: 'translated' for language in requested}
                    service._call_gemini_api = MagicMock(side_effect=gemini)
                    service._translate_with_openai_fallback = MagicMock(side_effect=fallback)
                    self.assertEqual(set(service.translate_joint('Ticība dod cerību.', languages)), set(languages))
                    self.assertEqual(events, ['gemini', 'openai'])
                    service._call_gemini_api.assert_called_once()
                    service._translate_with_openai_fallback.assert_called_once()

    def test_healthy_multilingual_gemini_never_calls_openai(self):
        service = self.translator()
        service._call_gemini_api = MagicMock(return_value='{"en":"Hope brings peace.","lt":"Viltis teikia ramybę."}')
        service._translate_with_openai_fallback = MagicMock()
        self.assertEqual(set(service.translate_joint('Cerība nes mieru.', ['en', 'lt'])), {'en', 'lt'})
        service._translate_with_openai_fallback.assert_not_called()

    def test_local_pacing_never_counts_as_failure_for_paid_fallback(self):
        for languages in (['en'], ['en', 'lt']):
            with self.subTest(languages=languages):
                service = self.translator()
                service._translate_with_gemini = MagicMock(side_effect=GeminiRetryLater(9, pacing=True))
                service._translate_joint_with_gemini = MagicMock(side_effect=GeminiRetryLater(9, pacing=True))
                service._translate_with_openai_fallback = MagicMock()
                with self.assertRaises(GeminiRetryLater) as failure:
                    service.translate_joint('Cerība.', languages)
                self.assertTrue(failure.exception.pacing)
                service._translate_with_openai_fallback.assert_not_called()

    def test_incomplete_multilingual_response_is_failure_before_fallback(self):
        service = self.translator()
        service._call_gemini_api = MagicMock(return_value='{"en":"Hope."}')
        service._translate_with_openai_fallback = MagicMock(return_value={'en': 'Hope.', 'lt': 'Viltis.'})
        result = service.translate_joint('Cerība.', ['en', 'lt'])
        self.assertEqual(set(result), {'en', 'lt'})
        self.assertEqual(service._call_gemini_api.call_count, 2)
        self.assertEqual(len({call.args[0] for call in service._call_gemini_api.call_args_list}), 1)
        service._translate_with_openai_fallback.assert_called_once()

    def test_disabled_fallback_keeps_recovery_and_never_calls_paid_provider(self):
        service = self.translator(replace(self.config, openai_fallback_enabled=False))
        service._call_gemini_api = MagicMock(side_effect=TimeoutError('Request timed out'))
        service._translate_with_openai_fallback = MagicMock()
        service._free_google_translate_fallback = MagicMock()
        with self.assertRaises(GeminiRetryLater):
            service.translate_joint('Cerība.', ['en', 'lt'])
        service._translate_with_openai_fallback.assert_not_called()
        service._free_google_translate_fallback.assert_not_called()

    def test_ordered_dual_output_pipeline_drains_after_gemini_failure(self):
        logs, errors, heard, translated, spoken = [], [], [], [], []
        manager = SessionManager(self.root / 'sessions')
        sid = manager.start_session(secondary_language='lt')
        settings = EngineSettings(0, True, True, 3, 3, lambda: 1., lambda: .7, 'lt')
        started = threading.Event()
        with patch('church_translator.engine.ChunkRecorder') as recorder, \
                patch('church_translator.audio.sd.OutputStream') as output:
            recorder.return_value.start.side_effect = started.set
            output.return_value.__enter__.return_value.write.return_value = False
            engine = TranslationEngine(self.config, settings, logs.append, errors.append, lambda _: None,
                                       heard.append, lambda lang, text: translated.append((lang, text)),
                                       session_manager=manager)
            engine._transcriber = MagicMock()
            engine._translator._call_gemini_api = MagicMock(side_effect=TimeoutError('Request timed out'))
            engine._translator._translate_with_openai_fallback = MagicMock(
                side_effect=lambda text, languages, **kwargs: {language: text for language in languages})
            engine._translator.refresh_available_models = MagicMock()
            wav = io.BytesIO()
            sf.write(wav, np.zeros(160), 16000, format='WAV')
            engine._tts = MagicMock()
            engine._tts.synthesize.side_effect = lambda text, lang: (spoken.append((lang, text)) or wav.getvalue())
            engine.start()
            self.assertTrue(started.wait(2))
            source = ['Dievs dod cerību.', 'Cerība nes mieru.']
            for index, text in enumerate(source):
                engine._chunks.put(ProcessingItem(index, time.monotonic() - 1,
                                                  manual_text=text, enqueued_at=time.monotonic()))
            engine.stop()
        manager.end_session()
        self.assertEqual(errors, [])
        self.assertEqual(heard, source)
        self.assertEqual([row.latvian_text for row in manager.get_segments(sid)], source)
        for language in ('en', 'lt'):
            self.assertEqual([line for lang, text in translated if lang == language for line in text.splitlines()], source)
            self.assertEqual([line for lang, text in spoken if lang == language for line in text.splitlines()], source)
        engine._translator._call_gemini_api.assert_called_once()
        self.assertTrue(any('stage=translation' in line and 'capture_to_text=' in line for line in logs))
        self.assertTrue(any('stage=playback' in line and 'capture_to_output_start=' in line for line in logs))
        self.assertFalse(engine.session_dir.exists())
