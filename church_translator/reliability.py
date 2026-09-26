"""Bounded retries for idempotent network requests; never retry playback."""
import os
import random
import re
import time


class GeminiAccessError(RuntimeError):
    """A provider-side access failure requiring account/key intervention."""


class GeminiRetryLater(RuntimeError):
    def __init__(self, delay, pacing=False):
        self.delay = delay
        self.pacing = pacing
        super().__init__(f"Flash-Lite models temporarily unavailable; automatic retry in {delay:.1f}s")


def gemini_retry_delay(error):
    text = str(error).lower()
    # Respect daily exhaustion and absent model access without hammering them.
    if any(term in text for term in ("perday", "per_day", "daily", "limit: 0", "404", "not_found")):
        return 86400.0
    match = re.search(r"retry in\s+([\d.]+)\s*(ms|s)", text)
    if not match:
        match = re.search(r"retrydelay['\"]?\s*:\s*['\"]([\d.]+)(ms|s)", text)
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
