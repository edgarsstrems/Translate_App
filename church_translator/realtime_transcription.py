"""Persistent, journal-fed OpenAI Realtime transcription.

Only this transport worker touches the socket. Capture advertises committed
sample ranges; recognition consumes item-matched finals in the original FIFO.
No partial is source text, and no failed/possibly submitted audio is forgotten.
"""
import base64
from collections import deque
import hashlib
import json
import re
import threading
import time
import uuid

import numpy as np

from .config import LIVE_TRANSCRIPTION_MODEL
from .realtime_store import RealtimeStore
from .reliability import safe_error
from .speech_gate import audio_evidence

REALTIME_URL = 'wss://api.openai.com/v1/realtime?intent=transcription'
REALTIME_PRICE_PER_MINUTE = .017  # Official published rate, checked 2026-11-01.


class RealtimeUnavailable(RuntimeError):
    """An account/model rejection needs user action, not audio replay."""


def provider_error(event):
    error = event.get('error', {})
    message = str(error.get('message', 'unknown error'))
    code = error.get('code') or error.get('type')
    if code in ('credit_balance_exhausted', 'insufficient_quota', 'invalid_api_key',
                'model_not_found', 'permission_denied'):
        return RealtimeUnavailable(message)
    return RuntimeError('Realtime API: ' + message)


class Pcm24kResampler:
    """Stateful 3:2 linear interpolation; packet partitions cannot change PCM.

    One source sample of lookahead is retained. At a committed turn's end its
    last value supplies the fractional edge. Original 16 kHz PCM is untouched.
    """
    def __init__(self):
        self.input_count = self.output_count = 0
        self.last = None

    def process(self, samples, final=False):
        samples = np.asarray(samples, dtype=np.float32).reshape(-1)
        base = self.input_count - (self.last is not None)
        values = np.concatenate(([self.last], samples)) if self.last is not None else samples
        self.input_count += len(samples)
        if not len(values):
            return b''
        last_index = self.input_count - 1
        limit = ((self.input_count * 3 + 1) // 2 if final else (last_index * 3) // 2 + 1)
        indices = np.arange(self.output_count, limit, dtype=np.int64)
        numerator = indices * 2
        left, fraction = numerator // 3 - base, numerator % 3
        right = np.minimum(left + 1, len(values) - 1)
        output = (values[left].astype('float64') * (3 - fraction) +
                  values[right].astype('float64') * fraction) / 3
        self.last = float(values[-1])
        self.output_count = limit
        return np.clip(np.rint(output * 32768), -32768, 32767).astype('<i2').tobytes()


def session_configuration(prompt='', keywords=()):
    return {'type': 'session.update', 'session': {'type': 'transcription', 'audio': {'input': {
        'format': {'type': 'audio/pcm', 'rate': 24000},
        'noise_reduction': None, 'turn_detection': None,
        'transcription': {'model': LIVE_TRANSCRIPTION_MODEL, 'languages': ['lv'],
                          'delay': 'medium', 'prompt': prompt, 'keywords': list(keywords)}}}}}


class RealtimeTranscriber:
    is_realtime = True

    def __init__(self, config, glossary, status_cb):
        self.config, self.glossary, self.status_cb = config, glossary, status_cb
        self._client = None  # The transport is closed explicitly, not as an HTTP client.
        self.store = self.audit = None
        self._thread = self._socket = None
        self._finished = threading.Event()
        self._abort = threading.Event()
        self.connection_factory = None
        self.on_partial = lambda item, text: None
        self.context_provider = lambda: (None, None)
        self.max_session_seconds = 55 * 60
        self.session_clock = time.monotonic
        self.final_timeout = 25.0
        self.result_timeout = 90.0
        self.window_samples = 8000
        self._generation = 0
        self._pending_ack = deque()
        self._inflight = {}
        self._orphans = {}
        self._partials = {}
        self._event_ids, self._event_order = set(), deque()
        self._active = None
        self._previous_text = ''
        self._last_partial_at = 0
        self._last_commit_id = None
        self._last_context = None
        self._closed = False
        self._fatal_error = None

    def bind_stream(self, audit, on_partial=None, context_provider=None):
        self.audit = audit
        self.store = RealtimeStore(audit.path.parent / 'realtime.sqlite')
        if on_partial:
            self.on_partial = on_partial
        if context_provider:
            self.context_provider = context_provider

    def ensure_model(self):
        if not self.config.openai_api_key:
            raise RuntimeError('OPENAI_API_KEY is not set.')
        if self.store is None:
            raise RuntimeError('Realtime requires the durable capture journal.')
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name='realtime-transcription', daemon=True)
            self._thread.start()
            self.status_cb('[REALTIME] Journal-fed persistent transcription selected; partials are not translated.')

    def feed(self, capture_id, start, end, details):
        self.store.feed(capture_id, start, end, details)
        if self._fatal_error:
            self.store.fail_pending(self._fatal_error)

    def register_turn(self, sequence, capture, captured_at):
        self.store.register(sequence, capture, captured_at)
        if self._fatal_error:
            self.store.fail_pending(self._fatal_error)

    def seal_capture(self, capture_id):
        self.store.seal(capture_id)

    def ack_capture(self, capture):
        if capture:
            self.store.consume(capture)

    def finish_stream(self):
        self._finished.set()

    def close(self):
        if self._closed:
            return
        self.finish_stream()
        if self._thread:
            self._thread.join()
        self._closed = True

    def close_store(self):
        if self.store:
            self.store.close()

    def _send(self, event):
        event = {'event_id': 'evt_' + uuid.uuid4().hex, **event}
        self._socket.send(json.dumps(event, separators=(',', ':')))

    def _open(self):
        if self.connection_factory is None:
            from websockets.sync.client import connect
            self.connection_factory = connect
        self._socket = self.connection_factory(REALTIME_URL,
            additional_headers={'Authorization': 'Bearer ' + self.config.openai_api_key},
            open_timeout=10, close_timeout=2, ping_interval=20, ping_timeout=20,
            max_size=2 * 1024 ** 2, max_queue=32)
        self._generation += 1
        self._pending_ack.clear()
        self._inflight.clear()
        self._orphans.clear()
        self._partials.clear()
        self._event_ids.clear()
        self._event_order.clear()
        self._last_commit_id = self._last_context = None
        self._opened_at = self.session_clock()
        self._rotate_at = self._opened_at + self.max_session_seconds
        self._send(session_configuration())
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                event = json.loads(self._socket.recv(timeout=.2))
            except TimeoutError:
                continue
            if event['type'] == 'error':
                raise provider_error(event)
            if event['type'] == 'session.created':
                expires = event.get('session', {}).get('expires_at')
                if isinstance(expires, (int, float)):
                    self._rotate_at = min(self._rotate_at, self.session_clock() + max(1, expires - time.time() - 60))
            if event['type'] == 'session.updated':
                configured = event.get('session', {}).get('audio', {}).get('input', {})
                if configured.get('transcription', {}).get('model') != LIVE_TRANSCRIPTION_MODEL:
                    raise RuntimeError('Realtime server did not acknowledge the selected transcription model')
                self.audit.event('realtime_session', None, {'generation': self._generation,
                    'session_id': event.get('session', {}).get('id'), 'config': session_configuration()['session']})
                self.status_cb(f'[REALTIME] Connected generation={self._generation}; 24 kHz PCM, client turn detection.')
                return
        raise TimeoutError('Realtime session configuration acknowledgement timed out')

    def _disconnect(self):
        for item_id in self._partials:
            self._preview(str(item_id), '')
        self._partials.clear()
        if self._socket is not None:
            try:
                self._socket.close()
            except Exception:
                pass
        self._socket = None
        self._active = None

    def _packet(self, active, audio, start, end, final=False):
        from .engine import TranslationEngine
        from .services import recognition_context, recognition_keywords
        evidence = audio_evidence(audio) if len(audio) else {'use_context': False, 'allow_gain': False}
        processed, gain = audio, 1.0
        if len(audio) and evidence['allow_gain']:
            processed, gain = TranslationEngine._prepare_whisper_audio(None, audio,
                float(np.sqrt(np.mean(audio * audio))), float(np.max(np.abs(audio))))
        short_window = end - active['source_start'] < 10400
        previous, prepared = self.context_provider() if evidence['use_context'] and not short_window else (None, None)
        prompt, keywords = recognition_context(previous, prepared), recognition_keywords(prepared)
        context_key = (prompt, tuple(keywords))
        if context_key != self._last_context:
            self._send(session_configuration(prompt, keywords))
            self._last_context = context_key
        pcm = active['resampler'].process(processed, final=final)
        # The server's minimum commit window is transport padding, never speech evidence.
        padding = 0
        if final and active['resampler'].output_count < 2400:
            padding = 2400 - active['resampler'].output_count
            pcm += b'\0\0' * padding
        if pcm:
            event = {'type': 'input_audio_buffer.append', 'event_id': 'evt_' + uuid.uuid4().hex,
                     'audio': base64.b64encode(pcm).decode('ascii')}
            wire = json.dumps(event, separators=(',', ':'))
            metadata = {'format': 'pcm16_mono_le', 'sample_rate': 24000, 'gain': gain,
                'audio_evidence': evidence, 'padding_samples': padding,
                'short_window_references_omitted': short_window,
                'pcm_sha256': hashlib.sha256(pcm).hexdigest(),
                'wire_sha256': hashlib.sha256(wire.encode()).hexdigest(), 'event_id': event['event_id'],
                'prompt': prompt, 'keywords': keywords}
            receipt = self.store.packet(active['id'], self._generation, start, end, pcm, metadata)
            self._socket.send(wire)
            self.store.sent(receipt)

    def _advance(self):
        # At most two committed turns in flight; excess work stays on disk.
        if self._active is None:
            if self._pending_ack or len(self._inflight) >= 2 or self.session_clock() >= self._rotate_at:
                return False
            turn = self.store.next_turn()
            if not turn or turn['available_end'] <= turn['start']:
                return False
            self.store.begin(turn['id'], self._generation)
            self._active = {'id': turn['id'], 'capture_id': turn['capture_id'],
                            'source_start': turn['start'], 'cursor': turn['start'], 'resampler': Pcm24kResampler()}
        active = self._active
        with self.store.condition:
            turn = dict(self.store._row(active['id']))
            available = self.store.db.execute('SELECT available_end FROM streams WHERE capture_id=?',
                                             (active['capture_id'],)).fetchone()[0]
        if turn['state'] == 'failed':
            raise RuntimeError('Active Realtime turn was held for recovery')
        if turn['end'] is not None and active['cursor'] > turn['end']:
            raise RuntimeError('Realtime transport crossed a logical source boundary')
        limit = min(available, turn['end']) if turn['end'] is not None else available
        remaining = limit - active['cursor']
        if remaining >= self.window_samples or (turn['end'] is not None and limit == turn['end']):
            end = min(limit, active['cursor'] + self.window_samples)
            final = turn['end'] is not None and end == turn['end']
            audio = self.audit.audio_range(active['capture_id'], active['cursor'], end) if end > active['cursor'] else np.empty(0, 'float32')
            self._packet(active, audio, active['cursor'], end, final=final)
            active['cursor'] = end
            if final:
                self.store.committed(turn['id'])
                self._pending_ack.append(turn['id'])
                self._inflight[turn['id']] = time.monotonic()
                self._send({'type': 'input_audio_buffer.commit'})
                self.audit.state(turn['sequence_id'], 'SUBMITTED', {'transport': 'realtime',
                    'generation': self._generation, 'original_start': turn['start'], 'original_end': end})
                self._active = None
            return True
        return False

    def _handle_event(self, event, generation=None):
        generation = self._generation if generation is None else generation
        if generation != self._generation:
            return  # Retired connection events can never replace current results.
        event_id = event.get('event_id')
        if event_id and event_id in self._event_ids:
            return
        if event_id:
            self._event_ids.add(event_id)
            self._event_order.append(event_id)
            if len(self._event_order) > 4096:
                self._event_ids.discard(self._event_order.popleft())
        kind = event.get('type', '')
        if kind == 'error':
            self.store.event(generation, event)
            raise provider_error(event)
        item_id = event.get('item_id')
        if not (kind.startswith('conversation.item.input_audio_transcription.') or kind == 'input_audio_buffer.committed'):
            return
        self.store.event(generation, event)
        if kind == 'input_audio_buffer.committed':
            if self.store.item(generation, item_id):
                return
            if not self._pending_ack or ('previous_item_id' in event and event['previous_item_id'] != self._last_commit_id):
                raise RuntimeError('Unmatched/out-of-order Realtime commit acknowledgement; audio retained')
            ident = self._pending_ack.popleft()
            self.store.bind(generation, item_id, ident)
            self._last_commit_id = item_id
            for orphan in self._orphans.pop(item_id, []):
                self._apply_transcript(orphan)
            return
        if kind.endswith('.delta'):
            row = self.store.item(generation, item_id)
            if row and row['state'] in ('final', 'failed'):
                return
            # Only event IDs deduplicate deltas. Identical words can be genuine.
            text, first = self._partials.get(item_id, ('', time.monotonic()))
            text += str(event.get('delta', ''))
            if len(text) > 20000 or len(self._partials) > 8:
                raise RuntimeError('Unbounded Realtime partial stream; original audio retained')
            self._partials[item_id] = (text, first)
            if time.monotonic() - self._last_partial_at >= .1:
                self._preview(str(item_id), text)
                self._last_partial_at = time.monotonic()
        elif not self.store.item(generation, item_id):
            if len(self._orphans) >= 8:
                raise RuntimeError('Too many unmatched Realtime final events')
            self._orphans.setdefault(item_id, []).append(event)
        else:
            self._apply_transcript(event)

    def _apply_transcript(self, event):
        row = self.store.item(self._generation, event.get('item_id'))
        if not row or row['generation'] != self._generation:
            return
        kind = event['type']
        if kind.endswith('.completed'):
            text = str(event.get('transcript', '')).strip()
            changed = self.store.complete(row['id'], text)
            self._inflight.pop(row['id'], None)
            partial = self._partials.pop(event['item_id'], None)
            self._preview(str(event['item_id']), '')
            if changed:
                self.audit.event('realtime_final', row['sequence_id'], {'item_id': event['item_id'],
                    'generation': self._generation, 'text': text, 'capture_id': row['capture_id'],
                    'start_sample': row['start'], 'end_sample': row['end'], 'sample_rate': 16000,
                    'timestamp_kind': 'local_original_sample_range_not_word_alignment',
                    'capture_to_final': max(0, time.monotonic() - row['captured_at']),
                    'capture_to_first_delta': max(0, partial[1] - row['captured_at']) if partial else None,
                    'confidence_available': False})
        elif kind.endswith('.failed'):
            self._inflight.pop(row['id'], None)
            self.store.fail(row['id'], 'Provider transcription failed: ' + str(event.get('error', {}).get('message', 'unknown')))

    def _preview(self, item_id, text):
        try:
            self.on_partial(item_id, text)
        except Exception:
            # Display failures must never replay potentially billed audio.
            pass

    def _run(self):
        failures = 0
        attempts = 1 + max(0, min(1, int(self.config.stt_max_recovery_requests)))
        try:
            while not self._abort.is_set():
                if self._finished.is_set() and not self.store.pending_network():
                    return
                if self._fatal_error:
                    self.store.fail_pending(self._fatal_error)
                    self._finished.wait(.1)
                    continue
                try:
                    if self._socket is None:
                        if not self.store.next_turn():
                            self._finished.wait(.05)
                            continue
                        self._open()
                    if self._active is None and not self._inflight and self.session_clock() >= self._rotate_at:
                        self.status_cb('[REALTIME] Rotating at a drained turn boundary; capture continues on disk.')
                        self._disconnect()
                        continue
                    self._advance()
                    try:
                        event = json.loads(self._socket.recv(timeout=.02))
                    except TimeoutError:
                        event = None
                    if event:
                        self._handle_event(event)
                        if event.get('type', '').endswith('.completed'):
                            failures = 0
                    if any(time.monotonic() - sent > self.final_timeout for sent in self._inflight.values()):
                        raise TimeoutError('Realtime final response timed out')
                except Exception as exc:
                    error = safe_error(exc)
                    failures += 1
                    self.audit.event('realtime_connection_failure', None,
                                     {'generation': self._generation, 'error': error, 'original_audio_retained': True})
                    self._disconnect()
                    if isinstance(exc, RealtimeUnavailable):
                        self._fatal_error = 'Realtime requires account/model action: ' + error
                        self.store.fail_pending(self._fatal_error)
                        self.status_cb('[REALTIME] ' + self._fatal_error + '; originals retained for recovery.')
                        continue
                    self.store.reset_connection(attempts, error)
                    self._pending_ack.clear()
                    self._inflight.clear()
                    self.status_cb(f'[REALTIME RECOVERY] {error}; unresolved audio retained, replay budget={attempts - 1}.')
                    if failures >= 3:
                        self.store.fail_pending('Realtime unavailable; original audio requires recovery: ' + error)
                        if not self._finished.is_set():
                            self._finished.wait(30)
                        failures = 0
                    else:
                        self._finished.wait(min(5, failures))
        except Exception as exc:
            self.store.fail_pending('Realtime worker stopped: ' + safe_error(exc))
            self.status_cb('[REALTIME] Transport stopped; originals and pending/review work retained.')
        finally:
            self._disconnect()

    def transcribe(self, audio_float32, leading_context_seconds=0.0, previous_context=None, prepared_context=None):
        from .services import TranscriptionResult, contains_non_latvian_script, OpenAITranscriber
        from .recognition_quality import short_audio_warning, unsupported_previous_echo
        self.ensure_model()
        capture = getattr(self, 'current_capture', None)
        if not capture:
            raise RuntimeError('Realtime result requires the original capture/sample identity')
        row = self.store.result(capture, self.result_timeout)
        raw_text = row['text'] or ''
        text = raw_text if any(char.isalpha() for char in raw_text) else ''
        evidence = getattr(self, 'speech_evidence', {}) or {}
        self.last_short_audio = short_audio_warning(len(audio_float32), text)
        self.last_completeness = OpenAITranscriber._completeness_signals(audio_float32, text, evidence)
        echo = unsupported_previous_echo(text, previous_context or self._previous_text, evidence)
        # A persistent service can carry context internally even after hints are
        # cleared. Rich text over near-floor, speech-unsupported audio is held.
        weak = bool(text and len(re.findall(r'\w+', text)) >= 4 and evidence.get('vad_available')
                    and not evidence.get('probable_speech_seconds')
                    and evidence.get('rms_dbfs', 0) < -60 and evidence.get('peak_dbfs', 0) < -40)
        reasons = [reason for condition, reason in (
            (row['state'] == 'failed', 'realtime_recovery_required'),
            (raw_text and not text, 'non_speech_symbols'),
            (echo or weak or evidence.get('skip_stt'), 'unsupported_realtime_recognition'),
            (contains_non_latvian_script(text), 'recognition_language'),
            (self.last_short_audio['possible_short_audio_hallucination'], 'possible_short_audio_hallucination')) if condition]
        held = bool(reasons)
        if text and not held:
            self._previous_text = text
        self.last_context_reconciliation = {'performed': False, 'selection': 'held_for_review' if held else 'finalized_wording',
            'item_id': row['item_id'], 'generation': row['generation'], 'proof_of_accuracy': False}
        with self.store.condition:
            packets = self.store.db.execute('SELECT metadata,pcm_samples FROM packets WHERE turn_id=? AND generation=? ORDER BY id',
                                            (row['id'], row['generation'])).fetchall()
        metadata = [json.loads(packet['metadata']) for packet in packets]
        self.last_context_diagnostics = {'mechanism': 'realtime.audio.input.transcription', 'provider_support': True,
            'previous_text_prompt': any(packet.get('prompt') for packet in metadata),
            'prepared_context': any(packet.get('keywords') for packet in metadata), 'confidence_available': False}
        self.last_audio_diagnostics = {'source_samples': len(audio_float32), 'sample_rate': 16000,
            'transport_sample_rate': 24000, 'transport': 'persistent_websocket',
            'request_attempts': row['attempts'], 'request_status': row['state'],
            'submitted_samples': sum(packet['pcm_samples'] for packet in packets),
            'window_gains': [packet.get('gain', 1) for packet in metadata],
            'item_id': row['item_id'], 'generation': row['generation']}
        self.last_usage = {}  # Duration billing is estimated from retained transport packets, not token confidence.
        return TranscriptionResult(text=text, raw_text=raw_text, uncertain=held,
            translation_allowed=bool(text) and not held, source_visible=not held,
            confidence_note=row['error'] or ('Realtime hypothesis held with original audio for review' if held else None),
            coverage_review=held or self.last_completeness['suspicious'], uncertainty_reasons=reasons)
