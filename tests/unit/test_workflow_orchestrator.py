"""Ordered multi-site workflow orchestration (Section 15-17/33-34): sequential execution,
verify-before-advance, retry/block on verification failure, and structured cross-site fact
passing — using a FakeRunner (same style as tests/unit/test_batch_orchestrator.py's) so this
exercises real orchestration/persistence logic without a live model or browser.
"""
from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Any

from memory.event_store import EventStore, EventType
from memory.models import TaskState
from memory.task_state import TaskStateStore
from workflow.models import WorkflowPolicy, WorkflowStepStatus
from workflow.orchestrator import WorkflowOrchestrator
from workflow.store import WorkflowStore


class FakeWorkflowRunner:
    def __init__(self, outcomes: list[dict[str, Any]]):
        self.outcomes = list(outcomes)
        self.calls: list[dict[str, Any]] = []

    async def run_child(
        self, config, batch_id, work_item, child_goal, success_criteria, profile_dir,
        max_steps, resume_task_id=None, runtime_policy=None, approval_callback=None,
        seed_facts=None,
    ) -> str:
        self.calls.append({
            "ordinal": work_item["ordinal"], "target": work_item["target"], "goal": child_goal,
            "approval_callback": approval_callback, "seed_facts": seed_facts,
        })
        outcome = self.outcomes.pop(0)
        task_id = uuid.uuid4().hex[:12]
        _write_step_task(config, task_id, work_item["target"], outcome)
        return task_id


def _write_step_task(config, task_id: str, target: str, outcome: dict[str, Any]) -> None:
    """Writes a TASK_COMPLETED event using the structured verified/outputs contract
    (agent/schemas.py::FinishAction, persisted by agent/loop.py::_handle_finish) — not the
    old JSON-embedded-in-the-result-string shape workflow/orchestrator.py used to parse."""
    db_path = Path(config.storage.tasks_dir) / task_id / "task.db"
    store = EventStore(db_path)
    try:
        store.create_task(task_id, f"step {target}", [])
        store.append(task_id, 0, EventType.TASK_CREATED, {"goal": f"step {target}", "success_criteria": []})
        if outcome["status"] == "completed":
            result = outcome["result"]
            store.append(task_id, 1, EventType.TASK_COMPLETED, {
                "result": result.get("summary", ""), "final_url": target, "final_title": "Fixture",
                "final_text_excerpt": outcome.get("final_text_excerpt", "evidence"),
                "verified": result.get("verified"),
                "outputs": result.get("outputs", []),
            })
            state = TaskState(task_id=task_id, current_step=1, status="completed", last_event_id=store.max_event_id(task_id))
        elif outcome["status"] == "blocked":
            store.append(task_id, 1, EventType.TASK_BLOCKED, {"reason": outcome.get("blocked_reason", "blocked")})
            state = TaskState(task_id=task_id, current_step=1, status="blocked",
                               blocked_reason=outcome.get("blocked_reason", "blocked"),
                               last_event_id=store.max_event_id(task_id))
        else:
            state = TaskState(task_id=task_id, current_step=0, status="running", last_event_id=store.max_event_id(task_id))
        TaskStateStore(store).save(state)
    finally:
        store.close()


def _make_store(tmp_path, objective: str, steps: list[dict[str, Any]], policy: WorkflowPolicy | None = None):
    store = WorkflowStore(tmp_path / "workflows" / "w1" / "workflow.db")
    policy = policy or WorkflowPolicy(max_attempts_per_step=2)
    workflow_id = store.create_workflow(objective, steps, policy, workflow_id="w1")
    return store, workflow_id, policy


def _verified(summary: str = "ok", facts: dict | None = None) -> dict:
    outputs = [{"key": key, "value": value} for key, value in (facts or {}).items()]
    return {"status": "completed", "result": {"verified": True, "summary": summary, "outputs": outputs}}


def _unverified(reason: str = "state not confirmed") -> dict:
    return {"status": "completed", "result": {"verified": False, "summary": reason, "outputs": []}}


def test_workflow_completes_sequentially(tmp_config, tmp_path):
    steps = [
        {"ordinal": 1, "target": "http://a.test", "objective": "set mode to compact"},
        {"ordinal": 2, "target": "http://b.test", "objective": "enable notifications"},
    ]
    store, workflow_id, policy = _make_store(tmp_path, "run workflow", steps)
    runner = FakeWorkflowRunner([_verified("step1 done"), _verified("step2 done")])
    try:
        final = asyncio.run(WorkflowOrchestrator(tmp_config, store, workflow_id, policy, runner).run())
        assert final["status"] == "completed"
        assert [s["status"] for s in final["steps"]] == [WorkflowStepStatus.COMPLETED.value] * 2
        assert len(runner.calls) == 2
        assert runner.calls[0]["ordinal"] == 1
        assert runner.calls[1]["ordinal"] == 2
    finally:
        store.close()


def test_workflow_blocks_after_verification_failures_exhausted(tmp_path, tmp_config):
    steps = [
        {"ordinal": 1, "target": "http://a.test", "objective": "set mode to compact"},
        {"ordinal": 2, "target": "http://b.test", "objective": "enable notifications"},
    ]
    store, workflow_id, policy = _make_store(tmp_path, "run workflow", steps, WorkflowPolicy(max_attempts_per_step=2))
    # step 1 fails verification twice (max_attempts_per_step=2) -> workflow blocks, step 2 never runs.
    runner = FakeWorkflowRunner([_unverified("dropdown still shows Standard"), _unverified("dropdown still shows Standard")])
    try:
        final = asyncio.run(WorkflowOrchestrator(tmp_config, store, workflow_id, policy, runner).run())
        assert final["status"] == "blocked"
        assert final["steps"][0]["status"] == WorkflowStepStatus.FAILED.value
        assert final["steps"][1]["status"] == WorkflowStepStatus.PENDING.value  # never advanced
        assert len(runner.calls) == 2  # only step 1, retried once
    finally:
        store.close()


def test_workflow_retries_then_succeeds(tmp_path, tmp_config):
    steps = [{"ordinal": 1, "target": "http://a.test", "objective": "set mode to compact"}]
    store, workflow_id, policy = _make_store(tmp_path, "run workflow", steps, WorkflowPolicy(max_attempts_per_step=2))
    runner = FakeWorkflowRunner([_unverified("not yet"), _verified("now compact")])
    try:
        final = asyncio.run(WorkflowOrchestrator(tmp_config, store, workflow_id, policy, runner).run())
        assert final["status"] == "completed"
        assert final["steps"][0]["status"] == WorkflowStepStatus.COMPLETED.value
        assert len(runner.calls) == 2
    finally:
        store.close()


def test_workflow_passes_structured_facts_between_steps(tmp_path, tmp_config):
    """Section 33/34: a fact discovered on step 1 (project code) must be passed into step 2's
    goal as an explicit structured block, not left to the model to recall."""
    steps = [
        {"ordinal": 1, "target": "http://a.test", "objective": "find the project code"},
        {"ordinal": 2, "target": "http://b.test", "objective": "enter the project code"},
    ]
    store, workflow_id, policy = _make_store(tmp_path, "cross-site dependency", steps)
    runner = FakeWorkflowRunner([_verified("found AX-42", facts={"project_code": "AX-42"}), _verified("saved")])
    try:
        final = asyncio.run(WorkflowOrchestrator(tmp_config, store, workflow_id, policy, runner).run())
        assert final["status"] == "completed"
        assert final["steps"][0]["facts"] == {"project_code": "AX-42"}
        assert "AX-42" in runner.calls[1]["goal"]
    finally:
        store.close()


def test_workflow_passes_multiple_facts_across_three_steps(tmp_path, tmp_config):
    """Fact-test-matrix item (docs/PHASE5B_REPORT.md corrective pass): step 1 discovers two
    facts, step 2 uses one, step 3 uses the other — and step 3 still sees both (facts_so_far
    accumulates everything verified before its ordinal, not just the immediately-prior step)."""
    steps = [
        {"ordinal": 1, "target": "http://a.test", "objective": "find the build filename and version"},
        {"ordinal": 2, "target": "http://b.test", "objective": "enter the filename"},
        {"ordinal": 3, "target": "http://c.test", "objective": "enter the version"},
    ]
    store, workflow_id, policy = _make_store(tmp_path, "multi-fact dependency", steps)
    runner = FakeWorkflowRunner([
        _verified("found build info", facts={"filename": "report-v3.zip", "version": "3.2.1"}),
        _verified("filename entered"),
        _verified("version entered"),
    ])
    try:
        final = asyncio.run(WorkflowOrchestrator(tmp_config, store, workflow_id, policy, runner).run())
        assert final["status"] == "completed"
        assert final["steps"][0]["facts"] == {"filename": "report-v3.zip", "version": "3.2.1"}
        assert "report-v3.zip" in runner.calls[1]["goal"]
        assert runner.calls[1]["seed_facts"] == {"filename": "report-v3.zip", "version": "3.2.1"}
        assert "3.2.1" in runner.calls[2]["goal"]
        assert "report-v3.zip" in runner.calls[2]["goal"]  # still available, not dropped after step 2
    finally:
        store.close()


def test_workflow_does_not_invent_facts_when_absent(tmp_path, tmp_config):
    """Section 8 of the corrective pass: when a step discovers nothing reusable, the next
    step's goal must say so plainly rather than fabricating or carrying over a stale value."""
    steps = [
        {"ordinal": 1, "target": "http://a.test", "objective": "check for a project code"},
        {"ordinal": 2, "target": "http://b.test", "objective": "proceed without a project code"},
    ]
    store, workflow_id, policy = _make_store(tmp_path, "no code available", steps)
    runner = FakeWorkflowRunner([_verified("no code was shown on the page", facts={}), _verified("proceeded")])
    try:
        final = asyncio.run(WorkflowOrchestrator(tmp_config, store, workflow_id, policy, runner).run())
        assert final["status"] == "completed"
        assert final["steps"][0]["facts"] == {}
        assert "VERIFIED WORKFLOW INPUTS: none yet." in runner.calls[1]["goal"]
        assert runner.calls[1]["seed_facts"] is None
    finally:
        store.close()


def test_workflow_threads_approval_callback_to_each_step(tmp_path, tmp_config):
    steps = [{"ordinal": 1, "target": "http://a.test", "objective": "set mode to compact"}]
    store, workflow_id, policy = _make_store(tmp_path, "run workflow", steps)
    runner = FakeWorkflowRunner([_verified()])

    async def _approve(decision, element) -> bool:
        return True

    try:
        asyncio.run(WorkflowOrchestrator(tmp_config, store, workflow_id, policy, runner, approval_callback=_approve).run())
        assert runner.calls[0]["approval_callback"] is _approve
    finally:
        store.close()


def test_workflow_login_required_blocks_without_burning_all_steps(tmp_path, tmp_config):
    steps = [
        {"ordinal": 1, "target": "http://a.test", "objective": "check assignments"},
        {"ordinal": 2, "target": "http://b.test", "objective": "check assignments"},
    ]
    store, workflow_id, policy = _make_store(tmp_path, "check LMS", steps)
    runner = FakeWorkflowRunner([{"status": "blocked", "blocked_reason": "login_required"}])
    try:
        final = asyncio.run(WorkflowOrchestrator(tmp_config, store, workflow_id, policy, runner).run())
        assert final["status"] == "blocked"
        assert "login_required" in (final["blocked_reason"] or "") or "AUTH_REQUIRED" in (final["blocked_reason"] or "")
        assert len(runner.calls) == 1  # never retried a login wall, never advanced to step 2
    finally:
        store.close()
