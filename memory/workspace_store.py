"""TaskWorkspace projection store (Phase 1 of the general-controller migration).

Mirrors the task_state.py pattern: `events` remains the sole source of truth. A
WorkspacePatch is validated here, appended as exactly one WORKSPACE_MUTATED event, and the
three projection tables (workspace_state, workspace_entities, workspace_evidence) are updated
in the same transaction. Projections are always rebuildable from scratch by replaying every
WORKSPACE_MUTATED event for a task in order — `rebuild_if_stale` / `rebuild` do this, and
`load` uses them whenever the stored `workspace_state.last_event_id` doesn't match the task's
true max WORKSPACE_MUTATED event id (same staleness check as TaskStateStore.load).

The model never writes SQL and never receives a raw connection: it only ever produces a
WorkspacePatch, which this module validates before anything is persisted.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Optional

from memory.event_store import EventStore, EventType
from agent.workspace_models import (
    EvidenceRef,
    WorkspaceEntity,
    WorkspacePatch,
    WorkspaceView,
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class WorkspacePatchError(ValueError):
    """Raised when a WorkspacePatch references state that doesn't exist (unknown entity id,
    unknown/foreign source_event_id, etc). Deterministic validation failure, not a model bug
    the caller should silently swallow."""


class WorkspaceStore:
    def __init__(self, event_store: EventStore):
        self.event_store = event_store
        self.conn: sqlite3.Connection = event_store.conn

    # ------------------------------------------------------------------ mutation

    def apply_patch(self, task_id: str, patch: WorkspacePatch) -> WorkspaceView:
        """Validate `patch`, append one WORKSPACE_MUTATED event, update projections, return the
        resulting view. Raises WorkspacePatchError before anything is persisted if validation
        fails — partial application never happens."""
        self._validate(task_id, patch)

        view = self.load(task_id)
        step = (self.event_store.max_event_id(task_id) or 0) + 1
        event_id = self.event_store.append(
            task_id, step, EventType.WORKSPACE_MUTATED, patch.model_dump(mode="json")
        )
        self._apply_to_projections(task_id, patch, event_id, view)
        return self.load(task_id)

    def _validate(self, task_id: str, patch: WorkspacePatch) -> None:
        if not self.event_store.task_exists(task_id):
            raise WorkspacePatchError(f"unknown task_id {task_id!r}")

        max_event_id = self.event_store.max_event_id(task_id) or 0
        existing_ids = {e.id for e in self._load_entities(task_id)}
        added_ids = {e.id for e in patch.add_entities}

        for e in patch.add_entities:
            if e.id in existing_ids:
                raise WorkspacePatchError(f"add_entities: id {e.id!r} already exists")
        if len(added_ids) != len(patch.add_entities):
            raise WorkspacePatchError("add_entities: duplicate ids in same patch")

        known_after_add = existing_ids | added_ids
        for upd in patch.update_entities:
            if upd.id not in known_after_add:
                raise WorkspacePatchError(f"update_entities: unknown entity id {upd.id!r}")

        for ev in patch.add_evidence:
            if ev.source_event_id > max_event_id or ev.source_event_id < 1:
                raise WorkspacePatchError(
                    f"add_evidence: source_event_id {ev.source_event_id} does not exist for task {task_id!r}"
                )
            if ev.entity_id is not None and ev.entity_id not in known_after_add:
                raise WorkspacePatchError(f"add_evidence: unknown entity id {ev.entity_id!r}")

    def _apply_to_projections(
        self, task_id: str, patch: WorkspacePatch, event_id: int, prior: WorkspaceView
    ) -> None:
        now = _now_iso()

        for e in patch.add_entities:
            self.conn.execute(
                """INSERT INTO workspace_entities
                   (id, task_id, entity_type, name, attributes, status, source_event_id, updated_event_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (e.id, task_id, e.entity_type, e.name, json.dumps(e.attributes), e.status,
                 event_id, event_id),
            )

        for upd in patch.update_entities:
            fields, values = [], []
            if upd.name is not None:
                fields.append("name = ?"); values.append(upd.name)
            if upd.attributes is not None:
                fields.append("attributes = ?"); values.append(json.dumps(upd.attributes))
            if upd.status is not None:
                fields.append("status = ?"); values.append(upd.status)
            fields.append("updated_event_id = ?"); values.append(event_id)
            values.extend([task_id, upd.id])
            self.conn.execute(
                f"UPDATE workspace_entities SET {', '.join(fields)} WHERE task_id = ? AND id = ?",
                values,
            )

        for ev in patch.add_evidence:
            self.conn.execute(
                """INSERT INTO workspace_evidence
                   (task_id, entity_id, fact_key, field_key, source_event_id, source_url,
                    excerpt, confidence, verification_status, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (task_id, ev.entity_id, ev.fact_key, ev.field_key, ev.source_event_id,
                 ev.source_url, ev.excerpt, ev.confidence, "observed", now),
            )

        facts = dict(prior.facts)
        for f in patch.add_facts:
            facts[f.key] = f.value

        open_questions = list(prior.open_questions)
        for q in patch.open_questions_add:
            if q not in open_questions:
                open_questions.append(q)
        resolved = set(patch.open_questions_resolve)
        open_questions = [q for q in open_questions if q not in resolved]

        completion_requirements = (
            patch.completion_requirements
            if patch.completion_requirements is not None
            else prior.completion_requirements
        )

        blob = json.dumps({
            "facts": facts,
            "open_questions": open_questions,
            "completion_requirements": completion_requirements,
        })
        self.conn.execute(
            """INSERT INTO workspace_state (task_id, version, data, last_event_id, updated_at)
               VALUES (?, 1, ?, ?, ?)
               ON CONFLICT(task_id) DO UPDATE SET
                 version = workspace_state.version + 1,
                 data = excluded.data,
                 last_event_id = excluded.last_event_id,
                 updated_at = excluded.updated_at""",
            (task_id, blob, event_id, now),
        )
        self.conn.commit()

    # ------------------------------------------------------------------ read / rebuild

    def load(self, task_id: str) -> WorkspaceView:
        """Always returns a view at least as current as the event log; rebuilds when the
        stored projection row is missing or stale (mirrors TaskStateStore.load)."""
        true_max = self._max_workspace_event_id(task_id)
        row = self.conn.execute(
            "SELECT * FROM workspace_state WHERE task_id = ?", (task_id,)
        ).fetchone()
        if row is None:
            if true_max is None:
                return WorkspaceView(task_id=task_id)
            return self.rebuild(task_id)
        if row["last_event_id"] != true_max:
            return self.rebuild(task_id)
        return self._assemble_view(task_id, row)

    def rebuild_if_stale(self, task_id: str) -> WorkspaceView:
        return self.load(task_id)

    def rebuild(self, task_id: str) -> WorkspaceView:
        """Recreate all three projection tables from scratch by replaying every
        WORKSPACE_MUTATED event for this task in event-id order. Deterministic and
        idempotent: same events always produce the same projection state."""
        self.conn.execute("DELETE FROM workspace_state WHERE task_id = ?", (task_id,))
        self.conn.execute("DELETE FROM workspace_entities WHERE task_id = ?", (task_id,))
        self.conn.execute("DELETE FROM workspace_evidence WHERE task_id = ?", (task_id,))
        self.conn.commit()

        events = [e for e in self.event_store.all_events(task_id) if e.type == EventType.WORKSPACE_MUTATED]
        view = WorkspaceView(task_id=task_id)
        for event in events:
            patch = WorkspacePatch.model_validate(event.payload)
            self._apply_to_projections(task_id, patch, event.id, view)
            view = self._load_view_direct(task_id, event.id)
        return view if events else WorkspaceView(task_id=task_id)

    def _max_workspace_event_id(self, task_id: str) -> Optional[int]:
        row = self.conn.execute(
            "SELECT MAX(id) as max_id FROM events WHERE task_id = ? AND type = ?",
            (task_id, EventType.WORKSPACE_MUTATED.value),
        ).fetchone()
        return row["max_id"]

    def _load_entities(self, task_id: str) -> list[WorkspaceEntity]:
        rows = self.conn.execute(
            "SELECT * FROM workspace_entities WHERE task_id = ? ORDER BY id ASC", (task_id,)
        ).fetchall()
        return [
            WorkspaceEntity(
                id=r["id"], entity_type=r["entity_type"], name=r["name"],
                attributes=json.loads(r["attributes"]), status=r["status"],
            )
            for r in rows
        ]

    def _load_evidence(self, task_id: str) -> list[EvidenceRef]:
        rows = self.conn.execute(
            "SELECT * FROM workspace_evidence WHERE task_id = ? ORDER BY id ASC", (task_id,)
        ).fetchall()
        return [
            EvidenceRef(
                entity_id=r["entity_id"], fact_key=r["fact_key"], field_key=r["field_key"],
                source_event_id=r["source_event_id"], source_url=r["source_url"],
                excerpt=r["excerpt"], confidence=r["confidence"],
            )
            for r in rows
        ]

    def _assemble_view(self, task_id: str, state_row: sqlite3.Row) -> WorkspaceView:
        data = json.loads(state_row["data"])
        return WorkspaceView(
            task_id=task_id,
            version=state_row["version"],
            facts=data.get("facts", {}),
            open_questions=data.get("open_questions", []),
            completion_requirements=data.get("completion_requirements", []),
            entities=self._load_entities(task_id),
            evidence=self._load_evidence(task_id),
            last_event_id=state_row["last_event_id"],
        )

    def _load_view_direct(self, task_id: str, last_event_id: int) -> WorkspaceView:
        row = self.conn.execute(
            "SELECT * FROM workspace_state WHERE task_id = ?", (task_id,)
        ).fetchone()
        if row is None:
            return WorkspaceView(task_id=task_id, last_event_id=last_event_id)
        return self._assemble_view(task_id, row)
