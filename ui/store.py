"""Persisted record of every UI job (Section 26/27/59): what makes "survive page refresh"
possible. `GET /api/jobs/{id}` always re-derives its answer from here (plus the underlying
task_state/batch/workflow progress it points at) instead of from any open HTTP connection.
Stored under `runtime/ui/jobs.db`, local-only (Section 28/59) — never sent anywhere.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path
from typing import Any

from memory.event_store import now_iso

SCHEMA = """
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS ui_jobs (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    prompt TEXT NOT NULL,
    kind TEXT,
    router_decision TEXT,
    status TEXT NOT NULL,
    activity TEXT,
    task_id TEXT,
    batch_id TEXT,
    workflow_id TEXT,
    pending_approval TEXT,
    pending_clarification TEXT,
    final_result TEXT,
    error TEXT
);
"""

# `pending_clarification` was added after the original schema shipped; ALTER TABLE rather
# than a version-gated migration framework since this is a single local SQLite file with one
# reader/writer process (Section 29 of the semantic planner task: don't over-build this).
_MIGRATIONS = (
    "ALTER TABLE ui_jobs ADD COLUMN pending_clarification TEXT",
)


class UIJobStore:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()
        for migration in _MIGRATIONS:
            try:
                self.conn.execute(migration)
                self.conn.commit()
            except sqlite3.OperationalError:
                pass  # column already exists on a pre-existing runtime/ui/jobs.db

    def close(self) -> None:
        self.conn.close()

    def create(self, prompt: str) -> str:
        job_id = uuid.uuid4().hex[:12]
        now = now_iso()
        with self.conn:
            self.conn.execute(
                """INSERT INTO ui_jobs (id, created_at, updated_at, prompt, status, activity)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (job_id, now, now, prompt, "starting", "Routing task..."),
            )
        return job_id

    def get(self, job_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM ui_jobs WHERE id = ?", (job_id,)).fetchone()
        return _row_to_dict(row) if row else None

    def list_recent(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM ui_jobs ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [_row_to_dict(r) for r in rows]

    def update(self, job_id: str, **fields: Any) -> None:
        if not fields:
            return
        json_fields = {"router_decision", "final_result", "pending_approval", "pending_clarification"}
        columns = []
        values = []
        for key, value in fields.items():
            columns.append(f"{key} = ?")
            values.append(json.dumps(value) if key in json_fields and value is not None else value)
        columns.append("updated_at = ?")
        values.append(now_iso())
        values.append(job_id)
        with self.conn:
            self.conn.execute(f"UPDATE ui_jobs SET {', '.join(columns)} WHERE id = ?", values)

    def clear_history(self) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM ui_jobs")


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    for key in ("router_decision", "final_result", "pending_approval", "pending_clarification"):
        if data.get(key):
            try:
                data[key] = json.loads(data[key])
            except (TypeError, json.JSONDecodeError):
                pass
    return data
