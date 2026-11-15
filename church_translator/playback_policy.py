"""Bounded per-language duration correction; never edits text or source audio."""
from collections import deque
from fractions import Fraction
import os
import time

import av
import numpy as np


def _number(name, default, low, high):
    try:
        value = float(os.getenv(name, default))
        return max(low, min(high, value)) if np.isfinite(value) else default
    except (ValueError, TypeError):
        return default


class DurationController:
    """Small sentence-boundary steps and slow release prevent speed oscillation.

    Limits are conservative operating defaults, not a listening-quality claim.
    Original encoded speech remains in the playback FIFO throughout correction.
    """
    def __init__(self):
        self.enabled = os.getenv('PLAYBACK_ADAPTIVE_ENABLED', 'true').lower() not in ('0', 'false', 'no')
        self.maximum = _number('PLAYBACK_MAX_SPEED', 1.15, 1.0, 1.20)
        self.warning_seconds = _number('PLAYBACK_BACKLOG_WARNING_SECONDS', 12, 4, 120)
        self.rate = 1.0
        self.history = deque(maxlen=60)
        self.last_increase = 0.0

    def decide(self, duration, source_duration, pending_seconds, now=None):
        now = time.monotonic() if now is None else now
        if source_duration > 0 and duration > 0:
            self.history.append((duration, source_duration))
        source = sum(s for _, s in self.history)
        ratio = sum(d for d, _ in self.history) / source if source else 0.0
        target = 1.0
        # Unknown source duration and very short tails must not be accelerated.
        if self.enabled and source_duration > 0 and duration >= .4:
            if pending_seconds >= 2:
                target = max(1.04, ratio * 1.01)
            if pending_seconds >= 6:
                target = max(target, 1.10)
            if pending_seconds >= self.warning_seconds:
                target = self.maximum
            target = min(self.maximum, target)
            if target > self.rate:
                self.rate = min(target, self.rate + .025)
                self.last_increase = now
            elif pending_seconds < 1 and now - self.last_increase >= 20:
                self.rate = max(target, self.rate - .025)
        else:
            self.rate = 1.0
        return {'playback_speed': self.rate, 'generated_to_source_ratio': ratio or None,
                'pending_audio_seconds': max(0.0, pending_seconds),
                'backlog_warning': pending_seconds >= self.warning_seconds,
                'duration_unsustainable': len(self.history) >= 6 and ratio > self.maximum
                    and pending_seconds >= self.warning_seconds,
                'maximum_speed': self.maximum, 'content_discarded': False}


def stretch_pcm(data, samplerate, speed):
    """FFmpeg atempo preserves pitch; run only on a decoder/voice worker.

    Drain the filter, including its final partial window. Padding protects the
    end of a phrase from the overlap window; retain that short quiet tail.
    """
    if speed == 1.0:
        return data
    if not 1.0 <= speed <= 1.20 or data.shape[1] not in (1, 2):
        raise ValueError('Unsupported playback duration correction')
    layout = 'mono' if data.shape[1] == 1 else 'stereo'
    graph = av.filter.Graph()
    source = graph.add('abuffer', sample_rate=str(samplerate), sample_fmt='fltp',
                       channel_layout=layout, time_base=f'1/{samplerate}')
    tempo = graph.add('atempo', str(speed))
    planar = graph.add('aformat', 'sample_fmts=fltp')
    sink = graph.add('abuffersink')
    source.link_to(tempo)
    tempo.link_to(planar)
    planar.link_to(sink)
    graph.configure()
    output = []
    def drain():
        while True:
            try:
                output.append(sink.pull().to_ndarray().T)
            except (av.error.BlockingIOError, av.error.EOFError):
                break
    padded = np.pad(data, ((0, round(.06 * samplerate)), (0, 0)))
    for offset in range(0, len(padded), 2048):
        frame = av.AudioFrame.from_ndarray(np.ascontiguousarray(padded[offset:offset + 2048].T),
                                           format='fltp', layout=layout)
        frame.sample_rate, frame.pts, frame.time_base = samplerate, offset, Fraction(1, samplerate)
        source.push(frame)
        drain()
    source.push(None)
    drain()
    result = np.ascontiguousarray(np.concatenate(output), dtype=np.float32)
    if not len(result) or not np.isfinite(result).all():
        raise ValueError('Invalid duration-corrected PCM; original retained')
    return result
