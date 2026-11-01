"""Production guards: real hypotheses are not accuracy references."""
from dataclasses import replace
import io
import json
import os
from pathlib import Path
import runpy
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

import numpy as np
import soundfile as sf
from PySide6.QtWidgets import QApplication
from church_translator.audio import OrderedAudioPlayer
from church_translator.backlog import DurableQueue
from church_translator.config import load_config
from church_translator.engine import TranslationEngine, TranslationItem, EngineSettings
from church_translator.glossary import Glossary
from church_translator.main import MainWindow
from church_translator.recognition_quality import short_audio_warning, supported_token_spans
from church_translator.services import OpenAITranscriber, Translator
from church_translator.speech_audit import SpeechAudit
from church_translator.speech_gate import audio_evidence
from church_translator.translation_quality import InvalidTranslationOutput, translation_issue, translation_warnings


class ProductionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        env = patch.dict(os.environ, {'CHURCH_TRANSLATOR_APPDATA': temp.name})
        env.start()
        self.addCleanup(env.stop)
        self.config = replace(load_config(), gemini_api_key='fixture', openai_api_key='fixture',
                              openai_fallback_enabled=False, translation_provider='gemini')

    def test_tiny_tail_does_not_reuse_previous_text_or_speak_a_dense_hypothesis(self):
        service = OpenAITranscriber(self.config, Glossary(), lambda _: None)
        service._client = MagicMock()
        service._client.audio.transcriptions.create.return_value = SimpleNamespace(text='Mīlestība', logprobs=[])
        audio = np.resize(np.array([.02, -.02], 'float32'), 1600)
        result = service.transcribe(audio, previous_context='Mīlestība šeit sasien visu kopā.')
        self.assertEqual(result.raw_text, 'Mīlestība')
        self.assertFalse(result.source_visible)
        self.assertFalse(result.translation_allowed)
        self.assertTrue(result.coverage_review)
        self.assertNotIn('prompt', service._client.audio.transcriptions.create.call_args.kwargs)
        service._client.audio.transcriptions.create.assert_called_once()
        self.assertEqual(service.last_short_audio['original_seconds'], .1)

    def test_real_short_words_and_genuine_repetitions_are_allowed(self):
        for text in ('Jā.', 'Nē.', 'Āmen.', 'Jā, jā.'):
            self.assertFalse(short_audio_warning(3200, text)['possible_short_audio_hallucination'], text)
        self.assertFalse(short_audio_warning(16000, 'Mīlestība')['possible_short_audio_hallucination'])

    def test_token_spans_need_exact_unicode_alignment(self):
        tokens = [{'token': 'Mēs ', 'logprob': -.1}, {'token': 'nevaram', 'logprob': -2}]
        self.assertEqual(supported_token_spans('Mēs nevaram', tokens)[0]['start'], 4)
        self.assertFalse(supported_token_spans('Mēs varam', tokens))

    def test_common_function_words_do_not_prove_a_prompt_echo(self):
        prompt = 'Context\n' + json.dumps({'previous_speech_not_current_audio':
                    'Tad mēs esam dārzā, bet mani draugi ir tepat. Tāpēc es to daru pats.'})
        self.assertFalse(OpenAITranscriber._sparse_reference_echo(
            'Varbūt tāpēc, ka mani, viņi, mēs, esam, mūsu ļaudis ir atnākuši.', prompt, 10))

    def test_short_probable_speech_surrounded_by_pause_is_not_a_sparse_prompt_echo(self):
        service = OpenAITranscriber(self.config, Glossary(), lambda _: None)
        service._client = MagicMock()
        service._client.audio.transcriptions.create.return_value = SimpleNamespace(text='Jēzus Kristus.', logprobs=[])
        service.speech_evidence = {'vad_available': True, 'probable_speech_seconds': 1}
        audio = np.concatenate((np.resize(np.array([.02, -.02], 'float32'), 16000), np.zeros(112000, 'float32')))
        result = service.transcribe(audio, previous_context='Mēs runājam par Jēzu Kristu.')
        self.assertEqual(result.text, 'Jēzus Kristus.')
        service._client.audio.transcriptions.create.assert_called_once()

    def test_loud_ambiguous_audio_is_preserved_without_previous_text_priming(self):
        samples = np.resize(np.array([.04, -.04], 'float32'), 16000)
        with patch('faster_whisper.vad.get_vad_model', return_value=lambda x: np.zeros(len(x) // 512)):
            evidence = audio_evidence(samples)
        self.assertFalse(evidence['skip_stt'])
        self.assertFalse(evidence['use_context'])
        self.assertFalse(evidence['allow_gain'])

    def test_ordered_recovery_keeps_every_original_word_and_rejects_negation_edits(self):
        source = 'Mēs šodien sākam kopīgu darbu.'
        extension = 'Mēs šodien, kā tu jau teici, sākam kopīgu darbu. Vēlāk paņemsim pārtraukumu.'
        self.assertEqual(OpenAITranscriber._supported_source_extension(source, extension,
            {'probable_speech_seconds': 8}), 'ordered_source_extension')
        self.assertIsNone(OpenAITranscriber._supported_source_extension(source,
            'Mēs šodien nesākam kopīgu darbu. Vēlāk paņemsim pārtraukumu.', {'probable_speech_seconds': 8}))
        self.assertIsNone(OpenAITranscriber._supported_source_extension(source,
            'Mēs šodien nekad sākam kopīgu darbu. Vēlāk paņemsim pārtraukumu.', {'probable_speech_seconds': 8}))

    def test_question_tag_is_not_a_negation_loss(self):
        self.assertFalse(translation_warnings('Mums katram ir trūkumi, ne?', 'We each have flaws, right?', 'en'))
        self.assertIn('possible_negation_change_unverified', translation_warnings(
            'Viņš nevar to darīt.', 'He can do it.', 'en'))
        self.assertIn('possible_negation_change_unverified', translation_warnings(
            'Viņš nepiedeva.', 'He forgave.', 'en'))
        self.assertFalse(translation_warnings('Viņš nepiedeva.', 'He did not forgive.', 'en'))
        self.assertEqual(translation_issue('Tu viņš nedara to pašu kļūdu.',
                          'You he does not make the same mistake.', 'en'), 'conflicting_english_subjects')
        self.assertIsNone(translation_issue('Jā, jā.', 'Yes, yes.', 'en'))
        self.assertIsNone(translation_issue('Tu, viņš...', 'You—he...', 'en'))

    def translator(self):
        with patch.object(Translator, '_init_gemini_client'):
            service = Translator(self.config, Glossary())
        service._get_model_candidates = lambda: ['fixture-model']
        service._wait_for_pacing = MagicMock()
        return service

    def test_bad_translation_repairs_once_and_preserves_both_raw_responses(self):
        service = self.translator()
        service._call_gemini_api = MagicMock(side_effect=[
            '{"en":"You he does not repeat that mistake."}',
            '{"en":"[unclear] does not repeat that mistake."}'])
        result = service.translate_joint('Tu viņš neatkārto to kļūdu.', ['en'])
        self.assertEqual(result['en'], '[unclear] does not repeat that mistake.')
        self.assertEqual([x['status'] for x in service.last_translation_attempts], ['rejected', 'accepted'])
        self.assertEqual(service._call_gemini_api.call_count, 2)

    def test_exhausted_quality_repair_is_not_an_outage_or_paid_fallback(self):
        service = self.translator()
        service._call_gemini_api = MagicMock(return_value='{"en":"You he does not repeat that mistake."}')
        service._translate_with_openai_fallback = MagicMock()
        with self.assertRaises(InvalidTranslationOutput):
            service.translate_joint('Tu viņš neatkārto to kļūdu.', ['en'])
        self.assertEqual(service._call_gemini_api.call_count, 2)
        service._translate_with_openai_fallback.assert_not_called()
        self.assertEqual(len(service.last_translation_attempts), 2)

    def test_rejected_translation_is_durable_and_later_speech_continues(self):
        with patch.object(Translator, '_init_gemini_client'):
            engine = TranslationEngine(self.config, EngineSettings(0, True, False, 0, None, lambda: 1, lambda: 1),
                lambda *_: None, lambda *_: None, lambda *_: None, lambda *_: None, lambda *_: None)
        for work in (engine._chunks, engine._translations, engine._speech, engine._unrecognized):
            self.addCleanup(work.close)
        engine._translator = MagicMock()
        engine._translator.translate_joint.side_effect = [InvalidTranslationOutput('conflicting_subjects'), {'en': 'We help.'}]
        engine._players = {'en': MagicMock()}
        engine._translations.put(TranslationItem(1, 'Tu viņš...', sequence_id=0, source_segment_ids=[0]))
        engine._translations.put(TranslationItem(2, 'Mēs palīdzam.', manual=True, sequence_id=1, source_segment_ids=[1]))
        engine._translations.finish()
        engine._translation_loop()
        self.assertEqual(engine._translation_review_count, 1)
        self.assertEqual(engine._speech.qsize(), 1)
        review = DurableQueue(engine.session_dir / 'translation-review.sqlite', TranslationItem)
        self.addCleanup(review.close)
        self.assertEqual(review.get().transcript, 'Tu viņš...')

    def test_playback_start_requires_successful_write_and_shared_load_counts_both_languages(self):
        wav = io.BytesIO()
        sf.write(wav, np.zeros(1600), 16000, format='WAV')
        reports, errors = [], []
        player = OrderedAudioPlayer('en', 0, lambda: 1, errors.append, self.root / 'playback.sqlite', on_started=reports.append)
        player.enqueue(wav.getvalue(), 'en', captured_at=1, sequence_id=0, source_duration=.1)
        player.enqueue(wav.getvalue(), 'yo', captured_at=1, sequence_id=0, source_duration=.1)
        player._queue.finish()
        self.assertAlmostEqual(player._source_seconds, .1)
        self.assertAlmostEqual(player._generated_audio_seconds, .2)
        with patch('church_translator.audio.sd.OutputStream') as device:
            device.return_value.__enter__.return_value.write.side_effect = OSError('disconnected')
            player._run()
        self.assertFalse(reports)
        self.assertEqual(player._queue.qsize(), 2)
        player._queue.close()

    def test_successful_write_reports_output_start_before_acknowledgement(self):
        wav = io.BytesIO()
        sf.write(wav, np.zeros(1600), 16000, format='WAV')
        reports, trace = [], []
        player = OrderedAudioPlayer('en', 0, lambda: 1, self.fail, self.root / 'timing.sqlite',
                                    on_started=lambda details: (trace.append('started'), reports.append(details)))
        with patch('church_translator.audio.time.monotonic', return_value=10):
            player.enqueue(wav.getvalue(), 'en', captured_at=1, sequence_id=0)
        player._queue.finish()
        with patch('church_translator.audio.time.monotonic', return_value=20), patch(
                'church_translator.audio.sd.OutputStream') as device:
            stream = device.return_value.__enter__.return_value
            stream.latency = .02
            stream.write.side_effect = lambda _: trace.append('write') or False
            stream.stop.side_effect = lambda: trace.append('drain')
            player._run()
        self.assertEqual(trace, ['write', 'started', 'drain'])
        self.assertAlmostEqual(reports[0]['capture_to_output_start'], 19.02)
        self.assertEqual(reports[0]['queue_wait'], 10)
        self.assertFalse(reports[0]['physical_audio_verified'])
        self.assertEqual(player._queue.qsize(), 0)
        player._queue.close()

    def test_synthesis_readiness_does_not_update_the_playback_latency_indicator(self):
        measured = MagicMock()
        with patch.object(Translator, '_init_gemini_client'):
            engine = TranslationEngine(self.config, EngineSettings(0, True, False, 0, None, lambda: 1, lambda: 1),
                lambda *_: None, self.fail, measured, lambda *_: None, lambda *_: None)
        for work in (engine._chunks, engine._translations, engine._speech, engine._unrecognized):
            self.addCleanup(work.close)
        engine._tts = MagicMock()
        engine._tts.synthesize.return_value = b'fixture audio'
        engine._players = {'en': MagicMock()}
        engine._speech.put({'sequence_id': 0, 'captured_at': time.monotonic(), 'translations': {'en': 'We help.'}, 'completed': []})
        engine._speech.finish()
        engine._speech_loop()
        measured.assert_not_called()
        engine._on_playback_started({'sequence_id': 0, 'language': 'en', 'capture_to_output_start': 12.5})
        engine._on_playback_started({'sequence_id': 0, 'language': 'yo', 'capture_to_output_start': 20.0})
        measured.assert_called_once_with(12.5)

    def test_backlog_export_skips_audit_database_and_keeps_encoded_playback(self):
        session = self.root / 'session'
        SpeechAudit(session / 'speech-audit.sqlite')
        wav = io.BytesIO()
        sf.write(wav, np.zeros(1600), 16000, format='WAV')
        backlog = DurableQueue(session / 'playback-en.sqlite')
        backlog.put({'audio': wav.getvalue(), 'language': 'en'})
        backlog.close()
        target = self.root / 'export'
        with patch('sys.argv', ['export_backlog.py', str(session), str(target)]):
            runpy.run_path('scripts/export_backlog.py', run_name='__main__')
        self.assertEqual(next(target.glob('*.wav')).read_bytes(), wav.getvalue())


class ProductionDisplayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_font_choices_fullscreen_and_restart_preserve_source_and_four_tabs(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {'CHURCH_TRANSLATOR_APPDATA': folder}), patch(
                'church_translator.main.list_audio_devices', return_value=[]):
            window = MainWindow()
            try:
                window.latvian_text.setPlainText('Jā. Jā. Mēs nevaram to darīt.')
                window.transcript_font_combo.setCurrentIndex(2)
                self.assertIn('19px', window.latvian_text.styleSheet())
                window._toggle_fullscreen()
                self.app.processEvents()
                self.assertTrue(window.isFullScreen())
                self.assertIn('24px', window.latvian_text.styleSheet())
                self.assertEqual(window.tabs.count(), 4)
                window._toggle_fullscreen()
                self.assertEqual(window.latvian_text.toPlainText(), 'Jā. Jā. Mēs nevaram to darīt.')
            finally:
                window.close()
            restarted = MainWindow()
            try:
                self.assertEqual(restarted.transcript_font_combo.currentData(), 19)
                self.assertIn('19px', restarted.english_text.styleSheet())
            finally:
                restarted.close()


if __name__ == '__main__':
    unittest.main()
