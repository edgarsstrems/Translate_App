"""Submission, source contribution and overlap ownership—not natural WER."""
from dataclasses import replace
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch
import zipfile

import numpy as np
import soundfile as sf

from church_translator.audio import ChunkRecorder, SAMPLE_RATE
from church_translator.audio_diagnostics import delete_drained_recording
from church_translator.boundary_alignment import owns_timestamped_words
from church_translator.config import load_config, get_speed_profile_settings
from church_translator.engine import TranslationEngine, EngineSettings, ProcessingItem
from church_translator.glossary import Glossary
from church_translator.services import OpenAITranscriber, TranscriptionResult
from church_translator.speech_audit import SpeechAudit
from church_translator.speech_gate import audio_evidence
from church_translator.services import Translator
from church_translator.translation_quality import translation_issue, translation_warnings
from scripts.export_speech_diagnostic import export_segment


class RecognitionCoverageTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        environment = patch.dict(os.environ, {'CHURCH_TRANSLATOR_APPDATA': folder.name})
        environment.start()
        self.addCleanup(environment.stop)

    def transcriber(self, responses, **changes):
        options = {'openai_api_key': 'fixture', 'openai_transcription_model': 'gpt-4o-transcribe', **changes}
        config = replace(load_config(), **options)
        service = OpenAITranscriber(config, Glossary(), lambda _: None)
        service._client = MagicMock()
        service._client.audio.transcriptions.create.side_effect = responses
        return service

    @staticmethod
    def speech():
        return np.resize(np.array([.05, -.05], 'float32'), 160000)

    def test_new_model_uses_languages_and_sanitized_context_without_legacy_parameters(self):
        service = self.transcriber([SimpleNamespace(text='Mēs palīdzam cits citam.', logprobs=None)],
                                  openai_transcription_model='gpt-transcribe', stt_max_recovery_requests=0)
        service.transcribe(self.speech()[:16000], previous_context='Ticība un cerība.',
                           prepared_context={'vocabulary': 'Pāvils; Kolosiešiem\n<bad>'})
        call = service._client.audio.transcriptions.create.call_args.kwargs
        self.assertEqual(call['languages'], ['lv'])
        for name in ('language', 'temperature', 'include'):
            self.assertNotIn(name, call)
        self.assertEqual(call['keywords'], ['Pāvils', 'Kolosiešiem'])
        self.assertIn('Earlier speech', call['prompt'])
        self.assertEqual(call['file'].name, 'chunk.wav')
        call['file'].seek(0)
        received, rate = sf.read(call['file'], dtype='float32')
        self.assertEqual(rate, 16000)
        self.assertEqual(len(received), 16000)

    def test_six_words_over_long_probable_speech_triggers_coverage_review(self):
        evidence = {'vad_available': True, 'probable_speech_seconds': 8.72}
        status = OpenAITranscriber._completeness_signals(self.speech(),
                  'Mēs varam palīdzēt viens otram šodien.', evidence)
        self.assertTrue(status['suspicious'])
        self.assertIn('sparse_output_over_probable_speech', status['warning_reasons'])
        self.assertFalse(status['proof_of_omission'])

    def test_bounded_resegmentation_accepts_only_an_exact_source_prefix_extension(self):
        initial = 'Mēs palīdzam cits citam.'
        recovered = initial + ' Mēs turpinām ar pacietību un atzīstam savas kļūdas.'
        service = self.transcriber([SimpleNamespace(text=initial, logprobs=[]),
                                   SimpleNamespace(text=recovered, logprobs=[])])
        service.speech_evidence = {'vad_available': True, 'probable_speech_seconds': 8.72}
        result = service.transcribe(self.speech())
        self.assertEqual(result.raw_text, initial)
        self.assertEqual(result.text, recovered)
        self.assertTrue(result.coverage_review)
        self.assertFalse(result.uncertain)  # Coverage is separate from word confidence.
        calls = service._client.audio.transcriptions.create.call_args_list
        self.assertEqual(len(calls), 2)
        self.assertNotIn('chunking_strategy', calls[0].kwargs)
        self.assertEqual(calls[1].kwargs['chunking_strategy'], 'auto')
        self.assertNotIn('prompt', calls[1].kwargs)

    def test_conflicting_recovery_cannot_replace_negation_or_original_words(self):
        initial = 'Mēs nepalīdzam cits citam.'
        alternative = 'Mēs palīdzam cits citam un darām visu pareizi.'
        service = self.transcriber([SimpleNamespace(text=initial, logprobs=[]),
                                   SimpleNamespace(text=alternative, logprobs=[])])
        service.speech_evidence = {'probable_speech_seconds': 8}
        result = service.transcribe(self.speech())
        self.assertEqual(result.text, initial)
        self.assertEqual(service.last_context_reconciliation['reference_free'], alternative)
        self.assertTrue(result.coverage_review)

    def test_failed_recovery_retains_visible_source_but_blocks_translation(self):
        service = self.transcriber([SimpleNamespace(text='Palīdzi mums.', logprobs=[]), TimeoutError('offline')])
        result = service.transcribe(self.speech())
        self.assertEqual(result.text, 'Palīdzi mums.')
        self.assertFalse(result.translation_allowed)
        self.assertTrue(result.coverage_review)
        self.assertEqual(service._client.audio.transcriptions.create.call_count, 2)

    def test_zero_recovery_budget_never_buys_an_extra_request(self):
        service = self.transcriber([SimpleNamespace(text='Palīdzi mums.', logprobs=[])], stt_max_recovery_requests=0)
        result = service.transcribe(self.speech())
        self.assertTrue(result.coverage_review)
        service._client.audio.transcriptions.create.assert_called_once()

    def test_stationary_room_noise_is_ambiguous_not_confirmed_silence(self):
        noise = np.random.default_rng(17).normal(0, .0005, 32000).astype('float32')
        self.assertFalse(audio_evidence(noise)['skip_stt'])

    def test_overlapping_requests_preserve_exact_logical_timeline_in_every_mode(self):
        original = np.resize(np.array([.05, -.05], 'float32'), 61 * SAMPLE_RATE + 73)
        for mode in ('balanced', 'dynamic', 'low_latency'):
            for overlap in (.3, .5, .7):
                with self.subTest(mode=mode, overlap=overlap):
                    profile = get_speed_profile_settings(mode)
                    chunks, captures = [], []
                    def emit(seq, audio, *_):
                        chunks.append(audio)
                        captures.append(recorder.last_capture.copy())
                    recorder = ChunkRecorder(0, profile['chunk_seconds'], overlap, profile['min_chunk_seconds'],
                        profile['early_flush_silence_seconds'], .004, .025, emit, self.fail,
                        dynamic_mode=mode == 'dynamic')
                    for offset in range(0, len(original), 1733):
                        recorder._consume(original[offset:offset + 1733])
                    recorder.stop()
                    np.testing.assert_array_equal(np.concatenate(chunks), original)
                    cursor = 0
                    for capture in captures:
                        self.assertEqual(capture['start_sample'], cursor)
                        cursor = capture['end_sample']
                        self.assertLessEqual((capture['request_end_sample'] - capture['request_start_sample']) / SAMPLE_RATE,
                                             profile['chunk_seconds'] + 1 / SAMPLE_RATE)
                        self.assertLessEqual(capture['request_end_sample'], len(original))

    def test_word_timestamps_preserve_a_crossing_word_and_a_real_repetition(self):
        capture = {'capture_id': 'take', 'start_sample': 16000, 'end_sample': 32000,
                   'request_start_sample': 8000, 'request_end_sample': 40000,
                   'request_overlap_samples': 16000, 'sample_rate': 16000}
        result = TranscriptionResult('Iepriekš Āmen. Āmen. Tagad.', False, words=[
            {'word': 'Iepriekš', 'start': 0, 'end': .4},
            {'word': 'Āmen.', 'start': .4, 'end': .65},
            {'word': 'Āmen.', 'start': .7, 'end': .95},
            {'word': 'Tagad.', 'start': 1, 'end': 1.3}])
        owned, evidence = owns_timestamped_words(result, capture)
        self.assertTrue(evidence['applied'])
        self.assertEqual(owned.text, 'Āmen. Āmen. Tagad.')
        result = replace(result, words=None)
        unchanged, evidence = owns_timestamped_words(result, capture)
        self.assertEqual(unchanged.text, result.text)
        self.assertTrue(evidence['review_required'])

    def test_partial_word_timestamps_cannot_delete_untimed_source_words(self):
        capture = {'capture_id': 'take', 'start_sample': 0, 'end_sample': 16000,
                   'request_start_sample': 0, 'request_end_sample': 24000,
                   'request_overlap_samples': 8000, 'sample_rate': 16000}
        result = TranscriptionResult('Jā. Bet es nezinu.', False,
                                    words=[{'word': 'Jā.', 'start': 0, 'end': .3}])
        owned, evidence = owns_timestamped_words(result, capture)
        self.assertEqual(owned.text, result.text)
        self.assertEqual(evidence['reason'], 'untimed_source_words')

    def test_diagnostic_contains_exact_submitted_bytes_and_raw_response(self):
        audit = SpeechAudit(self.root / 'speech-audit.sqlite')
        audio = self.speech()[:1600]
        audit.capture('take', 0, audio, {})
        audit.event('captured_segment', 0, {'capture': {'capture_id': 'take', 'start_sample': 0, 'end_sample': len(audio)}})
        buffer = io.BytesIO()
        sf.write(buffer, audio, 16000, format='WAV', subtype='PCM_16')
        receipt = audit.submission(0, 'submitted', {'audio': buffer.getvalue(), 'filename': 'chunk.wav'})
        audit.submission(0, 'returned', {'receipt': receipt, 'response': {'text': 'Jā. Jā.'}})
        output = self.root / 'private.zip'
        export_segment(audit.path, output, segment=0)
        with zipfile.ZipFile(output) as archive:
            self.assertEqual(archive.read('submitted-001.wav'), buffer.getvalue())
            self.assertEqual(json.loads(archive.read('submitted-001.json'))['raw_provider_response']['text'], 'Jā. Jā.')

    def test_retention_never_deletes_an_active_or_pending_queue(self):
        root = self.root / 'backlog'
        session = root / 'take'
        session.mkdir(parents=True)
        (session / 'integrity.json').write_text('{}')
        with self.assertRaises(ValueError):
            delete_drained_recording(session, root, active=session)
        database = sqlite3.connect(session / 'capture.sqlite')
        database.execute('CREATE TABLE work(id INTEGER)')
        database.execute('INSERT INTO work VALUES(1)')
        database.commit()
        database.close()
        with self.assertRaises(ValueError):
            delete_drained_recording(session, root)
        self.assertTrue(session.exists())
        database = sqlite3.connect(session / 'capture.sqlite')
        database.execute('DELETE FROM work')
        database.commit()
        database.close()
        delete_drained_recording(session, root)
        self.assertFalse(session.exists())

    def test_held_source_is_committed_in_order_without_translation(self):
        visible, errors = [], []
        config = replace(load_config(), speech_recognition_backend='openai')
        with patch.object(Translator, '_init_gemini_client'):
            engine = TranslationEngine(config, EngineSettings(0, True, False, None, None, lambda: 1, lambda: 1),
                lambda *_: None, errors.append, lambda *_: None, visible.append, lambda *_: None)
        for work in (engine._chunks, engine._translations, engine._speech, engine._unrecognized):
            self.addCleanup(work.close)
        engine._transcriber = MagicMock()
        engine._transcriber.transcribe.return_value = TranscriptionResult(
            'Es neesmu viens.', True, raw_text='Es neesmu viens.', translation_allowed=False, coverage_review=True)
        engine._chunks.put(ProcessingItem(0, 1, audio=self.speech()[:16000]))
        engine._chunks.finish()
        with patch('church_translator.engine.ChunkRecorder'):
            engine._process_loop()
        self.assertFalse(errors)
        self.assertEqual(visible, ['Es neesmu viens.'])
        self.assertEqual(engine._translations.qsize(), 0)
        self.assertEqual(engine._unrecognized.qsize(), 1)
        report = engine._audit.integrity_report()['recognition_coverage']
        self.assertEqual(report['source_committed'], 1)
        self.assertEqual(report['review_required'], 1)
        self.assertFalse(report['speech_completeness_verified'])

    def test_disk_reserve_aborts_future_input_and_preserves_queued_pcm(self):
        audit = SpeechAudit(self.root / 'reserve.sqlite')
        recorder = ChunkRecorder(0, 6, 0, 4, .6, .004, .025, lambda *_: None, self.fail, capture_audit=audit)
        recorder._stream = MagicMock()
        for index in range(3):
            recorder._raw.put((index * 1600, self.speech()[:1600], {}))
        with patch('church_translator.audio.shutil.disk_usage', return_value=SimpleNamespace(free=128 * 1024 ** 2)):
            recorder._journal_loop()
        recorder._stream.abort.assert_called_once()
        self.assertTrue(recorder._storage_stopped)
        self.assertEqual(recorder._journaled_samples, 4800)
        np.testing.assert_array_equal(audit.audio_range(recorder.capture_id, 0, 4800), self.speech()[:4800])

    def test_failed_journal_batch_is_retained_for_recovery(self):
        audit = SpeechAudit(self.root / 'failed.sqlite')
        recorder = ChunkRecorder(0, 6, 0, 4, .6, .004, .025, lambda *_: None, self.fail, capture_audit=audit)
        original = self.speech()[:1600]
        recorder._raw.put((0, original, {}))
        with patch.object(audit, 'capture_packets', side_effect=OSError('disk unavailable')):
            recorder._journal_loop()
        np.testing.assert_array_equal(recorder._failed_journal_packets[0][1], original)
        self.assertEqual(recorder._journaled_samples, 0)

    def test_invalid_timestamps_never_erase_source(self):
        capture = {'capture_id': 'take', 'start_sample': 0, 'end_sample': 16000,
                   'request_start_sample': 0, 'request_end_sample': 24000,
                   'request_overlap_samples': 8000, 'sample_rate': 16000}
        result = TranscriptionResult('Jā. Jā.', False, words=[{'word': 'Jā.', 'start': 0, 'end': float('nan')}])
        owned, evidence = owns_timestamped_words(result, capture)
        self.assertEqual(owned.text, result.text)
        self.assertTrue(evidence['review_required'])

    def test_translation_checks_warn_without_rewriting_negation_or_repetition(self):
        self.assertIn('possible_negation_change_unverified', translation_warnings('Mēs nevaram to darīt.', 'We can do it.', 'en'))
        self.assertFalse(translation_warnings('Mēs nevaram to darīt.', 'We cannot do it.', 'en'))
        self.assertFalse(translation_warnings('Mēs palīdzam cits citam. Mēs palīdzam cits citam.',
                                            'We help each other. We help each other.', 'en'))
        self.assertEqual(translation_issue('Mēs palīdzam viens otram šodien. ' * 8, 'We help.', 'en'), 'severely_shortened_source')


if __name__ == '__main__':
    unittest.main()
