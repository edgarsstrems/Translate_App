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


def load_user_settings() -> dict:
    path = settings_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


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


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _resolve_path_env(name: str) -> str | None:
    value = os.getenv(name)
    if not value:
        return None
    path = Path(value.strip().strip('"'))
    if not path.is_absolute():
        path = project_root() / path
    if not path.exists() and name == "GOOGLE_APPLICATION_CREDENTIALS":
        credential_files = sorted((project_root() / "credentials").glob("*.json"))
        if len(credential_files) == 1:
            path = credential_files[0]
    resolved = str(path)
    os.environ[name] = resolved
    return resolved


def load_config() -> AppConfig:
    load_dotenv(project_root() / ".env")
    whisper_model_size = os.getenv("WHISPER_MODEL_SIZE", "small").strip()
    if whisper_model_size == "base":
        whisper_model_size = "small"
    return AppConfig(
        gemini_api_key=os.getenv("GEMINI_API_KEY") or None,
        gemini_model=os.getenv("GEMINI_MODEL", "gemini-2.5-flash"),
        translation_provider=os.getenv("TRANSLATION_PROVIDER", "google").strip().lower(),
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
        vad_min_speech_seconds=float(os.getenv("VAD_MIN_SPEECH_SECONDS", "1.0")),
        vad_min_speech_ratio=float(os.getenv("VAD_MIN_SPEECH_RATIO", "0.06")),
        vad_padding_seconds=float(os.getenv("VAD_PADDING_SECONDS", "0.35")),
        max_audio_queue_size=max(1, int(os.getenv("MAX_AUDIO_QUEUE_SIZE", "2"))),
        max_translation_queue_size=max(1, int(os.getenv("MAX_TRANSLATION_QUEUE_SIZE", "6"))),
        max_chunk_age_seconds=float(os.getenv("MAX_CHUNK_AGE_SECONDS", "45")),
        max_tts_age_seconds=float(os.getenv("MAX_TTS_AGE_SECONDS", "60")),
        chunk_seconds=float(os.getenv("CHUNK_SECONDS", "8")),
        min_chunk_seconds=float(os.getenv("MIN_CHUNK_SECONDS", "5")),
        early_flush_silence_seconds=float(os.getenv("EARLY_FLUSH_SILENCE_SECONDS", "0.6")),
        chunk_overlap_seconds=float(os.getenv("CHUNK_OVERLAP_SECONDS", "0.0")),
        english_voice=os.getenv("TTS_ENGLISH_VOICE", "en-US-Standard-J"),
        russian_voice=os.getenv("TTS_RUSSIAN_VOICE", "ru-RU-Standard-D"),
    )
