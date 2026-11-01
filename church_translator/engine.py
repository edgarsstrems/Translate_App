from __future__ import annotations

import re
import queue
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
from .speech_audit import SpeechAudit, sample_hash
from .reliability import safe_error, GeminiRetryLater
from .glossary import load_glossary
from .services import LANGUAGE_NAMES, TextToSpeech, Translator, TranscriptionResult, create_transcriber
from .translation_quality import InvalidTranslationOutput


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
    recognized_text: str | None = None
    raw_recognition: str | None = None
    recognition_review: dict | None = None
    capture: dict | None = None


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
    source_duration: float = 0.0


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
            p95_idx = max(0, (95 * count + 99) // 100 - 1)
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
            p95_idx = max(0, (95 * count + 99) // 100 - 1)
            p95 = sorted_samples[p95_idx]
            return avg, p95, worst, count


class TranslationEngine:
    def _recognition_overlap_seconds(self):
        timestamped = (self.config.speech_recognition_backend == 'local' or
                       self.config.openai_transcription_model == 'whisper-1')
        return self.config.stt_overlap_seconds if timestamped else 0.0

    def __init__(self, config, settings, on_status, on_error, on_latency,
                 on_transcript, on_translation, on_level=None,
                 sermon_context=None, session_manager=None,
                 on_speed_mode_changed=None, on_partial=None):
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
        self.playback_latency_tracker = PipelineLatencyTracker()
        self._stage_latency_lock = threading.Lock()
        self._stage_latencies = {}
        self._translation_review_count = 0
        self.session_dir = app_data_dir() / "backlog" / uuid.uuid4().hex
        self._audit = SpeechAudit(self.session_dir / 'speech-audit.sqlite')
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
        self._is_realtime = getattr(self._transcriber, 'is_realtime', False) is True
        if self._is_realtime:
            self._transcriber.bind_stream(self._audit, on_partial,
                lambda: (self._recent_stt_context(), self.sermon_context.recognition_guidance(self._recent_stt_context())
                         if self.sermon_context else None))
        self._translator = Translator(config, self._glossary, status_cb=on_status, sermon_context=sermon_context)
        self._tts = None
        self._lane_tts = {}
        self._language_speech = {}
        self._ready_speech = {}
        self._admission_failed = set()
        self._synthesis_condition = threading.Condition()
        self._synthesis_turns = {}
        self._failed_synthesis_lanes = set()
        self._diagnostic_queue = queue.SimpleQueue()
        self._diagnostic_thread = None
        self._diagnostic_error_count = 0
        self._last_spoken_transcript = ""
        self._last_boundary = "pause"
        self._stt_history = []
        self._started = False
        self._capture_sequence = 0
        self._deferred_yoruba_jobs = 0

    def start(self):
        with self._lifecycle:
            if self._started:
                raise RuntimeError("This session has already started")
            self._started = True
        from .recording_guard import recording_lease
        self._recording_lease = recording_lease(self.session_dir)
        self._recording_lease.__enter__()
        self._start_diagnostics()
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
                                            self.session_dir / f"playback-{lang}.sqlite", self.on_status,
                                            on_started=self._on_playback_started,
                                            on_completed=self._on_playback_completed)
                self._players[lang] = player
                player.volume_getters[lang] = volume
                outputs[device_key] = player
                player.start()
        self.on_status(f"[OUTPUTS] enabled={','.join(self._players)} "
                       f"secondary={secondary_language} secondary_enabled={str(self.settings.russian_enabled).lower()}")
        if len(self._players) > 1:
            self._language_speech = {lang: DurableQueue(self.session_dir / f'synthesis-{lang}.sqlite')
                                     for lang in self._players}
        self._processor_thread = threading.Thread(target=self._process_loop, name="transcription", daemon=True)
        self._translation_thread = threading.Thread(target=self._translation_loop, name="translation", daemon=True)
        self._speech_thread = threading.Thread(target=self._speech_loop, name="synthesis", daemon=True)
        for worker in (self._processor_thread, self._translation_thread, self._speech_thread):
            worker.start()
        self.on_status("Starting continuous capture and preparing speech model.")

    def pause(self):
        """Pause audio capture and processing gracefully without destroying the session."""
        with self._lifecycle:
            if not self._started or self._stop.is_set() or self._paused.is_set():
                return
            self._paused.set()
            if self._recorder:
                self._recorder.stop()
                if self._is_realtime:
                    self._transcriber.seal_capture(self._recorder.capture_id)
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
                self.settings.input_device_index, chunk_sec, self._recognition_overlap_seconds(),
                min_chunk_sec, flush_silence,
                rms_thresh, peak_thresh,
                self._on_chunk, self.on_error, self.on_level,
                lambda info: self.on_status(f"Resumed listening: {info.device_name}, {info.sample_rate} Hz mono."),
                dynamic_mode=self.active_speed_settings["mode"] == "dynamic",
                dynamic_soft_seconds=self.active_speed_settings.get('dynamic_soft_seconds', 6.0),
                capture_audit=self._audit, **self._stream_options())
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
                    dynamic_soft_seconds=new_settings.get('dynamic_soft_seconds', 6.0),
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
        capture_stop_failed = 0
        with self._lifecycle:
            self._stop.set()
            if self._recorder:
                try:
                    self._recorder.stop()
                    if self._is_realtime:
                        self._transcriber.seal_capture(self._recorder.capture_id)
                except Exception as exc:
                    capture_stop_failed = 1
                    self.on_error(f'Capture finalization failed; original journal and pending queues retained: {safe_error(exc)}')
                finally:
                    self._recorder = None
        if self._is_realtime:
            self._transcriber.finish_stream()
        self._chunks.finish()
        if self._processor_thread:
            self._processor_thread.join()
        if self._is_realtime:
            self._transcriber.close()
            self._audit.event('realtime_usage', None, self._transcriber.store.usage())
        self._translations.finish()
        if self._translation_thread:
            self._translation_thread.join()
        self._speech.finish()
        if self._speech_thread:
            self._speech_thread.join()
        for player in set(self._players.values()):
            player.stop()
        self._stop_diagnostics()
        self.completion_info = {name: backlog.qsize() for name, backlog in (
            ('capture_pending', self._chunks), ('translation_pending', self._translations),
            ('synthesis_pending', self._speech), ('audio_review_items', self._unrecognized))}
        self.completion_info['realtime_pending'] = self._transcriber.store.qsize() if self._is_realtime else 0
        self.completion_info['synthesis_pending'] += sum(q.qsize() for q in self._language_speech.values())
        self.completion_info['synthesis_pending'] += sum(q.qsize() for q in self._ready_speech.values())
        self.completion_info['playback_pending'] = sum(player.pending_at_stop for player in set(self._players.values()))
        self.completion_info['yoruba_audio_pending'] = self._deferred_yoruba_jobs
        self.completion_info['translation_review_items'] = self._translation_review_count
        if capture_stop_failed:
            self.completion_info['capture_stop_failed'] = 1
        if self._audit.path.exists():
            try:
                self._audit.event('completion', None, self.completion_info)
                import json
                self.integrity_report = self._audit.integrity_report()
                self.integrity_report.update(self.completion_info)
                (self.session_dir / 'integrity.json').write_text(json.dumps(self.integrity_report, indent=2), encoding='utf-8')
                self.on_status(f"[INTEGRITY] captured={self.integrity_report['captured_samples']} "
                               f"segmented={self.integrity_report['logical_segmented_samples']} "
                               f"uncovered={len(self.integrity_report['uncovered_ranges'])} "
                               f"warnings={self.integrity_report['capture_warning_count']} "
                               f"report={self.session_dir / 'integrity.json'}")
            except Exception as exc:
                self.completion_info['integrity_report_failed'] = 1
                self.on_error(f'Could not save session integrity report; check storage: {safe_error(exc)}')
        for backlog in (*self._ready_speech.values(), *self._language_speech.values(), self._chunks, self._translations, self._speech, self._unrecognized):
            backlog.close()
        for client in (getattr(self._transcriber, "_client", None),
                       getattr(self._translator, "_genai_client", None)):
            if client and hasattr(client, "close"):
                client.close()
        for tts in {id(t): t for t in [self._tts, *self._lane_tts.values()] if t}.values():
            if tts._client:
                tts._client.transport.close()
        if self._is_realtime:
            self._transcriber.close_store()
        if getattr(self, '_recording_lease', None):
            self._recording_lease.__exit__(None, None, None)
            self._recording_lease = None
        if self.session_dir.exists() and not any(self.session_dir.iterdir()):
            self.session_dir.rmdir()
        elif any(value for key, value in self.completion_info.items() if key != 'audio_review_items'):
            self.on_error(f"Unfinished speech preserved for recovery in {self.session_dir}")
        elif self.completion_info['audio_review_items']:
            self.on_status(f"Session drained; {self.completion_info['audio_review_items']} recognition items retained for review in {self.session_dir}")
        self.on_status("Stopped.")

    def _on_chunk(self, chunk_index, chunk, captured_at, leading_context_seconds):
        # Called only by the segmentation worker, never by the audio callback.
        # A new recorder after Pause starts at zero; sermon IDs must not reset.
        chunk_index = self._capture_sequence
        duration = chunk.size / SAMPLE_RATE
        recorder = self._recorder
        capture = getattr(recorder, 'last_capture', None)
        boundary = getattr(recorder, 'last_boundary', 'pause')
        request_duration = ((capture.get('request_end_sample', capture['end_sample']) -
                             capture.get('request_start_sample', capture['start_sample'])) / SAMPLE_RATE
                            if isinstance(capture, dict) else duration)
        item = ProcessingItem(chunk_index, captured_at, audio=chunk,
                              original_duration=duration, upload_duration=request_duration,
                              boundary=boundary if isinstance(boundary, str) else 'pause',
                              continues_previous=getattr(recorder, "last_continues_previous", False),
                              enqueued_at=time.monotonic(),
                              capture=capture if isinstance(capture, dict) else None)
        self._chunks.put(item, key=(f"{capture['capture_id']}:{capture['start_sample']}:{capture['end_sample']}"
                                   if isinstance(capture, dict) else f'segment:{chunk_index}'))
        self._audit.event('captured_segment', chunk_index, {
            'capture': item.capture, 'samples': chunk.size, 'sha256': sample_hash(chunk),
            'boundary': item.boundary, 'continues_previous': item.continues_previous,
            'session_id': self._session_id})
        for state in ('CAPTURED', 'SEGMENTED', 'QUEUED'):
            self._audit.state(chunk_index, state, {'capture': item.capture, 'samples': chunk.size})
        if self._is_realtime and item.capture:
            self._transcriber.register_turn(chunk_index, item.capture, captured_at)
        self._capture_sequence += 1
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
        profile = getattr(self, 'active_speed_settings', {})
        rms_threshold = profile.get('vad_rms_threshold', self.config.vad_rms_threshold)
        peak_threshold = profile.get('vad_peak_threshold', self.config.vad_peak_threshold)
        recorder = getattr(self, '_recorder', None)
        policy = getattr(recorder, '_dynamic_policy', None)
        if profile.get('mode') == 'dynamic' and policy is not None:
            # Use the same quiet-speaker scale as capture boundaries instead of
            # discarding a syllable that the Dynamic segmenter identified as speech.
            rms_threshold = min(rms_threshold, max(.0002, policy.speech_level * .12))
            peak_threshold = min(peak_threshold, rms_threshold * 5)
        frame_size = int(SAMPLE_RATE * 0.02)
        speech_frames = 0
        for i in range(0, audio.size - frame_size + 1, frame_size):
            frame = audio[i:i + frame_size]
            f_rms = float(np.sqrt(np.mean(np.square(frame))))
            f_peak = float(np.max(np.abs(frame)))
            if f_rms >= rms_threshold or f_peak >= peak_threshold:
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
            ) and (rms >= rms_threshold * 0.4 or peak >= peak_threshold * 0.4)
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
            # Shared topic words are not evidence of repeated recognition.
            # Compare the ordered complete wording only, never word membership.
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

    def _stream_options(self):
        return {'on_stream_ready': self._transcriber.feed, 'streaming_mode': True} if self._is_realtime else {}

    def _ack_processing_item(self, item):
        self._chunks.ack()
        if self._is_realtime and item.capture:
            self._transcriber.ack_capture(item.capture)

    def _process_loop(self):
        listening_started = False
        try:
            with self._lifecycle:
                if self._stop.is_set():
                    return
                chunk_sec = self.active_speed_settings["chunk_seconds"]
                min_chunk_sec = self.active_speed_settings["min_chunk_seconds"]
                flush_silence = self.active_speed_settings["early_flush_silence_seconds"]
                rms_thresh = self.active_speed_settings.get("vad_rms_threshold", self.config.vad_rms_threshold)
                peak_thresh = self.active_speed_settings.get("vad_peak_threshold", self.config.vad_peak_threshold)
                self._recorder = ChunkRecorder(
                    self.settings.input_device_index, chunk_sec, self._recognition_overlap_seconds(),
                    min_chunk_sec, flush_silence,
                    rms_thresh, peak_thresh,
                    self._on_chunk, self.on_error, self.on_level,
                    lambda info: self.on_status(f"Listening: {info.device_name}, {info.sample_rate} Hz mono."),
                    dynamic_mode=self.active_speed_settings["mode"] == "dynamic",
                    dynamic_soft_seconds=self.active_speed_settings.get('dynamic_soft_seconds', 6.0),
                    capture_audit=self._audit, **self._stream_options())
                self._recorder.start()
                listening_started = True
            # Device capture and its original PCM journal run even while model
            # initialization or the first provider request is slow.
            self._transcriber.ensure_model()
            while True:
                item = self._chunks.get()
                if item is None:
                    return
                begin = time.monotonic()
                whisper_elapsed = 0.0
                gain = 1.0
                review_audio_retained = False
                translation_allowed = True
                if item.manual_text is not None:
                    text, uncertain = item.manual_text, False
                    analysis = VadResult(True, np.array([]), 0.0, 1.0, 0.0, 0.0, 0.0, 0.0)
                else:
                    analysis = self._analyze_speech(item.audio)
                    from .speech_gate import audio_evidence
                    acoustic = audio_evidence(item.audio)
                    self._audit.event('audio_evidence', item.chunk_index, acoustic)
                    self.on_status(f'[INPUT] segment={item.chunk_index} state={acoustic["state"]} '
                                   f'rms_dbfs={acoustic["rms_dbfs"]} peak_dbfs={acoustic["peak_dbfs"]}')
                    if acoustic['skip_stt']:
                        self._last_spoken_transcript = ''
                        if np.any(item.audio != 0):
                            self._unrecognized.put(replace(item, recognition_review={
                                'reason': 'stationary_noise_gate', 'audio_evidence': acoustic}))
                        self._audit.event('silence_gate', item.chunk_index, {
                            **acoustic, 'samples': item.audio.size})
                        self._audit.state(item.chunk_index, 'CONFIRMED_SILENCE', {
                            'evidence': acoustic, 'original_audio_retained': True})
                        self.on_status(
                            f"[SEGMENT {item.chunk_index}] {item.upload_duration:.2f}s ({item.boundary}): "
                            f"silence/noise (rms {analysis.rms:.4f}, peak {analysis.peak:.3f}, speech {analysis.speech_seconds:.2f}s); STT skipped."
                        )
                        self._ack_processing_item(item)
                        continue

                    # Unresolved quiet audio still reaches STT. Audio evidence,
                    # rather than amplitude alone, controls prompting and gain.
                    if not analysis.has_speech:
                        self.on_status(f'[SEGMENT {item.chunk_index}] Low-energy audio submitted; '
                                       'vad_is_boundary_hint_only=true')

                    original_request = item.audio
                    leading_context = 0.0
                    if item.capture and item.capture.get('request_overlap_samples'):
                        capture = item.capture
                        original_request = self._audit.audio_range(capture['capture_id'],
                            capture['request_start_sample'], capture['request_end_sample'])
                        leading_context = (capture['start_sample'] - capture['request_start_sample']) / SAMPLE_RATE
                    audio = original_request
                    if acoustic['allow_gain'] and not self._is_realtime:
                        request_rms = float(np.sqrt(np.mean(audio * audio)))
                        request_peak = float(np.max(np.abs(audio)))
                        audio, gain = self._prepare_whisper_audio(audio, request_rms, request_peak)
                    if gain >= 1.5:
                        self.on_status(f"Segment {item.chunk_index}: quiet speech boosted {gain:.1f}x before recognition.")
                    if analysis.peak >= 0.999:
                        self.on_error("Input is clipping; reduce the microphone/mixer gain.")
                    if self.config.save_debug_audio:
                        debug = app_data_dir() / "debug_audio"
                        debug.mkdir(parents=True, exist_ok=True)
                        sf.write(debug / f"{self.session_dir.name}_{item.chunk_index:06d}.wav", audio, SAMPLE_RATE)
                        # Diagnostic retention is user-controlled; never rotate
                        # away unresolved speech during a long service.
                    whisper_start = time.monotonic()
                    self._audit.event('recognition_started', item.chunk_index, {'capture': item.capture})
                    self._transcriber.speech_evidence = acoustic
                    self._transcriber.current_capture = item.capture
                    self._transcriber.timestamped_overlap = bool(item.capture and item.capture.get('request_overlap_samples'))
                    def record_submission(phase, details, seq=item.chunk_index, capture=item.capture):
                        if phase == 'submitted':
                            if details.get('attempt', 1) > 1:
                                self._audit.state(seq, 'RETRY_PENDING', {'attempt': details['attempt']})
                            self._audit.state(seq, 'SUBMITTED', {'capture': capture})
                            details = {**details, 'capture': capture, 'gain': gain,
                                       'audio_evidence': acoustic}
                        elif phase == 'failed':
                            self._audit.state(seq, 'FAILED_RETAINED', {'error': details.get('error')})
                        return self._audit.submission(seq, phase, details)
                    self._transcriber.submission_recorder = record_submission
                    # Nonoverlapping audio stays an exact partition. Recent speech
                    # guides decoding but never triggers text-overlap deletion.
                    ctx = (self._recent_stt_context() or None) if acoustic['use_context'] else None
                    prepared = (self.sermon_context.recognition_guidance(ctx or '')
                                if self.sermon_context and acoustic['use_context'] else None)
                    if prepared:
                        self.on_status('[CONTEXT] stage=stt_retrieval '
                                       f'loaded={str(prepared["loaded"]).lower()} '
                                       f'active={str(prepared["active"]).lower()} section={prepared["section"]} '
                                       f'reason={prepared["reason"]} processing_ms={prepared["processing_ms"]}')
                    local_receipt = None
                    if self.config.speech_recognition_backend == 'local':
                        import io
                        buffer = io.BytesIO()
                        sf.write(buffer, audio, SAMPLE_RATE, format='WAV', subtype='FLOAT')
                        local_receipt = record_submission('submitted', {'audio': buffer.getvalue(),
                            'filename': 'local-input.wav', 'model': self.config.whisper_model_size,
                            'diagnostics': {'sample_rate': SAMPLE_RATE, 'submitted_samples': len(audio)}})
                    try:
                        result = self._transcriber.transcribe(audio, leading_context, previous_context=ctx,
                                                             prepared_context=prepared)
                    except Exception as exc:
                        if local_receipt is not None:
                            record_submission('failed', {'receipt': local_receipt, 'error': safe_error(exc)})
                        raise
                    if local_receipt is not None:
                        record_submission('returned', {'receipt': local_receipt, 'response': {
                            'text': result.raw_text or result.text, 'words': result.words,
                            'confidence_note': result.confidence_note}})
                    from .boundary_alignment import owns_timestamped_words
                    result, alignment = owns_timestamped_words(result, item.capture)
                    if alignment.get('review_required'):
                        result = replace(result, translation_allowed=False, coverage_review=True, uncertain=True)
                    whisper_elapsed = time.monotonic() - whisper_start
                    candidate, uncertain = result.text.strip(), result.uncertain
                    source_visible = getattr(result, 'source_visible', True) is not False
                    text = candidate if source_visible else ''
                    translation_allowed = getattr(result, 'translation_allowed', True) is not False
                    self._audit.state(item.chunk_index, 'FAILED_RETAINED' if self._is_realtime and
                                      self._transcriber.last_audio_diagnostics.get('request_status') == 'failed' else 'RECOGNIZED', {
                        'text_characters': len(text), 'translation_allowed': translation_allowed,
                        'speech_completeness_verified': False})
                    evidence = {
                        'reason': getattr(result, 'confidence_note', None),
                        'check': getattr(self._transcriber, 'last_context_reconciliation', {}),
                        'completeness': getattr(self._transcriber, 'last_completeness', {}),
                        'submission': getattr(self._transcriber, 'last_audio_diagnostics', {}),
                        'context': getattr(self._transcriber, 'last_context_diagnostics', {}),
                        'usage': getattr(self._transcriber, 'last_usage', {}),
                        'model': self.config.openai_transcription_model if self.config.speech_recognition_backend == 'openai' else self.config.whisper_model_size,
                        'gain': gain, 'input_sha256': sample_hash(item.audio),
                        'audio_evidence': acoustic,
                        'short_audio': getattr(self._transcriber, 'last_short_audio', {}),
                        'uncertainty_reasons': getattr(result, 'uncertainty_reasons', None),
                        'uncertain_spans': getattr(result, 'uncertain_spans', None),
                        'boundary_alignment': alignment,
                        'processed_sha256': sample_hash(audio), 'queue_wait_seconds': max(0, begin - item.enqueued_at) if item.enqueued_at else 0,
                        'latency_seconds': whisper_elapsed,
                    }
                    for name in ('check', 'completeness', 'submission', 'context', 'usage', 'short_audio'):
                        if not isinstance(evidence[name], dict):
                            evidence[name] = {}
                    for name in ('uncertainty_reasons', 'uncertain_spans'):
                        if not isinstance(evidence[name], list):
                            evidence[name] = []
                    self._audit.event('recognition', item.chunk_index, {
                        **evidence, 'raw_text': getattr(result, 'raw_text', None),
                        'selected_text': candidate, 'source_contribution': text,
                        'source_visible': source_visible, 'uncertain': uncertain})
                    # Checkpoint before downstream writes. Recovery retains the
                    # selected hypothesis as well as the original acoustic data.
                    self._chunks.update_pending(replace(item, recognized_text=text,
                        raw_recognition=getattr(result, 'raw_text', None), recognition_review=evidence))
                    coverage_review = getattr(result, 'coverage_review', False) is True
                    if uncertain or coverage_review:
                        raw = getattr(result, 'raw_text', None)
                        note = getattr(result, 'confidence_note', None)
                        check = getattr(self._transcriber, 'last_context_reconciliation', None)
                        self._unrecognized.put(replace(item, recognized_text=text,
                            raw_recognition=raw if isinstance(raw, str) else text,
                            recognition_review=evidence))
                        review_audio_retained = True
                        self._audit.state(item.chunk_index, 'REVIEW_REQUIRED', {
                            'reason': note, 'source_retained': bool(text),
                            'speech_ranges': acoustic.get('probable_speech_sample_ranges', [])})
                        self.on_status(f'[STT REVIEW] segment={item.chunk_index} uncertain={uncertain} '
                                       f'coverage_review={coverage_review} audio_retained=true action=preserve_wording')
                    if not text and np.any(item.audio != 0) and not review_audio_retained:
                        self._unrecognized.put(item)
                        self._audit.state(item.chunk_index, 'REVIEW_REQUIRED', {
                            'reason': 'no_transcript', 'source_retained': False})
                        self.on_error(f"Segment {item.chunk_index}: no transcript; audio retained in unrecognized backlog for inspection.")
                if (text and self._is_hallucinated_repetition(item.chunk_index, text)
                        and item.manual_text is None and acoustic.get('state') != 'speech_candidate'):
                    self.on_status(
                        f"[LOOP GUARD] Segment {item.chunk_index}: repeated recognition flagged for review; "
                        "source and audio retained because a speaker may intentionally repeat a phrase."
                    )
                    if not review_audio_retained:
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
                        f"characters={len(text)} uncertain={uncertain} "
                        f"prepared_context={str(bool(getattr(self._transcriber, 'last_context_diagnostics', {}).get('prepared_context'))).lower()}"
                    )
                    # Diagnostic confidence labels belong in review metadata,
                    # never as invented words in the pastor's transcript.
                    self.on_transcript(text)
                    self._audit.event('source_committed', item.chunk_index, {
                        'text': text, 'characters': len(text), 'translation_allowed': translation_allowed,
                        'database_saved': self.session_manager is not None, 'ui_update_queued': True,
                        'speech_completeness_verified': False})
                    self._audit.state(item.chunk_index, 'COMMITTED', {
                        'characters': len(text), 'coverage_evaluated': True,
                        'speech_completeness_verified': False})
                    self.on_status(f"[TIMING] segment={item.chunk_index} stage=stt "
                                   f"segment_audio={item.original_duration:.2f}s "
                                   f"queue_wait={max(0.0, begin - item.enqueued_at) if item.enqueued_at else 0.0:.2f}s "
                                   f"request={whisper_elapsed:.2f}s "
                                   f"capture_to_transcript={max(0.0, time.monotonic() - item.captured_at):.2f}s")
                    self._report_stage_latency('transcript', 'lv', time.monotonic() - item.captured_at)
                    continuation = item.continues_previous or self._last_boundary == "forced"
                    continuation |= bool(self._last_spoken_transcript and not has_true_sentence_boundary(self._last_spoken_transcript))
                    if not translation_allowed:
                        self.on_status(f'[STT REVIEW] segment={item.chunk_index} source_visible={source_visible} '
                                       'translation_and_playback=held audio_retained=true')
                        self._last_spoken_transcript = ''
                        self._ack_processing_item(item)
                        continue
                    self._translations.put(TranslationItem(
                        item.captured_at, text, item.manual_text is not None, uncertain,
                        self._last_spoken_transcript[-1200:] or None,
                        continuation if self.config.smart_sentence_stitching else False,
                        item.chunk_index, item.boundary == "forced", [item.chunk_index], time.monotonic(),
                        item.original_duration or (len(item.audio) / SAMPLE_RATE if item.audio is not None else 0.0)),
                        key=f'segment:{item.chunk_index}')
                    self._last_spoken_transcript = text
                    self._last_boundary = item.boundary
                self._ack_processing_item(item)
        except Exception as exc:
            try:
                self._audit.event('recognition_failure', getattr(locals().get('item'), 'chunk_index', None),
                                  {'error': safe_error(exc), 'captured_audio_retained': True})
                self._audit.state(getattr(locals().get('item'), 'chunk_index', None), 'FAILED_RETAINED',
                                  {'error': safe_error(exc)})
            except Exception:
                self.on_error('Could not write the recognition audit; check local storage. Pending queue remains unacknowledged.')
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
                       uncertain=first.uncertain or following.uncertain,
                       source_duration=first.source_duration + following.source_duration)

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
                    self._translator.source_uncertain = item.uncertain
                    self._audit.event('translation_attempt', item.sequence_id, {})
                    self._translator.last_translation_attempts = []
                    try:
                        return self._translator.translate_joint(
                            item.transcript, langs, previous_transcript=item.previous_context,
                            is_split_continuation=item.is_split_continuation)
                    finally:
                        attempts = getattr(self._translator, 'last_translation_attempts', [])
                        if isinstance(attempts, list):
                            for attempt in attempts:
                                self._audit.event('translation_response', item.sequence_id, attempt)
                try:
                    translations = self._with_gemini_recovery(translate_pending)
                except InvalidTranslationOutput as exc:
                    pending = DurableQueue(self.session_dir / 'translation-review.sqlite', TranslationItem)
                    try:
                        pending.put(item, key=f'segment:{item.sequence_id}')
                    finally:
                        pending.close()
                    self._translation_review_count += 1
                    self._audit.event('translation_held', item.sequence_id, {
                        'reason': safe_error(exc), 'source_segment_ids': item.source_segment_ids,
                        'source_text': item.transcript, 'source_preserved': True,
                        'raw_responses_retained': True, 'automatic_repair_exhausted': True})
                    self.on_error(f'Translation {item.sequence_id} needs review after one repair; '
                                  'source and responses retained. Later segments continue; no bad output spoken.')
                    self._translations.ack()
                    continue
                translation_ready_at = time.monotonic()
                translation_elapsed = translation_ready_at - started
                provider = getattr(self._translator, 'last_provider_used', 'unknown')
                from .translation_quality import translation_warnings
                warnings = {lang: issues for lang, output in translations.items()
                            if (issues := translation_warnings(item.transcript, output, lang))}
                if warnings:
                    self._audit.event('translation_review', item.sequence_id, {
                        'warnings': warnings, 'source_segment_ids': item.source_segment_ids,
                        'source_text': item.transcript, 'translations': translations,
                        'semantic_accuracy_verified': False})
                    self.on_status(f'[TRANSLATION REVIEW] segment={item.sequence_id} '
                                   f'warnings={warnings} proof_of_error=false source_preserved=true')
                self._audit.event('translated', item.sequence_id, {'source_segment_ids': item.source_segment_ids,
                                  'source_text': item.transcript, 'translations': translations,
                                  'duration_seconds': translation_elapsed,
                                  'provider': provider if isinstance(provider, str) else 'unknown'})
                if any(not translations.get(lang, "").strip() for lang in langs):
                    raise RuntimeError("Incomplete translation response")
                self._speech.put({"sequence_id": item.sequence_id, "captured_at": item.captured_at,
                                  "source_duration": item.source_duration,
                                  "translation_ready_at": translation_ready_at,
                                  "translations": translations, "completed": [], "queued_at": time.monotonic()},
                                  key=f'segment:{item.sequence_id}')
                self._translations.ack()
                for lang, text in translations.items():
                    self.on_translation(lang, text)
                    self._report_stage_latency('text', lang, time.monotonic() - item.captured_at)
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
            self._audit.event('translation_failure', getattr(locals().get('item'), 'sequence_id', None), {'error': safe_error(exc)})
            self.on_error(f"Translation paused; pending text retained: {safe_error(exc)}")
            self.on_status("Translation blocked. Stop and check the Activity Log.")
        finally:
            self._speech.finish()

    def _speech_loop(self, speech_queue=None, language=None):
        if speech_queue is None and self._language_speech:
            return self._dispatch_synthesis()
        speech_queue = speech_queue if speech_queue is not None else self._speech
        tts = self._tts if language in (None, 'en') else self._lane_tts.get(language)
        failed = False
        try:
            # Prepare local credential/channel resources before the first text;
            # no inference or token-refresh request is made for warming up.
            if tts is None:
                tts = TextToSpeech(self.config, status_cb=self.on_status)
                if language:
                    self._lane_tts[language] = tts
                if language in (None, 'en'):
                    self._tts = tts
                if isinstance(tts, TextToSpeech) and hasattr(tts, 'prepare'):
                    try:
                        preparation_started = time.monotonic()
                        tts.prepare(language or 'en')
                        self._timing_event('tts_prepared', None, {'language': language or 'en',
                            'started_at': preparation_started, 'completed_at': time.monotonic(), 'paid_requests': 0})
                    except Exception as exc:
                        self.on_status(f'[TTS INIT] preparation deferred: {safe_error(exc)}')
            while True:
                if language in self._ready_speech:
                    with self._synthesis_condition:
                        while self._ready_speech[language].qsize() >= 2 and language not in self._admission_failed:
                            self._synthesis_condition.wait()
                        if language in self._admission_failed:
                            raise RuntimeError('Output admission blocked; synthesis retained')
                item = speech_queue.get()
                if item is None:
                    return
                worker_started_at = time.monotonic()
                captured_at = item.get("captured_at", 0)
                ready_at = None
                queued_at = item.get("queued_at", 0)
                speech_queue_wait = max(0.0, time.monotonic() - queued_at) if queued_at else 0.0
                if captured_at > 0:
                    age = time.monotonic() - captured_at
                    if age > self.config.max_tts_age_seconds:
                        self.on_status(f"Speech backlog: segment {item['sequence_id']} is {age:.1f}s old; preserving chronological playback.")

                if tts is None or (isinstance(tts, TextToSpeech) and
                                         tts.config.voice_settings != self.config.voice_settings):
                    if tts and tts._client:
                        tts._client.transport.close()
                    tts = TextToSpeech(self.config, status_cb=self.on_status)
                    if language:
                        self._lane_tts[language] = tts
                    if language in (None, 'en'):
                        self._tts = tts
                failures = []
                for lang, text in item["translations"].items():
                    if lang in item["completed"] or lang in item.get('deferred', []):
                        continue
                    tts_start = time.monotonic()
                    try:
                        audio = tts.synthesize(text, lang)
                        if not audio:
                            raise RuntimeError('No audio returned')
                    except Exception as exc:
                        failure_timing = getattr(tts, 'last_timing', {})
                        self._timing_event('tts_failed_timing', item['sequence_id'], {
                            'language': lang, 'tts_worker_started_at': worker_started_at,
                            'tts_call_started_at': tts_start, 'failed_at': time.monotonic(),
                            **(failure_timing if isinstance(failure_timing, dict) else {})})
                        if lang == 'yo':
                            # Optional Yoruba cannot halt subsequent English.
                            # Retain an independent durable job, never mark it played.
                            pending = DurableQueue(self.session_dir / 'deferred-speech-yo.sqlite')
                            try:
                                pending.put({**item, 'translations': {'yo': text}, 'completed': [], 'deferred': []},
                                            key=f'segment:{item["sequence_id"]}')
                            finally:
                                pending.close()
                            item.setdefault('deferred', []).append(lang)
                            speech_queue.update_pending(item)
                            self._deferred_yoruba_jobs += 1
                            self._release_synthesis_turn(item['sequence_id'], lang)
                            self.on_error(f'Yoruba audio unavailable; English continues. '
                                          f'Text retained for recovery: {safe_error(exc)}')
                            continue
                        failures.append(lang)
                        self.on_status(f'[TTS] {lang.upper()} failed; retained for recovery: {safe_error(exc)}')
                        continue
                    tts_elapsed = time.monotonic() - tts_start
                    ready_at = time.monotonic()
                    provider_timing = getattr(tts, 'last_timing', {})
                    provider_timing = provider_timing if isinstance(provider_timing, dict) else {}
                    timing = {'translation_ready_at': item.get('translation_ready_at', queued_at),
                              'tts_queue_entry_at': queued_at, 'tts_worker_started_at': worker_started_at,
                              'tts_call_started_at': tts_start, 'audio_ready_at': ready_at,
                              **provider_timing}
                    attempts = provider_timing.get('tts_attempts', [])
                    provider_seconds = sum(max(0.0, a['response_at'] - a['request_started_at']) for a in attempts)
                    timing['tts_provider_request_seconds'] = provider_seconds if attempts else None
                    timing['tts_queue_wait_seconds'] = speech_queue_wait
                    timing['translation_to_tts_start_seconds'] = max(0.0, tts_start - (item.get('translation_ready_at') or tts_start))
                    self._timing_event('tts_timing', item['sequence_id'], {'language': lang, **timing,
                        'translation_to_tts_start': max(0.0, tts_start - (item.get('translation_ready_at') or tts_start)),
                        'queue_wait': speech_queue_wait, 'synthesis_seconds': tts_elapsed})
                    self._report_stage_latency('audio_ready', lang, ready_at - captured_at)
                    self._timing_event('synthesized', item['sequence_id'], {'language': lang, 'duration_seconds': tts_elapsed,
                                                                          'audio_ready_at': ready_at})
                    self.on_status(f"[TIMING] segment={item['sequence_id']} stage=tts language={lang} "
                                   f"queue_wait={speech_queue_wait:.2f}s request={tts_elapsed:.2f}s "
                                   f"provider={provider_timing.get('tts_provider', 'unmeasured')} provider_requests={provider_seconds:.4f}s "
                                   f"capture_to_audio_ready={max(0.0, ready_at - captured_at):.2f}s")
                    if lang in self._ready_speech:
                        # Audio is durable before the text job is acknowledged.
                        # Admission can wait without occupying the voice worker.
                        self._ready_speech[lang].put({**item, 'audio': audio, 'language': lang, 'timing': timing},
                                                    key=f"segment:{item['sequence_id']}")
                    else:
                        self._wait_synthesis_turn(item['sequence_id'], lang)
                        self._players[lang].enqueue(audio, language=lang, captured_at=captured_at,
                            sequence_id=item['sequence_id'], source_duration=item.get('source_duration', 0.0), timing=timing)
                        self._release_synthesis_turn(item['sequence_id'], lang)
                    item["completed"].append(lang)
                    speech_queue.update_pending(item)
                    self.on_status(f"[TTS] {LANGUAGE_NAMES.get(lang, lang)} synthesized: {tts_elapsed:.2f}s (Segment {item['sequence_id']})")
                    if lang not in self._ready_speech:
                        self.on_status(f"[SYNTHESIS ADMISSION] segment={item['sequence_id']} language={lang} "
                                       f"shared_output_order_wait={max(0.0, time.monotonic() - ready_at):.2f}s")
                if failures:
                    raise RuntimeError(f'Audio unavailable for {", ".join(failures)}; other language outputs queued')
                speech_queue.ack()
                if ready_at is None:
                    continue
                total_latency = ready_at - item["captured_at"]
                primary = 'en' if self.settings.english_enabled else self.settings.secondary_language
                if language and language != primary:
                    continue
                avg, p95, worst, count = self.latency_tracker.record(total_latency)
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
            failed = True
            self._audit.event('synthesis_failure', locals().get('item', {}).get('sequence_id'), {'error': safe_error(exc)})
            self.on_error(f"Synthesis paused; pending translations retained: {safe_error(exc)}")

        finally:
            if language and failed:
                with self._synthesis_condition:
                    self._failed_synthesis_lanes.add(language)
                    self._synthesis_condition.notify_all()

    def _admit_speech(self, language, ready_queue):
        try:
            while (item := ready_queue.get()) is not None:
                self._wait_synthesis_turn(item['sequence_id'], language)
                eligible_at = time.monotonic()
                timing = {**item['timing'], 'playback_eligible_at': eligible_at,
                          'shared_output_order_wait_seconds': max(0.0, eligible_at - item['timing']['audio_ready_at'])}
                self._players[language].enqueue(item['audio'], language=language,
                    captured_at=item.get('captured_at', 0), sequence_id=item['sequence_id'],
                    source_duration=item.get('source_duration', 0), timing=timing)
                self.on_status(f"[SYNTHESIS ADMISSION] segment={item['sequence_id']} language={language} "
                               f"shared_output_order_wait={timing['shared_output_order_wait_seconds']:.4f}s")
                self._release_synthesis_turn(item['sequence_id'], language)
                ready_queue.ack()
                with self._synthesis_condition:
                    self._synthesis_condition.notify_all()
        except Exception as exc:
            with self._synthesis_condition:
                self._admission_failed.add(language)
                self._failed_synthesis_lanes.add(language)
                self._synthesis_condition.notify_all()
            self.on_error(f'{language.upper()} output admission paused; ready audio retained: {safe_error(exc)}')

    def _dispatch_synthesis(self):
        """Durable fanout; one CPU/network voice worker per enabled language.

        Shared devices admit ready audio in original batch/language order. Each
        lane can prepare its next sentence while another voice initializes.
        """
        self._ready_speech = {lang: DurableQueue(self.session_dir / f'synthesis-ready-{lang}.sqlite')
                              for lang in self._language_speech}
        admissions = [threading.Thread(target=self._admit_speech, args=(lang, queue),
                                       name=f'admission-{lang}', daemon=True)
                      for lang, queue in self._ready_speech.items()]
        for worker in admissions:
            worker.start()
        workers = [threading.Thread(target=self._speech_loop, args=(queue, lang),
                                   name=f'synthesis-{lang}', daemon=True)
                   for lang, queue in self._language_speech.items()]
        for worker in workers:
            worker.start()
        try:
            while (item := self._speech.get()) is not None:
                with self._synthesis_condition:
                    for lang in item['translations']:
                        if lang not in self._failed_synthesis_lanes:
                            self._synthesis_turns.setdefault(id(self._players[lang]), []).append((item['sequence_id'], lang))
                for lang, text in item['translations'].items():
                    self._language_speech[lang].put({**item, 'translations': {lang: text}},
                                                  key=f"segment:{item['sequence_id']}")
                self._speech.ack()
        except Exception as exc:
            self.on_error(f'Synthesis dispatch paused; durable work retained: {safe_error(exc)}')
        finally:
            for queue in self._language_speech.values():
                queue.finish()
            for worker in workers:
                worker.join()
            for queue in self._ready_speech.values():
                queue.finish()
            for worker in admissions:
                worker.join()

    def _wait_synthesis_turn(self, sequence, language):
        if not self._language_speech:
            return
        with self._synthesis_condition:
            turns = self._synthesis_turns.get(id(self._players[language]), [])
            while turns and turns[0] != (sequence, language):
                if turns[0][1] in self._failed_synthesis_lanes:
                    raise RuntimeError('Earlier speech unavailable on shared output; chronological audio retained')
                self._synthesis_condition.wait()
                turns = self._synthesis_turns.get(id(self._players[language]), [])

    def _release_synthesis_turn(self, sequence, language):
        if not self._language_speech:
            return
        with self._synthesis_condition:
            turns = self._synthesis_turns.get(id(self._players[language]), [])
            if (sequence, language) in turns:
                turns.remove((sequence, language))
            self._synthesis_condition.notify_all()

    def _start_diagnostics(self):
        self._diagnostic_thread = threading.Thread(target=self._diagnostic_loop, name='speech-timing', daemon=True)
        self._diagnostic_thread.start()

    def _stop_diagnostics(self):
        if self._diagnostic_thread is not None:
            self._diagnostic_queue.put(None)
            self._diagnostic_thread.join()
            self._diagnostic_thread = None

    def _diagnostic_loop(self):
        while (job := self._diagnostic_queue.get()) is not None:
            callback, args = job
            try:
                callback(*args)
            except Exception as exc:
                self._diagnostic_error_count += 1
                self.on_error(f'Speech timing diagnostic unavailable; audio queues preserved: {safe_error(exc)}')

    def _post_diagnostic(self, callback, *args):
        if getattr(self, '_diagnostic_thread', None) is None:
            # Isolated/offline callers can use the processing loops directly.
            return callback(*args)
        self._diagnostic_queue.put((callback, args))

    def _timing_event(self, kind, sequence, details):
        self._post_diagnostic(self._audit.event, kind, sequence,
                              {**details, 'observed_at_utc': time.time()})

    def _on_playback_started(self, details):
        self._post_diagnostic(self._record_playback_started, dict(details))

    def _record_playback_started(self, details):
        self._audit.event('playback_started', details.get('sequence_id'), details)
        self._audit.state(details.get('sequence_id'), 'PLAYING', {'language': details['language']})
        if details.get('capture_to_output_start') is not None:
            self._report_stage_latency('playback_start_estimate', details['language'], details['capture_to_output_start'])
        primary = 'en' if self.settings.english_enabled else self.settings.secondary_language
        if details['language'] == primary and details.get('capture_to_output_start') is not None:
            latency = details['capture_to_output_start']
            avg, p95, worst, count = self.playback_latency_tracker.record(latency)
            self.on_latency(latency)
            self.on_status(f'[OUTPUT LATENCY] language={primary} current={latency:.2f}s '
                           f'average={avg:.2f}s p95={p95:.2f}s count={count} '
                           'measurement=first_successful_write_plus_device_latency physical_audio_verified=false')

    def _on_playback_completed(self, details):
        self._post_diagnostic(self._record_playback_completed, dict(details))

    def _record_playback_completed(self, details):
        self._audit.event('playback_completed', details.get('sequence_id'), details)
        self._audit.state(details.get('sequence_id'), 'PLAYBACK_COMPLETED', {'language': details['language']})

    def _report_stage_latency(self, stage, language, elapsed):
        elapsed = max(0.0, elapsed)
        with self._stage_latency_lock:
            key = (stage, language)
            tracker, total, count, worst = self._stage_latencies.get(key, (PipelineLatencyTracker(), 0.0, 0, 0.0))
            total, count, worst = total + elapsed, count + 1, max(worst, elapsed)
            avg, p95, _, rolling_count = tracker.record(elapsed)
            self._stage_latencies[key] = (tracker, total, count, worst)
        self.on_status(f'[STAGE LATENCY] stage={stage} language={language} current={elapsed:.2f}s '
                       f'rolling_average={avg:.2f}s rolling_p95={p95:.2f}s rolling_count={rolling_count} '
                       f'session_average={total / count:.2f}s session_worst={worst:.2f}s session_count={count} '
                       'physical_audio_verified=false')

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
        # Modest upload-only gain; energy cannot distinguish quiet speech from
        # noise. Keep originals and avoid magnifying background noise by 12x.
        gain = min(4.0, target_rms / rms, 0.92 / peak)
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
