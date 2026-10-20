"""Reference-inspired report UI, concise generation, and editing regressions."""
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, patch

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QPushButton
from church_translator.config import load_config, save_user_settings
from church_translator.dialogs import AISummaryDialog
from church_translator.session_manager import SessionManager, TranscriptSegment
from church_translator.summarizer import StructuredSummary, SermonSummarizer, SUMMARY_PROMPT_TEMPLATE


class SummaryReportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env = patch.dict(os.environ, {'CHURCH_TRANSLATOR_APPDATA': self.temp.name})
        self.env.start()
        self.addCleanup(self.env.stop)
        save_user_settings({'summary': {'language': 'en'}})
        self.config = replace(load_config(), gemini_api_key=None, openai_api_key=None)
        self.manager = SessionManager(Path(self.temp.name) / 'sessions')
        self.sid = self.manager.start_session('Faith and Fulfilment')
        self.segment = TranscriptSegment(0, 1, '10:00', 'Runātājs pārdomā ticību, bagātību un dzīves piepildījumu.', '')
        self.manager.append_segment(self.segment)
        self.manager.end_session()
        self.report = StructuredSummary('Faith and Fulfilment',
            'The speaker reflects on wealth and personal fulfilment.',
            ['Possessions alone do not define fulfilment.', 'Faith develops differently for each person.'],
            highlights=['The speaker describes an imagined conversation with Jesus.'], report_style=True)
        self.manager.save_session_summary(self.sid, self.report.to_report_text())
        self.dialog = AISummaryDialog(SermonSummarizer(self.config), self.manager.get_session(self.sid),
            [self.segment], self.manager)
        self.dialog.show()
        self.app.processEvents()
        self.addCleanup(self.dialog.close)

    def test_report_has_three_sections_and_no_reference_builder(self):
        self.assertEqual(set(self.dialog.section_cards), {'main_message', 'key_teachings', 'highlights'})
        self.assertFalse(hasattr(self.dialog, 'add_bible_btn'))
        self.assertFalse(hasattr(self.dialog, 'add_section_combo'))
        self.assertEqual(self.dialog.overview_heading.text(), 'EXECUTIVE OVERVIEW')
        self.assertFalse(any('Bible' in button.text() for button in self.dialog.findChildren(QPushButton)))

    def test_reading_mode_has_wrapping_selectable_text_without_inner_scroll(self):
        self.assertTrue(self.dialog.card_main_msg.isReadOnly())
        self.assertTrue(all(editor.isReadOnly() for editor in self.dialog._teaching_edits))
        self.assertTrue(all(not button.isVisible() for button in self.dialog._point_tools))
        self.assertEqual(self.dialog.card_main_msg.verticalScrollBarPolicy(), Qt.ScrollBarAlwaysOff)
        self.assertFalse(self.dialog.card_main_msg.viewport().autoFillBackground())

    def test_edit_button_is_direct_action_and_other_edit_actions_remain_available(self):
        self.assertEqual(self.dialog.edit_btn.objectName(), 'editButton')
        self.assertIsInstance(self.dialog.edit_btn, QPushButton)
        self.assertIsNone(self.dialog.edit_btn.menu())
        self.assertIsNotNone(self.dialog.edit_actions_btn.menu())
        self.assertGreaterEqual(self.dialog.edit_btn.minimumWidth(), 96)
        self.assertGreaterEqual(self.dialog.edit_btn.minimumHeight(), 36)

    def test_edit_toggle_updates_text_save_and_returns_to_reading(self):
        self.dialog.edit_btn.click()
        self.assertFalse(self.dialog.card_main_msg.isReadOnly())
        self.assertTrue(self.dialog.add_point_btn.isVisible())
        self.dialog.card_main_msg.setPlainText('Edited executive overview.')
        self.dialog._highlight_edits[0].setText('Edited speech highlight.')
        self.dialog.save_btn.click()
        saved = self.manager.get_session(self.sid).summary_text_en
        self.assertIn('Edited executive overview.', saved)
        self.assertIn('Edited speech highlight.', saved)
        self.assertNotIn('Bible References:', saved)
        self.assertEqual(self.dialog.save_btn.text(), 'Saved')
        self.dialog.edit_btn.click()
        self.assertTrue(self.dialog.card_main_msg.isReadOnly())
        self.assertFalse(self.dialog.add_point_btn.isVisible())

    def test_points_can_be_added_removed_and_renumbered_in_edit_mode(self):
        self.dialog.edit_btn.click()
        self.dialog.add_point_btn.click()
        self.dialog._teaching_edits[-1].setText('A third point from the transcript.')
        rows = self.dialog.teachings_layout
        remove = rows.itemAt(0).widget().findChild(QPushButton)
        remove.click()
        self.assertEqual(len(self.dialog._teaching_edits), 2)
        self.assertIn('1. Faith develops differently', self.dialog.get_current_summary_text())
        self.assertIn('2. A third point', self.dialog.get_current_summary_text())

    def test_copy_matches_visible_report_and_gives_immediate_feedback(self):
        clipboard = MagicMock()
        with patch('PySide6.QtGui.QGuiApplication.clipboard', return_value=clipboard):
            self.dialog.copy_btn.click()
        clipboard.setText.assert_called_once_with(self.dialog.get_current_summary_text())
        self.assertEqual(self.dialog.copy_btn.text(), 'Copied')

    def test_export_uses_the_same_concise_report(self):
        target = Path(self.temp.name) / 'report.txt'
        with patch('church_translator.dialogs.QFileDialog.getSaveFileName', return_value=(str(target), 'Plain Text (*.txt)')), \
                patch('church_translator.dialogs.QMessageBox.information'):
            self.dialog.download_btn.click()
        self.assertEqual(target.read_text(encoding='utf-8').strip(), self.dialog.get_current_summary_text())
        self.assertIn('Executive Overview:', target.read_text(encoding='utf-8'))

    def test_language_switch_preserves_edits_and_localized_report_roundtrips(self):
        self.dialog.edit_btn.click()
        self.dialog.card_main_msg.setPlainText('English edit before language change.')
        latvian = StructuredSummary('Ticība un piepildījums', 'Runātājs pārdomā ticību un dzīves piepildījumu.',
            ['Mantiskās vērtības nav vienīgais piepildījuma avots.'],
            highlights=['Tiek uzdots jautājums par sarunu ar Jēzu.'], report_style=True)
        self.manager.save_session_summary(self.sid, latvian.to_report_text('lv'), 'lv')
        self.dialog.lang_combo.setCurrentIndex(1)
        self.app.processEvents()
        self.assertEqual(self.dialog.overview_heading.text(), 'ĪSS PĀRSKATS')
        self.assertFalse(any(editor.isVisible() and 'Possessions alone' in editor.text()
            for editor in self.dialog.report_scroll.findChildren(type(self.dialog.card_main_msg))))
        self.assertIn('Īss pārskats:', self.dialog.get_current_summary_text())
        restored = StructuredSummary.from_text(self.dialog.get_current_summary_text())
        self.assertEqual(restored.highlights, latvian.highlights)
        self.dialog.lang_combo.setCurrentIndex(0)
        self.assertEqual(self.dialog.card_main_msg.toPlainText(), 'English edit before language change.')

    def test_long_point_wraps_and_grows_when_the_window_narrows(self):
        self.dialog.edit_btn.click()
        self.dialog._teaching_edits[0].setText('The speaker describes a personal struggle and reflects on the different ways people develop faith over the course of a meaningful life. ' * 2)
        self.dialog.resize(980, 800)
        self.app.processEvents()
        wide = self.dialog._teaching_edits[0].height()
        self.dialog.resize(640, 560)
        self.app.processEvents()
        narrow = self.dialog._teaching_edits[0].height()
        self.assertGreater(narrow, wide)
        editor = self.dialog._teaching_edits[0]
        self.assertGreaterEqual(editor.height(), editor.document().size().height())
        self.assertEqual(editor.horizontalScrollBar().maximum(), 0)
        for button in (self.dialog.copy_btn, self.dialog.download_btn, self.dialog.save_btn, self.dialog.close_btn):
            self.assertGreaterEqual(button.width(), button.minimumSizeHint().width(), button.text())

    def test_legacy_summary_is_not_rewritten_just_by_opening(self):
        old = StructuredSummary('Old title', 'Old overview.', ['A core point.'], ['John 3:16'],
            'An older application.', examples=['An illustration used in the speech.'])
        original = old.to_clean_text()
        self.manager.save_session_summary(self.sid, original)
        self.dialog._check_cached_summary()
        self.assertEqual(self.manager.get_session(self.sid).summary_text_en, original)
        self.assertNotIn('Bible References:', self.dialog.get_current_summary_text())
        self.assertNotIn('Practical Takeaway:', self.dialog.get_current_summary_text())
        self.assertEqual(self.dialog._highlight_edits[0].text(), 'An illustration used in the speech.')

    def test_new_prompt_and_response_have_only_the_requested_report_sections(self):
        self.assertNotIn('"bible_references":', SUMMARY_PROMPT_TEMPLATE)
        self.assertNotIn('"practical_takeaway":', SUMMARY_PROMPT_TEMPLATE)
        self.assertIn('"highlights":', SUMMARY_PROMPT_TEMPLATE)
        summary = SermonSummarizer(self.config)
        response = '{"title":"Faith","main_message":"The speaker reflects on faith.","key_teachings":["Faith is personal."],"highlights":["The speaker poses a question."]}'
        with patch.object(summary, '_call_ai', return_value=response):
            output = summary.generate_summary([self.segment])
        self.assertIn('Executive Overview:', output)
        self.assertIn('Main Speech Highlights & Key Insights:', output)
        self.assertNotIn('Closing Reflection:', output)
        restored = StructuredSummary.from_text(output)
        self.assertTrue(restored.report_style)
        self.assertEqual(restored.highlights, ['The speaker poses a question.'])
