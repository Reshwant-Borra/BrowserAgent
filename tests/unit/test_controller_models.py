"""Schema-only tests for agent/controller_models.py — ControllerDecision and
CompletionEvaluation exactly per BrowserAgent_General_Autonomous_Agent_Architecture_
REVISED.pdf section 7. Model-output-shape validity is exercised end-to-end in
tests/unit/test_planner.py; this file only pins the contract itself."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from agent.controller_models import CompletionEvaluation, ControllerDecision


def test_controller_decision_minimal_valid():
    decision = ControllerDecision(decision="start_subgoal", reason_code="initial_plan")
    assert decision.active_subgoal is None
    assert decision.plan is None
    assert decision.resource_refs == []


def test_controller_decision_rejects_unknown_decision_value():
    with pytest.raises(ValidationError):
        ControllerDecision(decision="delete_database", reason_code="initial_plan")


def test_controller_decision_rejects_unknown_reason_code():
    with pytest.raises(ValidationError):
        ControllerDecision(decision="start_subgoal", reason_code="because_i_felt_like_it")


@pytest.mark.parametrize("decision_value", [
    "start_subgoal", "delegate_batch", "delegate_workflow", "discover_sources",
    "ask_user", "revise_plan", "finish",
])
def test_all_documented_decision_values_accepted(decision_value):
    ControllerDecision(decision=decision_value, reason_code="initial_plan")


@pytest.mark.parametrize("reason_code", [
    "initial_plan", "subgoal_complete", "new_constraint", "repeated_failure",
    "resource_missing", "independent_targets", "ordered_dependency",
    "evidence_insufficient", "completion_satisfied", "user_update",
])
def test_all_documented_reason_codes_accepted(reason_code):
    ControllerDecision(decision="start_subgoal", reason_code=reason_code)


def test_completion_evaluation_minimal_valid():
    evaluation = CompletionEvaluation(satisfied=True, next_recommendation="finish")
    assert evaluation.missing_requirements == []
    assert evaluation.unsupported_claims == []


def test_completion_evaluation_rejects_unknown_recommendation():
    with pytest.raises(ValidationError):
        CompletionEvaluation(satisfied=False, next_recommendation="give_up")


@pytest.mark.parametrize("recommendation", ["finish", "continue", "replan", "ask_user"])
def test_all_documented_recommendations_accepted(recommendation):
    CompletionEvaluation(satisfied=False, next_recommendation=recommendation)


def test_controller_decision_never_carries_a_click_level_field():
    """A ControllerDecision has no notion of element ids/selectors/coordinates — the
    schema itself is the enforcement that the controller cannot express a click-level
    action (BrowserAgent...REVISED.pdf section 5's "create its own actions" boundary)."""
    fields = set(ControllerDecision.model_fields.keys())
    assert fields.isdisjoint({"target", "selector", "coordinates", "element_id"})
