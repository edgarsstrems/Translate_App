import unittest
from unittest.mock import MagicMock, patch

import numpy as np

from church_translator.audio import ChunkRecorder, SAMPLE_RATE
from church_translator.config import (
    load_config,
    load_user_settings,
    save_user_settings,
    SPEED_MODE_BALANCED,
    SPEED_MODE_LOW_LATENCY,
    SPEED_MODE_CUSTOM,
    SPEED_PROFILES,
    get_speed_profile_settings,
)
from church_translator.engine import (
    TranslationEngine,
    EngineSettings,
    PipelineLatencyTracker,
)


class SpeedModeTests(unittest.TestCase):
    def setUp(self):
        self.config = load_config()

    def test_balanced_mode_defaults_preserved(self):
        """Verify Balanced Mode defaults match the exact baseline configuration."""
        prof = get_speed_profile_settings(SPEED_MODE_BALANCED)
        self.assertEqual(prof["mode"], "balanced")
        self.assertEqual(prof["chunk_seconds"], 5.0)
        self.assertEqual(prof["min_chunk_seconds"], 2.5)
        self.assertEqual(prof["early_flush_silence_seconds"], 0.45)
        self.assertEqual(prof["overlap_seconds"], 0.0)
        self.assertEqual(prof["vad_rms_threshold"], 0.004)
        self.assertEqual(prof["max_audio_queue_size"], 2)
        self.assertEqual(prof["max_translation_queue_size"], 6)

    def test_low_latency_mode_profile(self):
        """Verify Low Latency profile uses adaptive fast chunks and tight pause flush."""
        prof = get_speed_profile_settings(SPEED_MODE_LOW_LATENCY)
        self.assertEqual(prof["mode"], "low_latency")
        self.assertEqual(prof["chunk_seconds"], 2.8)
        self.assertEqual(prof["min_chunk_seconds"], 1.5)
        self.assertEqual(prof["early_flush_silence_seconds"], 0.30)
        self.assertEqual(prof["overlap_seconds"], 0.0)
        self.assertEqual(prof["max_audio_queue_size"], 3)
        self.assertEqual(prof["max_translation_queue_size"], 8)

    def test_custom_mode_validation_and_overrides(self):
        """Verify Custom Mode clamps to safe ranges and respects user parameters."""
        overrides = {
            "chunk_seconds": 3.2,
            "min_chunk_seconds": 1.8,
            "early_flush_silence_seconds": 0.35,
            "overlap_seconds": 0.1,
            "vad_rms_threshold": 0.005,
        }
        prof = get_speed_profile_settings(SPEED_MODE_CUSTOM, custom_overrides=overrides)
        self.assertEqual(prof["mode"], "custom")
        self.assertAlmostEqual(prof["chunk_seconds"], 3.2)
        self.assertAlmostEqual(prof["min_chunk_seconds"], 1.8)
        self.assertAlmostEqual(prof["early_flush_silence_seconds"], 0.35)
        self.assertAlmostEqual(prof["chunk_overlap_seconds"], 0.1)
        self.assertAlmostEqual(prof["vad_rms_threshold"], 0.005)

        # Clamping extreme unsafe values
        unsafe_overrides = {
            "chunk_seconds": 25.0,  # too high -> clamp to 10.0
            "min_chunk_seconds": 0.1,  # too low -> clamp to 0.8
            "early_flush_silence_seconds": 0.01,  # too low -> clamp to 0.20
            "overlap_seconds": 2.0,  # too high -> clamp to 0.50
        }
        clamped = get_speed_profile_settings(SPEED_MODE_CUSTOM, custom_overrides=unsafe_overrides)
        self.assertEqual(clamped["chunk_seconds"], 10.0)
        self.assertEqual(clamped["min_chunk_seconds"], 0.8)
        self.assertEqual(clamped["early_flush_silence_seconds"], 0.20)
        self.assertEqual(clamped["chunk_overlap_seconds"], 0.50)

    def test_mode_isolation(self):
        """Verify modifying Custom or Low Latency settings NEVER mutates Balanced Mode."""
        overrides = {"chunk_seconds": 1.5, "min_chunk_seconds": 0.8}
        _ = get_speed_profile_settings(SPEED_MODE_CUSTOM, custom_overrides=overrides)
        
        balanced = get_speed_profile_settings(SPEED_MODE_BALANCED)
        self.assertEqual(balanced["chunk_seconds"], 5.0)
        self.assertEqual(balanced["min_chunk_seconds"], 2.5)
        self.assertEqual(balanced["early_flush_silence_seconds"], 0.45)
        self.assertEqual(SPEED_PROFILES[SPEED_MODE_BALANCED]["chunk_seconds"], 5.0)

    def test_chunk_recorder_dynamic_update_without_audio_loss(self):
        """Verify update_parameters updates recorder limits while preserving active buffers."""
        chunks = []
        rec = ChunkRecorder(
            device_index=0,
            chunk_seconds=5.0,
            overlap_seconds=0.0,
            min_chunk_seconds=2.5,
            early_flush_silence_seconds=0.45,
            silence_rms_threshold=0.004,
            silence_peak_threshold=0.025,
            on_chunk=lambda seq, audio, at, ctx: chunks.append((seq, audio)),
            on_error=lambda err: None,
        )

        # Feed 1.0 second of audible speech
        audio_1s = np.full(SAMPLE_RATE, 0.05, dtype=np.float32)
        rec._consume(audio_1s)
        self.assertEqual(rec._active_frames, SAMPLE_RATE)
        self.assertEqual(len(chunks), 0)

        # Update parameters dynamically to Low Latency mode
        rec.update_parameters(
            chunk_seconds=2.8,
            min_chunk_seconds=1.5,
            early_flush_silence_seconds=0.30,
        )
        self.assertEqual(rec.chunk_seconds, 2.8)
        self.assertEqual(rec.min_chunk_seconds, 1.5)
        self.assertEqual(rec.early_flush_silence_seconds, 0.30)
        # Verify speech buffer was NOT discarded
        self.assertEqual(rec._active_frames, SAMPLE_RATE)

        # Feed 0.5s of speech then 0.35s pause -> total speech 1.5s >= min_chunk (1.5s), silence >= 0.30s -> emits chunk!
        speech_05s = np.full(int(SAMPLE_RATE * 0.5), 0.05, dtype=np.float32)
        pause_035s = np.zeros(int(SAMPLE_RATE * 0.35), dtype=np.float32)
        rec._consume(speech_05s)
        rec._consume(pause_035s)

        self.assertGreaterEqual(len(chunks), 1)
        rec.stop()

    def test_latency_tracker_consecutive_spikes(self):
        """Verify PipelineLatencyTracker tracks consecutive latency spikes for safety auto-return."""
        tracker = PipelineLatencyTracker()
        self.assertEqual(tracker.consecutive_spikes, 0)

        # Low latencies
        tracker.record(5.2)
        tracker.record(6.1)
        self.assertEqual(tracker.consecutive_spikes, 0)

        # Spike 1
        tracker.record(12.5)
        self.assertEqual(tracker.consecutive_spikes, 1)

        # Spike 2
        tracker.record(13.1)
        self.assertEqual(tracker.consecutive_spikes, 2)

        # Good latency resets consecutive counter
        tracker.record(5.8)
        self.assertEqual(tracker.consecutive_spikes, 0)

        # 3 consecutive spikes
        tracker.record(12.8)
        tracker.record(13.5)
        tracker.record(14.0)
        self.assertEqual(tracker.consecutive_spikes, 3)

    def test_safety_revert_to_balanced_mode(self):
        """Verify engine reverts to Balanced Mode upon instability when auto-return is enabled."""
        engine_settings = EngineSettings(
            input_device_index=0,
            english_enabled=True,
            russian_enabled=False,
            english_output_device_index=None,
            russian_output_device_index=None,
            english_volume_getter=lambda: 0.85,
            russian_volume_getter=lambda: 0.85,
        )

        reverted_modes = []
        status_msgs = []
        engine = TranslationEngine(
            self.config,
            engine_settings,
            on_status=lambda msg: status_msgs.append(msg),
            on_error=lambda err: None,
            on_latency=lambda lat: None,
            on_transcript=lambda txt: None,
            on_translation=lambda lang, txt: None,
            on_speed_mode_changed=lambda m: reverted_modes.append(m),
        )

        # Switch to Low Latency mode
        engine.switch_speed_mode(SPEED_MODE_LOW_LATENCY)
        self.assertEqual(engine.active_speed_settings["mode"], SPEED_MODE_LOW_LATENCY)

        # Trigger safety revert
        engine.revert_to_balanced_mode("3 consecutive latency spikes (13.5s > 12s)")
        self.assertEqual(engine.active_speed_settings["mode"], SPEED_MODE_BALANCED)
        self.assertEqual(reverted_modes, [SPEED_MODE_BALANCED])
        self.assertTrue(any("SAFETY" in msg and "Balanced Mode" in msg for msg in status_msgs))


if __name__ == "__main__":
    unittest.main()
