"""Replay local diagnostic audio + existing responses through admission. No APIs.

The supplied archive contains nested segment ZIPs. This verifies deterministic
classification/retention, never the accuracy of new provider recognition.
Output may contain private metadata; keep it in ignored build/private-audit.
"""
import argparse
from dataclasses import replace
import hashlib
import io
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock
import zipfile

import numpy as np
import soundfile as sf

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from church_translator.config import load_config
from church_translator.glossary import Glossary
from church_translator.services import OpenAITranscriber
from church_translator.speech_gate import audio_evidence


def verify(archive, report):
    rows = []
    with zipfile.ZipFile(archive) as outer:
        for name in outer.namelist():
            if not name.endswith('.zip'):
                continue
            with zipfile.ZipFile(io.BytesIO(outer.read(name))) as inner:
                audit = json.loads(inner.read('audit.json'))
                first = json.loads(inner.read('submitted-001.json'))
                audio, rate = sf.read(io.BytesIO(inner.read('original.wav')), dtype='float32')
                assert rate == 16000
                digest = hashlib.sha256(audio.astype('<f4').tobytes()).hexdigest()
                assert digest == audit['original_float32_sha256']
                submissions = []
                for entry in inner.namelist():
                    if not entry.startswith('submitted-') or not entry.endswith('.json'):
                        continue
                    submission = json.loads(inner.read(entry))
                    encoded = inner.read(entry.replace('.json', '.wav'))
                    decoded, sr = sf.read(io.BytesIO(encoded), dtype='float32')
                    diagnostics = submission['request']['diagnostics']
                    assert hashlib.sha256(encoded).hexdigest() == diagnostics['encoded_sha256']
                    assert hashlib.sha256(decoded.astype('<f4').tobytes()).hexdigest() == diagnostics['decoded_pcm_sha256']
                    assert sr == rate and len(decoded) == len(audio)
                    submissions.append(submission['request']['attempt'])
                evidence = audio_evidence(audio)
                prompt = first['request']['request_options'].get('prompt', '')
                reference = json.loads(prompt.split('\n', 1)[1]).get('previous_speech_not_current_audio') if prompt else None
                config = replace(load_config(), openai_api_key='fixture', openai_transcription_model=first['request']['model'])
                service = OpenAITranscriber(config, Glossary(), lambda _: None)
                service.speech_evidence = evidence
                service._previous_text = reference or ''
                service._client = MagicMock()
                raw = first['raw_provider_response'] or {}
                service._client.audio.transcriptions.create.return_value = SimpleNamespace(text=raw.get('text', ''), logprobs=raw.get('logprobs'))
                result = service.transcribe(audio, previous_context=reference)
                options = service._client.audio.transcriptions.create.call_args.kwargs
                assert not evidence['skip_stt']  # Ambiguity never deletes the audio.
                assert not evidence['use_context'] and not evidence['allow_gain']
                assert not options.get('prompt') and not options.get('keywords')
                if raw.get('text') and reference == raw['text']:
                    assert not result.translation_allowed and result.coverage_review
                    assert result.raw_text == raw['text']
                rows.append({'segment': audit['events'][0]['segment'], 'samples': len(audio),
                    'original_hash_matches': True, 'submitted_attempts_verified': submissions,
                    'evidence': evidence, 'old_context': bool(prompt), 'old_gain': first['request']['gain'],
                    'replay_text_characters': len(result.text), 'replay_translation_allowed': result.translation_allowed,
                    'replay_review': result.coverage_review, 'replay_requests_mocked': service._client.audio.transcriptions.create.call_count,
                    'start_sample': audit['start_sample'], 'end_sample': audit['end_sample']})
    rows.sort(key=lambda row: row['segment'])
    contiguous = all(a['end_sample'] == b['start_sample'] for a, b in zip(rows, rows[1:]))
    result = {'ok': True, 'paid_requests': 0, 'originals_verified': len(rows),
              'submitted_wavs_verified': sum(len(row['submitted_attempts_verified']) for row in rows),
              'contiguous_in_supplied_window': contiguous, 'rows': rows,
              'limitations': 'Existing responses replayed; no new ASR, checked reference, listening or word accuracy proof.'}
    Path(report).parent.mkdir(parents=True, exist_ok=True)
    Path(report).write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps({key: value for key, value in result.items() if key != 'rows'}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archive', type=Path)
    parser.add_argument('report', type=Path)
    args = parser.parse_args()
    verify(args.archive, args.report)
