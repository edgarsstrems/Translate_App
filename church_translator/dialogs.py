"""Custom Dialog Windows for Sermon Context, Transcript Export, AI Summary, and Session History.

Preserves the existing dark teal church dashboard theme (#0B0F17, #111827, #0D9488, #14B8A6).
"""
from __future__ import annotations

import os
import shutil
import time
from pathlib import Path
from typing import Optional, List

from PySide6.QtCore import Qt, QTimer, QObject, Signal, QThread
from PySide6.QtGui import QDesktopServices, QFont
from PySide6.QtWidgets import (
    QDialog,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QPlainTextEdit,
    QLineEdit,
    QComboBox,
    QCheckBox,
    QFileDialog,
    QMessageBox,
    QProgressBar,
    QTableWidget,
    QTableWidgetItem,
    QHeaderView,
    QFrame,
    QGroupBox,
    QScrollArea,
    QStackedWidget,
    QWidget,
)

from .session_manager import SessionManager, SessionRecord, TranscriptSegment
from .transcript_exporter import TranscriptExporter
from .reliability import safe_error
from .summarizer import (
    SermonSummarizer,
    GeminiSummaryError,
    estimate_openai_summary_cost,
    StructuredSummary,
    is_json_response,
)
from .sermon_context import SermonContextManager
from .config import load_user_settings, save_user_settings


DIALOG_STYLESHEET = """
QDialog {
    background-color: #0B0F17;
    color: #F8FAFC;
    font-family: 'Segoe UI', system-ui, sans-serif;
    font-size: 13px;
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
    padding: 6px 14px;
    font-weight: 600;
    font-size: 12px;
}
QPushButton:hover {
    background-color: #334155;
    border-color: #475569;
}
QPushButton#primaryAction {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #0D9488, stop:1 #14B8A6);
    color: #FFFFFF;
    border: 1px solid #14B8A6;
    font-weight: bold;
}
QPushButton#primaryAction:hover {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #0F766E, stop:1 #0D9488);
}
QCheckBox {
    color: #F8FAFC;
    spacing: 8px;
}
QTableWidget {
    background-color: #070C12;
    border: 1px solid #1E293B;
    color: #F8FAFC;
    gridline-color: #1E293B;
    selection-background-color: #0D9488;
}
QHeaderView::section {
    background-color: #131E2B;
    color: #94A3B8;
    padding: 6px;
    border: 1px solid #1E2D3D;
    font-weight: bold;
}
"""


class SermonContextDialog(QDialog):
    """Dialog for viewing, pasting, importing, and saving pastor's prepared sermon context."""

    def __init__(self, context_manager: SermonContextManager, parent=None) -> None:
        super().__init__(parent)
        self.context_manager = context_manager
        self.setWindowTitle("📖 Sermon Context — Prepared Sermon Reference")
        self.resize(760, 600)
        self.setStyleSheet(DIALOG_STYLESHEET)
        self._build_ui()
        self._load_data()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        header = QLabel("📖 Pastor's Prepared Sermon Context")
        header.setStyleSheet("font-size: 16px; font-weight: bold; color: #22D3EE;")
        layout.addWidget(header)

        desc = QLabel(
            "Paste the pastor's prepared sermon notes, manuscript, or outline below. "
            "Prepared vocabulary and recent Latvian speech guide supported recognition models; "
            "matching topic and vocabulary guide translation. Only actual speech is transcribed. "
            "Apply before Start for the next sermon, or during a session. End Session clears live context."
        )
        desc.setWordWrap(True)
        desc.setStyleSheet("color: #94A3B8; font-size: 11px;")
        layout.addWidget(desc)

        # Title Row
        title_row = QHBoxLayout()
        title_lbl = QLabel("Sermon Topic / Title:")
        self.title_input = QLineEdit()
        self.title_input.setPlaceholderText("e.g. Ticība un uzticēšanās Dievam (Romiešiem 8)")
        title_row.addWidget(title_lbl)
        title_row.addWidget(self.title_input, 1)
        layout.addLayout(title_row)

        # Action Toolbar
        btn_row = QHBoxLayout()
        self.paste_btn = QPushButton("📋 Paste from Clipboard")
        self.paste_btn.clicked.connect(self._paste_clipboard)
        self.load_txt_btn = QPushButton("📄 Load TXT File...")
        self.load_txt_btn.clicked.connect(self._load_txt_file)
        self.load_docx_btn = QPushButton("📘 Load DOCX File...")
        self.load_docx_btn.clicked.connect(self._load_docx_file)
        self.clear_btn = QPushButton("🗑 Clear Context")
        self.clear_btn.clicked.connect(self._clear_context)

        btn_row.addWidget(self.paste_btn)
        btn_row.addWidget(self.load_txt_btn)
        btn_row.addWidget(self.load_docx_btn)
        btn_row.addWidget(self.clear_btn)
        btn_row.addStretch(1)
        layout.addLayout(btn_row)

        # Large Text Area
        self.text_edit = QPlainTextEdit()
        self.text_edit.setPlaceholderText("Paste or import prepared sermon text here...")
        self.text_edit.textChanged.connect(self._update_counters)
        layout.addWidget(self.text_edit, 1)

        # Footer Status and Counters
        footer_row = QHBoxLayout()
        self.status_pill = QLabel("○ Inactive")
        self.status_pill.setStyleSheet("color: #94A3B8; font-weight: bold;")
        self.counter_label = QLabel("0 words | 0 characters")
        self.counter_label.setStyleSheet("color: #64748B; font-size: 11px;")

        self.save_btn = QPushButton("💾 Save Context")
        self.save_btn.setObjectName("primaryAction")
        self.save_btn.clicked.connect(self._save_context)

        self.close_btn = QPushButton("Close")
        self.close_btn.clicked.connect(self.accept)

        footer_row.addWidget(self.status_pill)
        footer_row.addWidget(self.counter_label)
        footer_row.addStretch(1)
        footer_row.addWidget(self.save_btn)
        footer_row.addWidget(self.close_btn)
        layout.addLayout(footer_row)

    def _load_data(self) -> None:
        self.title_input.setText(self.context_manager.title)
        self.text_edit.setPlainText(self.context_manager.raw_text)
        self._update_counters()

    def _update_counters(self) -> None:
        text = self.text_edit.toPlainText().strip()
        words = len(text.split()) if text else 0
        chars = len(text)
        self.counter_label.setText(f"{words:,} words | {chars:,} characters")
        if text:
            self.status_pill.setText("✓ Context Loaded")
            self.status_pill.setStyleSheet("color: #34D399; font-weight: bold;")
        else:
            self.status_pill.setText("○ Inactive")
            self.status_pill.setStyleSheet("color: #94A3B8; font-weight: bold;")

    def _paste_clipboard(self) -> None:
        from PySide6.QtGui import QGuiApplication
        clip = QGuiApplication.clipboard()
        text = clip.text()
        if text:
            self.text_edit.setPlainText(text)
            self._update_counters()

    def _load_txt_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Load Sermon TXT File", "", "Text Files (*.txt);;All Files (*.*)")
        if path:
            try:
                content = Path(path).read_text(encoding="utf-8", errors="replace")
                self.text_edit.setPlainText(content)
                if not self.title_input.text():
                    self.title_input.setText(Path(path).stem.replace("_", " "))
                self._update_counters()
            except Exception as exc:
                QMessageBox.critical(self, "Error", f"Could not read TXT file: {exc}")

    def _load_docx_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Load Sermon DOCX File", "", "Word Documents (*.docx);;All Files (*.*)")
        if path:
            try:
                full_text, document_title = SermonContextManager.read_docx(Path(path))
                self.text_edit.setPlainText(full_text)
                if not self.title_input.text():
                    self.title_input.setText(document_title or Path(path).stem.replace("_", " "))
                self._update_counters()
            except Exception as exc:
                QMessageBox.critical(self, "Error", f"Could not read DOCX file: {exc}")

    def _clear_context(self) -> None:
        if QMessageBox.question(self, "Clear Context", "Are you sure you want to clear the prepared sermon context?") == QMessageBox.Yes:
            self.text_edit.clear()
            self.title_input.clear()
            self.context_manager.clear()
            self._update_counters()

    def _save_context(self) -> None:
        text = self.text_edit.toPlainText().strip()
        title = self.title_input.text().strip()
        self.context_manager.set_text(text, title=title, save=True)
        self._update_counters()
        stats = self.context_manager.get_stats()
        QMessageBox.information(self, "Context Saved",
            f'Sermon context parsed and indexed: {stats["sections_count"]} sections, '
            f'{stats["word_count"]} words.\nVocabulary guidance is available for recognition and translation. '
            'The Activity Log records what each provider request actually receives.')
        self.accept()


class TranscriptExportDialog(QDialog):
    """Dialog for exporting sermon transcripts in TXT, PDF, DOCX, Markdown, or JSON."""

    def __init__(self, session: SessionRecord, segments: List[TranscriptSegment], parent=None) -> None:
        super().__init__(parent)
        self.session = session
        self.segments = segments
        self.setWindowTitle(f"📥 Export Sermon Transcript — {session.title}")
        self.resize(520, 420)
        self.setStyleSheet(DIALOG_STYLESHEET)
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(12)

        header = QLabel(f"📥 Export Transcript: {self.session.title}")
        header.setStyleSheet("font-size: 15px; font-weight: bold; color: #22D3EE;")
        layout.addWidget(header)

        meta = QLabel(f"Captured Segments: {len(self.segments)} | Duration: {int(self.session.duration_seconds // 60)} min")
        meta.setStyleSheet("color: #94A3B8; font-size: 11px;")
        layout.addWidget(meta)

        # Options Box
        grp = QGroupBox("Export Configuration")
        grp_layout = QVBoxLayout(grp)
        grp_layout.setSpacing(10)

        # Language Selection
        lang_row = QHBoxLayout()
        lang_lbl = QLabel("Languages to Include:")
        self.lang_combo = QComboBox()
        self.lang_combo.addItem("🌐 Latvian & English (Bilingual)", "bilingual")
        self.lang_combo.addItem("🇱🇻 Original Latvian Only", "lv")
        self.lang_combo.addItem("🇬🇧 English Translation Only", "en")
        if any(s.secondary_translation for s in self.segments):
            self.lang_combo.addItem("🌍 All Available Languages (LV + EN + Secondary)", "all")
        lang_row.addWidget(lang_lbl)
        lang_row.addWidget(self.lang_combo, 1)
        grp_layout.addLayout(lang_row)

        # Format Selection
        fmt_row = QHBoxLayout()
        fmt_lbl = QLabel("File Format:")
        self.fmt_combo = QComboBox()
        self.fmt_combo.addItem("📄 PDF Document (*.pdf)", "pdf")
        self.fmt_combo.addItem("📘 Word Document (*.docx)", "docx")
        self.fmt_combo.addItem("📝 Plain Text (*.txt)", "txt")
        self.fmt_combo.addItem("📋 Markdown (*.md)", "md")
        self.fmt_combo.addItem("⚙️ Structured Data (*.json)", "json")
        fmt_row.addWidget(fmt_lbl)
        fmt_row.addWidget(self.fmt_combo, 1)
        grp_layout.addLayout(fmt_row)

        # Checkboxes
        self.timestamps_cb = QCheckBox("Include timestamps for each segment")
        self.timestamps_cb.setChecked(True)
        self.title_cb = QCheckBox("Include sermon title and header metadata")
        self.title_cb.setChecked(True)
        grp_layout.addWidget(self.timestamps_cb)
        grp_layout.addWidget(self.title_cb)

        layout.addWidget(grp)

        # Buttons
        btn_row = QHBoxLayout()
        self.export_btn = QPushButton("💾 Export Transcript...")
        self.export_btn.setObjectName("primaryAction")
        self.export_btn.clicked.connect(self._do_export)

        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.clicked.connect(self.reject)

        btn_row.addStretch(1)
        btn_row.addWidget(self.cancel_btn)
        btn_row.addWidget(self.export_btn)
        layout.addLayout(btn_row)

    def _do_export(self) -> None:
        if not self.segments:
            QMessageBox.warning(self, "No Segments", "There are no transcript segments to export.")
            return

        fmt = self.fmt_combo.currentData()
        lang = self.lang_combo.currentData()
        include_ts = self.timestamps_cb.isChecked()
        include_title = self.title_cb.isChecked()

        # Generate default filename
        date_str = self.session.created_at.split()[0]
        safe_title = "".join(c for c in self.session.title if c.isalnum() or c in (" ", "-", "_")).strip()
        default_name = f"Sermon_{date_str}_{safe_title.replace(' ', '_')}_{lang}.{fmt}"

        filt = f"{fmt.upper()} Files (*.{fmt})"
        save_path, _ = QFileDialog.getSaveFileName(self, "Save Transcript", default_name, filt)
        if not save_path:
            return

        out_file = Path(save_path)
        try:
            if fmt == "txt":
                TranscriptExporter.export_to_txt(out_file, self.session, self.segments, lang, include_ts, include_title)
            elif fmt == "docx":
                TranscriptExporter.export_to_docx(out_file, self.session, self.segments, lang, include_ts, include_title)
            elif fmt == "pdf":
                TranscriptExporter.export_to_pdf(out_file, self.session, self.segments, lang, include_ts, include_title)
            elif fmt == "md":
                TranscriptExporter.export_to_markdown(out_file, self.session, self.segments, lang, include_ts, include_title)
            elif fmt == "json":
                TranscriptExporter.export_to_json(out_file, self.session, self.segments, lang, include_ts, include_title)

            QMessageBox.information(self, "Export Successful", f"Transcript saved successfully to:\n{out_file.name}")
            self.accept()
        except Exception as exc:
            QMessageBox.critical(self, "Export Error", f"Failed to export transcript: {exc}")


class SummaryWorkerSignals(QObject):
    """Qt signal bridge for thread-safe UI updates from the background summary worker."""
    progress = Signal(str, int, int)
    success = Signal(str, str)
    cancelled = Signal()
    gemini_error = Signal(str, bool)
    error = Signal(str)


class SummaryWorkerThread(QThread):
    """QThread running sermon summary generation safely off the Qt UI event loop."""

    def __init__(
        self,
        signals: SummaryWorkerSignals,
        summarizer: SermonSummarizer,
        segments: List[TranscriptSegment],
        language: str,
        sermon_title: str,
        sermon_context: str,
        provider: str,
    ) -> None:
        super().__init__()
        self.signals = signals
        self.summarizer = summarizer
        self.segments = segments
        self.language = language
        self.sermon_title = sermon_title
        self.sermon_context = sermon_context
        self.provider = provider
        self.is_cancelled = False

    def cancel(self) -> None:
        self.is_cancelled = True

    def run(self) -> None:
        try:
            res = self.summarizer.generate_summary(
                self.segments,
                language=self.language,
                sermon_title=self.sermon_title,
                sermon_context=self.sermon_context,
                provider=self.provider,
                progress_cb=self._on_progress,
                cancel_check=lambda: self.is_cancelled,
            )
            if self.is_cancelled:
                self.signals.cancelled.emit()
                return

            if not res or not res.strip():
                self.signals.error.emit("The summary provider returned an empty response.")
                return

            self.signals.success.emit(res.strip(), self.language)
        except InterruptedError:
            self.signals.cancelled.emit()
        except GeminiSummaryError as exc:
            err_msg = safe_error(exc)
            is_quota = getattr(exc, "is_quota_or_rate_limit", False)
            self.signals.gemini_error.emit(err_msg, is_quota)
        except Exception as exc:
            err_msg = safe_error(exc)
            self.signals.error.emit(err_msg)
        except BaseException as exc:
            self.signals.error.emit(f"Unexpected worker thread error: {exc}")

    def _on_progress(self, stage: str, step: int, total_steps: int) -> None:
        if not self.is_cancelled:
            self.signals.progress.emit(stage, step, total_steps)


class AISummaryDialog(QDialog):
    # Retain dismissed workers until their bounded network request returns.
    _background_dialogs = set()
    """Dialog for generating, editing, and downloading AI Sermon Summaries with a modern dashboard."""

    def __init__(
        self,
        summarizer: SermonSummarizer,
        session: SessionRecord,
        segments: List[TranscriptSegment],
        session_manager: Optional[SessionManager] = None,
        parent=None,
        sermon_context: Optional[SermonContextManager] = None,
    ) -> None:
        super().__init__(parent)
        self.summarizer = summarizer
        self.session = session
        self.segments = segments
        self.session_manager = session_manager
        self.sermon_context = sermon_context
        self._is_generating = False
        self._active_provider = "gemini"
        self._worker_thread: Optional[SummaryWorkerThread] = None
        self._is_syncing = False
        self._teaching_edits: List[QLineEdit] = []
        self._dirty = False
        self._pending_fallback = False
        self._loaded_lang = load_user_settings().get('summary', {}).get('language', 'lv')
        if self._loaded_lang not in ('lv', 'en'):
            self._loaded_lang = 'lv'
        self._dismissed = False
        self._discard_result = False
        self._edit_mode = False
        self._point_tools = []
        self._highlight_edits = []
        self._current_report = None

        self.setWindowTitle(f"🧠 AI Sermon Summary — {session.title}")
        self.resize(800, 800)
        self.setMinimumSize(640, 560)
        self.setStyleSheet(DIALOG_STYLESHEET)
        self._build_ui()
        self._check_cached_summary()

    def _build_ui(self) -> None:
        from .summary_workspace import build_summary_workspace
        build_summary_workspace(self)

    def _apply_report_language(self):
        from .summary_workspace import labels
        text = labels(self.lang_combo.currentData())
        self.report_heading.setText(text['heading'])
        self.report_subtitle.setText(text['subtitle'])
        self.overview_heading.setText(text['overview'])
        self.section_headings['key_teachings'].setText(text['points'])
        self.section_headings['highlights'].setText(text['highlights'])
        self.empty_title.setText(text['empty_title'])
        self.empty_label.setText(text['empty'])
        has_summary = bool(self.summary_text.toPlainText().strip())
        self.generate_btn.setText(text['regenerate'] if has_summary else text['generate'])
        self.edit_btn.setText(text['back'] if self.view_stack.currentIndex() == 1 else (text['done'] if self._edit_mode else text['edit']))
        self.copy_btn.setText(text['copy'])
        self.download_btn.setText(text['export'])
        self.save_btn.setText(text['saved'] if has_summary and not self._dirty else text['save'])
        self.close_btn.setText(text['close'])
        self.cancel_btn.setText(text['cancel'])
        self.raw_edit_action.setText(text['raw'])
        self.transcript_btn.setText(text['transcript'])
        self.edit_actions_btn.setText('More' if self._loaded_lang == 'en' else 'Vairāk')
        self.add_point_btn.setText(text['add_point'])
        self.add_highlight_btn.setText(text['add_highlight'])
        words = sum(len(segment.latvian_text.split()) for segment in self.segments)
        language = self.lang_combo.currentData()
        meta = f"{self.session.created_at[:16]}  ·  {len(self.segments)} segments  ·  {words:,} transcript words" if language == 'en' else f"{self.session.created_at[:16]}  ·  {len(self.segments)} daļas  ·  {words:,} vārdi transkriptā"
        self.transcript_meta.setText(meta)
        self._update_summary_word_count()

    def _update_summary_word_count(self):
        from .summary_workspace import labels
        report = self._current_report
        words = len(' '.join([report.title, report.main_message, *report.key_teachings, *report.highlights]).split()) if report else 0
        self.meta_words_pill.setText(f"{words} {labels(self.lang_combo.currentData())['words']}" if words else '')

    def _style_report_editor(self, editor):
        editor.setReadOnly(not self._edit_mode or self._is_generating)
        editor.setProperty('editing', self._edit_mode and not self._is_generating)
        editor.style().unpolish(editor)
        editor.style().polish(editor)

    def _set_edit_mode(self, enabled):
        self._edit_mode = bool(enabled)
        self.edit_btn.blockSignals(True)
        self.edit_btn.setChecked(self._edit_mode)
        self.edit_btn.blockSignals(False)
        for editor in [self.card_title_edit, self.card_main_msg, *self._teaching_edits, *self._highlight_edits]:
            self._style_report_editor(editor)
        has_summary = bool(self.summary_text.toPlainText().strip())
        for control in self._point_tools:
            control.setVisible(self._edit_mode and has_summary)
        if self._current_report:
            self.section_cards['key_teachings'].setVisible(has_summary and (bool(self._current_report.key_teachings) or self._edit_mode))
            self.section_cards['highlights'].setVisible(has_summary and (bool(self._current_report.highlights) or self._edit_mode))
        self._apply_report_language()

    def _toggle_edit_mode(self, checked=False):
        if self.view_stack.currentIndex() == 1:
            self._switch_to_dashboard_view()
            return
        self._set_edit_mode(checked)

    def _switch_to_dashboard_view(self) -> None:
        if self.view_stack.currentIndex() == 1:
            self._populate_dashboard_from_text(self.summary_text.toPlainText(), self._loaded_lang)
        self.view_stack.setCurrentIndex(0)
        self._set_edit_mode(False)

    def _switch_to_raw_view(self) -> None:
        if self._is_generating or not self.summary_text.toPlainText().strip():
            return
        self._set_edit_mode(False)
        self.view_stack.setCurrentIndex(1)
        self.summary_text.setReadOnly(False)
        self._apply_report_language()

    def _add_extra_teaching_point(self) -> None:
        self._add_report_point('key_teachings')

    def _add_report_point(self, section):
        edits = self._teaching_edits if section == 'key_teachings' else self._highlight_edits
        self._add_single_report_row(section, len(edits) + 1, '')
        edits[-1].setFocus()
        self._on_dashboard_field_changed()

    def _add_single_teaching_row(self, idx: int, text: str) -> None:
        self._add_single_report_row('key_teachings', idx, text)

    def _add_single_report_row(self, section, idx, text):
        from .summary_workspace import ReportTextEdit
        from PySide6.QtWidgets import QSizePolicy
        edits = self._teaching_edits if section == 'key_teachings' else self._highlight_edits
        layout = self.teachings_layout if section == 'key_teachings' else self.highlights_layout
        row = QFrame()
        row.setObjectName('pointRow')
        row.setStyleSheet('QFrame#pointRow { background: #111D2C; border: 1px solid #23344A; border-radius: 10px; }')
        row.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)
        items = QHBoxLayout(row)
        items.setContentsMargins(13, 8, 13, 8)
        items.setSpacing(10)
        marker = QLabel(f'{idx}.' if section == 'key_teachings' else '•')
        marker.setObjectName('pointMarker')
        marker.setFixedWidth(20)
        marker.setStyleSheet('font-size: 13px; color: #5EEAD4; font-weight: 600;' if section == 'key_teachings' else 'font-size: 14px; color: #FBBF24;')
        items.addWidget(marker, alignment=Qt.AlignTop)
        edit = ReportTextEdit(text)
        edit.setAccessibleName(f"Core point {idx}" if section == 'key_teachings' else f"Speech highlight {idx}")
        edit.textChanged.connect(self._on_dashboard_field_changed)
        edits.append(edit)
        self._style_report_editor(edit)
        items.addWidget(edit, 1)
        remove = QPushButton('×')
        remove.setToolTip('Remove point')
        remove.setAccessibleName(f'Remove {section} {idx}')
        remove.setFixedSize(28, 28)
        remove.setStyleSheet('padding: 0; min-height: 24px; border: none; color: #8BA5BA; background: transparent; font-size: 18px;')
        self._point_tools.append(remove)
        remove.setVisible(self._edit_mode)
        def remove_point():
            edits.remove(edit)
            self._point_tools.remove(remove)
            layout.removeWidget(row)
            row.setParent(None)
            row.deleteLater()
            if section == 'key_teachings':
                for index in range(layout.count()):
                    layout.itemAt(index).widget().findChild(QLabel, 'pointMarker').setText(f'{index + 1}.')
            self._on_dashboard_field_changed()
        remove.clicked.connect(remove_point)
        items.addWidget(remove, alignment=Qt.AlignTop)
        layout.addWidget(row)

    def _rebuild_report_rows(self, section, points):
        edits = self._teaching_edits if section == 'key_teachings' else self._highlight_edits
        layout = self.teachings_layout if section == 'key_teachings' else self.highlights_layout
        while layout.count():
            row = layout.takeAt(0).widget()
            if row:
                for control in row.findChildren(QPushButton):
                    if control in self._point_tools:
                        self._point_tools.remove(control)
                row.hide()
                row.setParent(None)
                row.deleteLater()
        edits.clear()
        for index, point in enumerate(points, 1):
            self._add_single_report_row(section, index, point)

    def _rebuild_teaching_rows(self, teachings: List[str]) -> None:
        self._rebuild_report_rows('key_teachings', teachings)

    def _on_dashboard_field_changed(self) -> None:
        if self._is_syncing:
            return
        self._dirty = True
        self._sync_from_dashboard_to_text()

    def _sync_from_dashboard_to_text(self) -> None:
        self._is_syncing = True
        try:
            report = StructuredSummary(title=self.card_title_edit.text().strip(),
                main_message=self.card_main_msg.toPlainText().strip(),
                key_teachings=[edit.text().strip() for edit in self._teaching_edits if edit.text().strip()],
                highlights=[edit.text().strip() for edit in self._highlight_edits if edit.text().strip()], report_style=True)
            self._current_report = report
            text = report.to_report_text(self._loaded_lang)
            self.summary_text.blockSignals(True)
            self.summary_text.setPlainText(text)
            self.summary_text.blockSignals(False)
            self._update_summary_word_count()
            self._update_action_buttons_state()
        finally:
            self._is_syncing = False

    def _on_raw_text_changed(self) -> None:
        if self._is_syncing:
            return
        self._dirty = True
        self._is_syncing = True
        try:
            self._populate_dashboard_from_text(self.summary_text.toPlainText(), self._loaded_lang, update_text=False)
            self._update_action_buttons_state()
        finally:
            self._is_syncing = False

    def _populate_dashboard_from_text(self, text: str, lang: str = 'en', update_text: bool = True) -> None:
        report = StructuredSummary.from_text(text, default_title=self.session.title).to_report()
        self._current_report = report if text.strip() else None
        for editor, value in ((self.card_title_edit, report.title if text.strip() else ''),
                              (self.card_main_msg, report.main_message)):
            editor.blockSignals(True)
            if isinstance(editor, QLineEdit):
                editor.setText(value)
            else:
                editor.setPlainText(value)
            editor.blockSignals(False)
        self._rebuild_report_rows('key_teachings', report.key_teachings)
        self._rebuild_report_rows('highlights', report.highlights)
        has_text = bool(text.strip())
        self.empty_panel.setVisible(not has_text)
        self.card_title_edit.setVisible(has_text and bool(report.title))
        self.section_cards['main_message'].setVisible(has_text and bool(report.main_message))
        self.section_cards['key_teachings'].setVisible(has_text and (bool(report.key_teachings) or self._edit_mode))
        self.section_cards['highlights'].setVisible(has_text and (bool(report.highlights) or self._edit_mode))
        if update_text:
            self.summary_text.blockSignals(True)
            self.summary_text.setPlainText(report.to_report_text(lang) if has_text else '')
            self.summary_text.blockSignals(False)
        self._set_edit_mode(self._edit_mode)

    def get_current_summary_text(self) -> str:
        if self.view_stack.currentIndex() == 0 and self._dirty:
            self._sync_from_dashboard_to_text()
        return self.summary_text.toPlainText().strip()

    def _update_action_buttons_state(self) -> None:
        has_text = bool(self.summary_text.toPlainText().strip())
        for control in (self.copy_btn, self.download_btn, self.save_btn, self.edit_btn, self.raw_edit_action):
            control.setEnabled(has_text and not self._is_generating)
        self.transcript_btn.setEnabled(not self._is_generating)
        self.summary_text.setReadOnly(self._is_generating)
        for card in self.section_cards.values():
            card.setEnabled(not self._is_generating)
        self.card_title_edit.setReadOnly(not self._edit_mode or self._is_generating)
        self._apply_report_language()

    def _check_cached_summary(self) -> None:
        if self.session_manager:
            self.session = self.session_manager.get_session(self.session.session_id) or self.session
        cached = self.session.summary_text_en if self.lang_combo.currentData() == "en" else self.session.summary_text_lv
        self._populate_dashboard_from_text(cached or "", lang=self.lang_combo.currentData(), update_text=True)
        self._dirty = False
        if cached:
            self.status_label.setText("Saved summary" if self.lang_combo.currentData() == "en" else "Saglabāts kopsavilkums")
            self.status_label.hide()
            meta = self.session_manager.get_summary_metadata(self.session.session_id, self.lang_combo.currentData()) if self.session_manager else {}
            if meta:
                self.provider_info_label.setText(f"Generated with {meta.get('provider', '')} · {meta.get('model', '')}")
            else:
                self.provider_info_label.setText("Saglabāts kopsavilkums · AI sniedzējs nav norādīts" if self.lang_combo.currentData() == 'lv' else "Saved summary · AI provider not recorded")
        else:
            self.status_label.show()
            words = sum(len(segment.latvian_text.split()) for segment in self.segments)
            if self.lang_combo.currentData() == 'lv':
                self.status_label.setText('Īss transkripts — kopsavilkums aptvers tikai ierakstīto.' if words < 250 else 'Šajā valodā vēl nav kopsavilkuma. Izveidojiet to no transkripta.')
            else:
                self.status_label.setText('Short transcript — the summary will cover only what was captured.' if words < 250 else 'No summary in this language yet. Generate one from the transcript.')
            self.provider_info_label.setText('Google Gemini · primary AI provider')
        self._update_action_buttons_state()

    def _on_lang_changed(self) -> None:
        if not self._is_generating:
            if self._dirty:
                self._save_summary(language=self._loaded_lang)
            self._loaded_lang = self.lang_combo.currentData()
            settings = load_user_settings()
            settings['summary'] = {'language': self._loaded_lang}
            try:
                save_user_settings(settings)
            except OSError as exc:
                self.summarizer.status_cb('[SUMMARY] Could not save language preference: ' + safe_error(exc))
            if self.parent() and hasattr(self.parent(), 'user_settings'):
                self.parent().user_settings['summary'] = settings['summary']
            self._check_cached_summary()

    def _save_summary(self, _checked=False, language=None) -> None:
        text = self.get_current_summary_text()
        if not text:
            return
        lang = language or self.lang_combo.currentData()
        if self.session_manager:
            self.session_manager.save_session_summary(self.session.session_id, text, lang)
        setattr(self.session, "summary_text_" + lang, text)
        title = self.card_title_edit.text().strip()
        if title and self.session_manager:
            self.session_manager.update_session_title(self.session.session_id, title)
            self.session.title = title
        self._dirty = False
        self.status_label.setText("Saved to sermon history." if lang == "en" else "Saglabāts sprediķu vēsturē.")
        self._update_action_buttons_state()

    def _edit_transcript(self) -> None:
        if not self.session_manager:
            return
        if self.session.state in {"active", "paused"}:
            QMessageBox.information(self, "Sermon Active", "Stop the sermon before editing its saved transcript.")
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("Edit Transcript — original timestamps are preserved")
        dialog.resize(700, 550)
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel("Edit each saved segment below. Changes invalidate existing summaries."))
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = QWidget()
        rows = QVBoxLayout(content)
        edits = []
        for segment in self.segments:
            rows.addWidget(QLabel(f"{segment.timestamp_str} · Segment {segment.segment_id}"))
            edit = QPlainTextEdit(segment.latvian_text)
            edit.setMinimumHeight(75)
            rows.addWidget(edit)
            edits.append((segment, edit))
        scroll.setWidget(content)
        layout.addWidget(scroll)
        save = QPushButton("Save Transcript")
        save.clicked.connect(dialog.accept)
        layout.addWidget(save)
        if dialog.exec() == QDialog.Accepted:
            self.session_manager.edit_transcript(self.session.session_id, {s.segment_id: e.toPlainText() for s, e in edits})
            self.segments = self.session_manager.get_segments(self.session.session_id)
            self._check_cached_summary()
            words = sum(len(s.latvian_text.split()) for s in self.segments)
            self.transcript_meta.setText(f"{self.session.created_at} · {len(self.segments)} segments · {words:,} transcript words")

    def _generate_summary(self, provider: str = "gemini") -> None:
        if self._dismissed:
            return
        if not self.segments:
            QMessageBox.warning(self, "No Transcript", "No transcript segments available for this session.")
            return

        if self._is_generating or (self._worker_thread and self._worker_thread.isRunning()):
            return

        # Priority guard: check if live translation is currently active
        is_live = False
        parent_win = self.parent()
        if parent_win and hasattr(parent_win, "engine") and parent_win.engine:
            is_live = getattr(parent_win.engine, "_started", False)
            if hasattr(parent_win, "_is_paused") and parent_win._is_paused:
                is_live = False

        if is_live:
            reply = QMessageBox.question(
                self,
                "Live Translation In Progress",
                "Live sermon translation is currently running.\n\n"
                "Generating a full sermon summary now may compete for Google Gemini free-tier rate limits "
                "and risk delaying live translation for listeners.\n\n"
                "Live translation is given strict priority. Would you like to proceed with summary generation anyway?",
                QMessageBox.Yes | QMessageBox.Cancel,
                QMessageBox.Cancel,
            )
            if reply != QMessageBox.Yes:
                return

        lang = self.lang_combo.currentData()
        if self.session_manager:
            self.segments = self.session_manager.get_segments(self.session.session_id)
        self._is_generating = True
        self._discard_result = False
        self.status_label.show()
        self._active_provider = provider

        self.generate_btn.setEnabled(False)
        self.lang_combo.setEnabled(False)
        self.cancel_btn.setVisible(True)
        self.cancel_btn.setEnabled(True)
        self.progress_bar.setVisible(True)
        self.progress_bar.setValue(5)
        self._update_action_buttons_state()

        prov_name = "Google Gemini (Free Tier)" if provider == "gemini" else "OpenAI"
        self.provider_info_label.setText(f"Generating with {prov_name} · selecting a supported model")
        self.status_label.setText(f"Preparing transcript via {prov_name} in {lang.upper()}...")

        # Summaries describe the captured transcript, not the prepared draft.
        ctx_text = ""

        # Create worker signals and worker thread
        self._worker_signals = SummaryWorkerSignals()
        self._worker_signals.progress.connect(self._on_worker_progress)
        self._worker_signals.success.connect(self._on_summary_success)
        self._worker_signals.cancelled.connect(self._on_summary_cancelled)
        self._worker_signals.gemini_error.connect(self._on_gemini_error)
        self._worker_signals.error.connect(self._on_summary_error)

        self._worker_thread = SummaryWorkerThread(
            signals=self._worker_signals,
            summarizer=self.summarizer,
            segments=self.segments,
            language=lang,
            sermon_title=self.session.title,
            sermon_context=ctx_text,
            provider=provider,
        )
        self._worker_thread.finished.connect(self._on_worker_finished)

        # Safety Watchdog: Prevent indefinite hangs at 5%
        if hasattr(self, "_watchdog_timer") and self._watchdog_timer:
            self._watchdog_timer.stop()
        self._watchdog_timer = QTimer(self)
        self._watchdog_timer.setSingleShot(True)
        self._watchdog_timer.timeout.connect(self._on_worker_timeout_watchdog)
        self._watchdog_timer.start(75000)  # inactivity timeout, refreshed at each stage

        self._worker_thread.start()

    def _on_worker_timeout_watchdog(self) -> None:
        if self._is_generating:
            self._cancel_generation()
            self.status_label.setText("Error: Summary generation took longer than expected and timed out.")
            self._update_action_buttons_state()
            if hasattr(self.summarizer, "status_cb") and self.summarizer.status_cb:
                self.summarizer.status_cb("[SUMMARY] Watchdog timeout: Summary generation exceeded 75s limit.")
            QMessageBox.warning(
                self,
                "Summary Timeout",
                "Summary generation timed out without receiving a response from the provider.\n\n"
                "The background task was safely cancelled. You may retry with Gemini or use OpenAI.",
            )

    def _on_worker_finished(self) -> None:
        if hasattr(self, "_watchdog_timer") and self._watchdog_timer:
            self._watchdog_timer.stop()
        if self._dismissed:
            self._background_dialogs.discard(self)
            self.deleteLater()
            return
        if self._discard_result:
            self._on_summary_cancelled()
        self.generate_btn.setEnabled(not self._is_generating)
        if self._pending_fallback:
            self._pending_fallback = False
            QTimer.singleShot(0, lambda: self._generate_summary("openai"))

    def _on_worker_progress(self, stage: str, step: int, total_steps: int) -> None:
        if not self._is_generating or self._dismissed or self._discard_result:
            return
        pct = int(min(98, max(5, (step / total_steps) * 100)))
        self.progress_bar.setValue(pct)
        self._watchdog_timer.start(75000)
        self.status_label.setText(f"{stage} (stage {step}/{total_steps})...")

    def _cancel_generation(self) -> None:
        self._discard_result = True
        if hasattr(self, "_watchdog_timer") and self._watchdog_timer:
            self._watchdog_timer.stop()
        if self._worker_thread:
            self._worker_thread.cancel()
        self.cancel_btn.setEnabled(False)
        self.status_label.setText("Cancelling summary generation...")

    def _on_summary_cancelled(self) -> None:
        if self._dismissed:
            return
        if hasattr(self, "_watchdog_timer") and self._watchdog_timer:
            self._watchdog_timer.stop()
        self._is_generating = False
        self.generate_btn.setEnabled(True)
        self.lang_combo.setEnabled(True)
        self.cancel_btn.setVisible(False)
        self.progress_bar.setVisible(False)
        self.status_label.setText("Summary generation cancelled. No changes were made.")
        self._update_action_buttons_state()

    def _on_summary_success(self, text: str, lang: str) -> None:
        if self._dismissed or self._discard_result or not text.strip():
            return
        if hasattr(self, "_watchdog_timer") and self._watchdog_timer:
            self._watchdog_timer.stop()
        self._is_generating = False
        self.lang_combo.blockSignals(True)
        self.lang_combo.setCurrentIndex(self.lang_combo.findData(lang))
        self.lang_combo.blockSignals(False)
        self._loaded_lang = lang
        self.summary_text.setPlainText(text)
        self._populate_dashboard_from_text(text, lang=lang, update_text=True)
        self._switch_to_dashboard_view()
        self.generate_btn.setEnabled(True)
        self.lang_combo.setEnabled(True)
        self.cancel_btn.setVisible(False)
        self.progress_bar.setValue(100)
        self.progress_bar.hide()
        self.status_label.setText("✓ Summary generated successfully! You can review and edit it above before downloading.")
        self._update_action_buttons_state()

        if self.session_manager:
            self.session_manager.save_summary_metadata(self.session.session_id, lang,
                self.summarizer.last_provider_used, self.summarizer.last_model_used)
        self.provider_info_label.setText(f"Generated with {self.summarizer.last_provider_used} · {self.summarizer.last_model_used}")
        self._save_summary()

        # Notify parent window / activity log that summary was displayed and delivered successfully
        if hasattr(self.summarizer, "status_cb") and self.summarizer.status_cb:
            self.summarizer.status_cb(f"[SUMMARY] Completed: Summary generated and displayed in UI ({len(text)} chars)")

    def _on_gemini_error(self, err: str, is_quota: bool) -> None:
        if self._dismissed or self._discard_result:
            return
        if hasattr(self, "_watchdog_timer") and self._watchdog_timer:
            self._watchdog_timer.stop()
        self._is_generating = False
        self.generate_btn.setEnabled(True)
        self.lang_combo.setEnabled(True)
        self.cancel_btn.setVisible(False)
        self.progress_bar.setVisible(False)
        self.status_label.setText(f"Gemini Notice: {err}")
        self._update_action_buttons_state()

        if hasattr(self.summarizer, "status_cb") and self.summarizer.status_cb:
            self.summarizer.status_cb(f"Gemini summary generation failed: {err}")

        # Check if OpenAI is configured as optional fallback
        has_openai = bool(self.summarizer.config.openai_api_key and self.summarizer.config.openai_api_key.strip())
        if has_openai:
            fallback_model = self.summarizer.config.openai_fallback_model or "gpt-4o-mini"
            est_cost = estimate_openai_summary_cost(self.segments, fallback_model)
            reply = QMessageBox.question(
                self,
                "Gemini Summary Unavailable",
                f"Google Gemini could not complete the summary:\n\n{err}\n\n"
                f"Would you like to generate the summary using OpenAI fallback ({fallback_model})?\n"
                f"Estimated cost: ~${est_cost:.4f} USD.\n\n"
                "OpenAI API usage may incur charges. This approval applies only to this generation.\n\n"
                f"(Click 'Yes' to proceed with OpenAI, or 'No' to keep Gemini and retry later.)",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if reply == QMessageBox.Yes:
                if self._worker_thread and self._worker_thread.isRunning():
                    self._pending_fallback = True
                else:
                    self._generate_summary(provider="openai")
        else:
            QMessageBox.warning(
                self,
                "Gemini Summary Unavailable",
                f"Google Gemini could not complete the summary:\n\n{err}\n\n"
                f"You can retry in a few moments, or configure an OpenAI API key in AI Models & Keys for emergency fallback.",
            )

    def _on_summary_error(self, err: str) -> None:
        if self._dismissed or self._discard_result:
            return
        if hasattr(self, "_watchdog_timer") and self._watchdog_timer:
            self._watchdog_timer.stop()
        self._is_generating = False
        self.generate_btn.setEnabled(True)
        self.lang_combo.setEnabled(True)
        self.cancel_btn.setVisible(False)
        self.progress_bar.setVisible(False)
        self.status_label.setText(f"Error: {err}")
        self._update_action_buttons_state()

        if hasattr(self.summarizer, "status_cb") and self.summarizer.status_cb:
            self.summarizer.status_cb(f"Summary generation error: {err}")

        QMessageBox.critical(self, "Generation Error", f"Failed to generate summary: {err}")

    def closeEvent(self, event) -> None:
        self._dismiss()
        event.accept()

    def _dismiss(self) -> None:
        if self._dismissed:
            return
        self._dismissed = True
        self._discard_result = True
        self._pending_fallback = False
        if hasattr(self, "_watchdog_timer") and self._watchdog_timer:
            self._watchdog_timer.stop()
        if self._worker_thread and self._worker_thread.isRunning():
            self._worker_thread.cancel()
            self._background_dialogs.add(self)
        if self._dirty:
            self._save_summary()
        # Calling close() from reject() recursed through QDialog.closeEvent.
        # done() dismisses the modal loop without destroying a running QThread.
        self.done(QDialog.Rejected)

    def reject(self) -> None:
        self._dismiss()

    def _copy_summary(self) -> None:
        from PySide6.QtGui import QGuiApplication
        txt = self.get_current_summary_text()
        if txt:
            QGuiApplication.clipboard().setText(txt)
            self.status_label.setText("✓ Summary copied to clipboard.")
            from .summary_workspace import labels
            self.copy_btn.setText(labels(self.lang_combo.currentData())['copied'])

    def _download_summary(self) -> None:
        txt = self.get_current_summary_text()
        if not txt:
            QMessageBox.warning(self, "No Summary", "Please generate a summary first.")
            return

        date_str = self.session.created_at.split()[0] if self.session.created_at else "session"
        safe_title = "".join(c for c in self.session.title if c.isalnum() or c in (" ", "-", "_")).strip()
        default_base = f"Sermon_Summary_{date_str}_{safe_title.replace(' ', '_')}"

        save_path, selected_filter = QFileDialog.getSaveFileName(
            self,
            "Download Summary",
            default_base + ".txt",
            "Plain Text (*.txt);;Word Document (*.docx);;PDF Document (*.pdf);;Markdown (*.md)",
        )
        if not save_path:
            return

        out_file = Path(save_path)
        if out_file.exists():
            confirm = QMessageBox.question(
                self,
                "Overwrite File?",
                f"The file '{out_file.name}' already exists. Are you sure you want to overwrite it?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if confirm != QMessageBox.Yes:
                return

        ext = out_file.suffix.lower()
        try:
            if ext == ".docx":
                SermonSummarizer.export_summary_to_docx(out_file, self.session.title, txt)
            elif ext == ".pdf":
                SermonSummarizer.export_summary_to_pdf(out_file, self.session.title, txt)
            elif ext == ".md":
                SermonSummarizer.export_summary_to_markdown(out_file, self.session.title, txt)
            else:
                SermonSummarizer.export_summary_to_txt(out_file, self.session.title, txt)

            QMessageBox.information(self, "Download Successful", f"Summary saved successfully:\n{out_file.name}")
        except Exception as exc:
            QMessageBox.critical(self, "Download Error", f"Failed to save summary: {exc}")


class SessionHistoryDialog(QDialog):
    """Dialog for reviewing past sermons, exporting transcripts, and viewing summaries."""

    def __init__(self, session_manager: SessionManager, summarizer: SermonSummarizer, parent=None) -> None:
        super().__init__(parent)
        self.session_manager = session_manager
        self.summarizer = summarizer
        self.setWindowTitle("📜 Sermon History & Crash Recovery")
        self.resize(880, 520)
        self.setStyleSheet(DIALOG_STYLESHEET)
        self._build_ui()
        self._refresh_table()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        header = QLabel("📜 Saved Sermon Sessions")
        header.setStyleSheet("font-size: 16px; font-weight: bold; color: #22D3EE;")
        layout.addWidget(header)

        # Sessions Table
        self.table = QTableWidget()
        self.table.setColumnCount(6)
        self.table.setHorizontalHeaderLabels(["Date & Time", "Sermon Title", "Duration", "Segments", "Status", "ID"])
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        layout.addWidget(self.table, 1)

        # Action Buttons
        btn_row = QHBoxLayout()
        self.export_btn = QPushButton("📥 Export Transcript...")
        self.export_btn.clicked.connect(self._open_export)

        self.summary_btn = QPushButton("🧠 AI Summary...")
        self.summary_btn.clicked.connect(self._open_summary)

        self.rename_btn = QPushButton("✏️ Rename Session")
        self.rename_btn.clicked.connect(self._rename_session)

        self.delete_btn = QPushButton("🗑 Delete Session")
        self.delete_btn.clicked.connect(self._delete_session)

        self.close_btn = QPushButton("Close")
        self.close_btn.clicked.connect(self.accept)

        btn_row.addWidget(self.export_btn)
        btn_row.addWidget(self.summary_btn)
        btn_row.addWidget(self.rename_btn)
        btn_row.addWidget(self.delete_btn)
        btn_row.addStretch(1)
        btn_row.addWidget(self.close_btn)
        layout.addLayout(btn_row)

    def _refresh_table(self) -> None:
        sessions = self.session_manager.list_sessions()
        self.table.setRowCount(len(sessions))
        for row_idx, s in enumerate(sessions):
            segs = self.session_manager.get_segments(s.session_id)
            duration_str = f"{int(s.duration_seconds // 60)}m {int(s.duration_seconds % 60)}s"

            self.table.setItem(row_idx, 0, QTableWidgetItem(s.created_at))
            self.table.setItem(row_idx, 1, QTableWidgetItem(s.title))
            self.table.setItem(row_idx, 2, QTableWidgetItem(duration_str))
            self.table.setItem(row_idx, 3, QTableWidgetItem(str(len(segs))))
            self.table.setItem(row_idx, 4, QTableWidgetItem(s.state.upper()))
            self.table.setItem(row_idx, 5, QTableWidgetItem(s.session_id))

        if sessions:
            self.table.selectRow(0)

    def _selected_session_id(self) -> Optional[str]:
        row = self.table.currentRow()
        if row < 0:
            return None
        item = self.table.item(row, 5)
        return item.text() if item else None

    def _open_export(self) -> None:
        sid = self._selected_session_id()
        if not sid:
            return
        sess = self.session_manager.get_session(sid)
        segs = self.session_manager.get_segments(sid)
        if sess:
            dlg = TranscriptExportDialog(sess, segs, self)
            dlg.exec()

    def _open_summary(self) -> None:
        sid = self._selected_session_id()
        if not sid:
            return
        sess = self.session_manager.get_session(sid)
        segs = self.session_manager.get_segments(sid)
        if sess:
            dlg = AISummaryDialog(self.summarizer.fork(), sess, segs, self.session_manager, self)
            dlg.exec()

    def _rename_session(self) -> None:
        sid = self._selected_session_id()
        if not sid:
            return
        sess = self.session_manager.get_session(sid)
        if not sess:
            return
        from PySide6.QtWidgets import QInputDialog
        new_title, ok = QInputDialog.getText(self, "Rename Sermon", "Enter new sermon title:", text=sess.title)
        if ok and new_title.strip():
            self.session_manager.update_session_title(sid, new_title.strip())
            self._refresh_table()

    def _delete_session(self) -> None:
        sid = self._selected_session_id()
        if not sid:
            return
        if QMessageBox.question(self, "Delete Session", "Are you sure you want to permanently delete this sermon session and its transcripts?") == QMessageBox.Yes:
            self.session_manager.delete_session(sid)
            self._refresh_table()
