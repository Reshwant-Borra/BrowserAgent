"""`browser-agent trace`: reconstructs a human-readable timeline for a job purely from
persisted EventStore/UIJobStore/BatchStore/WorkflowStore data — it never runs, resumes, or
replays a task, and it never touches the browser. This exists because the raw data (task.db
events, batch.db work items, jobs.db rows) is already sufficient to answer "what did this run
actually do", but reconstructing that by hand (as this module's forensic ancestor did, by
querying each sqlite file manually) is too slow for routine use.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from agent.config import AppConfig
from agent.logging_utils import redact_dict
from memory.event_store import Event, EventStore, EventType


def _connect(db_path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    return con


def _row(db_path: Path, query: str, params: tuple = ()) -> Optional[dict]:
    if not db_path.exists():
        return None
    con = _connect(db_path)
    try:
        cur = con.execute(query, params)
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        con.close()


def _rows(db_path: Path, query: str, params: tuple = ()) -> list[dict]:
    if not db_path.exists():
        return []
    con = _connect(db_path)
    try:
        cur = con.execute(query, params)
        return [dict(r) for r in cur.fetchall()]
    finally:
        con.close()


def jobs_db_path(config: AppConfig) -> Path:
    return Path(config.storage.runtime_dir) / "ui" / "jobs.db"


def batch_db_path(config: AppConfig, batch_id: str) -> Path:
    return Path(config.storage.runtime_dir) / "batches" / batch_id / "batch.db"


def workflow_db_path(config: AppConfig, workflow_id: str) -> Path:
    return Path(config.storage.runtime_dir) / "workflows" / workflow_id / "workflow.db"


def task_db_path(config: AppConfig, task_id: str) -> Path:
    return Path(config.storage.tasks_dir) / task_id / "task.db"


def metrics_path(config: AppConfig, task_id: str) -> Path:
    return Path(config.logging.dir) / f"{task_id}.metrics.jsonl"


def find_recent_job(config: AppConfig) -> Optional[dict]:
    return _row(jobs_db_path(config), "SELECT * FROM ui_jobs ORDER BY created_at DESC LIMIT 1")


def find_job(config: AppConfig, job_id: str) -> Optional[dict]:
    return _row(jobs_db_path(config), "SELECT * FROM ui_jobs WHERE id = ?", (job_id,))


@dataclass
class ChildTaskRef:
    """One AgentLoop trajectory involved in a job, with enough context to label it in output
    without the caller having to already know what a batch/workflow item is."""

    task_id: str
    label: str
    status: Optional[str] = None
    failure_category: Optional[str] = None


def resolve_child_tasks(config: AppConfig, job: dict) -> list[ChildTaskRef]:
    """job -> every AgentLoop trajectory it involved. Single-site jobs have exactly one
    (job['task_id']); batch/research jobs fan out to one child per work item; ordered
    workflows fan out to one child per step. A job can carry more than one of task_id/
    batch_id/workflow_id across its lifetime (e.g. a research job replans into a follow-up
    single-site task — see ui/jobs.py's mixed_intent_followup) so all three are checked."""
    refs: list[ChildTaskRef] = []
    if job.get("task_id"):
        refs.append(ChildTaskRef(task_id=job["task_id"], label="task", status=job.get("status")))
    if job.get("batch_id"):
        items = _rows(
            batch_db_path(config, job["batch_id"]),
            "SELECT ordinal, target, status, failure_category, browser_task_id "
            "FROM batch_work_items ORDER BY ordinal, id",
        )
        for item in items:
            if not item.get("browser_task_id"):
                continue
            refs.append(ChildTaskRef(
                task_id=item["browser_task_id"],
                label=f"work item {item['ordinal']} ({item['target']})",
                status=item.get("status"),
                failure_category=item.get("failure_category"),
            ))
    if job.get("workflow_id"):
        steps = _rows(
            workflow_db_path(config, job["workflow_id"]),
            "SELECT ordinal, target, status, browser_task_id FROM workflow_steps ORDER BY ordinal",
        )
        for step in steps:
            if not step.get("browser_task_id"):
                continue
            refs.append(ChildTaskRef(
                task_id=step["browser_task_id"],
                label=f"step {step['ordinal']} ({step['target']})",
                status=step.get("status"),
            ))
    return refs


@dataclass
class StepTrace:
    step: int
    url_before: Optional[str] = None
    url_after: Optional[str] = None
    title: Optional[str] = None
    action: Optional[str] = None
    target: Any = None
    decision_error: Optional[str] = None
    action_error: Optional[str] = None
    verified: Optional[bool] = None
    verification_reason: Optional[str] = None
    recovery_transitions: list[dict] = field(default_factory=list)
    started_at: Optional[str] = None
    ended_at: Optional[str] = None
    raw_events: list[Event] = field(default_factory=list)


@dataclass
class TaskTrace:
    task_id: str
    label: str
    goal: Optional[str] = None
    status: Optional[str] = None
    steps: list[StepTrace] = field(default_factory=list)
    model_call_count: int = 0
    action_count: int = 0
    verification_failures: int = 0
    recovery_transitions: int = 0
    latencies_ms: list[float] = field(default_factory=list)
    action_latencies_ms: list[float] = field(default_factory=list)
    duration_s: Optional[float] = None


def _parse_ts(ts: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def build_task_trace(config: AppConfig, task_id: str, label: str) -> Optional[TaskTrace]:
    db_path = task_db_path(config, task_id)
    if not db_path.exists():
        return None
    es = EventStore(db_path)
    try:
        events = es.all_events(task_id)
    finally:
        es.close()
    if not events:
        return None

    trace = TaskTrace(task_id=task_id, label=label)
    steps_by_no: dict[int, StepTrace] = {}

    for e in events:
        if e.type == EventType.TASK_CREATED:
            trace.goal = e.payload.get("goal")
            continue
        step_no = e.step
        st = steps_by_no.setdefault(step_no, StepTrace(step=step_no))
        st.raw_events.append(e)
        if st.started_at is None:
            st.started_at = e.timestamp
        st.ended_at = e.timestamp

        if e.type == EventType.OBSERVATION:
            phase = e.payload.get("phase")
            if phase == "pre_decision" or st.url_before is None:
                st.url_before = e.payload.get("url")
                st.title = e.payload.get("title")
            else:
                st.url_after = e.payload.get("url")
        elif e.type == EventType.MODEL_DECISION:
            trace.model_call_count += 1
            if e.payload.get("error"):
                st.decision_error = e.payload.get("message") or e.payload.get("error")
            else:
                decision = e.payload.get("decision") or {}
                st.action = decision.get("action")
                st.target = decision.get("target")
        elif e.type == EventType.ACTION_INTENT:
            trace.action_count += 1
            st.action = st.action or e.payload.get("action")
            st.target = st.target if st.target is not None else e.payload.get("target")
        elif e.type == EventType.ACTION_RESULT:
            if e.payload.get("error"):
                st.action_error = e.payload.get("error")
        elif e.type == EventType.VERIFICATION_RESULT:
            vr = e.verification_result
            if vr is not None:
                st.verified = bool(vr.get("passed"))
                if not st.verified:
                    trace.verification_failures += 1
                    checks = vr.get("checks") or []
                    reasons = [c.get("type") for c in checks if isinstance(c, dict) and not c.get("passed", True)]
                    if reasons:
                        st.verification_reason = ", ".join(str(r) for r in reasons if r)
        elif e.type == EventType.RECOVERY_TRANSITION:
            trace.recovery_transitions += 1
            st.recovery_transitions.append(e.payload)

    trace.steps = [steps_by_no[k] for k in sorted(steps_by_no) if k > 0]

    metrics_file = metrics_path(config, task_id)
    if metrics_file.exists():
        for line in metrics_file.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("event") == "model_call" and rec.get("total_latency_ms") is not None:
                trace.latencies_ms.append(float(rec["total_latency_ms"]))
            elif rec.get("event") == "action" and rec.get("latency_ms") is not None:
                trace.action_latencies_ms.append(float(rec["latency_ms"]))

    all_ts = [e.timestamp for e in events]
    start = _parse_ts(all_ts[0]) if all_ts else None
    end = _parse_ts(all_ts[-1]) if all_ts else None
    if start is not None and end is not None:
        trace.duration_s = (end - start).total_seconds()

    return trace


def _percentile(values: list[float], pct: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round(pct * (len(ordered) - 1)))))
    return ordered[idx]


def render_task_trace(trace: TaskTrace, verbose: bool) -> str:
    lines: list[str] = []
    lines.append(f"--- {trace.label} (task {trace.task_id}) ---")
    if trace.goal:
        lines.append(f"Goal: {trace.goal}")
    lines.append(f"Steps: {len(trace.steps)}  Model calls: {trace.model_call_count}  "
                  f"Actions: {trace.action_count}  Verification failures: {trace.verification_failures}  "
                  f"Recovery transitions: {trace.recovery_transitions}")
    if trace.duration_s is not None:
        lines.append(f"Duration: {trace.duration_s:.1f}s")
    if trace.latencies_ms:
        lines.append(
            f"Model latency ms  p50={_percentile(trace.latencies_ms, 0.5):.0f} "
            f"p95={_percentile(trace.latencies_ms, 0.95):.0f} max={max(trace.latencies_ms):.0f}"
        )
    if trace.action_latencies_ms:
        lines.append(
            f"Action latency ms p50={_percentile(trace.action_latencies_ms, 0.5):.0f} "
            f"p95={_percentile(trace.action_latencies_ms, 0.95):.0f} max={max(trace.action_latencies_ms):.0f}"
        )
    lines.append("")

    for st in trace.steps:
        lines.append(f"Step {st.step}")
        if st.url_before:
            lines.append(f"  URL:      {st.url_before}")
        if st.action:
            target_part = f" target={st.target}" if st.target is not None else ""
            lines.append(f"  Action:   {st.action}{target_part}")
        if st.decision_error:
            lines.append(f"  Decision error: {st.decision_error}")
        if st.action_error:
            first_line = st.action_error.splitlines()[0]
            lines.append(f"  Result:   error: {first_line}")
        elif st.action:
            lines.append("  Result:   ok")
        if st.verified is not None:
            verdict = "PASS" if st.verified else "FAIL"
            reason = f" ({st.verification_reason})" if st.verification_reason else ""
            lines.append(f"  Verified: {verdict}{reason}")
        for rt in st.recovery_transitions:
            lines.append(f"  Recovery: {rt.get('from')} -> {rt.get('to')} ({rt.get('reason')})")
        if st.started_at and st.ended_at:
            start, end = _parse_ts(st.started_at), _parse_ts(st.ended_at)
            if start and end:
                lines.append(f"  Duration: {(end - start).total_seconds():.2f}s")
        if verbose:
            if st.url_after and st.url_after != st.url_before:
                lines.append(f"  URL after: {st.url_after}")
            for e in st.raw_events:
                lines.append(f"    [{e.type.value}] {json.dumps(redact_dict(e.payload), default=str)}")
        lines.append("")

    return "\n".join(lines)


def render_job_header(job: dict) -> str:
    lines = [
        f"Job: {job['id']}",
        f"Prompt: {job.get('prompt', '')}",
        f"Kind: {job.get('kind', '')}",
        f"Status: {job.get('status', '')}",
    ]
    created, updated = job.get("created_at"), job.get("updated_at")
    if created and updated:
        start, end = _parse_ts(created), _parse_ts(updated)
        if start and end:
            lines.append(f"Duration: {(end - start).total_seconds():.1f}s "
                          f"({created} -> {updated})")
    if job.get("error"):
        lines.append(f"Error: {job['error']}")
    return "\n".join(lines)
