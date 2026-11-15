"""Production PCM, lifecycle and duration regressions; no external providers."""
from dataclasses import replace
import io
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

import numpy as np
import soundfile as sf
import sounddevice as sd

from church_translator.audio_mixer import MixedAudioPlayer
from church_translator.audio import OrderedAudioPlayer
from church_translator.audio_output import resolve_output, negotiate_output, open_output
from church_translator.playback_policy import DurationController, stretch_pcm
from church_translator.engine import TranslationEngine, EngineSettings, ProcessingItem
from church_translator.config import load_config
from church_translator import yoruba_tts
from scripts.verify_shared_mixer import MixerSimulation, fixture


class PlaybackManagementTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.env = patch.dict(os.environ, {'CHURCH_TRANSLATOR_APPDATA': folder.name})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_default_and_mapper_alias_resolve_to_same_endpoint(self):
        raw = {'name': 'Microsoft Sound Mapper - Output', 'hostapi': 0}
        with patch('church_translator.audio_output.sd.default.device', [-1, 9]), \
             patch('church_translator.audio_output.sd.query_devices', return_value=raw), \
             patch('church_translator.audio_output.sd.query_hostapis', return_value={'default_output_device': 10}):
            self.assertEqual(resolve_output(None), 10)
            self.assertEqual(resolve_output(9), 10)

    def test_native_format_probes_mono_when_stereo_is_rejected(self):
        info = {'native_sample_rate': 48000, 'maximum_output_channels': 48}
        with patch('church_translator.audio_output.output_description', return_value=info), \
             patch('church_translator.audio_output.sd.check_output_settings', side_effect=[sd.PortAudioError('stereo rejected'), None]) as probe:
            self.assertEqual(negotiate_output(17), (48000, 1, info))
            self.assertEqual([c.kwargs['channels'] for c in probe.call_args_list], [2, 1])
            self.assertTrue(all(c.kwargs['device'] == 17 for c in probe.call_args_list))

    def test_initialization_retries_are_bounded_and_partial_writes_never_retry(self):
        stream = MagicMock()
        stream.__enter__.return_value = stream
        with patch('church_translator.audio_output.sd.OutputStream', side_effect=[OSError('busy'), stream]) as create, \
             patch('church_translator.audio_output.time.sleep'):
            with open_output(lambda _: None, device=17) as opened:
                self.assertIs(opened, stream)
            self.assertEqual(create.call_count, 2)
        with patch('church_translator.audio_output.sd.OutputStream', return_value=stream) as create:
            with self.assertRaisesRegex(OSError, 'disconnect'):
                with open_output(lambda _: None, device=17):
                    raise OSError('disconnect during write')
            self.assertEqual(create.call_count, 1)
        with patch('church_translator.audio_output.sd.OutputStream', side_effect=OSError('offline')) as create, \
             patch('church_translator.audio_output.time.sleep'):
            with self.assertRaises(OSError):
                with open_output(lambda _: None, device=17):
                    self.fail('Unavailable stream opened')
            self.assertEqual(create.call_count, 3)

    def test_duration_limits_smooth_steps_hysteresis_and_unsustainable_detection(self):
        control = DurationController()
        rates = [control.decide(7.8, 6, 20, now=i)['playback_speed'] for i in range(10)]
        self.assertTrue(all(0 <= b - a <= .0250001 for a, b in zip([1.] + rates, rates)))
        self.assertAlmostEqual(rates[-1], 1.15)
        self.assertTrue(control.decide(7.8, 6, 20)['duration_unsustainable'])
        self.assertEqual(control.decide(6, 6, 0, now=15)['playback_speed'], rates[-1])
        self.assertLess(control.decide(6, 6, 0, now=60)['playback_speed'], rates[-1])
        self.assertEqual(control.decide(.2, .2, 50)['playback_speed'], 1)

    def test_opt_out_and_bad_configuration_are_safe(self):
        with patch.dict(os.environ, {'PLAYBACK_ADAPTIVE_ENABLED': 'false', 'PLAYBACK_MAX_SPEED': 'NaN'}):
            control = DurationController()
            self.assertEqual(control.decide(10, 6, 90)['playback_speed'], 1)
            self.assertEqual(control.maximum, 1.15)

    def test_tempo_preserves_pitch_stereo_and_final_phrase(self):
        rate = 16000
        t = np.arange(3 * rate) / rate
        # Distinct final phrase/tone must survive the filter's final drain.
        signal = np.sin(2 * np.pi * np.where(t < 2, 440 * t, 880 * t)).astype(np.float32) * .3
        data = np.column_stack((signal, signal * .5))
        output = stretch_pcm(data, rate, 1.15)
        self.assertLess(abs(len(output) / rate - 3 / 1.15), .08)
        np.testing.assert_allclose(output[:, 1], output[:, 0] * .5, atol=1e-6)
        for begin, end, expected in ((.3, 1.2, 440), (2., 2.5, 880)):
            samples = output[round(begin * rate):round(end * rate), 0]
            spectrum = np.abs(np.fft.rfft(samples * np.hanning(len(samples))))
            dominant = np.argmax(spectrum) * rate / len(samples)
            self.assertLess(abs(dominant - expected), 3)
        self.assertGreater(np.max(np.abs(output[-round(.1 * rate):])), .1)

    def test_live_mute_volume_change_and_remaining_pcm_accounting(self):
        errors = []
        volumes = {'en': 1., 'yo': 1.}
        player = MixedAudioPlayer('en', 0, lambda: 1, errors.append, self.root / 'playback-en.sqlite',
            languages=('en', 'yo'), samplerate=16000, channels=2)
        for lang in volumes:
            player.volume_getters[lang] = lambda lang=lang: volumes[lang]
            player.enqueue(fixture(.1, .25 if lang == 'en' else .5), lang, sequence_id=0)
        sim = MixerSimulation()
        def change():
            if sim.writes == 1: volumes['en'] = 0
            if sim.writes == 3: volumes['en'] = .5
        sim.after_write = change
        sim.drive(player)
        self.assertFalse(errors)
        np.testing.assert_allclose(sim.blocks[2], .25, atol=1e-6)
        np.testing.assert_allclose(sim.blocks[4], .3125, atol=1e-6)
        self.assertAlmostEqual(player.pending_seconds('en'), 0)
        self.assertAlmostEqual(player.pending_seconds('yo'), 0)
        self.assertGreater(player.nonzero_output_frames, 0)
        player.stop()

    def test_duration_derivative_never_replaces_durable_original(self):
        player = MixedAudioPlayer('en', 0, lambda: 1, self.fail, self.root / 'playback-en.sqlite',
            languages=('en', 'yo'), samplerate=16000, channels=2)
        self.addCleanup(player.stop)
        original = fixture(3, .25)
        for seq in range(5):
            player.enqueue(original, 'en', sequence_id=seq, source_duration=2.4)
        player._pump_lane(player._lanes['en'])
        lane = player._lanes['en']
        self.assertGreater(lane.ready[0]['item']['timing']['playback_speed'], 1)
        self.assertEqual(lane.ready[0]['item']['audio'], original)
        self.assertLess(len(lane.ready[0]['data']), 48000)
        player._drained = True
        self.assertEqual(lane.queue.qsize(), 5)

    def test_start_failure_releases_workers_and_recording_lease(self):
        cfg = replace(load_config(), gemini_api_key=None, google_application_credentials=None)
        engine = TranslationEngine(cfg, EngineSettings(0, True, True, 5, 5, lambda: 1, lambda: 1, 'yo'),
                                   *[MagicMock() for _ in range(5)])
        with patch('church_translator.engine.resolve_output', side_effect=ValueError('disconnected')):
            with self.assertRaisesRegex(ValueError, 'disconnected'):
                engine.start()
        self.assertIsNone(engine._recording_lease)
        self.assertIsNone(engine._diagnostic_thread)

    def test_yoruba_ready_requires_integrity_and_runtime_without_redownload(self):
        import hashlib
        path = self.root / 'model.safetensors'
        path.write_bytes(b'valid')
        manifest = {'model.safetensors': (5, hashlib.sha256(b'valid').hexdigest())}
        with patch.dict(yoruba_tts.FILES, manifest, clear=True), \
             patch.object(yoruba_tts, 'install_directory', return_value=self.root), \
             patch.object(yoruba_tts.importlib.util, 'find_spec', return_value=None), \
             patch.object(yoruba_tts.urllib.request, 'urlopen') as download:
            self.assertIn('Runtime missing', yoruba_tts.installation_status())
            path.write_bytes(b'wrong')
            self.assertEqual(yoruba_tts.installation_status(), 'Not installed / incomplete')
            download.assert_not_called()

    def test_full_pipeline_real_pcm_shared_separate_and_single_languages(self):
        for english, yoruba, shared in ((True, True, True), (True, True, False),
                                        (True, False, False), (False, True, False)):
            with self.subTest(english=english, yoruba=yoruba, shared=shared):
                ready = threading.Event()
                errors, sources, translations, starts, completions, streams = [], [], [], [], [], []
                class Stream:
                    latency = .02
                    def __init__(self, **kwargs):
                        self.device = kwargs['device']
                        self.peak = 0.
                        streams.append(self)
                    @property
                    def time(self): return time.monotonic()
                    def __enter__(self): return self
                    def __exit__(self, *args): pass
                    def stop(self): pass
                    def start(self): pass
                    def write(self, data):
                        self.peak = max(self.peak, float(np.max(np.abs(data))))
                        time.sleep(.0005)
                        return False
                class Voice:
                    _client = None
                    last_timing = {}
                    def synthesize(self, text, language):
                        time.sleep(.003 if language == 'en' else .012)
                        return fixture(.08, .25 if language == 'en' else .5,
                                       rate=24000 if language == 'en' else 16000)
                cfg = replace(load_config(), gemini_api_key=None, google_application_credentials=None,
                              low_latency_auto_return=False)
                with patch('church_translator.engine.resolve_output', side_effect=lambda i: i), \
                     patch('church_translator.audio_mixer.negotiate_output', return_value=(48000, 2, {'backend': 'fake DAC'})), \
                     patch('church_translator.engine.ChunkRecorder') as capture, \
                     patch('church_translator.audio.sd.OutputStream', Stream):
                    capture.return_value.start.side_effect = ready.set
                    engine = TranslationEngine(cfg, EngineSettings(0, english, yoruba, 7, 7 if shared else 8,
                        lambda: 1, lambda: 1, 'yo'), lambda _: None, errors.append, lambda _: None,
                        sources.append, lambda lang, text: translations.append((lang, text)))
                    engine._transcriber = MagicMock()
                    engine._translator = MagicMock()
                    engine._translator.translate_joint.side_effect = lambda text, langs, **kw: {lang: text for lang in langs}
                    engine._tts = Voice()
                    engine._lane_tts['yo'] = Voice()
                    record_start = engine._record_playback_started
                    record_end = engine._record_playback_completed
                    engine._record_playback_started = lambda d: (starts.append(d), record_start(d))
                    engine._record_playback_completed = lambda d: (completions.append(d), record_end(d))
                    engine.start()
                    self.assertTrue(ready.wait(2))
                    engine.pause()
                    engine.resume()
                    for seq in range(6):
                        engine._chunks.put(ProcessingItem(seq, time.monotonic(), manual_text=f'Frāze {seq}.'))
                    engine.stop()
                self.assertFalse(errors, errors)
                self.assertEqual(sources, [f'Frāze {i}.' for i in range(6)])
                expected = ['en'] * english + ['yo'] * yoruba
                for language in expected:
                    self.assertEqual([d['sequence_id'] for d in starts if d['language'] == language], list(range(6)))
                    self.assertEqual([d['sequence_id'] for d in completions if d['language'] == language], list(range(6)))
                self.assertEqual(len(streams), 1 if shared else len(expected))
                self.assertTrue(all(s.peak > 0 for s in streams))
                self.assertEqual({s.device for s in streams}, {7 if l == 'en' or shared else 8 for l in expected})
                self.assertEqual(engine.completion_info['playback_pending'], 0)

    def test_paused_live_route_changes_drain_fifo_and_reuse_unaffected_device(self):
        errors, starts, completed = [], [], []
        class Stream:
            latency = .01
            @property
            def time(self): return time.monotonic()
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def start(self): pass
            def stop(self): pass
            def write(self, data):
                time.sleep(.001)
                return False
        cfg = replace(load_config(), gemini_api_key=None, google_application_credentials=None)
        engine = TranslationEngine(cfg, EngineSettings(0, True, True, 17, 17, lambda: 1, lambda: 1, 'yo'),
            lambda _: None, errors.append, lambda _: None, lambda _: None, lambda *_: None)
        engine._record_playback_started = starts.append
        engine._record_playback_completed = completed.append
        engine._start_diagnostics()
        with patch('church_translator.engine.resolve_output', side_effect=lambda i: i), \
             patch('church_translator.audio_output.negotiate_output', return_value=(16000, 2, {})), \
             patch('church_translator.audio_mixer.negotiate_output', return_value=(16000, 2, {})), \
             patch('church_translator.audio.sd.OutputStream', side_effect=lambda **kw: Stream()):
            player = engine._create_player([('en', 17, lambda: 1), ('yo', 17, lambda: 1)])
            engine._players = {'en': player, 'yo': player}
            with self.assertRaisesRegex(RuntimeError, 'Pause'):
                engine.switch_outputs(17, 18)
            engine._paused.set()
            for seq in (0, 1):
                for lang in ('en', 'yo'):
                    engine._players[lang].enqueue(fixture(.04, .25), lang, sequence_id=seq)
            engine.switch_outputs(17, 18)
            self.assertIsNot(engine._players['en'], engine._players['yo'])
            unchanged = engine._players['yo']
            engine.switch_outputs(19, 18)
            self.assertIs(engine._players['yo'], unchanged)
            for seq in (2, 3):
                for lang in ('en', 'yo'):
                    engine._players[lang].enqueue(fixture(.04, .25), lang, sequence_id=seq)
            engine.switch_outputs(19, 19)
            self.assertIs(engine._players['en'], engine._players['yo'])
            for seq in (4, 5):
                for lang in ('en', 'yo'):
                    engine._players[lang].enqueue(fixture(.04, .25), lang, sequence_id=seq)
            engine.stop()
        self.assertFalse(errors, errors)
        for lang in ('en', 'yo'):
            self.assertEqual([d['sequence_id'] for d in starts if d['language'] == lang], list(range(6)))
            self.assertEqual([d['sequence_id'] for d in completed if d['language'] == lang], list(range(6)))
        self.assertEqual(engine.completion_info['playback_pending'], 0)

    def test_actual_ui_passes_independent_nonzero_live_volume_getters(self):
        from PySide6.QtWidgets import QApplication, QMessageBox
        from church_translator.main import MainWindow
        app = QApplication.instance() or QApplication([])
        with patch('church_translator.main.MainWindow._check_crash_recovery'), \
             patch('church_translator.main.TranslationEngine') as create, \
             patch('church_translator.main.QMessageBox.question', return_value=QMessageBox.Yes), \
             patch('church_translator.main.MainWindow._confirm_secondary_voice', return_value=True):
            window = MainWindow()
            try:
                window.input_combo.clear()
                window.input_combo.addItem('Synthetic input', 1)
                window.secondary_language_combo.setCurrentIndex(window.secondary_language_combo.findData('lt'))
                window.english_enabled.setChecked(True)
                window.russian_enabled.setChecked(True)
                window.english_volume.setValue(73)
                window.russian_volume.setValue(61)
                window.start()
                self.assertTrue(create.called)
                settings = create.call_args.args[1]
                self.assertAlmostEqual(settings.english_volume_getter(), .73)
                self.assertAlmostEqual(settings.russian_volume_getter(), .61)
                window.russian_volume.setValue(0)
                self.assertEqual(settings.russian_volume_getter(), 0)
                self.assertAlmostEqual(settings.english_volume_getter(), .73)
                window.russian_volume.setValue(85)
                self.assertAlmostEqual(settings.russian_volume_getter(), .85)
            finally:
                window.engine = None
                window.session_manager.end_session()
                window.close()
                app.processEvents()

    def test_wasapi_source_format_rejection_uses_native_format_without_changing_device(self):
        writes, errors, opens, details = [], [], [], []
        class Stream:
            latency = .02
            time = None
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def start(self): pass
            def stop(self): pass
            def write(self, data):
                writes.append(data.copy())
                return False
        def output(**kwargs):
            opens.append(kwargs)
            if kwargs['samplerate'] != 48000:
                raise sd.PortAudioError('Invalid sample rate', -9997)
            return Stream()
        player = OrderedAudioPlayer('en', 17, lambda: 1, errors.append,
            self.root / 'wasapi.sqlite', on_started=details.append)
        for seq, rate in enumerate((24000, 16000)):
            player.enqueue(fixture(.25, .25, rate=rate), 'en', sequence_id=seq)
        with patch('church_translator.audio.sd.OutputStream', side_effect=output), \
             patch('church_translator.audio_output.negotiate_output', return_value=(48000, 2, {})):
            player.start()
            player.stop()
        self.assertFalse(errors, errors)
        self.assertEqual([o['samplerate'] for o in opens], [24000, 48000])
        self.assertTrue(all(o['device'] == 17 for o in opens))
        mixed = np.concatenate(writes)
        self.assertEqual(mixed.shape, (24000, 2))
        np.testing.assert_allclose(mixed[100:-100], .25, atol=1e-5)
        self.assertEqual([d['output_sample_rate'] for d in details], [48000, 48000])

    def test_yoruba_only_warms_local_model_instead_of_english_cloud_client(self):
        cfg = replace(load_config(), gemini_api_key=None, google_application_credentials=None)
        engine = TranslationEngine(cfg, EngineSettings(0, False, True, None, None, lambda: 1, lambda: 1, 'yo'),
                                   *[MagicMock() for _ in range(5)])
        for fifo in (engine._chunks, engine._translations, engine._speech, engine._unrecognized):
            self.addCleanup(fifo.close)
        engine._players = {'yo': MagicMock()}
        engine._speech.finish()
        with patch('church_translator.yoruba_tts.prepare') as local, \
             patch('church_translator.services.TextToSpeech._ensure_client') as cloud:
            engine._speech_loop()
        local.assert_called_once()
        cloud.assert_not_called()


if __name__ == '__main__':
    unittest.main()
