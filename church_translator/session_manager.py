"""Session Storage, Progressive Autosaving, and Crash Recovery.

Persists live sermon transcripts, translations, durations, and summaries atomically.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Optional, Any

from .config import app_data_dir


@dataclass
class TranscriptSegment:
    segment_id: int
    captured_at: float
    timestamp_str: str
    latvian_text: str
    english_translation: str
    secondary_language: str = ""
    secondary_translation: str = ""
    status: str = "delivered"  # delivered, skipped_tts, failed, recovering
    uncertain: bool = False
    provider: str = "gemini"
    translation_group_id: int | None = None


@dataclass
class SessionRecord:
    session_id: str
    created_at: str
    title: str
    duration_seconds: float
    state: str  # active, paused, completed, crashed
    secondary_language: str
    sermon_context_title: str
    summary_text_en: str = ""
    summary_text_lv: str = ""


class SessionManager:
    """Manages SQLite-based atomic session storage and progressive segment commits."""

    def __init__(self, db_dir: Optional[Path] = None) -> None:
        self.db_dir = db_dir or (app_data_dir() / "sessions")
        self.db_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.db_dir / "sessions.sqlite"
        self._lock = threading.RLock()
        self._init_db()
        self.current_session_id: Optional[str] = None
        self.last_completed_session_id: Optional[str] = None
        self._start_time: float = 0.0
        self._total_paused_duration: float = 0.0
        self._pause_start: float = 0.0
        self.is_paused: bool = False

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        try:
            yield conn
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._lock, self._conn() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    title TEXT NOT NULL,
                    duration_seconds REAL DEFAULT 0,
                    state TEXT NOT NULL,
                    secondary_language TEXT DEFAULT '',
                    sermon_context_title TEXT DEFAULT '',
                    summary_text_en TEXT DEFAULT '',
                    summary_text_lv TEXT DEFAULT ''
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS segments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    segment_id INTEGER NOT NULL,
                    captured_at REAL NOT NULL,
                    timestamp_str TEXT NOT NULL,
                    latvian_text TEXT NOT NULL,
                    english_translation TEXT NOT NULL,
                    secondary_language TEXT DEFAULT '',
                    secondary_translation TEXT DEFAULT '',
                    status TEXT DEFAULT 'delivered',
                    uncertain INTEGER DEFAULT 0,
                    provider TEXT DEFAULT 'gemini',
                    FOREIGN KEY(session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_segments_session ON segments(session_id)")
            if 'translation_group_id' not in {row[1] for row in conn.execute('PRAGMA table_info(segments)')}:
                conn.execute('ALTER TABLE segments ADD COLUMN translation_group_id INTEGER')
            conn.execute("CREATE TABLE IF NOT EXISTS summary_metadata (session_id TEXT, language TEXT, provider TEXT, model TEXT, PRIMARY KEY(session_id, language))")
            conn.execute('CREATE TABLE IF NOT EXISTS completion_snapshots (session_id TEXT PRIMARY KEY, ended_at TEXT, snapshot_json TEXT)')
            conn.execute('CREATE TABLE IF NOT EXISTS summary_preferences (session_id TEXT PRIMARY KEY, language TEXT NOT NULL)')
            if 'created_at' not in {row[1] for row in conn.execute('PRAGMA table_info(summary_metadata)')}:
                conn.execute("ALTER TABLE summary_metadata ADD COLUMN created_at TEXT DEFAULT ''")
            conn.commit()

    def start_session(self, title: str = "", secondary_language: str = "ru", context_title: str = "") -> str:
        with self._lock:
            # Mark any previously 'active' or 'paused' sessions as 'crashed' if still open
            with self._conn() as conn:
                conn.execute(
                    "UPDATE sessions SET state='crashed' WHERE state IN ('active', 'paused')"
                )
                conn.commit()

            self.current_session_id = uuid.uuid4().hex[:12]
            created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            session_title = title.strip() or f"Sermon {datetime.now().strftime('%Y-%m-%d %H:%M')}"
            self._start_time = time.monotonic()
            self._total_paused_duration = 0.0
            self._pause_start = 0.0
            self.is_paused = False

            with self._conn() as conn:
                conn.execute("""
                    INSERT INTO sessions (session_id, created_at, title, duration_seconds, state, secondary_language, sermon_context_title)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (self.current_session_id, created_at, session_title, 0.0, "active", secondary_language, context_title))
                conn.commit()

            return self.current_session_id

    def pause_session(self) -> None:
        with self._lock:
            if not self.current_session_id or self.is_paused:
                return
            self.is_paused = True
            self._pause_start = time.monotonic()
            self._update_state_in_db("paused")

    def resume_session(self) -> None:
        with self._lock:
            if not self.current_session_id or not self.is_paused:
                return
            self.is_paused = False
            if self._pause_start > 0:
                self._total_paused_duration += time.monotonic() - self._pause_start
                self._pause_start = 0.0
            self._update_state_in_db("active")

    def end_session(self, completion_info: Optional[dict] = None) -> None:
        with self._lock:
            if not self.current_session_id:
                return
            duration = self.get_current_duration()
            with self._conn() as conn:
                conn.execute('UPDATE sessions SET state=?, duration_seconds=? WHERE session_id=?',
                             ('completed', duration, self.current_session_id))
                segments = conn.execute('SELECT COUNT(*), SUM(uncertain) FROM segments WHERE session_id=?',
                                        (self.current_session_id,)).fetchone()
                snapshot = {'segment_count': segments[0], 'uncertain_segments': segments[1] or 0,
                            'duration_seconds': duration,
                            'queues': completion_info if isinstance(completion_info, dict) else {}}
                conn.execute('INSERT OR REPLACE INTO completion_snapshots VALUES (?, ?, ?)',
                             (self.current_session_id, datetime.now().isoformat(), json.dumps(snapshot)))
                conn.commit()
            self.last_completed_session_id = self.current_session_id
            self.current_session_id = None
            self.is_paused = False

    def summary_session_id(self) -> Optional[str]:
        """Current sermon, then the explicitly ended sermon, including after restart."""
        with self._lock:
            if self.current_session_id:
                return self.current_session_id
            if self.last_completed_session_id and self.get_session(self.last_completed_session_id):
                return self.last_completed_session_id
            with self._conn() as conn:
                row = conn.execute("SELECT session_id FROM sessions WHERE state='completed' ORDER BY rowid DESC LIMIT 1").fetchone()
            return row[0] if row else None

    def completion_snapshot(self, session_id: str) -> dict:
        with self._lock, self._conn() as conn:
            row = conn.execute('SELECT snapshot_json FROM completion_snapshots WHERE session_id=?',
                               (session_id,)).fetchone()
        return json.loads(row[0]) if row else {}

    def get_current_duration(self) -> float:
        if not self._start_time:
            return 0.0
        now = time.monotonic()
        paused = self._total_paused_duration
        if self.is_paused and self._pause_start > 0:
            paused += (now - self._pause_start)
        return max(0.0, (now - self._start_time) - paused)

    def _update_state_in_db(self, state: str, duration: Optional[float] = None) -> None:
        if not self.current_session_id:
            return
        if duration is None:
            duration = self.get_current_duration()
        with self._conn() as conn:
            conn.execute(
                "UPDATE sessions SET state=?, duration_seconds=? WHERE session_id=?",
                (state, duration, self.current_session_id)
            )
            conn.commit()

    def append_segment(self, segment: TranscriptSegment, session_id: Optional[str] = None) -> None:
        with self._lock:
            if not session_id and not self.current_session_id:
                # Auto-initialize session if none active
                self.start_session()
            target_session = session_id or self.current_session_id
            with self._conn() as conn:
                # Retries must not duplicate the finalized source segment.
                exists = conn.execute("SELECT 1 FROM segments WHERE session_id=? AND segment_id=?",
                                      (target_session, segment.segment_id)).fetchone()
                if exists:
                    return
                conn.execute("""
                    INSERT INTO segments (
                        session_id, segment_id, captured_at, timestamp_str,
                        latvian_text, english_translation, secondary_language,
                        secondary_translation, status, uncertain, provider
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    target_session,
                    segment.segment_id,
                    segment.captured_at,
                    segment.timestamp_str,
                    segment.latvian_text,
                    segment.english_translation,
                    segment.secondary_language,
                    segment.secondary_translation,
                    segment.status,
                    1 if segment.uncertain else 0,
                    segment.provider,
                ))
                # Update duration progressively
                conn.execute(
                    "UPDATE sessions SET duration_seconds=? WHERE session_id=?",
                    (self.get_current_duration(), self.current_session_id)
                )
                conn.commit()

    def update_segment_translation(self, segment_id: int, english: str, secondary: str = "", provider: str = "gemini", session_id: Optional[str] = None) -> None:
        with self._lock:
            target_session = session_id or self.current_session_id
            if not target_session:
                return
            with self._conn() as conn:
                conn.execute("""
                    UPDATE segments
                    SET english_translation=?, secondary_translation=?, provider=?, status='delivered', translation_group_id=NULL
                    WHERE session_id=? AND segment_id=?
                """, (english, secondary, provider, target_session, segment_id))
                conn.commit()

    def update_translation_group(self, source_ids: list[int], english: str, secondary: str = "",
                                 provider: str = "gemini", session_id: Optional[str] = None) -> None:
        """Persist a batched translation and its source ownership atomically."""
        if not source_ids:
            return
        with self._lock, self._conn() as conn:
            target_session = session_id or self.current_session_id
            if not target_session:
                return
            anchor = source_ids[0]
            for index, source_id in enumerate(source_ids):
                conn.execute('''UPDATE segments SET english_translation=?, secondary_translation=?,
                    provider=?, status=?, translation_group_id=? WHERE session_id=? AND segment_id=?''',
                    (english if index == 0 else '', secondary if index == 0 else '', provider,
                     'delivered' if index == 0 else 'translated_in_group', anchor, target_session, source_id))
            conn.commit()

    def update_session_title(self, session_id: str, new_title: str) -> None:
        with self._lock:
            with self._conn() as conn:
                conn.execute(
                    "UPDATE sessions SET title=? WHERE session_id=?",
                    (new_title.strip(), session_id)
                )
                conn.commit()

    def save_session_summary(self, session_id: str, summary_text: str, language: str = "en") -> None:
        with self._lock:
            col = "summary_text_lv" if language == "lv" else "summary_text_en"
            with self._conn() as conn:
                conn.execute(f"UPDATE sessions SET {col}=? WHERE session_id=?", (summary_text, session_id))
                conn.execute('INSERT OR REPLACE INTO summary_preferences VALUES (?, ?)', (session_id, language))
                conn.commit()

    def set_summary_language(self, session_id: str, language: str) -> None:
        if language not in {'lv', 'en'}:
            raise ValueError('Unsupported summary language')
        with self._lock, self._conn() as conn:
            conn.execute('INSERT OR REPLACE INTO summary_preferences VALUES (?, ?)', (session_id, language))
            conn.commit()

    def get_summary_language(self, session_id: str) -> str:
        with self._lock, self._conn() as conn:
            row = conn.execute('SELECT language FROM summary_preferences WHERE session_id=?', (session_id,)).fetchone()
            if row and row[0] in {'lv', 'en'}:
                return row[0]
            # Migrate older summaries without applying another session's choice.
            row = conn.execute('SELECT language FROM summary_metadata WHERE session_id=? ORDER BY created_at DESC, rowid DESC LIMIT 1', (session_id,)).fetchone()
            if row and row[0] in {'lv', 'en'}:
                return row[0]
            row = conn.execute('SELECT summary_text_lv, summary_text_en FROM sessions WHERE session_id=?', (session_id,)).fetchone()
            return 'en' if row and row[1] and not row[0] else 'lv'

    def list_sessions(self) -> List[SessionRecord]:
        with self._lock, self._conn() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT * FROM sessions ORDER BY created_at DESC").fetchall()
            return [
                SessionRecord(
                    session_id=row["session_id"],
                    created_at=row["created_at"],
                    title=row["title"],
                    duration_seconds=row["duration_seconds"],
                    state=row["state"],
                    secondary_language=row["secondary_language"],
                    sermon_context_title=row["sermon_context_title"],
                    summary_text_en=row["summary_text_en"] or "",
                    summary_text_lv=row["summary_text_lv"] or "",
                )
                for row in rows
            ]

    def save_summary_metadata(self, session_id, language, provider, model):
        with self._lock, self._conn() as conn:
            conn.execute("INSERT OR REPLACE INTO summary_metadata (session_id, language, provider, model, created_at) VALUES (?, ?, ?, ?, ?)",
                         (session_id, language, provider, model, datetime.now().isoformat(timespec='seconds')))
            conn.commit()

    def get_summary_metadata(self, session_id, language):
        with self._lock, self._conn() as conn:
            row = conn.execute("SELECT provider, model, created_at FROM summary_metadata WHERE session_id=? AND language=?", (session_id, language)).fetchone()
            return {"provider": row[0], "model": row[1], "created_at": row[2]} if row else {}

    def edit_transcript(self, session_id, changes):
        with self._lock, self._conn() as conn:
            for segment_id, text in changes.items():
                conn.execute("UPDATE segments SET latvian_text=? WHERE session_id=? AND segment_id=?", (text.strip(), session_id, segment_id))
            conn.execute("UPDATE sessions SET summary_text_en='', summary_text_lv='' WHERE session_id=?", (session_id,))
            conn.execute("DELETE FROM summary_metadata WHERE session_id=?", (session_id,))
            conn.commit()

    def get_session(self, session_id: str) -> Optional[SessionRecord]:
        with self._lock, self._conn() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM sessions WHERE session_id=?", (session_id,)).fetchone()
            if not row:
                return None
            return SessionRecord(
                session_id=row["session_id"],
                created_at=row["created_at"],
                title=row["title"],
                duration_seconds=row["duration_seconds"],
                state=row["state"],
                secondary_language=row["secondary_language"],
                sermon_context_title=row["sermon_context_title"],
                summary_text_en=row["summary_text_en"] or "",
                summary_text_lv=row["summary_text_lv"] or "",
            )

    def get_segments(self, session_id: str) -> List[TranscriptSegment]:
        with self._lock, self._conn() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM segments WHERE session_id=? ORDER BY segment_id ASC, id ASC",
                (session_id,)
            ).fetchall()
            return [
                TranscriptSegment(
                    segment_id=row["segment_id"],
                    captured_at=row["captured_at"],
                    timestamp_str=row["timestamp_str"],
                    latvian_text=row["latvian_text"],
                    english_translation=row["english_translation"],
                    secondary_language=row["secondary_language"],
                    secondary_translation=row["secondary_translation"],
                    status=row["status"],
                    uncertain=bool(row["uncertain"]),
                    provider=row["provider"],
                    translation_group_id=row["translation_group_id"],
                )
                for row in rows
            ]

    def delete_session(self, session_id: str) -> None:
        with self._lock, self._conn() as conn:
            conn.execute("DELETE FROM segments WHERE session_id=?", (session_id,))
            conn.execute("DELETE FROM sessions WHERE session_id=?", (session_id,))
            conn.execute('DELETE FROM completion_snapshots WHERE session_id=?', (session_id,))
            conn.execute('DELETE FROM summary_preferences WHERE session_id=?', (session_id,))
            conn.execute('DELETE FROM summary_metadata WHERE session_id=?', (session_id,))
            conn.commit()

    def get_most_recent_crashed_session(self) -> Optional[SessionRecord]:
        """Check if the most recent session ended abruptly in 'crashed' or 'active' state."""
        sessions = self.list_sessions()
        if not sessions:
            return None
        latest = sessions[0]
        if latest.state in ("crashed", "active", "paused"):
            # Check if it has segments
            segments = self.get_segments(latest.session_id)
            if segments:
                return latest
        return None
