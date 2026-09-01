"""Phase 2 (BrowserAgent_General_Autonomous_Agent_Architecture_REVISED.pdf, section 18:
"General Controller in Shadow/Fixture Mode") integration tests. Real Playwright + event
store + WorkspaceStore + child AgentLoop pipeline, deterministic scripted model clients (no
live Ollama) — mirrors tests/integration/test_phase3_crash_recovery.py's approach to proving
crash-safety without a live model, and tests/integration/conftest.py's `loop.llama =
ScriptedLlamaClient(...)` pattern for driving a child AgentLoop's click-level decisions.

Live-model accuracy/overhead-vs-legacy-baseline is covered separately by
benchmarks/general_agent/run_phase2_controller.py (the ">= legacy success, <= 25% model-call
overhead" PASS gate needs a live comparison against the Phase 0 baseline, not a scripted test).
"""
from __future__ import annotations

import json

import pytest

from agent.config import AppConfig
from agent.controller import GeneralAgentController
from inference.llama_client import CompletionResult
from memory.event_store import EventStore, EventType
from tests.integration.fake_llama import ScriptedLlamaClient, decision


class _SequencedSchemaClient:
    """Like tests/unit/test_semantic_planner.py's schema-title-routed fake, but each title
    holds an ordered list consumed one at a time — needed here because the SAME
    ControllerDecision schema is used for both initial planning and every later replan, each
    with a different intended response."""

    def __init__(self, responses_by_title: dict[str, list]):
        self._queues = {k: list(v) for k, v in responses_by_title.items()}
        self.endpoint = "fake://controller"
        self.calls: list[str] = []

    async def complete(self, prompt, grammar=None, max_tokens=256, json_schema=None) -> CompletionResult:
        title = (json_schema or {}).get("title", "")
        self.calls.append(title)
        queue = self._queues.get(title)
        if not queue:
            raise AssertionError(f"schema {title!r} ran out of scripted responses (prompt: {prompt[:150]!r})")
        raw = queue.pop(0)
        return CompletionResult(text=json.dumps(raw), total_latency_ms=1.0)

    async def health_check(self) -> bool:
        return True


def _config(tmp_config: AppConfig, **agent_overrides) -> AppConfig:
    for key, value in agent_overrides.items():
        setattr(tmp_config.agent, key, value)
    return tmp_config


def _never_finishes_child_factory():
    """A child that always waits and never calls finish — burns its whole step budget,
    simulating a subgoal that repeatedly fails to make progress."""
    def factory():
        return ScriptedLlamaClient([decision("wait", params={"ms": 1}) for _ in range(50)])
    return factory


def _open_and_finish_child_factory(urls: list[str]):
    calls = {"n": 0}

    def factory():
        idx = calls["n"]
        calls["n"] += 1
        url = urls[idx]
        return ScriptedLlamaClient([
            decision("open_url", params={"url": url}),
            decision("finish", params={"result": f"visited {url}"}),
        ])
    return factory


async def test_full_run_two_subgoals_completes(tmp_config, fixture_site_url):
    config = _config(tmp_config, completion_check_after_subgoal=True, max_steps_per_subgoal=10)
    planner_client = _SequencedSchemaClient({
        "ControllerDecision": [
            {"decision": "start_subgoal", "reason_code": "initial_plan",
             "active_subgoal": "visit the index page", "plan": ["visit the index page", "visit the products page"],
             "resource_refs": [], "clarification_question": None, "completion_claim": None},
        ],
        "CompletionEvaluation": [
            {"satisfied": False, "missing_requirements": ["products page not visited yet"],
             "unsupported_claims": [], "next_recommendation": "continue"},
            {"satisfied": True, "missing_requirements": [], "unsupported_claims": [],
             "next_recommendation": "finish"},
        ],
    })
    child_factory = _open_and_finish_child_factory([
        f"{fixture_site_url}/index.html", f"{fixture_site_url}/products.html",
    ])
    controller = GeneralAgentController.create_new(
        config, "visit both pages", [], llama_client=planner_client, child_llama_client_factory=child_factory,
    )
    try:
        state = await controller.run()
        assert state.status == "completed"
        assert state.completed_subgoals == ["visit the index page", "visit the products page"]

        workspace = controller.workspace_store.load(controller.control_task_id)
        assert len(workspace.evidence) == 2
        fact_keys = {k for k in workspace.facts}
        assert "subgoal_result::visit the index page" in fact_keys
        assert "subgoal_result::visit the products page" in fact_keys

        events = controller.event_store.all_events(controller.control_task_id)
        delegate_started = [e for e in events if e.type == EventType.DELEGATE_STARTED]
        delegate_result = [e for e in events if e.type == EventType.DELEGATE_RESULT]
        assert len(delegate_started) == 2
        assert len(delegate_result) == 2
        assert any(e.type == EventType.COMPLETION_EVALUATED for e in events)
    finally:
        controller.close()


async def test_repeated_failure_triggers_replan(tmp_config, fixture_site_url):
    config = _config(tmp_config, max_subgoal_attempts=2, max_steps_per_subgoal=1, max_replans=3)
    planner_client = _SequencedSchemaClient({
        "ControllerDecision": [
            {"decision": "start_subgoal", "reason_code": "initial_plan",
             "active_subgoal": "an impossible subgoal", "plan": ["an impossible subgoal"],
             "resource_refs": [], "clarification_question": None, "completion_claim": None},
            {"decision": "revise_plan", "reason_code": "repeated_failure",
             "active_subgoal": "a different, achievable subgoal", "plan": ["a different, achievable subgoal"],
             "resource_refs": [], "clarification_question": None, "completion_claim": None},
        ],
        "CompletionEvaluation": [
            {"satisfied": True, "missing_requirements": [], "unsupported_claims": [],
             "next_recommendation": "finish"},
        ],
    })
    # Every child spawned for "an impossible subgoal" never finishes (step budget 1, always
    # waits); the replanned subgoal's child immediately finishes.
    calls = {"n": 0}

    def child_factory():
        calls["n"] += 1
        if calls["n"] <= 2:
            return ScriptedLlamaClient([decision("wait", params={"ms": 1})])
        # A single-step finish with evidence-backed structured_result — passes
        # _has_evidence_backed_structured_result's gate without needing a prior open_url,
        # since max_steps_per_subgoal=1 leaves no room for a second action.
        return ScriptedLlamaClient([decision("finish", params={
            "result": "done",
            "structured_result": {"relevant": True, "summary": "done",
                                   "findings": [{"value": "x", "evidence": "found x"}]},
        })])

    controller = GeneralAgentController.create_new(
        config, "goal", [], llama_client=planner_client, child_llama_client_factory=child_factory,
    )
    try:
        state = await controller.run()
        assert state.status == "completed"
        events = controller.event_store.all_events(controller.control_task_id)
        subgoal_changes = [e for e in events if e.type == EventType.SUBGOAL_CHANGED]
        # initial plan, the one replan, then advancing past the replanned subgoal to None
        # (plan exhausted) once it completes
        assert len(subgoal_changes) == 3
        assert subgoal_changes[1].payload["subgoal"] == "a different, achievable subgoal"
        assert subgoal_changes[2].payload["subgoal"] is None
        # exactly two failed delegate attempts before the replan fired
        delegate_started = [e for e in events if e.type == EventType.DELEGATE_STARTED]
        assert len([e for e in delegate_started if e.payload["subgoal"] == "an impossible subgoal"]) == 2
    finally:
        controller.close()


async def test_max_replans_exhausted_blocks(tmp_config):
    """With max_replans=0, a repeated failure must block immediately rather than ever calling
    the planner for a replan it has no budget for."""
    config = _config(tmp_config, max_subgoal_attempts=1, max_steps_per_subgoal=1, max_replans=0)
    planner_client = _SequencedSchemaClient({
        "ControllerDecision": [
            {"decision": "start_subgoal", "reason_code": "initial_plan",
             "active_subgoal": "an impossible subgoal", "plan": ["an impossible subgoal"],
             "resource_refs": [], "clarification_question": None, "completion_claim": None},
        ],
    })
    controller = GeneralAgentController.create_new(
        config, "goal", [], llama_client=planner_client,
        child_llama_client_factory=_never_finishes_child_factory(),
    )
    try:
        state = await controller.run()
        assert state.status == "blocked"
        assert "replan budget exhausted" in (state.blocked_reason or "")
        assert planner_client.calls == ["ControllerDecision"]  # only the initial plan call, never a replan
    finally:
        controller.close()


async def test_ask_user_decision_blocks_without_spawning_a_child(tmp_config):
    config = _config(tmp_config)
    planner_client = _SequencedSchemaClient({
        "ControllerDecision": [
            {"decision": "ask_user", "reason_code": "resource_missing",
             "active_subgoal": None, "plan": None, "resource_refs": [],
             "clarification_question": "which product line do you mean?", "completion_claim": None},
        ],
    })

    def child_factory():
        raise AssertionError("no child should ever be spawned for an ask_user decision")

    controller = GeneralAgentController.create_new(
        config, "goal", [], llama_client=planner_client, child_llama_client_factory=child_factory,
    )
    try:
        state = await controller.run()
        assert state.status == "blocked"
        assert state.blocked_reason == "which product line do you mean?"
    finally:
        controller.close()


async def test_delegate_batch_with_no_resolvable_targets_blocks(tmp_config):
    """Phase 4 implements delegate_batch for real; a decision naming it with nothing
    resolvable (no explicit URLs in the goal, nothing discovered yet) must still block with a
    clear reason rather than crash or silently no-op, exactly like any other exhausted replan
    budget (max_replans=0 forces an immediate block with no second scripted response needed)."""
    config = _config(tmp_config, max_replans=0)
    planner_client = _SequencedSchemaClient({
        "ControllerDecision": [
            {"decision": "delegate_batch", "reason_code": "independent_targets",
             "active_subgoal": "process each independent target", "plan": None, "resource_refs": [],
             "clarification_question": None, "completion_claim": None},
        ],
    })
    controller = GeneralAgentController.create_new(
        config, "goal", [], llama_client=planner_client,
        child_llama_client_factory=lambda: (_ for _ in ()).throw(AssertionError("no child expected")),
    )
    try:
        state = await controller.run()
        assert state.status == "blocked"
        assert "delegate_batch" in (state.blocked_reason or "")
        assert "resource_missing" in (state.blocked_reason or "")
    finally:
        controller.close()


async def test_planner_schema_violation_blocks_instead_of_crashing(tmp_config):
    config = _config(tmp_config)

    class _BadClient:
        endpoint = "fake://bad"

        async def complete(self, prompt, grammar=None, max_tokens=256, json_schema=None):
            return CompletionResult(text="not valid json {{{", total_latency_ms=1.0)

        async def health_check(self):
            return True

    controller = GeneralAgentController.create_new(
        config, "goal", [], llama_client=_BadClient(),
        child_llama_client_factory=lambda: (_ for _ in ()).throw(AssertionError("no child expected")),
    )
    try:
        state = await controller.run()
        assert state.status == "blocked"
        assert "schema validation" in (state.blocked_reason or "")
    finally:
        controller.close()


async def test_crash_between_child_completion_and_delegate_result_is_recoverable(tmp_config, fixture_site_url):
    """Simulates a process death after a child AgentLoop fully completed but before this
    controller recorded DELEGATE_RESULT — the exact window _reconcile_dangling_delegate
    exists for. A fresh controller instance (as a real restart would construct) must ingest
    the already-completed child's result rather than re-running or losing it."""
    config = _config(tmp_config, max_steps_per_subgoal=10)
    planner_client = _SequencedSchemaClient({
        "ControllerDecision": [
            {"decision": "start_subgoal", "reason_code": "initial_plan",
             "active_subgoal": "visit the index page", "plan": ["visit the index page"],
             "resource_refs": [], "clarification_question": None, "completion_claim": None},
        ],
        "CompletionEvaluation": [
            {"satisfied": True, "missing_requirements": [], "unsupported_claims": [],
             "next_recommendation": "finish"},
        ],
    })
    url = f"{fixture_site_url}/index.html"
    controller = GeneralAgentController.create_new(
        config, "visit the index page", [], llama_client=planner_client,
        child_llama_client_factory=_open_and_finish_child_factory([url]),
    )
    control_task_id = controller.control_task_id
    task = controller.state_store.get_task_record(control_task_id)
    await controller._remember_hub_url(url)

    # Drive exactly the initial plan + one subgoal run (which fully completes the child), but
    # deliberately stop short of _ingest_subgoal_result / DELEGATE_RESULT — this is the crash.
    state = await controller._initial_plan(task)
    assert state.current_subgoal == "visit the index page"
    child_task_id, child_state = await controller._run_subgoal(task, state.current_subgoal)
    assert child_state.status == "completed"
    events_before = controller.event_store.all_events(control_task_id)
    assert any(e.type == EventType.DELEGATE_STARTED for e in events_before)
    assert not any(e.type == EventType.DELEGATE_RESULT for e in events_before)
    controller.close()  # simulates the process dying here

    # AgentLoop.resume() on an already-completed child returns immediately without ever
    # calling .complete() on its model client (agent/loop.py's run() checks
    # state.status in ("completed", "blocked") before any model call) — this factory's
    # script is intentionally empty so ScriptedLlamaClient itself would raise if it were
    # ever actually called, without needing a separate assertion path here.
    resumed = GeneralAgentController.resume(
        config, control_task_id, llama_client=planner_client,
        child_llama_client_factory=lambda: ScriptedLlamaClient([]),
    )
    try:
        final_state = await resumed.run()
        assert final_state.status == "completed"
        events_after = resumed.event_store.all_events(control_task_id)
        delegate_started = [e for e in events_after if e.type == EventType.DELEGATE_STARTED]
        delegate_results = [e for e in events_after if e.type == EventType.DELEGATE_RESULT]
        # No new delegation was started during reconciliation/resume — the only
        # DELEGATE_STARTED is the original one from before the simulated crash.
        assert len(delegate_started) == 1
        assert len(delegate_results) == 1
        assert delegate_results[0].payload["child_task_id"] == child_task_id
    finally:
        resumed.close()
