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
    def __init__(self, path):
        self.path = Path(path)

    def _connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=15)
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('PRAGMA synchronous=FULL')
        db.execute('CREATE TABLE IF NOT EXISTS capture (capture_id TEXT, start_sample INTEGER, '
                   'end_sample INTEGER, sha256 TEXT, audio BLOB, details TEXT, '
                   'PRIMARY KEY(capture_id, start_sample))')
        db.execute('CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, '
                   'kind TEXT, segment INTEGER, at REAL, details TEXT)')
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
                'hardware_completeness_verified': False}

    def audio_range(self, capture_id, start, end):
        """Fail closed on gaps/corruption; never silently join incomplete audio."""
        db = self._connect()
        try:
            rows = db.execute('SELECT start_sample,end_sample,sha256,audio FROM capture '
                              'WHERE capture_id=? AND end_sample>? AND start_sample<? '
                              'ORDER BY start_sample', (capture_id, start, end)).fetchall()
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
