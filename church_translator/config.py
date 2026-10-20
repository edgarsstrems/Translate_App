from __future__ import annotations

import os
import json
import sys
import tempfile
import math
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv
from .languages import OUTPUT_LANGUAGES


APP_NAME = "ChurchTranslator"


def project_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def app_data_dir() -> Path:
    custom = os.getenv("CHURCH_TRANSLATOR_APPDATA")
    if custom:
        path = Path(custom)
        try:
            path.mkdir(parents=True, exist_ok=True)
            return path
        except Exception:
            pass
    base = os.getenv("LOCALAPPDATA")
    if base:
        root = Path(base)
    else:
        root = Path.home() / "AppData" / "Local"
    path = root / APP_NAME
    try:
        path.mkdir(parents=True, exist_ok=True)
        test_file = path / ".perm_check"
        test_file.touch(exist_ok=True)
        test_file.unlink(missing_ok=True)
        return path
    except (PermissionError, OSError):
        fallback = project_root() / ".appdata"
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


def model_root() -> Path:
    path = app_data_dir() / "models"
    path.mkdir(parents=True, exist_ok=True)
    return path


def settings_path() -> Path:
    return app_data_dir() / "settings.json"


# Stable, text-only Flash-Lite choices with free-tier availability. Actual
# quotas are project-specific; do not advertise invented RPM guarantees.
GEMINI_MODELS = {
    "auto": {"name": "auto", "label": "Automatic Flash-Lite (Recommended — checks availability)"},
    "gemini-3.5-flash-lite": {"name": "gemini-3.5-flash-lite", "label": "Gemini 3.5 Flash-Lite (Recommended — live translation)"},
    "gemini-3.1-flash-lite": {"name": "gemini-3.1-flash-lite", "label": "Gemini 3.1 Flash-Lite (Lightweight alternative)"},
}
DEFAULT_GEMINI_MODEL = "auto"


def live_gemini_model(value):
    return value if value in GEMINI_MODELS else DEFAULT_GEMINI_MODEL


SPEED_MODE_BALANCED = "balanced"
SPEED_MODE_LOW_LATENCY = "low_latency"
SPEED_MODE_DYNAMIC = "dynamic"
SPEED_MODE_CUSTOM = "custom"

SPEED_PROFILES = {
    SPEED_MODE_BALANCED: {
        "mode": SPEED_MODE_BALANCED,
        "label": "Balanced (Current — Recommended)",
        "chunk_seconds": 5.0,
        "min_chunk_seconds": 2.5,
        "early_flush_silence_seconds": 0.45,
        "chunk_overlap_seconds": 0.0,
        "vad_rms_threshold": 0.004,
        "vad_peak_threshold": 0.025,
        "vad_min_speech_seconds": 0.20,
        "max_audio_queue_size": 2,
        "max_translation_queue_size": 6,
        "description": "Current stable baseline. 2.5–5.0s adaptive chunks with 0.45s pause flush. Preserves full context and $0 Gemini free-tier pacing.",
    },
    SPEED_MODE_LOW_LATENCY: {
        "mode": SPEED_MODE_LOW_LATENCY,
        "label": "Low Latency (Experimental)",
        "chunk_seconds": 2.8,
        "min_chunk_seconds": 1.5,
        "early_flush_silence_seconds": 0.30,
        "chunk_overlap_seconds": 0.0,
        "vad_rms_threshold": 0.004,
        "vad_peak_threshold": 0.025,
        "vad_min_speech_seconds": 0.20,
        "max_audio_queue_size": 3,
        "max_translation_queue_size": 8,
        "description": "Faster speech dispatch. 1.5–2.8s chunks with 0.30s natural pause flush. Targets ~5–6s speech-to-audible-English.",
    },
    SPEED_MODE_DYNAMIC: {
        "mode": SPEED_MODE_DYNAMIC,
        "label": "Dynamic (Recommended — Natural Speech)",
        "chunk_seconds": 8.0,
        "min_chunk_seconds": 1.5,
        "early_flush_silence_seconds": 0.60,
        "chunk_overlap_seconds": 0.0,
        "vad_rms_threshold": 0.004,
        "vad_peak_threshold": 0.025,
        "vad_min_speech_seconds": 0.20,
        "max_audio_queue_size": 3,
        "max_translation_queue_size": 8,
        "description": "Natural speech: energy tracking and adaptive 0.64–0.80s phrase pauses, with an 8s maximum. Short pauses stay together; capture continues during processing. No speaker identification.",
    },
    SPEED_MODE_CUSTOM: {
        "mode": SPEED_MODE_CUSTOM,
        "label": "Custom (Advanced)",
        "chunk_seconds": 4.0,
        "min_chunk_seconds": 2.0,
        "early_flush_silence_seconds": 0.40,
        "chunk_overlap_seconds": 0.0,
        "vad_rms_threshold": 0.004,
        "vad_peak_threshold": 0.025,
        "vad_min_speech_seconds": 0.20,
        "max_audio_queue_size": 3,
        "max_translation_queue_size": 6,
        "description": "User-configured chunk durations, pause flush timing, and VAD thresholds.",
    },
}


def get_speed_profile_settings(
    mode: str = SPEED_MODE_BALANCED,
    custom_overrides: dict | None = None,
) -> dict:
    if mode in {SPEED_MODE_LOW_LATENCY, SPEED_MODE_DYNAMIC}:
        prof = dict(SPEED_PROFILES[mode])
        prof["overlap_seconds"] = prof["chunk_overlap_seconds"]
        return prof
    elif mode == SPEED_MODE_CUSTOM and custom_overrides:
        base = dict(SPEED_PROFILES[SPEED_MODE_CUSTOM])
        base["chunk_seconds"] = min(10.0, max(1.5, float(custom_overrides.get("chunk_seconds", 4.0))))
        base["min_chunk_seconds"] = min(base["chunk_seconds"], max(0.8, float(custom_overrides.get("min_chunk_seconds", 2.0))))
        base["early_flush_silence_seconds"] = min(1.2, max(0.20, float(custom_overrides.get("early_flush_silence_seconds", 0.40))))
        overlap = min(0.5, max(0.0, float(custom_overrides.get("overlap_seconds", custom_overrides.get("chunk_overlap_seconds", 0.0)))))
        base["chunk_overlap_seconds"] = overlap
        base["overlap_seconds"] = overlap
        base["vad_rms_threshold"] = min(0.02, max(0.001, float(custom_overrides.get("vad_rms_threshold", 0.004))))
        return base
    prof = dict(SPEED_PROFILES[SPEED_MODE_BALANCED])
    prof["overlap_seconds"] = prof["chunk_overlap_seconds"]
    return prof


def load_user_settings() -> dict:
    path = settings_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        backup = path.with_suffix(".json.bak")
        try:
            data = json.loads(backup.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
    if not isinstance(data, dict):
        return {}
    clean = {key: value for key, value in data.items() if isinstance(value, dict)}
    clean["devices"] = {key: value for key, value in clean.get("devices", {}).items() if isinstance(value, dict)}
    clean["recognition"] = {key: value for key, value in clean.get("recognition", {}).items() if isinstance(value, (str, bool))}
    languages = clean.get("languages", {})
    clean["languages"] = {
        key: value for key, value in languages.items()
        if isinstance(value, bool) or (key == "secondary_language" and isinstance(value, str) and value in OUTPUT_LANGUAGES and value != 'en')
    }
    clean["volumes"] = {key: max(0, min(100, value)) for key, value in clean.get("volumes", {}).items() if isinstance(value, int)}
    voices = {}
    for language, value in clean.get("voices", {}).items():
        if language not in OUTPUT_LANGUAGES or not isinstance(value, dict):
            continue
        locale = OUTPUT_LANGUAGES[language][1]
        if not isinstance(value.get("voice"), str) or not value["voice"].startswith(locale + "-"):
            continue
        voices[language] = {"voice": value["voice"]}
        for name, low, high, default in (("rate", .5, 2, 1.08), ("pitch", -10, 10, 0), ("gain", -12, 6, 0)):
            try:
                number = float(value.get(name, default))
                voices[language][name] = min(high, max(low, number)) if math.isfinite(number) else default
            except (ValueError, TypeError):
                voices[language][name] = default
    clean["voices"] = voices
    if "speed" in clean and isinstance(clean["speed"], dict):
        speed = clean["speed"]
        speed_mode = speed.get("mode", SPEED_MODE_BALANCED)
        if speed_mode not in SPEED_PROFILES:
            speed_mode = SPEED_MODE_BALANCED
        clean["speed"] = {
            "mode": speed_mode,
            "auto_return": bool(speed.get("auto_return", True)),
            "custom": {
                "chunk_seconds": min(8.0, max(1.5, float(speed.get("custom", {}).get("chunk_seconds", 4.0)))),
                "min_chunk_seconds": min(4.0, max(0.8, float(speed.get("custom", {}).get("min_chunk_seconds", 2.0)))),
                "early_flush_silence_seconds": min(1.0, max(0.20, float(speed.get("custom", {}).get("early_flush_silence_seconds", 0.40)))),
                "overlap_seconds": min(0.5, max(0.0, float(speed.get("custom", {}).get("overlap_seconds", 0.0)))),
                "vad_rms_threshold": min(0.02, max(0.001, float(speed.get("custom", {}).get("vad_rms_threshold", 0.004)))),
            } if isinstance(speed.get("custom"), dict) else {},
        }
    return {key: value for key, value in clean.items() if value}


def save_user_settings(settings: dict) -> None:
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        try:
            old = path.read_text(encoding="utf-8")
            if isinstance(json.loads(old), dict):
                atomic_write_text(path.with_suffix(".json.bak"), old)
        except (OSError, ValueError):
            pass
    atomic_write_text(path, json.dumps(settings, indent=2, sort_keys=True))


def atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


@dataclass(frozen=True)
class AppConfig:
    gemini_api_key: str | None
    gemini_model: str
    translation_provider: str
    google_translate_api_key: str | None
    google_application_credentials: str | None
    openai_api_key: str | None
    speech_recognition_backend: str
    openai_transcription_model: str
    whisper_model_size: str
    whisper_beam_size: int
    whisper_device: str
    whisper_compute_type: str
    whisper_vad_filter: bool
    whisper_hotwords: str | None
    whisper_quality_mode: str
    whisper_min_rms: float
    save_debug_audio: bool
    vad_enabled: bool
    vad_rms_threshold: float
    vad_peak_threshold: float
    vad_min_speech_seconds: float
    vad_min_speech_ratio: float
    vad_padding_seconds: float
    max_audio_queue_size: int
    max_translation_queue_size: int
    max_chunk_age_seconds: float
    max_tts_age_seconds: float
    chunk_seconds: float
    min_chunk_seconds: float
    early_flush_silence_seconds: float
    chunk_overlap_seconds: float
    english_voice: str
    russian_voice: str
    lithuanian_voice: str
    free_tier_mode: bool = True
    smart_sentence_stitching: bool = True
    openai_fallback_enabled: bool = False
    openai_fallback_model: str = "gpt-4o-mini"
    auto_return_to_gemini: bool = True
    gemini_timeout_seconds: float = 4.0
    fallback_spending_warning: float = 0.10
    fallback_spending_limit: float = 1.00
    speed_mode: str = "balanced"
    low_latency_auto_return: bool = True
    custom_chunk_seconds: float = 4.0
    custom_min_chunk_seconds: float = 2.0
    custom_early_flush_silence_seconds: float = 0.40
    custom_overlap_seconds: float = 0.0
    custom_vad_rms_threshold: float = 0.004
    voice_settings: dict = field(default_factory=dict)


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def inspect_service_account_file(file_path: Path | str | None) -> dict | None:
    if not file_path:
        return None
    path = Path(file_path)
    if not path.exists() or not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            project_id = data.get("project_id", "")
            client_email = data.get("client_email", "")
            key_type = data.get("type", "service_account")
            has_private_key = bool(data.get("private_key"))
            return {
                "project_id": project_id,
                "client_email": client_email,
                "type": key_type,
                "valid": bool(key_type == "service_account" and has_private_key and client_email and project_id),
                "path": str(path.resolve()),
                "filename": path.name,
            }
    except Exception:
        return None
    return None


def _resolve_path_env(name: str) -> str | None:
    value = os.getenv(name)
    path = None
    if value == "disabled":
        return None
    if value:
        path = Path(value.strip().strip('"'))
        if not path.is_absolute():
            path = project_root() / path
    if (not path or not path.exists()) and name == "GOOGLE_APPLICATION_CREDENTIALS":
        credential_files = sorted((project_root() / "credentials").glob("*.json"))
        if credential_files:
            path = credential_files[0]
    if path and path.exists():
        resolved = str(path.resolve())
        os.environ[name] = resolved
        return resolved
    if name in os.environ and not value:
        os.environ.pop(name, None)
    return None


def _env_number(name, default, low=0.0, high=3600.0):
    try:
        value = float(os.getenv(name, str(default)))
        return min(high, max(low, value)) if math.isfinite(value) else float(default)
    except (TypeError, ValueError):
        return float(default)


def load_config() -> AppConfig:
    load_dotenv(project_root() / ".env", override=True)
    whisper_model_size = os.getenv("WHISPER_MODEL_SIZE", "large-v3-turbo").strip()
    if whisper_model_size == "base":
        whisper_model_size = "large-v3-turbo"
    raw_gemini_model = os.getenv("GEMINI_MODEL", DEFAULT_GEMINI_MODEL).strip()
    gemini_key = os.getenv("GEMINI_API_KEY") or None
    raw_provider = os.getenv("TRANSLATION_PROVIDER")
    if raw_provider and raw_provider.strip():
        provider = raw_provider.strip().lower()
    elif gemini_key:
        provider = "gemini"
    else:
        provider = "auto"
    return AppConfig(
        gemini_api_key=gemini_key,
        gemini_model=live_gemini_model(raw_gemini_model),
        translation_provider=provider,
        google_translate_api_key=os.getenv("GOOGLE_TRANSLATE_API_KEY") or None,
        google_application_credentials=_resolve_path_env("GOOGLE_APPLICATION_CREDENTIALS"),
        openai_api_key=os.getenv("OPENAI_API_KEY") or None,
        speech_recognition_backend=os.getenv("SPEECH_RECOGNITION_BACKEND", "openai").strip().lower(),
        openai_transcription_model=os.getenv("OPENAI_TRANSCRIPTION_MODEL", "gpt-4o-transcribe").strip(),
        whisper_model_size=whisper_model_size,
        whisper_beam_size=int(_env_number("WHISPER_BEAM_SIZE", 3, 1)),
        whisper_device=os.getenv("WHISPER_DEVICE", "auto"),
        whisper_compute_type=os.getenv("WHISPER_COMPUTE_TYPE", "auto"),
        whisper_vad_filter=_env_bool("WHISPER_VAD_FILTER", False),
        whisper_hotwords=os.getenv("WHISPER_HOTWORDS") or None,
        whisper_quality_mode=os.getenv("WHISPER_QUALITY_MODE", "balanced").strip().lower(),
        whisper_min_rms=_env_number("WHISPER_MIN_RMS", 0.0),
        save_debug_audio=_env_bool("SAVE_DEBUG_AUDIO", False),
        vad_enabled=_env_bool("VAD_ENABLED", True),
        vad_rms_threshold=_env_number("VAD_RMS_THRESHOLD", 0.004),
        vad_peak_threshold=_env_number("VAD_PEAK_THRESHOLD", 0.025),
        vad_min_speech_seconds=_env_number("VAD_MIN_SPEECH_SECONDS", 0.20),
        vad_min_speech_ratio=_env_number("VAD_MIN_SPEECH_RATIO", 0.03),
        vad_padding_seconds=_env_number("VAD_PADDING_SECONDS", 0.50),
        max_audio_queue_size=max(1, int(_env_number("MAX_AUDIO_QUEUE_SIZE", 2, 1))),
        max_translation_queue_size=max(1, int(_env_number("MAX_TRANSLATION_QUEUE_SIZE", 6, 1))),
        max_chunk_age_seconds=_env_number("MAX_CHUNK_AGE_SECONDS", 45),
        max_tts_age_seconds=_env_number("MAX_TTS_AGE_SECONDS", 60),
        chunk_seconds=min(10.0, max(2.0, _env_number("CHUNK_SECONDS", 5.0))),
        min_chunk_seconds=_env_number("MIN_CHUNK_SECONDS", 2.5),
        early_flush_silence_seconds=_env_number("EARLY_FLUSH_SILENCE_SECONDS", 0.45),
        chunk_overlap_seconds=0.0,
        english_voice=os.getenv("TTS_ENGLISH_VOICE", "en-US-Chirp3-HD-Iapetus"),
        russian_voice=os.getenv("TTS_RUSSIAN_VOICE", "ru-RU-Standard-D"),
        lithuanian_voice=os.getenv("TTS_LITHUANIAN_VOICE", "lt-LT-Standard-B"),
        voice_settings=load_user_settings().get("voices", {}),
        free_tier_mode=_env_bool("FREE_TIER_MODE", True),
        smart_sentence_stitching=_env_bool("SMART_SENTENCE_STITCHING", True),
        openai_fallback_enabled=_env_bool("OPENAI_FALLBACK_ENABLED", False),
        openai_fallback_model=os.getenv("OPENAI_FALLBACK_MODEL", "gpt-4o-mini").strip(),
        auto_return_to_gemini=_env_bool("AUTO_RETURN_TO_GEMINI", True),
        gemini_timeout_seconds=_env_number("GEMINI_TIMEOUT_SECONDS", 4.0, low=1.0, high=30.0),
        fallback_spending_warning=_env_number("FALLBACK_SPENDING_WARNING", 0.10, low=0.01, high=100.0),
        fallback_spending_limit=_env_number("FALLBACK_SPENDING_LIMIT", 1.00, low=0.05, high=1000.0),
        speed_mode=os.getenv("TRANSLATION_SPEED_MODE", SPEED_MODE_BALANCED).strip().lower()
        if os.getenv("TRANSLATION_SPEED_MODE", SPEED_MODE_BALANCED).strip().lower() in SPEED_PROFILES
        else SPEED_MODE_BALANCED,
        low_latency_auto_return=_env_bool("LOW_LATENCY_AUTO_RETURN", True),
        custom_chunk_seconds=_env_number("CUSTOM_CHUNK_SECONDS", 4.0, low=1.5, high=10.0),
        custom_min_chunk_seconds=_env_number("CUSTOM_MIN_CHUNK_SECONDS", 2.0, low=0.8, high=5.0),
        custom_early_flush_silence_seconds=_env_number("CUSTOM_EARLY_FLUSH_SILENCE_SECONDS", 0.40, low=0.20, high=1.5),
        custom_overlap_seconds=_env_number("CUSTOM_OVERLAP_SECONDS", 0.0, low=0.0, high=0.5),
        custom_vad_rms_threshold=_env_number("CUSTOM_VAD_RMS_THRESHOLD", 0.004, low=0.001, high=0.05),
    )
