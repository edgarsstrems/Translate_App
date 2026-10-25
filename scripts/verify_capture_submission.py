"""Accelerated two-hour callback -> journal -> durable queue -> encoded STT soak.

No APIs or microphones. The fake STT endpoint decodes every actual request file
and compares it to the expected PCM16 representation of the prepared original.
Random provider delays create a real backlog. Float hashes cover digital zeros
as well as nonzero input. This does not measure recognition accuracy.
"""
import argparse
from dataclasses import replace
import hashlib
import io
import json
import os
from pathlib import Path
import queue
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import soundfile as sf
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from church_translator.audio import ChunkRecorder, SAMPLE_RATE
from church_translator.config import load_config, get_speed_profile_settings
from church_translator.engine import TranslationEngine, EngineSettings
from church_translator.services import OpenAITranscriber
from church_translator.glossary import Glossary
from church_translator.speech_audit import sample_hash


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('report', type=Path)
    p.add_argument('--seconds', type=int, default=7200)
    p.add_argument('--mode', choices=('balanced', 'low_latency', 'dynamic'), default='dynamic')
    args = p.parse_args()
    args.report.parent.mkdir(parents=True, exist_ok=True)
    os.environ['CHURCH_TRANSLATOR_APPDATA'] = str(args.report.parent.resolve() / ('soak-' + args.mode))
    cfg = replace(load_config(), speed_mode=args.mode, openai_api_key='synthetic-not-a-key',
                  openai_fallback_enabled=False, speech_recognition_backend='openai')
    settings = EngineSettings(0, False, False, None, None, lambda: 0, lambda: 0)
    errors, expected_requests = [], queue.Queue()
    engine = TranslationEngine(cfg, settings, lambda *_: None, errors.append, lambda *_: None,
                               lambda *_: None, lambda *_: None)
    service = OpenAITranscriber(cfg, Glossary(), lambda *_: None)
    counts = {'requests': 0, 'submitted_samples': 0, 'original_samples': 0, 'segments': 0,
              'accounted_digital_silence_samples': 0, 'maximum_backlog': 0}
    original_hash, partition_hash = hashlib.sha256(), hashlib.sha256()
    profile = get_speed_profile_settings(args.mode)
    def endpoint(**kwargs):
        expected = expected_requests.get(timeout=3)
        actual, rate = sf.read(kwargs['file'], dtype='float32')
        assert rate == SAMPLE_RATE
        np.testing.assert_array_equal(actual, expected)
        counts['requests'] += 1
        counts['submitted_samples'] += len(actual)
        time.sleep(.005 if counts['requests'] % 13 else .08)
        return SimpleNamespace(text='Mēs rūpējamies par savu ķermeni un ticību. ' * 8, logprobs=[])
    service._client = SimpleNamespace(audio=SimpleNamespace(transcriptions=SimpleNamespace(create=endpoint)))
    engine._transcriber = service
    def emitted(seq, data, at, overlap):
        meta = recorder.last_capture
        assert meta['start_sample'] == counts['original_samples']
        counts['original_samples'] = meta['end_sample']
        counts['segments'] += 1
        partition_hash.update(data.tobytes())
        if np.any(data != 0):
            analysis = engine._analyze_speech(data)
            processed, _ = engine._prepare_whisper_audio(data, analysis.rms, analysis.peak)
            if len(processed) < 8000:
                processed = np.pad(processed, (0, 8000 - len(processed)))
            encoded = io.BytesIO()
            sf.write(encoded, processed, SAMPLE_RATE, format='FLAC', subtype='PCM_16')
            encoded.seek(0)
            expected_requests.put(sf.read(encoded, dtype='float32')[0])
        else:
            counts['accounted_digital_silence_samples'] += len(data)
        engine._on_chunk(seq, data, at, overlap)
        counts['maximum_backlog'] = max(counts['maximum_backlog'], engine._chunks.qsize())
    recorder = ChunkRecorder(0, profile['chunk_seconds'], 0, profile['min_chunk_seconds'],
        profile['early_flush_silence_seconds'], .004, .025, emitted, errors.append,
        dynamic_mode=args.mode == 'dynamic', on_capture=engine._audit.capture)
    engine._recorder = recorder
    # Let the real engine processing loop drain concurrently without opening a
    # device. The physical callback is exercised via the real recorder below.
    with patch('church_translator.engine.ChunkRecorder', return_value=recorder), patch.object(recorder, 'start'):
        processor = threading.Thread(target=engine._process_loop)
        processor.start()
        recorder._capture_thread = threading.Thread(target=recorder._capture_loop)
        recorder._capture_thread.start()
        rng = np.random.default_rng(91)
        began = time.monotonic()
        for i in range(args.seconds * 10):
            data = rng.uniform(-.1, .1, 1600).astype('float32')
            if i % 197 < 3 or i % 401 < 17:
                data[:] = 0
            if i % 83 == 0:
                data *= .0001
            original_hash.update(data.tobytes())
            # Accelerated producer, bounded to avoid an artificial memory-speed
            # flood that real-time 100ms hardware callbacks cannot generate.
            while recorder._raw.qsize() > 400:
                if not recorder._capture_thread.is_alive():
                    raise RuntimeError('Segmenter failed')
                time.sleep(.002)
            recorder._callback(data[:, None], len(data), None, None)
        recorder.stop()
        engine._chunks.finish()
        processor.join(timeout=120)
        assert not processor.is_alive(), 'Processor did not drain'
    assert not errors, errors
    assert expected_requests.empty()
    assert recorder._received_samples == recorder._consumed_samples == recorder._emitted_samples == args.seconds * SAMPLE_RATE
    assert original_hash.digest() == partition_hash.digest()
    assert engine._chunks.qsize() == 0
    result = {'ok': True, 'simulated_seconds': args.seconds, 'mode': args.mode,
              'all_float32_samples_exact_including_zeros': True,
              'all_nonzero_segments_reached_encoded_request': True,
              'request_pcm16_samples_exact': True, 'sha256': original_hash.hexdigest(),
              'counts': counts, 'errors': errors, 'wall_seconds': round(time.monotonic() - began, 2),
              'audit': str(engine._audit.path), 'evidence': 'synthetic, accelerated; no real ASR or device claim'}
    for q in (engine._chunks, engine._translations, engine._speech, engine._unrecognized):
        q.close()
    args.report.write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps(result))


if __name__ == '__main__':
    main()
