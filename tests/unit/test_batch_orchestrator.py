from __future__ import annotations

import json
import asyncio
import uuid
from pathlib import Path
from typing import Any

from batch.models import BatchPolicy, FailureCategory, NavigationScope, ResultContract, WorkItemStatus
from batch.orchestrator import BatchOrchestrator
from batch.store import BatchStore
from memory.event_store import EventStore, EventType
from memory.models import TaskState
from memory.task_state import TaskStateStore


class FakeRunner:
    def __init__(self, outcomes: list[dict[str, Any]]):
        self.outcomes = list(outcomes)
        self.calls: list[dict[str, Any]] = []

    async def run_child(
        self,
        config,
        batch_id: str,
        work_item: dict[str, Any],
        child_goal: str,
        success_criteria: list[str],
        profile_dir: Path | None,
        max_steps: int,
        resume_task_id: str | None = None,
        runtime_policy=None,
    ) -> str:
        self.calls.append({"item": work_item["id"], "goal": child_goal, "resume_task_id": resume_task_id})
        outcome = self.outcomes.pop(0)
        task_id = resume_task_id or uuid.uuid4().hex[:12]
        _write_child_task(config, task_id, work_item["target"], outcome)
        return task_id


class PolicyCapturingRunner(FakeRunner):
    async def run_child(
        self,
        config,
        batch_id: str,
        work_item: dict[str, Any],
        child_goal: str,
        success_criteria: list[str],
        profile_dir: Path | None,
        max_steps: int,
        resume_task_id: str | None = None,
        runtime_policy=None,
    ) -> str:
        self.calls.append({"item": work_item["id"], "runtime_policy": runtime_policy})
        outcome = self.outcomes.pop(0)
        task_id = resume_task_id or uuid.uuid4().hex[:12]
        _write_child_task(config, task_id, work_item["target"], outcome)
        return task_id


def test_orchestrator_completes_and_dedupes_findings(tmp_config, tmp_path):
    store = BatchStore(tmp_path / "batches" / "b1" / "batch.db")
    contract = ResultContract(name="assignment", required_fields=["assignment", "due_date", "evidence"])
    policy = BatchPolicy(max_steps_per_item=5)
    batch_id = store.create_batch("find assignments", ["http://a.test", "http://b.test"], contract, policy, "b1")
    findings = [
        {"title": "Unit 4 Lab", "value": "Due Sep 14", "status": "upcoming", "actionable": True, "evidence": "Unit 4 Lab due Sep 14"},
        {"title": "Unit 4 Lab", "value": "Due Sep 14", "status": "upcoming", "actionable": True, "evidence": "Duplicate posting due Sep 14"},
    ]
    runner = FakeRunner([
        {"status": "completed", "result": {"summary": "one assignment", "findings": [findings[0]]}},
        {"status": "completed", "result": {"summary": "duplicate assignment", "findings": [findings[1]]}},
    ])
    final = asyncio.run(BatchOrchestrator(tmp_config, store, batch_id, policy, contract, runner).run())
    try:
        assert final["completed"] == 2
        assert final["failed"] == 0
        assert final["raw_findings"] == 2
        assert final["deduplicated_findings"] == 1
        assert len(store.results(batch_id)) == 2
        assert all("work_item_id" in p for f in final["findings"] for p in f["provenance"])
    finally:
        store.close()


def test_assignment_dedupe_normalizes_date_variants(tmp_config, tmp_path):
    store = BatchStore(tmp_path / "batches" / "b1" / "batch.db")
    contract = ResultContract(name="assignment", required_fields=["course", "title", "value", "evidence"])
    policy = BatchPolicy(max_steps_per_item=5)
    batch_id = store.create_batch("find assignments", ["http://a.test", "http://b.test"], contract, policy, "b1")
    runner = FakeRunner([
        {"status": "completed", "result": {"summary": "one", "findings": [
            {"type": "assignment", "course": "Biology", "title": "Unit 4 Lab", "value": "September 14", "evidence": "Assignment due September 14"}
        ]}},
        {"status": "completed", "result": {"summary": "two", "findings": [
            {"type": "assignment", "course": "Biology", "title": "Unit 4 Lab", "value": "Due Sep 14", "evidence": "Due Sep 14"}
        ]}},
    ])
    final = asyncio.run(BatchOrchestrator(tmp_config, store, batch_id, policy, contract, runner).run())
    try:
        assert final["raw_findings"] == 2
        assert final["deduplicated_findings"] == 1
    finally:
        store.close()


def test_assignment_result_postprocess_drops_completed_findings(tmp_config, tmp_path):
    store = BatchStore(tmp_path / "batches" / "b1" / "batch.db")
    contract = ResultContract(name="assignment", required_fields=["course", "title", "value", "evidence"])
    policy = BatchPolicy(max_steps_per_item=5)
    batch_id = store.create_batch("find assignments", ["http://a.test"], contract, policy, "b1")
    runner = FakeRunner([
        {"status": "completed", "result": {"summary": "one", "findings": [
            {
                "type": "assignment",
                "course": "Biology",
                "title": "Old Review Packet",
                "value": "Due Sep 14",
                "status": "Completed assignment from last month",
                "evidence": "Completed assignment from last month",
            }
        ]}},
    ])
    final = asyncio.run(BatchOrchestrator(tmp_config, store, batch_id, policy, contract, runner).run())
    try:
        assert final["raw_findings"] == 0
        assert final["deduplicated_findings"] == 0
        stored = json.loads(store.results(batch_id)[0]["structured_data"])
        assert stored["findings"][0]["actionable"] is False
        assert stored["_quality"]["status_conflicts"] == 0
    finally:
        store.close()


def test_assignment_status_conflict_uses_page_evidence(tmp_config, tmp_path):
    store = BatchStore(tmp_path / "batches" / "b1" / "batch.db")
    contract = ResultContract(name="assignment", required_fields=["course", "title", "due_date", "status", "actionable", "evidence"])
    policy = BatchPolicy(max_steps_per_item=5)
    batch_id = store.create_batch("find assignments", ["http://a.test"], contract, policy, "b1")
    runner = FakeRunner([
        {"status": "completed", "result": {"summary": "one", "findings": [
            {
                "type": "assignment",
                "course": "History",
                "title": "Old Review Packet",
                "due_date": "Deadline: September 18, 2026",
                "status": "upcoming",
                "actionable": True,
                "evidence": "Deadline: September 18, 2026",
            }
        ]}, "final_text_excerpt": "History\nOld Review Packet\nDeadline: September 18, 2026\nCompleted assignment from last month"},
    ])
    final = asyncio.run(BatchOrchestrator(tmp_config, store, batch_id, policy, contract, runner).run())
    try:
        stored = json.loads(store.results(batch_id)[0]["structured_data"])
        assert stored["findings"][0]["actionable"] is False
        assert stored["_quality"]["status_conflicts"] == 1
        assert final["deduplicated_findings"] == 0
    finally:
        store.close()


def test_orchestrator_continues_past_item_failure(tmp_config, tmp_path):
    store = BatchStore(tmp_path / "batches" / "b1" / "batch.db")
    contract = ResultContract()
    policy = BatchPolicy(work_item_max_attempts=1, max_steps_per_item=5, continue_on_failure=True)
    batch_id = store.create_batch("research", ["http://ok.test", "http://bad.test", "http://ok2.test"], contract, policy, "b1")
    runner = FakeRunner([
        {"status": "completed", "result": {"summary": "ok", "findings": [{"fact": "A", "evidence": "A"}]}},
        {"status": "blocked", "blocked_reason": "login required"},
        {"status": "completed", "result": {"summary": "ok2", "findings": [{"fact": "B", "evidence": "B"}]}},
    ])
    final = asyncio.run(BatchOrchestrator(tmp_config, store, batch_id, policy, contract, runner).run())
    try:
        assert final["completed"] == 2
        assert final["blocked"] == 1
        assert final["failed"] == 0
        assert final["status"] == "completed_with_failures"
        blocked = [item for item in store.items(batch_id) if item["status"] == WorkItemStatus.BLOCKED.value]
        assert blocked[0]["failure_category"] == FailureCategory.AUTH_REQUIRED.value
    finally:
        store.close()


def test_orchestrator_passes_batch_runtime_policy(tmp_config, tmp_path):
    store = BatchStore(tmp_path / "batches" / "b1" / "batch.db")
    contract = ResultContract()
    policy = BatchPolicy(read_only=True, navigation_scope=NavigationScope.SAME_DOMAIN)
    batch_id = store.create_batch("research", ["https://school.example.edu/a"], contract, policy, "b1")
    runner = PolicyCapturingRunner([
        {"status": "completed", "result": {"summary": "ok", "findings": []}},
    ])
    asyncio.run(BatchOrchestrator(tmp_config, store, batch_id, policy, contract, runner).run())
    try:
        runtime_policy = runner.calls[0]["runtime_policy"]
        assert runtime_policy is not None
        assert runtime_policy.read_only is True
        assert runtime_policy.target_url == "https://school.example.edu/a"
        assert runtime_policy.navigation_scope.value == "same_domain"
    finally:
        store.close()


def test_reconcile_child_completed_before_item_update(tmp_config, tmp_path):
    store = BatchStore(tmp_path / "batches" / "b1" / "batch.db")
    contract = ResultContract()
    policy = BatchPolicy()
    batch_id = store.create_batch("goal", ["http://a.test"], contract, policy, "b1")
    item = store.claim_next_item(batch_id, "worker", policy.lease_seconds)
    task_id = "childdone1"
    _write_child_task(
        tmp_config,
        task_id,
        item["target"],
        {"status": "completed", "result": {"summary": "done", "findings": [{"fact": "A", "evidence": "A"}]}},
    )
    store.set_item_browser_task(item["id"], task_id)
    try:
        BatchOrchestrator(tmp_config, store, batch_id, policy, contract, FakeRunner([])).reconcile_running_items()
        progress = store.progress(batch_id)
        assert progress["completed"] == 1
        assert progress["running"] == 0
        assert len(store.results(batch_id)) == 1
        events = [event["type"] for event in store.events(batch_id)]
        assert "WORK_ITEM_RECONCILED" in events
    finally:
        store.close()


def test_reconcile_after_result_persistence_does_not_duplicate(tmp_config, tmp_path):
    store = BatchStore(tmp_path / "batches" / "b1" / "batch.db")
    contract = ResultContract()
    policy = BatchPolicy()
    batch_id = store.create_batch("goal", ["http://a.test"], contract, policy, "b1")
    item = store.claim_next_item(batch_id, "worker", policy.lease_seconds)
    task_id = "childdone2"
    _write_child_task(
        tmp_config,
        task_id,
        item["target"],
        {"status": "completed", "result": {"summary": "done", "findings": [{"fact": "A", "evidence": "A"}]}},
    )
    store.set_item_browser_task(item["id"], task_id)
    orchestrator = BatchOrchestrator(tmp_config, store, batch_id, policy, contract, FakeRunner([]))
    first_result = orchestrator._persist_child_result(dict(store.get_item(item["id"])), task_id, _events(tmp_config, task_id))
    orchestrator.reconcile_running_items()
    try:
        assert len(store.results(batch_id)) == 1
        assert store.get_item(item["id"])["result_id"] == first_result
        assert store.progress(batch_id)["completed"] == 1
    finally:
        store.close()


def _write_child_task(config, task_id: str, target: str, outcome: dict[str, Any]) -> None:
    db_path = Path(config.storage.tasks_dir) / task_id / "task.db"
    store = EventStore(db_path)
    try:
        store.create_task(task_id, f"child {target}", [])
        store.append(task_id, 0, EventType.TASK_CREATED, {"goal": f"child {target}", "success_criteria": []})
        store.append(task_id, 1, EventType.OBSERVATION, {"url": target, "title": "Fixture", "page_hash": "h", "visible_text": ["Evidence"]})
        if outcome["status"] == "completed":
            store.append(
                task_id,
                2,
                EventType.TASK_COMPLETED,
                {
                    "result": json.dumps(outcome["result"]),
                    "final_url": target,
                    "final_title": "Fixture",
                    "final_text_excerpt": outcome.get("final_text_excerpt", "Evidence text"),
                },
            )
            state = TaskState(task_id=task_id, current_step=2, status="completed", last_event_id=store.max_event_id(task_id))
        elif outcome["status"] == "blocked":
            store.append(task_id, 2, EventType.TASK_BLOCKED, {"reason": outcome.get("blocked_reason", "blocked")})
            state = TaskState(
                task_id=task_id,
                current_step=2,
                status="blocked",
                blocked_reason=outcome.get("blocked_reason", "blocked"),
                last_event_id=store.max_event_id(task_id),
            )
        else:
            state = TaskState(task_id=task_id, current_step=1, status="running", last_event_id=store.max_event_id(task_id))
        TaskStateStore(store).save(state)
    finally:
        store.close()


def _events(config, task_id: str):
    store = EventStore(Path(config.storage.tasks_dir) / task_id / "task.db")
    try:
        return store.all_events(task_id)
    finally:
        store.close()
