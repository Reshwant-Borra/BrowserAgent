"""Structured contract the natural-language task router produces.

Mirrors the validation philosophy of `agent/schemas.py`'s `ModelAction`: the router is
never trusted to hand back free prose. Its output — whether produced deterministically or
by the Qwen fallback (see `router/llm_router.py`) — is always validated against this
Pydantic model before anything downstream (single AgentLoop task / BatchOrchestrator /
WorkflowOrchestrator / research pipeline) acts on it.
"""
from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, field_validator


class TaskType(str, Enum):
    SINGLE_SITE = "single_site"
    MULTISITE_SWEEP = "multisite_sweep"
    ORDERED_WORKFLOW = "ordered_workflow"
    RESEARCH = "research"


class SafetyPolicy(str, Enum):
    READ_ONLY = "read_only"
    REVERSIBLE_ACTIONS = "reversible_actions"


class WorkflowStepPlan(BaseModel):
    """One ordered step for `task_type == ordered_workflow`. Ordinal is explicit and
    persisted (ARCHITECTURE.md/Section 16 of the Phase 5B spec) — the model is never
    trusted to "remember" step order across separate AgentLoop task runs."""

    ordinal: int = Field(ge=1)
    target: str
    objective: str


class RouterDecision(BaseModel):
    task_type: TaskType
    objective: str
    targets: list[str] = Field(default_factory=list)
    requires_discovery: bool = False
    preferred_policy: SafetyPolicy = SafetyPolicy.READ_ONLY
    result_contract: str = "generic"  # "generic" | "assignment" | "research"
    workflow_steps: list[WorkflowStepPlan] = Field(default_factory=list)
    # Set only by the semantic planner's plan->decision translation (router/policy.py) when
    # the original TaskPlan's intent was "mixed" (Section 21 of the semantic planner task:
    # "find X, then deterministically act on it") and task_type is multisite_sweep — signals
    # ui/jobs.py to offer one bounded replan round after the sweep completes, rather than
    # ending the job at the raw findings. Always False for legacy/deterministic decisions.
    mixed_intent_followup: bool = False

    @field_validator("result_contract")
    @classmethod
    def _valid_contract(cls, v: str) -> str:
        if v not in {"generic", "assignment", "research"}:
            return "generic"
        return v
