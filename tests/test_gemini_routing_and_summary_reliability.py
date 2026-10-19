"""Comprehensive verification tests for:
1. Gemini-first routing reliability and elimination of unnecessary OpenAI calls
2. AI Sermon Summary speed, single-pass processing, cancellation, and error resilience
"""
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from dataclasses import replace

from church_translator.config import load_config
from church_translator.glossary import Glossary
from church_translator.circuit_breaker import ProviderCircuitBreaker
from church_translator.reliability import GeminiRetryLater
from church_translator.session_manager import TranscriptSegment, SessionRecord
from church_translator.services import Translator
from church_translator.summarizer import (
    SermonSummarizer,
    GeminiSummaryError,
    estimate_openai_summary_cost,
)


class GeminiFirstRoutingTests(unittest.TestCase):
    def setUp(self):
        self.config = replace(
            load_config(),
            gemini_api_key="valid-gemini-key",
            openai_api_key="valid-openai-key",
            openai_fallback_enabled=True,
            openai_fallback_model="gpt-4o-mini",
            translation_provider="gemini",
        )
        self.glossary = Glossary()

    def test_healthy_gemini_zero_openai_translation_calls(self):
        """Verify that when Gemini is healthy, zero OpenAI calls are made."""
        translator = Translator(self.config, self.glossary)
        translator._call_gemini_api = MagicMock(return_value="God is love")
        translator._translate_with_openai_fallback = MagicMock()

        result = translator.translate_joint("Dievs ir mīlestība", ["en"])
        self.assertEqual(result.get("en"), "God is love")
        self.assertEqual(translator.last_provider_used, "gemini")
        translator._translate_with_openai_fallback.assert_not_called()
        self.assertEqual(translator.circuit_breaker.stats.total_fallback_requests, 0)

    def test_healthy_gemini_across_100_simulated_segments_zero_openai_calls(self):
        """Simulate 100 consecutive segments with pacing sleep; verify ZERO OpenAI fallback calls."""
        translator = Translator(self.config, self.glossary)
        translator._call_gemini_api = MagicMock(return_value="Amen")
        translator._translate_with_openai_fallback = MagicMock()

        for seg_idx in range(100):
            # Advance clock virtual spacing by updating _last_call_time
            translator._last_call_time = time.monotonic() - 4.5
            result = translator.translate_joint(f"Segment {seg_idx} teksts", ["en"])
            self.assertEqual(result.get("en"), "Amen")
            self.assertEqual(translator.last_provider_used, "gemini")

        translator._translate_with_openai_fallback.assert_not_called()
        self.assertEqual(translator.circuit_breaker.stats.total_fallback_requests, 0)
        self.assertEqual(translator.circuit_breaker.stats.estimated_cost_usd, 0.0)

    def test_temporary_pacing_delay_does_not_falsely_trigger_openai(self):
        """When a segment arrives before 4.2s pacing delay, it must wait and succeed on Gemini, NOT failover to OpenAI."""
        translator = Translator(self.config, self.glossary)
        translator._call_gemini_api = MagicMock(return_value="Grace and peace")
        translator._translate_with_openai_fallback = MagicMock()

        # Set last call time to 2 seconds ago (spacing remaining ~2.2s)
        translator._last_call_time = time.monotonic() - 2.0

        # Mock time.sleep inside pacing wait to avoid real delay in tests
        with patch("time.sleep", return_value=None):
            def fake_wait(max_wait=4.5):
                translator._last_call_time = time.monotonic() - 5.0

            translator._wait_for_pacing = MagicMock(side_effect=fake_wait)
            result = translator.translate_joint("Žēlastība un miers", ["en"])
            self.assertEqual(result.get("en"), "Grace and peace")
            self.assertEqual(translator.last_provider_used, "gemini")
            translator._translate_with_openai_fallback.assert_not_called()

    def test_gemini_rate_limit_activates_correct_fallback_with_reason(self):
        """When Gemini genuinely hits HTTP 429, fallback is activated and reason logged."""
        logs = []
        translator = Translator(self.config, self.glossary, status_cb=logs.append)
        translator._call_gemini_api = MagicMock(side_effect=RuntimeError("429 Resource Exhausted: rate limit exceeded"))

        mock_resp = MagicMock()
        mock_resp.choices = [MagicMock(message=MagicMock(content='{"en": "The Lord is my shepherd"}', refusal=None))]
        mock_resp.usage = MagicMock(prompt_tokens=500, completion_tokens=40)
        translator._openai_client = MagicMock()
        translator._openai_client.chat.completions.create.return_value = mock_resp

        result = translator.translate_joint("Tas Kungs ir mans gans", ["en"])
        self.assertEqual(result.get("en"), "The Lord is my shepherd")
        self.assertEqual(translator.last_provider_used, "openai")
        self.assertEqual(translator.circuit_breaker.stats.total_fallback_requests, 1)

        # Verify visible fallback logs
        self.assertTrue(any("HTTP 429" in log or "rate limit" in log for log in logs))
        self.assertTrue(any("Gemini unavailable within latency budget" in log for log in logs))
        self.assertTrue(any("Activating gpt-4o-mini" in log for log in logs))
        self.assertTrue(any("[COST] Estimated translation cost" in log for log in logs))

    def test_gemini_recovery_auto_return(self):
        """When Gemini recovers, probe succeeds and next translation returns to Gemini."""
        logs = []
        translator = Translator(self.config, self.glossary, status_cb=logs.append)
        
        # Trip circuit breaker
        translator.circuit_breaker.report_gemini_failure("HTTP 429", cooldown_seconds=0.01)
        self.assertEqual(translator.circuit_breaker.active_provider, "openai_fallback")

        time.sleep(0.02)
        # Mock successful recovery probe
        translator._call_gemini_api = MagicMock(return_value="Hello")
        recovered = translator._probe_gemini_recovery()
        self.assertTrue(recovered)
        self.assertEqual(translator.circuit_breaker.active_provider, "gemini")
        self.assertTrue(any("Returned to Gemini primary" in log for log in logs))

    def test_working_alternative_gemini_model_preferred(self):
        """If model 1 is unavailable, alternative configured Gemini model is used before OpenAI."""
        translator = Translator(self.config, self.glossary)
        # First call fails on model 1, succeeds on model 2
        translator._call_gemini_api = MagicMock(side_effect=[RuntimeError("503 Service Unavailable"), "Light of the world"])
        translator._translate_with_openai_fallback = MagicMock()

        result = translator.translate_joint("Pasaules gaisma", ["en"])
        self.assertEqual(result.get("en"), "Light of the world")
        self.assertEqual(translator.last_provider_used, "gemini")
        translator._translate_with_openai_fallback.assert_not_called()

    def test_joint_multilingual_translation_with_gemini(self):
        """Verify joint multilingual translation (EN + LT) uses Gemini with JSON parsing."""
        translator = Translator(self.config, self.glossary)
        translator._call_gemini_api = MagicMock(return_value='{"en": "God is love", "lt": "Dievas yra meilė"}')
        result = translator.translate_joint("Dievs ir mīlestība", ["en", "lt"])
        self.assertEqual(result.get("en"), "God is love")
        self.assertEqual(result.get("lt"), "Dievas yra meilė")
        self.assertEqual(translator.last_provider_used, "gemini")

    def test_openai_fallback_accurate_cost_tracking(self):
        """Verify fallback costs are computed accurately per model."""
        cb = ProviderCircuitBreaker()
        # gpt-4o-mini: 1000 input, 200 output -> 1000 * 0.15/1M + 200 * 0.60/1M = 0.00015 + 0.00012 = $0.00027
        call_cost = cb.record_fallback_usage(1000, 200, "gpt-4o-mini", measured=True)
        self.assertAlmostEqual(call_cost, 0.00027, places=6)
        self.assertAlmostEqual(cb.stats.estimated_cost_usd, 0.00027, places=6)


class AISummaryReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.config = replace(
            load_config(),
            gemini_api_key="valid-gemini-key",
            openai_api_key="valid-openai-key",
        )
        self.segments = [
            TranscriptSegment(
                segment_id=1,
                captured_at=time.time(),
                timestamp_str="10:00:00",
                latvian_text="Šodien mēs lasām no Jāņa evaņģēlija 3:16.",
                english_translation="Today we read from the Gospel of John 3:16.",
            ),
            TranscriptSegment(
                segment_id=2,
                captured_at=time.time() + 5,
                timestamp_str="10:00:05",
                latvian_text="Jo tik ļoti Dievs pasauli mīlējis, ka devis savu vienpiedzimušo Dēlu.",
                english_translation="For God so loved the world that He gave His only begotten Son.",
            ),
        ]

    def test_short_transcript_generates_summary(self):
        """Short transcript generates a complete structured summary."""
        summarizer = SermonSummarizer(self.config)
        expected = "1. Sermon Title: God's Love\n2. Main Message: John 3:16\n8. Short Recap: Believe in Christ."
        with patch.object(summarizer, "_call_gemini", return_value=expected):
            res = summarizer.generate_summary(self.segments, language="en")
            self.assertIn("John 3:16", res)

    def test_long_sermon_single_pass_efficiency(self):
        """90-120 min sermon (~15,000 words) executes in a single pass without unnecessary chunking."""
        summarizer = SermonSummarizer(self.config)
        long_text = " ".join(["Mācītājs skaidro Rakstus un ticības ceļu."] * 2500)
        long_segs = [
            TranscriptSegment(
                segment_id=1,
                captured_at=time.time(),
                timestamp_str="10:00:00",
                latvian_text=long_text,
                english_translation="Pastor explains the Scriptures.",
            )
        ]
        with patch.object(summarizer, "_call_gemini", return_value="1. Sermon Title: Faith\n2. Main Message: Faith brings hope.\n8. Short Recap: Walk in faith.") as mock_call:
            res = summarizer.generate_summary(long_segs, language="en")
            self.assertEqual(mock_call.call_count, 1)
            self.assertIn("Walk in faith", res)

    def test_cancellation_works_promptly(self):
        """Cancellation aborts summary immediately."""
        summarizer = SermonSummarizer(self.config)
        with self.assertRaises(InterruptedError):
            summarizer.generate_summary(self.segments, cancel_check=lambda: True)

    def test_gemini_timeout_produces_clear_error(self):
        """Timeout produces clear GeminiSummaryError without hanging indefinitely."""
        summarizer = SermonSummarizer(self.config)
        with patch.object(summarizer, "_call_gemini_single_model", side_effect=TimeoutError("Request timed out after 30s")):
            with self.assertRaises(GeminiSummaryError) as ctx:
                summarizer.generate_summary(self.segments)
            self.assertIn("timed out", str(ctx.exception).lower())

    def test_gemini_http_429_handled(self):
        """HTTP 429 raises GeminiSummaryError with rate limit information."""
        summarizer = SermonSummarizer(self.config)
        with patch.object(summarizer, "_call_gemini_single_model", side_effect=RuntimeError("429 Resource Exhausted")):
            with self.assertRaises(GeminiSummaryError) as ctx:
                summarizer.generate_summary(self.segments)
            self.assertTrue(ctx.exception.is_quota_or_rate_limit)

    def test_latvian_and_english_summary_languages(self):
        """Summary prompt correctly passes requested language."""
        summarizer = SermonSummarizer(self.config)
        captured_prompts = []
        def fake_call(prompt, **kwargs):
            captured_prompts.append(prompt)
            return "1. Sermon Title: Summary\n2. Main Message: Faith brings hope."

        with patch.object(summarizer, "_call_gemini", side_effect=fake_call):
            summarizer.generate_summary(self.segments, language="lv")
            self.assertIn("Latvian", captured_prompts[0])

            summarizer.generate_summary(self.segments, language="en")
            self.assertIn("English", captured_prompts[1])

    def test_summary_exports_to_txt_and_markdown(self):
        """Export helpers write valid structured text and markdown files."""
        summary_text = "1. Sermon Title: Grace\n2. Main Message: Christ died for us."
        with tempfile.TemporaryDirectory() as tmp_dir:
            p_txt = Path(tmp_dir) / "summary.txt"
            p_md = Path(tmp_dir) / "summary.md"
            SermonSummarizer.export_summary_to_txt(p_txt, "Grace", summary_text)
            SermonSummarizer.export_summary_to_markdown(p_md, "Grace", summary_text)

            self.assertTrue(p_txt.exists())
            self.assertIn("Grace", p_txt.read_text(encoding="utf-8"))
            self.assertTrue(p_md.exists())
            self.assertIn("# Sermon Summary: Grace", p_md.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
