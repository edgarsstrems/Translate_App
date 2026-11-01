"""Explicitly authorized, capped recorded-audio comparison. Private output.

Requires --live and --budget-usd. No translation/TTS requests, downloads, or
settings changes. Billing is estimated from published duration rates; each
potential audio request/append is reserved before sending, including retries.
"""
import argparse
from dataclasses import replace
import io
import json
from pathlib import Path
import re
import sys
import time
import zipfile
from unittest.mock import patch

import numpy as np
import soundfile as sf

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from church_translator.config import load_config
from church_translator.engine import TranslationEngine
from church_translator.glossary import Glossary
from church_translator.realtime_transcription import RealtimeTranscriber
from church_translator.services import OpenAITranscriber
from church_translator.speech_audit import SpeechAudit
from church_translator.speech_gate import audio_evidence
from church_translator.reliability import safe_error
from scripts.compare_sermon_context import error_counts

RATES = {'gpt-4o-mini-transcribe': .003, 'gpt-transcribe': .0045, 'gpt-live-transcribe': .017}


class Budget:
    def __init__(self, limit, save):
        self.limit, self.reserved, self.calls, self.save = limit, 0.0, 0, save
    def reserve(self, model, seconds):
        estimate = seconds / 60 * RATES[model]
        if self.reserved + estimate > self.limit + 1e-10:
            raise RuntimeError('Authorized estimated audio budget exhausted; request not sent')
        self.reserved += estimate
        self.calls += 1
        self.save()


def checked_score(reference, text):
    if not reference or not reference.get('verified'):
        return None
    words = re.findall(r'\w+', text.casefold())
    anchor = re.findall(r'\w+', reference['terminal_anchor'].casefold())
    for index in range(len(words) - len(anchor) + 1):
        if words[index:index + len(anchor)] == anchor:
            return {**error_counts(reference['checked_clear_portion'], ' '.join(words[:index + len(anchor)])),
                    'scope': reference['scope'], 'unchecked_tail_excluded': True}
    return {'wer': None, 'reason': 'Checked terminal anchor absent; cannot safely separate the unchecked ending.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('audio', type=Path)
    parser.add_argument('report', type=Path)
    parser.add_argument('--reference', type=Path)
    parser.add_argument('--near-floor', type=Path)
    parser.add_argument('--budget-usd', required=True, type=float)
    parser.add_argument('--live', action='store_true')
    args = parser.parse_args()
    if not args.live or not 0 < args.budget_usd <= .03:
        raise ValueError('Explicit --live and a positive budget no greater than $0.03 are required.')
    original, rate = sf.read(args.audio, dtype='float32')
    if rate != 16000 or original.ndim != 1 or len(original) > 160000:
        raise ValueError('Use a mono 16 kHz recording of at most ten seconds.')
    cases = [('original', original), ('quiet_1_percent', original * .01)]
    if args.near_floor:
        floor, sr = sf.read(args.near_floor, dtype='float32')
        if sr != 16000 or len(floor) > 40000:
            raise ValueError('Near-floor fixture must be 16 kHz, at most 2.5 seconds.')
        cases.append(('near_floor', floor))
    reference = json.loads(args.reference.read_text(encoding='utf-8')) if args.reference else None
    args.report.parent.mkdir(parents=True, exist_ok=True)
    report = {'private': True, 'rates_usd_per_minute': RATES, 'authorized_budget_usd': args.budget_usd,
              'file': [], 'realtime': [], 'translation_tts_calls': 0,
              'limits': 'Small recorded sample, one partially checked reference; no natural-service or audible acceptance.'}
    budget = None
    def save():
        report['potential_audio_operations'] = budget.calls if budget else 0
        report['estimated_reserved_usd'] = budget.reserved if budget else 0
        report['billing_verified'] = False
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    budget = Budget(args.budget_usd, save)
    cfg = replace(load_config(), stt_max_recovery_requests=0)
    if not cfg.openai_api_key:
        raise RuntimeError('No configured OpenAI key; no requests sent.')
    save()
    from openai import OpenAI
    for name, audio in cases:
        evidence = audio_evidence(audio)
        upload, gain = (TranslationEngine._prepare_whisper_audio(None, audio,
            float(np.sqrt(np.mean(audio * audio))), float(np.max(np.abs(audio))))) if evidence['allow_gain'] else (audio, 1.0)
        # The baseline remains selected in the app and is not bought again.
        # Compare only the two new duration-billed workflows.
        for model in ('gpt-transcribe',):
            client = OpenAI(api_key=cfg.openai_api_key, max_retries=0, timeout=20)
            service = OpenAITranscriber(replace(cfg, openai_transcription_model=model), Glossary(), lambda _: None)
            service._client, service.speech_evidence = client, evidence
            create = client.audio.transcriptions.create
            def request(**kwargs):
                kwargs['file'].seek(0)
                duration = sf.info(kwargs['file']).duration
                kwargs['file'].seek(0)
                budget.reserve(model, duration)
                return create(**kwargs)
            started = time.monotonic()
            try:
                with patch.object(client.audio.transcriptions, 'create', side_effect=request):
                    result = service.transcribe(upload)
                elapsed = time.monotonic() - started
                report['file'].append({'case': name, 'model': model, 'text': result.text, 'raw_text': result.raw_text,
                    'translation_allowed': result.translation_allowed, 'gain': gain,
                    'request_seconds': elapsed, 'estimated_capture_to_final_seconds': len(audio) / rate + elapsed,
                    'usage': service.last_usage, 'checked_portion_score': checked_score(reference, result.text) if name != 'near_floor' else None})
            except Exception as exc:
                report['file'].append({'case': name, 'model': model, 'error': safe_error(exc)})
                code = getattr(exc, 'code', None)
                if code in ('credit_balance_exhausted', 'insufficient_quota', 'invalid_api_key', 'model_not_found'):
                    report['blocked'] = 'Account/model action required; remaining paid tests skipped.'
                    save()
                    return
            finally:
                client.close()
                save()
    from websockets.sync.client import connect
    import base64
    class CappedSocket:
        def __init__(self, socket): self.socket = socket
        def send(self, wire):
            event = json.loads(wire)
            if event['type'] == 'input_audio_buffer.append':
                budget.reserve('gpt-live-transcribe', len(base64.b64decode(event['audio'])) / 2 / 24000)
            return self.socket.send(wire)
        def recv(self, **kwargs): return self.socket.recv(**kwargs)
        def close(self): return self.socket.close()
    audit = SpeechAudit(args.report.parent / 'live-comparison' / 'speech-audit.sqlite')
    service = RealtimeTranscriber(replace(cfg, openai_transcription_model='gpt-live-transcribe'), Glossary(), lambda _: None)
    service.bind_stream(audit)
    service.connection_factory = lambda *a, **kw: CappedSocket(connect(*a, **kw))
    service.ensure_model()
    try:
        for sequence, (name, audio) in enumerate(cases):
            capture_id = name
            origin = time.monotonic()
            capture = {'capture_id': capture_id, 'start_sample': 0, 'end_sample': len(audio), 'sample_rate': rate}
            # Announcing the final extent is a fixture shortcut only. The app
            # registers a boundary when client-side segmentation actually emits it.
            service.register_turn(sequence, capture, origin)
            for start in range(0, len(audio), 1600):
                block = audio[start:start + 1600]
                details = {'captured_at': origin + start / rate, 'wall_time': time.time()}
                audit.capture(capture_id, start, block, details)
                service.feed(capture_id, start, start + len(block), details)
                time.sleep(len(block) / rate)
            service.seal_capture(capture_id)
            service.current_capture, service.speech_evidence = capture, audio_evidence(audio)
            result = service.transcribe(audio)
            report['realtime'].append({'case': name, 'model': 'gpt-live-transcribe', 'text': result.text,
                'raw_text': result.raw_text, 'translation_allowed': result.translation_allowed,
                'capture_to_final_seconds': time.monotonic() - origin,
                'diagnostics': service.last_audio_diagnostics, 'review': result.coverage_review,
                'checked_portion_score': checked_score(reference, result.text) if name != 'near_floor' else None})
            service.ack_capture(capture)
            save()
            if service._fatal_error:
                report['blocked'] = service._fatal_error
                break
    finally:
        service.finish_stream()
        service.close()
        report['realtime_transport_usage'] = service.store.usage()
        service.close_store()
        save()
    print(json.dumps({'report': str(args.report), 'estimated_reserved_usd': budget.reserved,
                      'file_results': len(report['file']), 'realtime_results': len(report['realtime'])}))


if __name__ == '__main__':
    main()
