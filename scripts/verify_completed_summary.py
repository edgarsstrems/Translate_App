"""Opt-in rendered completed-session summaries: generate, regenerate, History.

Uses a fixed synthetic transcript, real Qt dialogs/workers and configured Gemini.
Capture/Stop's provider drain is simulated; no microphone or saved preferences
are changed. API consent and provider limits remain in force.
"""
import argparse
from dataclasses import replace
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
from church_translator.config import load_config
from church_translator.dialogs import AISummaryDialog, SessionHistoryDialog
from church_translator.main import MainWindow
from church_translator.session_manager import TranscriptSegment


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('report', type=Path)
    parser.add_argument('--live', action='store_true')
    args = parser.parse_args()
    if not args.live:
        parser.error('--live is required for Gemini usage')
    args.report = args.report.resolve()
    args.report.parent.mkdir(parents=True, exist_ok=True)
    cfg = replace(load_config(), auto_return_to_gemini=False)
    app = QApplication.instance() or QApplication([])
    report = {'ok': False, 'fixture': 'fixed synthetic transcript, mocked capture drain, real Gemini and Qt', 'stages': []}
    with tempfile.TemporaryDirectory(dir=args.report.parent) as temp, patch.dict(os.environ, {'CHURCH_TRANSLATOR_APPDATA': temp}):
        window = MainWindow()
        window._active_config = lambda: cfg
        window.summarizer.config = cfg
        window.show()
        sid = window.session_manager.start_session('Controlled summary verification')
        source = ('Šodien runājam par mieru un cerību. Pēc darba cilvēkam ir vajadzīga atpūta. '
                  'Mēs varam palīdzēt cits citam ar laipnu vārdu un uzmanīgu klausīšanos. '
                  'Runātājs atgādina, ka ne vienmēr zinām otra cilvēka grūtības. '
                  'Viņš aicina veltīt laiku ģimenei un draugiem. '
                  'Neliela palīdzība var būt nozīmīga, pat ja nevaram atrisināt visas problēmas. '
                  'Noslēgumā runātājs pateicas klātesošajiem un novēl mierīgu nedēļu.')
        window.engine = SimpleNamespace(stop=lambda: window.session_manager.append_segment(
            TranscriptSegment(0, time.time(), '12:00', source, '')), completion_info={'capture_pending': 0})
        window._set_running(True)
        window.stop_button.click()
        deadline = time.monotonic() + 5
        while window._stopping and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(.005)
        assert not window._stopping and window.engine is None
        assert window.session_manager.get_session(sid).state == 'completed'
        assert window.session_manager.completion_snapshot(sid)['segment_count'] == 1
        assert not window.context_manager.is_active
        report['completed_snapshot'] = True
        report['language'] = 'lv'
        state = {'calls': 0, 'started': False, 'deadline': time.monotonic() + 70}
        errors = []
        timer = QTimer()
        timer.setInterval(50)

        def advance():
            try:
                dialog = next((widget for widget in app.topLevelWidgets()
                               if isinstance(widget, AISummaryDialog) and widget.isVisible()), None)
                if not dialog:
                    return
                assert dialog.session.session_id == sid
                if time.monotonic() > state['deadline']:
                    raise TimeoutError('Summary workflow did not finish within its bounded deadline')
                if not state['started']:
                    state['started'] = True
                    dialog.generate_btn.click()
                    return
                if dialog._is_generating or (dialog._worker_thread and dialog._worker_thread.isRunning()):
                    return
                # Let queued layout/paint events settle after the worker result;
                # an immediate grab can omit newly inserted report rows.
                if 'settle_until' not in state:
                    state['settle_until'] = time.monotonic() + .25
                    return
                if time.monotonic() < state['settle_until']:
                    return
                state.pop('settle_until')
                saved = window.session_manager.get_session(sid).summary_text_lv
                if not saved.strip():
                    raise AssertionError('Real summary was not saved; ' + dialog.status_label.text())
                report['stages'].append({'stage': ('after_end', 'regeneration', 'history')[state['calls']],
                    'saved': True, 'session_matches': True, 'provider': dialog.summarizer.last_provider_used,
                    'model': dialog.summarizer.last_model_used, 'summary_words': len(saved.split())})
                state['calls'] += 1
                dialog.grab().save(str(args.report.with_name(f'summary-stage-{state["calls"]}.png')))
                if state['calls'] == 1:
                    state['started'] = False
                else:
                    dialog.close()
            except Exception as exc:
                errors.append(str(exc))
                for widget in app.topLevelWidgets():
                    if isinstance(widget, (AISummaryDialog, SessionHistoryDialog)):
                        widget.close()
                timer.stop()

        timer.timeout.connect(advance)
        timer.start()
        window.summary_button.click()  # Real modal dialog; timer drives its buttons.
        if not errors:
            history = SessionHistoryDialog(window.session_manager, window.summarizer, window)
            history.show()
            history.table.selectRow(0)
            state['started'] = False
            history._open_summary()
            history.close()
        timer.stop()
        report.update(ok=not errors and state['calls'] == 3, errors=errors,
                      microphone_active=False, saved_preferences_changed=False)
        window.close()
    args.report.write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report))
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
