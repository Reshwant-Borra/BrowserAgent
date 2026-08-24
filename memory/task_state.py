"""Storage for the `tasks` (immutable record) and `task_state` (derived/materialized view)
tables. Shares the EventStore's sqlite3 connection (same file, same WAL) rather than opening
a second connection to the same database.

Critical invariant (ARCHITECTURE.md §8, "task_state must never be authoritative"): `load()`
always checks the row's `last_event_id` against the true max event id for the task before
trusting it, and rebuilds via `memory.replay.replay_task` on any mismatch — including the
"row doesn't exist at all" case. Nothing here is allowed to hand back a state that could be
behind what the event log actually says happened.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Optional

from memory.event_store import EventStore
from memory.models import TaskRecord, TaskState
from memory.replay import replay_task


class TaskStateStore:
    def __init__(self, event_store: EventStore):
        self.event_store = event_store
        self.conn: sqlite3.Connection = event_store.conn

    def get_task_record(self, task_id: str) -> Optional[TaskRecord]:
        row = self.conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            return None
        return TaskRecord(
            id=row["id"], created_at=row["created_at"], status=row["status"],
            goal=row["goal"], success_criteria=json.loads(row["success_criteria"]),
        )

    def _row_to_state(self, row: sqlite3.Row) -> TaskState:
        return TaskState(
            task_id=row["task_id"],
            current_step=row["current_step"],
            current_subgoal=row["current_subgoal"],
            plan=json.loads(row["plan"]) if row["plan"] else [],
            completed_subgoals=json.loads(row["completed_subgoals"]) if row["completed_subgoals"] else [],
            current_url=row["current_url"],
            current_page_hash=row["current_page_hash"],
            recent_actions=json.loads(row["recent_actions"]) if row["recent_actions"] else [],
            recovery_level=row["recovery_level"],
            retry_count=row["retry_count"],
            blocked_reason=row["blocked_reason"],
            status=row["status"],
            last_event_id=row["last_event_id"],
        )

    def _rebuild_from_events(self, task_id: str) -> TaskState:
        events = self.event_store.all_events(task_id)
        state = replay_task(task_id, events)
        self.save(state)
        return state

    def load(self, task_id: str) -> TaskState:
        """Always returns a state that is at least as current as the event log. Rebuilds
        from events whenever the stored row is missing or behind."""
        true_max = self.event_store.max_event_id(task_id)
        row = self.conn.execute("SELECT * FROM task_state WHERE task_id = ?", (task_id,)).fetchone()
        if row is None:
            return self._rebuild_from_events(task_id)
        state = self._row_to_state(row)
        if true_max is not None and state.last_event_id != true_max:
            return self._rebuild_from_events(task_id)
        # pending_action_intent is derived-on-demand, never persisted in the row itself.
        events = self.event_store.all_events(task_id)
        state.pending_action_intent = replay_task(task_id, events).pending_action_intent
        return state

    def save(self, state: TaskState) -> None:
        self.conn.execute(
            """INSERT INTO task_state
               (task_id, current_step, current_subgoal, plan, completed_subgoals, current_url,
                current_page_hash, recent_actions, recovery_level, retry_count, blocked_reason,
                status, last_event_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(task_id) DO UPDATE SET
                 current_step=excluded.current_step, current_subgoal=excluded.current_subgoal,
                 plan=excluded.plan, completed_subgoals=excluded.completed_subgoals,
                 current_url=excluded.current_url, current_page_hash=excluded.current_page_hash,
                 recent_actions=excluded.recent_actions, recovery_level=excluded.recovery_level,
                 retry_count=excluded.retry_count, blocked_reason=excluded.blocked_reason,
                 status=excluded.status, last_event_id=excluded.last_event_id
            """,
            (
                state.task_id, state.current_step, state.current_subgoal,
                json.dumps(state.plan), json.dumps(state.completed_subgoals),
                state.current_url, state.current_page_hash, json.dumps(state.recent_actions),
                state.recovery_level, state.retry_count, state.blocked_reason,
                state.status, state.last_event_id,
            ),
        )
        self.conn.commit()  # checkpoint: this row is never ahead of committed events
