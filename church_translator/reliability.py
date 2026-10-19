"""Bounded retries for idempotent network requests; never retry playback."""
import os
import random
import re
import time
import threading
import hashlib

_gemini_gate_lock = threading.Lock()
_gemini_slots = {}


def pace_gemini_request(api_key, check_cancelled=None):
    """Reserve one shared request slot across translation, recovery and summary.

    Model discovery is not a generation request. The conservative spacing is
    local policy, not a claim about the account's actual provider quota.
    """
    identity = hashlib.sha256(api_key.encode()).hexdigest()
    while True:
        if check_cancelled:
            check_cancelled()
        with _gemini_gate_lock:
            now = time.monotonic()
            remaining = _gemini_slots.get(identity, 0.0) - now
            if remaining <= 0:
                _gemini_slots[identity] = now + 4.2
                return
        time.sleep(min(.1, remaining))


class GeminiAccessError(RuntimeError):
    """A provider-side access failure requiring account/key intervention."""


class GeminiRetryLater(RuntimeError):
    def __init__(self, delay, pacing=False):
        self.delay = delay
        self.pacing = pacing
        if pacing:
            super().__init__(f"Gemini free-tier request pacing spacing; next slot in {delay:.1f}s")
        else:
            super().__init__(f"Flash-Lite models temporarily unavailable; automatic retry in {delay:.1f}s")


def extract_gemini_retry_delay_info(error) -> tuple[float | None, str]:
    """Extract explicit retry delay and quota dimension if provided in Gemini error.

    Returns (retry_delay_seconds or None, quota_dimension: 'RPM' | 'RPD' | 'TPM' | 'UNKNOWN').
    """
    text = str(error).lower()

    # Quota dimension detection
    if any(term in text for term in ("perday", "per_day", "daily", "limit: 0", "requests_per_day", "generate_content_requests_per_day", "free_tier_quota_exceeded")):
        dimension = "RPD"
    elif any(term in text for term in ("perminute", "per_minute", "minute", "requests_per_minute", "15 rpm", "rpm")):
        dimension = "RPM"
    elif any(term in text for term in ("token", "tokens_per_minute", "tpm")):
        dimension = "TPM"
    elif "429" in text or "resource_exhausted" in text:
        dimension = "RPM"  # Standard default assumption for free-tier 429 when not daily
    else:
        dimension = "UNKNOWN"

    match = re.search(r"retry in\s+([\d.]+)\s*(ms|s)", text)
    if not match:
        match = re.search(r"retrydelay['\"]?\s*:\s*['\"]([\d.]+)(ms|s)", text)
    if not match:
        match = re.search(r"retry-after['\"]?\s*:\s*['\"]?(\d+)", text)
        if match:
            return float(match[1]), dimension
    if match:
        val = float(match[1]) / (1000 if match[2] == "ms" else 1)
        return round(val, 1), dimension

    return None, dimension


def gemini_retry_delay(error):
    text = str(error).lower()
    # Respect daily exhaustion and absent model access without hammering them.
    if any(term in text for term in ("perday", "per_day", "daily", "limit: 0", "404", "not_found")):
        return 86400.0
    match = re.search(r"retry in\s+([\d.]+)\s*(ms|s)", text)
    if not match:
        match = re.search(r"retrydelay['\"]?\s*:\s*['\"]([\d.]+)(ms|s)", text)
    if not match:
        match = re.search(r"retry-after['\"]?\s*:\s*['\"]?(\d+)", text)
        if match:
            return max(1.0, float(match[1]) + 0.25)
    if match:
        return max(1.0, float(match[1]) / (1000 if match[2] == "ms" else 1) + .25)
    if "429" in text or "quota" in text or "resource_exhausted" in text:
        return 60.0
    if "400" in text or "not supported" in text:
        return 86400.0
    return 5.0



def gemini_access_error(error):
    message = str(error).lower()
    if "project has been denied access" in message:
        return GeminiAccessError(
            "Google has denied Gemini access to the project associated with this API key. "
            "Changing models will not fix a project denial. Open Google AI Studio > API Keys, "
            "check the key's project, and contact Google support to resolve the restriction. "
            "After access is restored, use Test API Key, then Start."
        )
    if any(value in message for value in ("401", "403", "permission_denied", "api_key_invalid", "api key not valid", "reported as leaked")):
        return GeminiAccessError(
            "Google rejected Gemini access for this API key. Check its status, project and "
            "Gemini API permissions in Google AI Studio. If Google marks the key as blocked "
            "or leaked, replace it there. Use Test API Key before starting. "
            f"Google response: {safe_error(error)}"
        )
    return None


def safe_error(error):
    text = str(error)
    for name in ("OPENAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_TRANSLATE_API_KEY"):
        secret = os.getenv(name)
        if secret:
            text = text.replace(secret, "[redacted]")
    text = re.sub(r"(?i)(key|api_key|token)=([^&\s]+)", r"\1=[redacted]", text)
    text = re.sub(r"\b(?:sk-|AIza)[A-Za-z0-9_-]+", "[redacted]", text)
    return text[:1000]


def transient(error):
    code = getattr(error, "status_code", None) or getattr(error, "code", None)
    if callable(code):
        code = code()
    text = f"{code} {type(error).__name__} {error}".lower()
    if any(x in text for x in ("401", "403", "unauthenticated", "permissiondenied", "invalidargument", "insufficient_quota")):
        return False
    return any(x in text for x in ("429", "500", "502", "503", "504", "timeout", "connection", "unavailable", "resourceexhausted", "deadlineexceeded"))


def retry_call(operation, status=lambda message: None, attempts=3):
    for attempt in range(attempts):
        try:
            return operation()
        except Exception as exc:
            if attempt + 1 == attempts or not transient(exc):
                raise
            delay = min(4.0, 0.5 * 2 ** attempt) + random.uniform(0, 0.25)
            status(f"Temporary API failure; retry {attempt + 1}/{attempts - 1} in {delay:.1f}s.")
            time.sleep(delay)


def classify_gemini_error(error) -> tuple[str, str, float]:
    """Classify a Gemini error into (category, safe_description, cooldown_seconds).

    Distinguishes:
    - HTTP 429 rate limit
    - Daily quota exhaustion
    - Temporary server error (5xx)
    - Timeout
    - Authentication error
    - Unsupported model
    - Network error
    - Other API errors
    """
    text = str(error).lower()

    # 1. Access / Key denied
    access = gemini_access_error(error)
    if access:
        return "AUTHENTICATION_ERROR", "Authentication error / invalid API key", 86400.0
    if any(k in text for k in ("401", "403", "permission_denied", "api_key_invalid", "api key not valid")):
        return "AUTHENTICATION_ERROR", "Invalid API key or access denied", 86400.0

    # 2. Daily exhaustion
    if any(term in text for term in ("perday", "per_day", "daily", "limit: 0", "generate_content_requests_per_day", "free_tier_quota_exceeded")):
        return "DAILY_QUOTA_EXHAUSTED", "Daily quota limit reached for today", 86400.0

    # 3. Model unsupported / 404
    if any(term in text for term in ("404", "not_found", "is not supported", "not supported for api")):
        return "UNSUPPORTED_MODEL", "Selected Gemini model not found or unsupported", 86400.0

    # 4. HTTP 429 rate limit (per minute RPM)
    if "429" in text or "quota" in text or "resource_exhausted" in text:
        delay = gemini_retry_delay(error)
        cooldown = min(60.0, max(15.0, delay))
        return "RATE_LIMITED_429", f"HTTP 429 — rate limit (cooldown {cooldown:.0f}s)", cooldown

    # 5. Timeout
    if "timeout" in text or "timed out" in text or "deadline_exceeded" in text:
        return "TIMEOUT", "Request timed out", 10.0

    # 6. Server error (5xx)
    if any(term in text for term in ("500", "502", "503", "504", "internal server error", "unavailable")):
        return "SERVER_ERROR", "Temporary Google server error (HTTP 5xx)", 5.0

    # 7. Network / connection error
    if any(term in text for term in ("connection", "network", "name resolution", "getaddrinfo", "socket")):
        return "NETWORK_ERROR", "Network connection error", 5.0

    # 8. General API error
    return "API_ERROR", f"API error: {safe_error(error)[:80]}", 15.0
