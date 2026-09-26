"""Disk-backed, single-consumer FIFO with explicit acknowledgement.

Only an in-flight item is decoded in memory. Failed work remains on disk; never
deserialize executable objects from a recovery file.
"""
from __future__ import annotations

import base64
import dataclasses
import json
import queue
import sqlite3
import threading
import time
from pathlib import Path

import numpy as np


class DurableQueue:
    def __init__(self, path: Path, item_type=None):
        self.path = path
        self.item_type = item_type
        path.parent.mkdir(parents=True, exist_ok=True)
        self._condition = threading.Condition()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("CREATE TABLE IF NOT EXISTS work (id INTEGER PRIMARY KEY, payload TEXT NOT NULL)")
        self._db.commit()
        self._pending = None
        self._finished = False

    @staticmethod
    def _encode(value):
        if dataclasses.is_dataclass(value):
            return dataclasses.asdict(value)
        if isinstance(value, np.ndarray):
            return {"__audio__": base64.b64encode(value.astype('<f4').tobytes()).decode('ascii')}
        if isinstance(value, bytes):
            return {"__bytes__": base64.b64encode(value).decode('ascii')}
        raise TypeError(type(value).__name__)

    @staticmethod
    def _decode(value):
        if "__audio__" in value:
            return np.frombuffer(base64.b64decode(value["__audio__"]), dtype='<f4').copy()
        if "__bytes__" in value:
            return base64.b64decode(value["__bytes__"])
        return value

    def put(self, item):
        if item is None:
            self.finish()
            return
        payload = json.dumps(item, default=self._encode, ensure_ascii=False)
        with self._condition:
            if self._finished:
                raise RuntimeError("Cannot append to finished backlog")
            self._db.execute("INSERT INTO work(payload) VALUES (?)", (payload,))
            self._db.commit()
            self._condition.notify()

    put_nowait = put

    def get(self, block=True, timeout=None):
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._condition:
            if self._pending is not None:
                raise RuntimeError("Acknowledge previous work before reading next item")
            while True:
                row = self._db.execute("SELECT id, payload FROM work ORDER BY id LIMIT 1").fetchone()
                if row:
                    self._pending = row[0]
                    value = json.loads(row[1], object_hook=self._decode)
                    return self.item_type(**value) if self.item_type else value
                if self._finished:
                    return None
                remaining = None if deadline is None else deadline - time.monotonic()
                if not block or (remaining is not None and remaining <= 0):
                    raise queue.Empty
                self._condition.wait(remaining)

    def ack(self):
        with self._condition:
            if self._pending is not None:
                self._db.execute("DELETE FROM work WHERE id=?", (self._pending,))
                self._db.commit()
                self._pending = None

    def coalesce_pending(self, combine, max_items=3):
        """Atomically combine consecutive waiting text without losing crash recovery."""
        with self._condition:
            if self._pending is None:
                raise RuntimeError("No pending work")
            rows = self._db.execute("SELECT id, payload FROM work WHERE id>=? ORDER BY id LIMIT ?",
                                    (self._pending, max_items)).fetchall()
            def decode(payload):
                value = json.loads(payload, object_hook=self._decode)
                return self.item_type(**value) if self.item_type else value
            merged = decode(rows[0][1])
            removed = []
            for ident, payload in rows[1:]:
                candidate = combine(merged, decode(payload))
                if candidate is None:
                    break
                merged = candidate
                removed.append((ident,))
            if removed:
                with self._db:
                    self._db.execute("UPDATE work SET payload=? WHERE id=?",
                                     (json.dumps(merged, default=self._encode, ensure_ascii=False), self._pending))
                    self._db.executemany("DELETE FROM work WHERE id=?", removed)
            return merged

    def update_pending(self, item):
        payload = json.dumps(item, default=self._encode, ensure_ascii=False)
        with self._condition:
            if self._pending is None:
                raise RuntimeError("No pending work")
            self._db.execute("UPDATE work SET payload=? WHERE id=?", (payload, self._pending))
            self._db.commit()

    def finish(self):
        with self._condition:
            self._finished = True
            self._condition.notify_all()

    def qsize(self):
        with self._condition:
            return self._db.execute("SELECT COUNT(*) FROM work").fetchone()[0]

    def close(self):
        with self._condition:
            empty = self.qsize() == 0
            self._db.close()
        if empty:
            self.path.unlink(missing_ok=True)
