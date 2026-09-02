"""Persistence for ordered multi-site workflows. Same sqlite-per-job pattern as
batch/store.py's BatchStore, simplified for strictly sequential execution (Section 16: the
model is never trusted to "remember" step order — ordinal is explicit and persisted here).
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path
from typing import Any

from memory.event_store import now_iso
from workflow.models import WorkflowEventType, WorkflowPolicy, WorkflowStatus, WorkflowStepStatus

SCHEMA = """
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS workflow_jobs (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    status TEXT NOT NULL,
    objective TEXT NOT NULL,
    policy TEXT NOT NULL,
    step_count INTEGER NOT NULL DEFAULT 0,
    current_step_ordinal INTEGER,
    blocked_reason TEXT,
    final_result TEXT
);

CREATE TABLE IF NOT EXISTS workflow_steps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_job_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    target TEXT NOT NULL,
    objective TEXT NOT NULL,
    status TEXT NOT NULL,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    browser_task_id TEXT,
    started_at TEXT,
    completed_at TEXT,
    verified INTEGER,
    result_summary TEXT,
    facts_out TEXT NOT NULL DEFAULT '{}',
    last_error TEXT,
    UNIQUE(workflow_job_id, ordinal),
    FOREIGN KEY(workflow_job_id) REFERENCES workflow_jobs(id)
);

CREATE TABLE IF NOT EXISTS workflow_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_job_id TEXT NOT NULL,
    step_id INTEGER,
    timestamp TEXT NOT NULL,
    type TEXT NOT NULL,
    payload TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_workflow_steps_job ON workflow_steps(workflow_job_id, ordinal);
CREATE INDEX IF NOT EXISTS idx_workflow_events_job ON workflow_events(workflow_job_id, id);
"""


class WorkflowStore:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    @classmethod
    def for_workflow_dir(cls, workflow_dir: Path) -> "WorkflowStore":
        return cls(Path(workflow_dir) / "workflow.db")

    def close(self) -> None:
        self.conn.close()

    def create_workflow(
        self,
        objective: str,
        steps: list[dict[str, Any]],
        policy: WorkflowPolicy,
        workflow_id: str | None = None,
    ) -> str:
        workflow_id = workflow_id or uuid.uuid4().hex[:12]
        now = now_iso()
        with self.conn:
            self.conn.execute(
                """INSERT INTO workflow_jobs (id, created_at, updated_at, status, objective, policy, step_count)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (workflow_id, now, now, WorkflowStatus.PENDING.value, objective,
                 json.dumps(_policy_dict(policy)), len(steps)),
            )
            for step in steps:
                self.conn.execute(
                    """INSERT INTO workflow_steps (workflow_job_id, ordinal, target, objective, status)
                       VALUES (?, ?, ?, ?, ?)""",
                    (workflow_id, step["ordinal"], step["target"], step["objective"],
                     WorkflowStepStatus.PENDING.value),
                )
            self._append_event_locked(workflow_id, None, WorkflowEventType.WORKFLOW_CREATED,
                                       {"objective": objective, "step_count": len(steps)})
        return workflow_id

    def get_job(self, workflow_id: str) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM workflow_jobs WHERE id = ?", (workflow_id,)).fetchone()
        if row is None:
            raise ValueError(f"no such workflow: {workflow_id}")
        return row

    def steps(self, workflow_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM workflow_steps WHERE workflow_job_id = ? ORDER BY ordinal", (workflow_id,)
        ).fetchall()

    def get_step(self, step_id: int) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM workflow_steps WHERE id = ?", (step_id,)).fetchone()
        if row is None:
            raise ValueError(f"no such workflow step: {step_id}")
        return row

    def next_pending_step(self, workflow_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            """SELECT * FROM workflow_steps WHERE workflow_job_id = ?
               AND status IN (?, ?) ORDER BY ordinal LIMIT 1""",
            (workflow_id, WorkflowStepStatus.PENDING.value, WorkflowStepStatus.VERIFICATION_FAILED.value),
        ).fetchone()

    def facts_so_far(self, workflow_id: str, before_ordinal: int) -> dict[str, Any]:
        """Structured facts accumulated from completed+verified steps before `before_ordinal`
        (Section 33/34) — passed into the next step's goal as an explicit block rather than
        relying on the model to recall an earlier, separate AgentLoop task's context."""
        facts: dict[str, Any] = {}
        for row in self.conn.execute(
            """SELECT facts_out FROM workflow_steps WHERE workflow_job_id = ? AND ordinal < ?
               AND status = ? ORDER BY ordinal""",
            (workflow_id, before_ordinal, WorkflowStepStatus.COMPLETED.value),
        ):
            facts.update(json.loads(row["facts_out"] or "{}"))
        return facts

    def facts_so_far_with_origin(self, workflow_id: str, before_ordinal: int) -> dict[str, dict[str, Any]]:
        """Same accumulation as `facts_so_far`, additionally carrying which step's own
        `target` each fact was discovered on (Phase 5's cross-origin sensitive-transfer gate —
        `workflow/orchestrator.py::_run_step` needs to know a fact's *source* origin, not just
        its value, to tell a same-origin re-use apart from a genuine cross-origin transfer)."""
        facts: dict[str, dict[str, Any]] = {}
        for row in self.conn.execute(
            """SELECT target, facts_out FROM workflow_steps WHERE workflow_job_id = ? AND ordinal < ?
               AND status = ? ORDER BY ordinal""",
            (workflow_id, before_ordinal, WorkflowStepStatus.COMPLETED.value),
        ):
            for key, value in json.loads(row["facts_out"] or "{}").items():
                facts[key] = {"value": value, "source_target": row["target"]}
        return facts

    def start_step(self, step_id: int) -> None:
        step = self.get_step(step_id)
        with self.conn:
            self.conn.execute(
                """UPDATE workflow_steps SET status = ?, attempt_count = attempt_count + 1,
                   started_at = COALESCE(started_at, ?) WHERE id = ?""",
                (WorkflowStepStatus.RUNNING.value, now_iso(), step_id),
            )
            self.conn.execute(
                "UPDATE workflow_jobs SET status = ?, current_step_ordinal = ?, updated_at = ? WHERE id = ?",
                (WorkflowStatus.RUNNING.value, step["ordinal"], now_iso(), step["workflow_job_id"]),
            )
            self._append_event_locked(step["workflow_job_id"], step_id, WorkflowEventType.STEP_STARTED,
                                       {"ordinal": step["ordinal"], "target": step["target"]})

    def complete_step(self, step_id: int, browser_task_id: str, summary: str, facts: dict[str, Any]) -> None:
        step = self.get_step(step_id)
        with self.conn:
            self.conn.execute(
                """UPDATE workflow_steps SET status = ?, completed_at = ?, verified = 1,
                   browser_task_id = ?, result_summary = ?, facts_out = ?, last_error = NULL WHERE id = ?""",
                (WorkflowStepStatus.COMPLETED.value, now_iso(), browser_task_id, summary[:2000],
                 json.dumps(facts), step_id),
            )
            self._append_event_locked(step["workflow_job_id"], step_id, WorkflowEventType.STEP_VERIFIED,
                                       {"ordinal": step["ordinal"], "facts": facts})
            self._append_event_locked(step["workflow_job_id"], step_id, WorkflowEventType.STEP_COMPLETED,
                                       {"ordinal": step["ordinal"], "summary": summary[:500]})

    def fail_step(self, step_id: int, error: str, retryable: bool, max_attempts: int) -> None:
        step = self.get_step(step_id)
        will_retry = retryable and int(step["attempt_count"]) < max_attempts
        status = WorkflowStepStatus.VERIFICATION_FAILED.value if will_retry else WorkflowStepStatus.FAILED.value
        event = WorkflowEventType.STEP_RETRY_SCHEDULED if will_retry else WorkflowEventType.STEP_VERIFICATION_FAILED
        with self.conn:
            self.conn.execute(
                "UPDATE workflow_steps SET status = ?, verified = 0, last_error = ? WHERE id = ?",
                (status, error[:1000], step_id),
            )
            self._append_event_locked(step["workflow_job_id"], step_id, event,
                                       {"ordinal": step["ordinal"], "error": error[:1000], "will_retry": will_retry})

    def block_workflow(self, workflow_id: str, reason: str) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE workflow_jobs SET status = ?, blocked_reason = ?, updated_at = ? WHERE id = ?",
                (WorkflowStatus.BLOCKED.value, reason[:1000], now_iso(), workflow_id),
            )
            self._append_event_locked(workflow_id, None, WorkflowEventType.WORKFLOW_BLOCKED, {"reason": reason[:1000]})

    def complete_workflow(self, workflow_id: str, final_result: dict[str, Any]) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE workflow_jobs SET status = ?, final_result = ?, updated_at = ? WHERE id = ?",
                (WorkflowStatus.COMPLETED.value, json.dumps(final_result), now_iso(), workflow_id),
            )
            self._append_event_locked(workflow_id, None, WorkflowEventType.WORKFLOW_COMPLETED, final_result)

    def events(self, workflow_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM workflow_events WHERE workflow_job_id = ? ORDER BY id", (workflow_id,)
        ).fetchall()

    def append_event(self, workflow_id: str, step_id: int | None, type_: WorkflowEventType, payload: dict[str, Any]) -> int:
        with self.conn:
            return self._append_event_locked(workflow_id, step_id, type_, payload)

    def _append_event_locked(self, workflow_id: str, step_id: int | None, type_: WorkflowEventType, payload: dict[str, Any]) -> int:
        cur = self.conn.execute(
            "INSERT INTO workflow_events (workflow_job_id, step_id, timestamp, type, payload) VALUES (?, ?, ?, ?, ?)",
            (workflow_id, step_id, now_iso(), type_.value, json.dumps(payload)),
        )
        return int(cur.lastrowid)


def _policy_dict(policy: WorkflowPolicy) -> dict[str, Any]:
    data = policy.__dict__.copy()
    data["navigation_scope"] = policy.navigation_scope.value
    return data
