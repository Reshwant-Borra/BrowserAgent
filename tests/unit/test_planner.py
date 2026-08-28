"""agent/planner.py: schema-constrained qwen3:8b calls for the general controller's
high-level planning (BrowserAgent_General_Autonomous_Agent_Architecture_REVISED.pdf,
section 7/14). Mirrors tests/unit/test_semantic_planner.py's schema-title-routed fake model
pattern — the model is always faked here; live-model behavior is covered by
benchmarks/general_agent/run_phase2_controller.py.
"""
from __future__ import annotations

import json

import pytest

from agent import planner
from agent.controller_models import CompletionEvaluation, ControllerDecision
from agent.planner import PlannerOutputError
from agent.workspace_models import WorkspaceView
from inference.llama_client import CompletionResult


class _FakeSchemaRoutedClient:
    """Routes canned responses by the requested schema's `title` — same pattern
    tests/unit/test_semantic_planner.py uses for router/semantic_planner.py."""

    def __init__(self, responses: dict[str, dict | list | str]):
        self.responses = responses
        self.endpoint = "fake://planner"
        self.calls: list[str] = []

    async def complete(self, prompt, grammar=None, max_tokens=256, json_schema=None) -> CompletionResult:
        title = (json_schema or {}).get("title", "")
        self.calls.append(title)
        if title not in self.responses:
            raise AssertionError(f"unexpected schema requested: {title!r} (prompt: {prompt[:120]!r})")
        raw = self.responses[title]
        text = raw if isinstance(raw, str) else json.dumps(raw)
        return CompletionResult(text=text, total_latency_ms=1.0)

    async def health_check(self) -> bool:
        return True


def _empty_workspace() -> WorkspaceView:
    return WorkspaceView(task_id="t1")


@pytest.mark.asyncio
async def test_initial_plan_returns_valid_decision():
    client = _FakeSchemaRoutedClient({
        "ControllerDecision": {
            "decision": "start_subgoal", "reason_code": "initial_plan",
            "active_subgoal": "find pricing information",
            "plan": ["find pricing information", "find education discount information"],
            "resource_refs": [], "clarification_question": None, "completion_claim": None,
        },
    })
    decision = await planner.initial_plan(
        client, "find pricing and education discounts", [], _empty_workspace(),
        max_entities=12, max_evidence=8, max_subgoals=5,
    )
    assert isinstance(decision, ControllerDecision)
    assert decision.decision == "start_subgoal"
    assert decision.plan == ["find pricing information", "find education discount information"]
    assert client.calls == ["ControllerDecision"]


@pytest.mark.asyncio
async def test_initial_plan_truncates_oversized_plan_defensively():
    """json_schema-constrained decoding does not enforce a dynamic max list length — a
    non-compliant model response longer than max_subgoals must still be capped, never
    trusted as-is (planner_max_subgoals PASS gate)."""
    client = _FakeSchemaRoutedClient({
        "ControllerDecision": {
            "decision": "start_subgoal", "reason_code": "initial_plan",
            "active_subgoal": "step 1",
            "plan": [f"step {i}" for i in range(1, 9)],  # 8 items, max_subgoals=5
            "resource_refs": [], "clarification_question": None, "completion_claim": None,
        },
    })
    decision = await planner.initial_plan(
        client, "goal", [], _empty_workspace(), max_entities=12, max_evidence=8, max_subgoals=5,
    )
    assert len(decision.plan) == 5
    assert decision.plan == ["step 1", "step 2", "step 3", "step 4", "step 5"]


@pytest.mark.asyncio
async def test_replan_includes_reason_hint_in_prompt():
    captured_prompts: list[str] = []

    class _CapturingClient(_FakeSchemaRoutedClient):
        async def complete(self, prompt, grammar=None, max_tokens=256, json_schema=None):
            captured_prompts.append(prompt)
            return await super().complete(prompt, grammar, max_tokens, json_schema)

    client = _CapturingClient({
        "ControllerDecision": {
            "decision": "revise_plan", "reason_code": "repeated_failure",
            "active_subgoal": "try an alternate approach",
            "plan": ["try an alternate approach"],
            "resource_refs": [], "clarification_question": None, "completion_claim": None,
        },
    })
    decision = await planner.replan(
        client, "goal", [], _empty_workspace(),
        reason_hint="repeated_failure: subgoal X failed 2 consecutive time(s)",
        max_entities=12, max_evidence=8, max_subgoals=5,
    )
    assert decision.reason_code == "repeated_failure"
    assert any("repeated_failure: subgoal X failed 2 consecutive time(s)" in p for p in captured_prompts)


@pytest.mark.asyncio
async def test_evaluate_completion_satisfied_true():
    client = _FakeSchemaRoutedClient({
        "CompletionEvaluation": {
            "satisfied": True, "missing_requirements": [], "unsupported_claims": [],
            "next_recommendation": "finish",
        },
    })
    evaluation = await planner.evaluate_completion(
        client, "goal", ["criterion"], _empty_workspace(), max_entities=12, max_evidence=8,
    )
    assert isinstance(evaluation, CompletionEvaluation)
    assert evaluation.satisfied is True
    assert evaluation.next_recommendation == "finish"


@pytest.mark.asyncio
async def test_evaluate_completion_not_satisfied_lists_missing_requirements():
    client = _FakeSchemaRoutedClient({
        "CompletionEvaluation": {
            "satisfied": False, "missing_requirements": ["pricing not yet found"],
            "unsupported_claims": [], "next_recommendation": "continue",
        },
    })
    evaluation = await planner.evaluate_completion(
        client, "goal", ["criterion"], _empty_workspace(), max_entities=12, max_evidence=8,
    )
    assert evaluation.satisfied is False
    assert evaluation.missing_requirements == ["pricing not yet found"]


@pytest.mark.asyncio
async def test_invalid_json_raises_planner_output_error():
    client = _FakeSchemaRoutedClient({"ControllerDecision": "not json at all"})
    with pytest.raises(PlannerOutputError):
        await planner.initial_plan(
            client, "goal", [], _empty_workspace(), max_entities=12, max_evidence=8, max_subgoals=5,
        )


@pytest.mark.asyncio
async def test_schema_invalid_json_raises_planner_output_error():
    """Valid JSON, but violates the ControllerDecision contract (bad enum value) — must still
    be rejected, never silently coerced."""
    client = _FakeSchemaRoutedClient({
        "ControllerDecision": {"decision": "invent_a_new_thing", "reason_code": "initial_plan"},
    })
    with pytest.raises(PlannerOutputError):
        await planner.initial_plan(
            client, "goal", [], _empty_workspace(), max_entities=12, max_evidence=8, max_subgoals=5,
        )


@pytest.mark.asyncio
async def test_workspace_summary_appears_in_prompt():
    from agent.workspace_models import EvidenceRef, WorkspaceEntity

    workspace = WorkspaceView(
        task_id="t1",
        entities=[WorkspaceEntity(id="ent_1", entity_type="candidate", name="Widget",
                                   attributes={"price_usd": 9.99})],
        evidence=[EvidenceRef(entity_id="ent_1", source_event_id=1, excerpt="seen on page")],
        open_questions=["is it in stock?"],
        completion_requirements=["pick cheapest"],
    )
    captured_prompts: list[str] = []

    class _CapturingClient(_FakeSchemaRoutedClient):
        async def complete(self, prompt, grammar=None, max_tokens=256, json_schema=None):
            captured_prompts.append(prompt)
            return await super().complete(prompt, grammar, max_tokens, json_schema)

    client = _CapturingClient({
        "ControllerDecision": {
            "decision": "start_subgoal", "reason_code": "initial_plan",
            "active_subgoal": "s1", "plan": ["s1"],
            "resource_refs": [], "clarification_question": None, "completion_claim": None,
        },
    })
    await planner.initial_plan(client, "goal", [], workspace, max_entities=12, max_evidence=8, max_subgoals=5)
    prompt = captured_prompts[0]
    assert "Widget" in prompt
    assert "is it in stock?" in prompt
    assert "pick cheapest" in prompt
