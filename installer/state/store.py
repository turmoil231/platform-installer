"""
installer/state/store.py

Persistent phase-state store backed by a local SQLite database.
Allows the installer to resume from a failed phase without re-running
completed work, and provides a full audit trail of every phase run.

Schema
──────
  phases
    id           INTEGER PRIMARY KEY
    name         TEXT NOT NULL
    status       TEXT NOT NULL   (pending | running | complete | failed | skipped)
    started_at   TEXT
    finished_at  TEXT
    rc           INTEGER
    message      TEXT            (error message or success note)
    attempt      INTEGER DEFAULT 1

  events
    id           INTEGER PRIMARY KEY
    phase_name   TEXT NOT NULL
    ts           TEXT NOT NULL
    level        TEXT            (info | warn | error)
    message      TEXT
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator


class PhaseStatus:
    PENDING  = "pending"
    RUNNING  = "running"
    COMPLETE = "complete"
    FAILED   = "failed"
    SKIPPED  = "skipped"


class PhaseRecord:
    def __init__(self, row: sqlite3.Row):
        self.name:        str       = row["name"]
        self.status:      str       = row["status"]
        self.started_at:  str | None = row["started_at"]
        self.finished_at: str | None = row["finished_at"]
        self.rc:          int | None = row["rc"]
        self.message:     str | None = row["message"]
        self.attempt:     int       = row["attempt"]

    @property
    def is_complete(self) -> bool:
        return self.status == PhaseStatus.COMPLETE

    @property
    def is_failed(self) -> bool:
        return self.status == PhaseStatus.FAILED

    def __repr__(self) -> str:
        return f"<PhaseRecord {self.name} status={self.status}>"


class StateStore:
    """
    SQLite-backed state store.

    Usage
    ─────
        store = StateStore("/var/lib/platform-installer/state.db")
        store.initialize(PHASE_NAMES)

        with store.phase_context("bootstrap") as phase:
            # phase.record is updated automatically on __exit__
            runner.run_playbook("bootstrap.yml")
    """

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    def initialize(self, phase_names: list[str]) -> None:
        """Create tables and insert pending rows for any phase not yet recorded."""
        with self._conn() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS phases (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    name        TEXT NOT NULL UNIQUE,
                    status      TEXT NOT NULL DEFAULT 'pending',
                    started_at  TEXT,
                    finished_at TEXT,
                    rc          INTEGER,
                    message     TEXT,
                    attempt     INTEGER DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS events (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    phase_name  TEXT NOT NULL,
                    ts          TEXT NOT NULL,
                    level       TEXT NOT NULL DEFAULT 'info',
                    message     TEXT
                );
            """)
            for name in phase_names:
                conn.execute(
                    "INSERT OR IGNORE INTO phases (name, status) VALUES (?, ?)",
                    (name, PhaseStatus.PENDING),
                )

    def get_phase(self, name: str) -> PhaseRecord | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM phases WHERE name = ?", (name,)
            ).fetchone()
        return PhaseRecord(row) if row else None

    def all_phases(self) -> list[PhaseRecord]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM phases ORDER BY id").fetchall()
        return [PhaseRecord(r) for r in rows]

    def mark_running(self, name: str) -> None:
        now = self._now()
        with self._conn() as conn:
            conn.execute(
                """UPDATE phases
                   SET status = ?, started_at = ?, finished_at = NULL,
                       rc = NULL, attempt = attempt + 1
                   WHERE name = ?""",
                (PhaseStatus.RUNNING, now, name),
            )

    def mark_complete(self, name: str, message: str = "") -> None:
        self._finish(name, PhaseStatus.COMPLETE, rc=0, message=message)

    def mark_failed(self, name: str, rc: int = 1, message: str = "") -> None:
        self._finish(name, PhaseStatus.FAILED, rc=rc, message=message)

    def mark_skipped(self, name: str, message: str = "") -> None:
        self._finish(name, PhaseStatus.SKIPPED, rc=0, message=message)

    def reset_phase(self, name: str) -> None:
        """Reset a phase back to pending so it will re-run."""
        with self._conn() as conn:
            conn.execute(
                "UPDATE phases SET status = ?, started_at = NULL, finished_at = NULL, rc = NULL, message = NULL WHERE name = ?",
                (PhaseStatus.PENDING, name),
            )

    def log_event(self, phase_name: str, message: str, level: str = "info") -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO events (phase_name, ts, level, message) VALUES (?, ?, ?, ?)",
                (phase_name, self._now(), level, message),
            )

    def events_for(self, phase_name: str) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT ts, level, message FROM events WHERE phase_name = ? ORDER BY id",
                (phase_name,),
            ).fetchall()
        return [dict(r) for r in rows]

    @contextmanager
    def phase_context(self, name: str) -> Iterator["PhaseContext"]:
        """
        Context manager that marks the phase running on entry and
        complete/failed on exit based on whether an exception was raised.

            with store.phase_context("bootstrap") as ctx:
                result = runner.run_playbook("bootstrap.yml")
                if not result.success:
                    ctx.fail(rc=result.rc, message="Playbook failed")
        """
        self.mark_running(name)
        ctx = PhaseContext(name=name, store=self)
        try:
            yield ctx
            if not ctx._manually_resolved:
                self.mark_complete(name)
        except Exception as exc:
            if not ctx._manually_resolved:
                self.mark_failed(name, rc=1, message=str(exc))
            raise

    # ── Internals ──────────────────────────────────────────────────────────────

    def _finish(self, name: str, status: str, rc: int, message: str) -> None:
        now = self._now()
        with self._conn() as conn:
            conn.execute(
                "UPDATE phases SET status = ?, finished_at = ?, rc = ?, message = ? WHERE name = ?",
                (status, now, rc, message, name),
            )

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()


class PhaseContext:
    """Returned by StateStore.phase_context().  Call fail() to mark failure."""

    def __init__(self, name: str, store: StateStore):
        self.name               = name
        self._store             = store
        self._manually_resolved = False

    def fail(self, rc: int = 1, message: str = "") -> None:
        self._store.mark_failed(self.name, rc=rc, message=message)
        self._manually_resolved = True

    def skip(self, message: str = "") -> None:
        self._store.mark_skipped(self.name, message=message)
        self._manually_resolved = True

    def complete(self, message: str = "") -> None:
        self._store.mark_complete(self.name, message=message)
        self._manually_resolved = True

    def log(self, message: str, level: str = "info") -> None:
        self._store.log_event(self.name, message, level)
