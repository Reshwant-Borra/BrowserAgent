"""Pydantic contracts for the general controller's own decisions (BrowserAgent_General_
Autonomous_Agent_Architecture_REVISED.pdf, section 7). These are strictly high-level: a
ControllerDecision never names a browser element, selector, or click-level action — that
remains agent/schemas.py's ModelAction, decided by AgentLoop's own model call, never by the
controller. agent/planner.py is the only place a ControllerDecision or CompletionEvaluation
is ever produced (a schema-constrained qwen3:8b call); agent/controller.py is the only place
either is ever consumed.
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

ControllerDecisionType = Literal[
    "start_subgoal",
    "delegate_batch",
    "delegate_workflow",
    "discover_sources",
    "ask_user",
    "revise_plan",
    "finish",
]

ControllerReasonCode = Literal[
    "initial_plan",
    "subgoal_complete",
    "new_constraint",
    "repeated_failure",
    "resource_missing",
    "independent_targets",
    "ordered_dependency",
    "evidence_insufficient",
    "completion_satisfied",
    "user_update",
]


class ControllerDecision(BaseModel):
    decision: ControllerDecisionType
    reason_code: ControllerReasonCode
    active_subgoal: Optional[str] = None
    plan: Optional[list[str]] = None
    resource_refs: list[int] = Field(default_factory=list)
    clarification_question: Optional[str] = None
    completion_claim: Optional[str] = None


class CompletionEvaluation(BaseModel):
    satisfied: bool
    missing_requirements: list[str] = Field(default_factory=list)
    unsupported_claims: list[str] = Field(default_factory=list)
    next_recommendation: Literal["finish", "continue", "replan", "ask_user"]
