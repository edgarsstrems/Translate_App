"""Cross-process recording leases using SQLite's Windows/POSIX file locks.

Readers (capture/export) coexist; cleanup needs an exclusive lease. The tiny
lock database lives outside the recording, so manual deletion is safe too.
OS locks disappear after a crash; no stale PID or time-based unlock guesses.
"""
from contextlib import contextmanager
from pathlib import Path
import sqlite3


@contextmanager
def recording_lease(session, exclusive=False):
    original = Path(session).absolute()
    if original.is_symlink() or (original.exists() and getattr(original.lstat(), 'st_file_attributes', 0) & 0x400):
        raise ValueError('Linked recordings cannot be leased.')
    session = original.resolve()
    root = session.parent / '.recording-locks'
    root.mkdir(exist_ok=True)
    path = root / (session.name + '.sqlite')
    if root.is_symlink() or getattr(root.lstat(), 'st_file_attributes', 0) & 0x400 or path.is_symlink():
        raise ValueError('Linked recording locks are not allowed.')
    db = sqlite3.connect(path, timeout=0, check_same_thread=False)
    try:
        try:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='lease'").fetchone():
                db.execute('CREATE TABLE IF NOT EXISTS lease (id INTEGER)')
                db.commit()
            db.execute('BEGIN EXCLUSIVE' if exclusive else 'BEGIN')
            db.execute('SELECT * FROM lease').fetchall()
        except sqlite3.OperationalError as exc:
            raise ValueError('Recording is in use or its protection lock is unavailable.') from exc
        yield
    finally:
        db.rollback()
        db.close()
