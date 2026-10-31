"""Optional local MMS Yoruba. Installation is explicit; inference is offline."""
from __future__ import annotations

import hashlib
import io
from pathlib import Path
import threading
import time
import unicodedata
import urllib.request

import numpy as np
import soundfile as sf

from .config import model_root

MODEL_ID = 'facebook/mms-tts-yor'
REVISION = 'd359e06b481cb5402e2e4215475b90c07a3f12e2'
VOICE_NAME = 'yo-NG-MMS-Local'
LICENSE_URL = 'https://creativecommons.org/licenses/by-nc/4.0/'
# Pinned upstream files. Never load a pickle or remote executable model code.
FILES = {
    'config.json': (1641, 'd0378ee0c78342b0e6ccaa43a2c7d93949ca93cd6e74ea5ede0937533c8d9b2a'),
    'tokenizer_config.json': (287, '25adc57f7cc2f274c334e8c34eedc222737b5e6ea436d4b6fcf37a00c70d5111'),
    'special_tokens_map.json': (47, '845f635933c404f060a1d7a2f4c4494bf4a91f8ebec99a41108778d026426dfc'),
    'vocab.json': (488, '2f95ef547f1aec1cabb7afd18c4a991c6dd4d504211fb86e5cca02decec996cf'),
    'README.md': (3975, '49f45adb52f4bc61a565bf82e7b836daf02bac314127be7c3c75423ede7b6566'),
    'model.safetensors': (145231352, 'aa4e892d1d4feecec41e60757a116de8beac58517a042849340861cfb17043c5'),
}
_lock = threading.Lock()
_loaded = None


def install_directory():
    return model_root() / 'mms-tts-yor' / REVISION


def _valid(path, size, digest):
    if not path.is_file() or path.stat().st_size != size:
        return False
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest() == digest


def installation_status(verify=False):
    directory = install_directory()
    ready = all((directory / name).is_file() and (directory / name).stat().st_size == size
                and (not verify or _valid(directory / name, size, digest))
                for name, (size, digest) in FILES.items())
    return 'Ready' if ready else 'Not installed / incomplete'


def install(progress=lambda percent, text: None, *, consent=False, directory=None):
    if not consent:
        raise RuntimeError('Yoruba installation requires explicit download and noncommercial-license consent.')
    directory = Path(directory) if directory is not None else install_directory()
    total = sum(size for size, _ in FILES.values())
    directory.mkdir(parents=True, exist_ok=True)
    with _lock:
        completed = 0
        for name, (size, digest) in FILES.items():
            target = directory / name
            if _valid(target, size, digest):
                completed += size
                continue
            partial = target.with_suffix(target.suffix + '.part')
            request = urllib.request.Request(
                f'https://huggingface.co/{MODEL_ID}/resolve/{REVISION}/{name}',
                headers={'User-Agent': 'ChurchTranslator-Yoruba-Installer/1'})
            received = 0
            # Restart only an incomplete file; verified files survive interruptions.
            with urllib.request.urlopen(request, timeout=30) as response, partial.open('wb') as output:
                while data := response.read(1024 * 1024):
                    received += len(data)
                    if received > size:
                        raise RuntimeError(f'Unexpected size for Yoruba {name}')
                    output.write(data)
                    progress(int((completed + received) * 100 / total), f'Downloading {name}')
            if not _valid(partial, size, digest):
                raise RuntimeError(f'Yoruba {name} failed integrity verification. Retry Install.')
            partial.replace(target)
            completed += size
        (directory / 'ATTRIBUTION.txt').write_text(
            f'Meta MMS Yoruba TTS — {MODEL_ID}\nRevision {REVISION}\n'
            f'License: CC BY-NC 4.0, {LICENSE_URL}\n'
            'Noncommercial use only. Model files are unmodified. See README.md for authors and citation.\n',
            encoding='utf-8')
        progress(100, 'Yoruba model verified. Ready for offline voice preview.')


def _runtime(status):
    global _loaded
    directory = install_directory()
    if _loaded is not None and _loaded[0] == directory:
        return _loaded[1:]
    if installation_status(verify=True) != 'Ready':
        raise RuntimeError('Yoruba voice is unavailable. Open Voice Settings, select Yoruba and click Install.')
    started = time.monotonic()
    status('[YORUBA] Loading local CPU voice…')
    import torch
    from transformers import VitsModel, VitsTokenizer
    torch.set_num_threads(min(4, max(1, torch.get_num_threads())))
    tokenizer = VitsTokenizer.from_pretrained(str(directory), local_files_only=True)
    model = VitsModel.from_pretrained(str(directory), local_files_only=True,
                                      use_safetensors=True).to('cpu').eval()
    _loaded = (directory, model, tokenizer, torch)
    status(f'[YORUBA] CPU voice loaded in {time.monotonic() - started:.2f}s; network=false.')
    return model, tokenizer, torch


def synthesize(text, status=lambda message: None):
    # NFC composes equivalent Unicode without deleting Yoruba tone marks.
    text = unicodedata.normalize('NFC', text).strip()
    if not text:
        return b''
    from .services import TextToSpeech
    with _lock:
        model, tokenizer, torch = _runtime(status)
        output = []
        started = time.monotonic()
        for part in TextToSpeech._web_speech_parts(text, limit=250):
            # VitsTokenizer normally deletes every unknown character. Never let
            # numbers or a name silently lose meaningful letters before speech.
            unsupported = [char for char in part.lower()
                           if (char.isalpha() or char.isdecimal() or unicodedata.category(char).startswith('M'))
                           and char not in tokenizer.get_vocab()]
            if unsupported:
                raise RuntimeError('Yoruba voice cannot pronounce some characters or digits. '
                                   'Keep the text for review; write numbers out in Yoruba before retrying.')
            inputs = tokenizer(part, return_tensors='pt')
            if not inputs['input_ids'].numel():
                raise RuntimeError('No Yoruba characters recognized by the local voice tokenizer.')
            with torch.inference_mode():
                samples = model(**inputs).waveform[0].cpu().numpy()
            if not samples.size or not np.isfinite(samples).all():
                raise RuntimeError('Local Yoruba synthesis returned invalid audio.')
            output.append(samples)
        audio = np.concatenate(output)
        buffer = io.BytesIO()
        sf.write(buffer, np.clip(audio, -.98, .98), model.config.sampling_rate, format='WAV', subtype='PCM_16')
        status(f'[YORUBA] Local synthesis {time.monotonic() - started:.2f}s; audio={len(audio) / model.config.sampling_rate:.2f}s.')
        return buffer.getvalue()


def run_smoke(report_path):
    """Opt-in source/EXE diagnostic against an already installed local model."""
    import json
    import os
    from unittest.mock import patch
    report_path = Path(report_path).resolve()
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report = {'ok': False, 'model': MODEL_ID, 'revision': REVISION, 'rows': [],
              'provider_requests': 0, 'pronunciation_verified': False}
    messages = []
    fixtures = ['Ẹ jẹ́ kí a ní ìfẹ́ àti sùúrù sí ara wa.',
                'Jésù Kristi fẹ́ràn wa. Ẹ má ṣe bẹ̀rù.',
                'Pọ́ọ̀lù kọ̀wé sí àwọn ará Kólósè. A ní ìrètí àti àlàáfíà.']
    try:
        with patch.dict(os.environ, {'HF_HUB_OFFLINE': '1', 'TRANSFORMERS_OFFLINE': '1'}), patch(
                'socket.socket.connect', side_effect=AssertionError('Offline synthesis attempted a network connection')):
            for index, text in enumerate(fixtures):
                started = time.monotonic()
                audio = synthesize(text, messages.append)
                samples, rate = sf.read(io.BytesIO(audio), dtype='float32')
                output = report_path.with_name(f'{report_path.stem}-{index + 1}.wav')
                output.write_bytes(audio)
                report['rows'].append({'characters': len(text), 'seconds': len(samples) / rate,
                    'synthesis_including_first_load_seconds': round(time.monotonic() - started, 3),
                    'peak': float(np.max(np.abs(samples))), 'output': str(output)})
            # Confirm the actual tokenizer retains tone-bearing characters.
            _, tokenizer, _ = _runtime(messages.append)
            accented = tokenizer('fẹ́', return_tensors='pt')['input_ids'].tolist()
            unaccented = tokenizer('fe', return_tensors='pt')['input_ids'].tolist()
            assert accented != unaccented, 'Tone marks were lost by the tokenizer'
            report['tone_marks_reach_tokenizer'] = True
            report['ok'] = True
    except Exception as exc:
        from .reliability import safe_error
        report['error'] = safe_error(exc)
    report['logs'] = messages
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return 0 if report['ok'] else 1
