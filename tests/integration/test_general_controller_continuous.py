"""Phase 2 corrective pass — `GeneralAgentController.run(strategy="continuous")` (see docs/
BROWSERAGENT_MASTER_STATUS.md's Phase 2 corrective-pass section). One AgentLoop task/event-
log/browser session drives every subgoal instead of one child task per subgoal; agent/
loop.py's step() is unmodified except for the additive `finish_intercept` hook. Real
Playwright + event store + WorkspaceStore, deterministic scripted model clients — mirrors
tests/integration/test_general_controller.py's approach for the (unchanged) delegated
strategy, so a diff between the two files is a diff of what's actually new here.

Live-model accuracy/overhead-vs-delegated-strategy comparison is covered separately by
benchmarks/general_agent/run_phase2_controller.py's repeated-trial harness.
"""
from __future__ import annotations

import json

import pytest

from agent.config import AppConfig
from agent.controller import GeneralAgentController
from agent.loop import AgentLoop
from inference.llama_client import CompletionResult
from memory.event_store import EventStore, EventType
from tests.integration.fake_llama import ScriptedLlamaClient, decision


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
            raise AssertionError(f"schema {title!r} ran out of scripted responses (prompt: {prompt[:150]!r})")
        raw = queue.pop(0)
        return CompletionResult(text=json.dumps(raw), total_latency_ms=1.0)

    async def health_check(self) -> bool:
        return True


def _config(tmp_config: AppConfig, **agent_overrides) -> AppConfig:
    for key, value in agent_overrides.items():
        setattr(tmp_config.agent, key, value)
    return tmp_config


def _finding_result(text: str) -> dict:
    return {"result": text, "structured_result": {
        "relevant": True, "summary": text, "findings": [{"value": "x", "evidence": f"evidence for {text}"}],
    }}


async def test_continuous_two_subgoals_share_one_task(tmp_config, fixture_site_url):
    """Two subgoals, ONE continuous AgentLoop: no DELEGATE_STARTED (nothing is delegated), one
    DELEGATE_RESULT per subgoal (substrate="continuous_step"), workspace accumulates both
    subgoals' evidence, and the whole run never leaves the control task's own task_id/event
    log — the literal claim of the continuous design."""
    config = _config(tmp_config, max_steps_per_subgoal=10)
    planner_client = _SequencedSchemaClient({
        "ControllerDecision": [
            {"decision": "start_subgoal", "reason_code": "initial_plan",
             "active_subgoal": "visit the index page", "plan": ["visit the index page", "visit the products page"],
             "resource_refs": [], "clarification_question": None, "completion_claim": None},
        ],
        "CompletionEvaluation": [
            {"satisfied": True, "missing_requirements": [], "unsupported_claims": [],
             "next_recommendation": "finish"},
        ],
    })
    url_a = f"{fixture_site_url}/index.html"
    url_b = f"{fixture_site_url}/products.html"
    child_client = ScriptedLlamaClient([
        decision("open_url", params={"url": url_a}),
        decision("finish", params=_finding_result("visited index")),
        decision("open_url", params={"url": url_b}),
        decision("finish", params=_finding_result("visited products")),
    ])
    controller = GeneralAgentController.create_new(
        config, "visit both pages", [], llama_client=planner_client,
        child_llama_client_factory=lambda: child_client,
    )
    try:
        state = await controller.run(strategy="continuous")
        assert state.status == "completed"
        assert state.completed_subgoals == ["visit the index page", "visit the products page"]

        events = controller.event_store.all_events(controller.control_task_id)
        assert not [e for e in events if e.type == EventType.DELEGATE_STARTED]
        delegate_results = [e for e in events if e.type == EventType.DELEGATE_RESULT]
        assert len(delegate_results) == 2
        assert all(e.payload["substrate"] == "continuous_step" for e in delegate_results)
        assert all(e.payload["child_task_id"] == controller.control_task_id for e in delegate_results)
        # No COMPLETION_EVALUATED for the *intermediate* subgoal (item 8: no planner call
        # after every subgoal) -- only the mandatory final-subgoal boundary check.
        completion_evals = [e for e in events if e.type == EventType.COMPLETION_EVALUATED]
        assert len(completion_evals) == 1
        assert completion_evals[0].payload["satisfied"] is True

        workspace = controller.workspace_store.load(controller.control_task_id)
        assert "subgoal_result::visit the index page" in workspace.facts
        assert "subgoal_result::visit the products page" in workspace.facts
        assert len(workspace.evidence) == 2

        # Only one task.db was ever created for this run (no per-subgoal child task dirs).
        task_dirs = sorted(p.name for p in (controller.tasks_dir).iterdir())
        assert task_dirs == [controller.control_task_id]
    finally:
        controller.close()


async def test_continuous_desynced_subgoal_triggers_controller_replan(tmp_config, fixture_site_url):
    """If agent/loop.py's own low-level internal replan (unrelated to this controller) ever
    renames current_subgoal to something outside the controller's plan, the finish intercept
    must never trust it as "the last subgoal" -- it re-grounds via the controller's own
    bounded replan instead of silently ending the task early."""
    config = _config(tmp_config, max_steps_per_subgoal=10, max_replans=2)
    planner_client = _SequencedSchemaClient({
        "ControllerDecision": [
            {"decision": "start_subgoal", "reason_code": "initial_plan",
             "active_subgoal": "visit the index page", "plan": ["visit the index page"],
             "resource_refs": [], "clarification_question": None, "completion_claim": None},
            {"decision": "revise_plan", "reason_code": "repeated_failure",
             "active_subgoal": "visit the index page", "plan": ["visit the index page"],
             "resource_refs": [], "clarification_question": None, "completion_claim": None},
        ],
        "CompletionEvaluation": [
            {"satisfied": True, "missing_requirements": [], "unsupported_claims": [],
             "next_recommendation": "finish"},
        ],
    })
    url_a = f"{fixture_site_url}/index.html"
    # First finish() call claims a subgoal the controller never planned for -- simulates
    # agent/loop.py's own internal _replan having renamed current_subgoal already.
    child_client = ScriptedLlamaClient([
        decision("open_url", params={"url": url_a}),
    ])

    controller = GeneralAgentController.create_new(
        config, "visit the index page", [], llama_client=planner_client,
        child_llama_client_factory=lambda: child_client,
    )
    try:
        # Drive the intercept directly against a hand-built "desynced" state rather than
        # trying to coax agent/loop.py's own recovery ladder into firing live -- the unit
        # under test is the intercept's guard, not the recovery ladder itself.
        from memory.models import TaskState
        from browser.page_model import PageObservation
        from agent.schemas import ModelDecision, ActionType

        task = controller.state_store.get_task_record(controller.control_task_id)
        state = await controller._initial_plan(task)
        assert state.current_subgoal == "visit the index page"

        desynced_state = TaskState(
            task_id=controller.control_task_id, status="running",
            current_subgoal="a subgoal the controller never planned", plan=state.plan,
            completed_subgoals=[],
        )
        intercept = controller._make_continuous_finish_intercept(task)
        finish_decision = ModelDecision(action=ActionType.FINISH, params={"result": "done"})
        observation = PageObservation(url=url_a, title="t", elements=[], visible_text=[],
                                       state_hash="h", element_count=0, char_count=0, truncated=False)
        result_state = await intercept(finish_decision, desynced_state, observation)

        # The intercept never trusted the desynced subgoal as "the last one" -- it re-grounded
        # via a normal, budgeted controller replan (revise_plan), which only updates the plan
        # and leaves the task running; it does not itself execute or finish anything.
        assert result_state is not None
        assert result_state.status == "running"
        assert result_state.current_subgoal == "visit the index page"
        events = controller.event_store.all_events(controller.control_task_id)
        subgoal_changes = [e for e in events if e.type == EventType.SUBGOAL_CHANGED]
        assert len(subgoal_changes) == 2  # initial plan, then the desync-triggered revise_plan
        assert subgoal_changes[-1].payload["subgoal"] == "visit the index page"
        # Never blindly ended the task on the ambiguous finish.
        assert not [e for e in events if e.type == EventType.TASK_COMPLETED]
    finally:
        controller.close()


async def test_continuous_crash_mid_action_reconciles_on_resume(tmp_config, fixture_site_url):
    """Kill point A/D (task spec): process dies mid-action inside a subgoal. Resuming with the
    same task_id must reconcile the dangling ACTION_INTENT via agent/loop.py's own existing
    _reconcile_pending_intent -- proving crash recovery for the continuous strategy needs no
    new machinery, just AgentLoop's already-proven one, now reachable because the continuous
    strategy shares one real task_id/event log with the browser session."""
    config = _config(tmp_config, max_steps_per_subgoal=10)
    url_a = f"{fixture_site_url}/index.html"

    controller = GeneralAgentController.create_new(
        config, "visit the index page", [], llama_client=ScriptedLlamaClient([]),
        child_llama_client_factory=lambda: ScriptedLlamaClient([]),
    )
    control_task_id = controller.control_task_id
    try:
        controller.event_store.append(control_task_id, 1, EventType.SUBGOAL_CHANGED, {
            "subgoal": "visit the index page", "plan": ["visit the index page"],
        })
        # A real ACTION_INTENT with no matching ACTION_RESULT -- the exact crash window
        # agent/loop.py's own _reconcile_pending_intent exists for (memory/replay.py derives
        # pending_action_intent from this pairing on every load(), it is never a settable
        # snapshot field).
        controller.event_store.append(control_task_id, 2, EventType.ACTION_INTENT, {
            "action": "open_url", "target": None, "params": {"url": url_a},
            "pre_state_hash": "h0", "action_fingerprint": "open_url:" + url_a,
            "expected_result": {}, "risk": "read_only",
        })
    finally:
        controller.close()

    resumed = GeneralAgentController.resume(
        config, control_task_id, llama_client=ScriptedLlamaClient([
            json.dumps({"decision": "start_subgoal", "reason_code": "initial_plan",
                        "active_subgoal": "visit the index page", "plan": ["visit the index page"],
                        "resource_refs": [], "clarification_question": None, "completion_claim": None}),
        ]),
        child_llama_client_factory=lambda: ScriptedLlamaClient([
            decision("finish", params=_finding_result("done")),
        ]),
    )
    try:
        # Directly exercise the reconciliation path a fresh continuous AgentLoop performs on
        # resume, mirroring what _run_continuous's first loop.run() call does internally.
        loop = AgentLoop(
            config, control_task_id, event_store=resumed.event_store, state_store=resumed.state_store,
        )
        await loop.start_browser()
        try:
            state = resumed.state_store.load(control_task_id)
            assert state.pending_action_intent is not None
            state = await loop._reconcile_pending_intent(state)
            assert state.pending_action_intent is None
            events = resumed.event_store.all_events(control_task_id)
            assert any(e.type == EventType.ACTION_RESULT and e.payload.get("resolved_via") == "resume_reconciliation"
                       for e in events)
        finally:
            await loop.aclose()
    finally:
        resumed.close()


async def test_continuous_consequential_action_still_requires_approval(tmp_config, fixture_site_url, monkeypatch):
    """item 12: a subgoal transition must never bypass classify_risk()/approval. The finish
    intercept only ever fires on a `finish` decision -- every other action (including this
    CONSEQUENTIAL "Submit Application" click) must still flow through agent/loop.py's
    completely unmodified requires_approval()/_request_approval() gate, unchanged by the
    continuous strategy. A decline blocks the whole run, exactly as it already does for every
    other AgentLoop caller."""
    config = _config(tmp_config, max_steps_per_subgoal=10)
    config.browser.interactive_approval = True
    monkeypatch.setattr(AgentLoop, "_prompt_for_approval", lambda self, decision, element: False)

    planner_client = _SequencedSchemaClient({
        "ControllerDecision": [
            {"decision": "start_subgoal", "reason_code": "initial_plan",
             "active_subgoal": "submit the application", "plan": ["submit the application"],
             "resource_refs": [], "clarification_question": None, "completion_claim": None},
        ],
    })
    url = f"{fixture_site_url}/wizard_confirm.html"
    child_client = ScriptedLlamaClient([
        decision("open_url", params={"url": url}),
        # Element ids are assigned from the live observation, but "Submit Application" is the
        # only interactive control on this fixture page, so it is always id 1.
        decision("click", target=1),
    ])
    controller = GeneralAgentController.create_new(
        config, "submit the application", [], llama_client=planner_client,
        child_llama_client_factory=lambda: child_client,
    )
    try:
        state = await controller.run(strategy="continuous")
        assert state.status == "blocked"
        assert "declined" in (state.blocked_reason or "")
        events = controller.event_store.all_events(controller.control_task_id)
        assert not [e for e in events if e.type == EventType.TASK_COMPLETED]
        assert not [e for e in events if e.type == EventType.DELEGATE_RESULT]
    finally:
        controller.close()


async def test_continuous_recovery_reset_does_not_erase_completed_subgoals_or_workspace(tmp_config):
    """item 13: a subgoal transition may reset transient recovery counters, but must never
    erase completed_subgoals or accumulated workspace facts."""
    config = _config(tmp_config)
    controller = GeneralAgentController.create_new(config, "goal", [])
    try:
        from agent.workspace_models import WorkspaceFact, WorkspacePatch
        controller.event_store.append(controller.control_task_id, 1, EventType.SUBGOAL_CHANGED, {
            "subgoal": "a", "plan": ["a", "b"],
        })
        controller.event_store.append(controller.control_task_id, 2, EventType.SUBGOAL_CHANGED, {
            "subgoal": "b", "plan": ["a", "b"],
        })
        controller.workspace_store.apply_patch(controller.control_task_id, WorkspacePatch(
            add_facts=[WorkspaceFact(key="subgoal_result::a", value="evidence from a")],
        ))
        state = controller.state_store.load(controller.control_task_id)
        state.recovery_level = "deep_recovery"
        state.retry_count = 3
        state.status = "blocked"
        controller.state_store.save(state)

        result = controller._reset_recovery_for_new_subgoal()
        assert result.status == "running"
        assert result.recovery_level == "normal"
        assert result.retry_count == 0
        assert result.completed_subgoals == ["a"]

        workspace = controller.workspace_store.load(controller.control_task_id)
        assert "subgoal_result::a" in workspace.facts
    finally:
        controller.close()
