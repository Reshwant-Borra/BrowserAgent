"""Derived task-local summary and episodic memory.

The event log remains the source of truth. Rows here are a reconstructable working index
used to keep long prompts bounded: summaries and memories can be deleted and rebuilt from
events without changing task correctness.
"""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from typing import Iterable

from agent.token_budget import count_tokens, trim_to_token_budget
from browser.page_model import PageObservation
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


@dataclass(frozen=True)
class ActiveFact:
    id: int
    task_id: str
    kind: str
    key: str
    value: str
    status: str
    source_event_id: int
    source_text: str
    confidence: float


@dataclass(frozen=True)
class RetrievalQuery:
    name: str
    terms: list[str]


@dataclass(frozen=True)
class RetrievalMetrics:
    query_terms: list[str]
    candidate_memory_ids: list[int]
    selected_memory_ids: list[int]
    selected_memory_kinds: list[str]
    selected_memory_source_event_ids: list[int]
    selected_memory_tokens: int
    retrieval_ms: float


@dataclass(frozen=True)
class MemorySearchResult:
    records: list[MemoryRecord]
    metrics: RetrievalMetrics


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
        source_events = [ev for ev in events if ev.id is not None and ev.id <= target_event_id]
        rendered = build_running_summary(source_events, summary_token_budget)
        source_ids = [ev.id for ev in source_events if ev.id is not None]
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

    def ingest_new_events(self, task_id: str) -> int:
        last_id = self._last_ingested_event_id(task_id)
        events = self.event_store.events_after(task_id, last_id)
        inserted = self.ingest_events(task_id, events)
        max_id = max((event.id or 0 for event in events), default=last_id)
        if max_id > last_id:
            self._set_last_ingested_event_id(task_id, max_id)
        return inserted

    def _last_ingested_event_id(self, task_id: str) -> int:
        row = self.conn.execute(
            "SELECT last_ingested_event_id FROM task_memory_ingest_state WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        return int(row["last_ingested_event_id"]) if row else 0

    def _set_last_ingested_event_id(self, task_id: str, event_id: int) -> None:
        now = now_iso()
        with self.conn:
            self.conn.execute(
                """INSERT INTO task_memory_ingest_state (task_id, last_ingested_event_id, updated_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(task_id) DO UPDATE SET
                     last_ingested_event_id=excluded.last_ingested_event_id,
                     updated_at=excluded.updated_at""",
                (task_id, event_id, now),
            )

    def ingest_events(self, task_id: str, events: Iterable[Event]) -> int:
        inserted = 0
        for event in events:
            if event.id is None:
                continue
            for kind, content, importance, confidence in derive_memories(event):
                if self.write_memory(task_id, kind, content, event.id, importance, confidence):
                    inserted += 1
            for fact in derive_active_facts(event):
                if self.write_active_fact(task_id, fact, event.id):
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

    def write_active_fact(
        self,
        task_id: str,
        fact: tuple[str, str, str, str, float],
        source_event_id: int,
    ) -> bool:
        kind, key, value, source_text, confidence = fact
        key = normalize_fact_key(key)
        value = _clean_fact_value(value)
        source_text = " ".join(source_text.split())
        if not key or not value:
            return False
        now = now_iso()
        with self.conn:
            cur = self.conn.execute(
                """INSERT OR IGNORE INTO active_task_facts
                   (task_id, kind, key, value, status, source_event_id, source_text, confidence, created_at)
                   VALUES (?, ?, ?, ?, 'active', ?, ?, ?, ?)""",
                (task_id, kind, key, value, source_event_id, source_text, confidence, now),
            )
        return cur.rowcount == 1

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
            self.conn.execute("DELETE FROM active_task_facts WHERE task_id = ?", (task_id,))
            self.conn.execute("DELETE FROM task_memory_ingest_state WHERE task_id = ?", (task_id,))
        inserted = self.ingest_events(task_id, events)
        max_id = max((event.id or 0 for event in events), default=0)
        if max_id:
            self._set_last_ingested_event_id(task_id, max_id)
        return inserted

    def active_facts(
        self,
        task_id: str,
        observation: PageObservation | None = None,
        token_budget: int = 300,
        limit: int = 12,
    ) -> list[ActiveFact]:
        rows = self.conn.execute(
            """SELECT * FROM active_task_facts
               WHERE task_id = ? AND status = 'active'
               ORDER BY confidence DESC, source_event_id DESC, id DESC""",
            (task_id,),
        ).fetchall()
        raw_facts = [
            ActiveFact(
                id=row["id"],
                task_id=row["task_id"],
                kind=row["kind"],
                key=row["key"],
                value=row["value"],
                status=row["status"],
                source_event_id=row["source_event_id"],
                source_text=row["source_text"],
                confidence=row["confidence"],
            )
            for row in rows
        ]
        facts: list[ActiveFact] = []
        seen_facts: set[tuple[str, str, str]] = set()
        for fact in raw_facts:
            dedupe_key = (fact.kind, normalize_fact_key(fact.key), fact.value.strip().lower())
            if dedupe_key in seen_facts:
                continue
            seen_facts.add(dedupe_key)
            facts.append(fact)
        if observation is not None:
            page_terms = set(extract_page_terms(observation, include_options=False))
            facts.sort(key=lambda f: (
                normalize_fact_key(f.key) not in page_terms,
                -f.confidence,
                -f.source_event_id,
            ))
        selected: list[ActiveFact] = []
        used = 0
        for fact in facts:
            cost = count_tokens(f"{fact.key} = {fact.value} [event {fact.source_event_id}]")
            if selected and used + cost > token_budget:
                break
            if cost > token_budget:
                continue
            selected.append(fact)
            used += cost
            if len(selected) >= limit:
                break
        if selected:
            now = now_iso()
            with self.conn:
                self.conn.executemany(
                    "UPDATE active_task_facts SET last_used_at = ? WHERE id = ?",
                    [(now, fact.id) for fact in selected],
                )
        return selected

    def search(
        self,
        task_id: str,
        query: str,
        top_k: int,
        token_budget: int,
        ) -> list[MemoryRecord]:
        terms = _query_terms(query)
        if not terms or top_k <= 0 or token_budget <= 0:
            return []
        return self.search_queries(
            task_id,
            [RetrievalQuery("query", terms)],
            top_k=top_k,
            token_budget=token_budget,
        ).records

    def search_queries(
        self,
        task_id: str,
        queries: list[RetrievalQuery],
        top_k: int,
        token_budget: int,
    ) -> MemorySearchResult:
        import time

        started = time.monotonic()
        if top_k <= 0 or token_budget <= 0:
            return MemorySearchResult([], RetrievalMetrics([], [], [], [], [], 0, 0.0))
        ranked: dict[int, tuple[float, sqlite3.Row]] = {}
        all_terms: list[str] = []
        for idx, query_group in enumerate(queries):
            terms = _bounded_unique_terms(query_group.terms, 12)
            all_terms.extend(terms)
            fts = _fts_query_from_terms(terms)
            if not fts:
                continue
            rows = self.conn.execute(
                """SELECT m.*, bm25(task_memories_fts) AS rank
                   FROM task_memories_fts
                   JOIN task_memories m ON m.id = task_memories_fts.rowid
                   WHERE task_memories_fts MATCH ? AND m.task_id = ?
                   ORDER BY rank ASC, m.importance DESC, m.source_event_id DESC
                   LIMIT ?""",
                (fts, task_id, top_k * 5),
            ).fetchall()
            for row in rows:
                priority_bonus = idx * 0.25
                kind_penalty = _memory_kind_penalty(row["kind"])
                score = float(row["rank"]) + priority_bonus + kind_penalty - float(row["importance"])
                current = ranked.get(row["id"])
                if current is None or score < current[0]:
                    ranked[row["id"]] = (score, row)
        ordered_rows = [
            row for _score, row in sorted(
                ranked.values(),
                key=lambda item: (
                    item[0],
                    -float(item[1]["importance"]),
                    -int(item[1]["source_event_id"]),
                ),
            )
        ]
        records: list[MemoryRecord] = []
        used = 0
        for row in ordered_rows:
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
        elapsed_ms = (time.monotonic() - started) * 1000
        metrics = RetrievalMetrics(
            query_terms=_bounded_unique_terms(all_terms, 50),
            candidate_memory_ids=[row["id"] for row in ordered_rows],
            selected_memory_ids=[record.id for record in records],
            selected_memory_kinds=[record.kind for record in records],
            selected_memory_source_event_ids=[record.source_event_id for record in records],
            selected_memory_tokens=used,
            retrieval_ms=elapsed_ms,
        )
        return MemorySearchResult(records, metrics)


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
            data = payload.get("result_data") or {}
            if data.get("suggested_filename"):
                memories.append(("artifact", f"Verified download: {data['suggested_filename']}", 1.0, 1.0))
            elif action in {"extract", "download"}:
                memories.append(("completed_step", f"Completed {action} target={target} at {url}", 0.3, 1.0))
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
    requirements: list[str] = []
    collected: list[str] = []
    milestones: list[str] = []
    failed: list[str] = []
    blockers: list[str] = []
    artifacts: list[str] = []

    for event in events:
        for kind, key, value, source_text, _confidence in derive_active_facts(event):
            line = f"{key} = {value} [event {event.id}]"
            if kind == "collected_fact":
                collected.append(line)
            elif kind == "artifact_target":
                artifacts.append(line)
            else:
                requirements.append(line)
        for kind, content, _importance, _confidence in derive_memories(event):
            if kind == "failed_path":
                failed.append(content)
            elif kind == "artifact":
                artifacts.append(content)
            elif kind == "blocker":
                blockers.append(content)
            elif kind == "completed_step":
                milestones.append(content)

    sections = [
        ("ACTIVE REQUIREMENTS", _dedupe_semantic(requirements, 12)),
        ("COLLECTED FACTS", _dedupe_semantic(collected, 12)),
        ("ARTIFACTS", _dedupe_semantic(artifacts, 8)),
        ("COMPLETED MILESTONES", _dedupe_semantic(milestones, 6)),
        ("FAILED APPROACHES WORTH AVOIDING", _dedupe_semantic(failed, 8)),
        ("CURRENT BLOCKERS", _dedupe_semantic(blockers, 6)),
    ]
    lines = ["TASK MEMORY SUMMARY"]
    for heading, items in sections:
        lines.append("")
        lines.append(heading)
        if items:
            lines.extend(f"- {item}" for item in items)
        else:
            lines.append("(none)")
    rendered = "\n".join(lines)
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
        "use ",
        "fact ",
        "the required",
        " = ",
        ":",
    )
    return any(cue in lowered for cue in cues)


def _fts_query(text: str) -> str:
    return _fts_query_from_terms(_query_terms(text)[:12])


def build_retrieval_queries(
    task_goal: str,
    success_criteria: list[str],
    current_subgoal: str | None,
    observation: PageObservation,
    active_facts: list[ActiveFact] | None = None,
) -> list[RetrievalQuery]:
    page_terms = extract_page_terms(observation, include_options=True)
    fact_terms = [fact.key for fact in active_facts or []]
    groups = [
        RetrievalQuery("current_page_affordances", _bounded_unique_terms(page_terms, 8)),
        RetrievalQuery("current_subgoal", _bounded_unique_terms(_query_terms(current_subgoal or ""), 4)),
        RetrievalQuery("active_fact_keys", _bounded_unique_terms(fact_terms, 4)),
        RetrievalQuery("goal", _bounded_unique_terms(_query_terms(task_goal), 5)),
        RetrievalQuery("completion_criteria", _bounded_unique_terms(_query_terms(" ".join(success_criteria)), 5)),
        RetrievalQuery("page_title_domain", _bounded_unique_terms(_query_terms(f"{observation.title} {observation.url}"), 4)),
    ]
    return [group for group in groups if group.terms]


def extract_page_terms(observation: PageObservation, include_options: bool = True) -> list[str]:
    terms: list[str] = []
    control_roles = {"textbox", "select", "combobox", "button", "link", "checkbox", "radio"}
    for element in observation.elements:
        if element.disabled or element.role not in control_roles:
            continue
        terms.extend(_query_terms(element.name))
        if include_options and element.options and element.role in {"select", "combobox"}:
            for option in element.options[:6]:
                terms.extend(_query_terms(option))
    for text in observation.visible_text[:3]:
        terms.extend(_query_terms(text))
    return _bounded_unique_terms(terms, 30)


def derive_active_facts(event: Event) -> list[tuple[str, str, str, str, float]]:
    texts: list[str] = []
    if event.type == EventType.OBSERVATION:
        texts.extend(event.payload.get("visible_text", []))
    elif event.type == EventType.ACTION_RESULT:
        data = event.payload.get("result_data") or {}
        if data.get("extracted"):
            texts.append(data["extracted"])
        if data.get("suggested_filename"):
            texts.append(f"artifact = {data['suggested_filename']}")
    facts: list[tuple[str, str, str, str, float]] = []
    for text in texts:
        for line in _candidate_fact_lines(text):
            facts.extend(_extract_facts_from_line(line))
    return facts


def normalize_fact_key(key: str) -> str:
    key = key.strip().lower()
    key = re.sub(r"^(the|required|required\s+)", "", key).strip()
    key = re.sub(r"[^a-z0-9 _-]+", "", key)
    key = re.sub(r"\s+", " ", key).strip(" -_")
    return key[:48]


def _candidate_fact_lines(text: str) -> list[str]:
    parts = re.split(r"[\n\r]+", text)
    out: list[str] = []
    for part in parts:
        for segment in re.split(r"(?<=[.;])\s+", part):
            segment = segment.strip()
            if segment and _looks_salient_observation_text(segment):
                out.append(segment)
    return out


def _extract_facts_from_line(line: str) -> list[tuple[str, str, str, str, float]]:
    clean = " ".join(line.strip().split())
    patterns = [
        ("collected_fact", re.compile(r"^(fact\s*\d+)\s*[:=-]\s*(.+)$", re.IGNORECASE)),
        ("requirement", re.compile(r"^use\s+(?:the\s+)?([a-z][a-z0-9 _-]{1,40}?)\s+(.+)$", re.IGNORECASE)),
        ("requirement", re.compile(r"^remember\s+(?:the\s+)?([a-z][a-z0-9 _-]{1,40}?)\s+(.+)$", re.IGNORECASE)),
        ("requirement", re.compile(r"^the\s+required\s+([a-z][a-z0-9 _-]{1,40}?)\s+(?:is|=)\s+(.+)$", re.IGNORECASE)),
        ("requirement", re.compile(r"^required\s+([a-z][a-z0-9 _-]{1,40}?)\s+(?:is|=)\s+(.+)$", re.IGNORECASE)),
        ("requirement", re.compile(r"^([a-z][a-z0-9 _-]{1,40}?)\s*[:=]\s*(.+)$", re.IGNORECASE)),
    ]
    facts: list[tuple[str, str, str, str, float]] = []
    for kind, pattern in patterns:
        match = pattern.match(clean)
        if not match:
            continue
        key = normalize_fact_key(match.group(1))
        value = _clean_fact_value(match.group(2))
        if key and value and not _looks_like_status_label(key) and not _looks_like_sentence_value(value):
            fact_kind = "artifact_target" if key in {"artifact", "file", "filename", "download"} else kind
            facts.append((fact_kind, key, value, clean, 1.0))
            break
    return facts


def _clean_fact_value(value: str) -> str:
    value = value.strip().strip('"').strip("'")
    value = re.split(r"\s+(?:for this task|when needed|later|at the end|on the final page)\b", value, maxsplit=1, flags=re.IGNORECASE)[0]
    value = value.split(";")[0].strip()
    value = value.rstrip(". ")
    return value[:120]


def _looks_like_sentence_value(value: str) -> bool:
    words = value.split()
    if len(words) > 8:
        return True
    return "," in value and len(words) > 3


def _looks_like_status_label(key: str) -> bool:
    return key.startswith("confirming ") or key.startswith("section ")


def _query_terms(text: str) -> list[str]:
    words = []
    for raw in (text or "").replace('"', " ").replace("'", " ").split():
        word = "".join(ch for ch in raw if ch.isalnum() or ch in "_-").strip("-_")
        if len(word) >= 3:
            words.append(word.lower())
    return words


def _bounded_unique_terms(terms: list[str], limit: int) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for raw in terms:
        for term in _query_terms(raw):
            if term in seen:
                continue
            seen.add(term)
            out.append(term)
            if len(out) >= limit:
                return out
    return out


def _fts_query_from_terms(terms: list[str]) -> str:
    return " OR ".join(f'"{word}"' for word in _bounded_unique_terms(terms, 12))


def _memory_kind_penalty(kind: str) -> float:
    return {
        "fact": 0.0,
        "artifact": 0.0,
        "blocker": 0.1,
        "failed_path": 0.25,
        "decision": 0.4,
        "completed_step": 1.5,
    }.get(kind, 0.5)


def _dedupe_semantic(items: list[str], limit: int) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in reversed(items):
        key = re.sub(r"\s+", " ", item.lower()).strip()
        key = re.sub(r"\s+\[event \d+\]", "", key)
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
        if len(out) >= limit:
            break
    return list(reversed(out))
