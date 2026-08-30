from __future__ import annotations

import unittest
from unittest.mock import MagicMock
import time
from pathlib import Path

from church_translator.config import GEMINI_MODELS, DEFAULT_GEMINI_MODEL, load_config
from church_translator.services import Translator, LocalWhisperTranscriber, OpenAITranscriber
from church_translator.glossary import load_glossary, Glossary


class TestTranslationPipeline(unittest.TestCase):
    def test_chunking_parameters_preserved(self):
        config = load_config()
        self.assertEqual(config.chunk_seconds, 4.5)
        self.assertEqual(config.min_chunk_seconds, 2.5)
        self.assertEqual(config.early_flush_silence_seconds, 0.4)
        self.assertEqual(config.chunk_overlap_seconds, 0.0)

    def test_gemini_models_centralized_and_valid(self):
        self.assertIn("gemini-2.0-flash", GEMINI_MODELS)
        self.assertIn("gemini-2.0-flash-lite", GEMINI_MODELS)
        self.assertIn("gemini-1.5-flash", GEMINI_MODELS)
        self.assertIn("gemini-1.5-flash-8b", GEMINI_MODELS)
        self.assertEqual(DEFAULT_GEMINI_MODEL, "gemini-2.0-flash")

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

    def test_glossary_source_replacements_no_kungs_to_lord(self):
        glossary = load_glossary(Path(__file__).resolve().parents[1])
        # "Kungs" should NOT be translated to "Lord" in source replacements
        latvian_sample = "Mūsu Kungs Jēzus Kristus ir dzīvs"
        replaced = glossary.apply_source_replacements(latvian_sample)
        self.assertIn("Kungs", replaced)
        self.assertNotIn("Lord", replaced)

    def test_whisper_deduplication_boundary_overlap(self):
        config = load_config()
        glossary = Glossary({}, {})
        transcriber = LocalWhisperTranscriber(config, glossary, lambda msg: None)
        transcriber._previous_text = "Dievs svētī mūsu draudzi un"
        
        # Test true boundary overlap stripping
        current_chunk = "mūsu draudzi un dāvā mums mieru"
        deduped = transcriber._dedupe_against_previous(current_chunk)
        self.assertEqual(deduped, "dāvā mums mieru")

        # Test repeated spoken words like "Āmen" are NOT wiped out
        transcriber._previous_text = "Mēs lūdzam Jēzus vārdā. Āmen."
        repeated_amen = "Āmen."
        deduped_amen = transcriber._dedupe_against_previous(repeated_amen)
        self.assertEqual(deduped_amen, "Āmen.")

    def test_sermon_acoustic_and_phonetic_normalization(self):
        glossary = load_glossary(Path(__file__).resolve().parents[1])

        # Test case 1 from user: "ne tā, kā tas kungstoa ir. radīsu ir veidojis."
        raw1 = "ne tā, kā tas kungstoa ir. radīsu ir veidojis."
        clean1 = glossary.apply_source_replacements(raw1)
        self.assertIn("Tas Kungs to", clean1)
        self.assertIn("radījis", clean1)
        self.assertNotIn("kungstoa", clean1)
        self.assertNotIn("radīsu", clean1)

        # Test case 2 from user: "Visvis mūsu personīgās attiecības sākas... Ar vīņu."
        raw2 = "Visvis mūsu personīgās attiecības sākas... Ar vīņu."
        clean2 = glossary.apply_source_replacements(raw2)
        self.assertIn("Viss", clean2)
        self.assertIn("Ar Viņu", clean2)
        self.assertNotIn("vīņu", clean2)

        # Test case 3: "attiecībās ar vīnu" -> "attiecībās ar Viņu" (Him, not wine)
        raw3 = "viss mūsu personīgajās attiecībās ar vīnu"
        clean3 = glossary.apply_source_replacements(raw3)
        self.assertIn("attiecībās ar Viņu", clean3)

        # Test case 4: Literal wine (e.g. Communion / Lord's Supper) must be PRESERVED
        raw_communion1 = "Mācītājs pacēla kausu ar vīnu un maizi."
        clean_communion1 = glossary.apply_source_replacements(raw_communion1)
        self.assertIn("kausu ar vīnu", clean_communion1)
        self.assertNotIn("kausu ar Viņu", clean_communion1)

        raw_communion2 = "Jēzus pārvērta ūdeni par vīnu Kānas kāzās."
        clean_communion2 = glossary.apply_source_replacements(raw_communion2)
        self.assertIn("par vīnu", clean_communion2)

    def test_translator_joint_passes_context_and_theology_instructions(self):
        config = load_config()
        config = type(config)(**{**config.__dict__, "gemini_api_key": "test_key"})
        glossary = load_glossary(Path(__file__).resolve().parents[1])
        translator = Translator(config, glossary)
        
        # Populate history
        translator._remember_joint_context(
            "caur Jēzu Kristu Svēto Garu viņa dzīve ir piesātināta.",
            {"en": "Through Jesus Christ and the Holy Spirit, his life is enriched."}
        )

        captured_prompts = []
        def mock_call_api(model_name, prompt):
            captured_prompts.append(prompt)
            return '{"en": "Everything in our personal relationship begins with Him."}'

        translator._call_gemini_api = mock_call_api
        results = translator.translate_joint("Visvis mūsu personīgās attiecības sākas... Ar vīņu.", ["en"])
        
        self.assertEqual(len(captured_prompts), 1)
        sent_prompt = captured_prompts[0]
        # Check theological rules and previous context presence
        self.assertIn("CHRISTIAN THEOLOGY & SERMON CONTEXT", sent_prompt)
        self.assertIn("with Him", sent_prompt)
        self.assertIn("caur Jēzu Kristu Svēto Garu", sent_prompt)
        self.assertIn("Tas Kungs", sent_prompt)
        self.assertIn("en", results)

    def test_transcribers_filter_prompt_hallucinations(self):
        config = load_config()
        glossary = Glossary({}, {})
        
        # Local whisper transcriber
        local_transcriber = LocalWhisperTranscriber(config, glossary, lambda msg: None)
        hallucinated_sample = "Kristīgs dievkalpojums, sprediķis, Dievs, Jēzus Kristus, Svētais Gars, Bībele, lūgšana, ticība, draudze. Staigājam ikdienā ar to Kungu."
        cleaned_local = local_transcriber._filter_prompt_hallucinations(hallucinated_sample)
        self.assertEqual(cleaned_local, "Staigājam ikdienā ar to Kungu.")

        # OpenAI transcriber
        openai_transcriber = OpenAITranscriber(config, glossary, lambda msg: None)
        cleaned_openai = openai_transcriber._filter_prompt_hallucinations(hallucinated_sample)
        self.assertEqual(cleaned_openai, "Staigājam ikdienā ar to Kungu.")


if __name__ == "__main__":
    unittest.main()
