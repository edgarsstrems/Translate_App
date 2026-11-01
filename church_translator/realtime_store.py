"""Durable Realtime transport ledger. Original PCM remains in SpeechAudit.

`work` is deliberately compatible with recovery export and retention guards.
Transport retries never erase raw events or possibly submitted packet bytes.
"""
import json
import sqlite3
import threading
import time
from pathlib import Path


class RealtimeStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.condition = threading.Condition(threading.RLock())
        self.db = sqlite3.connect(self.path, check_same_thread=False, timeout=15)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS streams (
                capture_id TEXT PRIMARY KEY, available_end INTEGER, origin REAL, sealed INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS turns (
                id INTEGER PRIMARY KEY, capture_id TEXT, start INTEGER, end INTEGER,
                sequence_id INTEGER, captured_at REAL, state TEXT DEFAULT 'open', attempts INTEGER DEFAULT 0,
                consumed INTEGER DEFAULT 0, generation INTEGER, item_id TEXT, text TEXT, error TEXT,
                final_at REAL, UNIQUE(capture_id,start));
            CREATE TABLE IF NOT EXISTS work (id INTEGER PRIMARY KEY, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS packets (
                id INTEGER PRIMARY KEY, turn_id INTEGER, generation INTEGER, start INTEGER, end INTEGER,
                pcm BLOB, pcm_samples INTEGER, sent INTEGER DEFAULT 0, metadata TEXT);
            CREATE TABLE IF NOT EXISTS bindings (
                generation INTEGER, item_id TEXT, turn_id INTEGER, PRIMARY KEY(generation,item_id));
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY, generation INTEGER, item_id TEXT, turn_id INTEGER,
                received_at REAL, payload TEXT);
            CREATE INDEX IF NOT EXISTS packets_turn ON packets(turn_id,id);
            CREATE INDEX IF NOT EXISTS events_turn ON events(turn_id,id);
        ''')
        self.db.commit()

    def _work(self, row):
        if row['consumed'] and row['state'] in ('final', 'failed'):
            self.db.execute('DELETE FROM work WHERE id=?', (row['id'],))
            return
        stream = self.db.execute('SELECT available_end FROM streams WHERE capture_id=?', (row['capture_id'],)).fetchone()
        payload = {'stage': 'realtime', 'sequence_id': row['sequence_id'], 'state': row['state'],
                   'capture': {'capture_id': row['capture_id'], 'start_sample': row['start'],
                               'end_sample': row['end'] if row['end'] is not None else (stream[0] if stream else row['start']),
                               'sample_rate': 16000}, 'captured_at': row['captured_at'],
                   'recognized_text': row['text'], 'error': row['error'],
                   'note': 'Original PCM is in speech-audit.sqlite. Partials are never source. Do not auto-replay.'}
        self.db.execute('INSERT OR REPLACE INTO work VALUES(?,?)',
                        (row['id'], json.dumps(payload, ensure_ascii=False)))

    def _row(self, ident):
        return self.db.execute('SELECT * FROM turns WHERE id=?', (ident,)).fetchone()

    def feed(self, capture_id, start, end, details):
        with self.condition, self.db:
            stream = self.db.execute('SELECT * FROM streams WHERE capture_id=?', (capture_id,)).fetchone()
            if stream is None:
                if start != 0:
                    raise ValueError('Realtime capture must start at the first journaled sample')
                origin = details.get('captured_at', time.monotonic()) - start / 16000
                self.db.execute('INSERT INTO streams(capture_id,available_end,origin) VALUES(?,?,?)',
                                (capture_id, 0, origin))
                stream = self.db.execute('SELECT * FROM streams WHERE capture_id=?', (capture_id,)).fetchone()
            if start != stream['available_end'] or stream['sealed'] or end <= start:
                raise ValueError('Realtime journal availability gap/overlap or sealed capture')
            self.db.execute('UPDATE streams SET available_end=? WHERE capture_id=?', (end, capture_id))
            left = self.db.execute('SELECT COALESCE(MAX(end),0) FROM turns WHERE capture_id=?', (capture_id,)).fetchone()[0]
            if end > left:
                self.db.execute('INSERT OR IGNORE INTO turns(capture_id,start,captured_at) VALUES(?,?,?)',
                                (capture_id, left, stream['origin'] + left / 16000))
                self._work(self.db.execute('SELECT * FROM turns WHERE capture_id=? AND start=?', (capture_id, left)).fetchone())
            self.condition.notify_all()

    def register(self, sequence, capture, captured_at):
        with self.condition, self.db:
            key = (capture['capture_id'], capture['start_sample'])
            row = self.db.execute('SELECT * FROM turns WHERE capture_id=? AND start=?', key).fetchone()
            if row and row['end'] is not None and (row['end'] != capture['end_sample'] or row['sequence_id'] != sequence):
                raise ValueError('Conflicting Realtime turn ownership')
            self.db.execute('INSERT INTO turns(capture_id,start,end,sequence_id,captured_at) VALUES(?,?,?,?,?) '
                            'ON CONFLICT(capture_id,start) DO UPDATE SET end=excluded.end, '
                            'sequence_id=excluded.sequence_id,captured_at=excluded.captured_at',
                            (*key, capture['end_sample'], sequence, captured_at))
            row = self.db.execute('SELECT * FROM turns WHERE capture_id=? AND start=?', key).fetchone()
            self._work(row)
            self.condition.notify_all()
            return dict(row)

    def seal(self, capture_id):
        with self.condition, self.db:
            self.db.execute('UPDATE streams SET sealed=1 WHERE capture_id=?', (capture_id,))
            # A failed segment handoff must not manufacture a source segment.
            for row in self.db.execute('SELECT * FROM turns WHERE capture_id=? AND end IS NULL', (capture_id,)).fetchall():
                self.db.execute("UPDATE turns SET state='failed',error='Unsegmented original tail retained' WHERE id=?", (row['id'],))
                self._work(self._row(row['id']))
            self.condition.notify_all()

    def next_turn(self):
        with self.condition:
            row = self.db.execute("SELECT t.*,s.available_end,s.sealed FROM turns t JOIN streams s USING(capture_id) "
                                  "WHERE t.state NOT IN ('final','failed','committed') ORDER BY t.id LIMIT 1").fetchone()
            return dict(row) if row else None

    def begin(self, ident, generation):
        with self.condition, self.db:
            self.db.execute("UPDATE turns SET state='sending',attempts=attempts+1,generation=?,item_id=NULL WHERE id=?",
                            (generation, ident))
            self._work(self._row(ident))

    def committed(self, ident):
        with self.condition, self.db:
            self.db.execute("UPDATE turns SET state='committed' WHERE id=?", (ident,))
            self._work(self._row(ident))
            self.condition.notify_all()

    def bind(self, generation, item_id, ident):
        with self.condition, self.db:
            self.db.execute('INSERT INTO bindings VALUES(?,?,?)', (generation, item_id, ident))
            self.db.execute('UPDATE turns SET item_id=? WHERE id=? AND generation=?', (item_id, ident, generation))
            self.db.execute('UPDATE events SET turn_id=? WHERE generation=? AND item_id=?', (ident, generation, item_id))

    def item(self, generation, item_id):
        with self.condition:
            row = self.db.execute('SELECT t.* FROM turns t JOIN bindings b ON b.turn_id=t.id '
                                  'WHERE b.generation=? AND b.item_id=?', (generation, item_id)).fetchone()
            return dict(row) if row else None

    def event(self, generation, event):
        with self.condition, self.db:
            item_id = event.get('item_id')
            row = self.item(generation, item_id) if item_id else None
            self.db.execute('INSERT INTO events(generation,item_id,turn_id,received_at,payload) VALUES(?,?,?,?,?)',
                            (generation, item_id, row['id'] if row else None, time.monotonic(),
                             json.dumps(event, ensure_ascii=False)))

    def packet(self, turn, generation, start, end, pcm, metadata):
        with self.condition, self.db:
            return self.db.execute('INSERT INTO packets(turn_id,generation,start,end,pcm,pcm_samples,metadata) '
                                   'VALUES(?,?,?,?,?,?,?)', (turn, generation, start, end, pcm, len(pcm) // 2,
                                                           json.dumps(metadata, ensure_ascii=False))).lastrowid

    def sent(self, receipt):
        with self.condition, self.db:
            self.db.execute('UPDATE packets SET sent=1 WHERE id=?', (receipt,))

    def complete(self, ident, text):
        with self.condition, self.db:
            row = self._row(ident)
            if row['state'] == 'failed':
                return False
            if row['state'] == 'final':
                if row['text'] != text and not row['consumed']:
                    self.db.execute("UPDATE turns SET state='failed',error='Conflicting final hypotheses retained' WHERE id=?", (ident,))
                    self._work(self._row(ident))
                    self.condition.notify_all()
                return False
            self.db.execute("UPDATE turns SET state='final',text=?,final_at=? WHERE id=?", (text, time.monotonic(), ident))
            self._work(self._row(ident))
            self.condition.notify_all()
            return True

    def fail(self, ident, error):
        with self.condition, self.db:
            self.db.execute("UPDATE turns SET state='failed',error=? WHERE id=? AND state!='final'", (error, ident))
            self._work(self._row(ident))
            self.condition.notify_all()

    def reset_connection(self, max_attempts, error):
        with self.condition, self.db:
            for row in self.db.execute("SELECT * FROM turns WHERE state IN ('sending','committed')").fetchall():
                if row['attempts'] >= max_attempts:
                    self.db.execute("UPDATE turns SET state='failed',error=? WHERE id=?", (error, row['id']))
                else:
                    self.db.execute("UPDATE turns SET state='open',error=? WHERE id=?", (error, row['id']))
                self._work(self._row(row['id']))
            self.condition.notify_all()

    def fail_pending(self, error):
        with self.condition, self.db:
            for row in self.db.execute("SELECT * FROM turns WHERE state NOT IN ('final','failed')").fetchall():
                self.db.execute("UPDATE turns SET state='failed',error=? WHERE id=?", (error, row['id']))
                self._work(self._row(row['id']))
            self.condition.notify_all()

    def result(self, capture, timeout=90):
        deadline = time.monotonic() + timeout
        with self.condition:
            while True:
                row = self.db.execute('SELECT * FROM turns WHERE capture_id=? AND start=?',
                                      (capture['capture_id'], capture['start_sample'])).fetchone()
                if row and row['state'] in ('final', 'failed'):
                    return dict(row)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    if row:
                        self.fail(row['id'], 'Realtime final timed out; original audio retained')
                        return dict(self._row(row['id']))
                    raise RuntimeError('Realtime turn was not registered; original capture remains pending')
                self.condition.wait(min(.2, remaining))

    def consume(self, capture):
        with self.condition, self.db:
            self.db.execute('UPDATE turns SET consumed=1 WHERE capture_id=? AND start=?',
                            (capture['capture_id'], capture['start_sample']))
            row = self.db.execute('SELECT * FROM turns WHERE capture_id=? AND start=?',
                                  (capture['capture_id'], capture['start_sample'])).fetchone()
            if row:
                self._work(row)

    def pending_network(self):
        with self.condition:
            return self.db.execute("SELECT COUNT(*) FROM turns WHERE state NOT IN ('final','failed')").fetchone()[0]

    def qsize(self):
        with self.condition:
            return self.db.execute('SELECT COUNT(*) FROM work').fetchone()[0]

    def usage(self):
        with self.condition:
            samples, written = self.db.execute('SELECT COALESCE(SUM(pcm_samples),0), '
                'COALESCE(SUM(CASE WHEN sent=1 THEN pcm_samples ELSE 0 END),0) FROM packets').fetchone()
        return {'potentially_submitted_seconds': samples / 24000, 'successful_socket_write_seconds': written / 24000,
                'estimated_potential_cost_usd': samples / 24000 / 60 * .017,
                'estimate_includes_replays_and_padding': True, 'billing_verified': False}

    def close(self):
        with self.condition:
            self.db.close()
