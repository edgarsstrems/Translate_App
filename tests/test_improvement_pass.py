"""Real Qt/SQLite/segmentation regressions; network and audio hardware are mocked."""
import io
import os
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import soundfile as sf
from PySide6.QtWidgets import QApplication, QPlainTextEdit
from church_translator.audio import ChunkRecorder, OrderedAudioPlayer, AudioDevice, SAMPLE_RATE
from church_translator.config import load_config, save_user_settings
from church_translator.sermon_context import SermonContextManager
from church_translator.session_manager import SessionManager, TranscriptSegment
from church_translator.summarizer import SermonSummarizer
from church_translator.dialogs import AISummaryDialog
from church_translator.main import MainWindow
from church_translator.ui_compat import append_live_text
from church_translator.services import TextToSpeech, LocalWhisperTranscriber, LATVIAN_SERMON_STT_PROMPT
from church_translator.glossary import Glossary


class ImprovementTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        env = patch.dict(os.environ, {'CHURCH_TRANSLATOR_APPDATA': self.temp.name})
        env.start()
        self.addCleanup(env.stop)
        self.config = replace(load_config(), gemini_api_key='test', openai_api_key='test',
                              google_application_credentials=None, openai_fallback_enabled=False)

    def context(self):
        ctx = SermonContextManager(self.root / 'context.json')
        ctx.set_text('Romiešiem 8:28. Ticība un cerība. Dieva žēlastība palīdz cilvēkiem. '
                     'Pāvils māca pacietību un uzticēšanos.', 'Sermon topic')
        return ctx

    def test_context_requires_current_evidence_and_never_supplies_unspoken_verse(self):
        ctx = self.context()
        self.assertEqual(ctx.get_compact_prompt_hint('Rīt autobuss izbrauks pulksten deviņos.'), '')
        self.assertEqual(ctx.get_compact_prompt_hint('Dieva vārdā lūdzu novietojiet automašīnas stāvvietā.'), '')
        hint = ctx.get_compact_prompt_hint('Ticība un cerība palīdz cilvēkiem.')
        self.assertIn('ticība', hint)
        self.assertNotIn('8:28', hint)
        self.assertNotIn('Sermon topic', hint)
        # No relevance inherited from a preceding related segment.
        self.assertEqual(ctx.get_compact_prompt_hint('Tagad sākas paziņojumi par autobusu.'), '')

    def test_context_restart_is_empty_and_explicit_template_load_still_works(self):
        ctx = self.context()
        reopened = SermonContextManager(ctx.storage_path)
        self.assertFalse(reopened.is_active)
        reopened.load()
        self.assertTrue(reopened.is_active)
        reopened.clear()
        self.assertFalse(reopened.storage_path.exists())

    def test_local_stt_has_no_implicit_sermon_topic_or_biblical_seed(self):
        stt = LocalWhisperTranscriber(replace(self.config, whisper_hotwords=''), Glossary({}, {}), lambda _: None)
        self.assertIsNone(stt._build_hotwords())
        self.assertNotIn('Kristus', LATVIAN_SERMON_STT_PROMPT)
        self.assertNotIn('sprediķis', LATVIAN_SERMON_STT_PROMPT)

    def test_dynamic_retains_440_and_600ms_pauses_with_exact_sample_continuity(self):
        for pause in (.44, .60):
            emitted = []
            rec = ChunkRecorder(0, 8, 0, 1.5, .6, .004, .025,
                                lambda seq, audio, *_: emitted.append(audio), self.fail, dynamic_mode=True)
            speech = np.resize(np.array([.05, -.05], dtype='float32'), 5 * SAMPLE_RATE)
            quiet = np.zeros(int(pause * SAMPLE_RATE), dtype='float32')
            rec._consume(speech)
            rec._consume(quiet)
            self.assertEqual(emitted, [])
            ending = speech[:SAMPLE_RATE]
            rec._consume(ending)
            rec._consume(np.zeros(SAMPLE_RATE, dtype='float32'))
            rec.stop()
            actual = np.concatenate(emitted)
            expected = np.concatenate((speech, quiet, ending))
            np.testing.assert_array_equal(actual[actual != 0], expected[expected != 0])
            self.assertEqual(len(emitted), 1)

    def test_each_live_panel_follows_bottom_and_preserves_manual_read_position(self):
        panel = QPlainTextEdit()
        panel.resize(300, 120)
        panel.show()
        self.addCleanup(panel.close)
        append_live_text(panel, '\n'.join(str(i) for i in range(80)))
        self.app.processEvents()
        bar = panel.verticalScrollBar()
        self.assertEqual(bar.value(), bar.maximum())
        bar.setValue(12)
        append_live_text(panel, 'new text')
        self.assertEqual(bar.value(), 12)
        bar.setValue(bar.maximum())
        append_live_text(panel, 'latest text')
        self.assertEqual(bar.value(), bar.maximum())

    def dialog(self):
        manager = SessionManager(self.root / 'sessions')
        sid = manager.start_session()
        segment = TranscriptSegment(1, 1, '10:00', 'Ticība un cerība.', '')
        manager.append_segment(segment)
        dialog = AISummaryDialog(SermonSummarizer(self.config), manager.get_session(sid), [segment], manager)
        self.addCleanup(dialog.close)
        return dialog, manager, sid

    def test_summary_latvian_default_autosave_creation_time_and_restart(self):
        dialog, manager, sid = self.dialog()
        self.assertEqual(dialog.lang_combo.currentData(), 'lv')
        dialog.summarizer.last_provider_used = 'fixture'
        dialog.summarizer.last_model_used = 'fixture-model'
        text = 'Galvenā doma:\nTicība dod cerību.'
        dialog._on_summary_success(text, 'lv')
        self.assertTrue(manager.get_session(sid).summary_text_lv)
        self.assertTrue(manager.get_summary_metadata(sid, 'lv')['created_at'])
        dialog.lang_combo.setCurrentIndex(0)
        dialog.close_btn.click()
        self.assertFalse(dialog.isVisible())
        reopened = AISummaryDialog(SermonSummarizer(self.config), manager.get_session(sid), [], manager)
        self.assertEqual(reopened.lang_combo.currentData(), 'en')
        reopened.lang_combo.setCurrentIndex(1)
        self.assertTrue(reopened.get_current_summary_text())
        reopened.close()

    def test_close_during_generation_dismisses_and_late_result_is_discarded(self):
        dialog, manager, sid = self.dialog()
        entered, release = threading.Event(), threading.Event()
        def generate(*args, **kwargs):
            entered.set()
            release.wait(3)
            return 'Main Message:\nLate result must not appear.'
        dialog.summarizer.generate_summary = generate
        dialog.show()
        dialog._generate_summary()
        self.assertTrue(entered.wait(1))
        dialog.close_btn.click()
        self.assertFalse(dialog.isVisible())
        manager.end_session()
        new_sid = manager.start_session()
        dialog._on_summary_success('stale', 'lv')
        self.assertFalse(manager.get_session(new_sid).summary_text_lv)
        self.assertFalse(manager.get_session(sid).summary_text_lv)
        release.set()
        dialog._worker_thread.wait(2000)
        self.app.processEvents()
        self.assertNotIn(dialog, AISummaryDialog._background_dialogs)

    def test_cancelled_worker_error_still_restores_generation_controls(self):
        dialog, _, _ = self.dialog()
        dialog._is_generating = True
        dialog._discard_result = True
        dialog._on_gemini_error('request failed after cancellation', False)
        dialog._on_worker_finished()
        self.assertFalse(dialog._is_generating)
        self.assertTrue(dialog.generate_btn.isEnabled())
        self.assertTrue(dialog.lang_combo.isEnabled())

    def test_shared_player_preserves_language_order_and_independent_volume(self):
        wav = io.BytesIO()
        sf.write(wav, np.full(1600, .25, dtype='float32'), 16000, format='WAV')
        logs, errors, samples = [], [], []
        player = OrderedAudioPlayer('en', 3, lambda: 1, errors.append, self.root / 'audio.sqlite', logs.append)
        player.volume_getters = {'en': lambda: .8, 'lv': lambda: .4}
        with patch('sounddevice.OutputStream') as stream:
            stream.return_value.__enter__.return_value.write.side_effect = lambda data: samples.append(data.copy()) or False
            player.start()
            player.enqueue(wav.getvalue(), 'en')
            player.enqueue(wav.getvalue(), 'lv')
            player.stop()
        self.assertEqual(errors, [])
        self.assertEqual([x for x in logs if '[PLAYBACK]' in x], ['[PLAYBACK] English started', '[PLAYBACK] LV started'])
        self.assertAlmostEqual(float(samples[0].mean()), .2, places=4)
        self.assertAlmostEqual(float(samples[1].mean()), .1, places=4)

    def test_multilingual_voice_and_unicode_payload_stay_associated(self):
        settings = {'en': {'voice': 'en-US-Standard-B', 'rate': .9, 'pitch': 2, 'gain': -2},
                    'lv': {'voice': 'lv-LV-Standard-A', 'rate': 1.1, 'pitch': -1, 'gain': 0}}
        tts = TextToSpeech(replace(self.config, google_application_credentials='fixture', voice_settings=settings))
        tts._client = MagicMock()
        tts._client.synthesize_speech.return_value.audio_content = b'audio'
        for language, text, voice in [('en', 'Hope brings peace.', 'en-US-Standard-B'),
                                      ('lv', 'Žēlastība dod cerību.', 'lv-LV-Standard-A')]:
            self.assertEqual(tts.synthesize(text, language, strict=True), b'audio')
            call = tts._client.synthesize_speech.call_args.kwargs
            self.assertEqual(call['input'].text, text)
            self.assertEqual(call['voice'].name, voice)
            self.assertAlmostEqual(call['audio_config'].speaking_rate, settings[language]['rate'])

    def test_free_speech_requests_preserve_all_unicode_text_at_bounded_size(self):
        text = ('Žēlastība un cerība — мир и надежда. ' * 40) + 'ending'
        parts = list(TextToSpeech._web_speech_parts(text))
        self.assertEqual(''.join(parts), text)
        self.assertTrue(all(0 < len(part) <= 180 for part in parts))

    def test_small_window_geometry_session_reset_and_old_signal_rejection(self):
        device = AudioDevice(0, 'Fixture mic', 1, 1)
        with patch('church_translator.main.list_audio_devices', return_value=[device]), patch.object(MainWindow, '_check_crash_recovery'):
            window = MainWindow()
            self.addCleanup(window.close)
            window.show()
            window.resize(720, 540)
            self.app.processEvents()
            self.assertEqual(window.width(), 720)
            self.assertEqual(window.height(), 540)
            self.assertGreater(window.main_scroll.horizontalScrollBar().maximum(), 0)
            window.context_manager.set_text('Old sermon context')
            window.append_transcript('Old speech')
            with patch.object(window, '_active_config', return_value=self.config), patch('church_translator.main.TranslationEngine') as engine:
                engine.return_value._translator.config = self.config
                window.start()
                self.assertTrue(engine.return_value.start.called)
                self.assertFalse(window.context_manager.is_active)
                self.assertFalse(window.latvian_text.toPlainText())
                sid = window.session_manager.current_session_id
                window._receive_session_update('old-session', 'transcript', 'stale')
                self.assertFalse(window.latvian_text.toPlainText())
                window._receive_session_update(sid, 'transcript', 'fresh')
                self.assertEqual(window.latvian_text.toPlainText(), 'fresh')
                window.engine = None
                window.session_manager.end_session()
            window.close()
            reopened = MainWindow()
            self.assertEqual(reopened.size(), window.size())
            reopened.close()
