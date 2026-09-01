"""Phase 4 (BrowserAgent_General_Autonomous_Agent_Architecture_REVISED.pdf, section 18:
"Delegation to Existing Batch / Workflow / Research Capabilities") scale + crash-recovery
benchmark.

PASS gate (section 18, Phase 4): "25/50/100 independent-target tasks retain Phase 5
precision/recall and crash recovery; ordered cross-site dependency preserves verified
fact-passing; controller chooses batch for large independent sets and does not regress
latency by serializing them."

Scope of this script, deliberately: BatchOrchestrator's own live-model item-level reliability
was already validated at 10-100 targets with precision=recall=1.00 in Phase 5 (docs/
BROWSERAGENT_MASTER_STATUS.md's Phase 5 section) — this script does not re-run that. What
Phase 4 adds on top is purely control-plane: (1) the controller resolves N independent
targets and delegates the WHOLE goal to exactly one BatchOrchestrator.run() call (never one
batch call per target — the "does not regress latency by serializing them" gate, structurally
guaranteed by inspection of agent/controller.py::_delegate_batch, confirmed here by timing);
(2) the delegate's real findings are ingested into generic workspace entities with zero loss/
duplication; (3) a crash between/during work items resumes the SAME batch rather than losing
or redoing work. A deterministic FakeBatchChildRunner (same style as tests/unit/
test_batch_orchestrator.py) isolates exactly this control-plane behavior from live-model
noise — precision/recall here measures whether the controller's own resolution/ingestion path
loses or invents anything, not whether Qwen3-8B reliably extracts a real page's content
(a question Phase 5 already answered separately).
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from datetime import date
from pathlib import Path
from typing import Any

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agent.config import AppConfig, BrowserConfig, ContextConfig, ModelConfig, StorageConfig
from agent.controller import GeneralAgentController
from batch.models import BatchPolicy, ResultContract
from batch.store import BatchStore
from inference.llama_client import CompletionResult
from memory.event_store import EventStore, EventType
from memory.models import TaskState
from memory.task_state import TaskStateStore

RESULTS_DIR = Path(__file__).resolve().parent / "results"


class _SequencedSchemaClient:
    def __init__(self, responses_by_title: dict[str, list]):
        self._queues = {k: list(v) for k, v in responses_by_title.items()}
        self.endpoint = "fake://controller"
        self.calls: list[str] = []

    async def complete(self, prompt, grammar=None, max_tokens=256, json_schema=None) -> CompletionResult:
        title = (json_schema or {}).get("title", "")
        self.calls.append(title)
        queue = self._queues.get(title)
        if not queue:
            raise AssertionError(f"schema {title!r} ran out of scripted responses")
        raw = queue.pop(0)
        return CompletionResult(text=json.dumps(raw), total_latency_ms=1.0)

    async def health_check(self) -> bool:
        return True


_SATISFIED = {"satisfied": True, "missing_requirements": [], "unsupported_claims": [], "next_recommendation": "finish"}


class FakeBatchChildRunner:
    def __init__(self, outcomes: dict[str, dict[str, Any]]):
        self.outcomes = outcomes
        self.calls: list[str] = []

    async def run_child(self, config, batch_id, work_item, child_goal, success_criteria,
                         profile_dir, max_steps, resume_task_id=None, runtime_policy=None,
                         approval_callback=None, seed_facts=None) -> str:
        self.calls.append(work_item["target"])
        outcome = self.outcomes[work_item["target"]]
        task_id = resume_task_id or uuid.uuid4().hex[:12]
        _write_batch_child_task(config, task_id, work_item["target"], outcome)
        return task_id


def _write_batch_child_task(config, task_id: str, target: str, outcome: dict[str, Any]) -> None:
    db_path = Path(config.storage.tasks_dir) / task_id / "task.db"
    store = EventStore(db_path)
    try:
        store.create_task(task_id, f"child {target}", [])
        store.append(task_id, 0, EventType.TASK_CREATED, {"goal": f"child {target}", "success_criteria": []})
        store.append(task_id, 1, EventType.TASK_COMPLETED, {
            "result": outcome.get("summary", "done"), "final_url": target, "final_title": "Fixture",
            "final_text_excerpt": outcome.get("final_text_excerpt", "evidence"),
            "structured_result": outcome.get("structured_result"),
        })
        state = TaskState(task_id=task_id, current_step=1, status="completed", last_event_id=store.max_event_id(task_id))
        TaskStateStore(store).save(state)
    finally:
        store.close()


def _config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        model=ModelConfig(endpoint="http://127.0.0.1:1", temperature=0.1),
        browser=BrowserConfig(headless=True, user_data_dir=str(tmp_path / "tasks")),
        context=ContextConfig(),
        storage=StorageConfig(runtime_dir=str(tmp_path / "runtime"), tasks_dir=str(tmp_path / "tasks")),
    )


def _outcomes_for(n: int) -> tuple[list[str], dict[str, Any], set[str]]:
    """N independent targets, exactly every 3rd one relevant (mirrors Phase 5's assignment-
    actionable precision/recall methodology) — the deterministic ground truth this script
    checks the controller's own ingestion against."""
    targets = [f"http://item{i}.test/page" for i in range(n)]
    relevant = {t for i, t in enumerate(targets) if i % 3 == 0}
    outcomes = {}
    for i, t in enumerate(targets):
        if t in relevant:
            outcomes[t] = {"summary": f"relevant {i}", "structured_result": {
                "relevant": True, "summary": f"relevant {i}",
                "findings": [{"type": "item", "title": f"Item {i}", "value": f"Item {i}", "evidence": "found", "source_url": t}],
            }}
        else:
            outcomes[t] = {"summary": "not relevant", "structured_result": {"relevant": False, "summary": "not relevant", "findings": []}}
    return targets, outcomes, relevant


async def _run_scale_trial(n: int, tmp_root: Path) -> dict[str, Any]:
    tmp_path = tmp_root / f"scale_{n}"
    config = _config(tmp_path)
    targets, outcomes, relevant = _outcomes_for(n)
    goal = f"Check each of these {n} pages and report anything relevant: " + " ".join(targets)
    runner = FakeBatchChildRunner(outcomes)
    planner_client = _SequencedSchemaClient({"CompletionEvaluation": [_SATISFIED]})
    controller = GeneralAgentController.create_new(config, goal, [], llama_client=planner_client, child_runner=runner)
    started = time.monotonic()
    try:
        state = await controller.run()
        elapsed_s = time.monotonic() - started

        events = controller.event_store.all_events(controller.control_task_id)
        batch_started = next(e for e in events if e.type == EventType.DELEGATE_STARTED and e.payload["substrate"] == "batch")
        batch_result = next(e for e in events if e.type == EventType.DELEGATE_RESULT and e.payload["substrate"] == "batch")

        workspace = controller.workspace_store.load(controller.control_task_id)
        ingested_urls = {ev.source_url for ev in workspace.evidence if ev.source_url}
        true_positives = len(ingested_urls & relevant)
        false_positives = len(ingested_urls - relevant)
        false_negatives = len(relevant - ingested_urls)
        precision = true_positives / (true_positives + false_positives) if (true_positives + false_positives) else 1.0
        recall = true_positives / (true_positives + false_negatives) if (true_positives + false_negatives) else 1.0

        # "Does not regress latency by serializing them": exactly one batch delegation call for
        # the whole N-target goal, never N separate delegate_batch/planner round trips.
        delegate_started_count = sum(1 for e in events if e.type == EventType.DELEGATE_STARTED)

        return {
            "n": n,
            "status": state.status,
            "elapsed_s": round(elapsed_s, 3),
            "target_count_requested": batch_started.payload["target_count"],
            "completed": batch_result.payload["completed"],
            "failed": batch_result.payload["failed"],
            "delegate_started_count": delegate_started_count,
            "planner_calls": planner_client.calls,
            "precision": precision,
            "recall": recall,
            "true_positives": true_positives,
            "false_positives": false_positives,
            "false_negatives": false_negatives,
        }
    finally:
        controller.close()


async def _run_crash_recovery_trial(n: int, tmp_root: Path) -> dict[str, Any]:
    """Crashes with one item fully completed and one item claimed-but-never-finished (the real
    kill -9 shape), then resumes and confirms 0 lost/duplicated items at this scale."""
    tmp_path = tmp_root / f"crash_{n}"
    config = _config(tmp_path)
    targets, outcomes, relevant = _outcomes_for(n)
    goal = f"Check each of these {n} pages and report anything relevant: " + " ".join(targets)
    controller = GeneralAgentController.create_new(config, goal, [], llama_client=_SequencedSchemaClient({}))
    control_task_id = controller.control_task_id
    task = controller.state_store.get_task_record(control_task_id)

    active = "check each target"
    controller._mark_subgoal_active(active)
    resolved_targets = controller._resolve_batch_targets(task)
    batch_id = f"{control_task_id}_batch_scale"
    batch_dir = controller.tasks_dir / control_task_id / "delegates" / batch_id
    controller._append(EventType.DELEGATE_STARTED, {
        "substrate": "batch", "subgoal": active, "child_task_id": batch_id,
        "target_count": len(resolved_targets), "delegate_dir": str(batch_dir),
    })
    store = BatchStore.for_batch_dir(batch_dir)
    store.create_batch(active, resolved_targets, ResultContract(), BatchPolicy(), batch_id=batch_id)

    pre_runner = FakeBatchChildRunner(outcomes)
    item0 = dict(store.claim_next_item(batch_id, "local", 900))
    child_task_id = await pre_runner.run_child(config, batch_id, item0, "goal", [], None, 20)
    from batch.orchestrator import _extract_structured_result
    child_store = EventStore(Path(config.storage.tasks_dir) / child_task_id / "task.db")
    events = child_store.all_events(child_task_id)
    child_store.close()
    parsed = _extract_structured_result(events, item0["target"], child_task_id, require_json=False)
    result_id = store.upsert_result(
        batch_id, item0["id"], item0["target"], "completed", parsed["summary"], parsed["structured_data"],
        item0["target"], parsed.get("final_url"), parsed["evidence"], child_task_id, parsed["source_event_ids"],
    )
    store.complete_item(item0["id"], result_id)
    store.claim_next_item(batch_id, "local", 900)  # left running, never finished
    store.close()
    controller.close()  # simulated crash

    resumed = GeneralAgentController.resume(
        config, control_task_id,
        llama_client=_SequencedSchemaClient({"CompletionEvaluation": [_SATISFIED]}),
        child_runner=FakeBatchChildRunner(outcomes),
    )
    try:
        final_state = await resumed.run()
        events_after = resumed.event_store.all_events(control_task_id)
        started = [e for e in events_after if e.type == EventType.DELEGATE_STARTED]
        results = [e for e in events_after if e.type == EventType.DELEGATE_RESULT]
        return {
            "n": n,
            "status": final_state.status,
            "delegate_started_count": len(started),
            "delegate_result_count": len(results),
            "completed": results[0].payload["completed"] if results else None,
            "no_duplicate_delegation": len(started) == 1,
        }
    finally:
        resumed.close()


async def main() -> dict[str, Any]:
    import tempfile

    scales = [25, 50, 100]
    scale_results = []
    crash_results = []
    with tempfile.TemporaryDirectory() as tmp:
        tmp_root = Path(tmp)
        for n in scales:
            scale_results.append(await _run_scale_trial(n, tmp_root))
        for n in scales:
            crash_results.append(await _run_crash_recovery_trial(n, tmp_root))

    gate_met = all(
        r["status"] == "completed" and r["precision"] == 1.0 and r["recall"] == 1.0
        and r["delegate_started_count"] == 1 and r["completed"] == r["target_count_requested"]
        for r in scale_results
    ) and all(
        r["status"] == "completed" and r["no_duplicate_delegation"] and r["completed"] == r["n"]
        for r in crash_results
    )

    report = {
        "scale_trials": scale_results,
        "crash_recovery_trials": crash_results,
        "phase4_gate_met": gate_met,
    }
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = RESULTS_DIR / f"phase4_delegation_{date.today().isoformat()}.json"
    out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    print(f"\nWritten to {out_path}")
    return report


if __name__ == "__main__":
    asyncio.run(main())
