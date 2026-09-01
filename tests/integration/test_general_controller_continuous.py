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
import re

import pytest

from agent.config import AppConfig
from agent.controller import GeneralAgentController, _HUB_URL_FACT_KEY
from agent.loop import AgentLoop
from agent.schemas import ActionType, ModelDecision
from agent.workspace_models import EvidenceRef, WorkspaceEntity, WorkspaceFact, WorkspacePatch
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


# ---- Phase 3 corrective pass: candidate-acquisition reliability fixes ----------------------
#
# Live forensic evidence (docs/BROWSERAGENT_MASTER_STATUS.md's Phase 3 corrective pass, 10
# real Qwen3-8B trials across 5 holdout domains) diagnosed the generality-gate failure to three
# deterministic, entity-generic gaps rather than a model-capacity limit: (1) a fresh continuous
# session left the model to discover about:blank on its own instead of already being grounded
# on a known destination, (2) a desynced-subgoal finish paid for a real replanner call that
# cannot see what was just accomplished and, live, just re-proposed the same unrecognized text
# every time, and (3) a subgoal's own plausible-looking structured findings were never checked
# against *which page* they actually came from, letting one candidate's stale page data get
# silently recorded as a different candidate's evidence. These tests reproduce each gap
# directly against the fixed methods, mirroring this file's own existing style of driving the
# intercept/session methods without needing to coax a live model into the exact failure shape.

async def test_continuous_session_start_deterministically_navigates_to_explicit_target(tmp_config, fixture_site_url):
    """A fresh continuous session (the first one, or any controller-level-replan restart) must
    already be grounded on its known destination before the model's first decision — not left
    on about:blank hoping the model's own `open_url` catches it. A finish with no evidence at
    all is still correctly rejected (nothing was actually extracted here), but the resulting
    TaskState's `current_url` proves the browser was navigated before that decision was ever
    requested."""
    config = _config(tmp_config, max_steps_per_subgoal=5)
    url = f"{fixture_site_url}/index.html"
    controller = GeneralAgentController.create_new(config, "goal", [])
    try:
        cid = controller.control_task_id
        task = controller.state_store.get_task_record(cid)
        controller.event_store.append(cid, 1, EventType.SUBGOAL_CHANGED, {
            "subgoal": "goal", "plan": ["goal"], "source": "controller",
        })
        loop = AgentLoop(
            config, cid, explicit_target_url=url,
            event_store=controller.event_store, state_store=controller.state_store,
        )
        loop.llama = ScriptedLlamaClient([decision("finish", params={"result": "nothing found yet"})])
        loop.finish_intercept = controller._make_continuous_finish_intercept(task)

        result = await controller._drive_continuous_session(loop, step_budget=1)

        assert result.current_url == url
        events = controller.event_store.all_events(cid)
        assert any(e.type == EventType.RECOVERY_TRANSITION and e.payload.get("reason") == "premature_finish_rejected"
                   for e in events)
    finally:
        controller.close()


async def test_continuous_desynced_subgoal_recovers_via_name_match_without_replanning(tmp_config, fixture_site_url):
    """The dominant live failure mode for 2 of 5 holdout domains: agent/loop.py's own internal
    replan renames current_subgoal to fresh free text outside the controller's plan. When the
    finish that triggers this still carries real, evidence-backed findings identifying one of
    the controller's own still-pending plan items by name, that item must be credited directly
    — no real (budget-consuming) replanner call, and the evidence must not be discarded."""
    config = _config(tmp_config, max_steps_per_subgoal=10, max_replans=2)
    plan = [
        "visit the directory",
        "visit the Widget A detail page and record its price",
        "visit the Widget B detail page and record its price",
    ]
    planner_client = _SequencedSchemaClient({
        "ControllerDecision": [
            {"decision": "start_subgoal", "reason_code": "initial_plan",
             "active_subgoal": plan[0], "plan": plan,
             "resource_refs": [], "clarification_question": None, "completion_claim": None},
        ],
    })
    controller = GeneralAgentController.create_new(
        config, "visit each widget and record its price", [], llama_client=planner_client,
        child_llama_client_factory=lambda: ScriptedLlamaClient([]),
    )
    try:
        from memory.models import TaskState
        from browser.page_model import PageObservation

        cid = controller.control_task_id
        task = controller.state_store.get_task_record(cid)
        state = await controller._initial_plan(task)
        assert state.current_subgoal == plan[0]
        # Advance past the directory subgoal exactly like a normal continuous finish would,
        # then simulate agent/loop.py's own internal replan renaming current_subgoal.
        controller.event_store.append(cid, 2, EventType.SUBGOAL_CHANGED, {
            "subgoal": plan[1], "plan": plan, "source": "controller",
        })
        desynced_state = TaskState(
            task_id=cid, status="running",
            current_subgoal="go find widget A's page and get its price somehow",
            plan=plan, completed_subgoals=[plan[0]],
        )
        intercept = controller._make_continuous_finish_intercept(task)
        finish_decision = ModelDecision(action=ActionType.FINISH, params={
            "result": "Widget A price recorded",
            "structured_result": {"findings": [
                {"field": "price", "value": "$10.00", "evidence": "Price: $10.00"},
            ]},
        })
        observation = PageObservation(url=f"{fixture_site_url}/alpha_detail.html", title="t", elements=[],
                                       visible_text=[], state_hash="h", element_count=0, char_count=0,
                                       truncated=False)

        result_state = await intercept(finish_decision, desynced_state, observation)

        assert result_state is not None
        assert result_state.current_subgoal == plan[2]  # advanced past the recovered item
        assert planner_client.calls.count("ControllerDecision") == 1  # no replan call spent

        workspace = controller.workspace_store.load(cid)
        names = {e.name for e in workspace.entities}
        assert any("Widget A" in (n or "") for n in names)
    finally:
        controller.close()


async def test_stale_evidence_from_hub_page_is_rejected(tmp_config):
    """The directory/listing (hub) page never carries any one candidate's own attributes in
    this architecture's hub-and-branch shape — a finish whose evidence claims to be sourced
    from the hub itself must never count as valid completion evidence for a per-candidate
    subgoal, regardless of how well-formed the structured_result looks."""
    config = _config(tmp_config)
    controller = GeneralAgentController.create_new(config, "goal", [])
    try:
        cid = controller.control_task_id
        hub_url = "http://127.0.0.1:1/widgets/index.html"
        controller.workspace_store.apply_patch(cid, WorkspacePatch(
            add_facts=[WorkspaceFact(key=_HUB_URL_FACT_KEY, value=hub_url)],
        ))
        finish_decision = ModelDecision(action=ActionType.FINISH, params={
            "result": "Widget A recorded",
            "structured_result": {"findings": [
                {"field": "price", "value": "$10.00", "evidence": "Price: $10.00"},
            ]},
        })
        assert controller._continuous_subgoal_has_evidence(
            "visit the Widget A detail page and record its price", finish_decision, hub_url,
        ) is False
    finally:
        controller.close()


async def test_stale_evidence_from_sibling_page_is_rejected_but_same_candidate_reuse_allowed(tmp_config):
    """Live-reproduced silent-corruption bug (a *passing* trial still copied one candidate's
    exact attributes onto a different candidate's entity because the model finished without
    navigating away): a finish claiming a *different*-named candidate's evidence from a page
    already recorded as some other entity's own source must be rejected, while a legitimate
    re-confirmation of the SAME candidate from its own already-used page must still be
    accepted."""
    config = _config(tmp_config)
    controller = GeneralAgentController.create_new(config, "goal", [])
    try:
        cid = controller.control_task_id
        detail_url = "http://127.0.0.1:1/widgets/widget_a.html"
        controller.workspace_store.apply_patch(cid, WorkspacePatch(
            add_entities=[WorkspaceEntity(id="ent_a", entity_type="candidate", name="Widget A",
                                           attributes={"price": "$10.00"})],
            add_evidence=[EvidenceRef(entity_id="ent_a", field_key="price", source_event_id=1,
                                       source_url=detail_url, excerpt="Price: $10.00")],
        ))

        conflicting = ModelDecision(action=ActionType.FINISH, params={
            "result": "Widget B recorded",
            "structured_result": {"findings": [
                {"field": "price", "value": "$10.00", "evidence": "Price: $10.00"},
            ]},
        })
        assert controller._continuous_subgoal_has_evidence(
            "visit the Widget B detail page and record its price", conflicting, detail_url,
        ) is False

        reconfirm = ModelDecision(action=ActionType.FINISH, params={
            "result": "Widget A recorded again",
            "structured_result": {"findings": [
                {"field": "price", "value": "$10.00", "evidence": "Price: $10.00"},
            ]},
        })
        assert controller._continuous_subgoal_has_evidence(
            "visit the Widget A detail page and record its price", reconfirm, detail_url,
        ) is True
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


# ---- subgoal-scoped local-attempt limit (Phase 2 corrective pass #2) ------------------------
#
# Reproduces the diagnosed root cause of the 0/5 multi_step_registration failure: agent/
# loop.py's own internal low-level replan (build_replan_prompt, fired when the action-level
# recovery ladder reaches REPLAN_REQUIRED) has no bound of its own and produces a freshly
# model-worded subgoal string every time it fires — a text-matched attempt counter (an earlier
# version of this fix) could never see two consecutive firings as "the same subgoal being
# retried," since attempt #2 always lands on a different string than attempt #1. Continuous
# mode's much larger multi-subgoal step budget gave that pre-existing, task-agnostic mechanism
# far more room to cycle unboundedly than any single-subgoal task ever exercised it before.

def _find_target(prompt: str, name: str) -> int:
    match = re.search(rf'\[(\d+)\] \w+ "{re.escape(name)}"', prompt)
    assert match, f"element {name!r} not found in prompt: {prompt[-500:]!r}"
    return int(match.group(1))


async def test_subgoal_local_attempts_counts_by_controller_tag_not_subgoal_text(tmp_config):
    """Direct unit-level proof that the counter survives agent/loop.py's own internal replan
    renaming current_subgoal to a different string every time it fires — the exact defect an
    earlier, text-matched version of this fix had. Two "replanned" transitions interleaved
    with two *different-text* untagged (loop-internal) SUBGOAL_CHANGED events, after one
    controller-tagged SUBGOAL_CHANGED, must still count as 2, not 0 or 1."""
    config = _config(tmp_config)
    controller = GeneralAgentController.create_new(config, "goal", [])
    try:
        cid = controller.control_task_id
        controller.event_store.append(cid, 1, EventType.SUBGOAL_CHANGED, {
            "subgoal": "read the invite code", "plan": ["read the invite code"], "source": "controller",
        })
        assert controller._subgoal_local_attempts() == 0

        # agent/loop.py::_replan()'s own atomic pair: a freshly-worded, untagged SUBGOAL_CHANGED
        # followed by a "replanned" RECOVERY_TRANSITION — fired twice, with different text both
        # times, exactly like a real qwen3:8b replan call would produce.
        controller.event_store.append(cid, 2, EventType.SUBGOAL_CHANGED, {
            "subgoal": "retry reading the invite code (attempt 1)", "plan": ["retry reading the invite code (attempt 1)"],
        })
        controller.event_store.append(cid, 2, EventType.RECOVERY_TRANSITION, {
            "from": "replan_required", "to": "normal", "reason": "replanned",
        })
        assert controller._subgoal_local_attempts() == 1

        controller.event_store.append(cid, 3, EventType.SUBGOAL_CHANGED, {
            "subgoal": "let's try the invite code page again (attempt 2)",
            "plan": ["let's try the invite code page again (attempt 2)"],
        })
        controller.event_store.append(cid, 3, EventType.RECOVERY_TRANSITION, {
            "from": "replan_required", "to": "normal", "reason": "replanned",
        })
        assert controller._subgoal_local_attempts() == 2

        # A genuine controller-level replan (tagged) resets the count back to 0, even though
        # completed_subgoals/workspace facts/history remain untouched (item 5/13).
        controller.event_store.append(cid, 4, EventType.SUBGOAL_CHANGED, {
            "subgoal": "a genuinely different subgoal", "plan": ["a genuinely different subgoal"],
            "source": "controller",
        })
        assert controller._subgoal_local_attempts() == 0
    finally:
        controller.close()


async def test_continuous_blocks_when_local_attempts_already_exhausted_at_session_start(tmp_config, fixture_site_url):
    """End-to-end proof that _drive_continuous_session enforces the limit the moment it
    checks, using pre-seeded events to simulate "agent/loop.py's own replan already rescued
    this subgoal max_subgoal_attempts times" without needing to actually drive a real failing
    browser interaction for this particular assertion (that is covered by the live-ladder test
    below). With max_replans=0, the resulting block must name the exhausted-local-attempts
    reason and never silently keep running."""
    config = _config(tmp_config, max_subgoal_attempts=2, max_steps_per_subgoal=5, max_replans=0)
    planner_client = _SequencedSchemaClient({
        "ControllerDecision": [
            {"decision": "start_subgoal", "reason_code": "initial_plan",
             "active_subgoal": "an impossible subgoal", "plan": ["an impossible subgoal"],
             "resource_refs": [], "clarification_question": None, "completion_claim": None},
        ],
    })
    url = f"{fixture_site_url}/noop.html"
    controller = GeneralAgentController.create_new(
        config, "an impossible subgoal", [], llama_client=planner_client,
        child_llama_client_factory=lambda: ScriptedLlamaClient([decision("wait", params={"ms": 1}) for _ in range(10)]),
    )
    try:
        cid = controller.control_task_id
        task = controller.state_store.get_task_record(cid)
        state = await controller._initial_plan(task)
        assert state.current_subgoal == "an impossible subgoal"

        # Simulate two prior loop-internal replan firings against this exact controller-set
        # subgoal (different text each time — see the unit test above for why that matters).
        controller.event_store.append(cid, 2, EventType.SUBGOAL_CHANGED, {
            "subgoal": "retry (1)", "plan": ["retry (1)"],
        })
        controller.event_store.append(cid, 2, EventType.RECOVERY_TRANSITION, {
            "from": "replan_required", "to": "normal", "reason": "replanned",
        })
        controller.event_store.append(cid, 3, EventType.SUBGOAL_CHANGED, {
            "subgoal": "retry (2)", "plan": ["retry (2)"],
        })
        controller.event_store.append(cid, 3, EventType.RECOVERY_TRANSITION, {
            "from": "replan_required", "to": "normal", "reason": "replanned",
        })
        assert controller._subgoal_local_attempts() == 2

        loop = AgentLoop(
            config, cid, explicit_target_url=url, event_store=controller.event_store,
            state_store=controller.state_store,
        )
        loop.llama = ScriptedLlamaClient([decision("open_url", params={"url": url})])
        loop.finish_intercept = controller._make_continuous_finish_intercept(task)
        result = await controller._drive_continuous_session(loop, step_budget=5)

        assert result.status == "blocked"
        assert "subgoal local attempts exhausted" in (result.blocked_reason or "")
    finally:
        controller.close()


async def test_continuous_reproduces_and_bounds_repeated_internal_replans(tmp_config, fixture_site_url):
    """Live reproduction (no synthetic events) of the exact multi_step_registration failure
    mode: a subgoal whose actions always fail verification drives the REAL recovery ladder to
    REPLAN_REQUIRED, agent/loop.py's own _replan() fires with a freshly-worded subgoal, the
    same failure repeats, _replan() fires again — and this controller must now block once
    max_subgoal_attempts local rescues have happened, rather than burning the rest of its step
    budget cycling forever (the observed pre-fix behavior: `status: "running"`, never blocked
    or completed, with completed_subgoals full of near-duplicate rephrasings)."""
    config = _config(tmp_config, max_subgoal_attempts=2, max_steps_per_subgoal=60, max_replans=0)
    planner_client = _SequencedSchemaClient({
        "ControllerDecision": [
            {"decision": "start_subgoal", "reason_code": "initial_plan",
             "active_subgoal": "click the do-nothing button until it works", "plan": ["click the do-nothing button until it works"],
             "resource_refs": [], "clarification_question": None, "completion_claim": None},
        ],
    })
    url = f"{fixture_site_url}/noop.html"

    replan_calls = {"n": 0}

    def script_entry(prompt: str) -> str:
        if "updating the plan for a browser task that is stuck" in prompt:
            replan_calls["n"] += 1
            worded = f"try a different way to trigger the do-nothing button (attempt {replan_calls['n']})"
            return json.dumps({"subgoal": worded, "plan": [worded]})
        if "Do Nothing" not in prompt:
            return decision("open_url", params={"url": url})
        target = _find_target(prompt, "Do Nothing")
        # An expected_result that can never be satisfied forces a genuine, deterministic
        # verification failure every single time, regardless of the button's real (no-op)
        # effect — driving the real recovery ladder up to REPLAN_REQUIRED repeatedly.
        return decision("click", target=target, expected_result={"url_contains": "this-can-never-match-xyz"})

    child_client = ScriptedLlamaClient([script_entry for _ in range(300)])
    controller = GeneralAgentController.create_new(
        config, "click the do-nothing button until it works", [], llama_client=planner_client,
        child_llama_client_factory=lambda: child_client,
    )
    try:
        state = await controller.run(explicit_target_url=url, strategy="continuous")

        assert state.status == "blocked"
        assert "subgoal local attempts exhausted" in (state.blocked_reason or "")
        assert replan_calls["n"] >= config.agent.max_subgoal_attempts

        events = controller.event_store.all_events(controller.control_task_id)
        replanned = [e for e in events if e.type == EventType.RECOVERY_TRANSITION and e.payload.get("reason") == "replanned"]
        assert len(replanned) >= config.agent.max_subgoal_attempts
    finally:
        controller.close()
