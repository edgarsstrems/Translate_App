from __future__ import annotations

import os
import html
import re
import shutil
import subprocess
import sys
import threading
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QObject, Qt, Signal, QUrl, QTimer, QByteArray
from PySide6.QtGui import QDesktopServices, QIcon, QFont, QPixmap, QImage, QPainter, QPen, QColor
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QProgressBar,
    QPlainTextEdit,
    QSlider,
    QSizePolicy,
    QScrollArea,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .audio import AudioDevice, list_audio_devices
from .languages import OUTPUT_LANGUAGES, LANGUAGE_NAMES, default_voice
from .language_selector import LanguageSelector
from .config import (
    GEMINI_MODELS,
    live_gemini_model,
    app_data_dir,
    inspect_service_account_file,
    load_config,
    load_user_settings,
    project_root,
    save_user_settings,
    atomic_write_text,
    SPEED_MODE_BALANCED,
    SPEED_MODE_LOW_LATENCY,
    SPEED_MODE_DYNAMIC,
    SPEED_MODE_CUSTOM,
    SPEED_PROFILES,
    get_speed_profile_settings,
)
from .engine import EngineSettings, TranslationEngine
from .reliability import safe_error
from .sermon_context import SermonContextManager
from .session_manager import SessionManager, SessionRecord, TranscriptSegment
from .summarizer import SermonSummarizer
from .dialogs import SermonContextDialog, TranscriptExportDialog, AISummaryDialog, SessionHistoryDialog
from .ui_compat import apply_dark_style, append_live_text


def ensure_check_icon() -> Path:
    icon_path = project_root() / "assets" / "check.png"
    if not icon_path.exists():
        try:
            (project_root() / "assets").mkdir(parents=True, exist_ok=True)
            img = QImage(16, 16, QImage.Format_ARGB32)
            img.fill(QColor(0, 0, 0, 0))
            painter = QPainter(img)
            painter.setRenderHint(QPainter.Antialiasing)
            pen = QPen(QColor("#FFFFFF"), 2.2)
            painter.setPen(pen)
            painter.drawLine(3, 8, 6, 12)
            painter.drawLine(6, 12, 13, 4)
            painter.end()
            img.save(str(icon_path))
        except Exception:
            pass
    return icon_path


DARK_TEAL_DASHBOARD_QSS = """
QMainWindow {
    background-color: #0B0F17;
}
QWidget {
    font-family: 'Segoe UI', system-ui, -apple-system, sans-serif;
    font-size: 13px;
    color: #F8FAFC;
}
QTabWidget::pane {
    border: 1px solid #1E293B;
    border-radius: 12px;
    background-color: #111827;
    top: -1px;
}
QTabBar::tab {
    background-color: #131E2B;
    color: #94A3B8;
    padding: 7px 18px;
    border: 1px solid #1E2D3D;
    border-bottom: none;
    border-top-left-radius: 8px;
    border-top-right-radius: 8px;
    margin-right: 4px;
    font-weight: 600;
    font-size: 13px;
}
QTabBar::tab:selected {
    background-color: #0D9488;
    color: #FFFFFF;
    border: 1px solid #14B8A6;
    border-bottom: none;
    font-weight: bold;
}
QTabBar::tab:hover:!selected {
    background-color: #1E293B;
    color: #F8FAFC;
}
QGroupBox {
    background-color: #111827;
    border: 1px solid #1E293B;
    border-radius: 12px;
    margin-top: 10px;
    font-weight: 600;
    padding-top: 14px;
}
QGroupBox::title {
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 12px;
    padding: 0 6px;
    color: #22D3EE;
}
QLabel {
    color: #E2E8F0;
}
QLineEdit, QComboBox, QPlainTextEdit {
    background-color: #070C12;
    border: 1px solid #1E293B;
    border-radius: 8px;
    padding: 8px 12px;
    color: #F8FAFC;
    selection-background-color: #0D9488;
}
QLineEdit:focus, QComboBox:focus, QPlainTextEdit:focus {
    border: 1px solid #14B8A6;
}
QComboBox::drop-down {
    border: none;
    width: 24px;
}
QComboBox QAbstractItemView {
    background-color: #111827;
    border: 1px solid #1E293B;
    color: #F8FAFC;
    selection-background-color: #0D9488;
}
QPushButton {
    background-color: #1E293B;
    color: #F8FAFC;
    border: 1px solid #334155;
    border-radius: 6px;
    padding: 4px 12px;
    font-weight: 600;
    font-size: 12px;
    height: 30px;
}
QPushButton:hover {
    background-color: #334155;
    border-color: #475569;
}
QPushButton:pressed {
    background-color: #0F172A;
}
QPushButton:disabled {
    background-color: #111A24;
    color: #475569;
    border-color: #1E2D3D;
}
QPushButton#startButton:enabled {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #0D9488, stop:1 #14B8A6);
    color: #FFFFFF;
    border: 1px solid #14B8A6;
    font-size: 12px;
    font-weight: bold;
    border-radius: 6px;
    padding: 4px 12px;
    height: 30px;
}
QPushButton#startButton:enabled:hover {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #0F766E, stop:1 #0D9488);
}
QPushButton#startButton:disabled {
    background-color: #0F2E30;
    color: #0D9488;
    border: 1px solid #14B8A6;
    font-size: 12px;
    font-weight: bold;
    border-radius: 6px;
    padding: 4px 12px;
    height: 30px;
}
QPushButton#pauseButton:enabled {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #D97706, stop:1 #F59E0B);
    color: #FFFFFF;
    border: 1px solid #F59E0B;
    font-size: 12px;
    font-weight: bold;
    border-radius: 6px;
    padding: 4px 12px;
    height: 30px;
}
QPushButton#pauseButton:enabled:hover {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #B45309, stop:1 #D97706);
}
QPushButton#pauseButton:disabled {
    background-color: #111A24;
    color: #475569;
    border: 1px solid #1E2D3D;
    font-size: 12px;
    font-weight: bold;
    border-radius: 6px;
    padding: 4px 12px;
    height: 30px;
}
QPushButton#stopButton:enabled {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #E11D48, stop:1 #F43F5E);
    color: #FFFFFF;
    border: 1px solid #F43F5E;
    font-size: 12px;
    font-weight: bold;
    border-radius: 6px;
    padding: 4px 12px;
    height: 30px;
}
QPushButton#stopButton:enabled:hover {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #BE123C, stop:1 #E11D48);
}
QPushButton#stopButton:disabled {
    background-color: #111A24;
    color: #475569;
    border: 1px solid #1E2D3D;
    font-size: 12px;
    font-weight: bold;
    border-radius: 6px;
    padding: 4px 12px;
    height: 30px;
}
QSlider::groove:horizontal {
    height: 6px;
    background: #1E293B;
    border-radius: 3px;
}
QSlider::sub-page:horizontal {
    background: #0D9488;
    border-radius: 3px;
}
QSlider::handle:horizontal {
    background: #F8FAFC;
    border: 2px solid #14B8A6;
    width: 16px;
    margin-top: -5px;
    margin-bottom: -5px;
    border-radius: 8px;
}
QCheckBox {
    spacing: 8px;
    color: #F8FAFC;
    font-weight: 500;
}
QCheckBox::indicator {
    width: 18px;
    height: 18px;
    border-radius: 4px;
    border: 1px solid #334155;
    background: #070C12;
}
QCheckBox::indicator:checked {
    background: #0D9488;
    border-color: #14B8A6;
    image: url("assets/check.png");
}
"""


class UiSignals(QObject):
    session_update = Signal(str, str, object)
    stopped = Signal()
    status = Signal(str)
    error = Signal(str)
    latency = Signal(float)
    transcript = Signal(str)
    translation = Signal(str, str)
    level = Signal(float, float)
    local_setup_running = Signal(bool)
    provider_health = Signal(str)
    speed_mode_changed = Signal(str)


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        apply_dark_style(QApplication.instance())
        self.setWindowTitle("Church Sermon Translator")
        icon_path = project_root() / "assets" / "app.ico"
        if icon_path.exists():
            self.setWindowIcon(QIcon(str(icon_path)))
        self.resize(1280, 860)
        self.setMinimumSize(640, 480)
        ensure_check_icon()
        self.setStyleSheet(DARK_TEAL_DASHBOARD_QSS)

        self.config = load_config()
        self._voice_settings = self.config.voice_settings
        self._paid_fallback_authorized = self.config.openai_fallback_enabled
        self.context_manager = SermonContextManager()
        self._context_staged_for_next_session = False
        self.session_manager = SessionManager()
        self.summarizer = SermonSummarizer(
            self.config,
            status_cb=lambda msg: self.signals.status.emit(msg),
            get_working_model=self._get_active_gemini_working_model,
            get_last_call_time=self._get_last_gemini_call_time,
            record_call_time=self._record_gemini_call_time,
        )
        self.devices: list[AudioDevice] = []
        self.engine: TranslationEngine | None = None
        self._stopping = False
        self._is_paused = False
        self._close_after_stop = False
        self._volumes = {"en": 0.85, "ru": 0.85}
        self._latency_samples: list[float] = []
        self._activity_log_count = 0
        self.local_setup_thread: threading.Thread | None = None
        self.user_settings = load_user_settings()
        self._restoring_settings = True
        try:
            self.signals = UiSignals()
            self.signals.session_update.connect(self._receive_session_update)
            self._build_ui()
            self._wire_signals()
            self.refresh_devices()
            self._restore_user_settings()
            self._check_crash_recovery()
        finally:
            self._restoring_settings = False
        self._set_running(False)

    def _build_ui(self) -> None:
        main_widget = QWidget()
        content_layout = QVBoxLayout(main_widget)
        content_layout.setContentsMargins(12, 12, 12, 12)
        content_layout.setSpacing(8)

        # Main Tabs Container (Positioned at Very Top)
        self.tabs = QTabWidget()

        # Keep the original compact status group beside the navigation tabs.
        self.status_row = QWidget()
        corner_layout = QHBoxLayout(self.status_row)
        corner_layout.setContentsMargins(0, 0, 8, 4)
        corner_layout.setSpacing(8)

        self.status_pill = QLabel("○ IDLE")
        self.status_pill.setStyleSheet("color: #94A3B8; font-size: 12px; font-weight: bold;")

        self.latency_label = QLabel("⚡ Latency: --")
        self.latency_label.setToolTip("Oldest captured audio to synthesized speech queued for output. "
                                     "The Activity Log separates STT, translation, synthesis and playback waiting.")
        self.latency_label.setStyleSheet("color: #22D3EE; font-size: 12px; font-weight: bold;")

        self.input_level = QLabel("🎤 Mic: 0%")
        self.input_level.setStyleSheet("color: #60A5FA; font-size: 12px; font-weight: bold;")

        self.speed_badge = QLabel("⚡ B")
        self.speed_badge.setStyleSheet("color: #34D399; font-size: 12px; font-weight: bold;")

        sep1 = QLabel("|")
        sep1.setStyleSheet("color: #334155; font-weight: bold;")
        sep_speed = QLabel("|")
        sep_speed.setStyleSheet("color: #334155; font-weight: bold;")
        sep2 = QLabel("|")
        sep2.setStyleSheet("color: #334155; font-weight: bold;")

        corner_layout.addWidget(self.status_pill)
        corner_layout.addWidget(sep1)
        corner_layout.addWidget(self.speed_badge)
        corner_layout.addWidget(sep_speed)
        corner_layout.addWidget(self.latency_label)
        corner_layout.addWidget(sep2)
        corner_layout.addWidget(self.input_level)

        self.tabs.setCornerWidget(self.status_row, Qt.TopRightCorner)
        self.tabs.tabBar().setExpanding(False)
        self.tabs.tabBar().setElideMode(Qt.ElideNone)
        self.tabs.setUsesScrollButtons(True)

        # -------------------------------------------------------------
        # Tab 1: General (Dashboard with Controls & Hero Cards)
        # -------------------------------------------------------------
        general_tab = QWidget()
        general_layout = QVBoxLayout(general_tab)
        general_layout.setContentsMargins(12, 14, 12, 12)
        general_layout.setSpacing(10)


        # Compact Controls Bar (Button height = 30px, right by text boxes)
        controls = QHBoxLayout()
        controls.setSpacing(8)

        self.start_button = QPushButton("▶ Start Translation")
        self.start_button.setObjectName("startButton")
        self.start_button.setFixedHeight(30)

        self.pause_button = QPushButton("⏸ Pause")
        self.pause_button.setObjectName("pauseButton")
        self.pause_button.setFixedHeight(30)
        self.pause_button.setEnabled(False)

        self.stop_button = QPushButton("⏹ End Session")
        self.stop_button.setObjectName("stopButton")
        self.stop_button.setFixedHeight(30)

        self.context_button = QPushButton("📖 Sermon Context")
        self.context_button.setFixedHeight(30)

        self.export_button = QPushButton("📥 Export Transcript")
        self.export_button.setFixedHeight(30)

        self.summary_button = QPushButton("🧠 AI Summary")
        self.summary_button.setFixedHeight(30)

        self.history_button = QPushButton("📜 History")
        self.history_button.setFixedHeight(30)

        self.refresh_button = QPushButton("🔄 Devices")
        self.refresh_button.setFixedHeight(30)

        self.clear_text_button = QPushButton("🧹 Clear Text")
        self.clear_text_button.setFixedHeight(30)

        self.status_label = QLabel("Idle")
        self.status_label.setWordWrap(True)
        # Provider/status messages must not resize the transcript cards on every
        # log entry. Reserve two text lines in the existing controls row.
        self.status_label.setFixedWidth(150)
        self.status_label.setFixedHeight(self.status_label.fontMetrics().lineSpacing() * 2 + 8)
        self.status_label.setStyleSheet("color: #94A3B8; font-size: 11px; font-style: italic;")

        self.setup_progress = QProgressBar()
        self.setup_progress.setRange(0, 0)
        self.setup_progress.setVisible(False)
        self.setup_progress.setMaximumWidth(100)

        controls.addWidget(self.start_button)
        controls.addWidget(self.pause_button)
        controls.addWidget(self.stop_button)
        controls.addWidget(self.context_button)
        controls.addWidget(self.export_button)
        controls.addWidget(self.summary_button)
        controls.addWidget(self.history_button)
        controls.addWidget(self.refresh_button)
        controls.addWidget(self.clear_text_button)
        controls.addStretch(1)
        controls.addWidget(self.setup_progress)
        controls.addWidget(self.status_label)

        general_layout.addLayout(controls)

        # Three compact translation cards. Their text editors take all remaining
        # vertical space; headers and secondary-language controls stay compact.
        text_grid = QGridLayout()
        text_grid.setSpacing(14)

        self.latvian_text = self._read_only_text()
        self.english_text = self._read_only_text()
        self.russian_text = self._read_only_text("Translation will appear here...")
        self.secondary_language_combo = LanguageSelector(self)
        self.secondary_language_combo.setCurrentIndex(self.secondary_language_combo.findData('ru'))
        self.secondary_language_combo.setMinimumWidth(0)
        self.secondary_language_combo.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.secondary_language_combo.ensurePolished()
        self.secondary_language_combo.setFixedHeight(self.secondary_language_combo.sizeHint().height())

        latvian_box = self._create_hero_card(
            "LV", "Latvian Speech", "Source (Transcribed)", self.latvian_text,
            "#0D9488", "#14B8A6", "🎙")
        english_box = self._create_hero_card(
            "EN", "English Audio Translation", "", self.english_text,
            "#059669", "#10B981", "🔊")
        russian_box = self._create_hero_card(
            "🌐", "Selectable Audio Translation", "", self.russian_text,
            "#D97706", "#F59E0B", "🔊", self.secondary_language_combo)
        self.secondary_dashboard_toggle = QCheckBox("Enable selected output")
        self.secondary_dashboard_toggle.ensurePolished()
        self.secondary_dashboard_toggle.setFixedHeight(self.secondary_dashboard_toggle.sizeHint().height())
        russian_box.layout().addWidget(self.secondary_dashboard_toggle)
        # Reserve the same single compact control row so all three live text
        # viewports remain exactly aligned while only the selectable card uses it.
        for box in (latvian_box, english_box):
            alignment_row = QWidget()
            alignment_row.setFixedHeight(self.secondary_dashboard_toggle.height())
            box.layout().addWidget(alignment_row)

        text_grid.addWidget(latvian_box, 0, 0)
        text_grid.addWidget(english_box, 0, 1)
        text_grid.addWidget(russian_box, 0, 2)
        for column, box in enumerate((latvian_box, english_box, russian_box)):
            text_grid.setColumnStretch(column, 1)
            text_grid.setColumnMinimumWidth(column, 0)
            box.setMinimumWidth(0)
            box.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Expanding)
        general_layout.addLayout(text_grid, 1)

        # Activity log stays live while its space-saving drawer is collapsed.
        self.log_box = QGroupBox()
        self.log_box.setStyleSheet("""
            QGroupBox {
                background-color: #090F15;
                border: 1px solid #16222F;
                border-radius: 9px;
                margin-top: 0px;
                padding: 0px;
            }
        """)
        log_layout = QVBoxLayout(self.log_box)
        log_layout.setContentsMargins(8, 4, 8, 4)
        log_layout.setSpacing(4)

        log_header_box = QHBoxLayout()
        self.activity_log_toggle = QPushButton()
        self.activity_log_toggle.setCheckable(True)
        self.activity_log_toggle.setChecked(False)
        self.activity_log_toggle.setFixedHeight(28)
        self.activity_log_toggle.setStyleSheet("""
            QPushButton {
                background: transparent;
                border: none;
                color: #CBD5E1;
                font-size: 12px;
                font-weight: 600;
                padding: 2px 6px;
                text-align: left;
            }
            QPushButton:hover { color: #F8FAFC; }
        """)
        self._update_activity_log_title()
        
        self.clear_log_btn = QPushButton("🗑 Clear Log")
        self.clear_log_btn.setFixedSize(90, 26)
        self.clear_log_btn.setStyleSheet("""
            QPushButton {
                background-color: #16222F;
                color: #94A3B8;
                border: 1px solid #26374A;
                border-radius: 6px;
                font-size: 11px;
                padding: 2px 8px;
            }
            QPushButton:hover {
                background-color: #1E2D3D;
                color: #F8FAFC;
            }
        """)
        self.clear_log_btn.clicked.connect(self.clear_activity_log)

        log_header_box.addWidget(self.activity_log_toggle, 1)
        log_header_box.addStretch(1)
        log_header_box.addWidget(self.clear_log_btn)
        log_layout.addLayout(log_header_box)

        self.log = self._read_only_text()
        self.log.setMaximumBlockCount(600)
        self.log.setMaximumHeight(130)
        self.log.setVisible(False)
        self.clear_log_btn.setVisible(False)
        log_layout.addWidget(self.log)
        self.activity_log_toggle.toggled.connect(self._set_activity_log_expanded)
        self._set_activity_log_expanded(False)

        general_layout.addWidget(self.log_box)
        self.tabs.addTab(general_tab, "📊 Dashboard")

        # -------------------------------------------------------------
        # Tab 2: Audio Routing
        # -------------------------------------------------------------
        audio_tab = QWidget()
        audio_layout = QFormLayout(audio_tab)
        audio_layout.setContentsMargins(24, 24, 24, 24)
        audio_layout.setSpacing(16)
        self.input_combo = QComboBox()
        self.english_output_combo = QComboBox()
        self.russian_output_combo = QComboBox()
        audio_layout.addRow("🎤 Input Device (Microphone):", self.input_combo)
        audio_layout.addRow("🔊 English Speaker Output:", self.english_output_combo)
        audio_layout.addRow("🔊 Selectable Language Speaker Output:", self.russian_output_combo)

        dynamic_note = QLabel("⚡ Dynamic capture prefers natural pauses, with an eight-second maximum. Other profiles use their configured limits.")
        dynamic_note.setWordWrap(True)
        dynamic_note.setStyleSheet("color: #0D9488; font-size: 12px; font-style: italic; margin-top: 8px;")
        audio_layout.addRow("", dynamic_note)
        self.tabs.addTab(audio_tab, "🎙️ Audio Routing")

        # -------------------------------------------------------------
        # Tab 3: Speech & AI Translation Settings
        # -------------------------------------------------------------
        ai_tab = QWidget()
        ai_layout = QFormLayout(ai_tab)
        ai_layout.setContentsMargins(24, 24, 24, 24)
        ai_layout.setSpacing(14)

        # 1. Gemini Translation
        self.gemini_model_combo = QComboBox()
        for key, info in GEMINI_MODELS.items():
            self.gemini_model_combo.addItem(info["label"], key)
        if self.config.gemini_model and self.config.gemini_model not in GEMINI_MODELS:
            self.gemini_model_combo.addItem(f"{self.config.gemini_model} (Custom)", self.config.gemini_model)
        gemini_index = self.gemini_model_combo.findData(self.config.gemini_model)
        if gemini_index >= 0:
            self.gemini_model_combo.setCurrentIndex(gemini_index)

        self.set_gemini_key_button = QPushButton("🔑 Set Gemini API Key")
        self.get_gemini_key_button = QPushButton("🌐 Get Key")
        self.gemini_status_label = QLabel()

        gemini_row = QHBoxLayout()
        gemini_row.addWidget(self.gemini_model_combo, 1)
        gemini_row.addWidget(self.set_gemini_key_button)
        gemini_row.addWidget(self.get_gemini_key_button)

        # 2. Speech Recognition (STT)
        self.speech_backend_combo = QComboBox()
        for label, backend in (
            ("OpenAI API (Cloud, Fast)", "openai"),
            ("Local Whisper (Offline, Free)", "local"),
        ):
            self.speech_backend_combo.addItem(label, backend)
        backend_index = self.speech_backend_combo.findData(self.config.speech_recognition_backend)
        if backend_index >= 0:
            self.speech_backend_combo.setCurrentIndex(backend_index)

        self.model_combo = QComboBox()
        for label, model in (
            ("large-v3-turbo - recommended: best accuracy & speed", "large-v3-turbo"),
            ("medium - high accuracy, balanced", "medium"),
            ("large-v3 - maximum accuracy, heavier download", "large-v3"),
            ("small - fast, lower accuracy", "small"),
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
            ("whisper-1 - OpenAI Whisper Large V2", "whisper-1"),
            ("gpt-4o-mini-transcribe - fast LLM transcriber", "gpt-4o-mini-transcribe"),
            ("gpt-4o-transcribe - recommended for Latvian", "gpt-4o-transcribe"),
        ):
            self.openai_model_combo.addItem(label, model)
        openai_model_index = self.openai_model_combo.findData(self.config.openai_transcription_model)
        if openai_model_index >= 0:
            self.openai_model_combo.setCurrentIndex(openai_model_index)

        self.set_openai_key_button = QPushButton("🔑 Set OpenAI API Key")
        self.get_openai_key_button = QPushButton("🌐 Get Key")
        self.openai_status_label = QLabel()
        self.install_local_whisper_button = QPushButton("📦 Install Local Whisper Dependencies")

        openai_row = QHBoxLayout()
        openai_row.addWidget(self.openai_model_combo, 1)
        openai_row.addWidget(self.set_openai_key_button)
        openai_row.addWidget(self.get_openai_key_button)

        # 3. Google Cloud & TTS Credentials
        self.tts_status_label = QLabel()
        self.import_google_creds_button = QPushButton("📁 Import Service Account JSON...")
        self.open_creds_folder_button = QPushButton("📂 Open Credentials Folder")
        self.google_tts_guide_button = QPushButton("🔗 How to Get Google TTS Credentials")
        self.clear_google_creds_button = QPushButton("🗑️ Clear")

        tts_actions_row = QHBoxLayout()
        tts_actions_row.addWidget(self.import_google_creds_button)
        tts_actions_row.addWidget(self.open_creds_folder_button)
        tts_actions_row.addWidget(self.google_tts_guide_button)
        tts_actions_row.addWidget(self.clear_google_creds_button)
        tts_actions_row.addStretch(1)

        ai_layout.addRow("✨ Gemini Model:", gemini_row)
        ai_layout.addRow("🔑 Gemini Key Status:", self.gemini_status_label)
        ai_layout.addRow("🎙️ Speech Recognition Backend:", self.speech_backend_combo)
        ai_layout.addRow("☁️ OpenAI Model:", openai_row)
        ai_layout.addRow("🔑 OpenAI Key Status:", self.openai_status_label)
        ai_layout.addRow("🧠 Local Whisper Model Size:", self.model_combo)
        ai_layout.addRow("⚙️ Recognition Mode:", self.quality_combo)
        ai_layout.addRow("🔧 Local Setup:", self.install_local_whisper_button)
        ai_layout.addRow("🔊 Google TTS Status:", self.tts_status_label)
        ai_layout.addRow("📁 Google Credentials:", tts_actions_row)

        # 4. Translation Reliability & Emergency Fallback Section
        reliability_group = QGroupBox("🛡️ Translation Reliability & Emergency Fallback")
        reliability_layout = QFormLayout(reliability_group)
        reliability_layout.setContentsMargins(14, 14, 14, 14)
        reliability_layout.setSpacing(10)

        self.primary_provider_label = QLabel("✨ Primary Provider: Google Gemini (free-tier eligible)")
        self.primary_provider_label.setStyleSheet("color: #34D399; font-weight: bold;")
        reliability_layout.addRow("Primary:", self.primary_provider_label)

        self.openai_fallback_toggle = QCheckBox("Allow automatic OpenAI translation fallback (paid API)")
        self.openai_fallback_toggle.setChecked(self.config.openai_fallback_enabled)
        reliability_layout.addRow("Emergency Fallback:", self.openai_fallback_toggle)

        fallback_model_row = QHBoxLayout()
        self.openai_fallback_model_combo = QComboBox()
        for label, model in (
            ("gpt-4o-mini (Recommended: fast, ultra-low cost, high quality)", "gpt-4o-mini"),
            ("gpt-5-nano (Low-cost fallback, if available on account)", "gpt-5-nano"),
            ("gpt-4o (Standard flagship model)", "gpt-4o"),
        ):
            self.openai_fallback_model_combo.addItem(label, model)
        fb_idx = self.openai_fallback_model_combo.findData(self.config.openai_fallback_model)
        if fb_idx >= 0:
            self.openai_fallback_model_combo.setCurrentIndex(fb_idx)

        self.auto_return_toggle = QCheckBox("Auto-Return to Gemini as soon as healthy")
        self.auto_return_toggle.setChecked(self.config.auto_return_to_gemini)
        fallback_model_row.addWidget(self.openai_fallback_model_combo, 1)
        fallback_model_row.addWidget(self.auto_return_toggle)
        reliability_layout.addRow("Fallback Model:", fallback_model_row)

        self.test_providers_button = QPushButton("🩺 Test All Providers (Gemini, OpenAI, Google TTS)")
        self.test_providers_button.clicked.connect(self.test_providers_health)
        self.provider_health_label = QLabel("All providers ready.")
        self.provider_health_label.setStyleSheet("color: #94A3B8; font-size: 11px;")
        test_row = QHBoxLayout()
        test_row.addWidget(self.test_providers_button)
        test_row.addWidget(self.provider_health_label, 1)
        reliability_layout.addRow("Health Diagnostic:", test_row)

        self.fallback_cost_label = QLabel("Est. Fallback Cost: $0.0000 | Fallback Requests: 0 | Tokens: 0")
        self.fallback_cost_label.setStyleSheet("color: #22D3EE; font-size: 11px; font-weight: bold;")
        reliability_layout.addRow("Fallback Cost Monitor:", self.fallback_cost_label)

        ai_layout.addRow("", reliability_group)

        pricing_note = QLabel(
            "💡 <b>Service usage:</b> Free-tier-eligible Gemini models are preferred. Your account tier determines billing. "
            "OpenAI translation is only invoked in emergency outages if enabled, automatically returning to Gemini immediately upon recovery."
        )
        pricing_note.setWordWrap(True)
        pricing_note.setStyleSheet("color: #94A3B8; font-size: 11px; padding: 8px 12px; background-color: #0B1320; border-radius: 6px; border: 1px solid #1E293B;")
        ai_layout.addRow("", pricing_note)

        self.tabs.addTab(ai_tab, "🤖 AI Models & Keys")

        # -------------------------------------------------------------
        # Tab 4: Languages & Audio Output Volume
        # -------------------------------------------------------------
        lang_tab = QWidget()
        lang_layout = QFormLayout(lang_tab)
        lang_layout.setContentsMargins(24, 24, 24, 24)
        lang_layout.setSpacing(16)

        self.english_enabled = QCheckBox("Enable English Translation & Speech Output")
        self.russian_enabled = QCheckBox("Enable Russian Translation & Speech Output")
        self.english_enabled.setChecked(True)

        self.english_volume = QSlider(Qt.Horizontal)
        self.english_vol_label = QLabel("85%")
        self.english_vol_label.setFixedWidth(40)
        self.english_vol_label.setStyleSheet("font-weight: bold; color: #22D3EE;")

        self.russian_volume = QSlider(Qt.Horizontal)
        self.russian_vol_label = QLabel("85%")
        self.russian_vol_label.setFixedWidth(40)
        self.russian_vol_label.setStyleSheet("font-weight: bold; color: #22D3EE;")

        for slider in (self.english_volume, self.russian_volume):
            slider.setRange(0, 100)
            slider.setValue(85)

        en_vol_box = QHBoxLayout()
        en_vol_box.addWidget(self.english_volume)
        en_vol_box.addWidget(self.english_vol_label)

        ru_vol_box = QHBoxLayout()
        ru_vol_box.addWidget(self.russian_volume)
        ru_vol_box.addWidget(self.russian_vol_label)

        lang_layout.addRow("🇬🇧 English:", self.english_enabled)
        lang_layout.addRow("🔊 English Audio Volume:", en_vol_box)
        lang_layout.addRow("🌍 Selectable language:", self.russian_enabled)
        lang_layout.addRow("🔊 Selectable Audio Volume:", ru_vol_box)
        self.voice_settings_button = QPushButton("Cloud Voice Settings…")
        self.voice_settings_button.clicked.connect(self._open_voice_settings)
        lang_layout.addRow("🗣️ Voices:", self.voice_settings_button)

        # -------------------------------------------------------------
        # Translation Speed Mode Section
        # -------------------------------------------------------------
        speed_box = QGroupBox("⚡ Translation Speed Mode")
        speed_box.setStyleSheet("""
            QGroupBox {
                background-color: #0E1726;
                border: 1px solid #1E293B;
                border-radius: 8px;
                margin-top: 8px;
                padding: 12px;
                font-weight: bold;
                color: #22D3EE;
            }
        """)
        speed_layout = QVBoxLayout(speed_box)
        speed_layout.setSpacing(10)

        speed_row = QHBoxLayout()
        speed_lbl = QLabel("Speed Profile:")
        speed_lbl.setStyleSheet("color: #E2E8F0; font-weight: normal; font-size: 12px;")
        self.speed_mode_combo = QComboBox()
        self.speed_mode_combo.addItem("Balanced (Current — Recommended)", SPEED_MODE_BALANCED)
        self.speed_mode_combo.addItem("Low Latency (Experimental)", SPEED_MODE_LOW_LATENCY)
        self.speed_mode_combo.addItem("Dynamic (Recommended — Natural Speech)", SPEED_MODE_DYNAMIC)
        self.speed_mode_combo.addItem("Custom (Advanced)", SPEED_MODE_CUSTOM)
        self.speed_mode_combo.currentIndexChanged.connect(self._on_speed_mode_selection_changed)

        speed_row.addWidget(speed_lbl)
        speed_row.addWidget(self.speed_mode_combo, 1)
        speed_layout.addLayout(speed_row)

        self.speed_mode_desc = QLabel(SPEED_PROFILES[SPEED_MODE_BALANCED]["description"])
        self.speed_mode_desc.setWordWrap(True)
        self.speed_mode_desc.setStyleSheet("color: #94A3B8; font-size: 11px;")
        speed_layout.addWidget(self.speed_mode_desc)

        self.speed_auto_return_cb = QCheckBox("Automatically return to Balanced Mode if latency spikes or queue grows")
        self.speed_auto_return_cb.setChecked(True)
        self.speed_auto_return_cb.toggled.connect(self._save_user_settings)
        speed_layout.addWidget(self.speed_auto_return_cb)

        # Custom Settings Sub-Widget
        self.custom_speed_widget = QWidget()
        custom_layout = QFormLayout(self.custom_speed_widget)
        custom_layout.setContentsMargins(0, 4, 0, 0)
        custom_layout.setSpacing(8)

        self.custom_min_chunk_spin = QDoubleSpinBox()
        self.custom_min_chunk_spin.setRange(0.8, 4.0)
        self.custom_min_chunk_spin.setSingleStep(0.1)
        self.custom_min_chunk_spin.setValue(2.0)
        self.custom_min_chunk_spin.setSuffix(" s")
        self.custom_min_chunk_spin.valueChanged.connect(self._on_custom_param_changed)

        self.custom_chunk_spin = QDoubleSpinBox()
        self.custom_chunk_spin.setRange(1.5, 8.0)
        self.custom_chunk_spin.setSingleStep(0.1)
        self.custom_chunk_spin.setValue(4.0)
        self.custom_chunk_spin.setSuffix(" s")
        self.custom_chunk_spin.valueChanged.connect(self._on_custom_param_changed)

        self.custom_flush_spin = QDoubleSpinBox()
        self.custom_flush_spin.setRange(0.20, 1.00)
        self.custom_flush_spin.setSingleStep(0.05)
        self.custom_flush_spin.setValue(0.40)
        self.custom_flush_spin.setSuffix(" s")
        self.custom_flush_spin.valueChanged.connect(self._on_custom_param_changed)

        self.custom_overlap_spin = QDoubleSpinBox()
        self.custom_overlap_spin.setRange(0.0, 0.50)
        self.custom_overlap_spin.setSingleStep(0.05)
        self.custom_overlap_spin.setValue(0.0)
        self.custom_overlap_spin.setSuffix(" s")
        self.custom_overlap_spin.valueChanged.connect(self._on_custom_param_changed)

        self.custom_rms_spin = QDoubleSpinBox()
        self.custom_rms_spin.setRange(0.001, 0.020)
        self.custom_rms_spin.setDecimals(3)
        self.custom_rms_spin.setSingleStep(0.001)
        self.custom_rms_spin.setValue(0.004)
        self.custom_rms_spin.valueChanged.connect(self._on_custom_param_changed)

        self.custom_reset_btn = QPushButton("↺ Reset to Defaults")
        self.custom_reset_btn.setFixedWidth(140)
        self.custom_reset_btn.clicked.connect(self._reset_custom_speed_defaults)

        custom_layout.addRow("Minimum Chunk Duration:", self.custom_min_chunk_spin)
        custom_layout.addRow("Maximum Chunk Duration:", self.custom_chunk_spin)
        custom_layout.addRow("Pause Flush Silence:", self.custom_flush_spin)
        custom_layout.addRow("Audio Overlap:", self.custom_overlap_spin)
        custom_layout.addRow("VAD RMS Silence Threshold:", self.custom_rms_spin)
        custom_layout.addRow("", self.custom_reset_btn)

        warn_lbl = QLabel(
            "⚠️ Warning: Low chunk durations increase Gemini free-tier request frequency and may trigger "
            "15 RPM rate limiting. Overlap > 0.0s may require deduplication."
        )
        warn_lbl.setWordWrap(True)
        warn_lbl.setStyleSheet("color: #F59E0B; font-size: 11px;")
        custom_layout.addRow("", warn_lbl)

        self.custom_speed_widget.setVisible(False)
        speed_layout.addWidget(self.custom_speed_widget)

        lang_layout.addRow("", speed_box)
        self.tabs.addTab(lang_tab, "🔊 Audio Levels & Languages")

        # Scroll each page rather than the entire navigation/status header.
        # Full-size page controls remain reachable even at the minimum window size.
        for index in range(self.tabs.count()):
            page, title = self.tabs.widget(index), self.tabs.tabText(index)
            self.tabs.removeTab(index)
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QFrame.NoFrame)
            scroll.setWidget(page)
            self.tabs.insertTab(index, scroll, title)
            if index == 0:
                self.main_scroll = scroll
        self.tabs.setCurrentIndex(0)
        content_layout.addWidget(self.tabs, 1)
        self.setCentralWidget(main_widget)

    def _create_hero_card(
        self,
        code: str,
        title: str,
        subtitle: str,
        text_widget: QPlainTextEdit,
        bg_color: str,
        accent_color: str,
        header_icon: str,
        header_widget: QWidget | None = None,
    ) -> QGroupBox:
        box = QGroupBox()
        box.setStyleSheet(f"""
            QGroupBox {{
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 #0F1A24, stop:1 #0C1721);
                border: 1px solid {bg_color};
                border-radius: 12px;
                margin-top: 0px;
                padding: 8px;
            }}
        """)
        layout = QVBoxLayout(box)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(5)

        header = QWidget()
        header.setFixedHeight(42)
        top_row = QHBoxLayout(header)
        top_row.setContentsMargins(0, 0, 0, 0)
        top_row.setSpacing(9)

        badge = QLabel(code)
        badge.setFixedSize(32, 32)
        badge.setAlignment(Qt.AlignCenter)
        badge.setStyleSheet(f"""
            background-color: {bg_color};
            color: #FFFFFF;
            font-weight: bold;
            font-size: 13px;
            border-radius: 8px;
        """)
        top_row.addWidget(badge)

        title_box = QVBoxLayout()
        title_box.setContentsMargins(0, 0, 0, 0)
        title_box.setSpacing(1)
        t_label = QLabel(title)
        t_label.setStyleSheet("font-weight: bold; font-size: 13px; color: #F8FAFC;")
        title_box.addWidget(t_label)
        if subtitle:
            sub_label = QLabel(subtitle)
            sub_label.setStyleSheet("font-size: 11px; color: #64748B;")
            title_box.addWidget(sub_label)
        else:
            title_box.addStretch(1)
        top_row.addLayout(title_box, 1)

        icon = QLabel(header_icon)
        icon.setAlignment(Qt.AlignCenter)
        icon.setFixedSize(30, 30)
        icon.setStyleSheet(f"background-color: #162535; color: {accent_color}; border: 1px solid #26384A; border-radius: 7px; font-size: 15px;")
        top_row.addWidget(icon)
        layout.addWidget(header)

        waveform = self._create_waveform_widget(accent_color)
        if header_widget is None:
            layout.addWidget(waveform)
        else:
            compact_controls = QHBoxLayout()
            compact_controls.setContentsMargins(0, 0, 0, 0)
            compact_controls.setSpacing(7)
            compact_controls.addWidget(header_widget, 1)
            compact_controls.addWidget(waveform, 1)
            layout.addLayout(compact_controls)

        # Text Box Area
        layout.addWidget(text_widget, 1)
        text_widget.setMinimumWidth(0)
        text_widget.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Expanding)
        text_widget.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOn)

        return box

    def _create_waveform_widget(self, color_hex: str) -> QWidget:
        w = QWidget()
        w.setFixedHeight(self.secondary_language_combo.height())
        layout = QHBoxLayout(w)
        layout.setContentsMargins(0, 2, 0, 2)
        layout.setSpacing(3)
        layout.setAlignment(Qt.AlignCenter)

        bar_heights = [6, 11, 18, 10, 20, 14, 11, 18, 8, 17, 12, 9, 18, 14, 9, 16, 11, 8, 14, 10, 6]
        for h in bar_heights:
            bar = QFrame()
            bar.setFixedWidth(2)
            bar.setFixedHeight(h)
            bar.setStyleSheet(f"background-color: {color_hex}; border-radius: 1px;")
            layout.addWidget(bar)
        return w

    def _read_only_text(self, placeholder: str = "") -> QPlainTextEdit:
        text = QPlainTextEdit()
        text.setReadOnly(True)
        # Live sermon text survives Stop and must remain selectable after long
        # sessions. Only the activity log has a rolling block limit.
        text.setUndoRedoEnabled(False)
        if placeholder:
            text.setPlaceholderText(placeholder)
        text.setStyleSheet("""
            QPlainTextEdit {
                background-color: #070C12;
                border: 1px solid #1B2A38;
                border-radius: 8px;
                color: #F8FAFC;
                font-family: 'Consolas', 'Segoe UI', monospace;
                font-size: 13px;
                padding: 10px;
            }
        """)
        return text

    def _switch_nav_tab(self, tab_idx: int) -> None:
        self.tabs.setCurrentIndex(tab_idx)

    def _on_tab_changed(self, tab_idx: int) -> None:
        pass

    def _update_nav_buttons(self, active_tab_idx: int) -> None:
        pass

    def _wire_signals(self) -> None:
        self.start_button.clicked.connect(self.start)
        self.pause_button.clicked.connect(self.toggle_pause)
        self.stop_button.clicked.connect(self.stop)
        self.context_button.clicked.connect(self.open_sermon_context_dialog)
        self.export_button.clicked.connect(self.open_export_transcript_dialog)
        self.summary_button.clicked.connect(self.open_ai_summary_dialog)
        self.history_button.clicked.connect(self.open_session_history_dialog)
        self.refresh_button.clicked.connect(self.refresh_devices)
        self.clear_text_button.clicked.connect(self.clear_text_windows)
        self.set_gemini_key_button.clicked.connect(self.set_gemini_key_dialog)
        self.get_gemini_key_button.clicked.connect(lambda: QDesktopServices.openUrl(QUrl("https://aistudio.google.com/app/apikey")))
        self.set_openai_key_button.clicked.connect(self.set_openai_key_dialog)
        self.get_openai_key_button.clicked.connect(lambda: QDesktopServices.openUrl(QUrl("https://platform.openai.com/api-keys")))
        self.import_google_creds_button.clicked.connect(self.import_google_credentials_dialog)
        self.open_creds_folder_button.clicked.connect(self.open_credentials_folder)
        self.google_tts_guide_button.clicked.connect(self.show_google_tts_guide_dialog)
        self.clear_google_creds_button.clicked.connect(self.clear_google_credentials)
        self.install_local_whisper_button.clicked.connect(self.install_local_whisper)
        self.openai_fallback_toggle.clicked.connect(self._on_paid_fallback_clicked)
        self.openai_fallback_model_combo.currentIndexChanged.connect(self._save_user_settings)
        self.auto_return_toggle.stateChanged.connect(self._save_user_settings)
        self.speech_backend_combo.currentIndexChanged.connect(self._sync_speech_controls)
        self.secondary_language_combo.currentIndexChanged.connect(self._secondary_language_changed)
        self.english_volume.valueChanged.connect(lambda v: self.english_vol_label.setText(f"{v}%"))
        self.russian_volume.valueChanged.connect(lambda v: self.russian_vol_label.setText(f"{v}%"))

        for combo in (
            self.input_combo,
            self.english_output_combo,
            self.russian_output_combo,
            self.secondary_language_combo,
            self.speech_backend_combo,
            self.gemini_model_combo,
            self.model_combo,
            self.quality_combo,
            self.openai_model_combo,
        ):
            combo.currentIndexChanged.connect(self._save_user_settings)
        for checkbox in (self.english_enabled, self.russian_enabled):
            checkbox.stateChanged.connect(self._save_user_settings)
            checkbox.toggled.connect(self._sync_output_state)
        self.secondary_dashboard_toggle.toggled.connect(self.russian_enabled.setChecked)
        for slider in (self.english_volume, self.russian_volume):
            slider.valueChanged.connect(self._save_user_settings)
        self.signals.stopped.connect(self._finished_stop)
        self.english_volume.valueChanged.connect(lambda v: self._volumes.update(en=v / 100.0))
        self.russian_volume.valueChanged.connect(lambda v: self._volumes.update(ru=v / 100.0))
        self.signals.status.connect(self.set_status)
        self.signals.error.connect(self.log_error)
        self.signals.latency.connect(self.set_latency)
        self.signals.transcript.connect(self.append_transcript)
        self.signals.translation.connect(self.append_translation)
        self.signals.level.connect(self.set_input_level)
        self.signals.local_setup_running.connect(self._set_local_setup_running)
        self.signals.provider_health.connect(self._on_health_test_done)
        self.signals.speed_mode_changed.connect(self._apply_reverted_speed_mode)
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
        if not self._restoring_settings:
            self._save_user_settings()

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

    def _select_saved_device(
        self,
        combo: QComboBox,
        devices: list[AudioDevice],
        saved: dict | None,
        include_default: bool,
    ) -> None:
        if not saved:
            return
        saved_name = str(saved.get("name") or "").strip()
        saved_index = saved.get("index")

        # 1. Default output check
        if include_default and saved_index is None and not saved_name:
            combo.setCurrentIndex(0)
            return

        saved_identity = saved.get("identity")
        if saved_identity:
            for device in devices:
                if device.identity == saved_identity:
                    combo.setCurrentIndex(combo.findData(device.index))
                    return

        # 2. Try exact match: both name and index
        if not saved_identity and saved_name and saved_index is not None:
            for row in range(combo.count()):
                data = combo.itemData(row)
                text = combo.itemText(row)
                if data == saved_index and (text == saved_name or text.startswith(f"{saved_name} [")):
                    combo.setCurrentIndex(row)
                    return

        # 3. Match by device name (in case device index shifted)
        if saved_name and not saved_identity:
            for row in range(combo.count()):
                text = combo.itemText(row)
                if text == saved_name or text.startswith(f"{saved_name} ["):
                    combo.setCurrentIndex(row)
                    return

        # 4. Match by index
        if saved_index is not None and not saved_name:
            for row in range(combo.count()):
                if combo.itemData(row) == saved_index:
                    combo.setCurrentIndex(row)
                    return

        # 5. Preserve as disconnected if not currently enumerated
        if saved_name:
            combo.addItem(f"{saved_name} (disconnected)", {"missing": True, "name": saved_name, "identity": saved_identity})
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
        self.secondary_language_combo._commit_search()
        if not self.english_enabled.isChecked() and not self.russian_enabled.isChecked():
            QMessageBox.warning(self, "Select a language", "Enable English, the selectable language, or both before starting.")
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
        russian_output = self._output_device_or_default(
            self.russian_output_combo, self.secondary_language_combo.currentText())
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
            english_volume_getter=lambda: self._volumes["en"],
            russian_volume_getter=lambda: self._volumes["ru"],
            secondary_language=str(self.secondary_language_combo.currentData() or "ru"),
        )
        active_config = self._active_config()
        if not self._confirm_secondary_voice(active_config):
            return
        active_config = self._active_config()
        if not active_config.gemini_api_key:
            answer = QMessageBox.question(
                self,
                "Gemini API Key Recommended",
                "No Gemini API key was found.\n\nThe app will use built-in free instant web translate.\nWould you like to enter your Gemini API key now for AI context-aware translation?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer == QMessageBox.Yes:
                self.set_gemini_key_dialog()
                active_config = self._active_config()

        if active_config.speech_recognition_backend == "openai" and not active_config.openai_api_key:
            answer = QMessageBox.question(
                self,
                "OpenAI API Key Required",
                "OpenAI speech recognition is selected, but no OpenAI API key was found. Would you like to enter your OpenAI API key now?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.Yes,
            )
            if answer == QMessageBox.Yes:
                self.set_openai_key_dialog()
                active_config = self._active_config()

        self._log_configuration_warnings(active_config)
        self._begin_session_context()
        self.clear_text_windows()
        sid = self.session_manager.start_session(
            secondary_language=settings.secondary_language,
            context_title=self.context_manager.title if self.context_manager.is_active else '',
        )
        self.engine = TranslationEngine(
            active_config,
            settings,
            on_status=self.signals.status.emit,
            on_error=self.signals.error.emit,
            on_latency=lambda value: self.signals.session_update.emit(sid, 'latency', value),
            on_transcript=lambda text: self.signals.session_update.emit(sid, 'transcript', text),
            on_translation=lambda lang, text: self.signals.session_update.emit(sid, 'translation', (lang, text)),
            on_level=lambda rms, peak: self.signals.session_update.emit(sid, 'level', (rms, peak)),
            sermon_context=self.context_manager,
            session_manager=self.session_manager,
            on_speed_mode_changed=self._on_engine_speed_mode_reverted,
        )
        try:
            self._latency_samples.clear()
            self.engine.start()
            self._save_user_settings()
            self._set_running(True)
        except Exception as exc:
            self.engine = None
            self.session_manager.end_session()
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
            geometry = self.user_settings.get('window', {}).get('geometry')
            if isinstance(geometry, str):
                self.restoreGeometry(QByteArray.fromHex(geometry.encode('ascii', errors='ignore')))
            languages = self.user_settings.get("languages", {})
            if "english_enabled" in languages:
                self.english_enabled.setChecked(bool(languages["english_enabled"]))
            if "russian_enabled" in languages:
                self.russian_enabled.setChecked(bool(languages["russian_enabled"]))
            self._set_combo_data(self.secondary_language_combo, languages.get("secondary_language", "ru"))
            volumes = self.user_settings.get("volumes", {})
            self.english_volume.setValue(int(volumes.get("english", self.english_volume.value())))
            self.russian_volume.setValue(int(volumes.get("russian", self.russian_volume.value())))
            recognition = self.user_settings.get("recognition", {})
            self._set_combo_data(self.speech_backend_combo, recognition.get("backend"))
            self._set_combo_data(self.gemini_model_combo, live_gemini_model(recognition.get("gemini_model")))
            self._set_combo_data(self.model_combo, recognition.get("whisper_model"))
            self._set_combo_data(self.quality_combo, recognition.get("whisper_quality"))
            self._set_combo_data(self.openai_model_combo, recognition.get("openai_model"))
            if "openai_fallback_enabled" in recognition:
                # Earlier versions defaulted ON and autosaved that value. An
                # old True alone is not evidence that the user authorized cost.
                self._paid_fallback_authorized = bool(recognition.get("paid_translation_consent_v1", False))
                self.openai_fallback_toggle.setChecked(bool(recognition["openai_fallback_enabled"]) and self._paid_fallback_authorized)
            self._set_combo_data(self.openai_fallback_model_combo, recognition.get("openai_fallback_model"))
            if "auto_return_to_gemini" in recognition:
                self.auto_return_toggle.setChecked(bool(recognition["auto_return_to_gemini"]))
            speed_data = self.user_settings.get("speed", {})
            speed_mode = speed_data.get("mode", SPEED_MODE_BALANCED)
            self._set_combo_data(self.speed_mode_combo, speed_mode)
            if "auto_return" in speed_data:
                self.speed_auto_return_cb.setChecked(bool(speed_data["auto_return"]))
            custom_data = speed_data.get("custom", {})
            if custom_data:
                self.custom_chunk_spin.setValue(float(custom_data.get("chunk_seconds", 4.0)))
                self.custom_min_chunk_spin.setValue(float(custom_data.get("min_chunk_seconds", 2.0)))
                self.custom_flush_spin.setValue(float(custom_data.get("early_flush_silence_seconds", 0.40)))
                self.custom_overlap_spin.setValue(float(custom_data.get("overlap_seconds", 0.0)))
                self.custom_rms_spin.setValue(float(custom_data.get("vad_rms_threshold", 0.004)))
            self._update_speed_ui()
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
        # Guard against wiping saved device settings if combos are not yet populated
        saved_devices = self.user_settings.get("devices", {})
        input_dev = self._device_setting(self.input_combo) if self.input_combo.count() > 0 else saved_devices.get("input", {"index": None, "name": ""})
        en_dev = self._device_setting(self.english_output_combo) if self.english_output_combo.count() > 0 else saved_devices.get("english_output", {"index": None, "name": ""})
        ru_dev = self._device_setting(self.russian_output_combo) if self.russian_output_combo.count() > 0 else saved_devices.get("russian_output", {"index": None, "name": ""})

        settings = {
            **self.user_settings,
            'window': {'geometry': bytes(self.saveGeometry().toHex()).decode('ascii')},
            "voices": self._voice_settings,
            "devices": {
                "input": input_dev,
                "english_output": en_dev,
                "russian_output": ru_dev,
            },
            "languages": {
                "english_enabled": self.english_enabled.isChecked(),
                "russian_enabled": self.russian_enabled.isChecked(),
                "secondary_language": str(self.secondary_language_combo.currentData() or "ru"),
            },
            "volumes": {
                "english": self.english_volume.value(),
                "russian": self.russian_volume.value(),
            },
            "recognition": {
                "backend": self.speech_backend_combo.currentData(),
                "gemini_model": self.gemini_model_combo.currentData(),
                "whisper_model": self.model_combo.currentData(),
                "whisper_quality": self.quality_combo.currentData(),
                "openai_model": self.openai_model_combo.currentData(),
                "openai_fallback_enabled": self.openai_fallback_toggle.isChecked(),
                "paid_translation_consent_v1": self._paid_fallback_authorized,
                "openai_fallback_model": self.openai_fallback_model_combo.currentData(),
                "auto_return_to_gemini": self.auto_return_toggle.isChecked(),
            },
            "speed": {
                "mode": str(self.speed_mode_combo.currentData() or SPEED_MODE_BALANCED),
                "auto_return": bool(self.speed_auto_return_cb.isChecked()),
                "custom": {
                    "chunk_seconds": float(self.custom_chunk_spin.value()),
                    "min_chunk_seconds": float(self.custom_min_chunk_spin.value()),
                    "early_flush_silence_seconds": float(self.custom_flush_spin.value()),
                    "overlap_seconds": float(self.custom_overlap_spin.value()),
                    "vad_rms_threshold": float(self.custom_rms_spin.value()),
                },
            },
        }
        self.user_settings = settings
        if self.engine:
            self.engine._translator.config = replace(self.engine._translator.config,
                openai_fallback_enabled=self.openai_fallback_toggle.isChecked())
        try:
            save_user_settings(settings)
        except Exception as exc:
            self.log_error(f"Could not save settings: {exc}")

    def _on_paid_fallback_clicked(self, checked):
        self._paid_fallback_authorized = bool(checked)
        self._save_user_settings()

    def _on_speed_mode_selection_changed(self) -> None:
        mode = self.speed_mode_combo.currentData() or SPEED_MODE_BALANCED
        self._update_speed_ui()
        if self.engine and not self._is_paused:
            reply = QMessageBox.question(
                self,
                "Apply Speed Mode to Active Session?",
                f"Translation is currently active. Would you like to switch to '{mode.replace('_', ' ').title()}' "
                "immediately at the next audio boundary without pausing?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.Yes,
            )
            if reply == QMessageBox.Yes:
                custom_params = {
                    "chunk_seconds": self.custom_chunk_spin.value(),
                    "min_chunk_seconds": self.custom_min_chunk_spin.value(),
                    "early_flush_silence_seconds": self.custom_flush_spin.value(),
                    "overlap_seconds": self.custom_overlap_spin.value(),
                    "vad_rms_threshold": self.custom_rms_spin.value(),
                } if mode == SPEED_MODE_CUSTOM else None
                self.engine.switch_speed_mode(mode, custom_overrides=custom_params)
            else:
                active_m = self.engine.active_speed_settings.get("mode", SPEED_MODE_BALANCED)
                self._set_combo_data(self.speed_mode_combo, active_m)
                self._update_speed_ui()
                return
        elif self.engine and self._is_paused:
            custom_params = {
                "chunk_seconds": self.custom_chunk_spin.value(),
                "min_chunk_seconds": self.custom_min_chunk_spin.value(),
                "early_flush_silence_seconds": self.custom_flush_spin.value(),
                "overlap_seconds": self.custom_overlap_spin.value(),
                "vad_rms_threshold": self.custom_rms_spin.value(),
            } if mode == SPEED_MODE_CUSTOM else None
            self.engine.switch_speed_mode(mode, custom_overrides=custom_params)
        self._save_user_settings()

    def _on_custom_param_changed(self) -> None:
        if self.speed_mode_combo.currentData() == SPEED_MODE_CUSTOM:
            if self.engine:
                custom_params = {
                    "chunk_seconds": self.custom_chunk_spin.value(),
                    "min_chunk_seconds": self.custom_min_chunk_spin.value(),
                    "early_flush_silence_seconds": self.custom_flush_spin.value(),
                    "overlap_seconds": self.custom_overlap_spin.value(),
                    "vad_rms_threshold": self.custom_rms_spin.value(),
                }
                self.engine.switch_speed_mode(SPEED_MODE_CUSTOM, custom_overrides=custom_params)
            self._save_user_settings()

    def _reset_custom_speed_defaults(self) -> None:
        self.custom_min_chunk_spin.setValue(2.0)
        self.custom_chunk_spin.setValue(4.0)
        self.custom_flush_spin.setValue(0.40)
        self.custom_overlap_spin.setValue(0.0)
        self.custom_rms_spin.setValue(0.004)
        self._save_user_settings()

    def _update_speed_ui(self) -> None:
        mode = self.speed_mode_combo.currentData() or SPEED_MODE_BALANCED
        desc = SPEED_PROFILES.get(mode, {}).get("description", "")
        self.speed_mode_desc.setText(desc)
        self.custom_speed_widget.setVisible(mode == SPEED_MODE_CUSTOM)
        self.speed_badge.setToolTip(self.speed_mode_combo.currentText())
        if mode == SPEED_MODE_BALANCED:
            self.speed_badge.setText("⚡ B")
            self.speed_badge.setStyleSheet("color: #34D399; font-size: 12px; font-weight: bold;")
        elif mode == SPEED_MODE_LOW_LATENCY:
            self.speed_badge.setText("⚡ L")
            self.speed_badge.setStyleSheet("color: #F59E0B; font-size: 12px; font-weight: bold;")
        elif mode == SPEED_MODE_DYNAMIC:
            self.speed_badge.setText("⚡ D")
            self.speed_badge.setStyleSheet("color: #34D399; font-size: 12px; font-weight: bold;")
        else:
            self.speed_badge.setText("⚡ C")
            self.speed_badge.setStyleSheet("color: #A78BFA; font-size: 12px; font-weight: bold;")

    def _on_engine_speed_mode_reverted(self, mode: str) -> None:
        self.signals.speed_mode_changed.emit(mode)

    def _apply_reverted_speed_mode(self, mode: str) -> None:
        self._set_combo_data(self.speed_mode_combo, mode)
        self._update_speed_ui()
        self._save_user_settings()

    def _device_setting(self, combo: QComboBox) -> dict:
        value = combo.currentData()
        if isinstance(value, dict) and value.get("missing"):
            return {"index": None, "name": str(value.get("name", "")).strip(), "identity": value.get("identity")}
        if value is None:
            return {"index": None, "name": ""}
        name = ""
        for device in self.devices:
            if device.index == value:
                return {"index": int(value), "name": device.name, "identity": device.identity}
        if not name:
            text = combo.currentText()
            name = re.sub(r"\s*\[\d+\]$", "", text).strip()
        return {"index": int(value), "name": name}

    def _active_config(self):
        latest = load_config()
        self.config = latest
        return replace(
            latest,
            voice_settings=self._voice_settings,
            gemini_model=str(
                self.gemini_model_combo.currentData() or latest.gemini_model
            ),
            speech_recognition_backend=str(
                self.speech_backend_combo.currentData() or latest.speech_recognition_backend
            ),
            openai_transcription_model=str(
                self.openai_model_combo.currentData() or latest.openai_transcription_model
            ),
            whisper_model_size=str(self.model_combo.currentData() or latest.whisper_model_size),
            whisper_quality_mode=str(self.quality_combo.currentData() or latest.whisper_quality_mode),
            openai_fallback_enabled=bool(self.openai_fallback_toggle.isChecked()),
            openai_fallback_model=str(self.openai_fallback_model_combo.currentData() or "gpt-4o-mini"),
            auto_return_to_gemini=bool(self.auto_return_toggle.isChecked()),
            speed_mode=str(self.speed_mode_combo.currentData() or latest.speed_mode),
            low_latency_auto_return=bool(self.speed_auto_return_cb.isChecked()),
            custom_chunk_seconds=float(self.custom_chunk_spin.value()),
            custom_min_chunk_seconds=float(self.custom_min_chunk_spin.value()),
            custom_early_flush_silence_seconds=float(self.custom_flush_spin.value()),
            custom_overlap_seconds=float(self.custom_overlap_spin.value()),
            custom_vad_rms_threshold=float(self.custom_rms_spin.value()),
        )

    def _log_configuration_warnings(self, config=None) -> None:
        config = config or self.config
        if not config.gemini_api_key:
            self.log_status(
                "No GEMINI_API_KEY set. Translation will use built-in free instant web translate."
            )
        if not config.google_application_credentials:
            self.log_status(
                "Google Cloud service account not configured. Using built-in free instant speech synthesis."
            )
        elif not Path(config.google_application_credentials).exists():
            self.log_error(
                f"GOOGLE_APPLICATION_CREDENTIALS file was not found: {config.google_application_credentials}. Using built-in free instant speech synthesis."
            )
        if config.speech_recognition_backend == "openai" and not config.openai_api_key:
            self.log_error("OPENAI_API_KEY is not set. OpenAI transcription will fail.")
        if config.speech_recognition_backend == "openai":
            if config.chunk_overlap_seconds > 0.0:
                self.log_error(
                    "OpenAI chunk overlap retranscribes audio and increases API cost. "
                    "Use CHUNK_OVERLAP_SECONDS=0.0 unless boundary accuracy is more important than cost."
                )
            if not config.vad_enabled:
                self.log_error("VAD is disabled; silence/noise chunks may be uploaded and billed.")
        profile = get_speed_profile_settings(config.speed_mode, custom_overrides={
            'chunk_seconds': config.custom_chunk_seconds, 'min_chunk_seconds': config.custom_min_chunk_seconds,
            'early_flush_silence_seconds': config.custom_early_flush_silence_seconds,
            'overlap_seconds': config.custom_overlap_seconds, 'vad_rms_threshold': config.custom_vad_rms_threshold})
        self.log_status(
            f"Runtime tuning: mode={profile['mode']} chunk {profile['min_chunk_seconds']:.1f}-{profile['chunk_seconds']:.1f}s, "
            f"pause flush {profile['early_flush_silence_seconds']:.2f}s, overlap 0.0s, "
            f"VAD {'on' if config.vad_enabled else 'off'} (rms {profile['vad_rms_threshold']:.4f})."
        )
        if config.speech_recognition_backend == "local" and not self._local_whisper_dependencies_ready():
            self.log_error("Local Whisper is not installed. Click Install local Whisper first, or use OpenAI API.")

    def toggle_pause(self) -> None:
        if not self.engine:
            return
        if not self._is_paused:
            self._is_paused = True
            self.engine.pause()
            self.pause_button.setText("▶ Resume")
            self.status_pill.setText("⏸ PAUSE")
            self.status_pill.setToolTip("Capture is paused; pending speech may still finish.")
            self.status_pill.setStyleSheet("color: #F59E0B; font-size: 12px; font-weight: bold;")
        else:
            self._is_paused = False
            self.engine.resume()
            self.pause_button.setText("⏸ Pause")
            self.status_pill.setText("✓ LIVE")
            self.status_pill.setToolTip("Translation is running.")
            self.status_pill.setStyleSheet("color: #34D399; font-size: 12px; font-weight: bold;")

    def stop(self) -> None:
        if self._stopping:
            return
        if self.engine:
            self._stopping = True
            self.stop_button.setEnabled(False)
            self.stop_button.setText("Ending...")
            self.pause_button.setEnabled(False)
            engine = self.engine
            def drain():
                try:
                    engine.stop()
                except Exception as exc:
                    self.signals.error.emit(f"Stop failed: {safe_error(exc)}")
                finally:
                    if self.session_manager:
                        self.session_manager.end_session(getattr(engine, 'completion_info', None))
                    self.signals.stopped.emit()
            threading.Thread(target=drain, name="session-stop", daemon=True).start()
        else:
            self._set_running(False)

    def _finished_stop(self):
        self.engine = None
        self._stopping = False
        self._is_paused = False
        self.context_manager.clear()
        self._context_staged_for_next_session = False
        self.log_status('[CONTEXT] reset reason=session_finished enabled=false')
        self._set_running(False)
        if self._close_after_stop:
            self.close()

    def closeEvent(self, event) -> None:
        self._save_user_settings()
        if any(d._worker_thread and d._worker_thread.isRunning()
               for d in AISummaryDialog._background_dialogs):
            event.ignore()
            QTimer.singleShot(200, self.close)
            return
        if self.engine:
            self._close_after_stop = True
            event.ignore()
            self.stop()
        else:
            event.accept()

    def _set_running(self, running: bool) -> None:
        self.start_button.setEnabled(not running)
        self.pause_button.setEnabled(running)
        self.pause_button.setText("⏸ Pause")
        self.stop_button.setEnabled(running)
        self.input_combo.setEnabled(not running)
        self.speech_backend_combo.setEnabled(not running)
        self.gemini_model_combo.setEnabled(not running)
        self.model_combo.setEnabled(not running)
        self.quality_combo.setEnabled(not running)
        self.openai_model_combo.setEnabled(not running)
        self.english_enabled.setEnabled(not running)
        self.russian_enabled.setEnabled(not running)
        self.secondary_dashboard_toggle.setEnabled(not running)
        self._sync_output_state()
        self.secondary_language_combo.setEnabled(not running)
        self.english_output_combo.setEnabled(not running)
        self.russian_output_combo.setEnabled(not running)
        self.refresh_button.setEnabled(not running)
        for button in (self.set_gemini_key_button, self.set_openai_key_button,
                       self.import_google_creds_button, self.clear_google_creds_button, self.google_tts_guide_button):
            button.setEnabled(not running)
        self.clear_text_button.setEnabled(True)
        self.context_button.setEnabled(True)
        self.export_button.setEnabled(True)
        self.summary_button.setEnabled(True)
        self.history_button.setEnabled(True)
        self.install_local_whisper_button.setEnabled(not running and not self._local_setup_running())

        if running:
            self.start_button.setText("✓ Started (Active)")
            self.stop_button.setText("⏹ End Session")
            self.status_pill.setText("✓ LIVE")
            self.status_pill.setToolTip("Translation is running.")
            self.status_pill.setStyleSheet("color: #34D399; font-size: 12px; font-weight: bold;")
        else:
            self.start_button.setText("▶ Start Translation")
            self.stop_button.setText("⏹ End Session")
            self.status_pill.setText("○ IDLE")
            self.status_pill.setToolTip("Translation is stopped.")
            self.status_pill.setStyleSheet("color: #94A3B8; font-size: 12px; font-weight: bold;")
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
        self.gemini_model_combo.setEnabled(not running)
        self.model_combo.setEnabled(not running and not use_openai)
        self.quality_combo.setEnabled(not running and not use_openai)
        self.openai_model_combo.setEnabled(not running and use_openai)
        self.install_local_whisper_button.setEnabled(not running and not self._local_setup_running())
        self._update_credentials_status_ui()

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

        setup_config = self._active_config()
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
                transcriber = LocalWhisperTranscriber(setup_config, glossary, self.signals.status.emit)
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

    def _save_env_var(self, key: str, value: str) -> None:
        os.environ[key] = value
        env_path = project_root() / ".env"
        content = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
        lines = content.splitlines()
        found = False
        new_lines = []
        for line in lines:
            if line.startswith(f"{key}="):
                new_lines.append(f"{key}={value}")
                found = True
            else:
                new_lines.append(line)
        if not found:
            new_lines.insert(0, f"{key}={value}")
        atomic_write_text(env_path, "\n".join(new_lines))

    def _remove_env_var(self, key: str) -> None:
        os.environ.pop(key, None)
        env_path = project_root() / ".env"
        if not env_path.exists():
            return
        content = env_path.read_text(encoding="utf-8")
        lines = [line for line in content.splitlines() if not line.startswith(f"{key}=")]
        atomic_write_text(env_path, "\n".join(lines))

    def _update_credentials_status_ui(self) -> None:
        gemini_key = os.getenv("GEMINI_API_KEY") or self.config.gemini_api_key
        if gemini_key:
            masked = f"{gemini_key[:6]}...{gemini_key[-4:]}" if len(gemini_key) >= 12 else "Configured"
            self.gemini_status_label.setText(f"Configured ({masked}) — use Test API Key to verify access")
            self.gemini_status_label.setStyleSheet("color: #34D399; font-size: 11px; font-weight: bold;")
        else:
            self.gemini_status_label.setText("ℹ️ Free Web Translate Active ($0) - Gemini key not set")
            self.gemini_status_label.setStyleSheet("color: #38BDF8; font-size: 11px; font-weight: bold;")

        openai_key = os.getenv("OPENAI_API_KEY") or self.config.openai_api_key
        if openai_key:
            masked = f"{openai_key[:6]}...{openai_key[-4:]}" if len(openai_key) >= 12 else "Configured"
            self.openai_status_label.setText(f"✅ Active ({masked}) - Cloud Whisper")
            self.openai_status_label.setStyleSheet("color: #34D399; font-size: 11px; font-weight: bold;")
        else:
            self.openai_status_label.setText("⚠️ Key missing (Required for OpenAI Whisper STT)")
            self.openai_status_label.setStyleSheet("color: #FBBF24; font-size: 11px; font-weight: bold;")

        creds_path = self.config.google_application_credentials or os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
        info = inspect_service_account_file(creds_path) if creds_path else None
        if info and info.get("valid"):
            proj = f" (Project: {info['project_id']})" if info.get("project_id") else ""
            self.tts_status_label.setText(f"✅ Cloud TTS Active: {info['filename']}{proj}")
            self.tts_status_label.setStyleSheet(
                "color: #34D399; font-size: 11px; font-weight: bold; padding: 4px 8px; "
                "background-color: #07271E; border: 1px solid #059669; border-radius: 6px;"
            )
            self.clear_google_creds_button.setEnabled(self.engine is None)
        else:
            self.tts_status_label.setText("ℹ️ Free Instant TTS Active (Google Service Account JSON not loaded)")
            self.tts_status_label.setStyleSheet(
                "color: #38BDF8; font-size: 11px; font-weight: bold; padding: 4px 8px; "
                "background-color: #082F49; border: 1px solid #0284C7; border-radius: 6px;"
            )
            self.clear_google_creds_button.setEnabled(False)

    def set_gemini_key_dialog(self) -> None:
        if self.engine is not None:
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("Set Gemini API Key")
        dialog.setMinimumWidth(480)
        dialog.setStyleSheet("""
            QDialog {
                background-color: #0F172A;
                color: #F8FAFC;
            }
        """)
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(14)

        header = QLabel("✨ Gemini Translation API Key")
        header.setStyleSheet("font-size: 15px; font-weight: bold; color: #22D3EE;")
        layout.addWidget(header)

        desc = QLabel(
            "Gemini provides fast, intelligent context-aware translation into English and Russian.\n"
            "Google AI Studio provides API keys; quotas and pricing depend on your account.\n"
            "If not set, the app automatically uses built-in free instant web translate ($0)."
        )
        desc.setWordWrap(True)
        desc.setStyleSheet("color: #94A3B8; font-size: 12px;")
        layout.addWidget(desc)

        link_row = QHBoxLayout()
        link_btn = QPushButton("🌐 Open Google AI Studio (Get Free Key)")
        link_btn.setStyleSheet("""
            QPushButton {
                background-color: #0284C7;
                color: #FFFFFF;
                font-weight: bold;
                border: none;
                border-radius: 6px;
                padding: 6px 14px;
            }
            QPushButton:hover {
                background-color: #0369A1;
            }
        """)
        link_btn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl("https://aistudio.google.com/app/apikey")))
        link_row.addWidget(link_btn)
        link_row.addStretch(1)
        layout.addLayout(link_row)

        input_label = QLabel("API Key:")
        input_label.setStyleSheet("font-weight: 600; color: #CBD5E1;")
        layout.addWidget(input_label)

        current_key = os.getenv("GEMINI_API_KEY", "") or (self.config.gemini_api_key or "")
        key_input = QLineEdit(current_key)
        key_input.setEchoMode(QLineEdit.Password)
        key_input.setPlaceholderText("Paste your Gemini API key (e.g. AIzaSy...)")
        key_input.setStyleSheet("""
            QLineEdit {
                background-color: #070C12;
                border: 1px solid #334155;
                border-radius: 6px;
                padding: 8px 12px;
                color: #F8FAFC;
                font-size: 13px;
            }
            QLineEdit:focus {
                border-color: #22D3EE;
            }
        """)
        layout.addWidget(key_input)

        # Network work stays off the GUI thread; only the timer touches widgets.
        import queue
        from .services import Translator
        from .glossary import load_glossary
        results = queue.Queue()
        test_result = QLabel("")
        test_result.setTextFormat(Qt.PlainText)
        test_result.setWordWrap(True)
        layout.addWidget(test_result)
        test_btn = QPushButton("Test API Key")
        layout.addWidget(test_btn)
        timer = QTimer(dialog)
        timer.setInterval(100)

        def finish_test():
            try:
                ok, message = results.get_nowait()
            except queue.Empty:
                return
            timer.stop()
            test_result.setText(message)
            test_result.setStyleSheet("color: #34D399;" if ok else "color: #F59E0B;")
            test_btn.setEnabled(True)
            save_btn.setEnabled(True)
            key_input.setEnabled(True)

        def test_key():
            key = key_input.text().strip()
            if not key:
                test_result.setText("Enter an API key first.")
                return
            test_config = replace(self.config, gemini_api_key=key,
                                  gemini_model=str(self.gemini_model_combo.currentData() or self.config.gemini_model))
            glossary = load_glossary(project_root())
            test_btn.setEnabled(False)
            save_btn.setEnabled(False)
            key_input.setEnabled(False)
            test_result.setText("Checking Gemini access with the selected model...")

            def check():
                translator = None
                try:
                    translator = Translator(test_config, glossary)
                    translator.check_access()
                    results.put((True, "Gemini access verified. You can save this key and start translation."))
                except Exception as exc:
                    results.put((False, safe_error(exc).replace(key, "[redacted]")))
                finally:
                    client = getattr(translator, "_genai_client", None)
                    if client is not None:
                        try:
                            client.close()
                        except Exception:
                            pass

            timer.start()
            threading.Thread(target=check, daemon=True).start()

        timer.timeout.connect(finish_test)
        dialog.finished.connect(timer.stop)
        test_btn.clicked.connect(test_key)

        btn_box = QHBoxLayout()
        btn_box.addStretch(1)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(dialog.reject)
        save_btn = QPushButton("Save API Key")
        save_btn.setStyleSheet("""
            QPushButton {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #0D9488, stop:1 #14B8A6);
                color: #FFFFFF;
                font-weight: bold;
                border: none;
                border-radius: 6px;
                padding: 6px 16px;
            }
            QPushButton:hover {
                background: #0F766E;
            }
        """)
        save_btn.clicked.connect(dialog.accept)
        btn_box.addWidget(cancel_btn)
        btn_box.addWidget(save_btn)
        layout.addLayout(btn_box)

        if dialog.exec() == QDialog.Accepted:
            new_key = key_input.text().strip()
            if new_key:
                self._save_env_var("GEMINI_API_KEY", new_key)
                self.config = replace(self.config, gemini_api_key=new_key)
                self._update_credentials_status_ui()
                self.log_status("[GEMINI] Gemini API key updated and saved to .env file.")
                QMessageBox.information(self, "API Key Saved", "Gemini API key saved successfully!")

    def set_openai_key_dialog(self) -> None:
        if self.engine is not None:
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("Set OpenAI API Key")
        dialog.setMinimumWidth(480)
        dialog.setStyleSheet("""
            QDialog {
                background-color: #0F172A;
                color: #F8FAFC;
            }
        """)
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(14)

        header = QLabel("☁️ OpenAI Speech Recognition API Key")
        header.setStyleSheet("font-size: 15px; font-weight: bold; color: #22D3EE;")
        layout.addWidget(header)

        desc = QLabel(
            "Configure OpenAI for Latvian transcription. API usage may incur charges.\n"
            "Gemini and Google Cloud TTS quotas and prices depend on your account."
        )
        desc.setWordWrap(True)
        desc.setStyleSheet("color: #94A3B8; font-size: 12px;")
        layout.addWidget(desc)

        link_row = QHBoxLayout()
        link_btn = QPushButton("🌐 Open OpenAI API Keys Page")
        link_btn.setStyleSheet("""
            QPushButton {
                background-color: #0284C7;
                color: #FFFFFF;
                font-weight: bold;
                border: none;
                border-radius: 6px;
                padding: 6px 14px;
            }
            QPushButton:hover {
                background-color: #0369A1;
            }
        """)
        link_btn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl("https://platform.openai.com/api-keys")))
        link_row.addWidget(link_btn)
        link_row.addStretch(1)
        layout.addLayout(link_row)

        input_label = QLabel("API Key:")
        input_label.setStyleSheet("font-weight: 600; color: #CBD5E1;")
        layout.addWidget(input_label)

        current_key = os.getenv("OPENAI_API_KEY", "") or (self.config.openai_api_key or "")
        key_input = QLineEdit(current_key)
        key_input.setEchoMode(QLineEdit.Password)
        key_input.setPlaceholderText("Paste your OpenAI API key (e.g. sk-...)")
        key_input.setStyleSheet("""
            QLineEdit {
                background-color: #070C12;
                border: 1px solid #334155;
                border-radius: 6px;
                padding: 8px 12px;
                color: #F8FAFC;
                font-size: 13px;
            }
            QLineEdit:focus {
                border-color: #22D3EE;
            }
        """)
        layout.addWidget(key_input)

        btn_box = QHBoxLayout()
        btn_box.addStretch(1)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(dialog.reject)
        save_btn = QPushButton("Save API Key")
        save_btn.setStyleSheet("""
            QPushButton {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #0D9488, stop:1 #14B8A6);
                color: #FFFFFF;
                font-weight: bold;
                border: none;
                border-radius: 6px;
                padding: 6px 16px;
            }
            QPushButton:hover {
                background: #0F766E;
            }
        """)
        save_btn.clicked.connect(dialog.accept)
        btn_box.addWidget(cancel_btn)
        btn_box.addWidget(save_btn)
        layout.addLayout(btn_box)

        if dialog.exec() == QDialog.Accepted:
            new_key = key_input.text().strip()
            if new_key:
                self._save_env_var("OPENAI_API_KEY", new_key)
                self.config = replace(self.config, openai_api_key=new_key)
                self._update_credentials_status_ui()
                self.log_status("[OPENAI] OpenAI API key updated and saved to .env file.")
                QMessageBox.information(self, "API Key Saved", "OpenAI API key saved successfully!")

    def import_google_credentials_dialog(self) -> None:
        if self.engine is not None:
            return
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "Select Google Cloud Service Account JSON Key",
            str(Path.home()),
            "JSON Files (*.json);;All Files (*.*)",
        )
        if not file_path:
            return

        src_path = Path(file_path)
        info = inspect_service_account_file(src_path)
        if not info or not info.get("valid"):
            reply = QMessageBox.warning(
                self,
                "Warning: JSON Format",
                "The selected JSON file does not appear to be a standard Google Cloud Service Account key.\n\nDo you want to import and use it anyway?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                return

        try:
            creds_dir = project_root() / "credentials"
            creds_dir.mkdir(parents=True, exist_ok=True)
            dest_filename = src_path.name
            dest_path = creds_dir / dest_filename
            if src_path.resolve() != dest_path.resolve():
                shutil.copy2(src_path, dest_path)

            rel_path = f"credentials/{dest_filename}"
            self._save_env_var("GOOGLE_APPLICATION_CREDENTIALS", rel_path)
            os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(dest_path.resolve())
            self.config = replace(self.config, google_application_credentials=str(dest_path.resolve()))


            self._update_credentials_status_ui()
            project_info = f"\nProject ID: {info['project_id']}" if info and info.get("project_id") else ""
            client_info = f"\nService Account: {info['client_email']}" if info and info.get("client_email") else ""
            self.log_status(f"[GOOGLE TTS] Imported credentials: {dest_filename}")
            QMessageBox.information(
                self,
                "Credentials Imported Successfully",
                f"Google Cloud credentials have been saved to your credentials folder!{project_info}{client_info}\n\nFile: {rel_path}\n\nGoogle Cloud Text-to-Speech is now active.",
            )
        except Exception as exc:
            self.log_error(f"Failed to import credentials: {exc}")
            QMessageBox.critical(self, "Import Failed", f"Could not import credentials file:\n{exc}")

    def open_credentials_folder(self) -> None:
        creds_dir = project_root() / "credentials"
        creds_dir.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(creds_dir.resolve())))
        self.log_status(f"Opened credentials folder: {creds_dir}")

    def clear_google_credentials(self) -> None:
        if self.engine is not None:
            return
        reply = QMessageBox.question(
            self,
            "Clear Google Credentials",
            "Are you sure you want to remove the Google Cloud credentials link?\n\nThe app will switch back to the built-in free instant speech synthesis fallback.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        self._save_env_var("GOOGLE_APPLICATION_CREDENTIALS", "disabled")
        self.config = replace(self.config, google_application_credentials=None)

        self._update_credentials_status_ui()
        self.log_status("[GOOGLE TTS] Google credentials unlinked. Using free instant TTS fallback.")
        QMessageBox.information(self, "Credentials Cleared", "Google Cloud credentials have been unlinked.\n\nFree speech synthesis is now active.")

    def show_google_tts_guide_dialog(self) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle("Google Cloud Text-to-Speech - Setup & Credentials Guide")
        dialog.setMinimumWidth(620)
        dialog.setStyleSheet("""
            QDialog {
                background-color: #0F172A;
                color: #F8FAFC;
            }
        """)
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(16)

        title = QLabel("🎙️ Google Cloud Text-to-Speech Setup Guide")
        title.setStyleSheet("font-size: 16px; font-weight: bold; color: #22D3EE;")
        layout.addWidget(title)

        intro = QLabel(
            "Google Cloud Text-to-Speech provides natural, high-definition neural voices for sermon translation.\n"
            "Follow these quick steps to get your service account JSON credentials:"
        )
        intro.setWordWrap(True)
        intro.setStyleSheet("color: #CBD5E1; font-size: 12px; line-height: 1.4;")
        layout.addWidget(intro)

        steps_box = QGroupBox("Step-by-Step Instructions")
        steps_box.setStyleSheet("""
            QGroupBox {
                background-color: #111827;
                border: 1px solid #1E293B;
                border-radius: 8px;
                padding: 16px;
                margin-top: 8px;
            }
            QGroupBox::title {
                color: #38BDF8;
                font-weight: bold;
            }
        """)
        steps_layout = QVBoxLayout(steps_box)
        steps_layout.setSpacing(10)

        step1 = QLabel("<b>1. Open Google Cloud Console:</b> Create a new project or select an existing one.")
        step1.setWordWrap(True)
        btn1 = QPushButton("🌐 Open Google Cloud Console")
        btn1.clicked.connect(lambda: QDesktopServices.openUrl(QUrl("https://console.cloud.google.com/")))

        step2 = QLabel("<b>2. Enable Text-to-Speech API:</b> Search for and enable the Cloud Text-to-Speech API.")
        step2.setWordWrap(True)
        btn2 = QPushButton("🌐 Enable Cloud Text-to-Speech API")
        btn2.clicked.connect(lambda: QDesktopServices.openUrl(QUrl("https://console.cloud.google.com/apis/library/texttospeech.googleapis.com")))

        step3 = QLabel("<b>3. Create Service Account:</b> Go to <i>IAM & Admin > Service Accounts</i>, click <b>Create Service Account</b> (name it e.g. <code>church-tts</code>), and grant role <b>Cloud Text-to-Speech User</b>.")
        step3.setWordWrap(True)
        btn3 = QPushButton("🌐 Open Service Accounts (IAM)")
        btn3.clicked.connect(lambda: QDesktopServices.openUrl(QUrl("https://console.cloud.google.com/iam-admin/serviceaccounts")))

        step4 = QLabel("<b>4. Create JSON Key:</b> Click on your Service Account -> <b>Keys</b> tab -> <b>Add Key</b> -> <b>Create new key</b> -> choose <b>JSON</b> -> Download the file.")
        step4.setWordWrap(True)

        step5 = QLabel("<b>5. Import Credentials:</b> Click <b>'Import JSON Key'</b> below or drop the JSON file into the <code>credentials/</code> folder.")
        step5.setWordWrap(True)

        for w in (step1, btn1, step2, btn2, step3, btn3, step4, step5):
            if isinstance(w, QLabel):
                w.setStyleSheet("color: #E2E8F0; font-size: 12px;")
            steps_layout.addWidget(w)

        layout.addWidget(steps_box)

        note_label = QLabel(
            "💡 <b>Free Tier:</b> Google Cloud provides 4 million characters free every month for Standard voices.\n"
            "If credentials are not configured, ChurchTranslator automatically uses built-in free speech synthesis."
        )
        note_label.setWordWrap(True)
        note_label.setStyleSheet("color: #94A3B8; font-size: 11px; padding: 6px 10px; background-color: #070C12; border-radius: 6px;")
        layout.addWidget(note_label)

        action_row = QHBoxLayout()
        import_now_btn = QPushButton("📁 Import Service Account JSON Now...")
        import_now_btn.setStyleSheet("""
            QPushButton {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #0D9488, stop:1 #14B8A6);
                color: #FFFFFF;
                font-weight: bold;
                border: none;
                border-radius: 6px;
                padding: 6px 14px;
            }
            QPushButton:hover {
                background: #0F766E;
            }
        """)
        import_now_btn.clicked.connect(lambda: [dialog.accept(), self.import_google_credentials_dialog()])

        folder_btn = QPushButton("📂 Open Credentials Folder")
        folder_btn.clicked.connect(self.open_credentials_folder)

        close_btn = QPushButton("Close")
        close_btn.clicked.connect(dialog.accept)

        action_row.addWidget(import_now_btn)
        action_row.addWidget(folder_btn)
        action_row.addStretch(1)
        action_row.addWidget(close_btn)
        layout.addLayout(action_row)

        dialog.exec()

    def clear_text_windows(self) -> None:
        self._latency_samples.clear()
        self.latvian_text.clear()
        self.english_text.clear()
        self.russian_text.clear()
        self.latency_label.setText("⚡ Latency: --")
        self.log_status("Cleared transcription and translation windows.")

    def clear_activity_log(self) -> None:
        self.log.clear()
        self._activity_log_count = 0
        self._update_activity_log_title()

    def _update_activity_log_title(self) -> None:
        if not hasattr(self, 'activity_log_toggle'):
            return
        arrow = "▴" if self.activity_log_toggle.isChecked() else "▾"
        self.activity_log_toggle.setText(f"📋  Activity Log ({self._activity_log_count})  {arrow}")

    def _set_activity_log_expanded(self, expanded: bool) -> None:
        self.log.setVisible(expanded)
        self.clear_log_btn.setVisible(expanded)
        collapsed_height = self.activity_log_toggle.sizeHint().height() + 8
        self.log_box.setMaximumHeight(16777215 if expanded else collapsed_height)
        self.log_box.updateGeometry()
        self._update_activity_log_title()

    def uninstall_setup(self) -> None:
        options = self._choose_uninstall_options()
        if not options:
            return

        if self.engine:
            self.stop()

        from .config import project_root as resolve_project_root
        project_root = resolve_project_root()
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
        from .config import project_root as resolve_project_root
        project_root = resolve_project_root()
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
        message = html.escape(safe_error(message))
        active = self.engine is not None and not self._is_paused and not self._stopping
        if active and message.startswith(("Checking Gemini access", "Translation blocked", "Waiting for Gemini")):
            label = "Waiting" if message.startswith("Waiting") else ("Checking" if message.startswith("Checking") else "Blocked")
            self.status_pill.setText(label)
            self.status_pill.setToolTip(message)
            self.status_pill.setStyleSheet("color: #F59E0B; font-size: 12px; font-weight: bold;")
        elif active and (message.startswith(("Listening:", "Resumed listening:")) or (message.startswith("Segment") and ": Gemini " in message)):
            self.status_pill.setText("✓ LIVE")
            self.status_pill.setToolTip("Translation is running.")
            self.status_pill.setStyleSheet("color: #34D399; font-size: 12px; font-weight: bold;")
        if message == "Startup error.":
            self.stop()
        # Append detailed message to Activity Log with colored bullets
        bullet = "●"
        if "ERROR" in message or "failed" in message.lower():
            bullet_colored = f'<span style="color:#EF4444;">{bullet}</span>'
        elif "warning" in message.lower() or "403" in message or "429" in message:
            bullet_colored = f'<span style="color:#F59E0B;">{bullet}</span>'
        elif "successful" in message.lower() or "ready" in message.lower():
            bullet_colored = f'<span style="color:#34D399;">{bullet}</span>'
        else:
            bullet_colored = f'<span style="color:#22D3EE;">{bullet}</span>'

        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log.appendHtml(f'<span style="color:#64748B;">[{timestamp}]</span> {bullet_colored} {message}')
        self._activity_log_count = min(self._activity_log_count + 1, self.log.maximumBlockCount())
        self._update_activity_log_title()
        if "TRANSLATION" in message or "COST" in message or "OPENAI" in message or "GEMINI" in message:
            self._update_fallback_cost_ui()

        # Show concise summary in top header status_label
        summary = message
        if len(summary) > 35:
            if ":" in summary:
                summary = summary.split(":")[0].strip()
            if len(summary) > 35:
                summary = summary[:32] + "..."
        self.status_label.setText(summary)

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

    def _get_active_gemini_working_model(self) -> str | None:
        if self.engine and hasattr(self.engine, "_translator"):
            return getattr(self.engine._translator, "_working_model", None)
        return None

    def _get_last_gemini_call_time(self) -> float:
        if self.engine and hasattr(self.engine, "_translator"):
            return self.engine._translator.get_last_gemini_call_time()
        return 0.0

    def _record_gemini_call_time(self, timestamp: float) -> None:
        if self.engine and hasattr(self.engine, "_translator"):
            self.engine._translator.record_gemini_call_time(timestamp)

    def _update_fallback_cost_ui(self) -> None:
        if self.engine and hasattr(self.engine, "_translator"):
            stats = self.engine._translator.circuit_breaker.get_summary_stats()
            cost = stats.get("estimated_cost_usd", 0.0)
            reqs = stats.get("total_requests", 0)
            toks = stats.get("total_tokens", 0)
            self.fallback_cost_label.setText(
                f"Est. Fallback Cost: ${cost:.4f} | Fallback Requests: {reqs} | Tokens: {toks:,}"
            )

    def set_latency(self, latency: float) -> None:
        self._latency_samples.append(latency)
        count = len(self._latency_samples)
        avg = sum(self._latency_samples) / count
        sorted_samples = sorted(self._latency_samples)
        p95_idx = max(0, (95 * count + 99) // 100 - 1)
        p95 = sorted_samples[p95_idx]
        latest = f"{latency:.1f}".rstrip('0').rstrip('.')
        lat_text = f"⚡ Latency: {latest}s(avg: {avg:.1f}s)"
        self.latency_label.setText(lat_text)
        self.latency_label.setToolTip(
            f"Oldest captured audio to synthesized speech queued for output. "
            f"Latest: {latency:.1f}s; average: {avg:.1f}s; p95: {p95:.1f}s. "
            "The Activity Log separates STT, translation, synthesis and playback waiting.")

    def set_input_level(self, rms: float, peak: float) -> None:
        level = max(0, min(100, int(max(rms * 500, peak * 120))))
        self.input_level.setText(f"🎤 Mic: {level}%")

    def append_transcript(self, text: str) -> None:
        append_live_text(self.latvian_text, text)

    def _receive_session_update(self, sid, kind, payload):
        visible_sid = self.session_manager.current_session_id or self.session_manager.last_completed_session_id
        if sid != visible_sid:
            return
        if kind == 'transcript':
            self.append_transcript(payload)
        elif kind == 'translation':
            self.append_translation(*payload)
        elif kind == 'latency':
            self.set_latency(payload)
        elif kind == 'level':
            self.set_input_level(*payload)

    def append_translation(self, language: str, text: str) -> None:
        if language == "en":
            append_live_text(self.english_text, text)
        elif language == self.secondary_language_combo.currentData():
            append_live_text(self.russian_text, text)

    def _confirm_secondary_voice(self, config) -> bool:
        language = self.secondary_language_combo.currentData()
        if not self.russian_enabled.isChecked() or not config.google_application_credentials:
            return True
        voice = default_voice(config, language)
        if language in self._voice_settings or '-Chirp3-HD-' not in voice:
            return True
        from .services import TextToSpeech
        if QMessageBox.question(self, 'Cloud voice for ' + LANGUAGE_NAMES[language],
                f'This language uses {voice}.\n{TextToSpeech.voice_price(voice)}.\n'
                'Use this voice for the selected audio translation?',
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return False
        self._voice_settings[language] = {'voice': voice, 'rate': 1.0, 'pitch': 0, 'gain': 0}
        self._save_user_settings()
        return True

    def _secondary_language_changed(self, _index: int = -1) -> None:
        language = LANGUAGE_NAMES.get(self.secondary_language_combo.currentData(), 'selected language')
        self.russian_enabled.setText(f"Enable {language} Translation & Speech Output")
        self.russian_text.clear()
        self._sync_output_state()
        self._save_user_settings()

    def _sync_output_state(self):
        language = LANGUAGE_NAMES.get(self.secondary_language_combo.currentData(), 'Selected')
        enabled = self.russian_enabled.isChecked()
        self.secondary_dashboard_toggle.blockSignals(True)
        self.secondary_dashboard_toggle.setChecked(enabled)
        self.secondary_dashboard_toggle.setText(f"Enable {language} output")
        self.secondary_dashboard_toggle.blockSignals(False)
        self.russian_text.setPlaceholderText(
            'Translation will appear here…' if enabled else
            f'{language} output is off. Enable it below before starting a session.')
        self.english_text.setPlaceholderText(
            'Translation will appear here…' if self.english_enabled.isChecked() else
            'English output is off. Enable it in Audio Levels & Languages before starting a session.')

    def log_error(self, message: str) -> None:
        message = html.escape(safe_error(message))
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log.appendHtml(f'<span style="color:#64748B;">[{timestamp}]</span> <span style="color:#EF4444;">● ERROR: {message}</span>')

    def log_status(self, message: str) -> None:
        self.set_status(message)

    def open_sermon_context_dialog(self) -> None:
        dlg = SermonContextDialog(self.context_manager, self)
        result = dlg.exec()
        if result == QDialog.Accepted and not self.engine:
            self._context_staged_for_next_session = self.context_manager.is_active
        elif not self.context_manager.is_active:
            self._context_staged_for_next_session = False
        self.log_status('[CONTEXT] loaded enabled=' + str(self.context_manager.is_active).lower() +
                        ' parsed=true stage=ready_for_stt_and_translation '
                        f'processing_ms={self.context_manager.processing_ms:.2f} supplied=false influence=unknown')

    def _begin_session_context(self):
        staged = self._context_staged_for_next_session and self.context_manager.is_active
        if not staged:
            self.context_manager.clear()
        self._context_staged_for_next_session = False
        self.log_status('[CONTEXT] new_session prepared_active=' + str(bool(staged)).lower() +
                        ' reason=' + ('explicitly_staged' if staged else 'reset') +
                        '; STT context_available=' + str(bool(staged)).lower() + ' supplied=false influence=unknown')

    def open_export_transcript_dialog(self) -> None:
        # Retrieve segments from current active session or database
        sid = self.session_manager.current_session_id
        if sid:
            sess = self.session_manager.get_session(sid)
            segs = self.session_manager.get_segments(sid)
        else:
            sessions = self.session_manager.list_sessions()
            sess = sessions[0] if sessions else None
            segs = self.session_manager.get_segments(sess.session_id) if sess else []

        if not sess:
            QMessageBox.information(self, 'No Transcripts', 'No saved or active session transcripts found.')
            return

        dlg = TranscriptExportDialog(sess, segs, self)
        dlg.exec()

    def open_ai_summary_dialog(self) -> None:
        if self._stopping:
            QMessageBox.information(self, 'Finishing Session', 'Captured speech is still being saved. Open AI Summary when Ending has finished.')
            return
        self.summarizer.config = self._active_config()
        sid = self.session_manager.summary_session_id()
        if sid:
            sess = self.session_manager.get_session(sid)
            segs = self.session_manager.get_segments(sid)
        else:
            sess, segs = None, []

        if not sess:
            QMessageBox.information(self, 'No Saved Session', 'No active or completed sermon is available. Start a session or open History to select a saved sermon.')
            return

        dlg = AISummaryDialog(self.summarizer.fork(), sess, segs, self.session_manager, self, sermon_context=self.context_manager)
        dlg.exec()

    def _open_voice_settings(self):
        from .voice_dialog import VoiceSettingsDialog
        dialog = VoiceSettingsDialog(self._active_config(), self._voice_settings,
            lambda language: (self.english_output_combo if language == "en" else self.russian_output_combo).currentData(),
            lambda language: (self.english_volume if language == "en" else self.russian_volume).value() / 100.0,
            self)
        if dialog.exec() == QDialog.Accepted:
            self._voice_settings = dialog.settings
            if self.engine:
                self.engine.config = replace(self.engine.config, voice_settings=dialog.settings)
            self._save_user_settings()
            self.log_status("Voice preferences saved. They apply to the next speech segment synthesized; already queued audio keeps its voice.")

    def open_session_history_dialog(self) -> None:
        self.summarizer.config = self._active_config()
        dlg = SessionHistoryDialog(self.session_manager, self.summarizer, self)
        dlg.exec()

    def _check_crash_recovery(self) -> None:
        crashed = self.session_manager.get_most_recent_crashed_session()
        if crashed:
            QTimer.singleShot(800, lambda: self._prompt_crash_recovery(crashed))

    def _prompt_crash_recovery(self, session: SessionRecord) -> None:
        segs = self.session_manager.get_segments(session.session_id)
        if not segs:
            return
        ans = QMessageBox.information(
            self,
            "Recovered Sermon Session Available",
            f"A previous sermon session ('{session.title}') with {len(segs)} segments was preserved.\n\n"
            f"Would you like to open Session History to review, export, or summarize it?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.Yes,
        )
        if ans == QMessageBox.Yes:
            self.open_session_history_dialog()

    def test_providers_health(self) -> None:
        self.provider_health_label.setText("Testing providers in background...")
        self.test_providers_button.setEnabled(False)
        cfg = self._active_config()

        import threading
        def worker():
            results = []

            from .glossary import Glossary
            from .services import Translator, OpenAITranscriber, TextToSpeech

            # 1. Test Gemini Translation
            translator = Translator(cfg, Glossary({}, {}))
            _, gem_msg, _ = translator.test_gemini()
            results.append(gem_msg)

            # 2. Test OpenAI Transcription
            transcriber = OpenAITranscriber(cfg, Glossary({}, {}), lambda _: None)
            _, stt_msg, _ = transcriber.test_transcription()
            results.append(f"OpenAI STT: {stt_msg}")

            # 3. Test Selected OpenAI Translation Fallback Model
            if cfg.openai_fallback_enabled:
                fb_model = cfg.openai_fallback_model or "gpt-4o-mini"
                _, fb_msg, _ = translator.test_openai_fallback_model(fb_model)
                results.append(fb_msg)
            else:
                results.append("OpenAI translation test skipped: paid fallback is OFF")

            # 4. Test Google Cloud TTS
            tts = TextToSpeech(cfg)
            _, tts_msg, _ = tts.test_tts()
            results.append(tts_msg)

            summary_text = " | ".join(results)
            self.signals.provider_health.emit(summary_text)

        threading.Thread(target=worker, daemon=True).start()

    def _on_health_test_done(self, summary: str) -> None:
        self.provider_health_label.setText(summary)
        self.test_providers_button.setEnabled(True)
        self.log_status(f"Provider Diagnostic: {summary}")


def main() -> int:
    if "--smoke-test" in sys.argv:
        from .smoke import run_smoke
        return run_smoke(Path(sys.argv[sys.argv.index("--smoke-test") + 1]))
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())



