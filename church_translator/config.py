from __future__ import annotations

import os
import json
import sys
import tempfile
import math
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


APP_NAME = "ChurchTranslator"


def project_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def app_data_dir() -> Path:
    base = os.getenv("LOCALAPPDATA")
    if base:
        root = Path(base)
    else:
        root = Path.home() / "AppData" / "Local"
    path = root / APP_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


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
    clean["recognition"] = {key: value for key, value in clean.get("recognition", {}).items() if isinstance(value, str)}
    clean["languages"] = {key: value for key, value in clean.get("languages", {}).items() if isinstance(value, bool)}
    clean["volumes"] = {key: max(0, min(100, value)) for key, value in clean.get("volumes", {}).items() if isinstance(value, int)}
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
    free_tier_mode: bool = True
    smart_sentence_stitching: bool = True


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
        english_voice=os.getenv("TTS_ENGLISH_VOICE", "en-US-Standard-J"),
        russian_voice=os.getenv("TTS_RUSSIAN_VOICE", "ru-RU-Standard-D"),
        free_tier_mode=_env_bool("FREE_TIER_MODE", True),
        smart_sentence_stitching=_env_bool("SMART_SENTENCE_STITCHING", True),
    )

