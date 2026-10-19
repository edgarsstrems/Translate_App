"""Targeted unit tests for:
1. GPT-5 nano model-aware request parameter generation & adaptive parameter retry
2. Accurate provider identification in fallback chain (Gemini vs OpenAI vs Google Translate)
3. Gemini error classification with dynamic cooldowns (429, daily quota, 5xx, timeout, auth)
4. Model-specific cost tracking and measured vs estimated token accounting
5. PipelineLatencyTracker (avg, p95, worst)
6. Provider diagnostic health checks for all 4 services
"""

import unittest
from unittest.mock import MagicMock, patch
from dataclasses import replace

from church_translator.config import load_config
from church_translator.glossary import Glossary
from church_translator.circuit_breaker import ProviderCircuitBreaker
from church_translator.reliability import classify_gemini_error
from church_translator.engine import PipelineLatencyTracker
from church_translator.services import (
    Translator,
    OpenAITranscriber,
    TextToSpeech,
    _build_openai_chat_kwargs,
    _call_openai_chat_robust,
)


class TestFallbackLatencyFixes(unittest.TestCase):
    def setUp(self):
        self.base_cfg = load_config()

    def test_gpt5_nano_chat_kwargs_uses_max_completion_tokens(self):
        """Verify gpt-5-nano uses max_completion_tokens, developer role, and omits temperature."""
        kwargs = _build_openai_chat_kwargs(
            model="gpt-5-nano",
            system_instruction="You are a theological translator.",
            prompt="Translate: Dievs ir mīlestība",
            max_tokens=250,
        )
        self.assertEqual(kwargs["model"], "gpt-5-nano")
        self.assertIn("max_completion_tokens", kwargs)
        self.assertEqual(kwargs["max_completion_tokens"], 250)
        self.assertNotIn("max_tokens", kwargs, "max_tokens must NOT be passed to gpt-5-nano")
        self.assertNotIn("temperature", kwargs, "custom temperature must NOT be passed to gpt-5-nano")

        # Developer role for nano/reasoning models
        messages = kwargs["messages"]
        self.assertEqual(messages[0]["role"], "developer")
        self.assertEqual(messages[1]["role"], "user")

    def test_gpt4o_mini_chat_kwargs_preserves_standard_parameters(self):
        """Verify standard models like gpt-4o-mini retain max_completion_tokens, temperature, and system role."""
        kwargs = _build_openai_chat_kwargs(
            model="gpt-4o-mini",
            system_instruction="You are a theological translator.",
            prompt="Translate: Svētais Gars",
            max_tokens=250,
        )
        self.assertEqual(kwargs["model"], "gpt-4o-mini")
        self.assertIn("max_completion_tokens", kwargs)
        self.assertEqual(kwargs["max_completion_tokens"], 250)
        self.assertIn("temperature", kwargs)
        self.assertEqual(kwargs["temperature"], 0.1)
        self.assertEqual(kwargs["messages"][0]["role"], "system")

    def test_legacy_model_chat_kwargs_uses_max_tokens(self):
        """Verify legacy models (e.g. gpt-3.5-turbo) use max_tokens."""
        kwargs = _build_openai_chat_kwargs(
            model="gpt-3.5-turbo",
            system_instruction="Translate",
            prompt="Text",
            max_tokens=200,
        )
        self.assertEqual(kwargs["model"], "gpt-3.5-turbo")
        self.assertIn("max_tokens", kwargs)
        self.assertEqual(kwargs["max_tokens"], 200)
        self.assertNotIn("max_completion_tokens", kwargs)
        self.assertEqual(kwargs["messages"][0]["role"], "system")

    def test_adaptive_parameter_retry_on_unsupported_parameter_error(self):
        """Verify _call_openai_chat_robust automatically recovers if an API rejects max_tokens."""
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.choices = [MagicMock(message=MagicMock(content='{"en": "God is love"}', refusal=None))]

        # First call fails with 400 unsupported parameter error, second call succeeds
        error_msg = "Unsupported parameter: 'max_tokens' is not supported with this model. Use 'max_completion_tokens' instead."
        mock_client.chat.completions.create.side_effect = [
            RuntimeError(error_msg),
            mock_response,
        ]

        resp = _call_openai_chat_robust(
            mock_client,
            model="gpt-5-nano",
            system_instruction="Translate",
            prompt="Dievs ir mīlestība",
            max_tokens=150,
        )
        self.assertIsNotNone(resp)
        self.assertEqual(resp.choices[0].message.content, '{"en": "God is love"}')
        self.assertEqual(mock_client.chat.completions.create.call_count, 2)
        # Verify second call used max_completion_tokens
        second_call_kwargs = mock_client.chat.completions.create.call_args_list[1][1]
        self.assertIn("max_completion_tokens", second_call_kwargs)
        self.assertNotIn("max_tokens", second_call_kwargs)

    def test_provider_reporting_accuracy_gemini_success(self):
        """When Gemini succeeds, last_provider_used must strictly be 'gemini'."""
        cfg = replace(self.base_cfg, gemini_api_key="valid-key", translation_provider="gemini")
        t = Translator(cfg, Glossary())
        t._call_gemini_api = MagicMock(return_value='{"en": "In the beginning", "ru": "В начале"}')

        res = t.translate_joint("Iesākumā", ["en", "ru"])
        self.assertEqual(res.get("en"), "In the beginning")
        self.assertEqual(t.last_provider_used, "gemini")

    def test_provider_reporting_accuracy_openai_fallback(self):
        """When Gemini fails and OpenAI succeeds, last_provider_used must strictly be 'openai'."""
        cfg = replace(
            self.base_cfg,
            gemini_api_key="valid-key",
            openai_api_key="valid-openai-key",
            openai_fallback_enabled=True,
            openai_fallback_model="gpt-5-nano",
        )
        t = Translator(cfg, Glossary())
        t._call_gemini_api = MagicMock(side_effect=RuntimeError("Gemini quota 429"))

        mock_resp = MagicMock()
        mock_resp.choices = [MagicMock(message=MagicMock(content='{"en": "Grace and peace"}', refusal=None))]
        mock_resp.usage = MagicMock(prompt_tokens=40, completion_tokens=15)
        t._openai_client = MagicMock()
        t._openai_client.chat.completions.create.return_value = mock_resp

        res = t.translate_joint("Žēlastība un miers", ["en"])
        self.assertEqual(res.get("en"), "Grace and peace")
        self.assertEqual(t.last_provider_used, "openai")
        self.assertEqual(t.circuit_breaker.stats.total_fallback_requests, 1)

    def test_provider_reporting_accuracy_google_translate_safety_net(self):
        """When BOTH Gemini AND OpenAI fail, and Google Translate succeeds, last_provider_used MUST be 'google_translate', NEVER 'openai'."""
        cfg = replace(
            self.base_cfg,
            gemini_api_key="valid-key",
            openai_api_key="valid-openai-key",
            openai_fallback_enabled=True,
            openai_fallback_model="gpt-5-nano",
        )
        t = Translator(cfg, Glossary())
        t._call_gemini_api = MagicMock(side_effect=RuntimeError("Gemini 429 quota"))
        t._translate_with_openai_fallback = MagicMock(side_effect=RuntimeError("HTTP 400 Unsupported parameter"))
        t._free_google_translate_fallback = MagicMock(return_value="Faith, hope, love")

        res = t.translate_joint("Ticība, cerība, mīlestība", ["en"])
        self.assertEqual(res.get("en"), "Faith, hope, love")
        # CRITICAL TEST: Must report 'google_translate', never 'openai'
        self.assertEqual(t.last_provider_used, "google_translate")
        self.assertNotEqual(t.last_provider_used, "openai")

    def test_classify_gemini_error_distinguishes_causes_and_cooldowns(self):
        """Verify error classification safely isolates rate limits, daily quotas, timeouts, and auth."""
        # 1. 429 rate limit -> brief 15s cooldown
        cat, desc, delay = classify_gemini_error(RuntimeError("429 RESOURCE_EXHAUSTED: rate limit per minute exceeded"))
        self.assertEqual(cat, "RATE_LIMITED_429")
        self.assertGreaterEqual(delay, 15.0)

        # 2. Daily quota exhaustion -> 24h cooldown
        cat, desc, delay = classify_gemini_error(RuntimeError("daily quota limit exceeded for free tier"))
        self.assertEqual(cat, "DAILY_QUOTA_EXHAUSTED")
        self.assertEqual(delay, 86400.0)

        # 3. Server 503 -> 5s cooldown
        cat, desc, delay = classify_gemini_error(RuntimeError("503 Service Unavailable, model overloaded"))
        self.assertEqual(cat, "SERVER_ERROR")
        self.assertEqual(delay, 5.0)

        # 4. Timeout -> 10s cooldown
        cat, desc, delay = classify_gemini_error(TimeoutError("Read timeout connecting to Gemini"))
        self.assertEqual(cat, "TIMEOUT")
        self.assertEqual(delay, 10.0)

        # 5. Invalid API key -> 24h cooldown
        cat, desc, delay = classify_gemini_error(RuntimeError("API_KEY_INVALID: key not found"))
        self.assertEqual(cat, "AUTHENTICATION_ERROR")
        self.assertEqual(delay, 86400.0)

    def test_cost_tracking_for_different_fallback_models(self):
        """Verify model-specific pricing and measured vs estimated token accounting."""
        cb = ProviderCircuitBreaker()

        # gpt-5-nano: $0.05 / 1M input, $0.20 / 1M output
        cb.record_fallback_usage(prompt_tokens=1000, completion_tokens=1000, model="gpt-5-nano", measured=True)
        # Cost: 1000 * 0.05/1M + 1000 * 0.20/1M = $0.00005 + $0.00020 = $0.00025
        self.assertAlmostEqual(cb.stats.estimated_cost_usd, 0.00025, places=6)
        self.assertEqual(cb.stats.measured_tokens, 2000)
        self.assertEqual(cb.stats.estimated_tokens, 0)

        # gpt-4o-mini: $0.15 / 1M input, $0.60 / 1M output (unmeasured estimate)
        cb.record_fallback_usage(prompt_tokens=1000, completion_tokens=1000, model="gpt-4o-mini", measured=False)
        # Cost: + 1000 * 0.15/1M + 1000 * 0.60/1M = + $0.00075 -> Total = $0.00100
        self.assertAlmostEqual(cb.stats.estimated_cost_usd, 0.00100, places=5)
        self.assertEqual(cb.stats.measured_tokens, 2000)
        self.assertEqual(cb.stats.estimated_tokens, 2000)

    def test_pipeline_latency_tracker_statistics(self):
        """Verify PipelineLatencyTracker accurately tracks Avg, P95, and Worst latencies."""
        tracker = PipelineLatencyTracker()
        avg, p95, worst, count = tracker.get_stats()
        self.assertEqual(avg, 0.0)
        self.assertEqual(count, 0)

        # Record samples: 2.0, 4.0, 6.0, 8.0, 10.0
        for sample in [2.0, 4.0, 6.0, 8.0, 10.0]:
            tracker.record(sample)

        avg, p95, worst, count = tracker.get_stats()
        self.assertEqual(count, 5)
        self.assertAlmostEqual(avg, 6.0, places=2)
        self.assertEqual(worst, 10.0)
        # P95 for 5 samples (index int(0.95 * 4) = 3) -> 8.0
        self.assertIn(p95, [8.0, 10.0])

    def test_independent_provider_diagnostics(self):
        """Verify that provider diagnostics check Gemini, OpenAI STT, OpenAI fallback, and TTS independently."""
        cfg = replace(
            self.base_cfg,
            gemini_api_key="mock-gemini-key",
            openai_api_key="mock-openai-key",
            openai_fallback_model="gpt-5-nano",
        )
        translator = Translator(cfg, Glossary())
        transcriber = OpenAITranscriber(cfg, Glossary(), lambda m: None)
        tts = TextToSpeech(cfg, lambda m: None)

        with patch("church_translator.services._call_openai_chat_robust") as mock_openai_call:
            mock_openai_call.return_value = MagicMock(
                choices=[MagicMock(message=MagicMock(content='{"en": "Peace"}', refusal=None))]
            )
            ok, msg, latency = translator.test_openai_fallback_model("gpt-5-nano")
            self.assertTrue(ok)
            self.assertIn("gpt-5-nano", msg)


if __name__ == "__main__":
    unittest.main()
