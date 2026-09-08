from __future__ import annotations

import os
import json
import sys
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


GEMINI_MODELS = {
    "gemini-flash-latest": {
        "name": "gemini-flash-latest",
        "label": "Gemini Flash Latest (Recommended - Auto-Updating)",
        "approx_rpm": 15,
        "target_interval_seconds": 0.8,
    },
    "gemini-flash-lite-latest": {
        "name": "gemini-flash-lite-latest",
        "label": "Gemini Flash Lite Latest (High Speed, 30 RPM)",
        "approx_rpm": 30,
        "target_interval_seconds": 0.5,
    },
    "gemini-3.5-flash-lite": {
        "name": "gemini-3.5-flash-lite",
        "label": "Gemini 3.5 Flash Lite (Fast & Efficient)",
        "approx_rpm": 30,
        "target_interval_seconds": 0.5,
    },
    "gemini-3.5-flash": {
        "name": "gemini-3.5-flash",
        "label": "Gemini 3.5 Flash (Balanced)",
        "approx_rpm": 15,
        "target_interval_seconds": 0.8,
    },
    "gemini-3.6-flash": {
        "name": "gemini-3.6-flash",
        "label": "Gemini 3.6 Flash (Advanced Reasoning)",
        "approx_rpm": 15,
        "target_interval_seconds": 0.8,
    },
}

DEFAULT_GEMINI_MODEL = "gemini-flash-latest"


def load_user_settings() -> dict:
    path = settings_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    recognition = data.get("recognition", {})
    if isinstance(recognition, dict):
        saved_model = recognition.get("gemini_model")
        if saved_model and (saved_model not in GEMINI_MODELS or "1.5" in saved_model or "2.0" in saved_model):
            recognition["gemini_model"] = DEFAULT_GEMINI_MODEL
    return data


def save_user_settings(settings: dict) -> None:
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings, indent=2, sort_keys=True), encoding="utf-8")


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
                "valid": bool(key_type == "service_account" or has_private_key),
                "path": str(path.resolve()),
                "filename": path.name,
            }
    except Exception:
        return None
    return None


def _resolve_path_env(name: str) -> str | None:
    value = os.getenv(name)
    path = None
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


def load_config() -> AppConfig:
    load_dotenv(project_root() / ".env", override=True)
    whisper_model_size = os.getenv("WHISPER_MODEL_SIZE", "large-v3-turbo").strip()
    if whisper_model_size == "base":
        whisper_model_size = "large-v3-turbo"
    raw_gemini_model = os.getenv("GEMINI_MODEL", DEFAULT_GEMINI_MODEL).strip()
    if raw_gemini_model not in GEMINI_MODELS:
        raw_gemini_model = DEFAULT_GEMINI_MODEL
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
        gemini_model=raw_gemini_model if raw_gemini_model else DEFAULT_GEMINI_MODEL,
        translation_provider=provider,
        google_translate_api_key=os.getenv("GOOGLE_TRANSLATE_API_KEY") or None,
        google_application_credentials=_resolve_path_env("GOOGLE_APPLICATION_CREDENTIALS"),
        openai_api_key=os.getenv("OPENAI_API_KEY") or None,
        speech_recognition_backend=os.getenv("SPEECH_RECOGNITION_BACKEND", "openai").strip().lower(),
        openai_transcription_model=os.getenv("OPENAI_TRANSCRIPTION_MODEL", "gpt-4o-mini-transcribe").strip(),
        whisper_model_size=whisper_model_size,
        whisper_beam_size=int(os.getenv("WHISPER_BEAM_SIZE", "3")),
        whisper_device=os.getenv("WHISPER_DEVICE", "auto"),
        whisper_compute_type=os.getenv("WHISPER_COMPUTE_TYPE", "auto"),
        whisper_vad_filter=_env_bool("WHISPER_VAD_FILTER", False),
        whisper_hotwords=os.getenv("WHISPER_HOTWORDS") or None,
        whisper_quality_mode=os.getenv("WHISPER_QUALITY_MODE", "balanced").strip().lower(),
        whisper_min_rms=float(os.getenv("WHISPER_MIN_RMS", "0.0")),
        save_debug_audio=_env_bool("SAVE_DEBUG_AUDIO", False),
        vad_enabled=_env_bool("VAD_ENABLED", True),
        vad_rms_threshold=float(os.getenv("VAD_RMS_THRESHOLD", "0.004")),
        vad_peak_threshold=float(os.getenv("VAD_PEAK_THRESHOLD", "0.025")),
        vad_min_speech_seconds=float(os.getenv("VAD_MIN_SPEECH_SECONDS", "0.20")),
        vad_min_speech_ratio=float(os.getenv("VAD_MIN_SPEECH_RATIO", "0.03")),
        vad_padding_seconds=float(os.getenv("VAD_PADDING_SECONDS", "0.50")),
        max_audio_queue_size=max(1, int(os.getenv("MAX_AUDIO_QUEUE_SIZE", "2"))),
        max_translation_queue_size=max(1, int(os.getenv("MAX_TRANSLATION_QUEUE_SIZE", "6"))),
        max_chunk_age_seconds=float(os.getenv("MAX_CHUNK_AGE_SECONDS", "45")),
        max_tts_age_seconds=float(os.getenv("MAX_TTS_AGE_SECONDS", "60")),
        chunk_seconds=float(os.getenv("CHUNK_SECONDS", "10.0")),
        min_chunk_seconds=float(os.getenv("MIN_CHUNK_SECONDS", "1.2")),
        early_flush_silence_seconds=float(os.getenv("EARLY_FLUSH_SILENCE_SECONDS", "0.70")),
        chunk_overlap_seconds=float(os.getenv("CHUNK_OVERLAP_SECONDS", "0.0")),
        english_voice=os.getenv("TTS_ENGLISH_VOICE", "en-US-Standard-J"),
        russian_voice=os.getenv("TTS_RUSSIAN_VOICE", "ru-RU-Standard-D"),
        free_tier_mode=_env_bool("FREE_TIER_MODE", True),
        smart_sentence_stitching=_env_bool("SMART_SENTENCE_STITCHING", True),
    )

