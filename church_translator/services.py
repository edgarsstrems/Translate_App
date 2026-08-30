from __future__ import annotations

import threading
import time
import ctypes
import io
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import soundfile as sf
from google.cloud import texttospeech, translate_v2 as translate

from .config import AppConfig, model_root
from .glossary import Glossary


LANGUAGE_NAMES = {
    "en": "English",
    "ru": "Russian",
}


@dataclass(frozen=True)
class TtsVoice:
    language_code: str
    voice_name: str
    speaking_rate: float = 1.0


@dataclass(frozen=True)
class TranscriptionResult:
    text: str
    uncertain: bool
    confidence_note: str | None = None


def create_transcriber(config: AppConfig, glossary: Glossary, status_cb):
    if config.speech_recognition_backend == "openai":
        return OpenAITranscriber(config, glossary, status_cb)
    return LocalWhisperTranscriber(config, glossary, status_cb)


class LocalWhisperTranscriber:
    def __init__(self, config: AppConfig, glossary: Glossary, status_cb) -> None:
        self.config = config
        self.glossary = glossary
        self.status_cb = status_cb
        self._model = None
        self._lock = threading.Lock()
        self._hotwords = self._build_hotwords()
        self._hint_terms = {self._normalize_term(term) for term in self._source_terms()}
        self._device = "cpu"
        self._compute_type = "int8"
        self._previous_text = ""

    def ensure_model(self) -> None:
        with self._lock:
            if self._model is not None:
                return
            root = model_root()
            if self.config.whisper_model_size.startswith("large"):
                self.status_cb(
                    f"Preparing large Whisper model '{self.config.whisper_model_size}'. First run can take several minutes..."
                )
            self.status_cb(f"Preparing Whisper model '{self.config.whisper_model_size}' in {root}...")
            model_size_or_path = self._download_model_path(root)
            self._device, self._compute_type = self._resolve_runtime()
            from faster_whisper import WhisperModel

            self.status_cb(
                f"Loading Whisper model '{self.config.whisper_model_size}' on "
                f"{self._device}/{self._compute_type}..."
            )
            try:
                self._model = WhisperModel(
                    str(model_size_or_path),
                    device=self._device,
                    compute_type=self._compute_type,
                    download_root=str(root),
                )
            except Exception:
                if self._device == "cuda":
                    self.status_cb("CUDA Whisper load failed; falling back to CPU/int8.")
                    self._device = "cpu"
                    self._compute_type = "int8"
                    self._model = WhisperModel(
                        str(model_size_or_path),
                        device=self._device,
                        compute_type=self._compute_type,
                        download_root=str(root),
                    )
                else:
                    raise
            self.status_cb(
                f"Whisper model ready: {self.config.whisper_model_size}, "
                f"{self._device}/{self._compute_type}."
            )
            self.status_cb(
                f"Recognition mode: {self.config.whisper_quality_mode}, "
                f"beam {self._effective_beam_size()}."
            )

    def _resolve_runtime(self) -> tuple[str, str]:
        configured_device = (self.config.whisper_device or "auto").strip().lower()
        configured_compute = (self.config.whisper_compute_type or "auto").strip().lower()
        try:
            import ctranslate2

            cuda_available = ctranslate2.get_cuda_device_count() > 0
        except Exception:
            cuda_available = False
        if cuda_available and not self._cuda_runtime_loadable():
            self.status_cb("CUDA device found, but CUDA 12 runtime DLLs are not loadable; using CPU/int8.")
            cuda_available = False

        if configured_device == "auto":
            device = "cuda" if cuda_available else "cpu"
        elif configured_device == "cuda" and not cuda_available:
            self.status_cb("CUDA was requested but is not usable; using CPU/int8.")
            device = "cpu"
        else:
            device = configured_device

        if configured_compute == "auto":
            compute_type = "float16" if device == "cuda" else "int8"
        else:
            compute_type = configured_compute
        return device, compute_type

    def _cuda_runtime_loadable(self) -> bool:
        if sys.platform != "win32":
            return True
        search_dirs = []
        cuda_path = os.getenv("CUDA_PATH")
        if cuda_path:
            search_dirs.append(str(Path(cuda_path) / "bin"))
        search_dirs.extend(path for path in os.getenv("PATH", "").split(os.pathsep) if path)

        for directory in dict.fromkeys(search_dirs):
            dll = Path(directory) / "cublas64_12.dll"
            if dll.exists():
                try:
                    with os.add_dll_directory(directory):
                        ctypes.WinDLL(str(dll))
                    return True
                except OSError:
                    continue
        try:
            ctypes.WinDLL("cublas64_12.dll")
            return True
        except OSError:
            return False

    def _download_model_path(self, root: Path) -> str:
        from huggingface_hub import snapshot_download
        from tqdm.auto import tqdm

        model = self.config.whisper_model_size
        if Path(model).exists():
            return model
        repo_id = model if "/" in model else f"Systran/faster-whisper-{model}"
        if model == "large-v3-turbo":
            repo_id = "deepdml/faster-whisper-large-v3-turbo-ct2"
        self.status_cb(f"Checking Whisper model files from {repo_id}...")
        status_cb = self.status_cb
        model_name = model
        cache_root = root
        download_done = threading.Event()

        class StatusTqdm(tqdm):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self._last_status = 0.0
                self._stop_status = threading.Event()
                self._status_thread = threading.Thread(
                    target=self._report_until_closed,
                    name="whisper-download-progress",
                    daemon=True,
                )
                self._status_thread.start()

            def update(self, n=1):
                result = super().update(n)
                now = time.monotonic()
                if now - self._last_status >= 1.0 or (self.total and self.n >= self.total):
                    self._last_status = now
                    status_cb(self._status_message(model_name))
                return result

            def close(self):
                self._stop_status.set()
                super().close()

            def _report_until_closed(self) -> None:
                while not download_done.is_set() and not self._stop_status.wait(2.0):
                    status_cb(self._status_message(model_name))

            def _status_message(self, model_name: str) -> str:
                description = (self.desc or "Downloading").strip()
                cache_size = self._cache_size_text()
                if self.total:
                    percent = min(100.0, (self.n / self.total) * 100.0)
                    return (
                        f"{description} Whisper {model_name}: "
                        f"{percent:.0f}% ({self.format_sizeof(self.n)}/{self.format_sizeof(self.total)}), "
                        f"cache {cache_size}"
                    )
                return f"{description} Whisper {model_name}: {self.format_sizeof(self.n)}, cache {cache_size}"

            def _cache_size_text(self) -> str:
                try:
                    total_bytes = sum(
                        path.stat().st_size
                        for path in cache_root.rglob("*")
                        if path.is_file()
                    )
                except OSError:
                    return "checking..."
                return self.format_sizeof(total_bytes)

        try:
            path = snapshot_download(
                repo_id=repo_id,
                cache_dir=str(root),
                tqdm_class=StatusTqdm,
            )
            status_cb(f"Download check complete for Whisper {model_name}; cache {self._cache_size_text(root)}.")
            return path
        finally:
            download_done.set()

    def _cache_size_text(self, root: Path) -> str:
        from tqdm.auto import tqdm

        try:
            total_bytes = sum(
                path.stat().st_size
                for path in root.rglob("*")
                if path.is_file()
            )
        except OSError:
            return "checking..."
        return tqdm.format_sizeof(total_bytes)

    def transcribe(self, audio_float32, leading_context_seconds: float = 0.0) -> TranscriptionResult:
        self.ensure_model()
        assert self._model is not None
        start = time.monotonic()
        try:
            collected = self._collect_segments(audio_float32)
        except Exception as exc:
            if self._device == "cuda":
                self.status_cb(f"CUDA transcription failed ({exc}); falling back to CPU/int8.")
                self._fallback_to_cpu()
                start = time.monotonic()
                collected = self._collect_segments(audio_float32)
            else:
                raise
        usable_segments = self._trim_context_segments(collected, leading_context_seconds)
        text = self._segments_to_text(usable_segments, leading_context_seconds)
        text = self._filter_prompt_hallucinations(text)
        text = self._dedupe_against_previous(text)
        text = self._collapse_repetitive_tail(text)
        text, english_note = self._repair_english_intrusions(text)
        if self._looks_like_hint_hallucination(text):
            return TranscriptionResult(text="", uncertain=True, confidence_note="hint hallucination")
        low_score_segments = [
            segment for segment in usable_segments if getattr(segment, "avg_logprob", 0.0) < -1.0
        ]
        uncertain = bool(text and low_score_segments)
        if english_note:
            uncertain = True
        note = english_note or ("low Whisper confidence" if uncertain else None)
        if text:
            self._remember_text(text)
        elapsed = time.monotonic() - start
        audio_seconds = max(0.001, len(audio_float32) / 16_000)
        speed = audio_seconds / max(0.001, elapsed)
        self.status_cb(f"Whisper processed {audio_seconds:.1f}s in {elapsed:.1f}s ({speed:.1f}x real time).")
        return TranscriptionResult(text=text, uncertain=uncertain, confidence_note=note)

    def _collect_segments(self, audio_float32) -> list:
        assert self._model is not None
        beam_size = self._effective_beam_size()
        patience = 1.2 if beam_size > 1 and self.config.whisper_quality_mode == "accuracy" else 1.0
        segments, _info = self._model.transcribe(
            audio_float32,
            language="lv",
            beam_size=beam_size,
            best_of=beam_size,
            patience=patience,
            temperature=0.0,
            vad_filter=self.config.whisper_vad_filter,
            condition_on_previous_text=False,
            hotwords=self._hotwords,
            no_speech_threshold=0.9,
            log_prob_threshold=-1.2,
            compression_ratio_threshold=2.8,
            word_timestamps=False,
        )
        return list(segments)

    def _effective_beam_size(self) -> int:
        configured = max(1, self.config.whisper_beam_size)
        mode = self.config.whisper_quality_mode
        model = self.config.whisper_model_size
        if mode == "live":
            target = 1
        elif mode == "accuracy":
            target = 3 if model == "small" else 2
        elif model == "small":
            target = 2
        else:
            target = 1
        if self._device == "cuda" and mode == "balanced":
            target = max(target, 2)
        return min(configured, target)

    def _fallback_to_cpu(self) -> None:
        with self._lock:
            from faster_whisper import WhisperModel

            root = model_root()
            model_size_or_path = self._download_model_path(root)
            self._device = "cpu"
            self._compute_type = "int8"
            self._model = WhisperModel(
                str(model_size_or_path),
                device=self._device,
                compute_type=self._compute_type,
                download_root=str(root),
            )
        self.status_cb(
            f"Whisper model ready: {self.config.whisper_model_size}, "
            f"{self._device}/{self._compute_type}."
        )
        self.status_cb(
            f"Recognition mode: {self.config.whisper_quality_mode}, "
            f"beam {self._effective_beam_size()}."
        )

    def _trim_context_segments(self, segments, leading_context_seconds: float):
        return [
            segment
            for segment in segments
            if leading_context_seconds <= 0.0
            or getattr(segment, "end", leading_context_seconds) > leading_context_seconds + 0.05
        ]

    def _segments_to_text(self, segments, leading_context_seconds: float) -> str:
        parts = []
        for segment in segments:
            words = getattr(segment, "words", None) or []
            if words and leading_context_seconds > 0.0:
                word_text = "".join(
                    word.word
                    for word in words
                    if getattr(word, "end", leading_context_seconds) > leading_context_seconds + 0.05
                ).strip()
                if word_text:
                    parts.append(word_text)
            else:
                segment_text = segment.text.strip()
                if segment_text:
                    parts.append(segment_text)
        return " ".join(parts).strip()

    def _filter_prompt_hallucinations(self, text: str) -> str:
        if not text:
            return ""
        hallucinated_patterns = [
            r"^\s*Kristīgs\s+dievkalpojums[,\s]+sprediķis[,\s]+Dievs[,\s]+Jēzus\s+Kristus[,\s]+Svētais\s+Gars[,\s]+Bībele[,\s]+lūgšana[,\s]+ticība[,\s]+draudze\.?\s*",
            r"\bKristīgs\s+dievkalpojums[,\s]+sprediķis[,\s]+Dievs[,\s]+Jēzus\s+Kristus[,\s]+Svētais\s+Gars[,\s]+Bībele[,\s]+lūgšana[,\s]+ticība[,\s]+draudze\.?\b",
            r"\b(?:Tas|Tā)\s+ir\s+kristīgs\s+dievkalpojuma\s+sprediķis\b.*",
            r"\bTranskribējiet\s+precīzi\b.*",
            r"\bPēdējais\s+teksts:?\b.*",
            r"\bTas\s+Kungs\s+to\s+ir\s+radījis\s+un\s+veidojis\b.*",
            r"\bViss\s+mūsu\s+personīgajās\s+attiecībās\s+sākās\b.*",
            r"\bMēs\s+augam\s+šajās\s+attiecībās\b.*",
            r"\bSvētapziņa\s+tā\s+būtu\s+atsevišķa\s+plaša\s+tēma\b.*",
            r"\bpar\s+Dievu,\s+Jēzu\s+Kristu,\s+Svēto\s+Garu\b.*",
        ]
        cleaned = text
        for pat in hallucinated_patterns:
            cleaned = re.sub(pat, "", cleaned, flags=re.IGNORECASE).strip()
        return cleaned

    def _build_hotwords(self) -> str | None:
        configured = (self.config.whisper_hotwords or "").strip()
        return configured or None

    def _source_terms(self) -> list[str]:
        defaults = [
            "Jēzus",
            "Kungs",
            "Dievs",
            "Dieva vārds",
            "Svētais Gars",
            "ticība",
            "ticības vīri",
            "brāļi",
            "māsas",
            "draudze",
            "dievkalpojums",
            "sprediķis",
        ]
        terms = [
            *defaults,
            *self.glossary.source_replacements.keys(),
            *self.glossary.source_replacements.values(),
            *self.glossary.translation_terms.keys(),
        ]
        return [term for term in dict.fromkeys(terms) if term]

    def _is_too_quiet(self, audio_float32, leading_context_seconds: float) -> bool:
        audio = np.asarray(audio_float32, dtype=np.float32)
        if leading_context_seconds > 0.0:
            start = min(audio.size, int(16_000 * leading_context_seconds))
            audio = audio[start:]
        if audio.size == 0:
            return True
        rms = float(np.sqrt(np.mean(np.square(audio))))
        peak = float(np.max(np.abs(audio)))
        return rms < self.config.whisper_min_rms and peak < self.config.whisper_min_rms * 8

    def _dedupe_against_previous(self, text: str) -> str:
        text = " ".join(text.split()).strip()
        if not text or not self._previous_text:
            return text
        previous_words = self._previous_text.split()
        current_words = text.split()
        previous_folded = [word.casefold().strip(" .,!?;:") for word in previous_words]
        current_folded = [word.casefold().strip(" .,!?;:") for word in current_words]
        max_overlap = min(len(previous_words), len(current_words), 40)
        for size in range(max_overlap, 1, -1):
            if previous_folded[-size:] == current_folded[:size]:
                return " ".join(current_words[size:]).strip()
        return text

    def _collapse_repetitive_tail(self, text: str) -> str:
        words = text.split()
        if len(words) < 12:
            return text
        folded = [word.casefold().strip(" .,!?;:") for word in words]
        for phrase_len in range(6, 0, -1):
            if len(words) < phrase_len * 3:
                continue
            phrase = folded[-phrase_len:]
            repeats = 1
            cursor = len(words) - phrase_len * 2
            while cursor >= 0 and folded[cursor : cursor + phrase_len] == phrase:
                repeats += 1
                cursor -= phrase_len
            if repeats >= 3:
                keep_until = len(words) - (repeats - 1) * phrase_len
                return " ".join(words[:keep_until]).strip()
        return text

    def _repair_english_intrusions(self, text: str) -> tuple[str, str | None]:
        if not text:
            return text, None
        # Filter common Whisper silence/subtitle hallucinations
        hallucinations = [
            r"\b(?:subtitles|captions)\s+by\b.*",
            r"\bamara\.org\b.*",
            r"\bthank\s+you\s+for\s+watching\b.*",
            r"\bplease\s+subscribe\b.*",
            r"\blike\s+and\s+subscribe\b.*",
        ]
        repaired = text
        for pat in hallucinations:
            if re.search(pat, repaired, flags=re.IGNORECASE):
                repaired = re.sub(pat, "", repaired, flags=re.IGNORECASE).strip()
                if not repaired:
                    return "", "Filtered subtitle hallucination"

        replacements = {
            r"\bconfession of faith\b": "ticības apliecība",
            r"\bholy spirit\b": "Svētais Gars",
            r"\bword of god\b": "Dieva vārds",
            r"\bmy god\b": "mans Dievs",
            r"\bo my god\b": "ak, mans Dievs",
            r"\bo, my god\b": "ak, mans Dievs",
            r"\bjesus christ\b": "Jēzus Kristus",
            r"\bjesus\b": "Jēzus",
            r"\bgod\b": "Dievs",
        }
        changed = False
        for pattern, replacement in replacements.items():
            updated = re.sub(pattern, replacement, repaired, flags=re.IGNORECASE)
            changed = changed or updated != repaired
            repaired = updated

        repaired = " ".join(repaired.split()).strip()
        if changed:
            return repaired, "Repaired term in Latvian transcript"
        return repaired, None

    def _english_intrusion_ratio(self, text: str) -> float:
        words = [word.casefold().strip(".,!?;:()[]{}\"'") for word in text.split()]
        words = [word for word in words if word]
        if not words:
            return 0.0
        english_words = {
            "and", "or", "but", "the", "to", "of", "in", "on", "with", "where",
            "who", "can", "be", "is", "are", "am", "was", "were", "will", "would",
            "not", "this", "that", "there", "here", "you", "your", "my", "wouldn't",
            "me", "we", "our", "he", "his", "she", "her", "they", "them", "ten",
            "faith", "confession", "hope", "unbeliever", "uncircumcised", "have",
            "jesus", "god", "happy", "joyful", "declaration", "commandments",
            "commandment", "observed", "observe", "bypassed", "impact", "life",
            "alone", "head", "high", "going", "ready", "think", "thought", "see",
            "people", "world", "speak", "talk", "say", "said", "make", "made",
        }
        hits = sum(1 for word in words if word in english_words)
        return hits / len(words)


    def _remember_text(self, text: str) -> None:
        combined = f"{self._previous_text} {text}".strip()
        self._previous_text = " ".join(combined.split()[-140:])

    def _looks_like_hint_hallucination(self, text: str) -> bool:
        if not text or "," not in text:
            return self._has_excessive_repetition(text)
        pieces = [self._normalize_term(piece) for piece in text.split(",") if piece.strip()]
        if len(pieces) < 4:
            return self._has_excessive_repetition(text)
        matches = sum(1 for piece in pieces if piece in self._hint_terms)
        return matches / len(pieces) >= 0.7 or self._has_excessive_repetition(text)

    def _has_excessive_repetition(self, text: str) -> bool:
        words = [word.strip(" .,!?;:").casefold() for word in text.split() if word.strip(" .,!?;:")]
        if len(words) < 8:
            return False
        most_common_count = max(words.count(word) for word in set(words))
        repeated_run = 1
        longest_run = 1
        for previous, current in zip(words, words[1:]):
            if previous == current:
                repeated_run += 1
                longest_run = max(longest_run, repeated_run)
            else:
                repeated_run = 1
        if len(words) >= 12:
            bigrams = list(zip(words, words[1:]))
            most_common_bigram_count = max(bigrams.count(bigram) for bigram in set(bigrams))
            if most_common_bigram_count / len(bigrams) >= 0.35:
                return True
        return longest_run >= 6 or most_common_count / len(words) >= 0.55

    def _normalize_term(self, text: str) -> str:
        return " ".join(text.casefold().strip(" .!?;:").split())


class OpenAITranscriber:
    def __init__(self, config: AppConfig, glossary: Glossary, status_cb) -> None:
        self.config = config
        self.glossary = glossary
        self.status_cb = status_cb
        self._client = None
        self._previous_text = ""

    def ensure_model(self) -> None:
        if not self.config.openai_api_key:
            raise RuntimeError("OPENAI_API_KEY is not set.")
        if self._client is None:
            from openai import OpenAI

            self._client = OpenAI(api_key=self.config.openai_api_key, max_retries=0, timeout=20.0)
        self.status_cb(f"OpenAI transcription ready: {self.config.openai_transcription_model}.")

    def transcribe(self, audio_float32, leading_context_seconds: float = 0.0) -> TranscriptionResult:
        self.ensure_model()
        start = time.monotonic()
        audio = np.asarray(audio_float32, dtype=np.float32)
        if leading_context_seconds > 0.0:
            start_frame = min(audio.size, int(16_000 * leading_context_seconds))
            audio = audio[start_frame:]
        if audio.size == 0:
            return TranscriptionResult(text="", uncertain=False)
        rms = float(np.sqrt(np.mean(np.square(audio)))) if audio.size else 0.0
        peak = float(np.max(np.abs(audio))) if audio.size else 0.0
        if self.config.vad_enabled and rms < self.config.vad_rms_threshold and peak < self.config.vad_peak_threshold:
            self.status_cb(f"OpenAI upload skipped by final quiet-audio guard: rms {rms:.4f}, peak {peak:.3f}.")
            return TranscriptionResult(text="", uncertain=False)

        wav_buffer = io.BytesIO()
        try:
            sf.write(wav_buffer, audio, 16_000, format="FLAC", subtype="PCM_16")
            wav_buffer.name = "chunk.flac"
        except Exception:
            wav_buffer = io.BytesIO()
            sf.write(wav_buffer, audio, 16_000, format="WAV", subtype="PCM_16")
            wav_buffer.name = "chunk.wav"
        wav_buffer.seek(0)

        assert self._client is not None
        last_error = None
        response = None
        for attempt in range(1, 4):
            try:
                wav_buffer.seek(0)
                response = self._client.audio.transcriptions.create(
                    model=self.config.openai_transcription_model,
                    file=wav_buffer,
                    language="lv",
                )
                break
            except Exception as exc:
                last_error = exc
                msg = str(exc).lower()
                if ("429" in msg or "rate" in msg or "timeout" in msg or "503" in msg) and attempt < 3:
                    backoff = 1.0 * (2 ** (attempt - 1)) + (time.monotonic() % 0.5)
                    self.status_cb(f"[OPENAI STT] Transient error/rate-limit; retrying in {backoff:.1f}s (attempt {attempt}/3)...")
                    time.sleep(backoff)
                else:
                    raise exc

        text = self._response_text(response)
        text = self._filter_prompt_hallucinations(text)
        text = self._dedupe_against_previous(text)
        text = self._collapse_repetitive_tail(text)
        text, english_note = self._filter_english_intrusion(text)
        if text:
            self._remember_text(text)

        elapsed = time.monotonic() - start
        audio_seconds = max(0.001, len(audio_float32) / 16_000)
        speed = audio_seconds / max(0.001, elapsed)
        self.status_cb(f"[OPENAI STT] Transcribed {audio_seconds:.1f}s in {elapsed:.1f}s ({speed:.1f}x real time).")
        return TranscriptionResult(text=text, uncertain=bool(english_note), confidence_note=english_note)

    def _response_text(self, response) -> str:
        if isinstance(response, str):
            return response.strip()
        return str(getattr(response, "text", "") or "").strip()

    def _filter_prompt_hallucinations(self, text: str) -> str:
        if not text:
            return ""
        hallucinated_patterns = [
            r"^\s*Kristīgs\s+dievkalpojums[,\s]+sprediķis[,\s]+Dievs[,\s]+Jēzus\s+Kristus[,\s]+Svētais\s+Gars[,\s]+Bībele[,\s]+lūgšana[,\s]+ticība[,\s]+draudze\.?\s*",
            r"\bKristīgs\s+dievkalpojums[,\s]+sprediķis[,\s]+Dievs[,\s]+Jēzus\s+Kristus[,\s]+Svētais\s+Gars[,\s]+Bībele[,\s]+lūgšana[,\s]+ticība[,\s]+draudze\.?\b",
            r"\b(?:Tas|Tā)\s+ir\s+kristīgs\s+dievkalpojuma\s+sprediķis\b.*",
            r"\bTranskribējiet\s+precīzi\b.*",
            r"\bPēdējais\s+teksts:?\b.*",
            r"\bTas\s+Kungs\s+to\s+ir\s+radījis\s+un\s+veidojis\b.*",
            r"\bViss\s+mūsu\s+personīgajās\s+attiecībās\s+sākās\b.*",
            r"\bMēs\s+augam\s+šajās\s+attiecībās\b.*",
            r"\bSvētapziņa\s+tā\s+būtu\s+atsevišķa\s+plaša\s+tēma\b.*",
            r"\bpar\s+Dievu,\s+Jēzu\s+Kristu,\s+Svēto\s+Garu\b.*",
        ]
        cleaned = text
        for pat in hallucinated_patterns:
            cleaned = re.sub(pat, "", cleaned, flags=re.IGNORECASE).strip()
        return cleaned

    def _source_terms(self) -> list[str]:
        defaults = [
            "Jēzus",
            "Kungs",
            "Dievs",
            "Dieva vārds",
            "Svētais Gars",
            "ticība",
            "ticības vīri",
            "brāļi",
            "māsas",
            "draudze",
            "dievkalpojums",
            "sprediķis",
        ]
        terms = [
            *defaults,
            *self.glossary.source_replacements.keys(),
            *self.glossary.source_replacements.values(),
            *self.glossary.translation_terms.keys(),
        ]
        return [term for term in dict.fromkeys(terms) if term]

    def _dedupe_against_previous(self, text: str) -> str:
        text = " ".join(text.split()).strip()
        if not text or not self._previous_text:
            return text
        previous_words = self._previous_text.split()
        current_words = text.split()
        previous_folded = [word.casefold().strip(" .,!?;:") for word in previous_words]
        current_folded = [word.casefold().strip(" .,!?;:") for word in current_words]
        max_overlap = min(len(previous_words), len(current_words), 40)
        for size in range(max_overlap, 1, -1):
            if previous_folded[-size:] == current_folded[:size]:
                return " ".join(current_words[size:]).strip()
        return text

    def _collapse_repetitive_tail(self, text: str) -> str:
        words = text.split()
        if len(words) < 12:
            return text
        folded = [word.casefold().strip(" .,!?;:") for word in words]
        for phrase_len in range(6, 0, -1):
            if len(words) < phrase_len * 3:
                continue
            phrase = folded[-phrase_len:]
            repeats = 1
            cursor = len(words) - phrase_len * 2
            while cursor >= 0 and folded[cursor : cursor + phrase_len] == phrase:
                repeats += 1
                cursor -= phrase_len
            if repeats >= 3:
                keep_until = len(words) - (repeats - 1) * phrase_len
                return " ".join(words[:keep_until]).strip()
        return text

    def _filter_english_intrusion(self, text: str) -> tuple[str, str | None]:
        if not text:
            return text, None
        ratio = self._english_intrusion_ratio(text)
        if ratio >= 0.5:
            return "", f"Filtered English STT intrusion ({ratio * 100:.0f}%)"
        return text, None

    def _english_intrusion_ratio(self, text: str) -> float:
        words = [word.casefold().strip(".,!?;:()[]{}\"'") for word in text.split()]
        words = [word for word in words if word]
        if not words:
            return 0.0
        english_words = {
            "and", "or", "but", "the", "to", "of", "in", "on", "with", "where",
            "who", "can", "be", "is", "are", "am", "was", "were", "will", "would",
            "not", "this", "that", "there", "here", "you", "your", "my", "wouldn't",
            "me", "we", "our", "he", "his", "she", "her", "they", "them", "ten",
            "faith", "confession", "hope", "unbeliever", "uncircumcised", "have",
            "jesus", "god", "happy", "joyful", "declaration", "commandments",
            "commandment", "observed", "observe", "bypassed", "impact", "life",
            "alone", "head", "high", "going", "ready", "think", "thought", "see",
            "people", "world", "speak", "talk", "say", "said", "make", "made",
        }
        hits = sum(1 for word in words if word in english_words)
        return hits / len(words)

    def _remember_text(self, text: str) -> None:
        combined = f"{self._previous_text} {text}".strip()
        self._previous_text = " ".join(combined.split()[-140:])


SERMON_TRANSLATION_SYSTEM_INSTRUCTIONS = (
    "You are an expert real-time translator for live Christian church services. "
    "You are translating spoken Latvian sermon audio transcripts into {languages} for church congregation members.\n\n"
    "CRITICAL THEOLOGICAL & CONTEXTUAL RULES:\n"
    "1. CHRISTIAN THEOLOGY & SERMON CONTEXT: Understand that this is a Christian sermon. "
    "All references to 'Tas Kungs' / 'Kungs' mean 'The Lord' / 'Господь', 'Dievs' means 'God' / 'Бог', "
    "'Svētais Gars' means 'Holy Spirit' / 'Святой Дух', 'Jēzus Kristus' means 'Jesus Christ' / 'Иисус Христос'.\n"
    "2. PRONOUNS ('Viņš' / 'Viņu' = He / Him) VS LITERAL WINE ('vīns' / 'vīnu'):\n"
    "   - In the context of faith, prayer, personal relationship, fellowship, spiritual life, or following the Lord: 'viņš', 'viņu', 'ar viņu / Viņu' refers to God / Jesus Christ ('He', 'Him', 'with Him' / 'с Ним'). For example: 'mūsu personīgās attiecības sākas ar Viņu' MUST be translated as 'our personal relationship begins with Him' / 'наши личные отношения начинаются с Ним'.\n"
    "   - In the context of Holy Communion / Lord's Supper, the wedding at Cana, bread and wine, a cup of wine, or drinking: 'vīns', 'vīnu' refers to literal wine ('wine' / 'вино', e.g. 'cup of wine', 'bread and wine', 'water turned into wine').\n"
    "3. SPEECH RECOGNITION ROBUSTNESS: The input is generated from live spoken audio and may contain minor speech-to-text slips or incomplete clauses. "
    "Translate the clear intended meaning in light of the sermon theme rather than translating phonetically confused words literally.\n"
    "4. NATURAL SPOKEN FLOW: Output natural, fluent, spoken phrasing suitable for live earphone audio. Do not repeat previous context sentences.\n"
    "5. OUTPUT FORMAT: Return ONLY the final translation without commentary, prefixes, notes, or explanations."
)


class Translator:
    def __init__(self, config: AppConfig, glossary: Glossary, status_cb=None) -> None:
        self.config = config
        self.glossary = glossary
        self.status_cb = status_cb or (lambda msg: None)
        self._translate_client = None
        self._rate_lock = threading.Lock()
        self._last_call_time = 0.0
        self._genai_client = None
        self._legacy_genai = None
        self._model_cooldowns: dict[str, float] = {}
        if config.gemini_api_key:
            self._init_gemini_client(config.gemini_api_key)
        else:
            self._gemini_model_name = ""
        self._context: dict[str, str] = {}
        self._history: list[dict[str, str]] = []

    def _is_cooling_down(self, model_name: str) -> bool:
        expires = self._model_cooldowns.get(model_name, 0.0)
        if expires > time.monotonic():
            return True
        if model_name in self._model_cooldowns:
            del self._model_cooldowns[model_name]
        return False

    def _set_cooldown(self, model_name: str, duration_seconds: float = 60.0) -> None:
        self._model_cooldowns[model_name] = time.monotonic() + duration_seconds

    def _get_model_candidates(self) -> list[str]:
        user_choice = (self._gemini_model_name or self.config.gemini_model or "gemini-2.0-flash").strip()
        defaults = [
            user_choice,
            "gemini-2.0-flash",
            "gemini-2.0-flash-lite",
            "gemini-1.5-flash",
            "gemini-1.5-flash-8b",
        ]
        deduped = list(dict.fromkeys([c for c in defaults if c]))
        active = [c for c in deduped if not self._is_cooling_down(c)]
        if not active:
            self._model_cooldowns.clear()
            active = deduped
        return active[:2]

    def _init_gemini_client(self, api_key: str) -> None:
        key = api_key.strip()
        self._gemini_model_name = self.config.gemini_model or "gemini-2.0-flash"
        try:
            from google import genai

            self._genai_client = genai.Client(api_key=key)
        except Exception as exc:
            self.status_cb(f"[GEMINI] SDK init note: {exc}; using fallback engine.")
            self._genai_client = None
            try:
                import google.generativeai as genai_legacy

                genai_legacy.configure(api_key=key)
                self._legacy_genai = genai_legacy
            except Exception:
                self._legacy_genai = None

    def translate_joint(self, text: str, target_languages: list[str]) -> dict[str, str]:
        if not text.strip() or not target_languages:
            return {}
        clean_text = self.glossary.apply_source_replacements(text)
        if len(target_languages) == 1:
            lang = target_languages[0]
            return {lang: self.translate(clean_text, lang)}

        if self.config.free_tier_mode and (self.config.translation_provider in {"gemini", "auto"} and self.config.gemini_api_key):
            try:
                results = self._translate_joint_with_gemini(clean_text, target_languages)
                if results:
                    self._remember_joint_context(clean_text, results)
                    return results
            except Exception as exc:
                self.status_cb(f"[GEMINI JOINT] Seamless fallback triggered: {exc}")

        results = {}
        for lang in target_languages:
            results[lang] = self.translate(clean_text, lang)
        return results

    def _translate_joint_with_gemini(self, text: str, target_languages: list[str]) -> dict[str, str]:
        if not self.config.gemini_api_key or not self.config.gemini_api_key.strip():
            raise RuntimeError(
                "GEMINI_API_KEY is missing or empty. Please click 'Set Gemini API Key' in the app or add GEMINI_API_KEY=your_key to your .env file."
            )

        clean_text = self.glossary.apply_source_replacements(text)
        lang_names = [LANGUAGE_NAMES[l] for l in target_languages if l in LANGUAGE_NAMES]
        lang_str = " and ".join(lang_names)
        hints = "\n\n".join(self.glossary.prompt_hints(l) for l in target_languages if l in LANGUAGE_NAMES)
        context_str = self._get_sermon_context_prompt(target_languages)

        system_instruction = SERMON_TRANSLATION_SYSTEM_INSTRUCTIONS.format(languages=lang_str)

        prompt = (
            f"{system_instruction}\n\n"
            f"{hints}\n\n"
            f"{context_str}\n\n"
            f"Latvian Sermon Text to Translate into {lang_str}:\n{clean_text}\n\n"
            "Return ONLY a valid JSON object mapping language codes ('en', 'ru') to their translations. "
            'Example format: {"en": "English translation text", "ru": "Russian translation text"}'
        )

        candidates = self._get_model_candidates()
        start = time.monotonic()

        for model_name in candidates:
            self._pace_request()
            try:
                raw_text = self._call_gemini_api(model_name, prompt)
                parsed = self._parse_json_translation(raw_text, target_languages)
                if parsed:
                    elapsed = time.monotonic() - start
                    self.status_cb(f"[GEMINI JOINT] Translation generated in {elapsed:.2f}s ({model_name}).")
                    return parsed
            except Exception as exc:
                msg = str(exc).lower()
                is_rate_limit = any(k in msg for k in ("429", "quota", "resource_exhausted", "503", "unavailable", "high demand", "500", "502"))
                if is_rate_limit:
                    self._set_cooldown(model_name, 30.0)
                else:
                    self._set_cooldown(model_name, 86400.0)

        raise RuntimeError("Gemini joint translation rate-limited or unavailable.")

    def _call_gemini_api(self, model_name: str, prompt: str) -> str:
        if self._genai_client is None and self._legacy_genai is None:
            if not self.config.gemini_api_key:
                raise RuntimeError("GEMINI_API_KEY is missing.")
            self._init_gemini_client(self.config.gemini_api_key)

        if self._genai_client is not None:
            from google.genai import types
            try:
                config = types.GenerateContentConfig(
                    temperature=0.1,
                    thinking_config=types.ThinkingConfig(thinking_budget=0),
                    http_options=types.HttpOptions(timeout=3500),
                )
                response = self._genai_client.models.generate_content(
                    model=model_name,
                    contents=prompt,
                    config=config,
                )
            except Exception as exc:
                if "thinking" in str(exc).lower():
                    config = types.GenerateContentConfig(
                        temperature=0.1,
                        http_options=types.HttpOptions(timeout=3500),
                    )
                    response = self._genai_client.models.generate_content(
                        model=model_name,
                        contents=prompt,
                        config=config,
                    )
                else:
                    raise
            return self._extract_response_text(response)
        elif self._legacy_genai is not None:
            model = self._legacy_genai.GenerativeModel(model_name)
            response = model.generate_content(prompt, generation_config={"temperature": 0.1})
            return self._extract_response_text(response)
        raise RuntimeError("No Gemini SDK client initialized.")

    def _extract_response_text(self, response) -> str:
        if response is None:
            return ""
        try:
            text = getattr(response, "text", "") or ""
            if text.strip():
                return text.strip()
        except (ValueError, AttributeError):
            pass

        if hasattr(response, "candidates") and response.candidates:
            for c in response.candidates:
                content = getattr(c, "content", None)
                if content and hasattr(content, "parts"):
                    parts = [getattr(p, "text", "") for p in content.parts if getattr(p, "text", "")]
                    joined = " ".join(parts).strip()
                    if joined:
                        return joined
        return ""

    def _parse_json_translation(self, text: str, languages: list[str]) -> dict[str, str]:
        if not text:
            return {}
        clean_text = re.sub(r"^```(?:json)?\s*", "", text.strip(), flags=re.IGNORECASE)
        clean_text = re.sub(r"\s*```$", "", clean_text, flags=re.IGNORECASE).strip()
        try:
            import json

            data = json.loads(clean_text)
            if isinstance(data, dict):
                return {lang: str(data.get(lang, "")).strip() for lang in languages if str(data.get(lang, "")).strip()}
        except Exception:
            pass

        results = {}
        for lang in languages:
            pattern = rf'"{lang}"\s*:\s*"([^"]+)"'
            match = re.search(pattern, text)
            if match:
                results[lang] = match.group(1).strip()
        return results

    def translate(self, text: str, target_language: str) -> str:
        if not text.strip():
            return ""

        clean_text = self.glossary.apply_source_replacements(text)

        if self.config.translation_provider in {"gemini", "auto"} and self.config.gemini_api_key:
            try:
                translated = self._translate_with_gemini(clean_text, target_language)
                if translated:
                    self._remember_context(target_language, clean_text, translated)
                    return translated
            except Exception as exc:
                self.status_cb(f"[TRANSLATION] Gemini unavailable ({exc}); falling back immediately to instant web translate.")

        if self.config.google_application_credentials or self.config.google_translate_api_key:
            try:
                translated = self._translate_with_google_cloud(clean_text, target_language)
                if translated:
                    self._remember_context(target_language, clean_text, translated)
                    return translated
            except Exception as exc:
                self.status_cb(f"[TRANSLATION] Google Cloud Translate failed: {exc}")

        # Emergency free Google translate web fallback so translation NEVER stalls
        try:
            start_fallback = time.monotonic()
            translated = self._free_google_translate_fallback(clean_text, target_language)
            if translated:
                elapsed = time.monotonic() - start_fallback
                self.status_cb(f"[FREE TRANSLATE] {target_language.upper()} translation generated in {elapsed:.2f}s.")
                self._remember_context(target_language, clean_text, translated)
                return translated
        except Exception as exc:
            self.status_cb(f"[FREE TRANSLATE] Emergency fallback failed: {exc}")

        raise RuntimeError(
            "No active translation provider available. Please set a Gemini API Key in the app."
        )

    def _free_google_translate_fallback(self, text: str, target_language: str) -> str:
        import json, urllib.parse, urllib.request
        clients = ["gtx", "dict-chrome-ex", "t"]
        for client in clients:
            try:
                url = f"https://translate.googleapis.com/translate_a/single?client={client}&sl=lv&tl={target_language}&dt=t&q={urllib.parse.quote(text)}"
                req = urllib.request.Request(
                    url,
                    headers={
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
                        "Accept": "*/*",
                    },
                )
                with urllib.request.urlopen(req, timeout=3.5) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                if data and isinstance(data, list) and data[0]:
                    translated_pieces = [piece[0] for piece in data[0] if piece and piece[0]]
                    result = "".join(translated_pieces).strip()
                    if result:
                        return result
            except Exception:
                continue
        return ""

    def _pace_request(self) -> None:
        with self._rate_lock:
            self._last_call_time = time.monotonic()

    def _translate_with_gemini(self, text: str, target_language: str) -> str:
        if not self.config.gemini_api_key or not self.config.gemini_api_key.strip():
            raise RuntimeError(
                "GEMINI_API_KEY is missing or empty. Please click 'Set Gemini API Key' in the app or add GEMINI_API_KEY=your_key to your .env file."
            )

        clean_text = self.glossary.apply_source_replacements(text)
        language_name = LANGUAGE_NAMES[target_language]
        hints = self.glossary.prompt_hints(target_language)
        context_str = self._get_sermon_context_prompt([target_language])

        system_instruction = SERMON_TRANSLATION_SYSTEM_INSTRUCTIONS.format(languages=language_name)

        prompt = (
            f"{system_instruction}\n\n"
            f"{hints}\n\n"
            f"{context_str}\n\n"
            f"Latvian Sermon Text to Translate into {language_name}:\n{clean_text}\n\n"
            "Translation (return ONLY the translated text without commentary):"
        )

        candidates = self._get_model_candidates()
        start = time.monotonic()
        last_error = None
        for model_name in candidates:
            self._pace_request()
            try:
                result = self._call_gemini_api(model_name, prompt)
                if result:
                    elapsed = time.monotonic() - start
                    self.status_cb(f"[GEMINI] {target_language.upper()} translation generated in {elapsed:.2f}s.")
                    return result
            except Exception as exc:
                last_error = exc
                message = str(exc).lower()
                is_rate_limit = any(k in message for k in ("quota", "429", "resource_exhausted", "503", "unavailable", "high demand", "500", "502"))
                if is_rate_limit:
                    self._set_cooldown(model_name, 30.0)
                else:
                    self._set_cooldown(model_name, 86400.0)

        if last_error:
            raise last_error
        raise RuntimeError("Gemini translation rate-limited or unavailable.")

    def _translate_with_google_cloud(self, text: str, target_language: str) -> str:
        if self._translate_client is None:
            self._translate_client = translate.Client()
        result = self._translate_client.translate(
            text,
            source_language="lv",
            target_language=target_language,
            format_="text",
        )
        return str(result["translatedText"]).strip()

    def _remember_context(self, target_language: str, source: str, translated: str) -> None:
        if not translated:
            return
        combined = (
            f"{self._context.get(target_language, '')}\n"
            f"LV: {source}\n{LANGUAGE_NAMES.get(target_language, target_language.upper())}: {translated}"
        ).strip()
        self._context[target_language] = "\n".join(combined.splitlines()[-10:])

        if self._history and self._history[-1].get("lv") == source:
            self._history[-1][target_language] = translated
        else:
            self._history.append({"lv": source, target_language: translated})
            if len(self._history) > 8:
                self._history.pop(0)

    def _remember_joint_context(self, source: str, translations: dict[str, str]) -> None:
        entry = {"lv": source, **translations}
        self._history.append(entry)
        if len(self._history) > 8:
            self._history.pop(0)
        for lang, trans in translations.items():
            combined = (
                f"{self._context.get(lang, '')}\n"
                f"LV: {source}\n{LANGUAGE_NAMES.get(lang, lang.upper())}: {trans}"
            ).strip()
            self._context[lang] = "\n".join(combined.splitlines()[-10:])

    def _get_sermon_context_prompt(self, target_languages: list[str]) -> str:
        if not self._history:
            return ""
        lines = ["Previous Sermon Context (for continuity & pronoun/meaning resolution):"]
        for entry in self._history[-4:]:
            lv = entry.get("lv", "")
            if lv:
                lines.append(f"LV: {lv}")
                for lang in target_languages:
                    if lang in entry and entry[lang]:
                        name = LANGUAGE_NAMES.get(lang, lang.upper())
                        lines.append(f"{name}: {entry[lang]}")
        return "\n".join(lines)


class TextToSpeech:
    def __init__(self, config: AppConfig, status_cb=None) -> None:
        self.config = config
        self.status_cb = status_cb or (lambda msg: None)
        self._client = None
        self._voices = {
            "en": TtsVoice("en-US", config.english_voice),
            "ru": TtsVoice("ru-RU", config.russian_voice),
        }
        self._cloud_tts_disabled = False
        if not config.google_application_credentials and not config.google_translate_api_key:
            self._cloud_tts_disabled = True

    def _free_fallback(self, text: str, target_language: str) -> bytes:
        if not text.strip():
            return b""
        start = time.monotonic()
        try:
            import io, urllib.request, urllib.parse, av, numpy as np, soundfile as sf
            url = f"https://translate.google.com/translate_tts?ie=UTF-8&q={urllib.parse.quote(text)}&tl={target_language}&client=tw-ob"
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
            with urllib.request.urlopen(req, timeout=5.0) as resp:
                mp3_bytes = resp.read()
            if not mp3_bytes:
                return b""
            container = av.open(io.BytesIO(mp3_bytes))
            resampler = av.AudioResampler(format="flt", layout="mono", rate=16000)
            samples = []
            for frame in container.decode(audio=0):
                resample = resampler.resample(frame)
                if resample:
                    samples.append(resample[0].to_ndarray().flatten())
            if not samples:
                return b""
            audio = np.concatenate(samples).astype(np.float32)
            wav_buf = io.BytesIO()
            sf.write(wav_buf, audio, 16000, format="WAV", subtype="PCM_16")
            elapsed = time.monotonic() - start
            self.status_cb(f"[FREE TTS] {target_language.upper()} audio generated via free TTS in {elapsed:.2f}s.")
            return wav_buf.getvalue()
        except Exception as exc:
            self.status_cb(f"[FREE TTS] Free speech synthesis failed: {exc}")
            return b""

    def synthesize(self, text: str, target_language: str) -> bytes:
        if not text.strip():
            return b""
        start = time.monotonic()
        if self._cloud_tts_disabled:
            return self._free_fallback(text, target_language)

        voice = self._voices[target_language]
        synthesis_input = texttospeech.SynthesisInput(text=text)
        voice_params = texttospeech.VoiceSelectionParams(
            language_code=voice.language_code,
            name=voice.voice_name,
        )
        audio_config = texttospeech.AudioConfig(
            audio_encoding=texttospeech.AudioEncoding.LINEAR16,
            speaking_rate=voice.speaking_rate,
        )
        try:
            if self._client is None:
                self._client = texttospeech.TextToSpeechClient()
            response = self._client.synthesize_speech(
                input=synthesis_input,
                voice=voice_params,
                audio_config=audio_config,
            )
            elapsed = time.monotonic() - start
            self.status_cb(f"[GOOGLE TTS] {target_language.upper()} audio generated in {elapsed:.1f}s.")
            return bytes(response.audio_content)
        except Exception as exc:
            self._cloud_tts_disabled = True
            self.status_cb(f"[GOOGLE TTS] Cloud API error ({exc}). Switched to instant free TTS.")
            return self._free_fallback(text, target_language)


