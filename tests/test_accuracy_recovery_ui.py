import os
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

import numpy as np
from PySide6.QtCore import Qt, QPoint
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QToolButton

from church_translator.audio import ChunkRecorder
from church_translator.main import MainWindow


class AdcTimingTests(unittest.TestCase):
    def capture(self, timestamps):
        errors, chunks = [], []
        recorder = ChunkRecorder(0, 8, 0, 1.5, .7, .004, .025,
            lambda _, audio, *_args: chunks.append(audio), errors.append, dynamic_mode=True)
        for adc in timestamps:
            recorder._callback(np.full((1600, 1), .01, 'float32'), 1600,
                               SimpleNamespace(inputBufferAdcTime=adc), None)
        recorder.stop()
        self.assertEqual(sum(len(a) for a in chunks), len(timestamps) * 1600)
        return errors

    def test_unavailable_adc_clock_does_not_report_loss_or_discard_audio(self):
        self.assertEqual(self.capture([0, 0, float('nan'), -1, None, 1, 1.1]), [])

    def test_forward_gap_warns_and_repeated_clock_is_bounded_and_not_claimed_as_loss(self):
        errors = self.capture([1, 1.1, 1.4, 1.4, 1.4, 1.4])
        self.assertEqual(len(errors), 2)
        self.assertIn('timing gap: 0.200000s', errors[0])
        self.assertIn('does not establish missing audio', errors[1])


class ResponsiveHeaderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        env = patch.dict(os.environ, {'CHURCH_TRANSLATOR_APPDATA': temp.name})
        env.start()
        self.addCleanup(env.stop)
        devices = patch('church_translator.main.list_audio_devices', return_value=[])
        devices.start()
        self.addCleanup(devices.stop)
        self.window = MainWindow()
        self.addCleanup(self.window.close)
        self.window.show()
        self.app.processEvents()

    def test_tabs_are_clickable_outside_page_scroll_with_changing_measurements(self):
        w = self.window
        for width, height in ((1280, 860), (960, 700), (720, 540), (640, 480)):
            w.resize(width, height)
            self.app.processEvents()
            header_geometry = w.status_row.geometry()
            bar = w.tabs.tabBar()
            for latency in (10, 99999.9, 0.2):
                w.set_latency(latency)
                w.set_input_level(.016, .01)
                self.app.processEvents()
                self.assertEqual(w.size().width(), width)
                self.assertEqual(w.status_row.geometry(), header_geometry)
                self.assertTrue(w.centralWidget().rect().contains(header_geometry))
                self.assertLess(header_geometry.bottom(), w.tabs.geometry().top())
                if w.tabs.currentIndex() == 0:
                    for button in (w.start_button, w.pause_button, w.stop_button, w.context_button,
                                   w.export_button, w.summary_button, w.history_button,
                                   w.refresh_button, w.clear_text_button):
                        self.assertGreaterEqual(button.width(), button.sizeHint().width(), button.text())
                for label in (w.status_pill, w.speed_badge, w.latency_label, w.input_level):
                    self.assertTrue(w.status_row.rect().contains(label.geometry()))
                for index in range(4):
                    # Use the visible overflow buttons, then click the target tab;
                    # do not programmatically select the tab being tested.
                    for _ in range(10):
                        rect = bar.tabRect(index)
                        buttons = [b for b in bar.findChildren(QToolButton) if b.isVisible()]
                        if bar.rect().contains(rect) and all(not rect.intersects(b.geometry()) for b in buttons):
                            break
                        arrow = Qt.LeftArrow if rect.left() < 0 else Qt.RightArrow
                        button = next(b for b in buttons if b.arrowType() == arrow)
                        self.assertTrue(button.isEnabled())
                        QTest.mouseClick(button, Qt.LeftButton)
                        self.app.processEvents()
                    rect = bar.tabRect(index)
                    self.assertTrue(bar.rect().contains(rect), (width, index, rect))
                    buttons = [b for b in bar.findChildren(QToolButton) if b.isVisible()]
                    self.assertTrue(all(not rect.intersects(b.geometry()) for b in buttons))
                    QTest.mouseClick(bar, Qt.LeftButton, pos=rect.center())
                    self.assertEqual(w.tabs.currentIndex(), index)
            w.tabs.setCurrentIndex(0)
            w.main_scroll.horizontalScrollBar().setValue(w.main_scroll.horizontalScrollBar().maximum())
            w.main_scroll.verticalScrollBar().setValue(w.main_scroll.verticalScrollBar().maximum())
            self.app.processEvents()
            self.assertEqual(w.status_row.geometry(), header_geometry)
            self.assertTrue(w.centralWidget().rect().contains(
                bar.mapTo(w.centralWidget(), QPoint(0, 0))))

    def test_compact_measurements_modes_and_paused_state_survive_queued_status(self):
        w = self.window
        for mode, badge in (('dynamic', 'D'), ('balanced', 'B'), ('low_latency', 'L'), ('custom', 'C')):
            w.speed_mode_combo.setCurrentIndex(w.speed_mode_combo.findData(mode))
            self.assertEqual(w.speed_badge.text(), badge)
            self.assertTrue(w.speed_badge.toolTip())
        w.set_latency(10)
        w.set_latency(13)
        self.assertEqual(w.latency_label.text(), 'L: 13.0s (avg: 11.5s)')
        self.assertIn('p95: 13.0s', w.latency_label.toolTip())
        for rms, peak, expected in ((.016, .01, 8), (1, 1, 100), (0, 0, 0)):
            w.set_input_level(rms, peak)
            self.assertEqual(w.input_level.text(), f'Mic: {expected}%')
        engine = MagicMock()
        w.engine = engine
        w._set_running(True)
        self.assertEqual(w.status_pill.text(), 'Live')
        w.toggle_pause()
        engine.pause.assert_called_once()
        for message in ('Listening: fixture', 'Segment 1: Gemini complete', 'Waiting for Gemini access'):
            w.set_status(message)
            self.assertEqual(w.status_pill.text(), 'Pause')
        w.toggle_pause()
        engine.resume.assert_called_once()
        self.assertEqual(w.status_pill.text(), 'Live')
        w.set_status('Translation blocked: permission denied')
        self.assertEqual(w.status_pill.text(), 'Blocked')
        w.engine = None
        w.append_transcript('Saglabāta runa.')
        w.append_translation('en', 'Preserved speech.')
        w.context_manager.set_text('Temporary sermon context')
        w._finished_stop()
        w.set_status('Listening: late fixture')
        self.assertEqual(w.status_pill.text(), 'Idle')
        self.assertEqual(w.latvian_text.toPlainText(), 'Saglabāta runa.')
        self.assertEqual(w.english_text.toPlainText(), 'Preserved speech.')
        self.assertFalse(w.context_manager.is_active)


if __name__ == '__main__':
    unittest.main()
