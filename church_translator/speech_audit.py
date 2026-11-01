"""Local-only original audio and recognition audit. Never included in releases.

Sample ranges are half-open and scoped to a capture_id (Pause starts a new one).
Each worker-delivered callback is committed before segmentation. PortAudio's
internal buffers and the still-queued callback tail cannot survive power loss.
"""
import hashlib
import json
from pathlib import Path
import sqlite3
import time

import numpy as np


def sample_hash(audio):
    return hashlib.sha256(np.asarray(audio, dtype='<f4').tobytes()).hexdigest()


class SpeechAudit:
    @classmethod
    def existing(cls, path):
        audit = cls.__new__(cls)
        audit.path = Path(path)
        audit.read_only = True
        if not audit.path.is_file():
            raise FileNotFoundError(audit.path)
        return audit

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=15)
        try:
            # Initialize before journal/recognition workers share this audit.
            # Concurrent journal_mode/DDL could fail with SQLITE_BUSY.
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('CREATE TABLE IF NOT EXISTS capture (capture_id TEXT, start_sample INTEGER, '
                       'end_sample INTEGER, sha256 TEXT, audio BLOB, details TEXT, '
                       'PRIMARY KEY(capture_id, start_sample))')
            db.execute('CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, '
                       'kind TEXT, segment INTEGER, at REAL, details TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS submissions (id INTEGER PRIMARY KEY, '
                       'segment INTEGER, audio BLOB, metadata TEXT, status TEXT, response TEXT, error TEXT)')
            db.commit()
        finally:
            db.close()

    def _connect(self):
        if getattr(self, 'read_only', False):
            suffix = '?mode=ro' if Path(str(self.path) + '-wal').exists() else '?immutable=1'
            return sqlite3.connect(self.path.resolve().as_uri() + suffix, uri=True, timeout=15)
        db = sqlite3.connect(self.path, timeout=15)
        db.execute('PRAGMA synchronous=FULL')
        return db

    def capture(self, capture_id, start, audio, details):
        self.capture_packets(capture_id, [(start, audio, details)])

    def capture_packets(self, capture_id, packets):
        rows = []
        for start, audio, details in packets:
            data = np.asarray(audio, dtype='<f4')
            rows.append((capture_id, start, start + len(data), sample_hash(data),
                         data.tobytes(), json.dumps(details, ensure_ascii=False)))
        db = self._connect()
        try:
            with db:
                db.executemany('INSERT INTO capture VALUES (?, ?, ?, ?, ?, ?)', rows)
        finally:
            db.close()

    def event(self, kind, segment, details):
        db = self._connect()
        try:
            with db:
                db.execute('INSERT INTO events(kind, segment, at, details) VALUES (?, ?, ?, ?)',
                           (kind, segment, time.time(), json.dumps(details, ensure_ascii=False)))
        finally:
            db.close()

    def state(self, segment, state, details=None):
        self.event('segment_state', segment, {'state': state, **(details or {})})

    def submission(self, segment, phase, details):
        """Exact provider input and immutable raw response, separate from UI text."""
        db = self._connect()
        try:
            with db:
                if phase == 'submitted':
                    metadata = {k: v for k, v in details.items() if k != 'audio'}
                    cursor = db.execute('INSERT INTO submissions(segment,audio,metadata,status) VALUES(?,?,?,?)',
                        (segment, details['audio'], json.dumps(metadata, ensure_ascii=False), 'SUBMITTED'))
                    return cursor.lastrowid
                db.execute('UPDATE submissions SET status=?,response=?,error=? WHERE id=?',
                    ('RECOGNIZED' if phase == 'returned' else 'FAILED_RETAINED',
                     json.dumps(details.get('response'), ensure_ascii=False), details.get('error'), details['receipt']))
        finally:
            db.close()

    def packet(self, capture_id, start):
        return self.packets(capture_id, start, 1)[0]

    def packets(self, capture_id, start, limit=32):
        db = self._connect()
        try:
            rows = db.execute('SELECT start_sample, audio, sha256, details FROM capture '
                              'WHERE capture_id=? AND start_sample>=? ORDER BY start_sample LIMIT ?',
                              (capture_id, start, limit)).fetchall()
        finally:
            db.close()
        if not rows or rows[0][0] != start:
            raise ValueError('Missing journaled callback')
        packets = []
        for left, raw, digest, details in rows:
            audio = np.frombuffer(raw, dtype='<f4').copy()
            if sample_hash(audio) != digest:
                raise ValueError('Journaled callback hash mismatch')
            packets.append((left, audio, json.loads(details)))
        return packets

    def integrity_report(self):
        """Compare original callback ranges with logical segments; no text/audio in report."""
        db = self._connect()
        try:
            captures = db.execute('SELECT capture_id,start_sample,end_sample,details FROM capture '
                                  'ORDER BY capture_id,start_sample').fetchall()
            events = [(kind, segment, json.loads(details)) for kind, segment, details in
                      db.execute('SELECT kind,segment,details FROM events ORDER BY id')]
        finally:
            db.close()
        ranges, stops = {}, {}
        for kind, _, details in events:
            if kind == 'captured_segment' and details.get('capture'):
                c = details['capture']
                ranges.setdefault(c['capture_id'], []).append((c['start_sample'], c['end_sample']))
            elif kind == 'capture_stopped':
                stops[details['capture_id']] = details
        total, logical, overlap, overruns = 0, 0, 0, 0
        uncovered, journal_gaps = [], []
        ends = {}
        for ident, left, right, raw in captures:
            expected = ends.get(ident, 0)
            if left != expected:
                journal_gaps.append({'capture_id': ident, 'start': expected, 'end': left})
            ends[ident] = right
            total += right - left
            info = json.loads(raw)
            if info.get('status') or (info.get('adc_gap_seconds') or 0) > 2 / 16000:
                overruns += 1  # Potential hardware loss; size is unknown.
        for ident, end in ends.items():
            cursor = 0
            for left, right in sorted(ranges.get(ident, [])):
                if left > cursor:
                    uncovered.append({'capture_id': ident, 'start': cursor, 'end': left})
                overlap += max(0, cursor - left)
                logical += right - left
                cursor = max(cursor, right)
            if cursor < end:
                uncovered.append({'capture_id': ident, 'start': cursor, 'end': end})
        received = sum(s['captured_samples'] for s in stops.values()) if stops else total
        attempted = {s for k, s, _ in events if k == 'translation_attempt'}
        completed = {s: d.get('provider', 'unknown') for k, s, d in events if k == 'translated'}
        # Old audits without attempt/provider metadata cannot establish rates.
        gemini_completed = sum(p == 'gemini' for p in completed.values())
        translation_metrics = {
            'attempted_batches': len(attempted), 'completed_batches': len(completed),
            'gemini_completed': gemini_completed,
            'openai_completed': sum(p == 'openai' for p in completed.values()),
            'review_required_batches': len({s for k, s, _ in events if k == 'translation_held'}),
            'gemini_share_of_completed': gemini_completed / len(completed) if completed else None,
            'gemini_share_of_attempted': gemini_completed / len(attempted) if attempted else None,
            'definition': 'Distinct ordered translation batch IDs; multilingual output counts once; retries do not add batches.'}
        segment_ranges = {s: d.get('capture') for k, s, d in events if k == 'captured_segment'}
        states = {}
        for kind, seq, details in events:
            if kind == 'segment_state':
                states.setdefault(seq, []).append(details['state'])
        reviews = {s for k, s, d in events if k == 'segment_state' and d['state'] == 'REVIEW_REQUIRED'}
        possible_unresolved = []
        for kind, seq, details in events:
            if kind != 'audio_evidence' or seq not in reviews or not segment_ranges.get(seq):
                continue
            capture = segment_ranges[seq]
            for left, right in details.get('probable_speech_sample_ranges', []):
                possible_unresolved.append({'segment': seq, 'capture_id': capture['capture_id'],
                    'start_sample': capture['start_sample'] + left,
                    'end_sample': capture['start_sample'] + right})
        recognition_coverage = {
            'segmented': len(segment_ranges),
            'submitted': sum('SUBMITTED' in sequence for sequence in states.values()),
            'recognition_returned': sum('RECOGNIZED' in sequence for sequence in states.values()),
            'source_committed': sum(k == 'source_committed' for k, _, _ in events),
            'confirmed_silence': sum('CONFIRMED_SILENCE' in sequence for sequence in states.values()),
            'review_required': len(reviews),
            'review_kinds': {reason: len({s for k, s, d in events if k == 'recognition'
                              and reason in (d.get('uncertainty_reasons') or [])})
                for reason in sorted({r for k, _, d in events if k == 'recognition'
                                      for r in (d.get('uncertainty_reasons') or [])})},
            'failed_retained': sum('FAILED_RETAINED' in sequence for sequence in states.values()),
            'possible_unresolved_speech_ranges': possible_unresolved,
            'possible_unresolved_speech_seconds': sum(r['end_sample'] - r['start_sample'] for r in possible_unresolved) / 16000,
            'speech_completeness_verified': False,
            'missed_word_count': None,
            'limitations': 'Speech probabilities and short-output checks are uncalibrated heuristics. Committed text and sample coverage do not prove word coverage.'}
        return {'captured_samples': received, 'journaled_samples': total,
                'logical_segmented_samples': logical, 'uncovered_ranges': uncovered,
                'journal_gaps': journal_gaps, 'overlap_samples': overlap,
                'intentional_overlap_samples': 0, 'capture_warning_count': overruns,
                'capture_overrun_count': sum(s['capture_overruns'] for s in stops.values()),
                'capacity_aborts': sum(s['capacity_aborts'] for s in stops.values()),
                'unjournaled_samples': max(0, received - total),
                'recognition_submissions': sum(k == 'recognition_started' for k, _, _ in events),
                'failed_segments': len({s for k, s, _ in events if k.endswith('_failure') and s is not None}),
                'recovered_segments': sum(k == 'recovered' for k, _, _ in events),
                'hardware_completeness_verified': False,
                'translation_metrics': translation_metrics,
                'recognition_coverage': recognition_coverage,
                'realtime_usage': next((d for k, _, d in reversed(events) if k == 'realtime_usage'), None),
                'silence_gated_segments': sum(k == 'silence_gate' for k, _, _ in events)}

    def audio_range(self, capture_id, start, end):
        """Fail closed on gaps/corruption; never silently join incomplete audio."""
        db = self._connect()
        try:
            # Streaming reads many small windows during a long service. Start
            # at the one packet covering `start`, rather than scanning every
            # earlier callback in the capture. The checks below still detect gaps.
            first = db.execute('SELECT MAX(start_sample) FROM capture WHERE capture_id=? AND start_sample<=?',
                               (capture_id, start)).fetchone()[0]
            rows = db.execute('SELECT start_sample,end_sample,sha256,audio FROM capture '
                              'WHERE capture_id=? AND start_sample>=? AND end_sample>? AND start_sample<? '
                              'ORDER BY start_sample', (capture_id, first if first is not None else start, start, end)).fetchall()
        finally:
            db.close()
        cursor, parts = start, []
        for left, right, digest, raw in rows:
            samples = np.frombuffer(raw, dtype='<f4')
            if len(samples) != right - left or sample_hash(samples) != digest:
                raise ValueError('Original capture hash/length mismatch')
            if left > cursor or (parts and left < cursor):
                raise ValueError('Capture gap or unexpected overlap')
            parts.append(samples[max(0, cursor - left):min(right, end) - left])
            cursor = min(right, end)
        if cursor != end:
            raise ValueError('Requested capture range is incomplete')
        return np.concatenate(parts).copy() if parts else np.empty(0, dtype='float32')
