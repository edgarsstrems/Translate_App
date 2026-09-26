import unittest
from unittest.mock import MagicMock
import numpy as np

from church_translator.config import load_config
from church_translator.glossary import Glossary
from church_translator.engine import TranslationEngine, EngineSettings
from church_translator.services import LocalWhisperTranscriber, OpenAITranscriber


class SttReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.config = load_config()
        self.settings = EngineSettings(0, True, False, None, None, lambda: 1.0, lambda: 1.0)
        self.engine = TranslationEngine(
            self.config,
            self.settings,
            lambda msg: None,
            lambda err: None,
            lambda lat: None,
            lambda t: None,
            lambda l, t: None,
        )
        self.glossary = Glossary({}, {})

    def test_silence_and_room_noise_classified_as_no_speech(self):
        # 1.6 seconds of digital zero
        zero_audio = np.zeros(16000 * 2, dtype=np.float32)
        vad_zero = self.engine._analyze_speech(zero_audio)
        self.assertFalse(vad_zero.has_speech)

        # 1.5 seconds of typical room tone / analog noise floor (peak 0.003, rms ~0.001)
        rng = np.random.default_rng(42)
        room_noise = rng.normal(0, 0.001, 24000).astype(np.float32)
        vad_noise = self.engine._analyze_speech(room_noise)
        self.assertFalse(vad_noise.has_speech)

    def test_speech_classified_as_has_speech(self):
        # 2 seconds with clear speech burst
        audio = np.zeros(32000, dtype=np.float32)
        # Add 1 second of speech-level signal (sine wave burst at 0.1 amplitude)
        t = np.linspace(0, 1.0, 16000, endpoint=False)
        audio[8000:24000] = 0.1 * np.sin(2 * np.pi * 300 * t)
        vad_speech = self.engine._analyze_speech(audio)
        self.assertTrue(vad_speech.has_speech)
        self.assertGreaterEqual(vad_speech.speech_seconds, 0.9)

    def test_loop_guard_detects_consecutive_identical_segments(self):
        sentence = "Dieva vārds ir mūsu ceļvedis."
        self.assertFalse(self.engine._is_hallucinated_repetition(1, sentence))
        self.engine._record_stt(1, sentence, 100.0)

        # Next segment produces the exact same sentence
        self.assertTrue(self.engine._is_hallucinated_repetition(2, sentence))
        self.assertTrue(self.engine._is_hallucinated_repetition(2, "dieva vārds ir mūsu ceļvedis"))

    def test_loop_guard_allows_distinct_segments(self):
        self.engine._record_stt(1, "Dieva vārds ir mūsu ceļvedis.", 100.0)
        self.assertFalse(self.engine._is_hallucinated_repetition(2, "Un šodien mēs lasām no Pāvila vēstules."))
        self.engine._record_stt(2, "Un šodien mēs lasām no Pāvila vēstules.", 105.0)
        self.assertFalse(self.engine._is_hallucinated_repetition(3, "Tas Kungs ir žēlīgs un lēnprātīgs."))

    def test_collapse_repetitive_tail_whisper_loops(self):
        transcriber = OpenAITranscriber(self.config, self.glossary, lambda _: None)
        # Loop on a 3-word phrase at the tail
        text = "Mēs zinām to, ka Dieva vārds. Dieva vārds. Dieva vārds."
        collapsed = transcriber._collapse_repetitive_tail(text)
        self.assertEqual(collapsed, "Mēs zinām to, ka Dieva vārds.")

        # Single word loop (3+ repeats)
        loop_word = "Mēs pateicamies Tev, Kungs. Āmen. Āmen. Āmen. Āmen."
        collapsed_word = transcriber._collapse_repetitive_tail(loop_word)
        self.assertEqual(collapsed_word, "Mēs pateicamies Tev, Kungs. Āmen.")

        # Natural 2-word liturgical ending "āmen, āmen" is preserved
        natural = "Tas Kungs ir uzticams, āmen, āmen."
        self.assertEqual(transcriber._collapse_repetitive_tail(natural), natural)

    def test_filter_context_leak(self):
        transcriber = OpenAITranscriber(self.config, self.glossary, lambda _: None)
        prev = "Mēs runājam par ticību un cerību"
        # Whisper repeats the last words from previous context
        leaked = "par ticību un cerību, un šodien mēs ejam tālāk."
        filtered = transcriber._filter_context_leak(leaked, previous_context=prev)
        self.assertEqual(filtered, "un šodien mēs ejam tālāk.")

        # Full echo of previous context is discarded
        full_echo = "Mēs runājam par ticību un cerību."
        filtered_echo = transcriber._filter_context_leak(full_echo, previous_context=prev)
        self.assertEqual(filtered_echo, "")

    def test_prompt_hallucinations_stripped(self):
        local_transcriber = LocalWhisperTranscriber(self.config, self.glossary, lambda _: None)
        prompt_echo = "Šis ir kristīgs dievkalpojums un sprediķis latviešu valodā. Pāvils raksta korintiešiem."
        cleaned = local_transcriber._filter_prompt_hallucinations(prompt_echo)
        self.assertEqual(cleaned, "Pāvils raksta korintiešiem.")


if __name__ == "__main__":
    unittest.main()
