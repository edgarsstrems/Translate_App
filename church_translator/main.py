from __future__ import annotations

import subprocess
import sys
import threading
import re
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QProgressBar,
    QPlainTextEdit,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from .audio import AudioDevice, list_audio_devices
from .config import app_data_dir, load_config, load_user_settings, project_root, save_user_settings
from .engine import EngineSettings, TranslationEngine


class UiSignals(QObject):
    status = Signal(str)
    error = Signal(str)
    latency = Signal(float)
    transcript = Signal(str)
    translation = Signal(str, str)
    level = Signal(float, float)
    local_setup_running = Signal(bool)


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Church Sermon Translator")
        icon_path = project_root() / "assets" / "app.ico"
        if icon_path.exists():
            self.setWindowIcon(QIcon(str(icon_path)))
        self.resize(1120, 780)
        self.config = load_config()
        self.devices: list[AudioDevice] = []
        self.engine: TranslationEngine | None = None
        self.local_setup_thread: threading.Thread | None = None
        self.user_settings = load_user_settings()
        self._restoring_settings = False
        self.signals = UiSignals()
        self._build_ui()
        self._wire_signals()
        self.refresh_devices()
        self._restore_user_settings()
        self._set_running(False)

    def _build_ui(self) -> None:
        root = QWidget()
        layout = QVBoxLayout(root)

        device_box = QGroupBox("Audio routing")
        device_layout = QFormLayout(device_box)
        self.input_combo = QComboBox()
        self.english_output_combo = QComboBox()
        self.russian_output_combo = QComboBox()
        device_layout.addRow("Input device", self.input_combo)
        device_layout.addRow("English output", self.english_output_combo)
        device_layout.addRow("Russian output", self.russian_output_combo)

        speech_box = QGroupBox("Speech recognition")
        speech_layout = QFormLayout(speech_box)
        self.speech_backend_combo = QComboBox()
        for label, backend in (
            ("local Whisper", "local"),
            ("OpenAI API", "openai"),
        ):
            self.speech_backend_combo.addItem(label, backend)
        backend_index = self.speech_backend_combo.findData(self.config.speech_recognition_backend)
        if backend_index >= 0:
            self.speech_backend_combo.setCurrentIndex(backend_index)
        self.model_combo = QComboBox()
        for label, model in (
            ("small - fastest usable", "small"),
            ("medium - better, slower", "medium"),
            ("large-v3-turbo - strongest, big download", "large-v3-turbo"),
        ):
            self.model_combo.addItem(label, model)
        model_index = self.model_combo.findData(self.config.whisper_model_size)
        if model_index >= 0:
            self.model_combo.setCurrentIndex(model_index)
        self.quality_combo = QComboBox()
        for label, mode in (
            ("balanced - recommended", "balanced"),
            ("live - fastest", "live"),
            ("accuracy - slower", "accuracy"),
        ):
            self.quality_combo.addItem(label, mode)
        quality_index = self.quality_combo.findData(self.config.whisper_quality_mode)
        if quality_index >= 0:
            self.quality_combo.setCurrentIndex(quality_index)
        self.openai_model_combo = QComboBox()
        for label, model in (
            ("gpt-4o-mini-transcribe - faster", "gpt-4o-mini-transcribe"),
            ("gpt-4o-transcribe - higher quality", "gpt-4o-transcribe"),
            ("whisper-1 - legacy", "whisper-1"),
        ):
            self.openai_model_combo.addItem(label, model)
        openai_model_index = self.openai_model_combo.findData(self.config.openai_transcription_model)
        if openai_model_index >= 0:
            self.openai_model_combo.setCurrentIndex(openai_model_index)
        self.install_local_whisper_button = QPushButton("Install local Whisper")
        speech_layout.addRow("Recognition backend", self.speech_backend_combo)
        speech_layout.addRow("Whisper model", self.model_combo)
        speech_layout.addRow("Recognition mode", self.quality_combo)
        speech_layout.addRow("OpenAI model", self.openai_model_combo)
        speech_layout.addRow("Local setup", self.install_local_whisper_button)

        language_box = QGroupBox("Languages")
        language_layout = QVBoxLayout(language_box)
        self.english_enabled = QCheckBox("English")
        self.russian_enabled = QCheckBox("Russian")
        self.english_volume = QSlider(Qt.Horizontal)
        self.russian_volume = QSlider(Qt.Horizontal)
        self.input_level = QProgressBar()
        self.input_level.setRange(0, 100)
        self.input_level.setValue(0)
        self.input_level.setFormat("Input level %p%")
        self.input_level.setMinimumWidth(260)
        for slider in (self.english_volume, self.russian_volume):
            slider.setRange(0, 100)
            slider.setValue(85)

        language_checks = QHBoxLayout()
        language_checks.addWidget(self.english_enabled)
        language_checks.addWidget(self.russian_enabled)
        language_checks.addStretch(1)
        language_layout.addLayout(language_checks)

        english_volume_layout = QFormLayout()
        english_volume_layout.addRow("English volume", self.english_volume)
        language_layout.addLayout(english_volume_layout)

        russian_volume_layout = QFormLayout()
        russian_volume_layout.addRow("Russian volume", self.russian_volume)
        language_layout.addLayout(russian_volume_layout)
        self.english_enabled.setChecked(True)

        top = QHBoxLayout()
        top.addWidget(device_box, 2)
        top.addWidget(language_box, 1)
        top.addWidget(speech_box, 1)
        layout.addLayout(top)

        controls = QHBoxLayout()
        self.start_button = QPushButton("Start")
        self.stop_button = QPushButton("Stop")
        self.refresh_button = QPushButton("Refresh devices")
        self.clear_text_button = QPushButton("Clear text")
        self.uninstall_button = QPushButton("Uninstall setup")
        self.status_label = QLabel("Idle")
        self.status_label.setMaximumWidth(360)
        self.latency_label = QLabel("Latency: --")
        self.latency_label.setMinimumWidth(90)
        self.setup_progress = QProgressBar()
        self.setup_progress.setRange(0, 0)
        self.setup_progress.setVisible(False)
        self.setup_progress.setMaximumWidth(180)
        controls.addWidget(self.start_button)
        controls.addWidget(self.stop_button)
        controls.addWidget(self.refresh_button)
        controls.addWidget(self.clear_text_button)
        controls.addWidget(self.uninstall_button)
        controls.addStretch(1)
        controls.addWidget(self.setup_progress)
        controls.addWidget(self.status_label)
        controls.addWidget(self.input_level)
        controls.addWidget(self.latency_label)
        layout.addLayout(controls)

        text_grid = QGridLayout()
        self.latvian_text = self._read_only_text()
        self.english_text = self._read_only_text()
        self.russian_text = self._read_only_text()
        text_grid.addWidget(QLabel("Latvian transcription"), 0, 0)
        text_grid.addWidget(QLabel("English translation"), 0, 1)
        text_grid.addWidget(QLabel("Russian translation"), 0, 2)
        text_grid.addWidget(self.latvian_text, 1, 0)
        text_grid.addWidget(self.english_text, 1, 1)
        text_grid.addWidget(self.russian_text, 1, 2)
        layout.addLayout(text_grid, 1)

        correction_box = QGroupBox("Manual correction")
        correction_layout = QHBoxLayout(correction_box)
        self.manual_correction = QLineEdit()
        self.manual_correction.setPlaceholderText("Type corrected Latvian sentence and resend translation/TTS")
        self.send_correction_button = QPushButton("Send correction")
        correction_layout.addWidget(self.manual_correction, 1)
        correction_layout.addWidget(self.send_correction_button)
        layout.addWidget(correction_box)

        layout.addWidget(QLabel("Errors and status log"))
        self.log = self._read_only_text()
        self.log.setMaximumBlockCount(600)
        layout.addWidget(self.log, 1)

        self.setCentralWidget(root)

    def _read_only_text(self) -> QPlainTextEdit:
        text = QPlainTextEdit()
        text.setReadOnly(True)
        text.setMaximumBlockCount(300)
        return text

    def _wire_signals(self) -> None:
        self.start_button.clicked.connect(self.start)
        self.stop_button.clicked.connect(self.stop)
        self.refresh_button.clicked.connect(self.refresh_devices)
        self.clear_text_button.clicked.connect(self.clear_text_windows)
        self.uninstall_button.clicked.connect(self.uninstall_setup)
        self.install_local_whisper_button.clicked.connect(self.install_local_whisper)
        self.send_correction_button.clicked.connect(self.send_manual_correction)
        self.manual_correction.returnPressed.connect(self.send_manual_correction)
        self.speech_backend_combo.currentIndexChanged.connect(self._sync_speech_controls)
        for combo in (
            self.input_combo,
            self.english_output_combo,
            self.russian_output_combo,
            self.speech_backend_combo,
            self.model_combo,
            self.quality_combo,
            self.openai_model_combo,
        ):
            combo.currentIndexChanged.connect(self._save_user_settings)
        for checkbox in (self.english_enabled, self.russian_enabled):
            checkbox.stateChanged.connect(self._save_user_settings)
        for slider in (self.english_volume, self.russian_volume):
            slider.valueChanged.connect(self._save_user_settings)
        self.signals.status.connect(self.set_status)
        self.signals.error.connect(self.log_error)
        self.signals.latency.connect(self.set_latency)
        self.signals.transcript.connect(self.append_transcript)
        self.signals.translation.connect(self.append_translation)
        self.signals.level.connect(self.set_input_level)
        self.signals.local_setup_running.connect(self._set_local_setup_running)
        self._sync_speech_controls()

    def refresh_devices(self) -> None:
        try:
            self.devices = list_audio_devices()
        except Exception as exc:
            self.log_error(f"Could not list audio devices: {exc}")
            return
        saved = self.user_settings.get("devices", {})
        self._fill_combo(
            self.input_combo,
            [d for d in self.devices if d.max_input_channels > 0],
            include_default=False,
            saved=saved.get("input"),
        )
        self._fill_combo(
            self.english_output_combo,
            [d for d in self.devices if d.max_output_channels > 0],
            include_default=True,
            saved=saved.get("english_output"),
        )
        self._fill_combo(
            self.russian_output_combo,
            [d for d in self.devices if d.max_output_channels > 0],
            include_default=True,
            saved=saved.get("russian_output"),
        )
        self.log_status("Audio device list refreshed.")

    def _fill_combo(
        self,
        combo: QComboBox,
        devices: list[AudioDevice],
        include_default: bool,
        saved: dict | None = None,
    ) -> None:
        previous_data = combo.currentData()
        previous_text = combo.currentText()
        combo.blockSignals(True)
        try:
            combo.clear()
            if include_default:
                combo.addItem("Default Windows output", None)
            for device in devices:
                combo.addItem(device.label, device.index)
            self._select_saved_device(combo, devices, saved, include_default)
            if combo.currentIndex() < 0 and previous_text:
                self._select_combo_value(combo, previous_data, previous_text)
        finally:
            combo.blockSignals(False)
        self._save_user_settings()

    def _select_saved_device(
        self,
        combo: QComboBox,
        devices: list[AudioDevice],
        saved: dict | None,
        include_default: bool,
    ) -> None:
        if not saved:
            return
        saved_name = str(saved.get("name") or "")
        saved_index = saved.get("index")
        if include_default and saved_index is None and not saved_name:
            combo.setCurrentIndex(0)
            return
        for row, device in enumerate(devices, start=1 if include_default else 0):
            if device.name == saved_name:
                combo.setCurrentIndex(row)
                return
        if isinstance(saved_index, int):
            for row in range(combo.count()):
                if combo.itemData(row) == saved_index:
                    combo.setCurrentIndex(row)
                    return
        if saved_name:
            combo.addItem(f"{saved_name} (disconnected)", {"missing": True, "name": saved_name})
            combo.setCurrentIndex(combo.count() - 1)

    def _select_combo_value(self, combo: QComboBox, value, fallback_text: str) -> None:
        index = combo.findData(value)
        if index < 0 and isinstance(value, dict):
            name = value.get("name")
            if name:
                index = combo.findText(f"{name} (disconnected)")
        if index < 0 and fallback_text:
            index = combo.findText(fallback_text)
        if index >= 0:
            combo.setCurrentIndex(index)

    def start(self) -> None:
        if self.engine:
            return
        if not self.english_enabled.isChecked() and not self.russian_enabled.isChecked():
            QMessageBox.warning(self, "Select a language", "Enable English, Russian, or both before starting.")
            return
        input_device = self.input_combo.currentData()
        if isinstance(input_device, dict) and input_device.get("missing"):
            QMessageBox.warning(
                self,
                "Input disconnected",
                f"Saved input device is disconnected: {input_device.get('name')}. Connect it or choose another input.",
            )
            return
        if input_device is None:
            QMessageBox.warning(self, "Select input", "Select an audio input device before starting.")
            return
        english_output = self._output_device_or_default(self.english_output_combo, "English")
        russian_output = self._output_device_or_default(self.russian_output_combo, "Russian")
        if self.speech_backend_combo.currentData() == "local" and not self._local_whisper_dependencies_ready():
            QMessageBox.warning(
                self,
                "Install local Whisper",
                "Local Whisper is not installed yet. Click Install local Whisper, or switch Recognition backend to OpenAI API.",
            )
            return

        settings = EngineSettings(
            input_device_index=int(input_device),
            english_enabled=self.english_enabled.isChecked(),
            russian_enabled=self.russian_enabled.isChecked(),
            english_output_device_index=english_output,
            russian_output_device_index=russian_output,
            english_volume_getter=lambda: self.english_volume.value() / 100.0,
            russian_volume_getter=lambda: self.russian_volume.value() / 100.0,
        )
        active_config = self._active_config()
        self._log_configuration_warnings(active_config)
        self.engine = TranslationEngine(
            active_config,
            settings,
            on_status=self.signals.status.emit,
            on_error=self.signals.error.emit,
            on_latency=self.signals.latency.emit,
            on_transcript=self.signals.transcript.emit,
            on_translation=self.signals.translation.emit,
            on_level=self.signals.level.emit,
        )
        try:
            self.engine.start()
            self._save_user_settings()
            self._set_running(True)
        except Exception as exc:
            self.engine = None
            self.log_error(f"Could not start: {exc}")
            self._set_running(False)

    def _output_device_or_default(self, combo: QComboBox, language: str) -> int | None:
        value = combo.currentData()
        if isinstance(value, dict) and value.get("missing"):
            self.log_error(
                f"{language} output device is disconnected: {value.get('name')}. Using default Windows output."
            )
            return None
        return value

    def _restore_user_settings(self) -> None:
        self._restoring_settings = True
        try:
            languages = self.user_settings.get("languages", {})
            if "english_enabled" in languages:
                self.english_enabled.setChecked(bool(languages["english_enabled"]))
            if "russian_enabled" in languages:
                self.russian_enabled.setChecked(bool(languages["russian_enabled"]))
            volumes = self.user_settings.get("volumes", {})
            self.english_volume.setValue(int(volumes.get("english", self.english_volume.value())))
            self.russian_volume.setValue(int(volumes.get("russian", self.russian_volume.value())))
            recognition = self.user_settings.get("recognition", {})
            self._set_combo_data(self.speech_backend_combo, recognition.get("backend"))
            self._set_combo_data(self.model_combo, recognition.get("whisper_model"))
            self._set_combo_data(self.quality_combo, recognition.get("whisper_quality"))
            self._set_combo_data(self.openai_model_combo, recognition.get("openai_model"))
        finally:
            self._restoring_settings = False
        self._sync_speech_controls()

    def _set_combo_data(self, combo: QComboBox, value) -> None:
        if value is None:
            return
        index = combo.findData(value)
        if index >= 0:
            combo.setCurrentIndex(index)

    def _save_user_settings(self) -> None:
        if self._restoring_settings:
            return
        settings = {
            "devices": {
                "input": self._device_setting(self.input_combo),
                "english_output": self._device_setting(self.english_output_combo),
                "russian_output": self._device_setting(self.russian_output_combo),
            },
            "languages": {
                "english_enabled": self.english_enabled.isChecked(),
                "russian_enabled": self.russian_enabled.isChecked(),
            },
            "volumes": {
                "english": self.english_volume.value(),
                "russian": self.russian_volume.value(),
            },
            "recognition": {
                "backend": self.speech_backend_combo.currentData(),
                "whisper_model": self.model_combo.currentData(),
                "whisper_quality": self.quality_combo.currentData(),
                "openai_model": self.openai_model_combo.currentData(),
            },
        }
        self.user_settings = settings
        try:
            save_user_settings(settings)
        except Exception as exc:
            self.log_error(f"Could not save settings: {exc}")

    def _device_setting(self, combo: QComboBox) -> dict:
        value = combo.currentData()
        if isinstance(value, dict) and value.get("missing"):
            return {"index": None, "name": value.get("name", "")}
        if value is None:
            return {"index": None, "name": ""}
        name = ""
        for device in self.devices:
            if device.index == value:
                name = device.name
                break
        return {"index": int(value), "name": name}

    def _active_config(self):
        return replace(
            self.config,
            speech_recognition_backend=str(
                self.speech_backend_combo.currentData() or self.config.speech_recognition_backend
            ),
            openai_transcription_model=str(
                self.openai_model_combo.currentData() or self.config.openai_transcription_model
            ),
            whisper_model_size=str(self.model_combo.currentData() or self.config.whisper_model_size),
            whisper_quality_mode=str(self.quality_combo.currentData() or self.config.whisper_quality_mode),
        )

    def _log_configuration_warnings(self, config=None) -> None:
        config = config or self.config
        if not config.gemini_api_key and not config.google_application_credentials:
            self.log_error(
                "No GEMINI_API_KEY or GOOGLE_APPLICATION_CREDENTIALS found. Translation calls will likely fail."
            )
        if not config.google_application_credentials:
            self.log_error(
                "GOOGLE_APPLICATION_CREDENTIALS is not set. Google Cloud Text-to-Speech may fail unless Application Default Credentials are configured."
            )
        elif not Path(config.google_application_credentials).exists():
            self.log_error(
                f"GOOGLE_APPLICATION_CREDENTIALS file was not found: {config.google_application_credentials}"
            )
        if config.speech_recognition_backend == "openai" and not config.openai_api_key:
            self.log_error("OPENAI_API_KEY is not set. OpenAI transcription will fail.")
        if config.speech_recognition_backend == "openai":
            hop_seconds = config.chunk_seconds - config.chunk_overlap_seconds
            if hop_seconds < 20.0:
                self.log_error(
                    "OpenAI chunks are configured faster than the current 3 RPM limit. "
                    "Use CHUNK_SECONDS=20 and CHUNK_OVERLAP_SECONDS=0.0, or raise the OpenAI rate limit."
                )
            if config.chunk_overlap_seconds > 0.0:
                self.log_error(
                    "OpenAI chunk overlap retranscribes audio and increases API cost. "
                    "Use CHUNK_OVERLAP_SECONDS=0.0 unless boundary accuracy is more important than cost."
                )
            if not config.vad_enabled:
                self.log_error("VAD is disabled; silence/noise chunks may be uploaded and billed.")
        self.log_status(
            f"Runtime tuning: chunk {config.min_chunk_seconds:.1f}-{config.chunk_seconds:.1f}s, "
            f"pause flush {config.early_flush_silence_seconds:.1f}s, "
            f"overlap {config.chunk_overlap_seconds:.1f}s, "
            f"VAD {'on' if config.vad_enabled else 'off'} "
            f"(rms {config.vad_rms_threshold:.4f}, min speech {config.vad_min_speech_seconds:.1f}s), "
            f"queues audio {config.max_audio_queue_size}, translation {config.max_translation_queue_size}."
        )
        if config.speech_recognition_backend == "local" and not self._local_whisper_dependencies_ready():
            self.log_error("Local Whisper is not installed. Click Install local Whisper first, or use OpenAI API.")

    def stop(self) -> None:
        if self.engine:
            self.engine.stop()
            self.engine = None
        self._set_running(False)

    def closeEvent(self, event) -> None:
        self.stop()
        event.accept()

    def _set_running(self, running: bool) -> None:
        self.start_button.setEnabled(not running)
        self.stop_button.setEnabled(running)
        self.input_combo.setEnabled(not running)
        self.speech_backend_combo.setEnabled(not running)
        self.model_combo.setEnabled(not running)
        self.quality_combo.setEnabled(not running)
        self.openai_model_combo.setEnabled(not running)
        self.english_enabled.setEnabled(not running)
        self.russian_enabled.setEnabled(not running)
        self.english_output_combo.setEnabled(not running)
        self.russian_output_combo.setEnabled(not running)
        self.refresh_button.setEnabled(not running)
        self.clear_text_button.setEnabled(True)
        self.uninstall_button.setEnabled(not running)
        self.install_local_whisper_button.setEnabled(not running and not self._local_setup_running())
        self.manual_correction.setEnabled(running)
        self.send_correction_button.setEnabled(running)
        self._sync_speech_controls()

    def _local_setup_running(self) -> bool:
        return self.local_setup_thread is not None and self.local_setup_thread.is_alive()

    def _set_local_setup_running(self, running: bool) -> None:
        self.install_local_whisper_button.setEnabled(not running and self.engine is None)
        self.start_button.setEnabled(not running and self.engine is None)
        if running:
            self.setup_progress.setRange(0, 0)
            self.setup_progress.setVisible(True)
            self.status_label.setText("Installing local Whisper...")
        else:
            self.setup_progress.setVisible(False)
            self.status_label.setText("Idle")

    def _sync_speech_controls(self) -> None:
        use_openai = self.speech_backend_combo.currentData() == "openai"
        running = self.engine is not None
        self.model_combo.setEnabled(not running and not use_openai)
        self.quality_combo.setEnabled(not running and not use_openai)
        self.openai_model_combo.setEnabled(not running and use_openai)
        self.install_local_whisper_button.setEnabled(not running and not self._local_setup_running())

    def _local_whisper_dependencies_ready(self) -> bool:
        try:
            import faster_whisper  # noqa: F401
            import huggingface_hub  # noqa: F401
            import ctranslate2  # noqa: F401
            return True
        except Exception:
            return False

    def install_local_whisper(self) -> None:
        if self.engine:
            QMessageBox.warning(self, "Stop live mode", "Stop the live translator before installing local Whisper.")
            return
        if self._local_setup_running():
            return

        self.signals.local_setup_running.emit(True)

        def worker() -> None:
            app_root = project_root()
            python = Path(sys.executable)
            try:
                if not self._local_whisper_dependencies_ready():
                    if getattr(sys, "frozen", False):
                        raise RuntimeError("This build does not include local Whisper support.")
                    self.signals.status.emit("Installing local Whisper packages...")
                    self._run_setup_command(
                        [
                            str(python),
                            "-m",
                            "pip",
                            "install",
                            "-r",
                            str(app_root / "requirements-local-whisper.txt"),
                        ],
                        app_root,
                    )
                self.signals.status.emit("Downloading/preparing local Whisper model...")
                from .glossary import load_glossary
                from .services import LocalWhisperTranscriber

                glossary = load_glossary(app_root)
                transcriber = LocalWhisperTranscriber(self._active_config(), glossary, self.signals.status.emit)
                transcriber.ensure_model()
                self.signals.status.emit("Local Whisper setup complete.")
            except Exception as exc:
                self.signals.error.emit(f"Local Whisper setup failed: {exc}")
            finally:
                self.signals.local_setup_running.emit(False)

        self.local_setup_thread = threading.Thread(target=worker, name="local-whisper-setup", daemon=True)
        self.local_setup_thread.start()

    def _run_setup_command(self, args: list[str], cwd: Path) -> None:
        process = subprocess.Popen(
            args,
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        assert process.stdout is not None
        for line in process.stdout:
            message = line.strip()
            if message:
                self.signals.status.emit(message)
        exit_code = process.wait()
        if exit_code != 0:
            raise RuntimeError(f"{Path(args[0]).name} exited with code {exit_code}")

    def clear_text_windows(self) -> None:
        self.latvian_text.clear()
        self.english_text.clear()
        self.russian_text.clear()
        self.latency_label.setText("Latency: --")
        self.log_status("Cleared transcription and translation windows.")

    def send_manual_correction(self) -> None:
        text = self.manual_correction.text().strip()
        if not text:
            return
        if not self.engine:
            self.log_error("Start the translator before sending a manual correction.")
            return
        self.engine.submit_manual_correction(text)
        self.manual_correction.clear()

    def uninstall_setup(self) -> None:
        options = self._choose_uninstall_options()
        if not options:
            return

        if self.engine:
            self.stop()

        project_root = Path(__file__).resolve().parents[1]
        script = project_root / "scripts" / "uninstall.ps1"
        args = [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-NoExit",
            "-File",
            str(script),
            "-ProjectRoot",
            str(project_root),
            "-FromApp",
        ]
        if options.get("venv"):
            args.append("-RemoveVenv")
        if options.get("cache"):
            args.append("-RemoveCache")
        if options.get("env"):
            args.append("-RemoveEnv")
        if options.get("scripts"):
            args.append("-RemoveSetupScripts")
        if options.get("python"):
            args.append("-RemovePython")

        try:
            subprocess.Popen(
                args,
                cwd=str(project_root),
                creationflags=subprocess.CREATE_NEW_CONSOLE,
            )
        except Exception as exc:
            self.log_error(f"Could not start uninstall helper: {exc}")
            return

        QApplication.quit()

    def _choose_uninstall_options(self) -> dict[str, bool] | None:
        project_root = Path(__file__).resolve().parents[1]
        items = [
            ("venv", "Virtual environment and installed Python packages", project_root / ".venv"),
            ("cache", "Downloaded Whisper models, app cache, and debug audio", app_data_dir()),
            ("env", ".env API/settings file", project_root / ".env"),
            ("scripts", "Setup helper scripts: run/uninstall batch files and scripts folder", project_root / "scripts"),
            ("python", "Python 3.11 from Windows/winget", None),
        ]

        dialog = QDialog(self)
        dialog.setWindowTitle("Choose what to uninstall")
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel("Select what should be removed. Python may be shared with other apps."))
        checks: dict[str, QCheckBox] = {}
        for key, label, path in items:
            exists = True if path is None else path.exists()
            suffix = "installed/found" if exists else "not found"
            check = QCheckBox(f"{label} ({suffix})")
            check.setChecked(key in {"venv", "cache"} and exists)
            check.setEnabled(exists)
            checks[key] = check
            layout.addWidget(check)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)

        if dialog.exec() != QDialog.Accepted:
            return None
        selected = {key: check.isChecked() for key, check in checks.items()}
        if not any(selected.values()):
            return None
        if selected.get("python"):
            answer = QMessageBox.warning(
                self,
                "Uninstall Python?",
                "Python may be used by other programs. Continue only if this app is the only reason Python 3.11 is installed.",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                selected["python"] = False
        return selected

    def set_status(self, message: str) -> None:
        self.status_label.setText(message)
        percent_match = re.search(r"(\d+(?:\.\d+)?)%", message)
        setup_active = (
            message.startswith("Preparing Whisper")
            or message.startswith("Preparing large Whisper")
            or message.startswith("Checking Whisper")
            or " Whisper " in message and "%" in message
            or message.startswith("Loading Whisper")
            or message.startswith("Installing local Whisper")
            or message.startswith("Downloading/preparing local Whisper")
        )
        setup_done = (
            "Whisper model ready" in message
            or "Local Whisper setup complete" in message
            or "Startup error" in message
            or message == "Stopped."
        )
        if percent_match and "Whisper" in message:
            self.setup_progress.setRange(0, 100)
            self.setup_progress.setValue(min(100, int(float(percent_match.group(1)))))
            self.setup_progress.setVisible(True)
        elif setup_active:
            self.setup_progress.setRange(0, 0)
            self.setup_progress.setVisible(True)
        elif setup_done:
            self.setup_progress.setVisible(False)
        self.log_status(message)

    def set_latency(self, latency: float) -> None:
        self.latency_label.setText(f"Latency: {latency:.1f}s")

    def set_input_level(self, rms: float, peak: float) -> None:
        level = min(100, int(max(rms * 500, peak * 120)))
        self.input_level.setValue(level)
        self.input_level.setFormat(f"Input level {level}%  rms {rms:.4f}  peak {peak:.3f}")

    def append_transcript(self, text: str) -> None:
        self.latvian_text.appendPlainText(text)

    def append_translation(self, language: str, text: str) -> None:
        if language == "en":
            self.english_text.appendPlainText(text)
        elif language == "ru":
            self.russian_text.appendPlainText(text)

    def log_error(self, message: str) -> None:
        self._append_log(f"ERROR: {message}")

    def log_status(self, message: str) -> None:
        self._append_log(message)

    def _append_log(self, message: str) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log.appendPlainText(f"[{timestamp}] {message}")


def main() -> int:
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
