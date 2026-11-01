"""Independent language timing, PCM integrity and durable shared-device recovery."""
from dataclasses import replace
import io
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

import numpy as np
import av
import soundfile as sf

from church_translator.audio_mixer import MixedAudioPlayer, prepare_pcm
from church_translator.backlog import DurableQueue
from church_translator.config import load_config
from church_translator.engine import EngineSettings, TranslationEngine
from scripts.verify_shared_mixer import MixerSimulation, fixture, two_hour_service


class MixerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.starts, self.completed, self.errors = [], [], []

    def player(self, path=None, **kwargs):
        return MixedAudioPlayer('en', 0, lambda: 1, self.errors.append, path or self.root / 'playback-en.sqlite',
            on_started=self.starts.append, on_completed=self.completed.append,
            languages=('en', 'yo'), samplerate=16000, channels=2, **kwargs)

    def test_simultaneous_languages_and_no_overlap_within_each_language(self):
        player = self.player()
        player.enqueue(fixture(.03, .25), 'en', sequence_id=0)
        player.enqueue(fixture(.03, .5), 'en', sequence_id=1)
        player.enqueue(fixture(.06, .125), 'yo', sequence_id=0)
        sim = MixerSimulation()
        sim.drive(player)
        self.assertFalse(self.errors)
        output = np.concatenate(sim.blocks)
        np.testing.assert_array_equal(output[:480], np.full((480, 2), .1875, np.float32))
        np.testing.assert_array_equal(output[480:], np.full((480, 2), .3125, np.float32))
        en = [d for d in self.starts if d['language'] == 'en']
        self.assertEqual([d['start_frame'] for d in en], [0, 480])
        self.assertEqual([d['end_frame'] for d in self.completed if d['language'] == 'en'], [480, 960])
        self.assertEqual([d['start_frame'] for d in self.starts if d['language'] == 'yo'], [0])
        self.assertEqual(player.overlap_frames, 960)
        self.assertEqual(en[1]['gap_seconds'], 0)
        self.assertEqual((sim.starts, sim.stops), (1, 1))
        player.stop()

    def test_independent_volumes_and_full_scale_headroom(self):
        player = self.player()
        player.volume_getters = {'en': lambda: .2, 'yo': lambda: .8}
        for lang, value in (('en', .25), ('yo', .5)):
            player.enqueue(fixture(.04, value), lang, sequence_id=0)
        sim = MixerSimulation()
        sim.drive(player)
        np.testing.assert_allclose(np.concatenate(sim.blocks), .225, atol=1e-7)
        player.stop()
        player = self.player(self.root / 'second' / 'playback-en.sqlite')
        for lang in ('en', 'yo'):
            player.enqueue(fixture(.04, 1), lang, sequence_id=0)
        sim = MixerSimulation()
        sim.drive(player)
        np.testing.assert_allclose(np.concatenate(sim.blocks), 32767 / 32768, atol=1e-7)
        self.assertLessEqual(player.peak_output, 1)
        player.stop()

    def test_sample_rates_and_channel_conversion_preserve_duration_and_stereo(self):
        audio = io.BytesIO()
        stereo = np.tile([.25, -.25], (960, 1)).astype(np.float32)
        sf.write(audio, stereo, 24000, format='WAV', subtype='FLOAT')
        prepared, rate, channels, gain = prepare_pcm(audio.getvalue(), 16000, 2)
        self.assertEqual(prepared.shape, (640, 2))
        np.testing.assert_allclose(prepared[30:-30], np.tile([.25, -.25], (580, 1)), atol=1e-6)
        self.assertEqual((rate, channels, gain), (24000, 2, 1))
        mono, *_ = prepare_pcm(audio.getvalue(), 16000, 1)
        np.testing.assert_array_equal(mono, np.zeros((640, 1)))
        stereo, *_ = prepare_pcm(fixture(.04, .25), 48000, 2)
        self.assertEqual(stereo.shape, (1920, 2))
        np.testing.assert_allclose(stereo[50:-50], .25, atol=1e-5)

    def test_malformed_earlier_audio_holds_only_its_own_language(self):
        player = self.player()
        player.enqueue(fixture(.04, .25), 'en', sequence_id=0)
        player.enqueue(b'bad encoded audio', 'en', sequence_id=1)
        player.enqueue(fixture(.04, .5), 'en', sequence_id=2)
        for seq in range(3):
            player.enqueue(fixture(.04, .125), 'yo', sequence_id=seq)
        sim = MixerSimulation()
        sim.drive(player)
        self.assertEqual([d['sequence_id'] for d in self.starts if d['language'] == 'en'], [0])
        self.assertEqual([d['sequence_id'] for d in self.starts if d['language'] == 'yo'], [0, 1, 2])
        self.assertEqual(player._lanes['en'].queue.qsize(), 2)
        self.assertEqual(player._lanes['yo'].queue.qsize(), 0)
        self.assertTrue(any('other languages continue' in e for e in self.errors))
        player.stop()

    def test_output_failure_retains_originals_and_explicit_recovery_is_fifo(self):
        path = self.root / 'playback-en.sqlite'
        audio = fixture(.1, .25)
        player = self.player(path)
        for seq in range(3):
            for lang in ('en', 'yo'):
                player.enqueue(audio, lang, sequence_id=seq)
        sim = MixerSimulation(fail_after=1)
        sim.drive(player)
        player.stop()
        self.assertEqual(player.pending_at_stop, 6)
        self.assertFalse(self.completed)
        original = DurableQueue(path)
        self.assertEqual(original.get()['audio'], audio)
        original.close()
        self.starts.clear()
        self.errors.clear()
        recovered = self.player(path)
        recovered.enqueue(audio, 'en', sequence_id=0)  # Stable receipt prevents duplication.
        MixerSimulation().drive(recovered)
        self.assertFalse(self.errors)
        for lang in ('en', 'yo'):
            self.assertEqual([d['sequence_id'] for d in self.starts if d['language'] == lang], [0, 1, 2])
        recovered.stop()
        self.assertEqual(recovered.pending_at_stop, 0)

    def test_decoder_block_in_either_language_cannot_hold_other_language(self):
        for blocked in ('en', 'yo'):
            with self.subTest(blocked=blocked):
                self.starts.clear()
                entered, release, other_started = threading.Event(), threading.Event(), threading.Event()
                player = self.player(self.root / blocked / 'playback-en.sqlite')
                player.on_started = lambda d: (self.starts.append(d), other_started.set() if d['language'] != blocked else None)
                encoded = {lang: fixture(.06, .25 if lang == 'en' else .125) for lang in ('en', 'yo')}
                def decode(audio, *args):
                    if audio == encoded[blocked]:
                        entered.set()
                        if not release.wait(3): raise RuntimeError('Decoder gate expired')
                    return prepare_pcm(audio, *args)
                class Stream:
                    latency = .02
                    @property
                    def time(self): return time.monotonic()
                    def __enter__(self): return self
                    def __exit__(self, *args): pass
                    def stop(self): pass
                    def write(self, data):
                        time.sleep(.001)
                        return False
                with patch('church_translator.audio_mixer.prepare_pcm', side_effect=decode), \
                     patch('church_translator.audio_mixer.sd.OutputStream', return_value=Stream()):
                    for lang in ('en', 'yo'):
                        player.enqueue(encoded[lang], lang, sequence_id=0)
                    player.start()
                    try:
                        self.assertTrue(entered.wait(1))
                        self.assertTrue(other_started.wait(1), 'Other language waited for blocked decoder')
                        self.assertFalse(any(d['language'] == blocked for d in self.starts))
                    finally:
                        release.set()
                        player.stop()
                self.assertEqual(player.pending_at_stop, 0)
        self.assertFalse(self.errors)

    def test_long_ready_service_preserves_every_sentence_and_zero_lane_gaps(self):
        report = two_hour_service(60)
        self.assertEqual(report['completed_sentences'], 20)
        self.assertEqual(report['maximum_same_language_ready_gap_seconds'], 0)

    def test_end_session_drains_short_tails_and_original_queues(self):
        player = self.player()
        for seq, seconds in enumerate((.003, .017, .041)):
            player.enqueue(fixture(seconds, .25), 'en', sequence_id=seq)
        player.enqueue(fixture(.079, .125), 'yo', sequence_id=0)
        sim = MixerSimulation()
        sim.drive(player)
        english = [d for d in self.completed if d['language'] == 'en']
        self.assertEqual([d['end_frame'] - d['start_frame'] for d in english], [48, 272, 656])
        self.assertEqual([d['start_frame'] for d in english], [0, 48, 320])
        player.stop()
        self.assertEqual(player.pending_at_stop, 0)

    def test_nonfinite_pcm_is_retained_and_multichannel_conversion_is_safe(self):
        audio = io.BytesIO()
        with av.open(audio, mode='w', format='wav') as container:
            stream = container.add_stream('pcm_s16le', rate=16000, layout='quad')
            frame = av.AudioFrame.from_ndarray(np.full((4, 320), .25, np.float32), format='fltp', layout='quad')
            frame.sample_rate = 16000
            for packet in [*stream.encode(frame), *stream.encode(None)]:
                container.mux(packet)
        converted, *_ = prepare_pcm(audio.getvalue(), 48000, 2)
        self.assertEqual(converted.shape, (960, 2))
        self.assertTrue(np.isfinite(converted).all())
        self.assertLessEqual(np.max(np.abs(converted)), 1)
        audio = io.BytesIO()
        sf.write(audio, np.array([.1, float('nan')], np.float32), 16000, format='WAV', subtype='FLOAT')
        with self.assertRaisesRegex(ValueError, 'non-finite'):
            prepare_pcm(audio.getvalue(), 16000, 2)

    def test_blocked_reporting_cannot_stall_mixed_output(self):
        player = self.player()
        entered, release = threading.Event(), threading.Event()
        def callback(details):
            entered.set()
            release.wait(3)
            self.starts.append(details)
        player.on_started = callback
        class Stream:
            latency = 0
            time = None
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def write(self, data):
                time.sleep(.001)
                return False
            def stop(self): pass
        for seq in range(3):
            for lang in ('en', 'yo'):
                player.enqueue(fixture(.04, .25), lang, sequence_id=seq)
        player.finish()
        with patch('church_translator.audio_mixer.sd.OutputStream', return_value=Stream()):
            player.start()
            try:
                self.assertTrue(entered.wait(1))
                player._thread.join(1)
                self.assertFalse(player._thread.is_alive(), 'Audio writer waited for reporting')
            finally:
                release.set()
                player.stop()
        self.assertFalse(self.errors)
        self.assertEqual(len(self.starts), 6)
        self.assertEqual(player.pending_at_stop, 0)

    def test_engine_uses_one_mixer_for_shared_output_and_independent_other_outputs(self):
        for shared in (False, True):
            created = []
            class Player:
                pending_at_stop = 0
                def __init__(self, *args, **kwargs):
                    self.volume_getters = {}
                    created.append(kwargs.get('languages'))
                def start(self): pass
                def stop(self): pass
            cfg = replace(load_config(), gemini_api_key=None, google_application_credentials=None)
            with patch('church_translator.engine.app_data_dir', return_value=self.root / str(shared)), \
                 patch('church_translator.engine.MixedAudioPlayer', Player), \
                 patch('church_translator.engine.OrderedAudioPlayer', Player):
                engine = TranslationEngine(cfg, EngineSettings(0, True, True, 3, 3 if shared else 4,
                    lambda: 1, lambda: 1, secondary_language='yo'), *[MagicMock() for _ in range(5)])
                with patch.object(engine, '_process_loop'), patch.object(engine, '_translation_loop'), patch.object(engine, '_speech_loop'):
                    engine.start()
                    self.assertEqual(engine._players['en'] is engine._players['yo'], shared)
                    engine.stop()
            self.assertEqual(created, [('en', 'yo')] if shared else [None, None])


if __name__ == '__main__':
    unittest.main()
