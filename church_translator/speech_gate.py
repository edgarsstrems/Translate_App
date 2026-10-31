"""Conservative audio evidence before STT. Never trims or alters source PCM.

Quietness alone cannot veto speech. Stationary broadband noise must also lack
speech evidence in both original and level-normalized Silero analysis. Ambiguous
audio still reaches recognition, without transcript prompting or upload gain.
"""
from __future__ import annotations

import math
import numpy as np


def audio_evidence(audio):
    samples = np.asarray(audio, dtype=np.float32).reshape(-1)
    rms = float(np.sqrt(np.mean(samples.astype('float64') ** 2))) if samples.size else 0.0
    peak = float(np.max(np.abs(samples))) if samples.size else 0.0
    result = {'rms_dbfs': round(20 * math.log10(max(rms, 1e-12)), 1),
              'peak_dbfs': round(20 * math.log10(max(peak, 1e-12)), 1),
              'skip_stt': False, 'use_context': True, 'allow_gain': True,
              'state': 'speech_candidate', 'vad_available': False}
    if not np.isfinite(samples).all():
        raise ValueError('Non-finite input audio; original retained for review')
    if not peak:
        result.update(state='digital_silence', skip_stt=True, use_context=False, allow_gain=False)
        return result
    frames = samples[:samples.size // 320 * 320].reshape(-1, 320)
    stationary = False
    if len(frames) >= 15:
        levels = np.sqrt(np.mean(frames.astype('float64') ** 2, axis=1))
        variation = float(np.std(levels) / max(1e-12, np.mean(levels)))
        power = np.abs(np.fft.rfft(frames * np.hanning(320), axis=1))[:, 1:] ** 2 + 1e-30
        flatness = float(np.median(np.exp(np.mean(np.log(power), axis=1)) / np.mean(power, axis=1)))
        stationary = variation < .12 and flatness > .35
        result.update(envelope_variation=round(variation, 4), spectral_flatness=round(flatness, 4))
    if rms < .004 or stationary:
        result.update(state='ambiguous_quiet_audio', use_context=False, allow_gain=False)
        try:
            # Already shipped with local Whisper; CPU only, no model download.
            from faster_whisper.vad import get_vad_model
            model = get_vad_model()
            centered = samples - np.mean(samples)
            padded = np.pad(centered, (0, (-len(centered)) % 512))
            raw_probability = float(np.max(model(padded)))
            scale = min(1000.0, .05 / max(1e-12, float(np.sqrt(np.mean(centered ** 2)))),
                        .8 / max(1e-12, float(np.max(np.abs(centered)))))
            normalized_probability = float(np.max(model(padded * max(1.0, scale))))
            probability = max(raw_probability, normalized_probability)
            result.update(vad_available=True, speech_probability=round(probability, 4))
            if probability >= .20:
                result.update(state='speech_candidate', use_context=True, allow_gain=True)
            elif stationary and rms < .004:
                result.update(state='stationary_noise', skip_stt=True)
        except Exception as exc:
            # Missing/broken VAD must never discard potentially genuine speech.
            result['vad_error'] = type(exc).__name__
    return result


def input_level_status(rms, peak):
    if peak >= .999:
        return 'Clipping'
    if rms < .0001 and peak < .0003:
        return 'Silent / near noise floor'
    if rms < .008:
        return 'Quiet'
    return 'Healthy'
