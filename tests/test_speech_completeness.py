import io
import json
from pathlib import Path
import queue
import sqlite3
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch
from dataclasses import replace

import numpy as np
import sounddevice as sd
import soundfile as sf

from church_translator.audio import ChunkRecorder, SAMPLE_RATE
from church_translator.config import load_config
from church_translator.glossary import Glossary
from church_translator.services import OpenAITranscriber
from church_translator.speech_audit import SpeechAudit, sample_hash
from scripts.export_speech_diagnostic import export_segment


class SpeechCompletenessTests(unittest.TestCase):
    def recorder(self, emit, **kwargs):
        return ChunkRecorder(0, 8, 0, 1.5, .7, .004, .025, emit, kwargs.pop('on_error', self.fail),
                             dynamic_mode=True, **kwargs)

    def stt(self, responses):
        service = OpenAITranscriber(replace(load_config(), openai_api_key='test',
                                           openai_transcription_model='gpt-4o-transcribe'), Glossary(), lambda _: None)
        service._client = MagicMock()
        service._client.audio.transcriptions.create.side_effect = responses
        return service

    def test_all_samples_including_tiny_onsets_and_silence_have_exact_ranges(self):
        rows = []
        rec = self.recorder(lambda seq, a, *_args: rows.append((rec.last_capture.copy(), a.copy())))
        samples = np.concatenate((np.zeros(50000, 'float32'), np.full(49, 1e-9, 'float32'),
                                  np.full(240000, .03, 'float32'), np.zeros(7700, 'float32')))
        for i in range(0, len(samples), 1733):
            rec._callback(samples[i:i+1733, None], len(samples[i:i+1733]), None, None)
        rec.stop()
        self.assertEqual(rec._received_samples, len(samples))
        self.assertEqual(rec._emitted_samples, len(samples))
        cursor = 0
        for metadata, data in rows:
            self.assertEqual(metadata['start_sample'], cursor)
            cursor = metadata['end_sample']
            self.assertEqual(len(data), cursor - metadata['start_sample'])
        np.testing.assert_array_equal(np.concatenate([a for _, a in rows]), samples)

    def test_callback_defers_warning_handlers_and_reports_adc_gap(self):
        errors = []
        rec = self.recorder(lambda *_: None, on_error=errors.append)
        rec._capture_thread = SimpleNamespace()
        rec._callback(np.ones((1600, 1), 'float32'), 1600, SimpleNamespace(inputBufferAdcTime=1), 'input overflow')
        rec._callback(np.ones((1600, 1), 'float32'), 1600, SimpleNamespace(inputBufferAdcTime=1.3), None)
        self.assertEqual(errors, [])
        for _ in range(2):
            start, audio, details = rec._raw.get_nowait()
            rec._consume(audio, start, details)
        self.assertEqual(len(errors), 2)

    def test_capture_queue_overflow_aborts_without_overwriting_old_frames(self):
        rec = self.recorder(lambda *_: None)
        rec._capture_thread = SimpleNamespace()
        rec._raw = queue.Queue(maxsize=1)
        rec._callback(np.ones((12, 1), 'float32'), 12, None, None)
        with self.assertRaises(sd.CallbackAbort):
            rec._callback(np.zeros((17, 1), 'float32'), 17, None, None)
        self.assertEqual(rec._dropped_samples, 17)
        np.testing.assert_array_equal(rec._raw.get_nowait()[1], np.ones(12))

    def test_journal_reopens_exact_original_and_rejects_gaps_and_corruption(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'audit.sqlite'
            audit = SpeechAudit(path)
            audio = np.arange(161, dtype='float32') / 200
            audit.capture('take', 0, audio[:100], {})
            audit.capture('take', 100, audio[100:], {})
            np.testing.assert_array_equal(SpeechAudit(path).audio_range('take', 0, 161), audio)
            with self.assertRaises(ValueError):
                audit.audio_range('take', 0, 162)
            with sqlite3.connect(path) as db:
                db.execute("UPDATE capture SET sha256='corrupt' WHERE start_sample=100")
            db.close()
            with self.assertRaises(ValueError):
                audit.audio_range('take', 0, 161)

    def test_storage_failure_keeps_active_prefix_and_raw_journal(self):
        with tempfile.TemporaryDirectory() as folder:
            audit = SpeechAudit(Path(folder) / 'audit.sqlite')
            emit = MagicMock(side_effect=OSError('disk full'))
            rec = self.recorder(emit, on_capture=audit.capture)
            original = np.ones(16000, 'float32') * .01
            rec._consume(original)
            with self.assertRaises(OSError):
                rec.stop()
            np.testing.assert_array_equal(np.concatenate(rec._active_buffer), original)
            np.testing.assert_array_equal(audit.audio_range(rec.capture_id, 0, len(original)), original)

    def test_short_tail_padding_and_pcm_quantization_are_explicit(self):
        service = self.stt([SimpleNamespace(text='Palīdzi mums.', logprobs=[])])
        original = np.sin(np.arange(733) * .3).astype('float32') * .05
        received = []
        def request(**kwargs):
            received.append(sf.read(kwargs['file'], dtype='float32')[0])
            return SimpleNamespace(text='Palīdzi mums.', logprobs=[])
        service._client.audio.transcriptions.create.side_effect = request
        service.transcribe(original)
        diag = service.last_audio_diagnostics
        self.assertEqual(diag['source_samples'], 733)
        self.assertEqual(diag['padding_samples'], 8000 - 733)
        self.assertEqual(diag['submitted_samples'], 8000)
        self.assertLessEqual(diag['pcm16_max_absolute_error'], 1 / 32768)
        self.assertTrue(np.all(received[0][733:] == 0))

    def test_sparse_unprompted_output_is_checked_once_and_disagreement_is_held(self):
        service = self.stt([SimpleNamespace(text='Dievs palīdz.', logprobs=[]),
                            SimpleNamespace(text='Dievs palīdz mums šodien rūpēties par savu ķermeni.', logprobs=[])])
        audio = np.resize(np.array([.05, -.05], 'float32'), 8 * SAMPLE_RATE)
        result = service.transcribe(audio)
        self.assertEqual(service._client.audio.transcriptions.create.call_count, 2)
        self.assertTrue(result.uncertain)
        self.assertEqual(result.text, '')
        self.assertEqual(result.raw_text, 'Dievs palīdz.')
        self.assertIn('mums', service.last_context_reconciliation['reference_free'])
        self.assertEqual(service.last_completeness['suspect_sample_range'], [0, len(audio)])

    def test_optional_recovery_failure_retains_candidate_and_does_not_retry_again(self):
        service = self.stt([SimpleNamespace(text='Dievs palīdz.', logprobs=[]), TimeoutError('offline')])
        result = service.transcribe(np.ones(128000, 'float32') * .02)
        self.assertTrue(result.uncertain)
        self.assertEqual(result.text, '')
        self.assertEqual(result.raw_text, 'Dievs palīdz.')
        self.assertEqual(service._client.audio.transcriptions.create.call_count, 2)

    def test_exhausted_key_fails_once_without_backoff_or_audio_mutation(self):
        from church_translator.reliability import retry_call
        error = RuntimeError('429 credit_balance_exhausted: You have no credits remaining')
        operation = MagicMock(side_effect=error)
        with patch('church_translator.reliability.time.sleep') as sleep:
            with self.assertRaises(RuntimeError):
                retry_call(operation)
        operation.assert_called_once()
        sleep.assert_not_called()

    def test_normal_speech_and_intentional_repetitions_do_not_trigger_extra_pass(self):
        phrase = 'Mēs runājam par ticību. Mēs runājam par ticību. Jā, jā, jā.'
        service = self.stt([SimpleNamespace(text=phrase, logprobs=[])])
        self.assertEqual(service.transcribe(np.ones(32000, 'float32') * .02).text, phrase)
        self.assertEqual(service._client.audio.transcriptions.create.call_count, 1)

    def test_text_patterns_cannot_delete_a_spoken_clause(self):
        phrase = 'Mēs runājam par To Kungu, Dievu Tēvu. Mēs rūpējamies par bērniem.'
        service = self.stt([SimpleNamespace(text=phrase, logprobs=[])])
        result = service.transcribe(np.ones(32000, 'float32') * .02)
        self.assertEqual(result.text, phrase)
        self.assertTrue(result.uncertain)

    def test_local_export_preserves_float_audio_and_exact_private_range(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            audit = SpeechAudit(root / 'audit.sqlite')
            audio = np.linspace(-.2, .2, 1003, dtype='float32')
            audit.capture('take', 0, audio, {})
            audit.event('captured_segment', 7, {'capture': {'capture_id': 'take', 'start_sample': 10, 'end_sample': 901}})
            result = export_segment(audit.path, root / 'private.zip', 7)
            self.assertEqual(result['original_float32_sha256'], sample_hash(audio[10:901]))
            self.assertFalse(result['verified_reference'])
