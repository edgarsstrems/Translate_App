"""Real capture workers and disk journal, with no device or external services."""
import queue
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

import numpy as np
import sounddevice as sd

from church_translator.audio import ChunkRecorder, SAMPLE_RATE
from church_translator.speech_audit import SpeechAudit
from church_translator.backlog import DurableQueue
from church_translator.config import load_config
from church_translator.engine import EngineSettings, TranslationEngine


class ContinuousJournalTests(unittest.TestCase):
    def test_capture_starts_before_model_readiness_and_stop_failure_still_closes_queues(self):
        with tempfile.TemporaryDirectory() as folder, patch('church_translator.engine.app_data_dir', return_value=Path(folder)), patch('church_translator.engine.ChunkRecorder') as recorder:
            entered, release = threading.Event(), threading.Event()
            engine = TranslationEngine(load_config(), EngineSettings(0, False, False, None, None, lambda: 0, lambda: 0),
                lambda *_: None, lambda *_: None, lambda *_: None, lambda *_: None, lambda *_: None)
            engine._transcriber = MagicMock()
            def ensure():
                entered.set()
                self.assertTrue(release.wait(10))
            engine._transcriber.ensure_model.side_effect = ensure
            engine.start()
            try:
                self.assertTrue(entered.wait(10))
                recorder.return_value.start.assert_called_once()
                recorder.return_value.stop.side_effect = OSError('disk full')
            finally:
                release.set()
                engine.stop()
            self.assertEqual(engine.completion_info['capture_stop_failed'], 1)
            self.assertEqual(engine.completion_info['capture_pending'], 0)
            self.assertFalse(engine._processor_thread.is_alive())

    def test_completed_downstream_receipt_prevents_requeue_after_reopen(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'work.sqlite'
            work = DurableQueue(path)
            self.assertTrue(work.put({'text': 'first'}, key='segment:7'))
            self.assertFalse(work.put({'text': 'duplicate'}, key='segment:7'))
            self.assertEqual(work.get()['text'], 'first')
            work.ack()
            work.close()
            work = DurableQueue(path)
            self.assertFalse(work.put({'text': 'replayed'}, key='segment:7'))
            self.assertEqual(work.qsize(), 0)
            work.close()

    def wait_for(self, predicate):
        deadline = time.monotonic() + 10
        while not predicate():
            if time.monotonic() > deadline:
                self.fail('Worker did not reach expected checkpoint')
            time.sleep(.005)

    def test_journal_advances_while_segment_delivery_is_blocked(self):
        with tempfile.TemporaryDirectory() as folder:
            audit = SpeechAudit(Path(folder) / 'speech-audit.sqlite')
            entered, release = threading.Event(), threading.Event()
            chunks, errors = [], []
            def emit(seq, data, *_):
                entered.set()
                if not release.wait(10):
                    raise RuntimeError('Test delivery gate timed out')
                chunks.append(data.copy())
                audit.event('captured_segment', seq, {'capture': rec.last_capture})
            rec = ChunkRecorder(0, 8, 0, 1.8, .6, .004, .025, emit, errors.append,
                                dynamic_mode=True, capture_audit=audit)
            rec._start_workers()
            original = np.resize(np.array([.05, -.05], 'float32'), 20 * SAMPLE_RATE + 73)
            try:
                for start in range(0, len(original), 1600):
                    block = original[start:start + 1600]
                    rec._callback(block[:, None], len(block), None, None)
                self.assertTrue(entered.wait(10))
                self.wait_for(lambda: rec._journaled_samples == len(original))
                self.assertEqual(chunks, [])
                np.testing.assert_array_equal(audit.audio_range(rec.capture_id, 0, len(original)), original)
            finally:
                release.set()
                rec.stop()
            self.assertEqual(errors, [])
            np.testing.assert_array_equal(np.concatenate(chunks), original)
            self.assertEqual([len(c) for c in chunks], [128000, 128000, 64073])
            report = audit.integrity_report()
            self.assertEqual(report['captured_samples'], len(original))
            self.assertEqual(report['logical_segmented_samples'], len(original))
            self.assertEqual(report['uncovered_ranges'], [])
            self.assertEqual(report['unjournaled_samples'], 0)

    def test_memory_overflow_preserves_reserved_packet_and_reports_stop(self):
        with tempfile.TemporaryDirectory() as folder:
            errors, chunks = [], []
            audit = SpeechAudit(Path(folder) / 'speech-audit.sqlite')
            rec = ChunkRecorder(0, 8, 0, 1.8, .6, .004, .025,
                                lambda seq, a, *args: chunks.append(a), errors.append, capture_audit=audit)
            rec._raw = queue.Queue(maxsize=1)
            rec._capture_thread = SimpleNamespace()  # Hold the consumer until overflow.
            rec._callback(np.full((1600, 1), .03, 'float32'), 1600, None, None)
            with self.assertRaises(sd.CallbackAbort):
                rec._callback(np.full((733, 1), .02, 'float32'), 733, None, None)
            rec._start_workers()
            rec.stop()
            self.assertEqual(sum(map(len, chunks)), 2333)
            self.assertEqual(rec._dropped_samples, 0)
            self.assertEqual(rec._journaled_samples, 2333)
            self.assertTrue(any('input stopped' in e for e in errors))
            self.assertEqual(audit.integrity_report()['capacity_aborts'], 1)

    def test_mode_changes_preserve_all_quiet_zero_and_partial_frames(self):
        emitted = []
        rec = ChunkRecorder(0, 8, 0, 1.8, .6, .004, .025,
                            lambda seq, a, *args: emitted.append(a.copy()), self.fail, dynamic_mode=True)
        rng = np.random.default_rng(42)
        blocks = []
        for i in range(500):
            data = rng.uniform(-1e-5, 1e-5, 733).astype('float32') if i % 5 else np.zeros(733, 'float32')
            blocks.append(data)
            if i in (70, 210, 400):
                dynamic = i == 210
                rec.update_parameters(8 if dynamic else 3, 1.8, .6, dynamic_mode=dynamic)
            rec._callback(data[:, None], len(data), None, None)
        rec.stop()
        np.testing.assert_array_equal(np.concatenate(emitted), np.concatenate(blocks))

    def test_integrity_exposes_unsegmented_crash_tail_and_hardware_warning(self):
        with tempfile.TemporaryDirectory() as folder:
            audit = SpeechAudit(Path(folder) / 'speech-audit.sqlite')
            audit.capture('take', 0, np.ones(320, 'float32'), {'status': 'input overflow'})
            audit.event('captured_segment', 0, {'capture': {'capture_id': 'take', 'start_sample': 0, 'end_sample': 200}})
            report = audit.integrity_report()
            self.assertEqual(report['uncovered_ranges'], [{'capture_id': 'take', 'start': 200, 'end': 320}])
            self.assertEqual(report['capture_warning_count'], 1)
            self.assertFalse(report['hardware_completeness_verified'])

