from __future__ import annotations

import unittest
from unittest.mock import MagicMock
import time

from church_translator.config import GEMINI_MODELS, DEFAULT_GEMINI_MODEL, load_config
from church_translator.services import Translator
from church_translator.glossary import Glossary


class TestTranslationPipeline(unittest.TestCase):
    def test_chunking_parameters_preserved(self):
        config = load_config()
        self.assertEqual(config.chunk_seconds, 8.0)
        self.assertEqual(config.min_chunk_seconds, 5.0)
        self.assertEqual(config.early_flush_silence_seconds, 0.6)
        self.assertEqual(config.chunk_overlap_seconds, 0.0)

    def test_gemini_models_centralized_and_valid(self):
        self.assertIn("gemini-2.0-flash-lite", GEMINI_MODELS)
        self.assertIn("gemini-2.0-flash", GEMINI_MODELS)
        self.assertEqual(DEFAULT_GEMINI_MODEL, "gemini-2.0-flash-lite")

    def test_translator_rate_limit_retry(self):
        config = load_config()
        config = type(config)(**{**config.__dict__, "gemini_api_key": "test_key"})
        glossary = Glossary({}, {})
        status_logs = []
        translator = Translator(config, glossary, status_cb=status_logs.append)
        
        call_records = []
        def mock_call_api(model_name, prompt):
            call_records.append(model_name)
            if len(call_records) == 1:
                raise Exception("429 Resource Exhausted")
            return "Hello everyone"

        translator._call_gemini_api = mock_call_api
        result = translator._translate_with_gemini("Labrīt visi", "en")
        self.assertEqual(result, "Hello everyone")
        self.assertEqual(len(call_records), 2)

    def test_translator_no_silent_empty_string_on_fatal_error(self):
        config = load_config()
        config = type(config)(**{**config.__dict__, "gemini_api_key": "test_key"})
        glossary = Glossary({}, {})
        status_logs = []
        translator = Translator(config, glossary, status_cb=status_logs.append)
        
        translator._call_gemini_api = MagicMock(side_effect=Exception("429 Quota Exceeded Permanently"))
        translator._free_google_translate_fallback = MagicMock(side_effect=Exception("Free fallback failed"))

        with self.assertRaises(Exception):
            translator.translate("Labrīt", "en")



if __name__ == "__main__":
    unittest.main()
