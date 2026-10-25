"""Rendered synthetic Qt stress; no APIs or microphone capture."""
import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import QObject, QEvent, QThread, QTimer
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import QApplication
from church_translator.main import MainWindow


class PaintCounter(QObject):
    def __init__(self):
        super().__init__()
        self.paints = 0

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Paint:
            self.paints += 1
        return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('report', type=Path)
    args = parser.parse_args()
    args.report = args.report.resolve()
    args.report.parent.mkdir(parents=True, exist_ok=True)
    app = QApplication.instance() or QApplication([])
    with tempfile.TemporaryDirectory(dir=args.report.parent) as directory, patch.dict(
            os.environ, {'CHURCH_TRANSLATOR_APPDATA': directory}):
        window = MainWindow()
        window.resize(1280, 900)
        window.show()
        app.processEvents()
        panels = [window.latvian_text, window.english_text, window.russian_text]
        window.secondary_language_combo.setCurrentIndex(window.secondary_language_combo.findData('lt'))
        changes, wrong_threads, counters = [], [], []
        for panel in panels:
            counter = PaintCounter()
            panel.viewport().installEventFilter(counter)
            counters.append(counter)
            panel.document().contentsChange.connect(lambda pos, removed, added: changes.append((pos, removed, added)))
            panel.document().contentsChanged.connect(lambda: wrong_threads.append(True)
                if QThread.currentThread() != app.thread() else None)
        ticks = [0]
        heartbeat = QTimer()
        heartbeat.timeout.connect(lambda: ticks.__setitem__(0, ticks[0] + 1))
        heartbeat.start(5)
        cpu, started = time.process_time(), time.monotonic()
        for batch in range(10):
            def emit_batch(batch=batch):
                for i in range(batch * 100, (batch + 1) * 100):
                    window.signals.transcript.emit(f'Synthetic Latvian segment {i}: mierīga balss, skaidra doma.')
                    window.signals.translation.emit('en', f'Synthetic English segment {i}: a clear thought spoken calmly.')
                    window.signals.translation.emit('lt', f'Synthetic Lithuanian segment {i}: aiški mintis.')
                    window.signals.level.emit(.01, .05)
                    if i % 10 == 0:
                        window.signals.status.emit(f'Synthetic simultaneous activity {i}')
            worker = threading.Thread(target=emit_batch)
            worker.start()
            while worker.is_alive():
                app.processEvents()
                worker.join(.001)
            app.processEvents()
            if batch in (3, 5):
                window.resize(1100 if batch == 3 else 1280, 800 if batch == 3 else 900)
                app.processEvents()
            if batch == 8:
                panel = panels[0]
                cursor = panel.textCursor()
                cursor.setPosition(20)
                cursor.setPosition(40, QTextCursor.KeepAnchor)
                panel.setTextCursor(cursor)
                panel.verticalScrollBar().setValue(40)
                panel.setFocus()
                app.processEvents()
                selected, scroll = cursor.selectedText(), panel.verticalScrollBar().value()
                before = panel.viewport().grab().toImage()
                window.grab().save(str(args.report.with_name('ui-before.png')))
        app.processEvents()
        after = panels[0].viewport().grab().toImage()
        before.save(str(args.report.with_name('viewport-before.png')))
        after.save(str(args.report.with_name('viewport-after.png')))
        window.grab().save(str(args.report.with_name('ui-after.png')))
        print(json.dumps({'scroll_before': scroll, 'scroll_after': panels[0].verticalScrollBar().value(),
                          'selection_before': selected, 'selection_after': panels[0].textCursor().selectedText()}))
        assert before == after, 'Reading viewport changed while appending below it'
        assert panels[0].textCursor().selectedText() == selected
        assert panels[0].verticalScrollBar().value() == scroll
        assert all(panel.document().blockCount() == 1000 for panel in panels)
        assert all('segment 0:' in panel.toPlainText() for panel in panels)
        assert not any(removed for _, removed, _ in changes), 'Document was cleared or trimmed'
        assert not wrong_threads, 'GUI updated from a worker thread'
        window.grab().save(str(args.report.with_name('ui-after.png')))
        panels[0].verticalScrollBar().setValue(panels[0].verticalScrollBar().maximum())
        window.append_transcript('Follow-bottom resumes.')
        assert panels[0].verticalScrollBar().value() == panels[0].verticalScrollBar().maximum()
        report = {'ok': True, 'kind': 'rendered synthetic Qt stress, no external providers',
            'segments_per_panel': 1000, 'rapid_updates_per_batch': 100,
            'gui_thread_only': True, 'manual_reading_viewport_pixel_identical': True,
            'selection_preserved': True, 'follow_bottom_resumes': True, 'resize_during_updates': True,
            'document_removals': 0, 'panel_paint_events': [c.paints for c in counters],
            'event_loop_heartbeat_ticks': ticks[0], 'waveform': 'existing static decorative bars; input-level events exercised',
            'wall_seconds': round(time.monotonic() - started, 3), 'cpu_seconds': round(time.process_time() - cpu, 3),
            'limits': 'Current Windows host only; synthetic event stress is not natural-service or all-driver acceptance.'}
        window.close()
        heartbeat.stop()
        app.processEvents()
    args.report.write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report))


if __name__ == '__main__':
    main()
