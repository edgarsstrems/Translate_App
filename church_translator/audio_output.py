"""Output negotiation and diagnostic identity shared by both playback paths."""
import sounddevice as sd
from contextlib import contextmanager
import sys
import time


def unsupported_format(error):
    return isinstance(error, sd.PortAudioError) and len(error.args) > 1 and error.args[1] in (-9997, -9998)


def convert_output_pcm(data, source_rate, target_rate, target_channels):
    """Convert a playback derivative to the negotiated mono/stereo format."""
    import av
    import numpy as np
    if data.shape[1] == 1 and target_channels == 2:
        data = np.repeat(data, 2, axis=1)
    elif data.shape[1] == 2 and target_channels == 1:
        data = data.mean(axis=1, keepdims=True)
    if data.shape[1] != target_channels or target_channels not in (1, 2):
        raise ValueError('Unsupported playback channel conversion; original retained')
    if source_rate != target_rate:
        layout = 'mono' if target_channels == 1 else 'stereo'
        frame = av.AudioFrame.from_ndarray(np.ascontiguousarray(data.T), format='fltp', layout=layout)
        frame.sample_rate = source_rate
        converter = av.AudioResampler(format='fltp', layout=layout, rate=target_rate)
        frames = [*converter.resample(frame), *converter.resample(None)]
        data = np.concatenate([f.to_ndarray().T for f in frames]).astype(np.float32)
    return np.ascontiguousarray(data)


def resolve_output(index):
    """Freeze default selection for the session, including Windows mapper aliases."""
    index = int(sd.default.device[1]) if index is None else int(index)
    raw = sd.query_devices(index, 'output')
    host = sd.query_hostapis(raw['hostapi'])
    if raw['name'] in ('Microsoft Sound Mapper - Output', 'Primary Sound Driver'):
        candidate = int(host['default_output_device'])
        if candidate >= 0:
            index = candidate
    return index


def output_description(index):
    raw = sd.query_devices(index, 'output')
    host = sd.query_hostapis(raw['hostapi'])
    return {'device_index': index, 'device_name': raw['name'], 'backend': host['name'],
            'maximum_output_channels': int(raw['max_output_channels']),
            'native_sample_rate': raw['default_samplerate']}


def negotiate_output(index):
    """Probe actual float32 support before opening; never change the endpoint."""
    info = output_description(index)
    rate = int(info['native_sample_rate'])
    failures = []
    for channels in dict.fromkeys((min(2, info['maximum_output_channels']), 1)):
        if channels < 1:
            continue
        try:
            sd.check_output_settings(device=index, channels=channels, samplerate=rate, dtype='float32')
            return rate, channels, info
        except sd.PortAudioError as exc:
            failures.append(str(exc))
    raise RuntimeError(f'Output format unsupported: {info}; attempts={failures}')


@contextmanager
def open_output(on_status, **kwargs):
    """Retry only initialization, when no PCM can yet have been submitted."""
    for attempt in range(3):
        context = None
        try:
            context = sd.OutputStream(**kwargs)
            stream = context.__enter__()
            break
        except Exception as exc:
            if context is not None:
                context.close()
            if unsupported_format(exc) or attempt == 2:
                raise
            on_status(f'[OUTPUT RETRY] device={kwargs.get("device")} attempt={attempt + 1}/3 '
                      'no_audio_submitted=true same_endpoint=true')
            time.sleep(.25 * (attempt + 1))
    try:
        yield stream
    finally:
        context.__exit__(*sys.exc_info())
