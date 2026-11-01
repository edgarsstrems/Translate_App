"""Expire only drained diagnostic audio after 336 hours from UTC capture.

History, raw hypotheses, audit ranges and databases survive. Derived Realtime
PCM expires first; original audio expires atomically in its own transaction.
"""
import json
import math
from pathlib import Path
import sqlite3
import time

from .recording_guard import recording_lease
from .speech_audit import SpeechAudit


def check_drained(session, active=None, strict=True):
    session = Path(session)
    if active and session.resolve() == Path(active).resolve():
        raise ValueError('Active recording is protected.')
    snapshot = json.loads((session / 'integrity.json').read_text(encoding='utf-8'))
    counts = ('capture_pending', 'translation_pending', 'synthesis_pending', 'playback_pending',
              'audio_review_items', 'yoruba_audio_pending', 'translation_review_items')
    if strict and any(name not in snapshot for name in counts):
        raise ValueError('Incomplete completion metadata is protected.')
    if snapshot.get('realtime_pending', 0) or any(snapshot.get(name, 0) != 0 for name in counts) or any(snapshot.get(name) for name in (
            'uncovered_ranges', 'unjournaled_samples', 'capture_stop_failed', 'integrity_report_failed')):
        raise ValueError('Pending, incomplete or review work is protected.')
    for path in session.glob('*.sqlite'):
        if path.is_symlink() or getattr(path.lstat(), 'st_file_attributes', 0) & 0x400:
            raise ValueError('Linked databases are protected.')
        db = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=0)
        try:
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='work'").fetchone() and db.execute(
                    'SELECT COUNT(*) FROM work').fetchone()[0]:
                raise ValueError('Durable pending/review work is protected.')
        finally:
            db.close()


def cleanup_recordings(root, active=None, now=None):
    root = Path(root)
    now = time.time() if now is None else now
    stats = dict(scanned=0, eligible=0, protected=0, deleted=0, estimated_bytes_reclaimed=0, errors=[])
    for session in root.glob('*'):
        if session.name.startswith('.') or not session.is_dir():
            continue
        stats['scanned'] += 1
        try:
            if (session.is_symlink() or getattr(session.lstat(), 'st_file_attributes', 0) & 0x400
                    or session.resolve().parent != root.resolve()):
                raise ValueError('Linked/outside recording is protected.')
            with recording_lease(session, exclusive=True):
                check_drained(session, active)
                audit = SpeechAudit.existing(session / 'speech-audit.sqlite')
                db = sqlite3.connect(audit.path, timeout=0)
                try:
                    db.execute('PRAGMA secure_delete=ON')
                    db.execute('BEGIN IMMEDIATE')
                    if db.execute("SELECT 1 FROM events WHERE kind='diagnostic_audio_expired'").fetchone():
                        continue
                    if not db.execute("SELECT 1 FROM events WHERE kind='completion'").fetchone():
                        raise ValueError('Missing completion event is protected.')
                    first = db.execute('SELECT details,start_sample,end_sample FROM capture ORDER BY rowid LIMIT 1').fetchone()
                    if not first:
                        raise ValueError('Missing original UTC capture time is protected.')
                    captured = json.loads(first[0]).get('wall_time')
                    if (not isinstance(captured, (float, int)) or not math.isfinite(captured)
                            or captured <= 0 or captured > now):
                        raise ValueError('Invalid original UTC capture time is protected.')
                    captured -= (first[2] - first[1]) / 16000
                    if now <= captured + 336 * 3600:
                        continue
                    integrity = audit.integrity_report()
                    if integrity['uncovered_ranges'] or integrity['unjournaled_samples'] or integrity['journal_gaps']:
                        raise ValueError('Uncovered capture audio is protected.')
                    stats['eligible'] += 1
                    realtime = session / 'realtime.sqlite'
                    realtime_bytes = 0
                    if realtime.exists():
                        # Remove eligible derived transport audio first. If
                        # interrupted before the main expiry commit, originals
                        # still exist and the next startup safely retries.
                        rt = sqlite3.connect(realtime, timeout=0)
                        try:
                            rt.execute('PRAGMA secure_delete=ON')
                            with rt:
                                realtime_bytes = rt.execute('SELECT COALESCE(SUM(length(pcm)),0) FROM packets').fetchone()[0]
                                rt.execute('UPDATE packets SET pcm=NULL')
                            rt.execute('PRAGMA wal_checkpoint(TRUNCATE)')
                            rt.execute('VACUUM')
                        finally:
                            rt.close()
                    size = db.execute('SELECT COALESCE(SUM(length(audio)),0) FROM capture').fetchone()[0]
                    size += realtime_bytes
                    size += db.execute('SELECT COALESCE(SUM(length(audio)),0) FROM submissions').fetchone()[0]
                    db.execute('UPDATE capture SET audio=NULL')
                    db.execute('UPDATE submissions SET audio=NULL')
                    db.execute('INSERT INTO events(kind,segment,at,details) VALUES(?,NULL,?,?)',
                               ('diagnostic_audio_expired', now, json.dumps({'captured_utc': captured, 'audio_bytes': size})))
                    db.commit()
                    stats['deleted'] += 1
                    stats['estimated_bytes_reclaimed'] += size
                    # SQLite owns WAL/shm handling. Never unlink them manually.
                    db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
                    db.execute('VACUUM')
                finally:
                    db.rollback()
                    db.close()
        except (ValueError, FileNotFoundError, json.JSONDecodeError) as exc:
            stats['protected'] += 1
        except Exception as exc:
            stats['protected'] += 1
            stats['errors'].append({'recording': session.name, 'error': type(exc).__name__})
    return stats
