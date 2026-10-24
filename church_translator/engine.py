from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable

import numpy as np
import sounddevice as sd
import soundfile as sf

from .audio import SAMPLE_RATE, ChunkRecorder, OrderedAudioPlayer, RecorderInfo, audio_device_name
from .config import (
    AppConfig,
    app_data_dir,
    project_root,
    get_speed_profile_settings,
    SPEED_MODE_BALANCED,
    SPEED_MODE_LOW_LATENCY,
    SPEED_MODE_CUSTOM,
)
from .backlog import DurableQueue
from .reliability import safe_error, GeminiRetryLater
from .glossary import load_glossary
from .services import LANGUAGE_NAMES, TextToSpeech, Translator, TranscriptionResult, create_transcriber


# ---------------------------------------------------------------------------
# Smart Semantic Sentence Stitching Constants & Helpers
# ---------------------------------------------------------------------------

DANGLING_CONNECTORS = {
    # Latvian
    "ka", "un", "jo", "bet", "lai", "kad", "kur", "kurš", "kura", "kuri", "kuras",
    "kas", "ja", "vai", "nevis", "arī", "taču", "tātad", "tomēr", "kā", "cik", "kādēļ", "kāpēc",
    # English
    "that", "who", "whom", "whose", "which", "because", "and", "but", "or", "so",
    "if", "when", "where", "although", "while", "as", "since", "though", "to", "until",
    "unless", "whether", "whereas",
    # Russian
    "что", "кто", "который", "которая", "которое", "которые", "и", "а", "но",
    "если", "когда", "где", "чтобы", "хотя", "или", "да", "ведь", "как", "пока",
}

DANGLING_MULTI_WORD_CONNECTORS = {
    # Latvian
    "tāpēc ka", "tā kā", "lai gan", "kaut gan", "kā arī", "ne vien", "ne tikai",
    "tiklīdz kā", "kamēr vien", "līdz ar to",
    # English
    "as well as", "even though", "so that", "in order to", "as if", "because of",
    "such that", "as long as",
    # Russian
    "потому что", "так как", "для того чтобы", "с тех пор как", "в то время как",
    "несмотря на то что",
}

COMMON_ABBREVIATIONS = {
    "dr.", "prof.", "mr.", "mrs.", "ms.", "vs.", "e.g.", "i.e.", "etc.", "st.",
    "plkst.", "resp.", "piem.", "skat.", "utt.", "t.i.",
    "г.", "ул.", "д.", "пр.", "др.", "т.д.", "т.п.", "т.е.",
}

PRESERVE_CAPITALIZATION = {
    # Latvian proper names and deities
    "Dievs", "Dieva", "Dievam", "Dievu", "Dievā",
    "Jēzus", "Jēzu", "Jēzum", "Jēzū",
    "Kristus", "Kristu", "Kristum", "Kristū",
    "Kungs", "Kunga", "Kungam", "Kungu",
    "Svētais", "Svēto", "Svētā",
    "Gars", "Gara", "Garam", "Garu",
    "Bībele", "Bībeles", "Bībelei", "Bībeli",
    "Tēvs", "Tēva", "Tēvam", "Tēvu",
    "Pestītājs", "Pestītāja", "Pestītājam", "Pestītāju",
    "Aleluja", "Āmen",
    # English proper names and deities
    "God", "God's", "Jesus", "Christ", "Lord", "Holy", "Spirit", "Father", "Scripture", "Bible", "Amen", "Hallelujah", "I",
    # Russian proper names and deities
    "Бог", "Бога", "Богу", "Богом", "Боге",
    "Господь", "Господа", "Господу", "Господом",
    "Иисус", "Иисуса", "Иисусу", "Иисусом",
    "Христос", "Христа", "Христу", "Христом",
    "Дух", "Духа", "Духу", "Духом",
    "Святой", "Святого", "Святому", "Святым",
    "Отец", "Отца", "Отцу", "Отцом",
    "Библия", "Библии", "Библию", "Аминь", "Аллилуйя",
}


def calculate_safety_timeout(chunk_seconds: float) -> float:
    """Dynamic safety timer: max(16s, chunkDuration + 5s)."""
    return max(16.0, float(chunk_seconds) + 5.0)


def is_dangling_connector(text: str) -> bool:
    """Check if text ends with a dangling conjunction/connector."""
    if not text:
        return False
    # Strip trailing punctuation, whitespace, and ellipses
    cleaned = re.sub(r'[\s.,!?;:…\-"\'—–\(\)]+$', '', text).strip()
    if not cleaned:
        return False
    words = cleaned.casefold().split()
    if not words:
        return False
    if len(words) >= 2:
        last_two = f"{words[-2]} {words[-1]}"
        if last_two in DANGLING_MULTI_WORD_CONNECTORS:
            return True
    return words[-1] in DANGLING_CONNECTORS


def has_true_sentence_boundary(text: str) -> bool:
    """Check if text ends with a genuine, complete sentence terminator.
    
    Ellipses (... or …) and trailing dangling connectors are NOT sentence terminators.
    """
    t = text.strip()
    if not t:
        return False
    # Ellipses are continuation markers, never terminators
    if t.endswith("...") or t.endswith("…") or t.endswith(".."):
        return False
    # Check for terminal punctuation mark
    match = re.search(r'([.!?])[\'")\]]*$', t)
    if not match:
        return False
    # Check if last word is a known abbreviation like Dr., plkst., etc.
    words = t.split()
    if words and words[-1].casefold() in COMMON_ABBREVIATIONS:
        return False
    # Check if clause ends with a dangling connector before or at the terminator
    if is_dangling_connector(t):
        return False
    return True


def split_sentence_boundary(text: str) -> tuple[str, str]:
    """Split text into complete sentences and a trailing incomplete clause.
    
    Returns (complete_part, trailing_part).
    If the entire text is a complete sentence, trailing_part is empty.
    If the entire text is an incomplete fragment, complete_part is empty.
    """
    t = text.strip()
    if not t:
        return "", ""
    if has_true_sentence_boundary(t):
        return t, ""

    # Look for sentence boundaries inside text (.!? followed by space and capital letter or quote)
    matches = list(re.finditer(r'([.!?])[\'")\]]*\s+(?=[A-ZĀČĒĢĪĶĻŅŠŪŽА-ЯЁ"\'«(])', t))
    for m in reversed(matches):
        candidate_end = m.end()
        punct_end = m.start() + 1
        prefix = t[:punct_end].strip()
        suffix = t[candidate_end:].strip()
        if has_true_sentence_boundary(prefix):
            return prefix, suffix

    # If no capital letter followed, check any sentence boundary followed by whitespace
    matches_any = list(re.finditer(r'([.!?])[\'")\]]*\s+', t))
    for m in reversed(matches_any):
        punct_end = m.start() + 1
        prefix = t[:punct_end].strip()
        suffix = t[m.end():].strip()
        if has_true_sentence_boundary(prefix):
            return prefix, suffix

    return "", t


def clean_splice(tail: str, head: str) -> str:
    """Cleanly splice a buffered clause tail with the incoming chunk head.
    
    Strips boundary ellipses and dangling commas, adjusts capitalization,
    and ensures natural punctuation between stitched clauses.
    """
    if not tail:
        return head.strip()
    if not head:
        return tail.strip()

    t = tail.strip()
    h = head.strip()

    had_comma = bool(re.search(r',[\s.]*$', t))

    # Strip boundary ellipses and multiple dots from tail
    t = re.sub(r'[,;\s]*(\.\.\.|\.\.|…)+[,;\s]*$', '', t).rstrip()
    t = re.sub(r'[,;\s]+$', '', t).rstrip()

    # Strip boundary ellipses and leading commas/dashes from head
    h = re.sub(r'^[,;\s]*(\.\.\.|\.\.|…)+[,;\s]*', '', h).lstrip()
    h = re.sub(r'^[,\-—–\s]+', '', h).lstrip()

    if not t:
        return h
    if not h:
        return t

    # Adjust capitalization of head's first word if tail does not end with sentence terminator
    if not re.search(r'[.!?][\'")\]]*$', t):
        first_word_match = re.match(r'^([A-ZĀČĒĢĪĶĻŅŠŪŽА-ЯЁ][a-zāčēģīķļņšūžа-яё]*)\b', h)
        if first_word_match:
            word = first_word_match.group(1)
            if word not in PRESERVE_CAPITALIZATION:
                h = word[0].lower() + word[1:] + h[len(word):]

    # Check if a comma belongs at the junction for subordinate clauses
    subordinate_connectors = {
        "ka", "jo", "lai", "kad", "kur", "kurš", "kura", "kuri", "kuras", "kas", "ja", "tāpēc ka", "tā kā",
        "that", "because", "which", "who", "whom", "whose", "although", "since",
        "что", "чтобы", "потому что", "так как", "если", "когда", "где", "который", "которая", "которое", "которые",
    }
    first_h_word = h.split()[0].casefold() if h.split() else ""
    needs_comma = had_comma or (first_h_word in subordinate_connectors)

    if needs_comma and not t.endswith(('.', '!', '?', ',')):
        result = f"{t}, {h}"
    else:
        result = f"{t} {h}"

    result = re.sub(r'\s+', ' ', result).strip()
    result = re.sub(r',\s*,', ',', result)
    return result


@dataclass(frozen=True)
class EngineSettings:
    input_device_index: int
    english_enabled: bool
    russian_enabled: bool
    english_output_device_index: int | None
    russian_output_device_index: int | None
    english_volume_getter: Callable[[], float]
    russian_volume_getter: Callable[[], float]
    secondary_language: str = "ru"


@dataclass(frozen=True)
class ProcessingItem:
    chunk_index: int
    captured_at: float
    audio: np.ndarray | None = None
    leading_context_seconds: float = 0.0
    original_duration: float = 0.0
    upload_duration: float = 0.0
    speech_seconds: float = 0.0
    speech_ratio: float = 0.0
    rms: float = 0.0
    peak: float = 0.0
    manual_text: str | None = None
    boundary: str = "pause"
    continues_previous: bool = False
    enqueued_at: float = 0.0


@dataclass(frozen=True)
class TranslationItem:
    captured_at: float
    transcript: str | None = None
    manual: bool = False
    uncertain: bool = False
    previous_context: str | None = None
    is_split_continuation: bool = False
    sequence_id: int = 0
    possibly_continues_next: bool = False
    source_segment_ids: list[int] | None = None
    enqueued_at: float = 0.0


@dataclass(frozen=True)
class VadResult:
    has_speech: bool
    audio: np.ndarray
    speech_seconds: float
    speech_ratio: float
    rms: float
    peak: float
    trim_start_seconds: float
    trim_end_seconds: float


class PipelineLatencyTracker:
    def __init__(self):
        from collections import deque
        self._samples = deque(maxlen=2048)
        self._lock = threading.Lock()
        self._consecutive_spikes: int = 0

    @property
    def consecutive_spikes(self) -> int:
        with self._lock:
            return self._consecutive_spikes

    def record(self, latency: float) -> tuple[float, float, float, int]:
        with self._lock:
            self._samples.append(latency)
            if latency > 12.0:
                self._consecutive_spikes += 1
            elif latency <= 8.0:
                self._consecutive_spikes = 0
            count = len(self._samples)
            avg = sum(self._samples) / count
            worst = max(self._samples)
            sorted_samples = sorted(self._samples)
            p95_idx = int(0.95 * (count - 1))
            p95 = sorted_samples[p95_idx]
            return avg, p95, worst, count

    def get_stats(self) -> tuple[float, float, float, int]:
        with self._lock:
            if not self._samples:
                return 0.0, 0.0, 0.0, 0
            count = len(self._samples)
            avg = sum(self._samples) / count
            worst = max(self._samples)
            sorted_samples = sorted(self._samples)
            p95_idx = int(0.95 * (count - 1))
            p95 = sorted_samples[p95_idx]
            return avg, p95, worst, count


class TranslationEngine:
    def __init__(self, config, settings, on_status, on_error, on_latency,
                 on_transcript, on_translation, on_level=None,
                 sermon_context=None, session_manager=None,
                 on_speed_mode_changed=None):
        import uuid
        self.config, self.settings = config, settings
        self.on_status, self.on_error = on_status, on_error
        self.on_latency, self.on_transcript = on_latency, on_transcript
        self.on_translation, self.on_level = on_translation, on_level
        self.sermon_context = sermon_context
        self.session_manager = session_manager
        self._session_id = session_manager.current_session_id if session_manager else None
        self.on_speed_mode_changed = on_speed_mode_changed
        self.active_speed_settings = get_speed_profile_settings(
            config.speed_mode,
            custom_overrides={
                "chunk_seconds": config.custom_chunk_seconds,
                "min_chunk_seconds": config.custom_min_chunk_seconds,
                "early_flush_silence_seconds": config.custom_early_flush_silence_seconds,
                "overlap_seconds": config.custom_overlap_seconds,
                "vad_rms_threshold": config.custom_vad_rms_threshold,
            } if config.speed_mode == SPEED_MODE_CUSTOM else None,
        )
        self.latency_tracker = PipelineLatencyTracker()
        self.session_dir = app_data_dir() / "backlog" / uuid.uuid4().hex
        self._chunks = DurableQueue(self.session_dir / "capture.sqlite", ProcessingItem)
        self._translations = DurableQueue(self.session_dir / "translation.sqlite", TranslationItem)
        self._speech = DurableQueue(self.session_dir / "synthesis.sqlite")
        self._unrecognized = DurableQueue(self.session_dir / "unrecognized.sqlite", ProcessingItem)
        self._stop = threading.Event()
        self._paused = threading.Event()
        self._lifecycle = threading.Lock()
        self._processor_thread = self._translation_thread = self._speech_thread = None
        self._recorder = None
        self._players = {}
        self._glossary = load_glossary(project_root())
        self._transcriber = create_transcriber(config, self._glossary, on_status)
        self._translator = Translator(config, self._glossary, status_cb=on_status, sermon_context=sermon_context)
        self._tts = None
        self._last_spoken_transcript = ""
        self._last_boundary = "pause"
        self._stt_history = []
        self._started = False
        self._capture_sequence = 0

    def start(self):
        with self._lifecycle:
            if self._started:
                raise RuntimeError("This session has already started")
            self._started = True
        secondary_language = self.settings.secondary_language
        if secondary_language not in LANGUAGE_NAMES or secondary_language == "en":
            secondary_language = "ru"
        outputs = {}
        for lang, enabled, output, volume in (
            ("en", self.settings.english_enabled, self.settings.english_output_device_index, self.settings.english_volume_getter),
            (secondary_language, self.settings.russian_enabled, self.settings.russian_output_device_index, self.settings.russian_volume_getter),
        ):
            if enabled:
                # One chronological queue per physical output. Independent
                # devices retain the existing simultaneous listener routing.
                device_key = output if output is not None else sd.default.device[1]
                if device_key in outputs:
                    player = outputs[device_key]
                    player.volume_getters[lang] = volume
                    self._players[lang] = player
                    continue
                player = OrderedAudioPlayer(lang, output, volume, self.on_error,
                                            self.session_dir / f"playback-{lang}.sqlite", self.on_status)
                self._players[lang] = player
                player.volume_getters[lang] = volume
                outputs[device_key] = player
                player.start()
        self.on_status(f"[OUTPUTS] enabled={','.join(self._players)} "
                       f"secondary={secondary_language} secondary_enabled={str(self.settings.russian_enabled).lower()}")
        self._processor_thread = threading.Thread(target=self._process_loop, name="transcription", daemon=True)
        self._translation_thread = threading.Thread(target=self._translation_loop, name="translation", daemon=True)
        self._speech_thread = threading.Thread(target=self._speech_loop, name="synthesis", daemon=True)
        for worker in (self._processor_thread, self._translation_thread, self._speech_thread):
            worker.start()
        self.on_status("Preparing speech model before listening.")

    def pause(self):
        """Pause audio capture and processing gracefully without destroying the session."""
        with self._lifecycle:
            if not self._started or self._stop.is_set() or self._paused.is_set():
                return
            self._paused.set()
            if self._recorder:
                self._recorder.stop()
                self._recorder = None
        if self.session_manager:
            self.session_manager.pause_session()
        self.on_status("⏸ Translation PAUSED. Microphone capture paused; state preserved.")

    def resume(self):
        """Resume audio capture and processing seamlessly in the same session."""
        with self._lifecycle:
            if not self._started or self._stop.is_set() or not self._paused.is_set():
                return
            self._paused.clear()
            chunk_sec = self.active_speed_settings["chunk_seconds"]
            min_chunk_sec = self.active_speed_settings["min_chunk_seconds"]
            flush_silence = self.active_speed_settings["early_flush_silence_seconds"]
            rms_thresh = self.active_speed_settings.get("vad_rms_threshold", self.config.vad_rms_threshold)
            peak_thresh = self.active_speed_settings.get("vad_peak_threshold", self.config.vad_peak_threshold)
            self._recorder = ChunkRecorder(
                self.settings.input_device_index, chunk_sec, 0.0,
                min_chunk_sec, flush_silence,
                rms_thresh, peak_thresh,
                self._on_chunk, self.on_error, self.on_level,
                lambda info: self.on_status(f"Resumed listening: {info.device_name}, {info.sample_rate} Hz mono."),
                dynamic_mode=self.active_speed_settings["mode"] == "dynamic")
            self._recorder.start()
        if self.session_manager:
            self.session_manager.resume_session()
        self.on_status("▶ Translation RESUMED. Microphone active.")

    def switch_speed_mode(self, mode: str, custom_overrides: dict | None = None) -> dict:
        """Dynamically and thread-safely switch the speed profile without losing audio."""
        new_settings = get_speed_profile_settings(mode, custom_overrides=custom_overrides)
        self.active_speed_settings = new_settings
        with self._lifecycle:
            if self._recorder:
                self._recorder.update_parameters(
                    chunk_seconds=new_settings["chunk_seconds"],
                    min_chunk_seconds=new_settings["min_chunk_seconds"],
                    early_flush_silence_seconds=new_settings["early_flush_silence_seconds"],
                    silence_rms_threshold=new_settings.get("vad_rms_threshold"),
                    silence_peak_threshold=new_settings.get("vad_peak_threshold"),
                    dynamic_mode=new_settings["mode"] == "dynamic",
                )
        self.on_status(f"⚡ Translation Speed Mode set to {new_settings['label']}.")
        return new_settings

    def revert_to_balanced_mode(self, reason: str) -> None:
        """Safety recovery: automatically revert to Balanced Mode when Low Latency becomes unstable."""
        balanced_settings = get_speed_profile_settings(SPEED_MODE_BALANCED)
        self.active_speed_settings = balanced_settings
        with self._lifecycle:
            if self._recorder:
                self._recorder.update_parameters(
                    chunk_seconds=balanced_settings["chunk_seconds"],
                    min_chunk_seconds=balanced_settings["min_chunk_seconds"],
                    early_flush_silence_seconds=balanced_settings["early_flush_silence_seconds"],
                    silence_rms_threshold=balanced_settings.get("vad_rms_threshold"),
                    silence_peak_threshold=balanced_settings.get("vad_peak_threshold"),
                )
        self.on_status(
            f"⚡ [SAFETY] Low Latency instability detected ({reason}). "
            f"Automatically returned to Balanced Mode to protect translation quality and quota."
        )
        if self.on_speed_mode_changed:
            self.on_speed_mode_changed(SPEED_MODE_BALANCED)

    def stop(self):
        self.on_status("Stopping capture; draining recorded speech in order...")
        with self._lifecycle:
            self._stop.set()
            if self._recorder:
                self._recorder.stop()
                self._recorder = None
        self._chunks.finish()
        if self._processor_thread:
            self._processor_thread.join()
        self._translations.finish()
        if self._translation_thread:
            self._translation_thread.join()
        self._speech.finish()
        if self._speech_thread:
            self._speech_thread.join()
        for player in set(self._players.values()):
            player.stop()
        for backlog in (self._chunks, self._translations, self._speech, self._unrecognized):
            backlog.close()
        for client in (getattr(self._transcriber, "_client", None),
                       getattr(self._translator, "_genai_client", None)):
            if client and hasattr(client, "close"):
                client.close()
        if self._tts and self._tts._client:
            self._tts._client.transport.close()
        if self.session_dir.exists() and not any(self.session_dir.iterdir()):
            self.session_dir.rmdir()
        elif self.session_dir.exists():
            self.on_error(f"Unfinished speech preserved for recovery in {self.session_dir}")
        self.on_status("Stopped.")

    def _on_chunk(self, chunk_index, chunk, captured_at, leading_context_seconds):
        # Called only by the segmentation worker, never by the audio callback.
        # A new recorder after Pause starts at zero; sermon IDs must not reset.
        chunk_index = self._capture_sequence
        self._capture_sequence += 1
        duration = chunk.size / SAMPLE_RATE
        recorder = self._recorder
        item = ProcessingItem(chunk_index, captured_at, audio=chunk,
                              original_duration=duration, upload_duration=duration,
                              boundary=getattr(recorder, "last_boundary", "pause"),
                              continues_previous=getattr(recorder, "last_continues_previous", False),
                              enqueued_at=time.monotonic())
        self._chunks.put(item)
        depth = self._chunks.qsize()
        self.on_status(f"Segment {chunk_index}: {duration:.2f}s, {item.boundary}, audio queue {depth}.")
        if depth > self.config.max_audio_queue_size:
            self.on_status(f"Recognition backlog: {depth} segments preserved on disk.")

    def _analyze_speech(self, chunk, leading_context_seconds=0.0):
        audio = np.asarray(chunk, dtype=np.float32)
        if audio.size == 0:
            return VadResult(False, audio, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
        rms = float(np.sqrt(np.mean(np.square(audio))))
        peak = float(np.max(np.abs(audio)))
        frame_size = int(SAMPLE_RATE * 0.02)
        speech_frames = 0
        for i in range(0, audio.size - frame_size + 1, frame_size):
            frame = audio[i:i + frame_size]
            f_rms = float(np.sqrt(np.mean(np.square(frame))))
            f_peak = float(np.max(np.abs(frame)))
            if f_rms >= self.config.vad_rms_threshold or f_peak >= self.config.vad_peak_threshold:
                speech_frames += 1
        speech_seconds = speech_frames * 0.02
        speech_ratio = speech_seconds / max(0.001, audio.size / SAMPLE_RATE)
        if not self.config.vad_enabled:
            has_speech = bool(audio.size)
        else:
            has_speech = (
                (speech_seconds >= self.config.vad_min_speech_seconds and speech_ratio >= self.config.vad_min_speech_ratio)
                or (speech_ratio >= 0.5 and speech_seconds >= 0.08)
                or (speech_seconds >= 0.35)
            ) and (rms >= self.config.vad_rms_threshold * 0.4 or peak >= self.config.vad_peak_threshold * 0.4)
        return VadResult(has_speech, audio, speech_seconds, speech_ratio, rms, peak, 0.0, 0.0)

    def _is_hallucinated_repetition(self, chunk_index: int, text: str) -> bool:
        if not text or not text.strip():
            return False
        clean = re.sub(r'[^\w\s]', '', text.casefold()).strip()
        words = clean.split()
        if not words:
            return False
        normalized = " ".join(words)
        for prev in reversed(self._stt_history[-3:]):
            prev_norm = prev.get("normalized", "")
            if not prev_norm:
                continue
            if normalized == prev_norm:
                if len(words) <= 3:
                    repeats = sum(1 for p in self._stt_history[-3:] if p.get("normalized") == normalized)
                    if repeats >= 2:
                        return True
                else:
                    return True
            prev_words = prev_norm.split()
            if len(words) >= 4 and len(prev_words) >= 4:
                common = sum(1 for w in words if w in prev_words)
                if common / max(len(words), len(prev_words)) >= 0.85:
                    return True
        return False

    def _record_stt(self, chunk_index: int, text: str, captured_at: float) -> None:
        clean = re.sub(r'[^\w\s]', '', text.casefold()).strip()
        self._stt_history.append({
            "chunk_index": chunk_index,
            "text": text,
            "normalized": " ".join(clean.split()),
            "captured_at": captured_at,
        })
        if len(self._stt_history) > 12:
            self._stt_history.pop(0)

    def _recent_stt_context(self) -> str:
        if not self._last_spoken_transcript:
            return ""
        words = self._last_spoken_transcript.split()
        return " ".join(words[-15:]).strip()

    def _process_loop(self):
        listening_started = False
        try:
            self._transcriber.ensure_model()
            with self._lifecycle:
                if self._stop.is_set():
                    return
                chunk_sec = self.active_speed_settings["chunk_seconds"]
                min_chunk_sec = self.active_speed_settings["min_chunk_seconds"]
                flush_silence = self.active_speed_settings["early_flush_silence_seconds"]
                rms_thresh = self.active_speed_settings.get("vad_rms_threshold", self.config.vad_rms_threshold)
                peak_thresh = self.active_speed_settings.get("vad_peak_threshold", self.config.vad_peak_threshold)
                self._recorder = ChunkRecorder(
                    self.settings.input_device_index, chunk_sec, 0.0,
                    min_chunk_sec, flush_silence,
                    rms_thresh, peak_thresh,
                    self._on_chunk, self.on_error, self.on_level,
                    lambda info: self.on_status(f"Listening: {info.device_name}, {info.sample_rate} Hz mono."),
                    dynamic_mode=self.active_speed_settings["mode"] == "dynamic")
                self._recorder.start()
                listening_started = True
            while True:
                item = self._chunks.get()
                if item is None:
                    return
                begin = time.monotonic()
                whisper_elapsed = 0.0
                gain = 1.0
                if item.manual_text is not None:
                    text, uncertain = item.manual_text, False
                    analysis = VadResult(True, np.array([]), 0.0, 1.0, 0.0, 0.0, 0.0, 0.0)
                else:
                    analysis = self._analyze_speech(item.audio)
                    # If this segment has no speech and is not a continuation of an earlier cut:
                    if not analysis.has_speech and not (item.continues_previous and item.upload_duration >= 0.2):
                        self.on_status(
                            f"[SEGMENT {item.chunk_index}] {item.upload_duration:.2f}s ({item.boundary}): "
                            f"silence/noise (rms {analysis.rms:.4f}, peak {analysis.peak:.3f}, speech {analysis.speech_seconds:.2f}s); STT skipped."
                        )
                        self._chunks.ack()
                        continue

                    audio, gain = self._prepare_whisper_audio(item.audio, analysis.rms, analysis.peak)
                    if gain >= 1.5:
                        self.on_status(f"Segment {item.chunk_index}: quiet speech boosted {gain:.1f}x before recognition.")
                    if analysis.peak >= 0.999:
                        self.on_error("Input is clipping; reduce the microphone/mixer gain.")
                    if self.config.save_debug_audio:
                        debug = app_data_dir() / "debug_audio"
                        debug.mkdir(parents=True, exist_ok=True)
                        sf.write(debug / f"{self.session_dir.name}_{item.chunk_index:06d}.wav", audio, SAMPLE_RATE)
                        for old in sorted(debug.glob("*.wav"), key=lambda p: p.stat().st_mtime)[:-120]:
                            old.unlink()
                    whisper_start = time.monotonic()
                    ctx = self._recent_stt_context()
                    try:
                        result = self._transcriber.transcribe(audio, 0.0, previous_context=ctx)
                    except TypeError:
                        result = self._transcriber.transcribe(audio, 0.0)
                    whisper_elapsed = time.monotonic() - whisper_start
                    text, uncertain = result.text.strip(), result.uncertain
                    if not text and analysis.peak > 0.00001:
                        self._unrecognized.put(item)
                        self.on_error(f"Segment {item.chunk_index}: no transcript; audio retained in unrecognized backlog for inspection.")
                if text and self._is_hallucinated_repetition(item.chunk_index, text):
                    self.on_status(
                        f"[LOOP GUARD] Segment {item.chunk_index}: repeated recognition flagged for review; "
                        "source and audio retained because a speaker may intentionally repeat a phrase."
                    )
                    self._unrecognized.put(item)
                    uncertain = True
                if text:
                    # Persist recognition before any provider call can fail. Audio
                    # timestamps use monotonic time; convert only for display/storage.
                    if self.session_manager:
                        from .session_manager import TranscriptSegment
                        from datetime import datetime
                        wall_time = time.time() - max(0.0, time.monotonic() - item.captured_at)
                        self.session_manager.append_segment(TranscriptSegment(
                            item.chunk_index, wall_time, datetime.fromtimestamp(wall_time).strftime("%H:%M:%S"),
                            text, "", self.settings.secondary_language, status="recovering",
                            uncertain=uncertain, provider="pending"), session_id=self._session_id)
                    self._record_stt(item.chunk_index, text, item.captured_at)
                    gain_str = f", gain {gain:.1f}x" if gain > 1.05 else ""
                    self.on_status(
                        f"[SEGMENT {item.chunk_index}] STT completed: {whisper_elapsed:.2f}s | "
                        f"{item.upload_duration:.2f}s audio ({item.boundary}){gain_str} | "
                        f"characters={len(text)} uncertain={uncertain} prepared_context=false"
                    )
                    self.on_transcript(("[uncertain] " if uncertain else "") + text)
                    self.on_status(f"[TIMING] segment={item.chunk_index} stage=stt "
                                   f"segment_audio={item.original_duration:.2f}s "
                                   f"queue_wait={max(0.0, begin - item.enqueued_at) if item.enqueued_at else 0.0:.2f}s "
                                   f"request={whisper_elapsed:.2f}s "
                                   f"capture_to_transcript={max(0.0, time.monotonic() - item.captured_at):.2f}s")
                    continuation = item.continues_previous or self._last_boundary == "forced"
                    continuation |= bool(self._last_spoken_transcript and not has_true_sentence_boundary(self._last_spoken_transcript))
                    self._translations.put(TranslationItem(
                        item.captured_at, text, item.manual_text is not None, uncertain,
                        self._last_spoken_transcript[-1200:] or None,
                        continuation if self.config.smart_sentence_stitching else False,
                        item.chunk_index, item.boundary == "forced", [item.chunk_index], time.monotonic()))
                    self._last_spoken_transcript = text
                    self._last_boundary = item.boundary
                self._chunks.ack()
        except Exception as exc:
            if listening_started:
                self.on_error(f"Recognition paused; captured speech retained: {safe_error(exc)}. Backlog: {self.session_dir}")
            else:
                self.on_error(f"Could not start translation: {safe_error(exc)}")
            if not listening_started:
                self.on_status("Startup error.")
        finally:
            self._translations.finish()

    def _with_gemini_recovery(self, operation):
        while True:
            try:
                return operation()
            except GeminiRetryLater as exc:
                if exc.pacing:
                    # Normal request pacing also drains on Stop; only a provider
                    # outage/quota wait is interrupted and preserved for recovery.
                    time.sleep(exc.delay)
                    continue
                self.on_status(f"Waiting for Gemini quota/service recovery ({exc.delay:.1f}s); text retained, retry is automatic.")
                deadline = time.monotonic() + exc.delay
                authorized = self._translator.config.openai_fallback_enabled
                while time.monotonic() < deadline:
                    if self._stop.wait(min(1.0, max(0.0, deadline - time.monotonic()))):
                        raise RuntimeError("Stopped during Gemini recovery; pending text retained") from exc
                    if self._translator.config.openai_fallback_enabled != authorized:
                        break

    @staticmethod
    def _combine_translation(first, following):
        text = first.transcript + "\n" + following.transcript
        if len(text) > 900 or first.manual or following.manual:
            return None
        return replace(first, transcript=text,
                       source_segment_ids=(first.source_segment_ids or [first.sequence_id]) +
                                          (following.source_segment_ids or [following.sequence_id]),
                       possibly_continues_next=following.possibly_continues_next,
                       uncertain=first.uncertain or following.uncertain)

    def _translation_loop(self):
        try:
            if self.config.translation_provider == "gemini" and self.config.gemini_api_key:
                self.on_status("Checking available Gemini Flash-Lite models in background...")
                threading.Thread(target=self._translator.refresh_available_models,
                                 name="gemini-catalog", daemon=True).start()
            while True:
                item = self._translations.get()
                if item is None:
                    return
                # Merge only text already waiting; no extra batching timer.
                langs = list(self._players)
                started = time.monotonic()
                # One context owner and one ordered request per segment.
                def translate_pending():
                    nonlocal item
                    item = self._translations.coalesce_pending(self._combine_translation, max_items=3)
                    self.on_status(f'[CONTEXT] segment={item.sequence_id} evaluating translation reference')
                    self._translator.possibly_continues_next = item.possibly_continues_next
                    return self._translator.translate_joint(
                        item.transcript, langs, previous_transcript=item.previous_context,
                        is_split_continuation=item.is_split_continuation)
                translations = self._with_gemini_recovery(translate_pending)
                translation_elapsed = time.monotonic() - started
                if any(not translations.get(lang, "").strip() for lang in langs):
                    raise RuntimeError("Incomplete translation response")
                self._speech.put({"sequence_id": item.sequence_id, "captured_at": item.captured_at,
                                  "translations": translations, "completed": [], "queued_at": time.monotonic()})
                self._translations.ack()
                for lang, text in translations.items():
                    self.on_translation(lang, text)
                self.on_status(f"[TIMING] segment={item.sequence_id} stage=translation "
                               f"queue_wait={max(0.0, started - item.enqueued_at) if item.enqueued_at else 0.0:.2f}s "
                               f"processing={translation_elapsed:.2f}s "
                               f"capture_to_text={max(0.0, time.monotonic() - item.captured_at):.2f}s "
                               f"backlog={self._translations.qsize()} languages={','.join(langs)}")
                provider_used = getattr(self._translator, "last_provider_used", "gemini")
                if provider_used == 'google_translate':
                    self.on_status(f'[CONTEXT] segment={item.sequence_id} provider=google_translate supplied=false reason=provider_has_no_context_support')
                if self.session_manager:
                    sec_lang = self.settings.secondary_language
                    sec_trans = translations.get(sec_lang, "")
                    self.session_manager.update_translation_group(
                        item.source_segment_ids or [item.sequence_id], translations.get("en", ""),
                        sec_trans, provider_used, session_id=self._session_id)
                self.on_status(f"[TRANSLATION] {provider_used.upper()} completed: {translation_elapsed:.2f}s (Segment {item.sequence_id})")
        except Exception as exc:
            self.on_error(f"Translation paused; pending text retained: {safe_error(exc)}")
            self.on_status("Translation blocked. Stop and check the Activity Log.")
        finally:
            self._speech.finish()

    def _speech_loop(self):
        try:
            while True:
                item = self._speech.get()
                if item is None:
                    return
                captured_at = item.get("captured_at", 0)
                queued_at = item.get("queued_at", 0)
                speech_queue_wait = max(0.0, time.monotonic() - queued_at) if queued_at else 0.0
                if captured_at > 0:
                    age = time.monotonic() - captured_at
                    if age > self.config.max_tts_age_seconds:
                        self.on_status(f"Speech backlog: segment {item['sequence_id']} is {age:.1f}s old; preserving chronological playback.")

                if self._tts is None or (isinstance(self._tts, TextToSpeech) and
                                         self._tts.config.voice_settings != self.config.voice_settings):
                    if self._tts and self._tts._client:
                        self._tts._client.transport.close()
                    self._tts = TextToSpeech(self.config, status_cb=self.on_status)
                failures = []
                for lang, text in item["translations"].items():
                    if lang in item["completed"]:
                        continue
                    tts_start = time.monotonic()
                    try:
                        audio = self._tts.synthesize(text, lang)
                        if not audio:
                            raise RuntimeError('No audio returned')
                    except Exception as exc:
                        failures.append(lang)
                        self.on_status(f'[TTS] {lang.upper()} failed; retained for recovery: {safe_error(exc)}')
                        continue
                    tts_elapsed = time.monotonic() - tts_start
                    self._players[lang].enqueue(audio, language=lang, captured_at=captured_at,
                                                sequence_id=item['sequence_id'])
                    item["completed"].append(lang)
                    self._speech.update_pending(item)
                    self.on_status(f"[TTS] {LANGUAGE_NAMES.get(lang, lang)} synthesized: {tts_elapsed:.2f}s (Segment {item['sequence_id']})")
                    self.on_status(f"[TIMING] segment={item['sequence_id']} stage=tts language={lang} "
                                   f"queue_wait={speech_queue_wait:.2f}s request={tts_elapsed:.2f}s")
                if failures:
                    raise RuntimeError(f'Audio unavailable for {", ".join(failures)}; other language outputs queued')
                self._speech.ack()
                total_latency = time.monotonic() - item["captured_at"]
                avg, p95, worst, count = self.latency_tracker.record(total_latency)
                self.on_latency(total_latency)
                self.on_status(
                    f"[LATENCY] Capture to speech ready: {total_latency:.1f}s | Avg: {avg:.1f}s, P95: {p95:.1f}s, Worst: {worst:.1f}s (Segment {item['sequence_id']})"
                )
                if (
                    self.config.low_latency_auto_return
                    and self.active_speed_settings.get("mode") == SPEED_MODE_LOW_LATENCY
                ):
                    revert_reason = None
                    if self.latency_tracker.consecutive_spikes >= 3:
                        revert_reason = f"3 consecutive latency spikes ({total_latency:.1f}s > 12s)"
                    elif self._translations.qsize() > self.active_speed_settings.get("max_translation_queue_size", 6):
                        revert_reason = f"translation queue backlog reached {self._translations.qsize()}"

                    if revert_reason:
                        self.revert_to_balanced_mode(revert_reason)
        except Exception as exc:
            self.on_error(f"Synthesis paused; pending translations retained: {safe_error(exc)}")

    def _prepare_whisper_audio(self, chunk: np.ndarray, rms: float, peak: float, has_speech: bool = True) -> tuple[np.ndarray, float]:
        if chunk.size == 0 or not has_speech:
            return chunk, 1.0
        # Remove DC offset to eliminate low-frequency microphone hum/rumble
        mean_offset = float(np.mean(chunk))
        if abs(mean_offset) > 1e-5:
            chunk = (chunk - mean_offset).astype(np.float32)
            rms = float(np.sqrt(np.mean(np.square(chunk))))
            peak = float(np.max(np.abs(chunk)))
        if rms <= 0.0 or peak <= 0.0:
            return chunk, 1.0
        target_rms = 0.075
        if rms >= target_rms * 0.85:
            return chunk, 1.0
        # Preserve waveform shape and leave headroom. A 12x ceiling recovers very
        # quiet speech without allowing one transient to clip or distort a word.
        gain = min(12.0, target_rms / rms, 0.92 / peak)
        if gain <= 1.05:
            return chunk, 1.0
        boosted = np.clip(chunk * gain, -0.98, 0.98).astype(np.float32)
        return boosted, gain



def run_transcription_test(
    config: AppConfig,
    input_device_index: int,
    on_status: Callable[[str], None],
    on_error: Callable[[str], None],
    on_transcript: Callable[[str], None],
    duration_seconds: float = 30.0,
) -> None:
    glossary = load_glossary(Path(__file__).resolve().parents[1])
    transcriber = create_transcriber(config, glossary, on_status)
    device_name = audio_device_name(input_device_index)
    frames: list[np.ndarray] = []
    stop_at = time.monotonic() + duration_seconds

    def callback(indata, _frames, _time_info, status) -> None:
        if status:
            on_error(f"Test input warning: {status}")
        frames.append(np.asarray(indata[:, 0], dtype=np.float32).copy())

    on_status(
        f"30s STT test recording from {device_name} [{input_device_index}] at "
        f"{SAMPLE_RATE} Hz, {duration_seconds:.0f}s. Translation/TTS disabled."
    )
    try:
        with sd.InputStream(
            samplerate=SAMPLE_RATE,
            device=input_device_index,
            channels=1,
            dtype="float32",
            blocksize=int(SAMPLE_RATE * 0.1),
            callback=callback,
        ):
            while time.monotonic() < stop_at:
                time.sleep(0.1)
    except Exception as exc:
        on_error(f"30s STT test recording failed: {exc}")
        return

    if not frames:
        on_error("30s STT test captured no audio.")
        return
    audio = np.concatenate(frames)
    rms = float(np.sqrt(np.mean(np.square(audio)))) if audio.size else 0.0
    peak = float(np.max(np.abs(audio))) if audio.size else 0.0
    on_status(f"30s STT test captured {audio.size / SAMPLE_RATE:.1f}s, rms {rms:.4f}, peak {peak:.3f}.")
    try:
        result = transcriber.transcribe(audio, leading_context_seconds=0.0)
        text = result.text
        on_transcript(f"[30s test] {text or '(no transcript)'}")
        on_status("30s STT test complete.")
    except Exception as exc:
        on_error(f"30s STT test transcription failed: {exc}")
