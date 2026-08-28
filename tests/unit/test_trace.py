"""`browser-agent trace` reads existing persisted data only (jobs.db -> batch.db/task.db) and
must correctly follow a UI job down to its underlying AgentLoop task(s) and reconstruct a
step-by-step timeline from that task's EventStore — this is the forensic tool used to diagnose
the real Amazon "find the 3 best vacuum cleaners" run (see docs/BROWSERAGENT_MASTER_STATUS.md)
without hand-querying five different sqlite files."""
from __future__ import annotations

from pathlib import Path

from agent.config import AppConfig, LoggingConfig, StorageConfig
from batch.models import BatchPolicy, ResultContract
from batch.store import BatchStore
from cli import trace as trace_mod
from memory.event_store import EventStore, EventType
from ui.store import UIJobStore


def _config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        storage=StorageConfig(runtime_dir=str(tmp_path / "runtime"), tasks_dir=str(tmp_path / "runtime" / "tasks")),
        logging=LoggingConfig(dir=str(tmp_path / "runtime" / "logs")),
    )


def _write_task_events(config: AppConfig, task_id: str) -> None:
    db_path = Path(config.storage.tasks_dir) / task_id / "task.db"
    es = EventStore(db_path)
    try:
        es.create_task(task_id, "Open the target page", [])
        es.append(task_id, 0, EventType.TASK_CREATED, {"goal": "Open the target page", "success_criteria": []})
        es.append(task_id, 1, EventType.OBSERVATION, {
            "url": "https://example.test/listing", "title": "Listing", "page_hash": "h1", "phase": "pre_decision",
            "element_names": [], "visible_text": [],
        })
        es.append(task_id, 1, EventType.MODEL_DECISION, {
            "decision": {"action": "click", "target": 1, "params": {}, "confidence": 1.0},
        })
        es.append(task_id, 1, EventType.ACTION_INTENT, {"action": "click", "target": 1})
        es.append(task_id, 1, EventType.ACTION_RESULT, {"error": "Locator.click: Timeout 10000ms exceeded."})
        es.append(
            task_id, 1, EventType.VERIFICATION_RESULT, {"action": "click", "target": 1},
            verification_result={"passed": False, "checks": [{"type": "execution_error", "passed": False}]},
        )
        es.append(task_id, 1, EventType.RECOVERY_TRANSITION, {"from": "normal", "to": "refresh_state", "reason": "loop_detected"})
    finally:
        es.close()


def test_resolve_child_tasks_follows_batch_work_items(tmp_path):
    config = _config(tmp_path)
    job_store = UIJobStore(Path(config.storage.runtime_dir) / "ui" / "jobs.db")
    job_id = job_store.create("find the 3 best vacuum cleaners")

    batch_dir = Path(config.storage.runtime_dir) / "batches" / job_id
    batch_store = BatchStore(batch_dir / "batch.db")
    contract = ResultContract(name="research")
    policy = BatchPolicy()
    batch_store.create_batch(
        "find the 3 best vacuum cleaners",
        ["https://example.test/listing", "https://example.test/other"],
        contract, policy, batch_id=job_id,
    )
    items = batch_store.items(job_id)
    batch_store.set_item_browser_task(items[0]["id"], "childtask01")
    job_store.update(job_id, batch_id=job_id, status="completed")

    _write_task_events(config, "childtask01")

    job = trace_mod.find_job(config, job_id)
    assert job is not None
    refs = trace_mod.resolve_child_tasks(config, job)
    assert len(refs) == 1
    assert refs[0].task_id == "childtask01"
    assert "https://example.test/listing" in refs[0].label

    task_trace = trace_mod.build_task_trace(config, refs[0].task_id, refs[0].label)
    assert task_trace is not None
    assert len(task_trace.steps) == 1
    step = task_trace.steps[0]
    assert step.action == "click"
    assert step.verified is False
    assert step.recovery_transitions[0]["reason"] == "loop_detected"

    rendered = trace_mod.render_task_trace(task_trace, verbose=False)
    assert "loop_detected" in rendered
    assert "FAIL" in rendered

    job_store.close()
    batch_store.close()


def test_find_recent_job_returns_newest_by_created_at(tmp_path):
    config = _config(tmp_path)
    job_store = UIJobStore(Path(config.storage.runtime_dir) / "ui" / "jobs.db")
    job_store.create("older job")
    newest_id = job_store.create("newest job")
    job_store.close()

    job = trace_mod.find_recent_job(config)
    assert job is not None
    assert job["id"] == newest_id
    assert job["prompt"] == "newest job"


def test_build_task_trace_returns_none_for_missing_task(tmp_path):
    config = _config(tmp_path)
    assert trace_mod.build_task_trace(config, "does-not-exist", "task") is None


def test_general_controller_events_render_concisely(tmp_path):
    """Phase 1/2 (agent/controller.py, memory/workspace_store.py) event types must show up in
    the default (non-verbose) trace render — section 20 of BrowserAgent_General_Autonomous_
    Agent_Architecture_REVISED.pdf: plan/subgoal transitions, workspace mutations (entity
    IDs/field names only, never values), delegate start/result child IDs, completion
    evaluations."""
    config = _config(tmp_path)
    task_id = "ctrl123"
    db_path = Path(config.storage.tasks_dir) / task_id / "task.db"
    es = EventStore(db_path)
    try:
        es.create_task(task_id, "goal", [])
        es.append(task_id, 0, EventType.TASK_CREATED, {"goal": "goal", "success_criteria": []})
        es.append(task_id, 1, EventType.SUBGOAL_CHANGED, {"subgoal": "find pricing", "plan": ["find pricing"]})
        es.append(task_id, 2, EventType.DELEGATE_STARTED, {
            "substrate": "agent_loop", "subgoal": "find pricing", "child_task_id": "child001",
        })
        es.append(task_id, 3, EventType.DELEGATE_RESULT, {
            "child_task_id": "child001", "status": "completed", "result": "SECRET_PRICE_VALUE_42",
        })
        es.append(task_id, 4, EventType.WORKSPACE_MUTATED, {
            "add_entities": [{"id": "ent_1", "entity_type": "candidate",
                               "attributes": {"price_usd": "SECRET_ATTR_VALUE"}}],
            "add_facts": [{"key": "budget", "value": "SECRET_FACT_VALUE"}],
            "add_evidence": [{"entity_id": "ent_1", "excerpt": "SECRET_EXCERPT_TEXT", "source_event_id": 3}],
            "update_entities": [], "open_questions_add": [], "open_questions_resolve": [],
        })
        es.append(task_id, 5, EventType.COMPLETION_EVALUATED, {
            "satisfied": False, "missing_requirements": ["pricing not confirmed"],
            "unsupported_claims": [], "next_recommendation": "continue",
        })
    finally:
        es.close()

    trace = trace_mod.build_task_trace(config, task_id, "task")
    assert trace is not None
    rendered = trace_mod.render_task_trace(trace, verbose=False)

    assert "find pricing" in rendered
    assert "agent_loop" in rendered and "child001" in rendered
    assert "status=completed" in rendered
    assert "+entities ['ent_1']" in rendered
    assert "+facts ['budget']" in rendered
    assert "satisfied=False" in rendered and "pricing not confirmed" in rendered
    # The doc's explicit instruction: entity IDs/field names only, never the values.
    assert "SECRET_PRICE_VALUE_42" not in rendered
    assert "SECRET_ATTR_VALUE" not in rendered
    assert "SECRET_FACT_VALUE" not in rendered
    assert "SECRET_EXCERPT_TEXT" not in rendered
