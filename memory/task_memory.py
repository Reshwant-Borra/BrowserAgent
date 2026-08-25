"""Derived task-local summary and episodic memory.

The event log remains the source of truth. Rows here are a reconstructable working index
used to keep long prompts bounded: summaries and memories can be deleted and rebuilt from
events without changing task correctness.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Iterable

from agent.token_budget import count_tokens, trim_to_token_budget
from memory.event_store import Event, EventStore, EventType, now_iso


@dataclass(frozen=True)
class MemoryRecord:
    id: int
    task_id: str
    kind: str
    content: str
    source_event_id: int
    importance: float
    confidence: float


@dataclass(frozen=True)
class RunningSummary:
    summary: str
    source_event_ids: list[int]
    covered_event_id: int
    compaction_count: int


class TaskMemoryStore:
    def __init__(self, event_store: EventStore):
        self.event_store = event_store
        self.conn: sqlite3.Connection = event_store.conn

    def get_summary(self, task_id: str) -> RunningSummary | None:
        row = self.conn.execute(
            "SELECT * FROM task_summaries WHERE task_id = ?", (task_id,)
        ).fetchone()
        if row is None:
            return None
        return RunningSummary(
            summary=row["summary"],
            source_event_ids=json.loads(row["source_event_ids"]),
            covered_event_id=row["covered_event_id"],
            compaction_count=row["compaction_count"],
        )

    def compact_if_needed(
        self,
        task_id: str,
        events: list[Event],
        keep_last_steps: int,
        summary_token_budget: int,
        force: bool = False,
        rebuild_interval: int = 4,
    ) -> RunningSummary | None:
        if not events:
            return None
        summary = self.get_summary(task_id)
        completed_steps = sorted({ev.step for ev in events if ev.step > 0})
        if len(completed_steps) <= keep_last_steps:
            return summary
        cutoff_step = completed_steps[-keep_last_steps - 1]
        compactable = [
            ev for ev in events
            if ev.id is not None and ev.step <= cutoff_step and ev.type != EventType.TASK_CREATED
        ]
        if not compactable:
            return summary
        target_event_id = max(ev.id for ev in compactable if ev.id is not None)
        if summary and summary.covered_event_id >= target_event_id and not force:
            return summary

        next_count = (summary.compaction_count + 1) if summary else 1
        rebuild_from_canonical = force or (rebuild_interval > 0 and next_count % rebuild_interval == 0)
        if rebuild_from_canonical:
            source_events = [ev for ev in events if ev.id is not None and ev.id <= target_event_id]
            previous_summary = ""
        else:
            previous_summary = summary.summary if summary else ""
            previous_covered = summary.covered_event_id if summary else 0
            source_events = [
                ev for ev in events
                if ev.id is not None and previous_covered < ev.id <= target_event_id
            ]

        rendered = build_running_summary(source_events, summary_token_budget, previous_summary=previous_summary)
        source_ids = [
            ev.id for ev in (source_events if rebuild_from_canonical else compactable)
            if ev.id is not None
        ]
        return self._commit_summary(task_id, rendered, source_ids, target_event_id, next_count)

    def _commit_summary(
        self,
        task_id: str,
        summary: str,
        source_event_ids: list[int],
        covered_event_id: int,
        compaction_count: int,
    ) -> RunningSummary:
        now = now_iso()
        started_payload = json.dumps({"target_event_id": covered_event_id})
        created_payload = json.dumps({
            "covered_event_id": covered_event_id,
            "source_event_ids": source_event_ids,
            "summary_tokens": count_tokens(summary),
        })
        committed_payload = json.dumps({"covered_event_id": covered_event_id})
        with self.conn:
            self.conn.execute(
                "INSERT INTO events (task_id, step, timestamp, type, payload, verification_result) "
                "VALUES (?, ?, ?, ?, ?, NULL)",
                (task_id, 0, now, EventType.COMPACTION_STARTED.value, started_payload),
            )
            self.conn.execute(
                """INSERT INTO task_summaries
                   (task_id, summary, source_event_ids, covered_event_id, compaction_count, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(task_id) DO UPDATE SET
                     summary=excluded.summary,
                     source_event_ids=excluded.source_event_ids,
                     covered_event_id=excluded.covered_event_id,
                     compaction_count=excluded.compaction_count,
                     updated_at=excluded.updated_at
                """,
                (task_id, summary, json.dumps(source_event_ids), covered_event_id, compaction_count, now),
            )
            self.conn.execute(
                "INSERT INTO events (task_id, step, timestamp, type, payload, verification_result) "
                "VALUES (?, ?, ?, ?, ?, NULL)",
                (task_id, 0, now_iso(), EventType.SUMMARY_CREATED.value, created_payload),
            )
            self.conn.execute(
                "INSERT INTO events (task_id, step, timestamp, type, payload, verification_result) "
                "VALUES (?, ?, ?, ?, ?, NULL)",
                (task_id, 0, now_iso(), EventType.COMPACTION_COMMITTED.value, committed_payload),
            )
        return RunningSummary(summary, source_event_ids, covered_event_id, compaction_count)

    def ingest_events(self, task_id: str, events: Iterable[Event]) -> int:
        inserted = 0
        for event in events:
            if event.id is None:
                continue
            for kind, content, importance, confidence in derive_memories(event):
                if self.write_memory(task_id, kind, content, event.id, importance, confidence):
                    inserted += 1
        return inserted

    def write_memory(
        self,
        task_id: str,
        kind: str,
        content: str,
        source_event_id: int,
        importance: float = 0.5,
        confidence: float = 1.0,
    ) -> bool:
        content = " ".join(content.split())
        if not content:
            return False
        now = now_iso()
        with self.conn:
            cur = self.conn.execute(
                """INSERT OR IGNORE INTO task_memories
                   (task_id, kind, content, source_event_id, importance, confidence, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (task_id, kind, content, source_event_id, importance, confidence, now),
            )
            if cur.rowcount != 1:
                return False
            memory_id = cur.lastrowid
            self.conn.execute(
                "INSERT INTO task_memories_fts(rowid, content, kind) VALUES (?, ?, ?)",
                (memory_id, content, kind),
            )
        return True

    def rebuild(self, task_id: str, events: list[Event]) -> int:
        with self.conn:
            ids = [
                row["id"] for row in self.conn.execute(
                    "SELECT id FROM task_memories WHERE task_id = ?", (task_id,)
                ).fetchall()
            ]
            if ids:
                self.conn.executemany("DELETE FROM task_memories_fts WHERE rowid = ?", [(i,) for i in ids])
            self.conn.execute("DELETE FROM task_memories WHERE task_id = ?", (task_id,))
        return self.ingest_events(task_id, events)

    def search(
        self,
        task_id: str,
        query: str,
        top_k: int,
        token_budget: int,
    ) -> list[MemoryRecord]:
        query = _fts_query(query)
        if not query or top_k <= 0 or token_budget <= 0:
            return []
        rows = self.conn.execute(
            """SELECT m.*, bm25(task_memories_fts) AS rank
               FROM task_memories_fts
               JOIN task_memories m ON m.id = task_memories_fts.rowid
               WHERE task_memories_fts MATCH ? AND m.task_id = ?
               ORDER BY rank ASC, m.importance DESC, m.source_event_id DESC
               LIMIT ?""",
            (query, task_id, top_k * 3),
        ).fetchall()
        records: list[MemoryRecord] = []
        used = 0
        for row in rows:
            record = MemoryRecord(
                id=row["id"],
                task_id=row["task_id"],
                kind=row["kind"],
                content=row["content"],
                source_event_id=row["source_event_id"],
                importance=row["importance"],
                confidence=row["confidence"],
            )
            cost = count_tokens(f"{record.kind}: {record.content}")
            if records and used + cost > token_budget:
                break
            if cost > token_budget:
                continue
            records.append(record)
            used += cost
            if len(records) >= top_k:
                break
        if records:
            now = now_iso()
            with self.conn:
                self.conn.executemany(
                    "UPDATE task_memories SET last_used_at = ? WHERE id = ?",
                    [(now, r.id) for r in records],
                )
        return records


def derive_memories(event: Event) -> list[tuple[str, str, float, float]]:
    payload = event.payload
    memories: list[tuple[str, str, float, float]] = []
    if event.type == EventType.OBSERVATION:
        for line in payload.get("visible_text", []):
            if _looks_salient_observation_text(line):
                memories.append(("fact", f"Observed fact: {line}", 0.85, 1.0))
    elif event.type == EventType.ACTION_RESULT:
        data = payload.get("result_data") or {}
        filename = data.get("suggested_filename")
        path = data.get("path")
        if filename:
            content = f"Downloaded artifact {filename}"
            if path:
                content += f" to {path}"
            memories.append(("artifact", content, 1.0, 1.0))
        extracted = data.get("extracted")
        if extracted:
            memories.append(("fact", f"Extracted text: {extracted[:500]}", 0.75, 1.0))
    elif event.type == EventType.VERIFICATION_RESULT:
        passed = bool(event.verification_result and event.verification_result.get("passed"))
        action = payload.get("action")
        target = payload.get("target")
        url = payload.get("url")
        if passed:
            memories.append(("completed_step", f"Step {event.step} succeeded: {action} target={target} at {url}", 0.55, 1.0))
            data = payload.get("result_data") or {}
            if data.get("suggested_filename"):
                memories.append(("artifact", f"Verified download: {data['suggested_filename']}", 1.0, 1.0))
        else:
            memories.append(("failed_path", f"Step {event.step} failed: {action} target={target} at {url}", 0.65, 1.0))
    elif event.type == EventType.SUBGOAL_CHANGED:
        subgoal = payload.get("subgoal")
        if subgoal:
            memories.append(("decision", f"Current subgoal set to: {subgoal}", 0.6, 1.0))
    elif event.type == EventType.TASK_BLOCKED:
        memories.append(("blocker", f"Blocked: {payload.get('reason', 'unknown')}", 0.9, 1.0))
    return memories


def build_running_summary(
    events: list[Event],
    token_budget: int,
    previous_summary: str = "",
) -> str:
    progress: list[str] = []
    facts: list[str] = []
    failed: list[str] = []
    blockers: list[str] = []
    artifacts: list[str] = []

    for event in events:
        for kind, content, _importance, _confidence in derive_memories(event):
            if kind == "completed_step":
                progress.append(content)
            elif kind == "failed_path":
                failed.append(content)
            elif kind == "artifact":
                artifacts.append(content)
            elif kind in {"fact", "decision"}:
                facts.append(content)
            elif kind == "blocker":
                blockers.append(content)

    lines: list[str] = []
    if previous_summary:
        lines.extend(["Previous summary:", previous_summary, ""])
    if facts:
        lines.append("Important facts:")
        lines.extend(f"- {line}" for line in _dedupe_tail(facts, 12))
    if artifacts:
        lines.append("Artifacts:")
        lines.extend(f"- {line}" for line in _dedupe_tail(artifacts, 8))
    if failed:
        lines.append("Failed paths worth avoiding:")
        lines.extend(f"- {line}" for line in _dedupe_tail(failed, 8))
    if blockers:
        lines.append("Unresolved blockers:")
        lines.extend(f"- {line}" for line in _dedupe_tail(blockers, 6))
    if progress:
        lines.append("Progress:")
        lines.extend(f"- {line}" for line in _dedupe_tail(progress, 12))
    rendered = "\n".join(lines) if lines else "(no compacted history yet)"
    return trim_to_token_budget(rendered, token_budget)


def _dedupe_tail(items: list[str], limit: int) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in reversed(items):
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
        if len(out) >= limit:
            break
    return list(reversed(out))


def _looks_salient_observation_text(text: str) -> bool:
    lowered = text.lower()
    cues = (
        "required",
        "remember",
        "use mode",
        "use region",
        "token",
        "fact ",
        "the required",
    )
    return any(cue in lowered for cue in cues)


def _fts_query(text: str) -> str:
    words = []
    for raw in text.replace('"', " ").replace("'", " ").split():
        word = "".join(ch for ch in raw if ch.isalnum() or ch in "_-").strip("-_")
        if len(word) >= 3:
            words.append(word)
    return " OR ".join(f'"{word}"' for word in words[:12])
