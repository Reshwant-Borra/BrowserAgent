from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path
from typing import Any, Iterable, Optional

from batch.models import BatchEventType, BatchPolicy, BatchStatus, ResultContract, WorkItemStatus
from batch.policies import normalize_target_url, target_payload
from memory.event_store import now_iso


SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS batch_jobs (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    status TEXT NOT NULL,
    goal TEXT NOT NULL,
    result_contract TEXT NOT NULL,
    policy TEXT NOT NULL,
    item_count INTEGER NOT NULL DEFAULT 0,
    completed_count INTEGER NOT NULL DEFAULT 0,
    failed_count INTEGER NOT NULL DEFAULT 0,
    blocked_count INTEGER NOT NULL DEFAULT 0,
    duplicate_input_count INTEGER NOT NULL DEFAULT 0,
    current_work_item_id INTEGER,
    synthesis_status TEXT NOT NULL DEFAULT 'pending',
    final_result TEXT
);

CREATE TABLE IF NOT EXISTS batch_work_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_job_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    target TEXT NOT NULL,
    target_key TEXT NOT NULL,
    target_payload TEXT NOT NULL,
    original_targets TEXT NOT NULL,
    status TEXT NOT NULL,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    browser_task_id TEXT,
    started_at TEXT,
    completed_at TEXT,
    last_error TEXT,
    failure_category TEXT,
    result_id INTEGER,
    claimed_at TEXT,
    worker_id TEXT,
    lease_expires_at TEXT,
    UNIQUE(batch_job_id, target_key),
    FOREIGN KEY(batch_job_id) REFERENCES batch_jobs(id)
);

CREATE TABLE IF NOT EXISTS batch_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_job_id TEXT NOT NULL,
    work_item_id INTEGER NOT NULL,
    target TEXT NOT NULL,
    status TEXT NOT NULL,
    summary TEXT NOT NULL,
    structured_data TEXT NOT NULL,
    source_url TEXT,
    final_url TEXT,
    evidence TEXT NOT NULL,
    browser_task_id TEXT,
    source_event_ids TEXT NOT NULL,
    dedupe_key TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(batch_job_id, work_item_id),
    FOREIGN KEY(batch_job_id) REFERENCES batch_jobs(id),
    FOREIGN KEY(work_item_id) REFERENCES batch_work_items(id)
);

CREATE TABLE IF NOT EXISTS batch_deduped_findings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_job_id TEXT NOT NULL,
    dedupe_key TEXT NOT NULL,
    finding TEXT NOT NULL,
    provenance TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(batch_job_id, dedupe_key),
    FOREIGN KEY(batch_job_id) REFERENCES batch_jobs(id)
);

CREATE TABLE IF NOT EXISTS batch_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_job_id TEXT NOT NULL,
    work_item_id INTEGER,
    timestamp TEXT NOT NULL,
    type TEXT NOT NULL,
    payload TEXT NOT NULL,
    FOREIGN KEY(batch_job_id) REFERENCES batch_jobs(id)
);

CREATE INDEX IF NOT EXISTS idx_batch_items_next ON batch_work_items(batch_job_id, status, ordinal);
CREATE INDEX IF NOT EXISTS idx_batch_events_job ON batch_events(batch_job_id, id);
CREATE INDEX IF NOT EXISTS idx_batch_results_job ON batch_results(batch_job_id, work_item_id);
"""


class BatchStore:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.execute(
            "INSERT OR IGNORE INTO schema_version(version, applied_at) VALUES (?, ?)",
            (1, now_iso()),
        )
        self.conn.commit()

    @classmethod
    def for_batch_dir(cls, batch_dir: Path) -> "BatchStore":
        return cls(Path(batch_dir) / "batch.db")

    def close(self) -> None:
        self.conn.close()

    def create_batch(
        self,
        goal: str,
        targets: Iterable[str],
        result_contract: ResultContract,
        policy: BatchPolicy,
        batch_id: str | None = None,
    ) -> str:
        batch_id = batch_id or uuid.uuid4().hex[:12]
        now = now_iso()
        target_list = [t.strip() for t in targets if t.strip()]
        if policy.max_total_items is not None:
            target_list = target_list[:policy.max_total_items]
        with self.conn:
            self.conn.execute(
                """INSERT INTO batch_jobs
                   (id, created_at, updated_at, status, goal, result_contract, policy)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    batch_id,
                    now,
                    now,
                    BatchStatus.PENDING.value,
                    goal,
                    json.dumps(result_contract.__dict__),
                    json.dumps(_policy_dict(policy)),
                ),
            )
            self._append_event_locked(batch_id, None, BatchEventType.BATCH_CREATED, {"goal": goal})
            seen: dict[str, int] = {}
            duplicate_count = 0
            ordinal = 0
            for raw in target_list:
                key = normalize_target_url(raw)
                if key in seen:
                    duplicate_count += 1
                    item_id = seen[key]
                    row = self.conn.execute(
                        "SELECT original_targets FROM batch_work_items WHERE id = ?", (item_id,)
                    ).fetchone()
                    originals = json.loads(row["original_targets"])
                    originals.append(raw)
                    self.conn.execute(
                        "UPDATE batch_work_items SET original_targets = ? WHERE id = ?",
                        (json.dumps(originals), item_id),
                    )
                    self._append_event_locked(
                        batch_id,
                        item_id,
                        BatchEventType.WORK_ITEM_DEDUPED,
                        {"duplicate_target": raw, "target_key": key},
                    )
                    continue
                ordinal += 1
                cur = self.conn.execute(
                    """INSERT INTO batch_work_items
                       (batch_job_id, ordinal, target, target_key, target_payload,
                        original_targets, status)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        batch_id,
                        ordinal,
                        raw,
                        key,
                        json.dumps(target_payload(raw)),
                        json.dumps([raw]),
                        WorkItemStatus.PENDING.value,
                    ),
                )
                item_id = int(cur.lastrowid)
                seen[key] = item_id
                self._append_event_locked(
                    batch_id,
                    item_id,
                    BatchEventType.WORK_ITEM_ADDED,
                    {"ordinal": ordinal, "target": raw, "target_key": key},
                )
            self.conn.execute(
                """UPDATE batch_jobs
                   SET item_count = ?, duplicate_input_count = ?, updated_at = ?
                   WHERE id = ?""",
                (ordinal, duplicate_count, now_iso(), batch_id),
            )
        return batch_id

    def get_job(self, batch_id: str) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM batch_jobs WHERE id = ?", (batch_id,)).fetchone()
        if row is None:
            raise ValueError(f"no such batch: {batch_id}")
        return row

    def progress(self, batch_id: str) -> dict[str, Any]:
        job = self.get_job(batch_id)
        counts = {
            row["status"]: row["count"]
            for row in self.conn.execute(
                "SELECT status, COUNT(*) AS count FROM batch_work_items WHERE batch_job_id = ? GROUP BY status",
                (batch_id,),
            )
        }
        current = self.conn.execute(
            "SELECT * FROM batch_work_items WHERE batch_job_id = ? AND status = ? ORDER BY ordinal LIMIT 1",
            (batch_id, WorkItemStatus.RUNNING.value),
        ).fetchone()
        return {
            "id": job["id"],
            "status": job["status"],
            "goal": job["goal"],
            "item_count": job["item_count"],
            "completed": counts.get(WorkItemStatus.COMPLETED.value, 0),
            "failed": counts.get(WorkItemStatus.FAILED_FINAL.value, 0),
            "blocked": counts.get(WorkItemStatus.BLOCKED.value, 0),
            "pending": counts.get(WorkItemStatus.PENDING.value, 0),
            "running": counts.get(WorkItemStatus.RUNNING.value, 0),
            "duplicates": job["duplicate_input_count"],
            "current": dict(current) if current else None,
        }

    def claim_next_item(self, batch_id: str, worker_id: str, lease_seconds: int) -> Optional[sqlite3.Row]:
        now = now_iso()
        expires = _iso_plus_seconds(lease_seconds)
        with self.conn:
            row = self.conn.execute(
                """SELECT * FROM batch_work_items
                   WHERE batch_job_id = ? AND status = ?
                   ORDER BY ordinal LIMIT 1""",
                (batch_id, WorkItemStatus.PENDING.value),
            ).fetchone()
            if row is None:
                return None
            self.conn.execute(
                """UPDATE batch_work_items
                   SET status = ?, attempt_count = attempt_count + 1, started_at = COALESCE(started_at, ?),
                       claimed_at = ?, worker_id = ?, lease_expires_at = ?
                   WHERE id = ?""",
                (WorkItemStatus.RUNNING.value, now, now, worker_id, expires, row["id"]),
            )
            self.conn.execute(
                "UPDATE batch_jobs SET status = ?, current_work_item_id = ?, updated_at = ? WHERE id = ?",
                (BatchStatus.RUNNING.value, row["id"], now_iso(), batch_id),
            )
            self._append_event_locked(
                batch_id,
                row["id"],
                BatchEventType.WORK_ITEM_STARTED,
                {"worker_id": worker_id, "attempt": row["attempt_count"] + 1, "target": row["target"]},
            )
        return self.get_item(int(row["id"]))

    def get_item(self, item_id: int) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM batch_work_items WHERE id = ?", (item_id,)).fetchone()
        if row is None:
            raise ValueError(f"no such work item: {item_id}")
        return row

    def running_items(self, batch_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM batch_work_items WHERE batch_job_id = ? AND status = ? ORDER BY ordinal",
            (batch_id, WorkItemStatus.RUNNING.value),
        ).fetchall()

    def set_item_browser_task(self, item_id: int, task_id: str) -> None:
        with self.conn:
            self.conn.execute("UPDATE batch_work_items SET browser_task_id = ? WHERE id = ?", (task_id, item_id))

    def complete_item(self, item_id: int, result_id: int) -> None:
        item = self.get_item(item_id)
        with self.conn:
            self.conn.execute(
                """UPDATE batch_work_items
                   SET status = ?, completed_at = ?, result_id = ?, last_error = NULL,
                       failure_category = NULL, claimed_at = NULL, worker_id = NULL,
                       lease_expires_at = NULL
                   WHERE id = ?""",
                (WorkItemStatus.COMPLETED.value, now_iso(), result_id, item_id),
            )
            self._append_event_locked(
                item["batch_job_id"], item_id, BatchEventType.WORK_ITEM_COMPLETED, {"result_id": result_id}
            )
            self.refresh_job_counts(item["batch_job_id"])

    def fail_item(
        self,
        item_id: int,
        category: str,
        error: str,
        retryable: bool,
        clear_browser_task: bool = False,
    ) -> None:
        item = self.get_item(item_id)
        status = WorkItemStatus.FAILED_RETRYABLE.value if retryable else WorkItemStatus.FAILED_FINAL.value
        event_type = BatchEventType.WORK_ITEM_RETRY_SCHEDULED if retryable else BatchEventType.WORK_ITEM_FAILED
        with self.conn:
            self.conn.execute(
                """UPDATE batch_work_items
                   SET status = ?, completed_at = CASE WHEN ? THEN NULL ELSE ? END,
                       last_error = ?, failure_category = ?, claimed_at = NULL, worker_id = NULL,
                       lease_expires_at = NULL,
                       browser_task_id = CASE WHEN ? THEN NULL ELSE browser_task_id END
                   WHERE id = ?""",
                (status, retryable, now_iso(), error[:1000], category, clear_browser_task, item_id),
            )
            self._append_event_locked(
                item["batch_job_id"], item_id, event_type,
                {"failure_category": category, "error": error[:1000], "retryable": retryable},
            )
            self.refresh_job_counts(item["batch_job_id"])

    def requeue_retryable(self, batch_id: str) -> int:
        with self.conn:
            rows = self.conn.execute(
                "SELECT id FROM batch_work_items WHERE batch_job_id = ? AND status = ? ORDER BY ordinal",
                (batch_id, WorkItemStatus.FAILED_RETRYABLE.value),
            ).fetchall()
            for row in rows:
                self.conn.execute(
                    "UPDATE batch_work_items SET status = ?, claimed_at = NULL, worker_id = NULL WHERE id = ?",
                    (WorkItemStatus.PENDING.value, row["id"]),
                )
        return len(rows)

    def block_item(self, item_id: int, category: str, reason: str) -> None:
        item = self.get_item(item_id)
        with self.conn:
            self.conn.execute(
                """UPDATE batch_work_items
                   SET status = ?, completed_at = ?, last_error = ?, failure_category = ?,
                       claimed_at = NULL, worker_id = NULL, lease_expires_at = NULL
                   WHERE id = ?""",
                (WorkItemStatus.BLOCKED.value, now_iso(), reason[:1000], category, item_id),
            )
            self._append_event_locked(
                item["batch_job_id"], item_id, BatchEventType.WORK_ITEM_BLOCKED,
                {"failure_category": category, "reason": reason[:1000]},
            )
            self.refresh_job_counts(item["batch_job_id"])

    def upsert_result(
        self,
        batch_id: str,
        work_item_id: int,
        target: str,
        status: str,
        summary: str,
        structured_data: dict[str, Any],
        source_url: str | None,
        final_url: str | None,
        evidence: list[dict[str, Any]],
        browser_task_id: str | None,
        source_event_ids: list[int],
        dedupe_key: str | None = None,
    ) -> int:
        created = now_iso()
        with self.conn:
            self.conn.execute(
                """INSERT INTO batch_results
                   (batch_job_id, work_item_id, target, status, summary, structured_data,
                    source_url, final_url, evidence, browser_task_id, source_event_ids,
                    dedupe_key, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(batch_job_id, work_item_id) DO UPDATE SET
                     status=excluded.status, summary=excluded.summary,
                     structured_data=excluded.structured_data, source_url=excluded.source_url,
                     final_url=excluded.final_url, evidence=excluded.evidence,
                     browser_task_id=excluded.browser_task_id,
                     source_event_ids=excluded.source_event_ids,
                     dedupe_key=excluded.dedupe_key""",
                (
                    batch_id,
                    work_item_id,
                    target,
                    status,
                    summary[:2000],
                    json.dumps(structured_data),
                    source_url,
                    final_url,
                    json.dumps(evidence)[:12000],
                    browser_task_id,
                    json.dumps(source_event_ids),
                    dedupe_key,
                    created,
                ),
            )
            row = self.conn.execute(
                "SELECT id FROM batch_results WHERE batch_job_id = ? AND work_item_id = ?",
                (batch_id, work_item_id),
            ).fetchone()
        return int(row["id"])

    def results(self, batch_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM batch_results WHERE batch_job_id = ? ORDER BY work_item_id", (batch_id,)
        ).fetchall()

    def items(self, batch_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM batch_work_items WHERE batch_job_id = ? ORDER BY ordinal", (batch_id,)
        ).fetchall()

    def append_event(self, batch_id: str, work_item_id: int | None, type_: BatchEventType, payload: dict[str, Any]) -> int:
        with self.conn:
            return self._append_event_locked(batch_id, work_item_id, type_, payload)

    def events(self, batch_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM batch_events WHERE batch_job_id = ? ORDER BY id", (batch_id,)
        ).fetchall()

    def refresh_job_counts(self, batch_id: str) -> None:
        counts = {
            row["status"]: row["count"]
            for row in self.conn.execute(
                "SELECT status, COUNT(*) AS count FROM batch_work_items WHERE batch_job_id = ? GROUP BY status",
                (batch_id,),
            )
        }
        completed = counts.get(WorkItemStatus.COMPLETED.value, 0)
        failed = counts.get(WorkItemStatus.FAILED_FINAL.value, 0)
        blocked = counts.get(WorkItemStatus.BLOCKED.value, 0)
        pending = counts.get(WorkItemStatus.PENDING.value, 0) + counts.get(WorkItemStatus.FAILED_RETRYABLE.value, 0)
        running = counts.get(WorkItemStatus.RUNNING.value, 0)
        status = BatchStatus.RUNNING.value
        if pending == 0 and running == 0:
            status = BatchStatus.COMPLETED.value if failed == 0 and blocked == 0 else BatchStatus.COMPLETED_WITH_FAILURES.value
        self.conn.execute(
            """UPDATE batch_jobs
               SET completed_count = ?, failed_count = ?, blocked_count = ?,
                   status = ?, current_work_item_id = CASE WHEN ? = 0 THEN NULL ELSE current_work_item_id END,
                   updated_at = ?
               WHERE id = ?""",
            (completed, failed, blocked, status, running, now_iso(), batch_id),
        )

    def save_final_result(self, batch_id: str, final_result: dict[str, Any]) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE batch_jobs SET synthesis_status = ?, final_result = ?, updated_at = ? WHERE id = ?",
                ("completed", json.dumps(final_result), now_iso(), batch_id),
            )
            self._append_event_locked(batch_id, None, BatchEventType.SYNTHESIS_COMPLETED, final_result)
            self._append_event_locked(batch_id, None, BatchEventType.BATCH_COMPLETED, {"status": self.get_job(batch_id)["status"]})

    def _append_event_locked(self, batch_id: str, work_item_id: int | None, type_: BatchEventType, payload: dict[str, Any]) -> int:
        cur = self.conn.execute(
            "INSERT INTO batch_events (batch_job_id, work_item_id, timestamp, type, payload) VALUES (?, ?, ?, ?, ?)",
            (batch_id, work_item_id, now_iso(), type_.value, json.dumps(payload)),
        )
        return int(cur.lastrowid)


def _policy_dict(policy: BatchPolicy) -> dict[str, Any]:
    data = policy.__dict__.copy()
    data["navigation_scope"] = policy.navigation_scope.value
    data["session_mode"] = policy.session_mode.value
    return data


def _iso_plus_seconds(seconds: int) -> str:
    from datetime import datetime, timedelta, timezone

    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()
