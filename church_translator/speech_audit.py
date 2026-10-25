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
        data = np.asarray(audio, dtype='<f4')
        db = self._connect()
        try:
            with db:
                db.execute('INSERT INTO capture VALUES (?, ?, ?, ?, ?, ?)',
                           (capture_id, start, start + len(data), sample_hash(data),
                            data.tobytes(), json.dumps(details, ensure_ascii=False)))
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
