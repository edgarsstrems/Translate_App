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
        self._prompt = self._build_prompt()
        self._prompt = self._sermon_prompt()
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
            initial_prompt=self._context_prompt(),
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

    def _build_prompt(self) -> str:
        return "Latviešu dievkalpojuma sprediķis latviešu valodā."

    def _sermon_prompt(self) -> str:
        terms = ", ".join(self._source_terms()[:40])
        return (
            "Latviešu dievkalpojuma sprediķis latviešu valodā. "
            "Tā ir nepārtraukta mācītāja runa draudzē, ar Bībeles, ticības, cerības, "
            "Jēzus, Dieva, Svētā Gara un draudzes vārdiem. "
            f"Bieži vārdi: {terms}."
        )

    def _context_prompt(self) -> str:
        return self._prompt

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
        tail = " ".join(previous_folded[-16:])
        current = " ".join(current_folded)
        if current and current in tail:
            return ""
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
        repaired = text
        replacements = {
            r"\bconfession of faith\b": "ticības apliecība",
            r"\bholy spirit\b": "Svētais Gars",
            r"\bword of god\b": "Dieva vārds",
            r"\bmy god\b": "mans Dievs",
            r"\bo my god\b": "ak, mans Dievs",
            r"\bo, my god\b": "ak, mans Dievs",
            r"\bjesus\b": "Jēzus",
            r"\bgod\b": "Dievs",
        }
        changed = False
        for pattern, replacement in replacements.items():
            updated = re.sub(pattern, replacement, repaired, flags=re.IGNORECASE)
            changed = changed or updated != repaired
            repaired = updated

        parts = re.split(r"(?<=[.!?])\s+|\n+", repaired)
        kept = []
        dropped = False
        for part in parts:
            candidate = part.strip()
            if not candidate:
                continue
            if self._english_intrusion_ratio(candidate) >= 0.45:
                dropped = True
                continue
            kept.append(candidate)
        repaired = " ".join(kept).strip()

        if repaired and self._english_intrusion_ratio(repaired) >= 0.55:
            return "", "English hallucination filtered"
        if changed or dropped:
            self.status_cb("Filtered English text from Latvian transcript.")
            return repaired, "English text repaired in Latvian transcript"
        return repaired, None

    def _english_intrusion_ratio(self, text: str) -> float:
        words = [word.casefold().strip(".,!?;:()[]{}\"'") for word in text.split()]
        words = [word for word in words if word]
        if not words:
            return 0.0
        english_words = {
            "and", "or", "but", "the", "to", "of", "in", "on", "with", "where",
            "who", "can", "be", "is", "are", "am", "was", "were", "will",
            "not", "this", "that", "there", "here", "you", "your", "my",
            "me", "we", "our", "he", "his", "she", "her", "they", "them",
            "faith", "confession", "hope", "unbeliever", "uncircumcised",
            "jesus", "god", "happy", "joyful", "declaration",
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
        self._prompt = self._sermon_prompt()

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
        response = self._client.audio.transcriptions.create(
            model=self.config.openai_transcription_model,
            file=wav_buffer,
            language="lv",
            prompt=self._context_prompt(),
        )
        text = self._response_text(response)
        text = self._dedupe_against_previous(text)
        text = self._collapse_repetitive_tail(text)
        text, english_note = self._filter_english_intrusion(text)
        if text:
            self._remember_text(text)

        elapsed = time.monotonic() - start
        audio_seconds = max(0.001, len(audio_float32) / 16_000)
        speed = audio_seconds / max(0.001, elapsed)
        self.status_cb(f"OpenAI transcribed {audio_seconds:.1f}s in {elapsed:.1f}s ({speed:.1f}x real time).")
        return TranscriptionResult(text=text, uncertain=bool(english_note), confidence_note=english_note)

    def _response_text(self, response) -> str:
        if isinstance(response, str):
            return response.strip()
        return str(getattr(response, "text", "") or "").strip()

    def _sermon_prompt(self) -> str:
        terms = ", ".join(self._source_terms()[:40])
        return (
            "Latvian church sermon audio. Transcribe only the Latvian speech, without translating. "
            "Keep Latvian diacritics and natural punctuation. "
            f"Common terms and names: {terms}."
        )

    def _context_prompt(self) -> str:
        return self._prompt

    def _source_terms(self) -> list[str]:
        defaults = [
            "Jezus",
            "Kungs",
            "Dievs",
            "Dieva vards",
            "Svetais Gars",
            "ticiba",
            "brali",
            "masas",
            "draudze",
            "dievkalpojums",
            "spredikis",
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
        tail = " ".join(previous_folded[-16:])
        current = " ".join(current_folded)
        if current and current in tail:
            return ""
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
        parts = re.split(r"(?<=[.!?])\s+|\n+", text)
        kept = []
        dropped = False
        for part in parts:
            candidate = part.strip()
            if not candidate:
                continue
            if self._english_intrusion_ratio(candidate) >= 0.45:
                dropped = True
                continue
            kept.append(candidate)
        filtered = " ".join(kept).strip()
        if filtered and self._english_intrusion_ratio(filtered) >= 0.55:
            self.status_cb("Filtered English text from Latvian OpenAI transcript.")
            return "", "English hallucination filtered"
        if dropped:
            self.status_cb("Filtered English text from Latvian OpenAI transcript.")
            return filtered, "English text filtered in Latvian transcript"
        return text, None

    def _english_intrusion_ratio(self, text: str) -> float:
        words = [word.casefold().strip(".,!?;:()[]{}\"'") for word in text.split()]
        words = [word for word in words if word]
        if not words:
            return 0.0
        english_words = {
            "and", "or", "but", "the", "to", "of", "in", "on", "with", "where",
            "who", "can", "be", "is", "are", "am", "was", "were", "will",
            "not", "this", "that", "there", "here", "you", "your", "my",
            "me", "we", "our", "he", "his", "she", "her", "they", "them",
            "faith", "confession", "hope", "love", "spirit", "god", "jesus",
            "christian", "chapter", "gifts", "divine", "greatest", "working",
        }
        hits = sum(1 for word in words if word in english_words)
        return hits / len(words)

    def _remember_text(self, text: str) -> None:
        combined = f"{self._previous_text} {text}".strip()
        self._previous_text = " ".join(combined.split()[-140:])


class Translator:
    def __init__(self, config: AppConfig, glossary: Glossary) -> None:
        self.config = config
        self.glossary = glossary
        self._translate_client = None
        if config.gemini_api_key and config.translation_provider in {"gemini", "auto"}:
            import google.generativeai as genai

            genai.configure(api_key=config.gemini_api_key)
            self._gemini_model_name = config.gemini_model
            self._fallback_model_names = [
                name
                for name in ("gemini-2.5-flash", "gemini-2.0-flash", "gemini-2.0-flash-lite")
                if name != self._gemini_model_name
            ]
            self._gemini_model = genai.GenerativeModel(self._gemini_model_name)
            self._genai = genai
        else:
            self._gemini_model_name = ""
            self._fallback_model_names = []
            self._gemini_model = None
            self._genai = None
        self._context: dict[str, str] = {}

    def translate(self, text: str, target_language: str) -> str:
        if not text.strip():
            return ""
        if self.config.translation_provider == "google":
            translated = self._translate_with_google_cloud(text, target_language)
            self._remember_context(target_language, text, translated)
            return translated
        if self.config.translation_provider == "gemini" and self._gemini_model:
            translated = self._translate_with_gemini(text, target_language)
            self._remember_context(target_language, text, translated)
            return translated
        if self.config.translation_provider == "auto" and self._gemini_model:
            try:
                translated = self._translate_with_gemini(text, target_language)
            except Exception:
                translated = self._translate_with_google_cloud(text, target_language)
            self._remember_context(target_language, text, translated)
            return translated
        translated = self._translate_with_google_cloud(text, target_language)
        self._remember_context(target_language, text, translated)
        return translated

    def _translate_with_gemini(self, text: str, target_language: str) -> str:
        language_name = LANGUAGE_NAMES[target_language]
        prompt = (
            "Translate this Latvian church sermon excerpt into "
            f"{language_name}. Keep it natural for spoken audio. "
            "Use previous context only for continuity, and translate only the current Latvian text. "
            "Return only the translation, with no commentary.\n\n"
            f"{self.glossary.prompt_hints(target_language)}\n\n"
            f"Previous context:\n{self._context.get(target_language, '')}\n\n"
            f"Latvian:\n{text}"
        )
        last_error: Exception | None = None
        for model_name in [self._gemini_model_name, *self._fallback_model_names]:
            try:
                if self._gemini_model is None or model_name != self._gemini_model_name:
                    self._gemini_model_name = model_name
                    if self._genai is None:
                        import google.generativeai as genai

                        genai.configure(api_key=self.config.gemini_api_key)
                        self._genai = genai
                    self._gemini_model = self._genai.GenerativeModel(model_name)
                response = self._gemini_model.generate_content(prompt)
                try:
                    return (response.text or "").strip()
                except ValueError:
                    return ""
            except Exception as exc:
                last_error = exc
                message = str(exc).lower()
                if "quota" in message or "429" in message or "finish_reason" in message:
                    return ""
                if "not found" not in message and "not supported" not in message:
                    break
        if last_error:
            raise last_error
        return ""

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
            f"LV: {source}\n{LANGUAGE_NAMES[target_language]}: {translated}"
        ).strip()
        self._context[target_language] = " ".join(combined.split()[-160:])


class TextToSpeech:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self._client = texttospeech.TextToSpeechClient()
        self._voices = {
            "en": TtsVoice("en-US", config.english_voice),
            "ru": TtsVoice("ru-RU", config.russian_voice),
        }

    def synthesize(self, text: str, target_language: str) -> bytes:
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
        response = self._client.synthesize_speech(
            input=synthesis_input,
            voice=voice_params,
            audio_config=audio_config,
        )
        return bytes(response.audio_content)
