"""Append-only event log — the sole source of truth (ARCHITECTURE.md §8/§12, event-sourcing
pattern). `task_state` rows are a derived, rebuildable view (see task_state.py/replay.py);
this module never updates or deletes an event row.

Uses stdlib `sqlite3` synchronously (not aiosqlite): every call here is a small local-disk
write/read, there is exactly one writer (this process, one task at a time), and WAL mode
already gives us the durability/concurrency properties we need — an async driver would add
a dependency without a correctness or throughput benefit at this scale.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel

_SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"


class EventType(str, Enum):
    TASK_CREATED = "TASK_CREATED"
    OBSERVATION = "OBSERVATION"
    MODEL_DECISION = "MODEL_DECISION"
    ACTION_INTENT = "ACTION_INTENT"
    ACTION_RESULT = "ACTION_RESULT"
    VERIFICATION_RESULT = "VERIFICATION_RESULT"
    RECOVERY_TRANSITION = "RECOVERY_TRANSITION"
    SUBGOAL_CHANGED = "SUBGOAL_CHANGED"
    TASK_COMPLETED = "TASK_COMPLETED"
    TASK_BLOCKED = "TASK_BLOCKED"
    CHECKPOINT = "CHECKPOINT"
    COMPACTION_STARTED = "COMPACTION_STARTED"
    SUMMARY_CREATED = "SUMMARY_CREATED"
    COMPACTION_COMMITTED = "COMPACTION_COMMITTED"
    WORKSPACE_MUTATED = "WORKSPACE_MUTATED"


class Event(BaseModel):
    id: Optional[int] = None
    task_id: str
    step: int
    timestamp: str
    type: EventType
    payload: dict[str, Any]
    verification_result: Optional[dict[str, Any]] = None


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class EventStore:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(db_path))
        self.conn.row_factory = sqlite3.Row
        with open(_SCHEMA_PATH, "r", encoding="utf-8") as f:
            self.conn.executescript(f.read())
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def create_task(self, task_id: str, goal: str, success_criteria: list[str]) -> None:
        self.conn.execute(
            "INSERT INTO tasks (id, created_at, status, goal, success_criteria) VALUES (?, ?, ?, ?, ?)",
            (task_id, now_iso(), "running", goal, json.dumps(success_criteria)),
        )
        self.conn.commit()

    def append(self, task_id: str, step: int, type_: EventType, payload: dict[str, Any],
               verification_result: Optional[dict[str, Any]] = None) -> int:
        """Appends one event and commits (fsync boundary / checkpoint). Returns the new event id."""
        cur = self.conn.execute(
            "INSERT INTO events (task_id, step, timestamp, type, payload, verification_result) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (task_id, step, now_iso(), type_.value, json.dumps(payload),
             json.dumps(verification_result) if verification_result is not None else None),
        )
        self.conn.commit()
        return cur.lastrowid

    def all_events(self, task_id: str) -> list[Event]:
        rows = self.conn.execute(
            "SELECT * FROM events WHERE task_id = ? ORDER BY id ASC", (task_id,)
        ).fetchall()
        return self._rows_to_events(rows)

    def events_after(self, task_id: str, event_id: int) -> list[Event]:
        rows = self.conn.execute(
            "SELECT * FROM events WHERE task_id = ? AND id > ? ORDER BY id ASC",
            (task_id, event_id),
        ).fetchall()
        return self._rows_to_events(rows)

    def _rows_to_events(self, rows: list[sqlite3.Row]) -> list[Event]:
        return [
            Event(
                id=r["id"], task_id=r["task_id"], step=r["step"], timestamp=r["timestamp"],
                type=EventType(r["type"]), payload=json.loads(r["payload"]),
                verification_result=json.loads(r["verification_result"]) if r["verification_result"] else None,
            )
            for r in rows
        ]

    def max_event_id(self, task_id: str) -> Optional[int]:
        row = self.conn.execute(
            "SELECT MAX(id) as max_id FROM events WHERE task_id = ?", (task_id,)
        ).fetchone()
        return row["max_id"]

    def task_exists(self, task_id: str) -> bool:
        row = self.conn.execute("SELECT 1 FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return row is not None
