"""Short, offline output diagnostic. No microphone, loopback recording or APIs.

--audible sends quiet 440/660 Hz test tones; otherwise the device receives zeros.
Nonzero software PCM and accepted writes do NOT prove physical audible output.
"""
import argparse
import io
import json
from pathlib import Path
import sys
import tempfile
import time

import numpy as np
import soundfile as sf

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from church_translator.audio_mixer import MixedAudioPlayer
from church_translator.audio import OrderedAudioPlayer
from church_translator.audio_output import resolve_output, output_description


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('report', type=Path)
    parser.add_argument('--device', type=int)
    parser.add_argument('--audible', action='store_true')
    args = parser.parse_args(argv)
    index = resolve_output(args.device)
    cases = []
    for languages in (('en',), ('yo',), ('en', 'yo')):
        starts, ends, errors, logs = [], [], [], []
        with tempfile.TemporaryDirectory() as directory:
            cls = MixedAudioPlayer if len(languages) == 2 else OrderedAudioPlayer
            options = {'languages': languages} if len(languages) == 2 else {}
            player = cls(languages[0], index, lambda: 1 if args.audible else 0, errors.append,
                Path(directory) / f'playback-{languages[0]}.sqlite', logs.append,
                on_started=starts.append, on_completed=ends.append, **options)
            for seq in range(3):
                for lang in languages:
                    rate = 24000 if lang == 'en' else 16000
                    t = np.arange(round(.25 * rate)) / rate
                    samples = (.08 * np.sin(2 * np.pi * (440 if lang == 'en' else 660) * t)).astype(np.float32)
                    # Smooth fixture edges so the diagnostic does not click.
                    ramp = round(.005 * rate)
                    samples[:ramp] *= np.linspace(0, 1, ramp)
                    samples[-ramp:] *= np.linspace(1, 0, ramp)
                    output = io.BytesIO()
                    sf.write(output, samples, rate, format='WAV', subtype='PCM_16')
                    player.enqueue(output.getvalue(), lang, captured_at=time.monotonic(), sequence_id=seq)
            player.start()
            player.stop()
            ok = not errors and len(ends) == len(languages) * 3 and player.pending_at_stop == 0
            if len(languages) == 2 and args.audible:
                ok &= player.nonzero_output_frames > 0 and player.peak_output > 0
            cases.append({'languages': languages, 'ok': bool(ok), 'starts': starts, 'ends': ends,
                'errors': errors, 'logs': logs, 'pending': player.pending_at_stop,
                'nonzero_output_frames': getattr(player, 'nonzero_output_frames', None),
                'peak_output': getattr(player, 'peak_output', None)})
    report = {'ok': all(c['ok'] for c in cases), 'device': output_description(index),
        'tones_enabled': args.audible, 'cases': cases, 'provider_requests': 0,
        'physical_audio_verified': False, 'measurement': 'real output device; software PCM and PortAudio writes, no acoustic measurement'}
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps({k: v for k, v in report.items() if k != 'cases'}))
    raise SystemExit(0 if report['ok'] else 1)


if __name__ == '__main__':
    main()
