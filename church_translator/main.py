from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import threading
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QObject, Qt, Signal, QUrl
from PySide6.QtGui import QDesktopServices, QIcon, QFont, QPixmap, QImage, QPainter, QPen, QColor
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
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
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .audio import AudioDevice, list_audio_devices
from .config import (
    GEMINI_MODELS,
    app_data_dir,
    inspect_service_account_file,
    load_config,
    load_user_settings,
    project_root,
    save_user_settings,
)
from .engine import EngineSettings, TranslationEngine


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
        self.resize(1280, 860)
        ensure_check_icon()
        self.setStyleSheet(DARK_TEAL_DASHBOARD_QSS)

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
        main_widget = QWidget()
        content_layout = QVBoxLayout(main_widget)
        content_layout.setContentsMargins(12, 12, 12, 12)
        content_layout.setSpacing(8)

        # Main Tabs Container (Positioned at Very Top)
        self.tabs = QTabWidget()

        # Small Text-only Status Readouts (Top Right Corner of Tab Bar)
        corner_widget = QWidget()
        corner_widget.setStyleSheet("margin-bottom: 3px;")
        corner_layout = QHBoxLayout(corner_widget)
        corner_layout.setContentsMargins(0, 0, 14, 4)
        corner_layout.setSpacing(10)

        self.status_pill = QLabel("○ IDLE")
        self.status_pill.setStyleSheet("color: #94A3B8; font-size: 12px; font-weight: bold;")

        self.latency_label = QLabel("⚡ Latency: --")
        self.latency_label.setStyleSheet("color: #22D3EE; font-size: 12px; font-weight: bold;")

        self.input_level = QLabel("🎤 Mic Level: 0%")
        self.input_level.setStyleSheet("color: #60A5FA; font-size: 12px; font-weight: bold;")

        sep1 = QLabel("|")
        sep1.setStyleSheet("color: #334155; font-weight: bold;")
        sep2 = QLabel("|")
        sep2.setStyleSheet("color: #334155; font-weight: bold;")

        corner_layout.addWidget(self.status_pill)
        corner_layout.addWidget(sep1)
        corner_layout.addWidget(self.latency_label)
        corner_layout.addWidget(sep2)
        corner_layout.addWidget(self.input_level)

        self.tabs.setCornerWidget(corner_widget, Qt.TopRightCorner)

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
        self.start_button.setMinimumWidth(130)

        self.stop_button = QPushButton("⏹ Stop")
        self.stop_button.setObjectName("stopButton")
        self.stop_button.setFixedHeight(30)
        self.stop_button.setMinimumWidth(100)

        self.refresh_button = QPushButton("🔄 Refresh Devices")
        self.refresh_button.setFixedHeight(30)

        self.clear_text_button = QPushButton("🧹 Clear Text")
        self.clear_text_button.setFixedHeight(30)

        self.uninstall_button = QPushButton("⚙️ Uninstall Setup")
        self.uninstall_button.setFixedHeight(30)

        self.status_label = QLabel("Idle")
        self.status_label.setWordWrap(True)
        self.status_label.setMaximumWidth(220)
        self.status_label.setStyleSheet("color: #94A3B8; font-size: 11px; font-style: italic;")

        self.setup_progress = QProgressBar()
        self.setup_progress.setRange(0, 0)
        self.setup_progress.setVisible(False)
        self.setup_progress.setMaximumWidth(120)

        controls.addWidget(self.start_button)
        controls.addWidget(self.stop_button)
        controls.addWidget(self.refresh_button)
        controls.addWidget(self.clear_text_button)
        controls.addWidget(self.uninstall_button)
        controls.addStretch(1)
        controls.addWidget(self.setup_progress)
        controls.addWidget(self.status_label)

        general_layout.addLayout(controls)

        # 3 Hero Cards Layout
        text_grid = QGridLayout()
        text_grid.setSpacing(14)

        self.latvian_text = self._read_only_text()
        self.english_text = self._read_only_text()
        self.russian_text = self._read_only_text("Translation will appear here...")

        latvian_box = self._create_hero_card("LV", "Latvian Speech", "(Source Transcribed)", self.latvian_text, "#0D9488", "#14B8A6", "🌐 Source Language")
        english_box = self._create_hero_card("EN", "English Audio Translation", "", self.english_text, "#059669", "#10B981", "🔊 Target Language")
        russian_box = self._create_hero_card("RU", "Russian Audio Translation", "", self.russian_text, "#D97706", "#F59E0B", "🔊 Target Language")

        text_grid.addWidget(latvian_box, 0, 0)
        text_grid.addWidget(english_box, 0, 1)
        text_grid.addWidget(russian_box, 0, 2)
        general_layout.addLayout(text_grid, 1)

        # Activity Log Drawer
        log_box = QGroupBox()
        log_box.setStyleSheet("""
            QGroupBox {
                background-color: #090F15;
                border: 1px solid #16222F;
                border-radius: 12px;
                margin-top: 0px;
                padding: 10px;
            }
        """)
        log_layout = QVBoxLayout(log_box)
        log_layout.setContentsMargins(10, 8, 10, 8)
        log_layout.setSpacing(6)

        log_header_box = QHBoxLayout()
        log_title = QLabel("📋 Activity Log")
        log_title.setStyleSheet("font-weight: bold; font-size: 13px; color: #F8FAFC;")
        
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

        log_header_box.addWidget(log_title)
        log_header_box.addStretch(1)
        log_header_box.addWidget(self.clear_log_btn)
        log_layout.addLayout(log_header_box)

        self.log = self._read_only_text()
        self.log.setMaximumBlockCount(600)
        self.log.setMaximumHeight(130)
        log_layout.addWidget(self.log)

        general_layout.addWidget(log_box)
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
        self.chunk_duration_combo = QComboBox()
        self.chunk_duration_combo.addItem("4.0 - 5.0 seconds (Recommended)", "4_5")
        self.chunk_duration_combo.addItem("4.5 - 5.5 seconds (Longer phrases)", "45_55")
        self.chunk_duration_combo.addItem("3.5 - 4.5 seconds (Faster turnaround)", "35_45")
        audio_layout.addRow("🎤 Input Device (Microphone):", self.input_combo)
        audio_layout.addRow("🔊 English Speaker Output:", self.english_output_combo)
        audio_layout.addRow("🔊 Russian Speaker Output:", self.russian_output_combo)
        audio_layout.addRow("⏱️ Audio Chunk Duration:", self.chunk_duration_combo)
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
            ("gpt-4o-mini-transcribe - fast & accurate", "gpt-4o-mini-transcribe"),
            ("gpt-4o-transcribe - highest accuracy", "gpt-4o-transcribe"),
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
        lang_layout.addRow("🇷🇺 Russian:", self.russian_enabled)
        lang_layout.addRow("🔊 Russian Audio Volume:", ru_vol_box)
        self.tabs.addTab(lang_tab, "🔊 Audio Levels & Languages")

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
        footer_text: str,
    ) -> QGroupBox:
        box = QGroupBox()
        box.setStyleSheet("""
            QGroupBox {
                background-color: #0F1A24;
                border: 1px solid #1E2D3D;
                border-radius: 12px;
                margin-top: 0px;
                padding: 12px;
            }
        """)
        layout = QVBoxLayout(box)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        # Card Top Header
        top_row = QHBoxLayout()
        top_row.setSpacing(10)

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
        title_box.setSpacing(2)
        t_label = QLabel(title)
        t_label.setStyleSheet("font-weight: bold; font-size: 13px; color: #F8FAFC;")
        title_box.addWidget(t_label)
        if subtitle:
            sub_label = QLabel(subtitle)
            sub_label.setStyleSheet("font-size: 11px; color: #64748B;")
            title_box.addWidget(sub_label)
        top_row.addLayout(title_box)
        top_row.addStretch(1)

        layout.addLayout(top_row)

        # Native Audio Waveform Visualizer Widget
        layout.addWidget(self._create_waveform_widget(accent_color))

        # Text Box Area
        layout.addWidget(text_widget, 1)

        # Footer Tag
        footer = QLabel(footer_text)
        footer.setStyleSheet(f"font-size: 11px; font-weight: 600; color: {accent_color}; margin-top: 4px;")
        layout.addWidget(footer)

        return box

    def _create_waveform_widget(self, color_hex: str) -> QWidget:
        w = QWidget()
        w.setFixedHeight(34)
        layout = QHBoxLayout(w)
        layout.setContentsMargins(0, 4, 0, 4)
        layout.setSpacing(4)
        layout.setAlignment(Qt.AlignCenter)

        bar_heights = [8, 14, 22, 12, 26, 18, 14, 24, 10, 22, 16, 12, 24, 18, 12, 20, 14, 10, 18, 12, 8]
        for h in bar_heights:
            bar = QFrame()
            bar.setFixedWidth(3)
            bar.setFixedHeight(h)
            bar.setStyleSheet(f"background-color: {color_hex}; border-radius: 1px;")
            layout.addWidget(bar)
        return w

    def _read_only_text(self, placeholder: str = "") -> QPlainTextEdit:
        text = QPlainTextEdit()
        text.setReadOnly(True)
        text.setMaximumBlockCount(400)
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
        self.stop_button.clicked.connect(self.stop)
        self.refresh_button.clicked.connect(self.refresh_devices)
        self.clear_text_button.clicked.connect(self.clear_text_windows)
        self.uninstall_button.clicked.connect(self.uninstall_setup)
        self.set_gemini_key_button.clicked.connect(self.set_gemini_key_dialog)
        self.get_gemini_key_button.clicked.connect(lambda: QDesktopServices.openUrl(QUrl("https://aistudio.google.com/app/apikey")))
        self.set_openai_key_button.clicked.connect(self.set_openai_key_dialog)
        self.get_openai_key_button.clicked.connect(lambda: QDesktopServices.openUrl(QUrl("https://platform.openai.com/api-keys")))
        self.import_google_creds_button.clicked.connect(self.import_google_credentials_dialog)
        self.open_creds_folder_button.clicked.connect(self.open_credentials_folder)
        self.google_tts_guide_button.clicked.connect(self.show_google_tts_guide_dialog)
        self.clear_google_creds_button.clicked.connect(self.clear_google_credentials)
        self.install_local_whisper_button.clicked.connect(self.install_local_whisper)
        self.speech_backend_combo.currentIndexChanged.connect(self._sync_speech_controls)
        self.english_volume.valueChanged.connect(lambda v: self.english_vol_label.setText(f"{v}%"))
        self.russian_volume.valueChanged.connect(lambda v: self.russian_vol_label.setText(f"{v}%"))

        for combo in (
            self.input_combo,
            self.english_output_combo,
            self.russian_output_combo,
            self.chunk_duration_combo,
            self.speech_backend_combo,
            self.gemini_model_combo,
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
        if not active_config.gemini_api_key and not active_config.google_application_credentials:
            answer = QMessageBox.question(
                self,
                "Gemini API Key Required",
                "No Gemini API key was found. Would you like to enter your Gemini API key now?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.Yes,
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
            timing = self.user_settings.get("timing", {})
            self._set_combo_data(self.chunk_duration_combo, timing.get("chunk_profile", "4_5"))
            recognition = self.user_settings.get("recognition", {})
            self._set_combo_data(self.speech_backend_combo, recognition.get("backend"))
            self._set_combo_data(self.gemini_model_combo, recognition.get("gemini_model"))
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
            "timing": {
                "chunk_profile": self.chunk_duration_combo.currentData() or "4_5",
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
                "gemini_model": self.gemini_model_combo.currentData(),
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
        latest = load_config()
        self.config = latest
        chunk_profile = self.chunk_duration_combo.currentData() or "4_5"
        if chunk_profile == "45_55":
            chunk_s, min_chunk_s, early_flush_s = 5.5, 4.5, 1.00
        elif chunk_profile == "35_45":
            chunk_s, min_chunk_s, early_flush_s = 4.5, 3.5, 0.80
        else:  # "4_5" default
            chunk_s, min_chunk_s, early_flush_s = 5.0, 4.0, 0.90
        return replace(
            latest,
            chunk_seconds=chunk_s,
            min_chunk_seconds=min_chunk_s,
            early_flush_silence_seconds=early_flush_s,
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
            free_tier_mode=True,
        )

    def _log_configuration_warnings(self, config=None) -> None:
        config = config or self.config
        if not config.gemini_api_key and not config.google_application_credentials:
            self.log_error(
                "No GEMINI_API_KEY or GOOGLE_APPLICATION_CREDENTIALS found. Translation calls will likely fail."
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
        self.gemini_model_combo.setEnabled(not running)
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

        if running:
            self.start_button.setText("✓ Started (Active)")
            self.stop_button.setText("⏹ Stop Translation")
            self.status_pill.setText("✓ LIVE TRANSLATING")
            self.status_pill.setStyleSheet("color: #34D399; font-size: 12px; font-weight: bold;")
        else:
            self.start_button.setText("▶ Start Translation")
            self.stop_button.setText("⏹ Stop")
            self.status_pill.setText("○ IDLE")
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
        env_path.write_text("\n".join(new_lines), encoding="utf-8")

    def _remove_env_var(self, key: str) -> None:
        os.environ.pop(key, None)
        env_path = project_root() / ".env"
        if not env_path.exists():
            return
        content = env_path.read_text(encoding="utf-8")
        lines = [line for line in content.splitlines() if not line.startswith(f"{key}=")]
        env_path.write_text("\n".join(lines), encoding="utf-8")

    def _update_credentials_status_ui(self) -> None:
        gemini_key = os.getenv("GEMINI_API_KEY") or self.config.gemini_api_key
        if gemini_key:
            masked = f"{gemini_key[:6]}...{gemini_key[-4:]}" if len(gemini_key) >= 12 else "Configured"
            self.gemini_status_label.setText(f"✅ Active ({masked})")
            self.gemini_status_label.setStyleSheet("color: #34D399; font-size: 11px; font-weight: bold;")
        else:
            self.gemini_status_label.setText("⚠️ Key missing (Required for Gemini translation)")
            self.gemini_status_label.setStyleSheet("color: #FBBF24; font-size: 11px; font-weight: bold;")

        openai_key = os.getenv("OPENAI_API_KEY") or self.config.openai_api_key
        if openai_key:
            masked = f"{openai_key[:6]}...{openai_key[-4:]}" if len(openai_key) >= 12 else "Configured"
            self.openai_status_label.setText(f"✅ Active ({masked})")
            self.openai_status_label.setStyleSheet("color: #34D399; font-size: 11px; font-weight: bold;")
        else:
            self.openai_status_label.setText("⚠️ Key missing (Required for OpenAI transcription)")
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
            self.clear_google_creds_button.setEnabled(True)
        else:
            self.tts_status_label.setText("ℹ️ Free Instant TTS Active (Google Service Account JSON not loaded)")
            self.tts_status_label.setStyleSheet(
                "color: #38BDF8; font-size: 11px; font-weight: bold; padding: 4px 8px; "
                "background-color: #082F49; border: 1px solid #0284C7; border-radius: 6px;"
            )
            self.clear_google_creds_button.setEnabled(False)

    def set_gemini_key_dialog(self) -> None:
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
            "Gemini is used for fast and accurate real-time translation into English and Russian.\n"
            "You can generate a free API key instantly in Google AI Studio."
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
                if self.engine:
                    from .services import Translator
                    self.engine.config = replace(self.engine.config, gemini_api_key=new_key)
                    self.engine._translator = Translator(self.engine.config, self.engine._glossary, status_cb=self.signals.status.emit)
                self._update_credentials_status_ui()
                self.log_status("[GEMINI] Gemini API key updated and saved to .env file.")
                QMessageBox.information(self, "API Key Saved", "Gemini API key saved successfully!")

    def set_openai_key_dialog(self) -> None:
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
            "OpenAI API is used for fast cloud-based Latvian speech-to-text recognition.\n"
            "You can create an API key in the OpenAI Developer Platform."
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
                if self.engine:
                    from .services import create_transcriber
                    self.engine.config = replace(self.engine.config, openai_api_key=new_key)
                    self.engine._transcriber = create_transcriber(self.engine.config, self.engine._glossary, self.signals.status.emit)
                self._update_credentials_status_ui()
                self.log_status("[OPENAI] OpenAI API key updated and saved to .env file.")
                QMessageBox.information(self, "API Key Saved", "OpenAI API key saved successfully!")

    def import_google_credentials_dialog(self) -> None:
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

            if self.engine:
                with self.engine._tts_lock:
                    self.engine.config = replace(self.engine.config, google_application_credentials=str(dest_path.resolve()))
                    from .services import TextToSpeech, Translator
                    self.engine._tts = TextToSpeech(self.engine.config, status_cb=self.signals.status.emit)
                    self.engine._translator = Translator(self.engine.config, self.engine._glossary, status_cb=self.signals.status.emit)

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
        reply = QMessageBox.question(
            self,
            "Clear Google Credentials",
            "Are you sure you want to remove the Google Cloud credentials link?\n\nThe app will switch back to the built-in free instant speech synthesis fallback.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        self._remove_env_var("GOOGLE_APPLICATION_CREDENTIALS")
        self.config = replace(self.config, google_application_credentials=None)
        if self.engine:
            with self.engine._tts_lock:
                self.engine.config = replace(self.engine.config, google_application_credentials=None)
                from .services import TextToSpeech
                self.engine._tts = TextToSpeech(self.engine.config, status_cb=self.signals.status.emit)

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
        self.latvian_text.clear()
        self.english_text.clear()
        self.russian_text.clear()
        self.latency_label.setText("⚡ Latency: --")
        self.log_status("Cleared transcription and translation windows.")

    def clear_activity_log(self) -> None:
        self.log.clear()

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

    def set_latency(self, latency: float) -> None:
        lat_text = f"⚡ Latency: {latency:.1f}s"
        self.latency_label.setText(lat_text)

    def set_input_level(self, rms: float, peak: float) -> None:
        level = min(100, int(max(rms * 500, peak * 120)))
        self.input_level.setText(f"🎤 Mic Level: {level}%")

    def append_transcript(self, text: str) -> None:
        self.latvian_text.appendPlainText(text)

    def append_translation(self, language: str, text: str) -> None:
        if language == "en":
            self.english_text.appendPlainText(text)
        elif language == "ru":
            self.russian_text.appendPlainText(text)

    def log_error(self, message: str) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log.appendHtml(f'<span style="color:#64748B;">[{timestamp}]</span> <span style="color:#EF4444;">● ERROR: {message}</span>')

    def log_status(self, message: str) -> None:
        self.set_status(message)


def main() -> int:
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())



