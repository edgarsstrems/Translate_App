"""Focused behavioral regressions for the October summary/Dynamic/voice upgrade.

All services here are mocked; the opt-in live diagnostic reports separately.
"""
import json
import os
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import numpy as np
from PySide6.QtWidgets import QApplication, QPushButton
from church_translator.audio import ChunkRecorder, SAMPLE_RATE
from church_translator.config import load_config, get_speed_profile_settings, load_user_settings, save_user_settings
from church_translator.engine import TranslationEngine, ProcessingItem, EngineSettings
from church_translator.session_manager import SessionManager, TranscriptSegment
from church_translator.services import Translator, TextToSpeech
from church_translator.summarizer import StructuredSummary, SermonSummarizer, GeminiSummaryError
from church_translator.glossary import Glossary
from church_translator.reliability import GeminiRetryLater


class UpgradeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = patch.dict(os.environ, {'CHURCH_TRANSLATOR_APPDATA': str(self.root)})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.config = replace(load_config(), gemini_api_key=None, openai_api_key=None, openai_fallback_enabled=False)

    def recorder(self, chunks):
        return ChunkRecorder(0, 8, 0, 1.5, .6, .004, .025,
            lambda seq, audio, at, overlap: chunks.append((seq, audio, overlap)),
            lambda error: self.fail(error), dynamic_mode=True)

    def feed(self, recorder, seconds, level=.05):
        # Alternating values simulate audible energy without DC-only input.
        samples = (np.resize(np.array([level, -level], dtype='float32'), int(seconds * SAMPLE_RATE)))
        for offset in range(0, len(samples), 1600):
            recorder._consume(samples[offset:offset + 1600])
        return samples

    def test_dynamic_sentence_pauses_at_three_and_five_seconds(self):
        for duration in (3, 5):
            with self.subTest(duration=duration):
                chunks = []
                recorder = self.recorder(chunks)
                self.feed(recorder, duration)
                self.assertEqual(chunks, [])
                self.feed(recorder, .8, 0)
                self.assertEqual(len(chunks), 1)
                self.assertLessEqual(len(chunks[0][1]) / SAMPLE_RATE, duration + .8)
                self.assertEqual(recorder.last_boundary, 'pause')

    def test_dynamic_ignores_short_midphrase_pause(self):
        chunks = []
        recorder = self.recorder(chunks)
        self.feed(recorder, 3)
        self.feed(recorder, .26, 0)
        self.feed(recorder, 1)
        self.assertFalse(chunks)
        self.feed(recorder, .8, 0)
        self.assertEqual(len(chunks), 1)

    def test_dynamic_eight_second_cap_and_exact_continuity(self):
        chunks = []
        recorder = self.recorder(chunks)
        original = self.feed(recorder, 26.13)
        recorder.stop()
        self.assertEqual([n for n, _, _ in chunks], list(range(len(chunks))))
        self.assertTrue(all(len(a) <= 8 * SAMPLE_RATE and overlap == 0 for _, a, overlap in chunks))
        np.testing.assert_array_equal(np.concatenate([a for _, a, _ in chunks]), original)

    def test_dynamic_quiet_speech_does_not_flush_every_syllable(self):
        chunks = []
        recorder = self.recorder(chunks)
        self.feed(recorder, 5, .001)
        self.assertFalse(chunks)
        self.feed(recorder, .8, 0)
        self.assertEqual(len(chunks), 1)

    def test_dynamic_long_digital_silence_submits_nothing(self):
        chunks = []
        recorder = self.recorder(chunks)
        self.feed(recorder, 15, 0)
        recorder.stop()
        self.assertFalse(chunks)

    def test_dynamic_enforces_ceiling_even_with_invalid_caller_parameters(self):
        recorder = self.recorder([])
        recorder.update_parameters(20, 2, .6, dynamic_mode=True)
        self.assertEqual(recorder.chunk_seconds, 8)

    def test_capture_shutdown_after_consumer_failure_cannot_deadlock(self):
        recorder = self.recorder([])
        consumer = threading.Thread(target=lambda: None)
        consumer.start()
        consumer.join()
        recorder._capture_thread = consumer
        for _ in range(recorder._raw.maxsize):
            recorder._raw.put(np.zeros(1, dtype='float32'))
        shutdown = threading.Thread(target=recorder.stop, daemon=True)
        shutdown.start()
        shutdown.join(1)
        self.assertFalse(shutdown.is_alive())

    def test_dynamic_and_voice_preferences_roundtrip(self):
        settings = {'speed': {'mode': 'dynamic'}, 'voices': {'lt': {'voice': 'lt-LT-Standard-B', 'rate': .95, 'pitch': 1, 'gain': -2}}}
        save_user_settings(settings)
        restored = load_user_settings()
        self.assertEqual(restored['speed']['mode'], 'dynamic')
        self.assertEqual(restored['voices']['lt'], settings['voices']['lt'])
        self.assertEqual(load_config().voice_settings['lt']['pitch'], 1)
        self.assertEqual(get_speed_profile_settings('dynamic')['chunk_seconds'], 8)

    def test_session_append_without_active_session_has_no_deadlock(self):
        manager = SessionManager(self.root / 'sessions')
        worker = threading.Thread(target=lambda: manager.append_segment(TranscriptSegment(0, 1, '10:00', 'Ticība.', '')), daemon=True)
        worker.start()
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(manager.get_segments(manager.current_session_id)), 1)

    def test_segment_retry_order_and_session_ownership(self):
        manager = SessionManager(self.root / 'sessions')
        first = manager.start_session()
        for number in (2, 1, 2):
            manager.append_segment(TranscriptSegment(number, number, '10:00', f'Source {number}', ''), session_id=first)
        second = manager.start_session()
        manager.append_segment(TranscriptSegment(3, 3, '10:01', 'Late source', ''), session_id=first)
        self.assertEqual([s.segment_id for s in manager.get_segments(first)], [1, 2, 3])
        self.assertEqual(manager.get_segments(second), [])

    def test_recognized_transcript_saved_before_failed_translation(self):
        manager = SessionManager(self.root / 'sessions')
        sid = manager.start_session()
        errors = []
        settings = EngineSettings(0, True, False, None, None, lambda: 1, lambda: 1)
        engine = TranslationEngine(self.config, settings, lambda _: None, errors.append,
            lambda _: None, lambda _: None, lambda *_: None, session_manager=manager)
        engine._chunks.put(ProcessingItem(0, time.monotonic(), manual_text='Dievs dod cerību.'))
        engine._chunks.finish()
        engine._transcriber = MagicMock()
        with patch('church_translator.engine.ChunkRecorder'):
            engine._process_loop()
        saved = manager.get_segments(sid)
        self.assertEqual(saved[0].latvian_text, 'Dievs dod cerību.')
        self.assertEqual(saved[0].status, 'recovering')
        self.assertGreater(saved[0].captured_at, 1_700_000_000)
        engine._players = {'en': MagicMock()}
        engine._translator.translate_joint = MagicMock(side_effect=RuntimeError('network unavailable'))
        engine._translation_loop()
        self.assertEqual(len(manager.get_segments(sid)), 1)
        self.assertEqual(engine._translations.qsize(), 1)
        engine.stop()

    def test_pause_resume_capture_sequence_does_not_reset(self):
        settings = EngineSettings(0, True, False, None, None, lambda: 1, lambda: 1)
        engine = TranslationEngine(self.config, settings, lambda _: None, lambda _: None,
            lambda _: None, lambda _: None, lambda *_: None)
        for recorder_id in (0, 1, 0, 1):
            engine._on_chunk(recorder_id, np.ones(1600, dtype='float32'), time.monotonic(), 0)
        for expected in range(4):
            item = engine._chunks.get()
            self.assertEqual(item.chunk_index, expected)
            engine._chunks.ack()
        engine.stop()

    def test_failed_synthesis_retains_pending_translation(self):
        settings = EngineSettings(0, True, False, None, None, lambda: 1, lambda: 1)
        engine = TranslationEngine(self.config, settings, lambda _: None, lambda _: None,
            lambda _: None, lambda _: None, lambda *_: None)
        engine._speech.put({'sequence_id': 0, 'captured_at': time.monotonic(), 'translations': {'en': 'Hope.'}, 'completed': []})
        engine._speech.finish()
        engine._tts = MagicMock()
        engine._tts.synthesize.return_value = b''
        engine._speech_loop()
        self.assertEqual(engine._speech.qsize(), 1)
        engine.stop()

    def test_paid_fallback_off_retains_work_during_cooldown(self):
        config = replace(self.config, gemini_api_key='test', openai_api_key='test', translation_provider='gemini')
        translator = Translator(config, Glossary())
        translator.circuit_breaker.report_gemini_failure('quota', 600)
        translator._translate_with_openai_fallback = MagicMock()
        translator._free_google_translate_fallback = MagicMock()
        with self.assertRaises(GeminiRetryLater):
            translator.translate_joint('Ticība.', ['en', 'lt'])
        translator._translate_with_openai_fallback.assert_not_called()
        translator._free_google_translate_fallback.assert_not_called()

    def test_paid_call_guard_cannot_be_bypassed(self):
        translator = Translator(self.config, Glossary())
        with self.assertRaisesRegex(RuntimeError, 'disabled'):
            translator._translate_with_openai_fallback('Ticība.', ['en'])

    def test_legacy_default_on_is_not_treated_as_paid_consent(self):
        from church_translator.main import MainWindow
        save_user_settings({'recognition': {'openai_fallback_enabled': True}})
        with patch('church_translator.main.load_config', return_value=self.config), patch.object(MainWindow, '_check_crash_recovery'):
            window = MainWindow()
            self.assertFalse(window.openai_fallback_toggle.isChecked())
            window.openai_fallback_toggle.click()
            self.assertTrue(load_user_settings()['recognition']['paid_translation_consent_v1'])
            window.close()
            reopened = MainWindow()
            self.assertTrue(reopened.openai_fallback_toggle.isChecked())
            reopened.close()

    def summary(self):
        return StructuredSummary('Hope', 'Christ gives hope.', ['Trust Christ.'], [], 'Live in hope.',
            examples=['The speaker described a journey.'], key_takeaways=['Hope sustains faith.'], closing_reflection='Consider this hope.')

    def test_all_eight_sections_roundtrip(self):
        original = self.summary()
        restored = StructuredSummary.from_text(original.to_clean_text())
        for name in ('title', 'main_message', 'key_teachings', 'bible_references', 'practical_takeaway', 'examples', 'key_takeaways', 'closing_reflection'):
            self.assertEqual(getattr(restored, name), getattr(original, name), name)

    def test_json_schema_rejects_wrong_types_and_missing_message(self):
        for value in ('[]', '{"title":"Only a title"}', '{"main_message":"Hope", "key_teachings":[{}]}'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                StructuredSummary.from_json(value)

    def test_summary_repairs_once_then_fails_honestly(self):
        summary = SermonSummarizer(self.config)
        segments = [TranscriptSegment(0, 1, '10:00', 'Cerība.', '')]
        with patch.object(summary, '_call_ai', return_value='{"title":') as call:
            with self.assertRaises(GeminiSummaryError):
                summary.generate_summary(segments)
            self.assertEqual(call.call_count, 2)

    def test_summary_repairs_malformed_json(self):
        summary = SermonSummarizer(self.config)
        with patch.object(summary, '_call_ai', side_effect=['{', '{"title":"Hope", "main_message":"Christ gives hope."}']) as call:
            result = summary.generate_summary([TranscriptSegment(0, 1, '', 'Cerība.', '')])
            self.assertIn('Christ gives hope', result)
            self.assertEqual(call.call_count, 2)

    def test_summary_reports_project_denial_without_paid_call(self):
        summary = SermonSummarizer(replace(self.config, gemini_api_key='test', openai_api_key='test'))
        with patch.object(summary, '_call_gemini_single_model', side_effect=RuntimeError('403 Your project has been denied access. Please contact support.')), patch.object(summary, '_call_openai') as paid:
            with self.assertRaisesRegex(GeminiSummaryError, 'denied Gemini access to the project'):
                summary.generate_summary([TranscriptSegment(0, 1, '', 'Cerība.', '')])
            paid.assert_not_called()

    def test_empty_source_does_not_call_any_provider(self):
        summary = SermonSummarizer(self.config)
        with patch.object(summary, '_call_ai') as call:
            with self.assertRaises(ValueError):
                summary.generate_summary([TranscriptSegment(0, 1, '', '  ', '')])
            call.assert_not_called()

    def test_long_summary_covers_first_and_last_sections(self):
        summary = SermonSummarizer(self.config)
        summary.max_single_pass_words = 100
        transcript = 'FIRST ' + 'faith ' * 2000 + 'LAST'
        prompts = []
        def respond(prompt, **kwargs):
            prompts.append(prompt)
            return 'Main Message:\nFaith brings hope.'
        with patch.object(summary, '_call_ai', side_effect=respond):
            summary.generate_summary([TranscriptSegment(0, 1, '', transcript, '')])
        self.assertIn('FIRST', prompts[0])
        self.assertIn('LAST', prompts[-2])

    def test_summary_edit_save_language_switch_and_restart(self):
        from church_translator.dialogs import AISummaryDialog
        manager = SessionManager(self.root / 'sessions')
        sid = manager.start_session()
        manager.end_session()
        manager.save_session_summary(sid, self.summary().to_clean_text())
        save_user_settings({'summary': {'language': 'en'}})
        dialog = AISummaryDialog(SermonSummarizer(self.config), manager.get_session(sid), [], manager)
        dialog._highlight_edits[0].setText('Edited illustration.')
        dialog.lang_combo.setCurrentIndex(1)
        self.assertFalse(dialog.summary_text.toPlainText())
        dialog.close()
        reopened = AISummaryDialog(SermonSummarizer(self.config), manager.get_session(sid), [], manager)
        self.assertEqual(reopened.lang_combo.currentData(), 'lv')
        reopened.lang_combo.setCurrentIndex(0)
        self.assertEqual(reopened._highlight_edits[0].text(), 'Edited illustration.')
        reopened.close()

    def test_summary_narrow_window_buttons_fit(self):
        from church_translator.dialogs import AISummaryDialog
        manager = SessionManager(self.root / 'sessions')
        sid = manager.start_session()
        dialog = AISummaryDialog(SermonSummarizer(self.config), manager.get_session(sid), [], manager)
        dialog.resize(640, 560)
        dialog.show()
        self.app.processEvents()
        for button in dialog.findChildren(QPushButton):
            if button.isVisible():
                self.assertGreaterEqual(button.width(), button.minimumSizeHint().width(), button.text())
        dialog.close()

    def test_voice_catalog_filters_language_and_model_only_names(self):
        tts = TextToSpeech(self.config)
        tts._client = MagicMock()
        tts._client.list_voices.return_value.voices = [
            SimpleNamespace(name='lt-LT-Standard-B', language_codes=['lt-LT'], ssml_gender=1),
            SimpleNamespace(name='en-US-Standard-J', language_codes=['en-US'], ssml_gender=1),
            SimpleNamespace(name='Aoede', language_codes=['lt-LT'], ssml_gender=1)]
        self.assertEqual([v['name'] for v in tts.list_voices('lt')], ['lt-LT-Standard-B'])

    def test_tts_applies_per_language_controls(self):
        tts = TextToSpeech(replace(self.config, google_application_credentials='test.json',
            voice_settings={'lt': {'voice':'lt-LT-Standard-B', 'rate':.9, 'pitch':2, 'gain':-3}}))
        tts._client = MagicMock()
        tts._client.synthesize_speech.return_value.audio_content = b'wave'
        self.assertEqual(tts.synthesize('Viltis.', 'lt', strict=True), b'wave')
        args = tts._client.synthesize_speech.call_args.kwargs
        self.assertEqual(args['voice'].name, 'lt-LT-Standard-B')
        self.assertAlmostEqual(args['audio_config'].speaking_rate, .9)
        self.assertEqual(args['audio_config'].pitch, 2)
        self.assertEqual(args['audio_config'].volume_gain_db, -3)

    def test_chirp_and_wavenet_capabilities(self):
        self.assertFalse(TextToSpeech.voice_capabilities('lt-LT-Chirp3-HD-Aoede')['pitch'])
        self.assertTrue(TextToSpeech.voice_capabilities('lt-LT-Chirp3-HD-Aoede')['rate'])
        self.assertTrue(TextToSpeech.voice_capabilities('en-US-Wavenet-J')['pitch'])

    def test_strict_voice_preview_does_not_misreport_web_fallback(self):
        # This test exercises the missing-credential branch, independent of
        # any developer-local Google credential path loaded from .env.
        tts = TextToSpeech(replace(self.config, google_application_credentials=None))
        tts._free_fallback = MagicMock(return_value=b'audio')
        with self.assertRaises(RuntimeError):
            tts.synthesize('Hope.', 'en', strict=True)
        tts._free_fallback.assert_not_called()

    def test_voice_preview_worker_uses_selected_routing(self):
        import io
        import soundfile as sf
        from PySide6.QtWidgets import QMessageBox
        from church_translator.voice_dialog import VoiceSettingsDialog
        wav = io.BytesIO()
        sf.write(wav, np.zeros(1600, dtype='float32'), 16000, format='WAV')
        dialog = VoiceSettingsDialog(self.config, {}, lambda _: 7, lambda _: .4)
        dialog._catalog['en'] = [{'name': self.config.english_voice}]
        with patch.object(QMessageBox, 'question', return_value=QMessageBox.Yes), patch.object(TextToSpeech, 'synthesize', return_value=wav.getvalue()) as synth, patch('sounddevice.OutputStream') as stream:
            dialog._preview()
            dialog._task.wait(2000)
            self.app.processEvents()
            self.assertIn('Preview complete', dialog.note.text())
            self.assertEqual(stream.call_args.kwargs['device'], 7)
            self.assertTrue(synth.call_args.kwargs['strict'])
        dialog.close()

    def test_voice_preview_does_not_overlap_active_sermon(self):
        from PySide6.QtWidgets import QWidget
        from church_translator.voice_dialog import VoiceSettingsDialog
        parent = QWidget()
        parent.engine = object()
        dialog = VoiceSettingsDialog(self.config, {}, lambda _: None, lambda _: 1, parent)
        with patch.object(TextToSpeech, 'synthesize') as synth:
            dialog._preview()
            synth.assert_not_called()
        self.assertIn('Stop the active sermon', dialog.note.text())
        dialog.close()
        parent.close()

    def test_unverified_voice_cannot_be_saved_as_available(self):
        from church_translator.voice_dialog import VoiceSettingsDialog
        dialog = VoiceSettingsDialog(self.config, {}, lambda _: None, lambda _: 1)
        dialog.voice.addItem('Unverified voice', 'en-US-Chirp3-HD-Unverified')
        dialog.voice.setCurrentIndex(dialog.voice.count() - 1)
        dialog._save()
        self.assertIn('Refresh the catalog', dialog.note.text())
        self.assertNotEqual(dialog.result(), 1)
        dialog.close()

    def test_transcript_edit_invalidates_summary_and_metadata(self):
        manager = SessionManager(self.root / 'sessions')
        sid = manager.start_session()
        manager.append_segment(TranscriptSegment(0, 1, '10:00', 'Old', ''))
        manager.save_session_summary(sid, 'Old summary')
        manager.save_summary_metadata(sid, 'en', 'gemini', 'gemini-3.5-flash-lite')
        manager.edit_transcript(sid, {0: 'Corrected source'})
        self.assertEqual(manager.get_segments(sid)[0].latvian_text, 'Corrected source')
        self.assertFalse(manager.get_session(sid).summary_text_en)
        self.assertFalse(manager.get_summary_metadata(sid, 'en'))
