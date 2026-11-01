"""Private local recording export and explicit, guarded retention controls."""
from __future__ import annotations

import json
from pathlib import Path
import threading
import shutil
import sqlite3

from PySide6.QtCore import QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QListWidget,
    QLabel, QPushButton, QFileDialog, QLineEdit, QDialogButtonBox, QMessageBox)

from .config import app_data_dir
from .speech_audit import SpeechAudit
from .reliability import safe_error
from .recording_guard import recording_lease
from .diagnostic_export import recording_info, segment_ranges, parse_segments, export_segments


def delete_drained_recording(path, root, active=None):
    with recording_lease(path, exclusive=True):
        return _delete_drained_recording(path, root, active)


def _delete_drained_recording(path, root, active=None):
    original = Path(path).absolute()
    root = Path(root).resolve()
    target = original.resolve()
    if target.parent != root or target == root or original.is_symlink() or (
            getattr(original.lstat(), 'st_file_attributes', 0) & 0x400):
        raise ValueError('Recording must be a real direct session directory under the audio backlog.')
    if active and target == Path(active).resolve():
        raise ValueError('The active session recording is protected.')
    snapshot = json.loads((target / 'integrity.json').read_text(encoding='utf-8'))
    for name in ('capture_pending', 'translation_pending', 'synthesis_pending',
                 'playback_pending', 'audio_review_items', 'yoruba_audio_pending', 'translation_review_items'):
        if snapshot.get(name, 0):
            raise ValueError('Pending or unresolved speech is protected. Export/recover it before deleting recordings.')
    if snapshot.get('uncovered_ranges') or snapshot.get('unjournaled_samples'):
        raise ValueError('An incomplete capture is protected for recovery.')
    for database in target.glob('*.sqlite'):
        db = sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True)
        try:
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='work'").fetchone() and db.execute(
                    'SELECT COUNT(*) FROM work').fetchone()[0]:
                raise ValueError('A durable queue still contains pending/review speech; recording protected.')
        finally:
            db.close()
    # The resolved absolute target and reparse attributes were checked above.
    shutil.rmtree(target)


class AudioDiagnosticsDialog(QDialog):
    export_finished = Signal(object, str)

    def __init__(self, parent=None, active=None):
        super().__init__(parent)
        self.setWindowTitle('Private audio diagnostics')
        self.resize(700, 410)
        self.root = app_data_dir() / 'backlog'
        self.active = active
        self._exporting = False
        self.export_finished.connect(self._export_done)
        layout = QVBoxLayout(self)
        note = QLabel('Recordings stay on this computer. Export includes original capture, '
                      'exact submitted audio, raw recognition responses and source contribution. '
                      'These contain private sermon content. Nothing is uploaded. '
                      'Pending or review audio is protected from deletion.')
        note.setWordWrap(True)
        layout.addWidget(note)
        self.sessions = QListWidget()
        layout.addWidget(self.sessions)
        self.detail = QLabel()
        self.detail.setWordWrap(True)
        layout.addWidget(self.detail)
        row = QHBoxLayout()
        self.export = QPushButton('Export segment audio…')
        self.folder = QPushButton('Open recording folder')
        self.delete = QPushButton('Delete drained recording…')
        close = QPushButton('Close')
        for button in (self.export, self.folder, self.delete, close):
            row.addWidget(button)
        layout.addLayout(row)
        self.export.clicked.connect(self._export)
        self.folder.clicked.connect(self._folder)
        self.delete.clicked.connect(self._delete)
        close.clicked.connect(self.accept)
        self.sessions.currentRowChanged.connect(self._selected)
        self._reload()

    def _reload(self):
        self.paths = sorted((p for p in self.root.glob('*')
                             if p.is_dir() and (p / 'speech-audit.sqlite').is_file()),
                            key=lambda p: p.stat().st_mtime, reverse=True)
        self.sessions.clear()
        for button in (self.export, self.folder, self.delete):
            button.setEnabled(bool(self.paths))
        for path in self.paths:
            self.sessions.addItem(path.name + (' — active' if self.active and path == Path(self.active) else ''))
        if self.paths:
            self.sessions.setCurrentRow(0)
        else:
            self.detail.setText('No retained session recordings found.')
            for button in (self.export, self.folder, self.delete):
                button.setEnabled(False)

    def _path(self):
        index = self.sessions.currentRow()
        return self.paths[index] if 0 <= index < len(self.paths) else None

    def _selected(self, *_):
        path = self._path()
        if path:
            info = recording_info(path / 'speech-audit.sqlite')
            size = sum(p.stat().st_size for p in path.iterdir() if p.is_file()) / 1024 ** 2
            self.detail.setText(f'Recording: {info["recording_id"]}\n'
                                f'Session: {", ".join(info["session_ids"]) or "unavailable"}\n'
                                f'Date (UTC): {info["first_segment_utc"] or "unavailable"}\n'
                                f'{len(info["segments"])} exportable segments: {segment_ranges(info["segments"])}\n'
                                f'{size:.1f} MiB retained.' + (' Diagnostic audio expired.' if info['audio_expired'] else ''))

    def _folder(self):
        if path := self._path():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def _export(self):
        path = self._path()
        if not path:
            return
        try:
            info = recording_info(path / 'speech-audit.sqlite')
            if not info['segments']:
                raise ValueError('No segmented audio yet. The original callback journal is retained.')
            chooser = QDialog(self)
            chooser.setWindowTitle('Choose segments')
            layout = QVBoxLayout(chooser)
            layout.addWidget(QLabel('Segment numbers or ranges'))
            entry = QLineEdit()
            layout.addWidget(entry)
            help_text = QLabel('Separate segment numbers with commas. Use a hyphen for a range. Example: 185-191, 193.')
            help_text.setWordWrap(True)
            layout.addWidget(help_text)
            chooser.resize(560, 160)
            buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
            buttons.accepted.connect(chooser.accept)
            buttons.rejected.connect(chooser.reject)
            layout.addWidget(buttons)
            if chooser.exec() != QDialog.Accepted:
                return
            segments = parse_segments(entry.text())
            destination, _ = QFileDialog.getSaveFileName(self, 'Save private diagnostic',
                f'private-audio-{path.name[:8]}-segments.zip', 'Private audio archive (*.zip)')
            if not destination:
                return
            self._exporting = True
            self.export.setEnabled(False)
            self.delete.setEnabled(False)
            self.sessions.setEnabled(False)
            self.detail.setText('Exporting private audio in background… Capture and playback continue.')
            def work():
                try:
                    self.export_finished.emit(export_segments(path / 'speech-audit.sqlite', destination, segments), '')
                except Exception as exc:
                    self.export_finished.emit(None, safe_error(exc))
            threading.Thread(target=work, name='diagnostic-export', daemon=True).start()
        except Exception as exc:
            QMessageBox.warning(self, 'Audio diagnostic', safe_error(exc))

    def _export_done(self, result, error):
        self._exporting = False
        self.export.setEnabled(True)
        self.delete.setEnabled(True)
        self.sessions.setEnabled(True)
        if error:
            QMessageBox.warning(self, 'Audio diagnostic', error)
        else:
            self.detail.setText(f'Exported {len(result["exported_segments"])} segments. '
                                f'Missing: {segment_ranges(result["missing_segments"]) or "none"}. '
                                f'Failures: {len(result["failures"])}. See manifest.json. No provider was called.')

    def done(self, result):
        if not self._exporting:
            super().done(result)

    def _delete(self):
        path = self._path()
        if not path:
            return
        if QMessageBox.question(self, 'Delete local recording',
                'Permanently delete this drained session’s local audio diagnostics? '
                'Saved transcript/history remain. Pending or unresolved speech will be protected.',
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        try:
            delete_drained_recording(path, self.root, self.active)
            self._reload()
        except Exception as exc:
            QMessageBox.warning(self, 'Recording protected', safe_error(exc))
