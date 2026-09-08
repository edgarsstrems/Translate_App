from __future__ import annotations

import unittest
from unittest.mock import MagicMock
import time
from pathlib import Path

from church_translator.config import GEMINI_MODELS, DEFAULT_GEMINI_MODEL, load_config
from church_translator.engine import (
    calculate_safety_timeout,
    is_dangling_connector,
    has_true_sentence_boundary,
    split_sentence_boundary,
    clean_splice,
)
from church_translator.services import Translator, LocalWhisperTranscriber, OpenAITranscriber
from church_translator.glossary import load_glossary, Glossary


class TestTranslationPipeline(unittest.TestCase):
    def test_chunking_parameters_preserved(self):
        config = load_config()
        self.assertEqual(config.chunk_seconds, 10.0)
        self.assertEqual(config.min_chunk_seconds, 1.2)
        self.assertEqual(config.early_flush_silence_seconds, 0.70)
        self.assertEqual(config.chunk_overlap_seconds, 0.0)
        self.assertEqual(config.vad_min_speech_seconds, 0.20)
        self.assertEqual(config.vad_padding_seconds, 0.50)
        self.assertTrue(config.smart_sentence_stitching)

    def test_gemini_models_centralized_and_valid(self):
        self.assertIn("gemini-flash-latest", GEMINI_MODELS)
        self.assertIn("gemini-flash-lite-latest", GEMINI_MODELS)
        self.assertIn("gemini-3.5-flash-lite", GEMINI_MODELS)
        self.assertIn("gemini-3.5-flash", GEMINI_MODELS)
        self.assertIn("gemini-3.6-flash", GEMINI_MODELS)
        self.assertEqual(DEFAULT_GEMINI_MODEL, "gemini-flash-latest")

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
        
        # When chunk_overlap_seconds > 0, boundary overlap is stripped
        config_overlap = type(config)(**{**config.__dict__, "chunk_overlap_seconds": 1.5})
        transcriber = LocalWhisperTranscriber(config_overlap, glossary, lambda msg: None)
        transcriber._previous_text = "Dievs svētī mūsu draudzi un"
        current_chunk = "mūsu draudzi un dāvā mums mieru"
        deduped = transcriber._dedupe_against_previous(current_chunk)
        self.assertEqual(deduped, "dāvā mums mieru")

        # When chunk_overlap_seconds == 0.0, no overlap exists so valid starting phrases are PRESERVED!
        config_no_overlap = type(config)(**{**config.__dict__, "chunk_overlap_seconds": 0.0})
        transcriber_no_overlap = LocalWhisperTranscriber(config_no_overlap, glossary, lambda msg: None)
        transcriber_no_overlap._previous_text = "Mēs lūdzam Jēzus vārdā. Tas Kungs ir labs."
        clean = transcriber_no_overlap._dedupe_against_previous("Tas Kungs ir uzticams.")
        self.assertEqual(clean, "Tas Kungs ir uzticams.")

        # Test repeated spoken words like "Āmen" are NOT wiped out
        transcriber_no_overlap._previous_text = "Mēs lūdzam Jēzus vārdā. Āmen."
        repeated_amen = "Āmen."
        deduped_amen = transcriber_no_overlap._dedupe_against_previous(repeated_amen)
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

        # Test case 5: Debesu Tēvs, Pestītājs, Evaņģēlijs normalization
        raw5 = "mūsu debess tevs un pestitajs sludina evangeliju visiem braļiem"
        clean5 = glossary.apply_source_replacements(raw5)
        self.assertIn("Debesu Tēvs", clean5)
        self.assertIn("Pestītājs", clean5)
        self.assertIn("Evaņģēliju", clean5)
        self.assertIn("brāļiem", clean5)

        # Test case 6: Prayer / approach context "nākt pie vīna" -> "nākt pie Viņa"
        raw6 = "mēs nākam lūgšanā pie vīna"
        clean6 = glossary.apply_source_replacements(raw6)
        self.assertIn("pie Viņa", clean6)
        self.assertNotIn("pie vīna", clean6)

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

        # Test with full Latvian sermon prompt
        full_prompt_sample = (
            "Kristīgs dievkalpojums un sprediķis latviešu valodā. "
            "Tas Kungs, Dievs Tēvs, Jēzus Kristus, Svētais Gars, Bībele, Svētie Raksti, Evaņģēlijs, "
            "lūgšana, ticība, cerība, mīlestība, žēlastība, pestīšana, svētība, draudze, brāļi un māsas, Āmen, Aleluja. "
            "Dzīvojam ticībā un mierā."
        )
        cleaned_full = local_transcriber._filter_prompt_hallucinations(full_prompt_sample)
        self.assertEqual(cleaned_full, "Dzīvojam ticībā un mierā.")

        # Test with new fluent Latvian sermon prompt
        new_prompt_sample = (
            "Šis ir kristīgs dievkalpojums un sprediķis latviešu valodā. "
            "Mēs runājam par To Kungu, Dievu Tēvu, Jēzu Kristu, Svēto Garu, Bībeli un Svētajiem Rakstiem, "
            "Evaņģēliju, lūgšanu, ticību, cerību, mīlestību, žēlastību, pestīšanu, draudzi, brāļiem un māsām. Āmen. "
            "Mēs pateicamies par šo dienu."
        )
        cleaned_new = local_transcriber._filter_prompt_hallucinations(new_prompt_sample)
        self.assertEqual(cleaned_new, "Mēs pateicamies par šo dienu.")

        # OpenAI transcriber
        openai_transcriber = OpenAITranscriber(config, glossary, lambda msg: None)
        cleaned_openai = openai_transcriber._filter_prompt_hallucinations(hallucinated_sample)
        self.assertEqual(cleaned_openai, "Staigājam ikdienā ar to Kungu.")

    def test_whisper_hotwords_built_from_glossary(self):
        config = load_config()
        glossary = load_glossary(Path(__file__).resolve().parents[1])
        local_transcriber = LocalWhisperTranscriber(config, glossary, lambda msg: None)
        hotwords = local_transcriber._build_hotwords()
        self.assertIsNotNone(hotwords)
        self.assertIn("Tas Kungs", hotwords)
        self.assertIn("Jēzus Kristus", hotwords)
        self.assertIn("Svētais Gars", hotwords)

    def test_dynamic_safety_timeout(self):
        # max(16s, chunk_duration + 5s)
        self.assertEqual(calculate_safety_timeout(10.0), 16.0)
        self.assertEqual(calculate_safety_timeout(14.0), 19.0)
        self.assertEqual(calculate_safety_timeout(7.0), 16.0)
        self.assertEqual(calculate_safety_timeout(5.0), 16.0)

    def test_dangling_connector_detection(self):
        # Latvian single and multi-word connectors
        self.assertTrue(is_dangling_connector("Mēs zinām, ka"))
        self.assertTrue(is_dangling_connector("Mēs zinām, ka..."))
        self.assertTrue(is_dangling_connector("Mēs zinām, ka…"))
        self.assertTrue(is_dangling_connector("Dievs ir mīlestība, un"))
        self.assertTrue(is_dangling_connector("Mēs ticam, jo"))
        self.assertTrue(is_dangling_connector("Nākam lūgšanā, lai"))
        self.assertTrue(is_dangling_connector("Mēs ticam, tāpēc ka"))
        self.assertFalse(is_dangling_connector("Tas Kungs ir mūsu patvērums."))

        # English connectors
        self.assertTrue(is_dangling_connector("We believe that"))
        self.assertTrue(is_dangling_connector("God is good, because"))
        self.assertTrue(is_dangling_connector("We praise Him, and..."))
        self.assertFalse(is_dangling_connector("Jesus Christ is alive."))

        # Russian connectors
        self.assertTrue(is_dangling_connector("Мы верим, что"))
        self.assertTrue(is_dangling_connector("Господь благ, потому что"))
        self.assertTrue(is_dangling_connector("Иисус молился, чтобы..."))
        self.assertFalse(is_dangling_connector("Бог есть любовь."))

    def test_true_sentence_boundaries(self):
        # Terminal marks (.!?) are boundaries
        self.assertTrue(has_true_sentence_boundary("Tas Kungs ir mans gans."))
        self.assertTrue(has_true_sentence_boundary("Tas Kungs ir mans gans!"))
        self.assertTrue(has_true_sentence_boundary("Vai Tas Kungs ir tavs gans?"))

        # Ellipses are continuation markers, never boundaries
        self.assertFalse(has_true_sentence_boundary("Tas Kungs ir mans gans..."))
        self.assertFalse(has_true_sentence_boundary("Tas Kungs ir mans gans…"))
        self.assertFalse(has_true_sentence_boundary("Tas Kungs ir mans gans.."))

        # Clause ending in dangling connector even with period is NOT complete
        self.assertFalse(has_true_sentence_boundary("Mēs zinām, ka."))
        self.assertFalse(has_true_sentence_boundary("Mēs zinām, ka"))

        # Abbreviation is not a sentence boundary
        self.assertFalse(has_true_sentence_boundary("Dievkalpojums sākas plkst."))

    def test_clean_splice(self):
        # Boundary ellipses and commas cleanly spliced
        spliced1 = clean_splice("Mēs zinām, ka Dievs mūs mīl,...", "...un dāvā mums mūžīgo dzīvību.")
        self.assertEqual(spliced1, "Mēs zinām, ka Dievs mūs mīl, un dāvā mums mūžīgo dzīvību.")

        # Lowercase normal leading word of head chunk, but preserve deity names
        spliced2 = clean_splice("Mēs zinām, ka", "... Viņš mūs mīl.")
        self.assertEqual(spliced2, "Mēs zinām, ka viņš mūs mīl.")

        spliced3 = clean_splice("Mēs zinām, ka", "... Dievs mūs mīl.")
        self.assertEqual(spliced3, "Mēs zinām, ka Dievs mūs mīl.")

        # Subordinate clause ensures comma
        spliced4 = clean_splice("Mēs ticam...", "ka Jēzus Kristus ir Kungs.")
        self.assertEqual(spliced4, "Mēs ticam, ka Jēzus Kristus ir Kungs.")

    def test_split_sentence_boundary(self):
        # Fully complete sentence
        complete1, trailing1 = split_sentence_boundary("Tas Kungs ir mans gans.")
        self.assertEqual(complete1, "Tas Kungs ir mans gans.")
        self.assertEqual(trailing1, "")

        # Incomplete clause (entire chunk)
        complete2, trailing2 = split_sentence_boundary("Tas Kungs ir mans gans, jo...")
        self.assertEqual(complete2, "")
        self.assertEqual(trailing2, "Tas Kungs ir mans gans, jo...")

        # Complete sentence + trailing incomplete clause
        complete3, trailing3 = split_sentence_boundary("Tas Kungs ir mans gans. Viņš mani vada pie ūdeņiem un...")
        self.assertEqual(complete3, "Tas Kungs ir mans gans.")
        self.assertEqual(trailing3, "Viņš mani vada pie ūdeņiem un...")

    def test_chunk_recorder_prespeech_and_dynamic_chunking(self):
        import numpy as np
        from church_translator.audio import ChunkRecorder, SAMPLE_RATE

        collected_chunks = []
        def on_chunk(chunk_idx, audio, captured_at, leading_ctx):
            collected_chunks.append((chunk_idx, audio, captured_at))

        recorder = ChunkRecorder(
            device_index=0,
            chunk_seconds=10.0,
            overlap_seconds=0.0,
            min_chunk_seconds=1.8,
            early_flush_silence_seconds=0.70,
            silence_rms_threshold=0.015,
            silence_peak_threshold=0.040,
            on_chunk=on_chunk,
            on_error=lambda err: None,
            pre_speech_seconds=0.40,
        )

        block_size = int(SAMPLE_RATE * 0.1) # 0.1s blocks = 1600 frames
        silent_block = np.zeros((block_size, 1), dtype=np.float32)
        speech_block = np.full((block_size, 1), 0.10, dtype=np.float32) # rms ~ 0.10, peak ~ 0.10

        # 1. Feed 2.0 seconds of dead silence (20 blocks)
        for _ in range(20):
            recorder._callback(silent_block, block_size, None, None)
        # Verify: No chunks should be dispatched during silence!
        self.assertEqual(len(collected_chunks), 0)
        # Pre-speech buffer should hold at most ~400ms (4 blocks)
        self.assertLessEqual(recorder._pre_speech_collected, int(SAMPLE_RATE * 0.5))

        # 2. Preacher speaks for 2.0 seconds (20 speech blocks)
        for _ in range(20):
            recorder._callback(speech_block, block_size, None, None)
        # Not flushed yet because speaker hasn't paused
        self.assertEqual(len(collected_chunks), 0)
        self.assertTrue(recorder._is_recording_speech)

        # 3. Preacher pauses for 0.4s (4 silent blocks - shorter than 0.7s)
        for _ in range(4):
            recorder._callback(silent_block, block_size, None, None)
        # Still not flushed (paused < 0.70s)
        self.assertEqual(len(collected_chunks), 0)

        # 4. Preacher resumes speaking for 0.5s (5 speech blocks)
        for _ in range(5):
            recorder._callback(speech_block, block_size, None, None)
        # Speech resumed -> pause counter reset, still no flush
        self.assertEqual(len(collected_chunks), 0)

        # 5. Natural sentence ending: Preacher pauses for 0.8s (8 silent blocks >= 0.70s)
        for _ in range(8):
            recorder._callback(silent_block, block_size, None, None)
        # Chunk flushed immediately at natural boundary!
        self.assertEqual(len(collected_chunks), 1)
        chunk_idx, audio, _ = collected_chunks[0]
        self.assertEqual(chunk_idx, 0)
        # Audio includes pre-speech buffer + speech + pause: ~3.7s audio
        duration = audio.size / SAMPLE_RATE
        self.assertGreater(duration, 2.5)
        self.assertLess(duration, 5.0)

        # 6. Another 2.0s of silence -> no new chunks produced
        for _ in range(20):
            recorder._callback(silent_block, block_size, None, None)
        self.assertEqual(len(collected_chunks), 1)

    def test_gemini_continuation_prompt_includes_context_note(self):
        config = load_config()
        config = type(config)(**{**config.__dict__, "gemini_api_key": "test_key"})
        glossary = Glossary({}, {})
        translator = Translator(config, glossary)

        captured_prompts = []
        def mock_call(model_name, prompt):
            captured_prompts.append(prompt)
            return '{"en": "transforms our lives.", "ru": "преображает нашу жизнь."}'

        translator._call_gemini_api = mock_call
        results = translator._translate_joint_with_gemini(
            "pārvērš mūsu dzīvi.",
            ["en", "ru"],
            previous_transcript="Mēs zinām, ka Dieva vārds",
            is_split_continuation=True,
        )
        self.assertEqual(results.get("en"), "transforms our lives.")
        self.assertEqual(len(captured_prompts), 1)
        prompt_text = captured_prompts[0]
        self.assertIn("NOTE ON ONGOING SENTENCE / CHUNK CONTINUATION", prompt_text)
        self.assertIn("Mēs zinām, ka Dieva vārds", prompt_text)

    def test_device_selection_and_persistence(self):
        from PySide6.QtWidgets import QApplication, QComboBox
        from church_translator.audio import AudioDevice
        from church_translator.main import MainWindow

        app = QApplication.instance() or QApplication([])

        mock_devices = [
            AudioDevice(index=1, name="Focusrite USB Audio", max_input_channels=2, max_output_channels=0),
            AudioDevice(index=2, name="Realtek High Definition Audio", max_input_channels=2, max_output_channels=0),
            AudioDevice(index=3, name="Headphones (Realtek)", max_input_channels=0, max_output_channels=2),
        ]

        combo = QComboBox()
        for d in mock_devices:
            combo.addItem(d.label, d.index)

        # 1. Exact match by name & index
        MainWindow._select_saved_device(None, combo, mock_devices, {"index": 2, "name": "Realtek High Definition Audio"}, include_default=False)
        self.assertEqual(combo.currentData(), 2)

        # 2. Index shifted (device index 2 moved to index 5 on USB re-enum, but name unchanged)
        shifted_devices = [
            AudioDevice(index=5, name="Realtek High Definition Audio", max_input_channels=2, max_output_channels=0)
        ]
        combo_shifted = QComboBox()
        for d in shifted_devices:
            combo_shifted.addItem(d.label, d.index)
        MainWindow._select_saved_device(None, combo_shifted, shifted_devices, {"index": 2, "name": "Realtek High Definition Audio"}, include_default=False)
        self.assertEqual(combo_shifted.currentData(), 5)

        # 3. Disconnected device preserves name with placeholder without crash
        combo_disc = QComboBox()
        combo_disc.addItem("Builtin Mic [0]", 0)
        MainWindow._select_saved_device(None, combo_disc, [AudioDevice(0, "Builtin Mic", 1, 0)], {"index": 9, "name": "Wireless Mic Pro"}, include_default=False)
        self.assertIn("disconnected", combo_disc.currentText())
        self.assertTrue(combo_disc.currentData().get("missing"))

    def test_short_phrase_speech_retention(self):
        """Verify that short phrases like 'Pazaudējat savu bērnu' (0.9s speech in 2.1s chunk) are NOT dropped."""
        import numpy as np
        from church_translator.engine import TranslationEngine, EngineSettings

        config = load_config()
        settings = EngineSettings(
            input_device_index=0,
            english_enabled=True,
            russian_enabled=False,
            english_output_device_index=None,
            russian_output_device_index=None,
            english_volume_getter=lambda: 1.0,
            russian_volume_getter=lambda: 1.0,
        )
        engine = TranslationEngine(
            config=config,
            settings=settings,
            on_status=lambda msg: None,
            on_error=lambda msg: None,
            on_latency=lambda lat: None,
            on_transcript=lambda txt: None,
            on_translation=lambda lang, txt: None,
        )

        # Create a 2.1s audio chunk (16000 Hz):
        # 0.4s pre-speech silence, 0.9s speech audio (e.g. sine wave), 0.8s trailing pause
        total_samples = int(16000 * 2.1)
        audio = np.zeros(total_samples, dtype=np.float32)
        speech_start = int(16000 * 0.4)
        speech_end = int(16000 * 1.3)
        t = np.linspace(0, 0.9, speech_end - speech_start, endpoint=False)
        audio[speech_start:speech_end] = 0.20 * np.sin(2 * np.pi * 400 * t).astype(np.float32)

        vad = engine._analyze_speech(audio, 0.0)
        self.assertTrue(vad.has_speech, "Short speech phrase was incorrectly classified as no-speech!")
        self.assertGreaterEqual(vad.speech_seconds, 0.20)

    def test_gemini_candidate_models_active(self):
        """Verify that Translator's model candidates only include active, supported models."""
        config = load_config()
        translator = Translator(config, Glossary({}, {}))
        candidates = translator._get_model_candidates()
        for c in candidates:
            self.assertNotIn("2.0", c)
            self.assertNotIn("1.5", c)
            self.assertTrue("flash" in c)

    def test_audio_pulse_vad_preservation(self):
        """Verify that a 0.5s audio pulse in a 2.0s chunk is NOT skipped by _analyze_speech()."""
        import numpy as np
        from church_translator.engine import TranslationEngine, EngineSettings

        config = load_config()
        settings = EngineSettings(
            input_device_index=0,
            english_enabled=True,
            russian_enabled=False,
            english_output_device_index=None,
            russian_output_device_index=None,
            english_volume_getter=lambda: 1.0,
            russian_volume_getter=lambda: 1.0,
        )
        engine = TranslationEngine(
            config=config,
            settings=settings,
            on_status=lambda msg: None,
            on_error=lambda msg: None,
            on_latency=lambda lat: None,
            on_transcript=lambda txt: None,
            on_translation=lambda lang, txt: None,
        )

        # 2.0s audio chunk with 0.5s audio pulse in the middle
        total_samples = int(16000 * 2.0)
        audio = np.zeros(total_samples, dtype=np.float32)
        start_idx = int(16000 * 0.5)
        end_idx = int(16000 * 1.0)
        t = np.linspace(0, 0.5, end_idx - start_idx, endpoint=False)
        audio[start_idx:end_idx] = 0.25 * np.sin(2 * np.pi * 350 * t).astype(np.float32)

        vad = engine._analyze_speech(audio, 0.0)
        self.assertTrue(vad.has_speech, "0.5s audio pulse in 2.0s chunk was skipped by _analyze_speech()!")
        self.assertGreaterEqual(vad.speech_seconds, 0.20)

    def test_gemini_404_not_found_triggers_day_cooldown(self):
        """Verify that 404 NOT_FOUND errors trigger extended 86400s cooldown and immediate candidate failover."""
        config = load_config()
        config = type(config)(**{**config.__dict__, "gemini_api_key": "test_key"})
        status_logs = []
        translator = Translator(config, Glossary({}, {}), status_cb=status_logs.append)

        calls = []
        def mock_call_api(model_name, prompt):
            calls.append(model_name)
            if len(calls) == 1:
                raise Exception("404 NOT_FOUND: models/gemini-2.0-flash is not found")
            return "Success after failover"

        translator._call_gemini_api = mock_call_api
        result = translator._translate_with_gemini("Miera jums", "en")
        self.assertEqual(result, "Success after failover")
        self.assertEqual(len(calls), 2)
        # First model should be cooling down for ~86400s
        first_model = calls[0]
        self.assertTrue(translator._is_cooling_down(first_model))
        remaining_cd = translator._model_cooldowns[first_model] - time.monotonic()
        self.assertGreater(remaining_cd, 80000.0)


if __name__ == "__main__":
    unittest.main()



