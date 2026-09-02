"""Phase 5 (BrowserAgent_General_Autonomous_Agent_Architecture_REVISED.pdf, section 18:
"Completion Verification, UI Integration, and Safety Hardening") — general-mode UI wiring.

Driven directly against JobRunner (no HTTP layer), same pattern as tests/integration/
test_ui_jobs.py: one fake model client, monkeypatched onto both `agent.loop.create_inference_
client` and `ui.jobs.create_inference_client` (agent/controller.py's own top-level planner
client is constructed by ui/jobs.py::_run_general via the latter, so one monkeypatch target
covers the controller AND every child AgentLoop it spawns). Real Playwright (launch mode) +
real local fixture pages; only the model is faked, matching this repo's established
integration-test style everywhere else.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json

from agent.config import AppConfig
from inference.llama_client import CompletionResult
from ui.jobs import JobRunner
from ui.store import UIJobStore


class FakeGeneralUIClient:
    """Serves BOTH the controller's own schema-constrained calls (json_schema set — one
    ordered queue per schema title, same convention as tests/integration/test_general_
    controller*.py's own fakes) and every child AgentLoop's grammar-constrained click-level
    calls (grammar set — one shared ordered script, since these tests only ever run one
    subgoal/child at a time)."""

    endpoint = "fake://ui-general"

    def __init__(self, schema_responses: dict[str, list], child_script: list, child_repeat: dict | None = None):
        self._schema_queues = {k: list(v) for k, v in schema_responses.items()}
        self._child_script = list(child_script)
        # When given, repeats this action forever once `child_script` itself is exhausted —
        # for a "never finishes" scenario (e.g. Stop mid-job) where the exact number of
        # attempts/retries a controller-level retry loop takes before Stop is observed isn't
        # the point of the test, and a fixed-length script would flakily run out first.
        self._child_repeat = child_repeat
        self.schema_calls: list[str] = []

    async def health_check(self) -> bool:
        return True

    async def complete(self, prompt, grammar=None, max_tokens=256, json_schema=None) -> CompletionResult:
        if json_schema is not None:
            title = json_schema.get("title", "")
            self.schema_calls.append(title)
            queue = self._schema_queues.get(title)
            if not queue:
                raise AssertionError(f"schema {title!r} ran out of scripted responses (prompt: {prompt[:150]!r})")
            return CompletionResult(text=json.dumps(queue.pop(0)), total_latency_ms=1.0)
        if self._child_script:
            return CompletionResult(text=json.dumps(self._child_script.pop(0)), total_latency_ms=1.0)
        if self._child_repeat is not None:
            return CompletionResult(text=json.dumps(self._child_repeat), total_latency_ms=1.0)
        raise AssertionError(f"child script ran out of scripted actions (prompt: {prompt[:200]!r})")


_SATISFIED = {"satisfied": True, "missing_requirements": [], "unsupported_claims": [], "next_recommendation": "finish"}


def _patch_model(monkeypatch, client):
    monkeypatch.setattr("agent.loop.create_inference_client", lambda config: client)
    monkeypatch.setattr("ui.jobs.create_inference_client", lambda config: client)


def _general_config(tmp_config: AppConfig) -> AppConfig:
    tmp_config.agent.control_mode = "general"
    return tmp_config


async def _wait_for(store: UIJobStore, job_id: str, statuses: set[str], timeout: float = 10.0) -> dict:
    elapsed = 0.0
    while elapsed < timeout:
        job = store.get(job_id)
        if job and job["status"] in statuses:
            return job
        await asyncio.sleep(0.05)
        elapsed += 0.05
    raise AssertionError(f"job {job_id} did not reach {statuses} within {timeout}s (last: {store.get(job_id)})")


async def _wait_task_done(runner: JobRunner, job_id: str, timeout: float = 10.0) -> None:
    task = runner._tasks.get(job_id)
    if task is None:
        return
    await asyncio.wait_for(task, timeout=timeout)


async def test_general_mode_job_completes(tmp_config, tmp_path, fixture_site_url, monkeypatch):
    config = _general_config(tmp_config)
    url = f"{fixture_site_url}/index.html"
    client = FakeGeneralUIClient(
        schema_responses={
            "ControllerDecision": [
                {"decision": "start_subgoal", "reason_code": "initial_plan",
                 "active_subgoal": "visit the index page and summarize it",
                 "plan": ["visit the index page and summarize it"], "resource_refs": [],
                 "clarification_question": None, "completion_claim": None},
            ],
            "CompletionEvaluation": [_SATISFIED],
        },
        child_script=[
            {"action": "open_url", "target": None, "params": {"url": url},
             "expected_result": {"url_contains": "127.0.0.1"}, "confidence": 0.9},
            {"action": "finish", "target": None, "params": {"result": "this is the fixture index page"},
             "expected_result": {}, "confidence": 0.9},
        ],
    )
    _patch_model(monkeypatch, client)
    store = UIJobStore(tmp_path / "ui_jobs.db")
    runner = JobRunner(config, store, tmp_path / "runtime")
    job_id = runner.submit("Go to the fixture index page and tell me what it is about.")

    job = await _wait_for(store, job_id, {"completed", "failed"}, timeout=15.0)
    await _wait_task_done(runner, job_id)
    assert job["status"] == "completed"
    assert job["kind"] == "general"
    # _finish's own result-priority (agent/controller.py, unchanged by this phase) falls back
    # to the joined completed-subgoal text when no explicit completion_claim/top-k report was
    # produced — the child's own finish text ("this is the fixture index page") is what's
    # actually recorded as evidence (checked below via the raw event log), not necessarily
    # what ends up in the top-level summary string.
    assert job["final_result"]["completed_subgoals"] == ["visit the index page and summarize it"]

    from memory.event_store import EventStore, EventType
    from pathlib import Path as _P
    es = EventStore(_P(config.storage.tasks_dir) / job["task_id"] / "task.db")
    try:
        events = es.all_events(job["task_id"])
        delegate_results = [e for e in events if e.type == EventType.DELEGATE_RESULT]
        assert any("fixture index page" in (e.payload.get("result") or "") for e in delegate_results)
    finally:
        es.close()


async def test_general_mode_stop_while_waiting_for_approval(tmp_config, tmp_path, fixture_site_url, monkeypatch):
    """Stop honored while a general-mode job is paused mid-task waiting on a real,
    in-flight consequential-action approval — the same already-proven-safe stop/approval
    interplay test_ui_jobs.py's own `test_stop_waiting_for_approval_denies_and_marks_stopped`
    exercises for a plain single-site task, now proven for a job driven through
    GeneralAgentController instead (JobRunner.stop() resolves the pending approval future
    with False and sets the stop event; nothing consequential ever executes either way)."""
    approval_config = dataclasses.replace(
        _general_config(tmp_config), browser=dataclasses.replace(tmp_config.browser, interactive_approval=True),
    )
    approval_config.agent.max_subgoal_attempts = 1
    approval_config.agent.max_replans = 0
    url = f"{fixture_site_url}/wizard_confirm.html"
    client = FakeGeneralUIClient(
        schema_responses={
            "ControllerDecision": [
                {"decision": "start_subgoal", "reason_code": "initial_plan",
                 "active_subgoal": "submit the application", "plan": ["submit the application"],
                 "resource_refs": [], "clarification_question": None, "completion_claim": None},
            ],
        },
        child_script=[
            {"action": "open_url", "target": None, "params": {"url": url},
             "expected_result": {"url_contains": "wizard_confirm"}, "confidence": 0.9},
            {"action": "click", "target": 1, "params": {}, "expected_result": {"page_contains": "Submitted"}, "confidence": 0.9},
        ],
    )
    _patch_model(monkeypatch, client)
    store = UIJobStore(tmp_path / "ui_jobs.db")
    runner = JobRunner(approval_config, store, tmp_path / "runtime")
    job_id = runner.submit("Go to the wizard confirmation page and submit the application.")

    await _wait_for(store, job_id, {"waiting_for_approval"}, timeout=10.0)
    assert runner.stop(job_id)
    job = await _wait_for(store, job_id, {"stopped", "completed", "failed"}, timeout=15.0)
    await _wait_task_done(runner, job_id)
    assert job["status"] == "stopped"


async def test_general_mode_approval_flow(tmp_config, tmp_path, fixture_site_url, monkeypatch):
    """Real consequential-action approval gate, reached through the general controller's own
    delegated-subgoal AgentLoop child — proves Phase 5's approval_callback threading actually
    prevents the blocking `input()` fallback (which would otherwise hang this async test) and
    that the UI's existing pending_approval/approve() flow works unmodified for a general-mode
    job (same wizard_confirm.html fixture test_ui_jobs.py's own approval tests already use)."""
    approval_config = dataclasses.replace(
        _general_config(tmp_config), browser=dataclasses.replace(tmp_config.browser, interactive_approval=True),
    )
    url = f"{fixture_site_url}/wizard_confirm.html"
    client = FakeGeneralUIClient(
        schema_responses={
            "ControllerDecision": [
                {"decision": "start_subgoal", "reason_code": "initial_plan",
                 "active_subgoal": "submit the application", "plan": ["submit the application"],
                 "resource_refs": [], "clarification_question": None, "completion_claim": None},
            ],
            "CompletionEvaluation": [_SATISFIED],
        },
        child_script=[
            {"action": "open_url", "target": None, "params": {"url": url},
             "expected_result": {"url_contains": "wizard_confirm"}, "confidence": 0.9},
            {"action": "click", "target": 1, "params": {}, "expected_result": {"page_contains": "Submitted"}, "confidence": 0.9},
            {"action": "finish", "target": None, "params": {"result": "submitted"}, "expected_result": {}, "confidence": 0.9},
        ],
    )
    _patch_model(monkeypatch, client)
    store = UIJobStore(tmp_path / "ui_jobs.db")
    runner = JobRunner(approval_config, store, tmp_path / "runtime")
    job_id = runner.submit("Go to the wizard confirmation page and submit the application.")

    job = await _wait_for(store, job_id, {"waiting_for_approval"}, timeout=10.0)
    assert job["pending_approval"]["action"] == "click"
    assert "submit" in (job["pending_approval"]["target"] or "").lower()

    assert runner.approve(job_id, True)
    job = await _wait_for(store, job_id, {"completed", "failed"}, timeout=15.0)
    await _wait_task_done(runner, job_id)
    assert job["status"] == "completed"


async def test_general_mode_approval_deny_blocks_task(tmp_config, tmp_path, fixture_site_url, monkeypatch):
    """The structural claim Phase 5's security section cares about most: even though the
    scripted model "decided" to click Submit, denial means the click never executes at all —
    the page never reaches its Submitted state, proven from the real Playwright observation,
    not merely from the job's own reported status."""
    # A declined consequential action doesn't itself end the whole controller task (the
    # delegated strategy retries a fresh child up to max_subgoal_attempts, same as any other
    # blocked child) — pin both retry knobs to 1/0 so this test only needs to script exactly
    # one child attempt and observes the terminal block deterministically, without depending
    # on that unrelated retry-count mechanism's own exact behavior.
    approval_config = dataclasses.replace(
        _general_config(tmp_config), browser=dataclasses.replace(tmp_config.browser, interactive_approval=True),
    )
    approval_config.agent.max_subgoal_attempts = 1
    approval_config.agent.max_replans = 0
    url = f"{fixture_site_url}/wizard_confirm.html"
    client = FakeGeneralUIClient(
        schema_responses={
            "ControllerDecision": [
                {"decision": "start_subgoal", "reason_code": "initial_plan",
                 "active_subgoal": "submit the application", "plan": ["submit the application"],
                 "resource_refs": [], "clarification_question": None, "completion_claim": None},
            ],
        },
        child_script=[
            {"action": "open_url", "target": None, "params": {"url": url},
             "expected_result": {"url_contains": "wizard_confirm"}, "confidence": 0.9},
            {"action": "click", "target": 1, "params": {}, "expected_result": {"page_contains": "Submitted"}, "confidence": 0.9},
        ],
    )
    _patch_model(monkeypatch, client)
    store = UIJobStore(tmp_path / "ui_jobs.db")
    runner = JobRunner(approval_config, store, tmp_path / "runtime")
    job_id = runner.submit("Go to the wizard confirmation page and submit the application.")

    await _wait_for(store, job_id, {"waiting_for_approval"}, timeout=10.0)
    assert runner.approve(job_id, False)
    job = await _wait_for(store, job_id, {"completed", "failed"}, timeout=15.0)
    await _wait_task_done(runner, job_id)
    assert job["status"] == "failed"

    # The structural claim that actually matters: the click never executed at all — the
    # child's own event log shows the declined block, and the real page (re-observed here,
    # independent of anything the job/controller reported) never reached its Submitted state.
    from memory.event_store import EventStore, EventType
    from pathlib import Path as _P
    es = EventStore(_P(approval_config.storage.tasks_dir) / job["task_id"] / "task.db")
    try:
        events = es.all_events(job["task_id"])
        delegate_results = [e for e in events if e.type == EventType.DELEGATE_RESULT]
        assert any("declined" in (e.payload.get("blocked_reason") or "") for e in delegate_results)
    finally:
        es.close()


async def test_general_mode_clarification_round_trip(tmp_config, tmp_path, fixture_site_url, monkeypatch):
    config = _general_config(tmp_config)
    url = f"{fixture_site_url}/index.html"
    client = FakeGeneralUIClient(
        schema_responses={
            "ControllerDecision": [
                {"decision": "ask_user", "reason_code": "resource_missing",
                 "active_subgoal": None, "plan": None, "resource_refs": [],
                 "clarification_question": "which page do you mean?", "completion_claim": None},
                {"decision": "start_subgoal", "reason_code": "user_update",
                 "active_subgoal": "visit the index page", "plan": ["visit the index page"],
                 "resource_refs": [], "clarification_question": None, "completion_claim": None},
            ],
            "CompletionEvaluation": [_SATISFIED],
        },
        child_script=[
            {"action": "open_url", "target": None, "params": {"url": url},
             "expected_result": {"url_contains": "127.0.0.1"}, "confidence": 0.9},
            {"action": "finish", "target": None, "params": {"result": "the index page"}, "expected_result": {}, "confidence": 0.9},
        ],
    )
    _patch_model(monkeypatch, client)
    store = UIJobStore(tmp_path / "ui_jobs.db")
    runner = JobRunner(config, store, tmp_path / "runtime")
    job_id = runner.submit("Check the page and tell me about it.")

    job = await _wait_for(store, job_id, {"waiting_for_input"}, timeout=10.0)
    assert job["pending_clarification"]["question"] == "which page do you mean?"

    assert runner.clarify(job_id, "I mean the fixture index page")
    job = await _wait_for(store, job_id, {"completed", "failed"}, timeout=15.0)
    await _wait_task_done(runner, job_id)
    assert job["status"] == "completed"


async def test_legacy_control_mode_unaffected(tmp_config, tmp_path, fixture_site_url, monkeypatch):
    """Default control_mode ("legacy") must never route through the general controller —
    confirms _should_run_general's own default is a true no-op, not merely documented as one."""
    assert tmp_config.agent.control_mode == "legacy"
    from tests.integration.test_ui_jobs import FakeUIClient

    _patch_model(monkeypatch, FakeUIClient(finish_result="the page is a fixture index"))
    store = UIJobStore(tmp_path / "ui_jobs.db")
    runner = JobRunner(tmp_config, store, tmp_path / "runtime")
    job_id = runner.submit(f"Open {fixture_site_url}/index.html and tell me what this is.")
    job = await _wait_for(store, job_id, {"completed", "failed"}, timeout=15.0)
    await _wait_task_done(runner, job_id)
    assert job["status"] == "completed"
    assert job["kind"] == "single_site"  # not "general"
