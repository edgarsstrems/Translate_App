"""Replay private recordings through the real journal/transport with mock ASR.

Checks acoustic admission, original retention and packet duration. Mock words
are not a recognition result or reference. No API, translation or TTS calls.
"""
import argparse
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import sys
import time
from unittest.mock import patch

import numpy as np
import soundfile as sf

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tests'))
from test_realtime_transcription import FakeSocket
from church_translator.config import load_config
from church_translator.glossary import Glossary
from church_translator.realtime_transcription import RealtimeTranscriber
from church_translator.speech_audit import SpeechAudit, sample_hash
from church_translator.speech_gate import audio_evidence


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('audio', type=Path)
    parser.add_argument('near_floor', type=Path)
    parser.add_argument('report', type=Path)
    args = parser.parse_args()
    original, rate = sf.read(args.audio, dtype='float32')
    floor, floor_rate = sf.read(args.near_floor, dtype='float32')
    assert rate == floor_rate == 16000 and original.ndim == floor.ndim == 1
    args.report.parent.mkdir(parents=True, exist_ok=True)
    os.environ['CHURCH_TRANSLATOR_APPDATA'] = str(args.report.parent.resolve() / 'recorded-appdata')
    config = replace(load_config(), openai_api_key='fixture', google_application_credentials=None,
                     openai_transcription_model='gpt-live-transcribe')
    results = []
    with patch('socket.socket.connect', side_effect=AssertionError('Offline replay only')):
        for name, source in [('original', original), ('quiet_1_percent', original * .01), ('near_floor', floor)]:
            audit = SpeechAudit(args.report.parent / 'recorded-replay' / name / 'speech-audit.sqlite')
            service = RealtimeTranscriber(config, Glossary(), lambda _: None)
            service.bind_stream(audit)
            socket = FakeSocket(['Mēs šodien kopā klausāmies un mācāmies.'])
            service.connection_factory = lambda *a, **kw: socket
            origin = time.monotonic()
            capture = {'capture_id': name, 'start_sample': 0, 'end_sample': len(source), 'sample_rate': rate}
            service.register_turn(0, capture, origin)
            try:
                for start in range(0, len(source), 1600):
                    block = source[start:start + 1600]
                    details = {'captured_at': origin + start / rate, 'wall_time': time.time()}
                    audit.capture(name, start, block, details)
                    service.feed(name, start, start + len(block), details)
                service.seal_capture(name)
                service.current_capture, service.speech_evidence = capture, audio_evidence(source)
                result = service.transcribe(source)
                assert sample_hash(audit.audio_range(name, 0, len(source))) == sample_hash(source)
                assert len(socket.audio) // 2 == (len(source) * 3 + 1) // 2
                if name == 'near_floor':
                    assert not result.translation_allowed
                results.append({'case': name, 'original_samples': len(source), 'original_sha256': sample_hash(source),
                    'original_unchanged': True, 'transport_samples': len(socket.audio) // 2,
                    'transport_sha256': hashlib.sha256(socket.audio).hexdigest(),
                    'mock_final_eligible': result.translation_allowed, 'acoustic_evidence': service.speech_evidence,
                    'window_gains': service.last_audio_diagnostics['window_gains']})
                service.ack_capture(capture)
            finally:
                service.finish_stream()
                service.close()
                service.close_store()
    report = {'ok': True, 'paid_requests': 0, 'results': results,
              'limits': 'Actual recordings and acoustic checks, mocked ASR words; not WER or provider latency.'}
    args.report.write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps({'ok': True, 'cases': len(results), 'paid_requests': 0}))


if __name__ == '__main__':
    main()
