"""Scheduling assertions against real queues and a finite-buffer output model."""
from dataclasses import replace
import io
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

import numpy as np
import soundfile as sf

from church_translator.audio import OrderedAudioPlayer
from church_translator.backlog import DurableQueue
from church_translator.config import load_config
from church_translator.engine import EngineSettings, TranslationEngine
from church_translator.services import TextToSpeech, Translator
from scripts.verify_playback_continuity import OutputSimulation, simulate, wav


class TtsContinuityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.addCleanup(patch.stopall)
        patch('church_translator.engine.app_data_dir', return_value=self.root).start()

    def engine(self):
        cfg = replace(load_config(), gemini_api_key=None, google_application_credentials=None)
        engine = TranslationEngine(cfg, EngineSettings(0, True, True, 0, 0, lambda: 1, lambda: 1,
                                   secondary_language='yo'), *[MagicMock() for _ in range(5)])
        for q in (engine._chunks, engine._translations, engine._speech, engine._unrecognized):
            self.addCleanup(q.close)
        engine._language_speech = {lang: DurableQueue(engine.session_dir / f'synthesis-{lang}.sqlite')
                                   for lang in ('en', 'yo')}
        for q in engine._language_speech.values():
            self.addCleanup(q.close)
        return engine

    def test_ready_audio_never_restarts_device_or_inserts_five_second_gap(self):
        result = simulate(restart_seconds=5)
        self.assertLess(result['maximum_ready_gap_seconds'], .2)
        self.assertEqual(result['stream_starts'], 1)
        self.assertEqual(result['stream_drains'], 1)

    def test_synthesis_and_admission_never_wait_for_other_language(self):
        engine = self.engine()
        prepared = threading.Event()
        requests, admitted = [], []
        class Voice:
            _client = None
            def __init__(self, config, **kw):
                self.config = config
            def synthesize(self, text, lang):
                requests.append((text, lang))
                if text == 'en2':
                    prepared.set()
                if text == 'yo0' and not prepared.wait(3):
                    raise RuntimeError('English pre-synthesis blocked by shared order')
                return b'audio'
        shared = MagicMock()
        shared.enqueue.side_effect = lambda audio, **kw: admitted.append((kw['sequence_id'], kw['language']))
        engine._players = dict(en=shared, yo=shared)
        for seq in range(4):
            engine._speech.put(dict(sequence_id=seq, captured_at=time.monotonic(), completed=[],
                                   translations=dict(en=f'en{seq}', yo=f'yo{seq}')))
        engine._speech.finish()
        with patch('church_translator.engine.TextToSpeech', Voice):
            engine._speech_loop()
        for q in engine._ready_speech.values():
            self.addCleanup(q.close)
        self.assertTrue(prepared.is_set())
        for language in ('en', 'yo'):
            self.assertEqual([seq for seq, lang in admitted if lang == language], list(range(4)))
        self.assertEqual(len(requests), 8)
        self.assertEqual(len(set(requests)), 8)
        self.assertTrue(all(q.qsize() == 0 for q in engine._ready_speech.values()))

    def test_claimed_output_failure_survives_restart_and_fifo_receipts(self):
        q = DurableQueue(self.root / 'claims.sqlite')
        q.put({'n': 0})
        q.put({'n': 1})
        first, second = q.claim(), q.claim()
        with self.assertRaises(RuntimeError):
            q.ack_claim(second[0])
        q.close()
        recovered = DurableQueue(self.root / 'claims.sqlite')
        self.assertEqual(recovered.get(), {'n': 0})
        recovered.ack()
        self.assertEqual(recovered.get(), {'n': 1})
        recovered.ack()
        recovered.close()

    def test_optional_ready_queue_lock_cannot_hold_english_backpressure_condition(self):
        engine = self.engine()
        optional_blocked, english_second = threading.Event(), threading.Event()
        created = []
        def queue_factory(path, *args, **kwargs):
            work = DurableQueue(path, *args, **kwargs)
            created.append(work)
            if path.name == 'synthesis-ready-yo.sqlite':
                original_size = work.qsize
                def stalled_size():
                    optional_blocked.set()
                    if not english_second.wait(2):
                        raise RuntimeError('English waited for optional queue lock')
                    return original_size()
                work.qsize = stalled_size
            return work
        class Voice:
            _client = None
            def __init__(self, config, **kwargs): self.config = config
            def synthesize(self, text, lang):
                if lang == 'en':
                    if not optional_blocked.wait(2): raise RuntimeError('Optional gate did not start')
                    if text == 'English 1': english_second.set()
                return b'fixture'
        player = MagicMock()
        engine._players = {'en': player, 'yo': player}
        for seq in range(2):
            engine._speech.put({'sequence_id': seq, 'captured_at': time.monotonic(),
                'translations': {'en': f'English {seq}', 'yo': f'Yoruba {seq}'}, 'completed': []})
        engine._speech.finish()
        with patch('church_translator.engine.DurableQueue', side_effect=queue_factory), \
             patch('church_translator.engine.TextToSpeech', Voice):
            engine._speech_loop()
        for work in created:
            self.addCleanup(work.close)
        self.assertTrue(english_second.is_set())
        self.assertEqual(player.enqueue.call_count, 4)

    def test_admission_failure_retains_synthesized_audio_without_resynthesizing(self):
        engine = self.engine()
        ready = DurableQueue(self.root / 'ready.sqlite')
        ready.put({'sequence_id': 0, 'audio': b'original-encoded-audio',
                   'timing': {'audio_ready_at': time.monotonic()}})
        ready.finish()
        player = MagicMock()
        player.enqueue.side_effect = OSError('output queue storage unavailable')
        engine._players = {'en': player}
        engine._admit_speech('en', ready)
        self.assertEqual(ready.qsize(), 1)
        ready.close()
        recovered = DurableQueue(self.root / 'ready.sqlite')
        recovered.finish()
        player.enqueue.side_effect = None
        engine._admit_speech('en', recovered)
        self.assertEqual(recovered.qsize(), 0)
        self.assertEqual(player.enqueue.call_args.args[0], b'original-encoded-audio')
        recovered.close()

    def test_embedded_padding_is_measured_without_cutting_any_samples(self):
        audio, samples = wav(padding=800)
        sim = OutputSimulation()
        reports = []
        player = OrderedAudioPlayer('en', 0, lambda: 1, self.fail, self.root / 'padding.sqlite', on_started=reports.append)
        with patch('church_translator.audio.sd.OutputStream', sim.output):
            player.enqueue(audio, sequence_id=0)
            player._queue.finish()
            player._run()
        self.assertAlmostEqual(reports[0]['leading_digital_zero_seconds'], .05)
        self.assertAlmostEqual(reports[0]['trailing_digital_zero_seconds'], .05)
        np.testing.assert_array_equal(np.concatenate([w[2][:, 0] for w in sim.writes]), samples)
        player._queue.close()

    def test_google_preparation_makes_no_synthesis_or_token_request(self):
        cfg = replace(load_config(), google_application_credentials='synthetic.json')
        tts = TextToSpeech(cfg)
        with patch('google.oauth2.service_account.Credentials.from_service_account_file'), \
             patch('church_translator.services.texttospeech.TextToSpeechClient') as client:
            tts.prepare('en')
            tts.prepare('en')
            client.assert_called_once()
            client.return_value.synthesize_speech.assert_not_called()

    def test_delayed_or_failed_initial_preparation_does_not_lose_first_english_job(self):
        engine = self.engine()
        engine._language_speech = {}
        engine._players = {'en': MagicMock()}
        engine._speech.put({'sequence_id': 0, 'captured_at': time.monotonic(),
                           'translations': {'en': 'Hope.'}, 'completed': []})
        engine._speech.finish()
        def delayed_prepare(*args):
            time.sleep(.02)
            raise RuntimeError('Synthetic preparation failure; normal synthesis may recover')
        with patch.object(TextToSpeech, 'prepare', side_effect=delayed_prepare), \
             patch.object(TextToSpeech, 'synthesize', return_value=b'fixture') as synthesize:
            engine._speech_loop()
        synthesize.assert_called_once_with('Hope.', 'en')
        engine._players['en'].enqueue.assert_called_once()
        self.assertEqual(engine._speech.qsize(), 0)

    def test_slow_provider_is_timed_separately_from_client_initialization(self):
        tts = TextToSpeech(replace(load_config(), google_application_credentials='synthetic.json'))
        client = MagicMock()
        def slow_provider(**kwargs):
            time.sleep(.02)
            return type('Response', (), {'audio_content': b'audio'})()
        client.synthesize_speech.side_effect = slow_provider
        tts._client = client
        self.assertEqual(tts.synthesize('Hope.', 'en', strict=True), b'audio')
        timing = tts.last_timing
        self.assertGreaterEqual(timing['tts_response_at'] - timing['tts_request_started_at'], .015)
        self.assertEqual(len(timing['tts_attempts']), 1)
        self.assertLess(timing['tts_client_init_seconds'], .01)

    def test_cloud_and_local_sample_rates_share_one_continuous_stream(self):
        sim = OutputSimulation(restart_seconds=5)
        reports = []
        player = OrderedAudioPlayer('en', 0, lambda: 1, self.fail, self.root / 'rates.sqlite', on_started=reports.append)
        for seq, rate in enumerate((24000, 16000, 24000, 16000)):
            audio, _ = wav(.3, rate=rate)
            player.enqueue(audio, 'en' if seq % 2 == 0 else 'yo', sequence_id=seq)
        player._queue.finish()
        with patch('church_translator.audio.time.monotonic', lambda: sim.now), \
             patch('church_translator.audio.sd.OutputStream', sim.output):
            player._run()
        self.assertEqual(len(sim.streams), 1)
        self.assertEqual(sim.streams[0].starts, 1)
        self.assertEqual(sum(len(w[2]) for w in sim.writes), 28800)
        self.assertTrue(all(r['output_sample_rate'] == 24000 for r in reports))
        self.assertTrue(all(r['gap_seconds'] < .2 for r in reports[1:]))
        player._queue.close()

    def test_wall_clock_ready_transition_is_under_200ms(self):
        # Real thread + real SQLite commits + real decoding; only the output
        # device is mocked. A write blocks briefly, as a finite buffer does.
        starts, errors = [], []
        audio, _ = wav(.3)
        player = OrderedAudioPlayer('en', 0, lambda: 1, errors.append, self.root / 'wall.sqlite')
        self.addCleanup(player._queue.close)
        class Stream:
            latency = 0
            time = None
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def start(self): raise AssertionError('Unnecessary restart')
            def stop(self): pass
            def write(self, data):
                starts.append(time.monotonic())
                time.sleep(.005)
                return False
        for seq in range(20):
            player.enqueue(audio, sequence_id=seq)
        player._queue.finish()
        with patch('church_translator.audio.sd.OutputStream', return_value=Stream()):
            player._run()
        self.assertFalse(errors)
        gaps = [starts[i] - starts[i - 1] - .005 for i in range(3, len(starts), 3)]
        self.assertLess(max(gaps), .2)

    def test_blocked_audit_callback_cannot_stall_ready_audio_writes(self):
        engine = self.engine()
        entered, release = threading.Event(), threading.Event()
        original_event = engine._audit.event
        def slow_audit(kind, *args):
            if kind == 'playback_started':
                entered.set()
                if not release.wait(3):
                    raise RuntimeError('Synthetic audit gate expired')
            return original_event(kind, *args)
        engine._audit.event = slow_audit
        errors, writes = [], []
        player = OrderedAudioPlayer('en', 0, lambda: 1, errors.append, self.root / 'audit-player.sqlite',
                                    on_started=engine._on_playback_started, on_completed=engine._on_playback_completed)
        class Stream:
            latency = 0
            time = None
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def stop(self): pass
            def start(self): raise AssertionError('Unnecessary restart')
            def write(self, data):
                writes.append(data.copy())
                time.sleep(.005)
                return False
        audio, _ = wav(.3)
        for seq in range(4):
            player.enqueue(audio, sequence_id=seq)
        player._queue.finish()
        engine._start_diagnostics()
        try:
            with patch('church_translator.audio.sd.OutputStream', return_value=Stream()):
                player.start()
                self.assertTrue(entered.wait(1))
                player._thread.join(1)
                self.assertFalse(player._thread.is_alive(), 'Audio writer waited for audit SQLite work')
                self.assertEqual(len(writes), 12)
        finally:
            release.set()
            player._thread.join(3)
            engine._stop_diagnostics()
            player._queue.close()
        self.assertFalse(errors)
        self.assertEqual(engine._diagnostic_error_count, 0)


if __name__ == '__main__':
    unittest.main()
