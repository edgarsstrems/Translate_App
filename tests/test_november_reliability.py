"""Acoustic admission, private export/retention and independent voice workers."""
from dataclasses import replace
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch
import zipfile

import numpy as np
from PySide6.QtWidgets import QApplication

from church_translator.backlog import DurableQueue
from church_translator.config import load_config
from church_translator.diagnostic_export import export_segments, parse_segments, recording_info
from church_translator.diagnostic_retention import cleanup_recordings
from church_translator.engine import EngineSettings, TranslationEngine
from church_translator.glossary import Glossary
from church_translator.recording_guard import recording_lease
from church_translator.services import OpenAITranscriber
from church_translator.speech_audit import SpeechAudit
from church_translator.speech_gate import audio_evidence
from church_translator.ui_compat import FollowingLog


class NovemberTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        env = patch.dict(os.environ, {'CHURCH_TRANSLATOR_APPDATA': temporary.name})
        env.start()
        self.addCleanup(env.stop)

    def transcriber(self, text):
        service = OpenAITranscriber(replace(load_config(), openai_api_key='fixture'), Glossary(), lambda _: None)
        service._client = MagicMock()
        service._client.audio.transcriptions.create.return_value = SimpleNamespace(text=text, logprobs=[])
        return service

    def test_noise_scores_cannot_enable_references_or_gain_without_speech_frames(self):
        for probability in (.20, .2449, .275, .2776, .2953, .3499):
            samples = np.resize(np.array([1e-5, -2e-5], 'float32'), 40000)
            with patch('faster_whisper.vad.get_vad_model', return_value=lambda x: np.full(len(x) // 512, probability)):
                evidence = audio_evidence(samples)
            self.assertFalse(evidence['skip_stt'])
            self.assertFalse(evidence['use_context'])
            self.assertFalse(evidence['allow_gain'])
            self.assertEqual(evidence['probable_speech_seconds'], 0)

    def test_supported_quiet_speech_keeps_gain_context_and_genuine_repetition(self):
        samples = np.resize(np.array([1e-5, -2e-5], 'float32'), 40000)
        with patch('faster_whisper.vad.get_vad_model', return_value=lambda x: np.full(len(x) // 512, .8)):
            evidence = audio_evidence(samples)
        self.assertTrue(evidence['use_context'])
        self.assertTrue(evidence['allow_gain'])
        text = 'Mēs šodien kopā mācāmies un klausāmies.'
        service = self.transcriber(text)
        service.speech_evidence = evidence
        result = service.transcribe(samples, previous_context=text)
        self.assertTrue(result.translation_allowed)
        self.assertEqual(result.text, text)
        self.assertIn('prompt', service._client.audio.transcriptions.create.call_args.kwargs)

    def test_unsupported_previous_echo_is_held_without_buying_a_retry(self):
        text = 'Mēs šodien kopā mācāmies un klausāmies.'
        for seconds in (1.78, 2.5, 8):
            service = self.transcriber(text)
            service.speech_evidence = {'vad_available': True, 'use_context': False,
                'rms_dbfs': -78, 'peak_dbfs': -56, 'probable_speech_seconds': 0,
                'probable_speech_sample_ranges': []}
            service._previous_text = text
            result = service.transcribe(np.full(int(seconds * 16000), 1e-5, 'float32'), previous_context=text)
            self.assertFalse(result.translation_allowed)
            self.assertTrue(result.coverage_review)
            self.assertEqual(result.raw_text, text)
            self.assertEqual(result.text, text)
            self.assertNotIn('prompt', service._client.audio.transcriptions.create.call_args.kwargs)
            service._client.audio.transcriptions.create.assert_called_once()

    def test_empty_ambiguous_input_never_buys_a_duplicate_decode(self):
        service = self.transcriber('')
        service.speech_evidence = {'vad_available': True, 'use_context': False, 'probable_speech_seconds': 0}
        result = service.transcribe(np.resize(np.array([0, .0001], 'float32'), 40000))
        service._client.audio.transcriptions.create.assert_called_once()
        self.assertEqual(result.text, '')

    def recording(self, name='take', captured=1000000):
        session = self.root / 'backlog' / name
        audit = SpeechAudit(session / 'speech-audit.sqlite')
        for position, segment in enumerate((181, 185, 186, 187, 188, 189, 190, 191, 193)):
            audio = np.full(160, .01, 'float32')
            audit.capture('fixture', position * 160, audio, {'wall_time': captured + position * .01})
            audit.event('captured_segment', segment, {'capture': {'capture_id': 'fixture',
                'start_sample': position * 160, 'end_sample': (position + 1) * 160}, 'session_id': 'test-session'})
        audit.event('completion', None, {})
        counts = dict.fromkeys(('capture_pending', 'translation_pending', 'synthesis_pending',
            'playback_pending', 'audio_review_items', 'yoruba_audio_pending', 'translation_review_items'), 0)
        (session / 'integrity.json').write_text(json.dumps(counts))
        return session, audit

    def test_parse_examples_duplicates_whitespace_invalid_and_large_range(self):
        self.assertEqual(parse_segments(' 181, 185 - 191, 193, 185 '), [181, *range(185, 192), 193])
        self.assertEqual(len(parse_segments('0-10000')), 10001)
        for bad in ('', '1,', '-1', '3-2', '1;2', '1--3', '1.5', '0-999999999999999'):
            with self.assertRaises(ValueError, msg=bad):
                parse_segments(bad)

    def test_export_manifest_and_exact_segment_mapping(self):
        session, audit = self.recording()
        destination = self.root / 'private.zip'
        result = export_segments(audit.path, destination, [193, 185, 185, 999])
        self.assertEqual(result['exported_segments'], [185, 193])
        self.assertEqual(result['missing_segments'], [999])
        self.assertEqual(result['session_ids'], ['test-session'])
        with zipfile.ZipFile(destination) as archive:
            self.assertIsNone(archive.testzip())
            self.assertIn('segment-185/original.wav', archive.namelist())
            metadata = json.loads(archive.read('segment-185/audit.json'))
            self.assertEqual(metadata['start_sample'], 160)
            self.assertEqual(json.loads(archive.read('manifest.json')), result)
        self.assertEqual(len(recording_info(audit.path)['segments']), 9)

    def test_cleanup_exact_age_boundary_and_preserves_database_metadata_and_exports(self):
        session, audit = self.recording()
        cutoff = 1000000 - .01 + 336 * 3600
        export = session / 'user-export.zip'
        export.write_bytes(b'keep')
        self.assertEqual(cleanup_recordings(session.parent, now=cutoff)['deleted'], 0)
        result = cleanup_recordings(session.parent, now=cutoff + .001)
        self.assertEqual(result['deleted'], 1, result)
        self.assertGreater(result['estimated_bytes_reclaimed'], 0)
        self.assertTrue(audit.path.exists())
        self.assertEqual(export.read_bytes(), b'keep')
        db = sqlite3.connect(audit.path)
        self.addCleanup(db.close)
        self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
        self.assertEqual(db.execute('SELECT COUNT(*) FROM capture WHERE audio IS NOT NULL').fetchone()[0], 0)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM events WHERE kind='captured_segment'").fetchone()[0], 9)
        self.assertTrue(recording_info(audit.path)['audio_expired'])
        self.assertEqual(cleanup_recordings(session.parent, now=cutoff + 10)['deleted'], 0)

    def test_export_and_capture_leases_protect_retention(self):
        session, audit = self.recording()
        with recording_lease(session):
            with recording_lease(session):  # Export and capture may coexist.
                result = cleanup_recordings(session.parent, now=10000000)
        self.assertEqual(result['protected'], 1)
        self.assertEqual(result['deleted'], 0)
        self.assertEqual(len(audit.audio_range('fixture', 0, 160)), 160)

    def test_interrupted_cleanup_rolls_back_all_audio_and_keeps_sqlite_valid(self):
        session, audit = self.recording()
        db = sqlite3.connect(audit.path)
        db.execute("CREATE TRIGGER interrupt_expiry BEFORE INSERT ON events "
                   "WHEN NEW.kind='diagnostic_audio_expired' BEGIN SELECT RAISE(ABORT, 'interrupted'); END")
        db.commit()
        db.close()
        result = cleanup_recordings(session.parent, now=10000000)
        self.assertEqual(result['deleted'], 0)
        self.assertEqual(len(result['errors']), 1)
        np.testing.assert_array_equal(audit.audio_range('fixture', 0, 160), np.full(160, .01, 'float32'))

    def test_pending_review_and_invalid_timestamps_protect_recordings(self):
        for field in ('audio_review_items', 'translation_pending', 'playback_pending', 'synthesis_pending'):
            session, audit = self.recording(field)
            snapshot = json.loads((session / 'integrity.json').read_text())
            snapshot[field] = 1
            (session / 'integrity.json').write_text(json.dumps(snapshot))
        session, audit = self.recording('bad-time', captured=float('nan'))
        result = cleanup_recordings(session.parent, now=10000000)
        self.assertEqual(result['deleted'], 0)
        self.assertEqual(result['protected'], 5)

    def test_another_process_holds_recording_until_its_export_finishes(self):
        session, audit = self.recording()
        script = ('import sys; from church_translator.recording_guard import recording_lease; '
                  'lease=recording_lease(sys.argv[1]); lease.__enter__(); '
                  'print("locked",flush=True); sys.stdin.readline(); lease.__exit__(None,None,None)')
        child = subprocess.Popen([sys.executable, '-c', script, str(session)],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(child.stdout.readline().strip(), 'locked')
            self.assertEqual(cleanup_recordings(session.parent, now=10000000)['protected'], 1)
            child.communicate('\n', timeout=5)
        finally:
            if child.poll() is None:
                child.kill()
                child.communicate()
        self.assertEqual(cleanup_recordings(session.parent, now=10000000)['deleted'], 1)

    def test_language_worker_failure_preserves_its_work_and_other_language_continues(self):
        for failing in ('en', 'yo'):
            cfg = replace(load_config(), gemini_api_key=None)
            engine = TranslationEngine(cfg, EngineSettings(0, True, True, None, None, lambda: 0, lambda: 0,
                                      secondary_language='yo'), *[MagicMock() for _ in range(5)])
            for q in (engine._chunks, engine._translations, engine._speech, engine._unrecognized):
                self.addCleanup(q.close)
            engine._language_speech = {lang: DurableQueue(engine.session_dir / f'synthesis-{lang}.sqlite') for lang in ('en', 'yo')}
            for q in engine._language_speech.values():
                self.addCleanup(q.close)
            player = MagicMock()
            engine._players = {'en': player, 'yo': player}
            class Voice:
                _client = None
                def __init__(self, config, **kwargs):
                    self.config = config
                def synthesize(self, text, lang):
                    if lang == failing:
                        raise RuntimeError('fixture failure')
                    return b'fixture'
            for seq in range(2):
                engine._speech.put({'sequence_id': seq, 'captured_at': time.monotonic(),
                    'translations': {'en': 'Hope.', 'yo': 'Ireti.'}, 'completed': []})
            engine._speech.finish()
            with patch('church_translator.engine.TextToSpeech', Voice):
                engine._speech_loop()
            self.assertEqual(player.enqueue.call_count, 2)
            self.assertTrue(all(call.kwargs['language'] != failing for call in player.enqueue.call_args_list))
            if failing == 'en':
                self.assertEqual(engine._language_speech['en'].qsize(), 2)
            else:
                self.assertEqual(engine._deferred_yoruba_jobs, 2)

    def test_log_follows_hidden_and_reopened_but_respects_manual_scroll(self):
        app = QApplication.instance() or QApplication([])
        panel = FollowingLog()
        panel.resize(450, 130)
        panel.show()
        self.addCleanup(panel.close)
        for i in range(70):
            panel.appendHtml(f'line {i}')
        app.processEvents()
        bar = panel.verticalScrollBar()
        self.assertEqual(bar.value(), bar.maximum())
        bar.setValue(5)
        panel.appendHtml('new line')
        self.assertEqual(bar.value(), 5)
        panel.hide()
        panel.appendHtml('hidden line')
        panel.show()
        app.processEvents()
        self.assertEqual(bar.value(), 5)
        bar.setValue(bar.maximum())
        panel.hide()
        panel.appendHtml('new hidden line')
        panel.show()
        app.processEvents()
        self.assertEqual(bar.value(), bar.maximum())

    def test_slow_optional_voice_does_not_occupy_english_worker_and_shared_order_is_stable(self):
        for shared in (False, True):
            cfg = replace(load_config(), gemini_api_key=None)
            engine = TranslationEngine(cfg, EngineSettings(0, True, True, None, None, lambda: 0, lambda: 0,
                                      secondary_language='yo'), *[MagicMock() for _ in range(5)])
            queues = [engine._chunks, engine._translations, engine._speech, engine._unrecognized]
            for q in queues:
                self.addCleanup(q.close)
            engine._language_speech = {lang: DurableQueue(engine.session_dir / f'synthesis-{lang}.sqlite') for lang in ('en', 'yo')}
            for q in engine._language_speech.values():
                self.addCleanup(q.close)
            order, english_ready = [], threading.Event()
            english_player, secondary_player = MagicMock(), MagicMock()
            english_player.enqueue.side_effect = lambda audio, **kw: order.append((kw['sequence_id'], kw['language']))
            secondary_player.enqueue.side_effect = english_player.enqueue.side_effect
            engine._players = {'en': english_player, 'yo': english_player if shared else secondary_player}
            class Voice:
                _client = None
                def __init__(self, config, **kwargs):
                    self.config = config
                def synthesize(self, text, lang):
                    if lang == 'yo':
                        if not english_ready.wait(3):
                            raise RuntimeError('English synthesis was blocked by optional initialization')
                    if text == 'English 1':
                        english_ready.set()
                    return b'fixture'
            for seq in range(2):
                engine._speech.put({'sequence_id': seq, 'captured_at': time.monotonic(),
                    'translations': {'en': f'English {seq}', 'yo': f'Optional {seq}'}, 'completed': []})
            engine._speech.finish()
            with patch('church_translator.engine.TextToSpeech', Voice):
                engine._speech_loop()
            self.assertTrue(english_ready.is_set())
            self.assertEqual(engine._speech.qsize(), 0)
            self.assertTrue(all(q.qsize() == 0 for q in engine._language_speech.values()))
            if shared:
                self.assertEqual(order, [(0, 'en'), (0, 'yo'), (1, 'en'), (1, 'yo')])
            else:
                self.assertEqual([seq for seq, lang in order if lang == 'en'], [0, 1])


if __name__ == '__main__':
    unittest.main()
