"""Synthetic regressions for long documents, context, boundaries and playback."""
import io
import json
import os
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import numpy as np
import soundfile as sf
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import QApplication, QPlainTextEdit

from church_translator.audio import ChunkRecorder, OrderedAudioPlayer, SAMPLE_RATE
from church_translator.config import get_speed_profile_settings, load_config
from church_translator.engine import EngineSettings, ProcessingItem, TranslationEngine, TranslationItem
from church_translator.glossary import Glossary
from church_translator.main import MainWindow
from church_translator.sermon_context import SermonContextManager
from church_translator.services import OpenAITranscriber, Translator, TextToSpeech
from church_translator.translation_quality import translation_issue
from church_translator.ui_compat import append_live_text


class LiveSermonReliabilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        env = patch.dict(os.environ, {'CHURCH_TRANSLATOR_APPDATA': temp.name})
        env.start()
        self.addCleanup(env.stop)
        self.cfg = replace(load_config(), openai_api_key='test', gemini_api_key='test',
                           openai_transcription_model='gpt-4o-transcribe',
                           google_application_credentials=None)

    def test_thousand_live_rows_survive_and_selection_at_eof_stays_fixed(self):
        window = MainWindow()
        self.addCleanup(window.close)
        for i in range(1000):
            window.append_transcript(f'Line {i}: synthetic Latvian source.')
        panel = window.latvian_text
        self.assertEqual(panel.document().blockCount(), 1000)
        self.assertTrue(panel.toPlainText().startswith('Line 0:'))
        cursor = panel.textCursor()
        cursor.movePosition(QTextCursor.End)
        cursor.movePosition(QTextCursor.PreviousWord, QTextCursor.KeepAnchor)
        panel.setTextCursor(cursor)
        selected = cursor.selectedText()
        for _ in range(100):
            window.append_transcript('Incoming sentence.')
        self.assertEqual(panel.textCursor().selectedText(), selected)
        self.assertEqual(panel.document().blockCount(), 1100)
        self.assertEqual(window.log.maximumBlockCount(), 600)

    def test_append_edits_only_tail_and_preserves_manual_scroll_and_focus(self):
        panel = QPlainTextEdit()
        panel.resize(350, 150)
        panel.show()
        self.addCleanup(panel.close)
        append_live_text(panel, '\n'.join(f'Line {i}' for i in range(200)))
        self.app.processEvents()
        bar = panel.verticalScrollBar()
        bar.setValue(30)
        old = bar.value()
        changes = []
        panel.document().contentsChange.connect(lambda pos, removed, added: changes.append((pos, removed, added)))
        focus = self.app.focusWidget()
        for i in range(100):
            append_live_text(panel, f'New {i}')
            self.app.processEvents()
        self.assertEqual(bar.value(), old)
        self.assertIs(self.app.focusWidget(), focus)
        self.assertTrue(all(removed == 0 and pos > 100 for pos, removed, _ in changes))
        bar.setValue(bar.maximum())
        append_live_text(panel, 'Follow again')
        self.assertEqual(bar.value(), bar.maximum())

    def test_status_messages_do_not_change_transcript_card_geometry(self):
        window = MainWindow()
        self.addCleanup(window.close)
        window.resize(1280, 900)
        window.show()
        window.set_status('Synthetic simultaneous activity 100')
        for _ in range(4):
            self.app.processEvents()
        geometry = window.latvian_text.geometry()
        for message in ('Idle', 'Listening: synthetic input', 'Synthetic simultaneous activity 990'):
            window.set_status(message)
            for _ in range(4):
                self.app.processEvents()
            self.assertEqual(window.latvian_text.geometry(), geometry)

    def recorder(self, emitted):
        p = get_speed_profile_settings('dynamic')
        return ChunkRecorder(0, p['chunk_seconds'], 0, p['min_chunk_seconds'], .6, .004, .025,
            lambda seq, audio, *_: emitted.append(audio.copy()), self.fail, dynamic_mode=True,
            dynamic_soft_seconds=p['dynamic_soft_seconds'])

    def test_dynamic_soft_target_waits_for_actual_pause_without_cutting_word(self):
        emitted = []
        recorder = self.recorder(emitted)
        audio = np.resize(np.array([.05, -.05], dtype='float32'), int(6.6 * SAMPLE_RATE))
        recorder._consume(audio)
        self.assertEqual(emitted, [])
        recorder._consume(np.zeros(int(.4 * SAMPLE_RATE), dtype='float32'))
        self.assertEqual(len(emitted), 1)
        self.assertEqual(recorder.last_boundary, 'pause')
        recorder.stop()
        np.testing.assert_array_equal(np.concatenate(emitted)[:len(audio)], audio)

    def test_dynamic_soft_and_hard_limits_can_be_configured_and_clamped(self):
        with patch.dict(os.environ, {'DYNAMIC_SOFT_SECONDS': '4.5', 'DYNAMIC_HARD_SECONDS': '7'}):
            p = get_speed_profile_settings('dynamic')
            self.assertEqual((p['dynamic_soft_seconds'], p['chunk_seconds']), (4.5, 7))
        with patch.dict(os.environ, {'DYNAMIC_SOFT_SECONDS': '99', 'DYNAMIC_HARD_SECONDS': '99'}):
            p = get_speed_profile_settings('dynamic')
            self.assertEqual((p['dynamic_soft_seconds'], p['chunk_seconds']), (8, 8))

    def test_inflected_context_match_is_current_and_omits_unspoken_text(self):
        ctx = SermonContextManager(self.root / 'context.json')
        ctx.set_text('Ķermenis un veselība. Nākotnē ieradīsies astronauts.', save=False)
        source = 'Ķermeni un veselību sargājam.'
        hint = ctx.get_compact_prompt_hint(source)
        self.assertIn('ķermeni', hint)
        self.assertIn('veselību', hint)
        self.assertNotIn('astronaut', hint)
        self.assertTrue(ctx.retrieval_diagnostics(source)['matched'])
        self.assertEqual(ctx.get_compact_prompt_hint('Autobuss aizbrauc deviņos.'), '')
        ctx.clear()
        self.assertEqual(ctx.retrieval_diagnostics(source)['reason'], 'inactive')

    def test_context_diagnostics_report_prepared_omission_even_with_recent_source(self):
        ctx = SermonContextManager(self.root / 'context.json')
        ctx.set_text('Ķermenis un veselība.', save=False)
        logs = []
        with patch.object(Translator, '_init_gemini_client'):
            service = Translator(self.cfg, Glossary(), status_cb=logs.append, sermon_context=ctx)
        service.source_uncertain = True
        payload = json.loads(service._build_translation_prompt('Autobuss aizbrauc.', ['en'],
            'Autobuss kavējas.', True).split('INPUT_DATA_JSON:\n')[1])
        self.assertTrue(payload['source_uncertain'])
        self.assertIn('recent_source', payload['reference_only'])
        self.assertTrue(any('prepared_supplied=false' in x and 'prepared_omitted=no_relevant_match' in x for x in logs))

    def test_low_asr_confidence_is_marked_without_rewriting_words(self):
        service = OpenAITranscriber(self.cfg, Glossary(), lambda _: None)
        service._client = MagicMock()
        service._client.audio.transcriptions.create.return_value = SimpleNamespace(
            text='Klusā frāze paliek.', logprobs=[{'logprob': -2.5}, {'logprob': -.1}])
        result = service.transcribe(np.ones(16000, dtype='float32') * .05)
        self.assertEqual(result.text, 'Klusā frāze paliek.')
        self.assertTrue(result.uncertain)
        kwargs = service._client.audio.transcriptions.create.call_args.kwargs
        self.assertEqual(kwargs['include'], ['logprobs'])
        self.assertNotIn('prompt', kwargs)

    def test_confidence_unavailable_is_not_misreported_as_low(self):
        service = OpenAITranscriber(replace(self.cfg, openai_transcription_model='whisper-1'), Glossary(), lambda _: None)
        service._client = MagicMock()
        service._client.audio.transcriptions.create.return_value = SimpleNamespace(text='Klusā frāze paliek.')
        self.assertFalse(service.transcribe(np.ones(16000, dtype='float32') * .05).uncertain)
        self.assertNotIn('include', service._client.audio.transcriptions.create.call_args.kwargs)

    def test_changed_explicit_reference_is_rejected_but_spelled_out_is_allowed(self):
        self.assertEqual(translation_issue('Lasām Jāņa 4:12.', 'We read John 4:13.', 'en'), 'changed_numeric_reference')
        self.assertIsNone(translation_issue('Lasām Jāņa 4:12–14.', 'We read John 4:12-14.', 'en'))
        self.assertIsNone(translation_issue('Lasām Jāņa 4:12.', 'We read John chapter four, verse twelve.', 'en'))

    def test_playback_reuses_stream_drains_before_ack_and_exposes_duration(self):
        logs, errors, heard = [], [], []
        player = OrderedAudioPlayer('en', 0, lambda: 1, errors.append, self.root / 'player.sqlite', logs.append)
        self.addCleanup(player._queue.close)
        wav = io.BytesIO()
        sf.write(wav, np.ones(1600, dtype='float32') * .1, 16000, format='WAV')
        for i in range(3):
            player.enqueue(wav.getvalue(), 'en', captured_at=1, sequence_id=i)
        with patch('church_translator.audio.sd.OutputStream') as factory:
            stream = factory.return_value.__enter__.return_value
            stream.latency = .02
            stream.write.side_effect = lambda data: heard.append(len(data)) or False
            stream.stop.side_effect = lambda: self.assertGreater(player._queue.qsize(), 0)
            player._queue.finish()
            player._run()
            self.assertEqual(factory.call_count, 1)
            self.assertEqual(stream.stop.call_count, 3)
            self.assertEqual(stream.start.call_count, 2)
        self.assertEqual(errors, [])
        self.assertEqual(sum(heard), 4800)
        self.assertAlmostEqual(player._queued_seconds, 0)
        self.assertTrue(any('audio_pending=0.30s' in line for line in logs))
        self.assertTrue(any('estimate=portaudio_not_physical' in line for line in logs))

    def test_output_drain_failure_retains_current_job(self):
        errors = []
        player = OrderedAudioPlayer('en', 0, lambda: 1, errors.append, self.root / 'drain.sqlite')
        wav = io.BytesIO()
        sf.write(wav, np.zeros(1600), 16000, format='WAV')
        player.enqueue(wav.getvalue(), 'en')
        with patch('church_translator.audio.sd.OutputStream') as factory:
            stream = factory.return_value.__enter__.return_value
            stream.write.return_value = False
            stream.stop.side_effect = RuntimeError('device disconnected during drain')
            player._run()
        self.assertEqual(player._queue.qsize(), 1)
        self.assertTrue(errors)
        player._queue.close()

    def test_uncertain_recognition_retains_audio_and_reaches_translation_request(self):
        with patch.object(Translator, '_init_gemini_client'):
            engine = TranslationEngine(self.cfg, EngineSettings(0, True, False, None, None, lambda: 1, lambda: 1),
                MagicMock(), MagicMock(), MagicMock(), MagicMock(), MagicMock())
        for work in (engine._chunks, engine._translations, engine._speech, engine._unrecognized):
            self.addCleanup(work.close)
        engine._transcriber = MagicMock()
        engine._transcriber.transcribe.return_value = SimpleNamespace(text='Klusā frāze paliek.', uncertain=True)
        samples = np.resize(np.array([.05, -.05], dtype='float32'), 16000)
        engine._chunks.put(ProcessingItem(0, 1, audio=samples))
        engine._chunks.finish()
        with patch('church_translator.engine.ChunkRecorder'):
            engine._process_loop()
        self.assertEqual(engine._unrecognized.qsize(), 1)
        engine._players = {'en': MagicMock()}
        observed = []
        def translate(*args, **kwargs):
            observed.append(engine._translator.source_uncertain)
            return {'en': 'The quiet phrase remains.'}
        engine._translator.translate_joint = translate
        engine._translation_loop()
        self.assertEqual(observed, [True])

    def test_audio_backlog_warning_preserves_all_jobs(self):
        logs = []
        player = OrderedAudioPlayer('en', 0, lambda: 1, self.fail, self.root / 'backlog.sqlite', logs.append)
        self.addCleanup(player._queue.close)
        wav = io.BytesIO()
        sf.write(wav, np.zeros(6 * 16000), 16000, format='WAV')
        for _ in range(3):
            player.enqueue(wav.getvalue(), 'en')
        self.assertEqual(player._queue.qsize(), 3)
        self.assertEqual(player._queued_seconds, 18)
        self.assertTrue(any('[PLAYBACK BACKLOG]' in line for line in logs))
