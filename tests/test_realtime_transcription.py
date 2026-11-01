"""Persistent transport contracts with journaled PCM and a mocked WebSocket."""
from dataclasses import replace
import base64
import json
import io
import hashlib
import os
from pathlib import Path
import queue
import threading
import tempfile
import time
from types import SimpleNamespace
import unittest
import zipfile
from unittest.mock import MagicMock, patch

import numpy as np
from PySide6.QtWidgets import QApplication

from church_translator.config import load_config, save_user_settings
from church_translator.glossary import Glossary
from church_translator.main import MainWindow
from church_translator.realtime_transcription import RealtimeTranscriber, Pcm24kResampler, REALTIME_URL, session_configuration
from church_translator.services import OpenAITranscriber, create_transcriber
from church_translator.speech_audit import SpeechAudit
from church_translator.engine import TranslationEngine, EngineSettings
from church_translator.audio import ChunkRecorder
from church_translator.realtime_store import RealtimeStore
from church_translator.diagnostic_retention import cleanup_recordings
from scripts.export_speech_diagnostic import export_segment


class FakeSocket:
    def __init__(self, texts, reverse_finals=False, drop_after=None, final_before_ack=False):
        self.texts = texts
        self.reverse_finals, self.drop_after, self.final_before_ack = reverse_finals, drop_after, final_before_ack
        self.events = queue.Queue()
        self.messages, self.audio = [], bytearray()
        self.previous = None
        self.commits = self.appends = 0
        self.closed = False
        self.held_final = None

    def send(self, wire):
        event = json.loads(wire)
        self.messages.append(event)
        if event['type'] == 'session.update':
            self.events.put({'type': 'session.updated', 'session': {'id': 'fixture', **event['session']}})
        elif event['type'] == 'input_audio_buffer.append':
            self.appends += 1
            self.audio.extend(base64.b64decode(event['audio']))
            if self.drop_after == self.appends:
                raise ConnectionError('fixture dropped after potentially receiving PCM')
            self.events.put({'type': 'conversation.item.input_audio_transcription.delta',
                             'event_id': f'partial-{self.appends}', 'item_id': f'item-{self.commits}', 'delta': 'jā '})
        elif event['type'] == 'input_audio_buffer.commit':
            item = f'item-{self.commits}'
            final = {'type': 'conversation.item.input_audio_transcription.completed',
                     'event_id': f'final-{self.commits}', 'item_id': item,
                     'transcript': self.texts[self.commits % len(self.texts)]}
            if self.final_before_ack:
                self.events.put(final)
            self.events.put({'type': 'input_audio_buffer.committed', 'event_id': f'ack-{self.commits}',
                             'item_id': item, 'previous_item_id': self.previous})
            self.previous = item
            self.commits += 1
            if not self.final_before_ack:
                if self.reverse_finals and self.commits == 1:
                    self.held_final = final
                else:
                    self.events.put(final)
                    if self.held_final:
                        self.events.put(self.held_final)
                        self.held_final = None

    def recv(self, timeout=None):
        try:
            return json.dumps(self.events.get(timeout=timeout))
        except queue.Empty:
            raise TimeoutError()

    def close(self):
        self.closed = True


class RealtimeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        env = patch.dict(os.environ, {'CHURCH_TRANSLATOR_APPDATA': temporary.name})
        env.start()
        self.addCleanup(env.stop)
        self.config = replace(load_config(), openai_api_key='fixture', google_application_credentials=None,
            openai_transcription_model='gpt-live-transcribe', gemini_api_key=None)

    def service(self, sockets):
        audit = SpeechAudit(self.root / 'take' / 'speech-audit.sqlite')
        service = RealtimeTranscriber(self.config, Glossary(), MagicMock())
        service.bind_stream(audit, MagicMock())
        service.connection_factory = MagicMock(side_effect=sockets)
        self.addCleanup(service.close_store)
        self.addCleanup(service.close)
        return service

    def feed(self, service, samples, boundary=16000, capture_id='capture'):
        origin = time.monotonic()
        captures = []
        previous = 0
        seq = 0
        for start in range(0, len(samples), 1600):
            block = samples[start:start + 1600]
            end = start + len(block)
            service.audit.capture(capture_id, start, block, {'captured_at': origin + start / 16000, 'wall_time': time.time()})
            if end - previous >= boundary or end == len(samples):
                capture = {'capture_id': capture_id, 'start_sample': previous, 'end_sample': end, 'sample_rate': 16000}
                service.register_turn(seq, capture, origin + previous / 16000)
                captures.append(capture)
                previous = end
                seq += 1
            service.feed(capture_id, start, end, {'captured_at': origin + start / 16000})
        service.seal_capture(capture_id)
        return captures

    def test_resampling_is_partition_independent_and_preserves_duration(self):
        source = np.random.default_rng(13).uniform(-.2, .2, 32001).astype('float32')
        whole = Pcm24kResampler().process(source, final=True)
        resampler = Pcm24kResampler()
        parts = [resampler.process(source[start:start + 317]) for start in range(0, len(source), 317)]
        parts.append(resampler.process([], final=True))
        self.assertEqual(b''.join(parts), whole)
        self.assertEqual(len(whole) // 2, (len(source) * 3 + 1) // 2)

    def test_factory_switches_only_the_selected_model_and_file_endpoint_rejects_live(self):
        self.assertIsInstance(create_transcriber(self.config, Glossary(), MagicMock()), RealtimeTranscriber)
        for model in ('gpt-transcribe', 'gpt-4o-mini-transcribe', 'whisper-1'):
            self.assertIsInstance(create_transcriber(replace(self.config, openai_transcription_model=model), Glossary(), MagicMock()), OpenAITranscriber)
        with self.assertRaisesRegex(RuntimeError, 'persistent Realtime'):
            OpenAITranscriber(self.config, Glossary(), MagicMock()).ensure_model()

    def test_continuous_packets_precede_commit_and_finals_match_source_order(self):
        texts = ['Mēs šodien kopā klausāmies.', 'Mēs kopā mācāmies tālāk.']
        socket = FakeSocket(texts, reverse_finals=True)
        service = self.service([socket])
        audio = (.03 * np.sin(np.arange(32000) * .1)).astype('float32')
        captures = self.feed(service, audio)
        service.ensure_model()
        results = []
        for capture in captures:
            service.current_capture = capture
            result = service.transcribe(service.audit.audio_range('capture', capture['start_sample'], capture['end_sample']))
            results.append(result.text)
            service.ack_capture(capture)
        service.finish_stream()
        service.close()
        self.assertEqual(results, texts)
        self.assertEqual(service.connection_factory.call_count, 1)
        self.assertEqual(service.connection_factory.call_args.args[0], REALTIME_URL)
        self.assertEqual(socket.commits, 2)
        self.assertGreaterEqual(socket.appends, 4)
        self.assertEqual(len(socket.audio) // 2, 48000)
        self.assertEqual(service.store.qsize(), 0)
        self.assertTrue(socket.closed)
        np.testing.assert_array_equal(service.audit.audio_range('capture', 0, 32000), audio)

    def test_final_before_commit_ack_is_bound_without_guessing_or_duplicate_source(self):
        socket = FakeSocket(['Jā, mēs turpinām.'], final_before_ack=True)
        service = self.service([socket])
        capture = self.feed(service, np.full(16000, .02, 'float32'))[0]
        service.current_capture = capture
        result = service.transcribe(np.full(16000, .02, 'float32'))
        self.assertEqual(result.text, 'Jā, mēs turpinām.')
        row = service.store.item(1, 'item-0')
        service._handle_event({'type': 'conversation.item.input_audio_transcription.completed',
            'item_id': 'item-0', 'event_id': 'different-duplicate-event', 'transcript': result.text})
        self.assertEqual(service.store.item(1, 'item-0')['final_at'], row['final_at'])
        service.ack_capture(capture)

    def test_disconnect_replays_only_unfinalized_originals_and_retains_both_attempts(self):
        broken, recovered = FakeSocket(['unused'], drop_after=1), FakeSocket(['Mēs turpinām ar cerību.'])
        service = self.service([broken, recovered])
        audio = np.full(16000, .01, 'float32')
        capture = self.feed(service, audio)[0]
        service.current_capture = capture
        result = service.transcribe(audio)
        self.assertEqual(result.text, 'Mēs turpinām ar cerību.')
        self.assertEqual(service.connection_factory.call_count, 2)
        self.assertEqual(service.last_audio_diagnostics['request_attempts'], 2)
        with service.store.condition:
            rows = service.store.db.execute('SELECT generation,sent,pcm FROM packets ORDER BY id').fetchall()
        self.assertEqual(rows[0]['generation'], 1)
        self.assertEqual(rows[0]['sent'], 0)  # Potentially sent: never pretend the socket receipt proves server delivery.
        self.assertTrue(rows[0]['pcm'])
        self.assertTrue(any(row['generation'] == 2 for row in rows))
        service.ack_capture(capture)

    def test_weak_audio_and_tiny_tail_hold_final_text_and_do_not_translate_partials(self):
        for source in (np.full(16000, 1e-5, 'float32'), np.full(1600, .01, 'float32')):
            local = self.root / str(len(source))
            audit = SpeechAudit(local / 'speech-audit.sqlite')
            socket = FakeSocket(['Mēs šodien kopā klausāmies un mācāmies.'])
            service = RealtimeTranscriber(self.config, Glossary(), MagicMock())
            service.bind_stream(audit, MagicMock())
            service.connection_factory = MagicMock(return_value=socket)
            self.addCleanup(service.close_store)
            self.addCleanup(service.close)
            capture = self.feed(service, source)[0]
            service.current_capture = capture
            service.speech_evidence = {'vad_available': True, 'probable_speech_seconds': 0,
                                      'rms_dbfs': -80, 'peak_dbfs': -55}
            result = service.transcribe(source)
            self.assertFalse(result.translation_allowed)
            self.assertFalse(result.source_visible)
            self.assertTrue(result.coverage_review)
            self.assertEqual(result.raw_text, 'Mēs šodien kopā klausāmies un mācāmies.')
            service.ack_capture(capture)

    def test_ui_preserves_all_choices_and_restores_manual_live_selection(self):
        app = QApplication.instance() or QApplication([])
        with patch('church_translator.main.list_audio_devices', return_value=[]):
            window = MainWindow()
            self.assertEqual(window.openai_model_combo.currentData(), 'gpt-4o-mini-transcribe')
            window.latvian_text.setPlainText('Saved source stays intact.')
            window.session_manager.current_session_id = 'live-ui'
            window._receive_session_update('live-ui', 'partial', ('draft', 'Unfinalized wording'))
            self.assertEqual(window.latvian_text.toPlainText(), 'Saved source stays intact.')
            self.assertIn('Unfinalized wording', window.latvian_text.toolTip())
            window.session_manager.current_session_id = None
            options = {window.openai_model_combo.itemData(i) for i in range(window.openai_model_combo.count())}
            self.assertTrue({'whisper-1', 'gpt-4o-mini-transcribe', 'gpt-4o-transcribe', 'gpt-transcribe', 'gpt-live-transcribe'} <= options)
            window.openai_model_combo.setCurrentIndex(window.openai_model_combo.findData('gpt-live-transcribe'))
            window._save_user_settings()
            window.close()
            restored = MainWindow()
            self.assertEqual(restored.openai_model_combo.currentData(), 'gpt-live-transcribe')
            self.assertEqual(restored._active_config().openai_transcription_model, 'gpt-live-transcribe')
            restored.close()

    def test_real_sdk_sends_supported_file_parameters_and_accepts_empty_languages(self):
        import httpx2
        from openai import OpenAI
        requests = []
        def endpoint(request):
            requests.append((request.url.path, request.read()))
            return httpx2.Response(200, json={'text': 'Mēs klausāmies.', 'languages': [],
                                             'usage': {'type': 'duration', 'seconds': 1.0}})
        client = OpenAI(api_key='fixture', max_retries=0, http_client=httpx2.Client(transport=httpx2.MockTransport(endpoint)))
        self.addCleanup(client.close)
        service = OpenAITranscriber(replace(self.config, openai_transcription_model='gpt-transcribe',
                                           stt_max_recovery_requests=0), Glossary(), MagicMock())
        service._client = client
        result = service.transcribe(np.full(16000, .01, 'float32'), prepared_context={'vocabulary': 'Pāvils; cerība'})
        self.assertEqual(result.text, 'Mēs klausāmies.')
        path, body = requests[0]
        self.assertEqual(path, '/v1/audio/transcriptions')
        self.assertIn(b'name="languages[]"', body)
        self.assertIn(b'name="keywords[]"', body)
        self.assertIn(b'filename="chunk.wav"', body)
        for forbidden in (b'name="language"', b'name="temperature"', b'name="include[]"', b'name="timestamp_granularities[]"'):
            self.assertNotIn(forbidden, body)

    def test_rotation_at_drained_boundary_preserves_both_capture_ids(self):
        sockets = [FakeSocket(['Pirmā doma.']), FakeSocket(['Otrā doma.'])]
        service = self.service(sockets)
        clock = [0]
        service.session_clock = lambda: clock[0]
        first = self.feed(service, np.full(16000, .01, 'float32'), capture_id='before-pause')[0]
        service.current_capture = first
        self.assertEqual(service.transcribe(np.full(16000, .01, 'float32')).text, 'Pirmā doma.')
        service.ack_capture(first)
        clock[0] = 3301
        second = self.feed(service, np.full(16000, .01, 'float32'), capture_id='after-resume')[0]
        service.current_capture = second
        self.assertEqual(service.transcribe(np.full(16000, .01, 'float32')).text, 'Otrā doma.')
        service.ack_capture(second)
        self.assertEqual(service.connection_factory.call_count, 2)
        self.assertTrue(sockets[0].closed)
        self.assertEqual(service.store.qsize(), 0)

    def test_identical_deltas_are_not_text_deduplicated_and_stale_generation_is_ignored(self):
        service = self.service([])
        service._generation = 2
        event = {'type': 'conversation.item.input_audio_transcription.delta', 'item_id': 'draft', 'delta': 'jā '}
        service._handle_event({**event, 'event_id': 'one'})
        service._handle_event({**event, 'event_id': 'one'})
        service._handle_event({**event, 'event_id': 'two'})
        service._handle_event({**event, 'event_id': 'retired'}, generation=1)
        self.assertEqual(service._partials['draft'][0], 'jā jā ')
        self.assertIsNone(service.store.item(2, 'draft'))

    def test_quota_failure_opens_circuit_and_preserves_future_audio_without_retries(self):
        class QuotaSocket(FakeSocket):
            def send(self, wire):
                self.events.put({'type': 'error', 'error': {'code': 'credit_balance_exhausted', 'message': 'no credits'}})
        service = self.service([QuotaSocket(['unused'])])
        audio = np.full(16000, .01, 'float32')
        first = self.feed(service, audio, capture_id='first')[0]
        service.current_capture = first
        result = service.transcribe(audio)
        self.assertFalse(result.translation_allowed)
        self.assertIn('account/model action', result.confidence_note)
        second = self.feed(service, audio, capture_id='second')[0]
        service.current_capture = second
        self.assertFalse(service.transcribe(audio).translation_allowed)
        self.assertEqual(service.connection_factory.call_count, 1)
        self.assertEqual(service.store.qsize(), 2)  # Unacknowledged, durable review work.
        self.assertEqual(service.store.usage()['potentially_submitted_seconds'], 0)

    def test_exhausted_replay_budget_and_timeout_keep_originals_and_work(self):
        service = self.service([FakeSocket(['unused'], drop_after=1), FakeSocket(['unused'], drop_after=1)])
        audio = np.full(16000, .01, 'float32')
        capture = self.feed(service, audio)[0]
        service.current_capture = capture
        result = service.transcribe(audio)
        self.assertFalse(result.translation_allowed)
        self.assertEqual(service.last_audio_diagnostics['request_attempts'], 2)
        self.assertEqual(service.store.qsize(), 1)
        np.testing.assert_array_equal(service.audit.audio_range('capture', 0, 16000), audio)
        other = self.feed(service, audio, capture_id='timeout')[0]
        row = service.store.result(other, timeout=0)
        self.assertEqual(row['state'], 'failed')
        self.assertIn('timed out', row['error'])

    def test_restart_and_conflicting_final_keep_review_and_exact_exported_packets(self):
        service = self.service([])
        audio = np.full(16000, .01, 'float32')
        capture = self.feed(service, audio)[0]
        service.audit.event('captured_segment', 0, {'capture': capture})
        row = service.store.next_turn()
        service.store.begin(row['id'], 1)
        pcm = Pcm24kResampler().process(audio, final=True)
        service.store.packet(row['id'], 1, 0, 16000, pcm, {'pcm_sha256': hashlib.sha256(pcm).hexdigest()})
        service.store.complete(row['id'], 'Jā.')
        service.store.complete(row['id'], 'Nē.')
        self.assertEqual(service.store.result(capture)['state'], 'failed')
        reopened = RealtimeStore(service.store.path)
        try:
            self.assertEqual(reopened.qsize(), 1)
            self.assertIsNone(reopened.next_turn())  # No automatic startup purchase/replay.
        finally:
            reopened.close()
        destination = self.root / 'diagnostic.zip'
        export_segment(service.audit.path, destination, segment=0)
        import soundfile as sf
        with zipfile.ZipFile(destination) as bundle:
            self.assertIsNone(bundle.testzip())
            manifest = json.loads(bundle.read('realtime.json'))
            actual, rate = sf.read(io.BytesIO(bundle.read(manifest['packets'][0]['audio_file'])), dtype='int16')
            self.assertEqual(rate, 24000)
            self.assertEqual(actual.astype('<i2').tobytes(), pcm)
            self.assertEqual(manifest['turns'][0]['state'], 'failed')

    def test_retention_protects_realtime_work_then_expires_drained_transport_and_originals(self):
        service = self.service([])
        audio = np.full(16000, .01, 'float32')
        capture = self.feed(service, audio)[0]
        service.audit.event('captured_segment', 0, {'capture': capture})
        service.audit.event('completion', None, {})
        session = service.audit.path.parent
        snapshot = dict.fromkeys(('capture_pending','translation_pending','synthesis_pending','playback_pending',
            'audio_review_items','yoruba_audio_pending','translation_review_items','realtime_pending'), 0)
        (session / 'integrity.json').write_text(json.dumps(snapshot))
        now = time.time() + 337 * 3600
        self.assertEqual(cleanup_recordings(session.parent, now=now)['deleted'], 0)
        row = service.store.next_turn()
        pcm = b'\0\0' * 24000
        service.store.packet(row['id'], 1, 0, 16000, pcm, {})
        service.store.complete(row['id'], 'Jā.')
        service.ack_capture(capture)
        self.assertEqual(cleanup_recordings(session.parent, now=now)['deleted'], 1)
        self.assertIsNone(service.store.db.execute('SELECT pcm FROM packets').fetchone()[0])
        self.assertEqual(service.store.db.execute('SELECT text FROM turns').fetchone()[0], 'Jā.')

    def test_supported_very_quiet_repetition_remains_eligible(self):
        text = 'Mēs šodien kopā mācāmies un klausāmies.'
        service = self.service([FakeSocket([text])])
        audio = np.full(40000, 1e-5, 'float32')
        capture = self.feed(service, audio, boundary=40000)[0]
        service.current_capture = capture
        service.speech_evidence = {'vad_available': True, 'probable_speech_seconds': 2.5,
                                  'rms_dbfs': -100, 'peak_dbfs': -100}
        result = service.transcribe(audio, previous_context=text)
        self.assertTrue(result.translation_allowed)
        self.assertEqual(result.text, text)
        service.ack_capture(capture)

    def test_tiny_genuine_word_uses_padding_but_never_reference_hints(self):
        service = self.service([FakeSocket(['Jā.'])])
        service.context_provider = lambda: ('Earlier words never become this word.', {'vocabulary': 'Pāvils'})
        audio = np.full(800, .01, 'float32')
        capture = self.feed(service, audio)[0]
        service.current_capture = capture
        with patch('church_translator.realtime_transcription.audio_evidence', return_value={
                'use_context': True, 'allow_gain': False}):
            result = service.transcribe(audio)
        self.assertTrue(result.translation_allowed)
        self.assertEqual(result.text, 'Jā.')
        self.assertEqual(service.last_audio_diagnostics['source_samples'], 800)
        self.assertEqual(service.last_audio_diagnostics['submitted_samples'], 2400)
        metadata = json.loads(service.store.db.execute('SELECT metadata FROM packets').fetchone()[0])
        self.assertEqual(metadata['padding_samples'], 1200)
        self.assertEqual(metadata['prompt'], '')
        self.assertEqual(metadata['keywords'], [])
        self.assertTrue(metadata['short_window_references_omitted'])
        service.ack_capture(capture)

    def test_full_engine_drains_realtime_tail_before_translation_and_playback_completion(self):
        source, translated, spoken, errors = [], [], [], []
        ready = threading.Event()
        engine = TranslationEngine(self.config, EngineSettings(0, True, False, 0, None, lambda: 0, lambda: 0),
            lambda _: None, errors.append, lambda _: None, source.append, lambda lang, text: translated.append(text))
        engine._transcriber.connection_factory = MagicMock(return_value=FakeSocket(['Mēs kopā klausāmies.']))
        engine._translator.translate_joint = MagicMock(return_value={'en': 'We listen together.'})
        engine._tts = MagicMock()
        engine._tts.synthesize.return_value = b'fixture'
        class Player:
            def __init__(self, *args, **kwargs):
                self.volume_getters = {}
                self.pending_at_stop = 0
            def start(self): pass
            def enqueue(self, audio, **kwargs): spoken.append(audio)
            def stop(self): pass
        def start(recorder):
            ready.set()
        with patch('church_translator.engine.OrderedAudioPlayer', Player), patch.object(ChunkRecorder, 'start', start):
            engine.start()
            self.assertTrue(ready.wait(2))
            # A short tail emitted only by Stop still has exact journal ownership.
            recorder = engine._recorder
            audio = (.03 * np.sin(np.arange(19200) * .13)).astype('float32')
            for offset in range(0, len(audio), 1600):
                block = audio[offset:offset + 1600]
                details = {'captured_at': time.monotonic(), 'wall_time': time.time()}
                engine._audit.capture(recorder.capture_id, offset, block, details)
                recorder._received_samples += len(block)
                recorder._consume(block, offset, details)
            engine.pause()
            deadline = time.monotonic() + 5
            while len(spoken) < 1 and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertEqual(len(spoken), 1)
            engine.resume()
            resumed = engine._recorder
            self.assertNotEqual(recorder.capture_id, resumed.capture_id)
            for offset in range(0, len(audio), 1600):
                block = audio[offset:offset + 1600]
                details = {'captured_at': time.monotonic(), 'wall_time': time.time()}
                engine._audit.capture(resumed.capture_id, offset, block, details)
                resumed._received_samples += len(block)
                resumed._consume(block, offset, details)
            engine.stop()
        self.assertEqual(errors, [])
        self.assertEqual(source, ['Mēs kopā klausāmies.'] * 2)
        self.assertEqual(translated, ['We listen together.'] * 2)
        self.assertEqual(spoken, [b'fixture'] * 2)
        self.assertEqual(engine._transcriber.connection_factory.call_count, 1)
        self.assertEqual(engine.completion_info['realtime_pending'], 0)
        self.assertEqual(engine.completion_info['capture_pending'], 0)
        self.assertEqual(engine.completion_info['playback_pending'], 0)
        self.assertEqual(engine.integrity_report['uncovered_ranges'], [])


if __name__ == '__main__':
    unittest.main()
