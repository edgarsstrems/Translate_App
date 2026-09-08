from __future__ import annotations

import queue
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np
import sounddevice as sd
import soundfile as sf

from .audio import SAMPLE_RATE, ChunkRecorder, OrderedAudioPlayer, RecorderInfo, audio_device_name
from .config import AppConfig, app_data_dir
from .glossary import load_glossary
from .services import TextToSpeech, Translator, TranscriptionResult, create_transcriber


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


@dataclass(frozen=True)
class TranslationItem:
    captured_at: float
    transcript: str | None = None
    manual: bool = False
    uncertain: bool = False
    previous_context: str | None = None
    is_split_continuation: bool = False


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


class TranslationEngine:
    def __init__(
        self,
        config: AppConfig,
        settings: EngineSettings,
        on_status: Callable[[str], None],
        on_error: Callable[[str], None],
        on_latency: Callable[[float], None],
        on_transcript: Callable[[str], None],
        on_translation: Callable[[str, str], None],
        on_level: Callable[[float, float], None] | None = None,
    ) -> None:
        self.config = config
        self.settings = settings
        self.on_status = on_status
        self.on_error = on_error
        self.on_latency = on_latency
        self.on_transcript = on_transcript
        self.on_translation = on_translation
        self.on_level = on_level

        self._chunks: queue.Queue[ProcessingItem | None] = queue.Queue(maxsize=config.max_audio_queue_size)
        self._translations: queue.Queue[TranslationItem | None] = queue.Queue(maxsize=config.max_translation_queue_size)
        self._stop = threading.Event()
        self._processor_thread: threading.Thread | None = None
        self._translation_thread: threading.Thread | None = None
        self._recorder: ChunkRecorder | None = None
        self._glossary = load_glossary(Path(__file__).resolve().parents[1])
        self._transcriber = create_transcriber(config, self._glossary, self.on_status)
        self._translator = Translator(config, self._glossary, status_cb=self.on_status)
        self._tts: TextToSpeech | None = None
        self._tts_lock = threading.Lock()
        self._players: dict[str, OrderedAudioPlayer] = {}
        self._debug_dir = app_data_dir() / "debug_audio"
        self._last_speed = 0.0
        self._stats_started_at = time.monotonic()
        self._api_request_count = 0
        self._captured_audio_seconds = 0.0
        self._uploaded_audio_seconds = 0.0
        self._skipped_audio_seconds = 0.0
        self._last_transcript_key = ""
        self._last_spoken_transcript = ""
        self._stitch_buffer = ""
        self._stitch_buffer_captured_at = 0.0
        self._stitch_count = 0
        self._stitch_lock = threading.Lock()
        self._stitch_timer: threading.Timer | None = None

    def start(self) -> None:
        self._stop.clear()
        if self.settings.english_enabled:
            self._players["en"] = OrderedAudioPlayer(
                "English",
                self.settings.english_output_device_index,
                self.settings.english_volume_getter,
                self.on_error,
            )
        if self.settings.russian_enabled:
            self._players["ru"] = OrderedAudioPlayer(
                "Russian",
                self.settings.russian_output_device_index,
                self.settings.russian_volume_getter,
                self.on_error,
            )
        for player in self._players.values():
            player.start()

        self._processor_thread = threading.Thread(target=self._process_loop, name="translation-processor", daemon=True)
        self._translation_thread = threading.Thread(target=self._translation_loop, name="translation-speaker", daemon=True)
        self._processor_thread.start()
        self._translation_thread.start()
        self.on_status("Preparing speech model before listening.")

    def stop(self) -> None:
        self.on_status("Stopping...")
        self._stop.set()
        self._disarm_stitch_timer()
        with self._stitch_lock:
            if self._stitch_buffer:
                flushed_clause = self._stitch_buffer
                captured_at = self._stitch_buffer_captured_at or time.monotonic()
                self._stitch_buffer = ""
                self._stitch_count = 0
                self._enqueue_translation(
                    TranslationItem(
                        captured_at=captured_at,
                        transcript=flushed_clause,
                        manual=False,
                        uncertain=False,
                    )
                )
        if self._recorder:
            self._recorder.stop()
            self._recorder = None
        try:
            self._chunks.put_nowait(None)
        except queue.Full:
            pass
        if self._processor_thread:
            self._processor_thread.join(timeout=5)
            self._processor_thread = None
        try:
            self._translations.put_nowait(None)
        except queue.Full:
            pass
        if self._translation_thread:
            self._translation_thread.join(timeout=5)
            self._translation_thread = None
        for player in self._players.values():
            player.stop()
        self._players.clear()
        self._drain_chunks()
        self._drain_translations()
        self.on_status("Stopped.")

    def _on_chunk(self, chunk_index: int, chunk: np.ndarray, captured_at: float, leading_context_seconds: float) -> None:
        if self._stop.is_set():
            return
        original_duration = chunk.size / SAMPLE_RATE
        self._captured_audio_seconds += original_duration
        vad = self._analyze_speech(chunk, leading_context_seconds)
        if not vad.has_speech:
            self._skipped_audio_seconds += original_duration
            self.on_status(
                f"Chunk {chunk_index}: skipped no-speech audio "
                f"({original_duration:.1f}s, rms {vad.rms:.4f}, peak {vad.peak:.3f}, "
                f"speech {vad.speech_seconds:.1f}s/{vad.speech_ratio:.0%})."
            )
            self._log_usage_stats()
            return
        if self._chunks.full():
            self._collapse_to_latest_chunk(chunk_index, captured_at, original_duration, vad)
            return
        item = ProcessingItem(
            chunk_index=chunk_index,
            captured_at=captured_at,
            audio=vad.audio,
            leading_context_seconds=0.0,
            original_duration=original_duration,
            upload_duration=vad.audio.size / SAMPLE_RATE,
            speech_seconds=vad.speech_seconds,
            speech_ratio=vad.speech_ratio,
            rms=vad.rms,
            peak=vad.peak,
        )
        try:
            self._chunks.put_nowait(item)
        except queue.Full:
            try:
                self._chunks.get_nowait()
                self._chunks.put_nowait(item)
                self.on_error("Transcription is behind; dropped stale audio to stay live.")
            except (queue.Empty, queue.Full):
                self.on_error("Transcription is behind; skipped one audio chunk.")

    def _collapse_to_latest_chunk(
        self,
        chunk_index: int,
        captured_at: float,
        original_duration: float,
        vad: VadResult,
    ) -> None:
        dropped = 0
        while True:
            try:
                old = self._chunks.get_nowait()
                if old is not None:
                    dropped += 1
                    self._skipped_audio_seconds += old.original_duration or old.upload_duration
            except queue.Empty:
                break
        item = ProcessingItem(
            chunk_index=chunk_index,
            captured_at=captured_at,
            audio=vad.audio,
            leading_context_seconds=0.0,
            original_duration=original_duration,
            upload_duration=vad.audio.size / SAMPLE_RATE,
            speech_seconds=vad.speech_seconds,
            speech_ratio=vad.speech_ratio,
            rms=vad.rms,
            peak=vad.peak,
        )
        try:
            self._chunks.put_nowait(item)
            self.on_error(f"Transcription is behind; skipped {dropped} stale chunk(s) and kept the newest audio.")
        except queue.Full:
            self.on_error("Transcription is behind; skipped one audio chunk.")

    def _on_recorder_started(self, info: RecorderInfo) -> None:
        self.on_status(
            "Audio input: "
            f"{info.device_name} [{info.device_index}], "
            f"{info.sample_rate} Hz, {info.channels} channel(s), "
            f"chunk {info.min_chunk_seconds:.1f}-{info.chunk_seconds:.1f}s, "
            f"pause flush {info.early_flush_silence_seconds:.1f}s, overlap {info.overlap_seconds:.1f}s, "
            f"hop {info.hop_seconds:.1f}s, block {info.blocksize} frames, "
            f"VAD {'on' if self.config.vad_enabled else 'off'}."
        )

    def _analyze_speech(self, chunk: np.ndarray, leading_context_seconds: float) -> VadResult:
        start = min(chunk.size, int(SAMPLE_RATE * leading_context_seconds))
        audio = np.asarray(chunk[start:], dtype=np.float32)
        rms = float(np.sqrt(np.mean(np.square(audio)))) if audio.size else 0.0
        peak = float(np.max(np.abs(audio))) if audio.size else 0.0
        if not self.config.vad_enabled:
            return VadResult(True, audio.copy(), audio.size / SAMPLE_RATE, 1.0, rms, peak, 0.0, 0.0)
        if audio.size == 0:
            return VadResult(False, audio, 0.0, 0.0, rms, peak, 0.0, 0.0)

        frame_size = max(1, int(SAMPLE_RATE * 0.03))
        frame_count = audio.size // frame_size
        if frame_count == 0:
            return VadResult(False, audio, 0.0, 0.0, rms, peak, 0.0, 0.0)
        framed = audio[: frame_count * frame_size].reshape(frame_count, frame_size)
        frame_rms = np.sqrt(np.mean(np.square(framed), axis=1))
        frame_peak = np.max(np.abs(framed), axis=1)
        threshold = max(self.config.vad_rms_threshold, float(np.percentile(frame_rms, 20)) * 2.0)
        speech_frames = (frame_rms >= threshold) | (frame_peak >= self.config.vad_peak_threshold)
        speech_seconds = float(np.count_nonzero(speech_frames) * frame_size / SAMPLE_RATE)
        speech_ratio = speech_seconds / max(0.001, audio.size / SAMPLE_RATE)
        
        # Effective minimum speech duration: maximum 0.20s so short phrases and single words
        # (e.g. "Pazaudējat savu bērnu", "Āmen", "Jā", "Paldies") are never dropped.
        min_speech_sec = min(0.20, max(0.10, self.config.vad_min_speech_seconds))
        has_speech = (
            speech_seconds >= min_speech_sec
            and (speech_ratio >= self.config.vad_min_speech_ratio or speech_seconds >= 0.20)
            and (peak >= self.config.vad_peak_threshold or rms >= self.config.vad_rms_threshold)
        )
        if not has_speech:
            return VadResult(False, audio, speech_seconds, speech_ratio, rms, peak, 0.0, 0.0)

        speech_indices = np.flatnonzero(speech_frames)
        padding_frames = int(max(0.40, self.config.vad_padding_seconds) * SAMPLE_RATE)
        trim_start = max(0, int(speech_indices[0]) * frame_size - padding_frames)
        trim_end = min(audio.size, (int(speech_indices[-1]) + 1) * frame_size + padding_frames)
        # Avoid clipping soft lead-in or trailing pauses so acoustic headroom is preserved for Whisper
        if trim_start < int(SAMPLE_RATE * 0.40):
            trim_start = 0
        if (audio.size - trim_end) < int(SAMPLE_RATE * 0.40):
            trim_end = audio.size
        trimmed = audio[trim_start:trim_end].copy()
        return VadResult(
            True,
            trimmed,
            speech_seconds,
            speech_ratio,
            rms,
            peak,
            trim_start / SAMPLE_RATE,
            max(0.0, (audio.size - trim_end) / SAMPLE_RATE),
        )

    def _drain_chunks(self) -> None:
        while True:
            try:
                self._chunks.get_nowait()
            except queue.Empty:
                return

    def _drain_translations(self) -> None:
        while True:
            try:
                self._translations.get_nowait()
            except queue.Empty:
                return

    def _process_loop(self) -> None:
        try:
            self._transcriber.ensure_model()
        except Exception as exc:
            self._stop.set()
            self.on_error(f"Whisper startup failed: {exc}")
            self.on_status("Startup error.")
            return
        if self._stop.is_set():
            return
        try:
            self._recorder = ChunkRecorder(
                self.settings.input_device_index,
                self.config.chunk_seconds,
                self.config.chunk_overlap_seconds,
                self.config.min_chunk_seconds,
                self.config.early_flush_silence_seconds,
                self.config.vad_rms_threshold,
                self.config.vad_peak_threshold,
                self._on_chunk,
                self.on_error,
                self.on_level,
                self._on_recorder_started,
            )
            self._recorder.start()
            self.on_status("Listening.")
        except Exception as exc:
            self._stop.set()
            self.on_error(f"Audio recorder startup failed: {exc}")
            self.on_status("Startup error.")
            return

        while not self._stop.is_set():
            item = self._chunks.get()
            if item is None:
                break
            try:
                if item.manual_text is not None:
                    corrected_text = self._glossary.apply_source_replacements(item.manual_text)
                    self._process_transcript(corrected_text, item.captured_at, uncertain=False, manual=True)
                elif item.audio is not None:
                    if time.monotonic() - item.captured_at > self.config.max_chunk_age_seconds:
                        self._skipped_audio_seconds += item.original_duration or item.upload_duration
                        self.on_error(
                            f"Chunk {item.chunk_index}: dropped stale audio before transcription "
                            f"({time.monotonic() - item.captured_at:.1f}s old)."
                        )
                        continue
                    self._process_chunk(
                        item.chunk_index,
                        item.audio,
                        item.captured_at,
                        item.leading_context_seconds,
                        item.original_duration,
                        item.upload_duration,
                        item.speech_seconds,
                        item.speech_ratio,
                        item.rms,
                        item.peak,
                    )
            except Exception as exc:
                self.on_error(f"Skipped one failed chunk: {exc}")

    def _process_chunk(
        self,
        chunk_index: int,
        chunk: np.ndarray,
        captured_at: float,
        leading_context_seconds: float,
        original_duration: float,
        upload_duration: float,
        speech_seconds: float,
        speech_ratio: float,
        rms: float,
        peak: float,
    ) -> None:
        duration = chunk.size / SAMPLE_RATE
        self.on_status(
            f"Chunk {chunk_index}: upload {duration:.1f}s from {original_duration:.1f}s captured, "
            f"speech {speech_seconds:.1f}s/{speech_ratio:.0%}, queue {self._chunks.qsize()}, "
            f"rms {rms:.4f}, peak {peak:.3f}."
        )
        whisper_audio, gain = self._prepare_whisper_audio(chunk, rms, peak)
        if gain > 1.05:
            self.on_status(f"Chunk {chunk_index}: applied {gain:.1f}x input gain before Whisper.")
        self._save_debug_chunk(chunk_index, whisper_audio)
        start = time.monotonic()
        self._api_request_count += 1
        self._uploaded_audio_seconds += upload_duration or duration
        result = self._transcriber.transcribe(whisper_audio, leading_context_seconds)
        if self._stop.is_set():
            return
        processing_time = time.monotonic() - start
        hop_seconds = max(0.1, duration - leading_context_seconds)
        self._last_speed = duration / max(0.001, processing_time)
        if processing_time > hop_seconds:
            self.on_error(
                f"Speech recognition is slower than real time on chunk {chunk_index}: "
                f"{processing_time:.1f}s for {duration:.1f}s audio with {hop_seconds:.1f}s hop. "
                "Try small, live recognition mode, reduce overlap, or install CUDA 12 runtime for GPU."
            )
        if not result.text:
            self.on_status(f"Chunk {chunk_index}: no transcript. Input rms {rms:.4f}, peak {peak:.3f}.")
            self._log_usage_stats()
            return
        self.on_status(f"Chunk {chunk_index} transcript: {result.text}")
        corrected = self._apply_glossary(result)
        self._process_transcript(corrected.text, captured_at, corrected.uncertain, manual=False)
        self._log_usage_stats()

    def _prepare_whisper_audio(self, chunk: np.ndarray, rms: float, peak: float) -> tuple[np.ndarray, float]:
        if chunk.size == 0:
            return chunk, 1.0
        # Remove DC offset to eliminate low-frequency microphone hum/rumble
        mean_offset = float(np.mean(chunk))
        if abs(mean_offset) > 1e-5:
            chunk = (chunk - mean_offset).astype(np.float32)
            rms = float(np.sqrt(np.mean(np.square(chunk))))
            peak = float(np.max(np.abs(chunk)))
        if rms <= 0.0 or peak <= 0.0:
            return chunk, 1.0
        target_rms = 0.055
        if rms >= target_rms * 0.75:
            return chunk, 1.0
        gain = min(8.0, target_rms / rms, 0.95 / peak)
        if gain <= 1.05:
            return chunk, 1.0
        boosted = np.clip(chunk * gain, -0.98, 0.98).astype(np.float32)
        return boosted, gain

    def _save_debug_chunk(self, chunk_index: int, chunk: np.ndarray) -> None:
        if not self.config.save_debug_audio:
            return
        try:
            self._debug_dir.mkdir(parents=True, exist_ok=True)
            sf.write(self._debug_dir / f"chunk_{chunk_index:05d}.wav", chunk, SAMPLE_RATE)
        except Exception as exc:
            self.on_error(f"Could not save debug audio chunk {chunk_index}: {exc}")

    def _apply_glossary(self, result: TranscriptionResult) -> TranscriptionResult:
        corrected_text = self._glossary.apply_source_replacements(result.text)
        return TranscriptionResult(
            text=corrected_text,
            uncertain=result.uncertain,
            confidence_note=result.confidence_note,
        )

    def _arm_stitch_timer(self, captured_at: float) -> None:
        self._disarm_stitch_timer()
        timeout = calculate_safety_timeout(self.config.chunk_seconds)
        self._stitch_timer = threading.Timer(timeout, self._on_stitch_timeout)
        self._stitch_timer.daemon = True
        self._stitch_timer.start()

    def _disarm_stitch_timer(self) -> None:
        if self._stitch_timer is not None:
            self._stitch_timer.cancel()
            self._stitch_timer = None

    def _on_stitch_timeout(self) -> None:
        with self._stitch_lock:
            if not self._stitch_buffer:
                return
            text_to_flush = self._stitch_buffer
            captured_at = self._stitch_buffer_captured_at or time.monotonic()
            self._stitch_buffer = ""
            self._stitch_count = 0
            self._stitch_timer = None
        timeout = calculate_safety_timeout(self.config.chunk_seconds)
        self.on_status(f"[STITCH] Dynamic safety timer expired ({timeout:.1f}s); flushed buffered clause.")
        self._enqueue_translation(
            TranslationItem(
                captured_at=captured_at,
                transcript=text_to_flush,
                manual=False,
                uncertain=False,
            )
        )

    def _process_transcript(self, transcript: str, captured_at: float, uncertain: bool, manual: bool) -> None:
        transcript_key = " ".join(transcript.casefold().strip().split())
        if not manual and transcript_key and transcript_key == self._last_transcript_key:
            self.on_status("Skipped duplicate transcript; no translation/TTS request made.")
            return
        if not manual and self._looks_like_previous_repeat(transcript_key):
            self.on_status("Skipped near-duplicate transcript; no translation/TTS request made.")
            return
        if transcript_key:
            self._last_transcript_key = transcript_key
        display_text = transcript
        if manual:
            display_text = f"[manual correction] {display_text}"
        elif uncertain:
            display_text = f"[uncertain] {display_text}"
        self.on_transcript(display_text)

        latency = time.monotonic() - captured_at
        self.on_latency(latency)
        if uncertain:
            self.on_status(f"Listening. Latest transcript uncertain, latency: {latency:.1f}s")
        else:
            self.on_status(f"Listening. Latest transcript latency: {latency:.1f}s")

        # Determine if this incoming segment continues an unfinished thought
        is_continuation = False
        prev_context = self._last_spoken_transcript or None
        if self._last_spoken_transcript:
            prev_ended = has_true_sentence_boundary(self._last_spoken_transcript)
            trimmed = transcript.strip()
            starts_connector = is_dangling_connector(trimmed) or trimmed.lower().startswith(("un ", "ka ", "jo ", "lai ", "bet ", "... ", "…"))
            starts_lower = bool(re.match(r'^[a-zāčēģīķļņšūžа-яё]', trimmed))
            if (not prev_ended) or starts_connector or starts_lower:
                is_continuation = True

        if manual or not getattr(self.config, "smart_sentence_stitching", True):
            with self._stitch_lock:
                self._disarm_stitch_timer()
                if self._stitch_buffer:
                    full_text = clean_splice(self._stitch_buffer, transcript)
                    self._stitch_buffer = ""
                    self._stitch_count = 0
                else:
                    full_text = transcript
            self._last_spoken_transcript = full_text
            self._enqueue_translation(
                TranslationItem(
                    captured_at=captured_at,
                    transcript=full_text,
                    manual=manual,
                    uncertain=uncertain,
                    previous_context=prev_context,
                    is_split_continuation=is_continuation,
                )
            )
            return

        with self._stitch_lock:
            self._disarm_stitch_timer()
            if self._stitch_buffer:
                combined_text = clean_splice(self._stitch_buffer, transcript)
                self._stitch_count += 1
                self.on_status(f"[STITCH] Clean-spliced incoming chunk with buffered clause ({len(combined_text)} chars).")
            else:
                combined_text = transcript
                self._stitch_count = 1

            complete_part, trailing_part = split_sentence_boundary(combined_text)

            # Safeguard against excessive buffering without terminal punctuation
            if trailing_part and (len(trailing_part) > 320 or self._stitch_count >= 2):
                self.on_status(f"[STITCH] Buffer threshold reached ({len(trailing_part)} chars / {self._stitch_count} chunks); flushing clause.")
                complete_part = combined_text
                trailing_part = ""

            if complete_part:
                self._last_spoken_transcript = complete_part
                self._enqueue_translation(
                    TranslationItem(
                        captured_at=captured_at,
                        transcript=complete_part,
                        manual=False,
                        uncertain=uncertain,
                        previous_context=prev_context,
                        is_split_continuation=is_continuation,
                    )
                )

            if trailing_part:
                self._stitch_buffer = trailing_part
                self._stitch_buffer_captured_at = captured_at
                self._arm_stitch_timer(captured_at)
                timeout = calculate_safety_timeout(self.config.chunk_seconds)
                self.on_status(f"[STITCH] Buffered incomplete clause: '{trailing_part}' (safety timer {timeout:.1f}s)")
            else:
                self._stitch_buffer = ""
                self._stitch_count = 0

    def _looks_like_previous_repeat(self, transcript_key: str) -> bool:
        if self.config.chunk_overlap_seconds <= 0.0:
            return False
        if not transcript_key or not self._last_transcript_key:
            return False
        previous_words = self._last_transcript_key.split()
        current_words = transcript_key.split()
        if len(current_words) < 4:
            return False
        previous_tail = previous_words[-40:]
        if len(current_words) <= len(previous_tail):
            joined_tail = " ".join(previous_tail)
            if transcript_key in joined_tail:
                return True
        previous_set = set(previous_tail)
        if not previous_set:
            return False
        overlap = sum(1 for word in current_words if word in previous_set)
        return overlap / len(current_words) >= 0.90

    def _enqueue_translation(self, item: TranslationItem) -> None:
        try:
            self._translations.put_nowait(item)
            return
        except queue.Full:
            pass

        try:
            self._translations.get_nowait()
            self._translations.put_nowait(item)
            self.on_error("Translation/TTS is behind; dropped the oldest translation to keep listening live.")
        except queue.Empty:
            pass
        except queue.Full:
            self.on_error("Translation/TTS is behind; skipped one translation.")

    def _translation_loop(self) -> None:
        while not self._stop.is_set():
            item = self._translations.get()
            if item is None:
                break
            if not item.transcript:
                continue

            # Intelligent Coalescing: merge at most 2 waiting items so output remains fast and continuous
            coalesced_items = [item]
            while not self._translations.empty() and len(coalesced_items) < 2:
                try:
                    next_item = self._translations.get_nowait()
                    if next_item is None:
                        # Put back stop signal if encountered
                        try:
                            self._translations.put_nowait(None)
                        except queue.Full:
                            pass
                        break
                    if next_item.transcript:
                        age = time.monotonic() - next_item.captured_at
                        if not next_item.manual and age > self.config.max_tts_age_seconds:
                            continue
                        coalesced_items.append(next_item)
                except queue.Empty:
                    break

            # Filter out stale items
            now = time.monotonic()
            valid_items = [
                it for it in coalesced_items
                if it.manual or (now - it.captured_at <= self.config.max_tts_age_seconds)
            ]
            if not valid_items:
                continue

            if len(valid_items) > 1:
                combined_transcript = " ".join(it.transcript.strip() for it in valid_items if it.transcript)
                self.on_status(f"[GEMINI] Coalesced {len(valid_items)} pending transcription segments into 1 translation request.")
            else:
                combined_transcript = valid_items[0].transcript

            try:
                first_item = valid_items[0]
                self._translate_transcript(
                    combined_transcript,
                    previous_transcript=first_item.previous_context,
                    is_split_continuation=first_item.is_split_continuation,
                )
            except Exception as exc:
                self.on_error(f"Skipped one failed translation/TTS job: {exc}")

    def _translate_transcript(
        self,
        transcript: str,
        previous_transcript: str | None = None,
        is_split_continuation: bool = False,
    ) -> None:
        enabled = []
        if self.settings.english_enabled:
            enabled.append("en")
        if self.settings.russian_enabled:
            enabled.append("ru")
        if not enabled:
            return

        translations: dict[str, str] = {}
        if self.config.free_tier_mode and len(enabled) > 1:
            try:
                translations = self._translator.translate_joint(
                    transcript,
                    enabled,
                    previous_transcript=previous_transcript,
                    is_split_continuation=is_split_continuation,
                )
            except Exception as exc:
                self.on_error(f"Joint translation fallback: {exc}")
                translations = {}

        def process_language(lang: str) -> None:
            if self._stop.is_set():
                return
            translated = translations.get(lang, "")
            if not translated:
                try:
                    start = time.monotonic()
                    translated = self._translator.translate(
                        transcript,
                        lang,
                        previous_transcript=previous_transcript,
                        is_split_continuation=is_split_continuation,
                    )
                    translation_time = time.monotonic() - start
                    if translated:
                        self.on_status(f"[TRANSLATION] {lang.upper()} translation ready in {translation_time:.1f}s.")
                except Exception as exc:
                    self.on_error(f"{lang.upper()} translation failed: {exc}")
                    return

            if not translated:
                return

            self.on_translation(lang, translated)

            try:
                audio_bytes = self._get_tts().synthesize(translated, lang)
                if not self._stop.is_set() and audio_bytes:
                    player = self._players.get(lang)
                    if player:
                        player.enqueue(audio_bytes)
            except Exception as exc:
                self.on_error(f"{lang.upper()} speech synthesis failed: {exc}")

        with ThreadPoolExecutor(max_workers=len(enabled)) as executor:
            list(executor.map(process_language, enabled))

    def _log_usage_stats(self) -> None:
        runtime_minutes = max(0.01, (time.monotonic() - self._stats_started_at) / 60.0)
        raw_minutes = self._captured_audio_seconds / 60.0
        upload_minutes = self._uploaded_audio_seconds / 60.0
        request_rate = self._api_request_count / runtime_minutes
        saved_seconds = max(0.0, self._captured_audio_seconds - self._uploaded_audio_seconds)
        saved_ratio = saved_seconds / max(0.001, self._captured_audio_seconds)
        self.on_status(
            f"Usage: {self._api_request_count} STT request(s), {request_rate:.2f}/min, "
            f"uploaded {upload_minutes:.1f} of {raw_minutes:.1f} audio min, "
            f"skipped/trimmed {saved_ratio:.0%}, queues audio {self._chunks.qsize()} translation {self._translations.qsize()}."
        )

    def _get_tts(self) -> TextToSpeech:
        with self._tts_lock:
            if self._tts is None:
                self._tts = TextToSpeech(self.config, status_cb=self.on_status)
            return self._tts



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
        text = glossary.apply_source_replacements(result.text)
        on_transcript(f"[30s test] {text or '(no transcript)'}")
        on_status("30s STT test complete.")
    except Exception as exc:
        on_error(f"30s STT test transcription failed: {exc}")
