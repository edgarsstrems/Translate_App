"""Accelerated journal/Realtime/order/Stop test, with no network or devices.

Audio bytes, queues, resampling and event reconciliation are real. Provider,
VAD probabilities, translation, TTS and playback are simulated; this is not ASR
accuracy, audible output or wall-clock service acceptance.
"""
import argparse
import base64
from dataclasses import replace
import hashlib
import io
import json
import os
from pathlib import Path
import queue
import sqlite3
import sys
import time
from unittest.mock import MagicMock, patch

import numpy as np
import soundfile as sf

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from church_translator.audio import ChunkRecorder
from church_translator.config import load_config
from church_translator.engine import EngineSettings, TranslationEngine


class Socket:
    def __init__(self, shared, drop=False):
        self.shared, self.drop = shared, drop
        self.events = queue.Queue()
        self.previous = None
        self.counter = self.appends = 0

    def send(self, wire):
        event = json.loads(wire)
        if event['type'] == 'session.update':
            self.events.put({'type': 'session.updated', 'session': event['session']})
        elif event['type'] == 'input_audio_buffer.append':
            self.appends += 1
            self.shared['pcm_samples'] += len(base64.b64decode(event['audio'])) // 2
            if self.drop and self.appends == 17:
                raise ConnectionError('Injected potentially-submitted packet disconnect')
            self.events.put({'type': 'conversation.item.input_audio_transcription.delta',
                'item_id': f'item-{self.counter}', 'event_id': f'delta-{self.appends}', 'delta': 'Mēs '})
        elif event['type'] == 'input_audio_buffer.commit':
            item = f'item-{self.counter}'
            self.events.put({'type': 'input_audio_buffer.committed', 'item_id': item, 'previous_item_id': self.previous})
            self.previous = item
            self.counter += 1
            self.events.put({'type': 'conversation.item.input_audio_transcription.completed', 'item_id': item,
                'transcript': ' '.join(['Mēs šodien kopā klausāmies un mācāmies.'] * 5)})

    def recv(self, timeout=None):
        try:
            return json.dumps(self.events.get(timeout=timeout))
        except queue.Empty:
            raise TimeoutError()

    def close(self): pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('report', type=Path)
    parser.add_argument('--seconds', type=int, default=180)
    parser.add_argument('--mode', choices=('dynamic', 'balanced', 'low_latency'), default='dynamic')
    parser.add_argument('--mix-shared-output', action='store_true', help='Exercise the production English/Yoruba mixer with a mock device')
    args = parser.parse_args()
    args.report.parent.mkdir(parents=True, exist_ok=True)
    os.environ['CHURCH_TRANSLATOR_APPDATA'] = str(args.report.parent.resolve() / f'stream-{args.mode}-{args.seconds}')
    config = replace(load_config(), openai_api_key='fixture', gemini_api_key=None,
        google_application_credentials=None, openai_transcription_model='gpt-live-transcribe',
        speech_recognition_backend='openai', speed_mode=args.mode, low_latency_auto_return=False)
    errors, source, translations, played = [], [], [], []
    engine = TranslationEngine(config, EngineSettings(0, True, args.mix_shared_output, 0, 0,
        lambda: 0, lambda: 0, secondary_language='yo'),
        lambda _: None, errors.append, lambda _: None, source.append,
        lambda lang, text: translations.append(text))
    shared = {'pcm_samples': 0, 'connections': 0}
    def connect(*args, **kwargs):
        shared['connections'] += 1
        return Socket(shared, drop=shared['connections'] == 1)
    engine._transcriber.connection_factory = connect
    engine._transcriber.session_clock = lambda: shared['pcm_samples'] / 24000
    engine._translator.translate_joint = MagicMock(side_effect=lambda text, languages, **kwargs:
        {lang: '\n'.join('We listen and learn together.' for line in text.splitlines()) for lang in languages})
    engine._tts = MagicMock()
    engine._tts.synthesize.return_value = b'fixture'
    if args.mix_shared_output:
        audio = io.BytesIO()
        sf.write(audio, np.full(4800, .1, np.float32), 16000, format='WAV')
        engine._tts.synthesize.return_value = audio.getvalue()
        engine._lane_tts['yo'] = engine._tts
        original_started = engine._on_playback_started
        def mixed_started(details):
            played.append((details['language'], details['sequence_id']))
            original_started(details)
        engine._on_playback_started = mixed_started
    class Player:
        def __init__(self, *args, **kwargs):
            self.volume_getters = {}
            self.pending_at_stop = 0
        def start(self): pass
        def enqueue(self, audio, **kwargs): played.append(kwargs['sequence_id'])
        def stop(self): pass
    def evidence(audio):
        nonzero = bool(np.any(audio != 0))
        return {'skip_stt': not nonzero, 'state': 'speech_candidate' if nonzero else 'digital_silence',
                'use_context': nonzero, 'allow_gain': False, 'vad_available': True,
                'speech_probability': .9 if nonzero else 0,
                'probable_speech_seconds': len(audio) / 16000 if nonzero else 0,
                'probable_speech_sample_ranges': [[0, len(audio)]] if nonzero else [],
                'rms_dbfs': -30 if nonzero else -240, 'peak_dbfs': -20 if nonzero else -240}
    expected = hashlib.sha256()
    began = time.monotonic()
    class Output:
        latency = .02
        def __init__(self, **kwargs): pass
        @property
        def time(self): return time.monotonic()
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def write(self, data):
            time.sleep(.002)
            return False
        def stop(self): pass
    with patch.object(ChunkRecorder, 'start', ChunkRecorder._start_workers), \
         patch('church_translator.engine.OrderedAudioPlayer', Player), \
         patch('church_translator.audio_mixer.sd.query_devices', return_value={'default_samplerate': 48000, 'max_output_channels': 2}), \
         patch('church_translator.audio_mixer.sd.OutputStream', Output), \
         patch('church_translator.speech_gate.audio_evidence', side_effect=evidence), \
         patch('church_translator.realtime_transcription.audio_evidence', side_effect=evidence):
        engine.start()
        while not engine._recorder or engine._recorder._capture_thread is None:
            time.sleep(.001)
        recorder = engine._recorder
        rng = np.random.default_rng(201)
        for i in range(args.seconds * 10):
            audio = rng.uniform(-.06, .06, 1600).astype('float32')
            if i % 130 > 100:
                audio.fill(0)
            expected.update(audio.tobytes())
            while recorder._raw.qsize() > 400:
                if not recorder._capture_thread.is_alive():
                    raise RuntimeError('Capture worker stopped')
                time.sleep(.002)
            recorder._callback(audio[:, None], len(audio), None, None)
        engine.stop()
    assert not errors, errors
    integrity = engine.integrity_report
    assert integrity['captured_samples'] == args.seconds * 16000
    assert not integrity['uncovered_ranges'] and not integrity['journal_gaps'] and not integrity['unjournaled_samples']
    assert all(engine.completion_info[k] == 0 for k in ('capture_pending','translation_pending','synthesis_pending','playback_pending','realtime_pending'))
    db = engine._audit._connect()
    actual = hashlib.sha256()
    for (blob,) in db.execute('SELECT audio FROM capture ORDER BY capture_id,start_sample'):
        actual.update(blob)
    sequences = [row[0] for row in db.execute("SELECT segment FROM events WHERE kind='source_committed' ORDER BY id")]
    usage = json.loads(db.execute("SELECT details FROM events WHERE kind='realtime_usage' ORDER BY id DESC LIMIT 1").fetchone()[0])
    db.close()
    assert actual.digest() == expected.digest()
    assert sequences == sorted(set(sequences))
    if args.mix_shared_output:
        for language in ('en', 'yo'):
            output_sequences = [seq for lang, seq in played if lang == language]
            assert output_sequences == sorted(set(output_sequences))
        assert engine._players['en'] is engine._players['yo']
        assert engine._players['en'].overlap_frames > 0
    else:
        assert played == sorted(set(played))
    assert sum(len(text.splitlines()) for text in translations) == len(source) * (2 if args.mix_shared_output else 1)
    result = {'ok': True, 'represented_seconds': args.seconds, 'captured_samples': args.seconds * 16000,
        'capture_profile': args.mode,
        'original_hash_matches': True, 'source_order_and_no_duplicate_commits': True,
        'source_rows': len(source), 'output_batches': len(played), 'connections': shared['connections'],
        'shared_mixer_overlap_frames': engine._players['en'].overlap_frames if args.mix_shared_output else None,
        'realtime_usage_estimate_with_mock_provider': usage, 'completion': engine.completion_info,
        'review_audio_retained': engine.completion_info['audio_review_items'],
        'wall_seconds': round(time.monotonic() - began, 3), 'paid_requests': 0,
        'limits': 'Mocked providers/VAD/output; real journal, resampling, queues, source ordering, reconnect and shutdown.'}
    args.report.write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps(result))


if __name__ == '__main__':
    main()
