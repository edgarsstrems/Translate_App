from __future__ import annotations

import queue
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
        self._translator = Translator(config, self._glossary)
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

    def submit_manual_correction(self, corrected_latvian: str) -> None:
        text = corrected_latvian.strip()
        if not text:
            return
        try:
            self._chunks.put(ProcessingItem(chunk_index=-1, captured_at=time.monotonic(), manual_text=text), timeout=0.25)
            self.on_status("Queued manual correction.")
        except queue.Full:
            self.on_error("Could not queue manual correction because processing is behind.")

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
        threshold = max(self.config.vad_rms_threshold, float(np.percentile(frame_rms, 20)) * 2.5)
        speech_frames = (frame_rms >= threshold) | (frame_peak >= self.config.vad_peak_threshold)
        speech_seconds = float(np.count_nonzero(speech_frames) * frame_size / SAMPLE_RATE)
        speech_ratio = speech_seconds / max(0.001, audio.size / SAMPLE_RATE)
        has_speech = (
            speech_seconds >= self.config.vad_min_speech_seconds
            and speech_ratio >= self.config.vad_min_speech_ratio
            and peak >= self.config.vad_peak_threshold
        )
        if not has_speech:
            return VadResult(False, audio, speech_seconds, speech_ratio, rms, peak, 0.0, 0.0)

        speech_indices = np.flatnonzero(speech_frames)
        padding_frames = int(self.config.vad_padding_seconds * SAMPLE_RATE)
        trim_start = max(0, int(speech_indices[0]) * frame_size - padding_frames)
        trim_end = min(audio.size, (int(speech_indices[-1]) + 1) * frame_size + padding_frames)
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
                    self._process_transcript(item.manual_text, item.captured_at, uncertain=False, manual=True)
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
        self._enqueue_translation(
            TranslationItem(
                captured_at=captured_at,
                transcript=transcript,
                manual=manual,
                uncertain=uncertain,
            )
        )
        latency = time.monotonic() - captured_at
        self.on_latency(latency)
        if uncertain:
            self.on_status(f"Listening. Latest transcript uncertain, latency: {latency:.1f}s")
        else:
            self.on_status(f"Listening. Latest transcript latency: {latency:.1f}s")

    def _looks_like_previous_repeat(self, transcript_key: str) -> bool:
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
        return overlap / len(current_words) >= 0.85

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
            age = time.monotonic() - item.captured_at
            if not item.manual and age > self.config.max_tts_age_seconds:
                self.on_error(f"Dropped stale translation/TTS job ({age:.1f}s old) to stay live.")
                continue
            try:
                self._translate_transcript(item.transcript)
            except Exception as exc:
                self.on_error(f"Skipped one failed translation/TTS job: {exc}")

    def _translate_transcript(self, transcript: str) -> None:
        enabled = []
        if self.settings.english_enabled:
            enabled.append("en")
        if self.settings.russian_enabled:
            enabled.append("ru")
        if not enabled:
            return

        with ThreadPoolExecutor(max_workers=len(enabled)) as executor:
            futures = {
                executor.submit(self._translate_and_speak, transcript, language): language
                for language in enabled
            }
            for future in as_completed(futures):
                language = futures[future]
                try:
                    translated = future.result()
                    if translated:
                        self.on_translation(language, translated)
                except Exception as exc:
                    self.on_error(f"{language.upper()} processing failed for one chunk: {exc}")

    def _translate_and_speak(self, transcript: str, language: str) -> str:
        if self._stop.is_set():
            return ""
        start = time.monotonic()
        translated = self._translator.translate(transcript, language)
        translation_time = time.monotonic() - start
        if not translated:
            return ""
        self.on_status(f"{language.upper()} translation in {translation_time:.1f}s: {translated}")
        tts_start = time.monotonic()
        audio_bytes = self._get_tts().synthesize(translated, language)
        if self._stop.is_set():
            return translated
        self.on_status(f"{language.upper()} TTS generated in {time.monotonic() - tts_start:.1f}s.")
        player = self._players.get(language)
        if player:
            player.enqueue(audio_bytes)
        return translated

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
                self._tts = TextToSpeech(self.config)
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
