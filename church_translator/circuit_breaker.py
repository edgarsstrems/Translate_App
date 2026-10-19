"""Circuit breaker and provider health tracker for zero-latency translation failover.

Guarantees Gemini-first routing, fast OpenAI emergency fallback, and automatic return to Gemini.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Optional, Callable


@dataclass
class FallbackStats:
    total_fallback_requests: int = 0
    total_fallback_tokens: int = 0
    measured_tokens: int = 0
    estimated_tokens: int = 0
    estimated_cost_usd: float = 0.0
    total_fallback_time_seconds: float = 0.0
    active_fallback_start_time: float = 0.0
    warning_triggered: bool = False
    limit_reached: bool = False


class ProviderCircuitBreaker:
    """Manages Gemini primary state and OpenAI emergency fallback with automatic recovery."""

    def __init__(
        self,
        gemini_timeout: float = 4.0,
        spending_warning: float = 0.10,
        spending_limit: float = 1.00,
        status_cb: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.gemini_timeout = gemini_timeout
        self.spending_warning = spending_warning
        self.spending_limit = spending_limit
        self.status_cb = status_cb or (lambda msg: None)

        self._lock = threading.Lock()
        self.active_provider: str = "gemini"  # "gemini" or "openai_fallback"
        self.gemini_healthy: bool = True
        self.gemini_error_count: int = 0
        self.gemini_cooldown_until: float = 0.0
        self.last_gemini_success: float = time.monotonic()
        self.last_gemini_check: float = 0.0
        self.stats = FallbackStats()

    def report_gemini_success(self) -> None:
        with self._lock:
            self.gemini_healthy = True
            self.gemini_error_count = 0
            self.gemini_cooldown_until = 0.0
            self.last_gemini_success = time.monotonic()
            if self.active_provider != "gemini":
                self.active_provider = "gemini"
                if self.stats.active_fallback_start_time > 0:
                    self.stats.total_fallback_time_seconds += (
                        time.monotonic() - self.stats.active_fallback_start_time
                    )
                    self.stats.active_fallback_start_time = 0.0
                self.status_cb("[GEMINI] Health restored")
                self.status_cb("[PROVIDER] Returned to Gemini primary (free-tier eligible; account billing applies)")
                self.status_cb("[OPENAI] Emergency fallback returned to standby")

    def report_gemini_failure(self, error_str: str, cooldown_seconds: float = 30.0) -> None:
        with self._lock:
            self.gemini_healthy = False
            self.gemini_error_count += 1
            now = time.monotonic()
            self.gemini_cooldown_until = now + max(0.0, cooldown_seconds)

            if self.active_provider == "gemini":
                self.active_provider = "openai_fallback"
                self.stats.active_fallback_start_time = now
                self.status_cb(
                    f"⚠️ [FAILOVER] Gemini unavailable ({error_str[:120]}). "
                    f"Fallback is available only if authorized in settings. Cooldown: {cooldown_seconds:.1f}s."
                )

    def should_probe_gemini(self) -> bool:
        """Lightweight check whether Gemini cooldown has expired and is eligible for auto-return."""
        with self._lock:
            if self.active_provider == "gemini" and self.gemini_healthy:
                return False
            now = time.monotonic()
            probe_spacing = max(10.0, min(60.0, 10.0 * max(1, self.gemini_error_count)))
            if now >= self.gemini_cooldown_until and (self.last_gemini_check == 0.0 or (now - self.last_gemini_check) >= probe_spacing):
                self.last_gemini_check = now
                return True
            return False

    def is_fallback_allowed(self) -> bool:
        with self._lock:
            if self.stats.limit_reached:
                return False
            return True

    def record_fallback_usage(
        self,
        prompt_tokens: int,
        completion_tokens: int,
        model: str = "gpt-4o-mini",
        measured: bool = False,
    ) -> float:
        """Track usage and estimated OpenAI API cost, distinguishing measured API usage from estimates.
        
        Returns the individual call cost in USD.
        """
        with self._lock:
            self.stats.total_fallback_requests += 1
            tokens = prompt_tokens + completion_tokens
            self.stats.total_fallback_tokens += tokens
            if measured:
                self.stats.measured_tokens += tokens
            else:
                self.stats.estimated_tokens += tokens

            # Cost estimation per 1M tokens by model family
            m = model.lower()
            if "gpt-5" in m or "nano" in m:
                cost_rate_input = 0.05 / 1_000_000.0
                cost_rate_output = 0.20 / 1_000_000.0
            elif "gpt-4o-mini" in m:
                cost_rate_input = 0.15 / 1_000_000.0
                cost_rate_output = 0.60 / 1_000_000.0
            elif "gpt-4o" in m:
                cost_rate_input = 2.50 / 1_000_000.0
                cost_rate_output = 10.00 / 1_000_000.0
            else:
                cost_rate_input = 0.15 / 1_000_000.0
                cost_rate_output = 0.60 / 1_000_000.0

            call_cost = (prompt_tokens * cost_rate_input) + (completion_tokens * cost_rate_output)
            self.stats.estimated_cost_usd += call_cost

            # Check warning
            if not self.stats.warning_triggered and self.stats.estimated_cost_usd >= self.spending_warning:
                self.stats.warning_triggered = True
                self.status_cb(
                    f"⚠️ [COST WARNING] OpenAI fallback spending reached ${self.stats.estimated_cost_usd:.3f} "
                    f"(warning threshold: ${self.spending_warning:.2f})."
                )

            # Check hard limit
            if self.stats.estimated_cost_usd >= self.spending_limit:
                self.stats.limit_reached = True
                self.status_cb(
                    f"🛑 [HARD LIMIT] OpenAI fallback spending reached hard limit ${self.spending_limit:.2f}. "
                    f"Stopping further paid fallback calls to protect billing."
                )
            return call_cost

    def get_summary_stats(self) -> dict:
        with self._lock:
            time_spent = self.stats.total_fallback_time_seconds
            if self.active_provider == "openai_fallback" and self.stats.active_fallback_start_time > 0:
                time_spent += (time.monotonic() - self.stats.active_fallback_start_time)
            return {
                "active_provider": self.active_provider,
                "gemini_healthy": self.gemini_healthy,
                "total_requests": self.stats.total_fallback_requests,
                "total_tokens": self.stats.total_fallback_tokens,
                "measured_tokens": self.stats.measured_tokens,
                "estimated_tokens": self.stats.estimated_tokens,
                "estimated_cost_usd": self.stats.estimated_cost_usd,
                "time_spent_seconds": time_spent,
                "limit_reached": self.stats.limit_reached,
                "gemini_cooldown_seconds": max(0.0, self.gemini_cooldown_until - time.monotonic()),
            }
