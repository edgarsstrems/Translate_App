"""Catalog routing, safe search, persisted settings and voice selection."""
import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QMessageBox
from church_translator.config import load_config, load_user_settings, save_user_settings
from church_translator.languages import OUTPUT_LANGUAGES, default_voice, web_language_code
from church_translator.language_selector import LanguageSelector
from church_translator.services import TextToSpeech, Translator
from church_translator.glossary import Glossary


class OutputLanguageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.env = patch.dict(os.environ, {'CHURCH_TRANSLATOR_APPDATA': temporary.name})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.config = replace(load_config(), gemini_api_key=None, openai_api_key=None)

    def test_selector_search_commits_only_real_languages(self):
        combo = LanguageSelector()
        self.assertEqual(combo.count(), 62)
        self.assertEqual(combo.completer().filterMode(), Qt.MatchContains)
        combo.setEditText('japanese')
        combo._commit_search()
        self.assertEqual(combo.currentData(), 'ja')
        combo.setEditText('not a language')
        combo._commit_search()
        self.assertEqual(combo.currentText(), 'Japanese')
        self.assertEqual(combo.currentData(), 'ja')
        self.assertEqual(combo.findData('en'), -1)
        combo.close()

    def test_every_output_has_a_matching_cloud_voice(self):
        service = TextToSpeech(self.config)
        service._cloud_tts_disabled = False
        service._client = MagicMock()
        service._client.synthesize_speech.return_value.audio_content = b'audio'
        for code, (name, locale, _) in OUTPUT_LANGUAGES.items():
            with self.subTest(language=code):
                self.assertEqual(service.synthesize('sample', code, strict=True), b'audio')
                voice = service._client.synthesize_speech.call_args.kwargs['voice']
                self.assertEqual(voice.language_code, locale)
                self.assertTrue(voice.name.startswith(locale + '-'), name)
        self.assertEqual(service._voices['lt'].voice_name, self.config.lithuanian_voice)
        self.assertEqual(service._voices['ru'].voice_name, self.config.russian_voice)

    def test_all_language_and_voice_preferences_roundtrip(self):
        settings = {'voices': {code: {'voice': default_voice(self.config, code), 'rate': .95}
                               for code in OUTPUT_LANGUAGES}}
        for code in OUTPUT_LANGUAGES:
            if code == 'en':
                continue
            settings['languages'] = {'secondary_language': code, 'russian_enabled': True}
            save_user_settings(settings)
            restored = load_user_settings()
            self.assertEqual(restored['languages']['secondary_language'], code)
            self.assertEqual(set(restored['voices']), set(OUTPUT_LANGUAGES))

    def test_invalid_codes_and_cross_language_voices_are_rejected(self):
        save_user_settings({'languages': {'secondary_language': ['de']}, 'voices': {
            'de': {'voice': 'fr-FR-Standard-F'}, 'unknown': {'voice': 'unknown'}}})
        restored = load_user_settings()
        self.assertNotIn('secondary_language', restored.get('languages', {}))
        self.assertFalse(restored.get('voices'))

    def test_catalog_request_uses_selected_locale(self):
        service = TextToSpeech(self.config)
        service._client = MagicMock()
        service._client.list_voices.return_value.voices = [
            SimpleNamespace(name='ja-JP-Standard-A', language_codes=['ja-JP'], ssml_gender=1),
            SimpleNamespace(name='fr-FR-Standard-F', language_codes=['fr-FR'], ssml_gender=1)]
        self.assertEqual([voice['name'] for voice in service.list_voices('ja')], ['ja-JP-Standard-A'])
        self.assertEqual(service._client.list_voices.call_args.kwargs['language_code'], 'ja-JP')

    def test_dashboard_routes_and_restores_new_language(self):
        from church_translator.main import MainWindow
        with patch('church_translator.main.load_config', return_value=self.config), \
                patch.object(MainWindow, '_check_crash_recovery'):
            window = MainWindow()
            self.addCleanup(window.close)
            combo = window.secondary_language_combo
            combo.setCurrentIndex(combo.findData('ja'))
            window.append_translation('ja', '希望について語ります。')
            window.append_translation('de', 'Ignored stale language')
            self.assertEqual(window.russian_text.toPlainText(), '希望について語ります。')
            self.assertIn('Japanese', window.russian_enabled.text())
            window._save_user_settings()
            reopened = MainWindow()
            self.addCleanup(reopened.close)
            self.assertEqual(reopened.secondary_language_combo.currentData(), 'ja')

    def test_premium_only_language_requires_consent_once(self):
        from church_translator.main import MainWindow
        with patch('church_translator.main.load_config', return_value=self.config), \
                patch.object(MainWindow, '_check_crash_recovery'):
            window = MainWindow()
            self.addCleanup(window.close)
            window.russian_enabled.setChecked(True)
            window.secondary_language_combo.setCurrentIndex(window.secondary_language_combo.findData('hr'))
            config = replace(self.config, google_application_credentials='configured.json')
            with patch('church_translator.main.QMessageBox.question', return_value=QMessageBox.No):
                self.assertFalse(window._confirm_secondary_voice(config))
            with patch('church_translator.main.QMessageBox.question', return_value=QMessageBox.Yes) as consent:
                self.assertTrue(window._confirm_secondary_voice(config))
                self.assertTrue(window._confirm_secondary_voice(config))
                self.assertEqual(consent.call_count, 1)

    def test_translation_language_names_and_regional_web_codes(self):
        translator = Translator(replace(self.config, gemini_api_key='mock-key'), Glossary(), lambda _: None)
        for code in ('de', 'ja', 'ar', 'zh-TW', 'en-GB'):
            with patch.object(translator, '_request_translation', return_value='translated') as request:
                self.assertEqual(translator._translate_with_gemini('Ticība.', code), 'translated')
            self.assertIn(OUTPUT_LANGUAGES[code][0], request.call_args.args[0])
        self.assertEqual(web_language_code('en-GB'), 'en')
        self.assertEqual(web_language_code('fr-CA'), 'fr')
        self.assertEqual(web_language_code('zh-TW'), 'zh-TW')
