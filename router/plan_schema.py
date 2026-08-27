"""Structured semantic task plan (see docs/SEMANTIC_PLANNER.md): the schema the semantic
planner's Qwen call (router/semantic_planner.py) is constrained to. Mirrors router/schema.py's
RouterDecision philosophy — the planner's output is a strict data contract, never trusted
prose, and is always validated before router/resources.py resolves it into anything
executable. The planner proposes *what the user means* and *what resources are needed*;
deterministic code decides what those resources actually resolve to (router/resources.py)
and what BrowserAgent execution shape ultimately runs (router/policy.py's plan translator).
"""
from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, field_validator


class PlanIntent(str, Enum):
    READ = "read"
    ACT = "act"
    RESEARCH = "research"
    MIXED = "mixed"


class ExecutionShape(str, Enum):
    """Maps 1:1 onto router/schema.py's existing TaskType — the planner never invents an
    execution shape the rest of the system doesn't already know how to run."""

    SINGLE = "single"
    SWEEP = "sweep"
    ORDERED_WORKFLOW = "ordered_workflow"
    OPEN_RESEARCH = "open_research"


class ResourceKind(str, Enum):
    EXPLICIT_URLS = "explicit_urls"    # URLs already written in the user's prompt text
    CURRENT_PAGE = "current_page"      # whatever page is already open/attached; no URL needed
    OPEN_TABS = "open_tabs"            # semantic filter over the browser's currently open tabs
    WEB_DISCOVERY = "web_discovery"    # open-web search + candidate-id selection (research/discovery.py)


class ResourceRequirement(BaseModel):
    """One thing the plan needs a concrete target (or targets) for. The planner never fills
    in a URL here — `description` is a semantic hint (e.g. "the user's course pages") that
    router/resources.py's ResourceResolver uses to choose a resolution strategy, and, for
    `open_tabs`, as the filtering objective handed to a *separate* schema-constrained
    id-selection call. Canonical URLs are always resolver-owned, never planner-owned."""

    kind: ResourceKind
    description: str = ""


class PlannedStep(BaseModel):
    """One step of an `ordered_workflow` plan. `resource_ref` indexes into the plan's
    `resource_requirements` list — the resolver must resolve that requirement to exactly one
    concrete URL for this step; a requirement that resolves to zero or multiple URLs for a
    workflow step is unresolved (triggers clarification), never guessed."""

    ordinal: int = Field(ge=1)
    resource_ref: int = Field(ge=0)
    objective: str


class PlanConstraints(BaseModel):
    """Only ever a *default*, same caveat as router/schema.py's SafetyPolicy — real
    consequential-action gating is agent/schemas.py::classify_risk() + the approval flow,
    completely independent of anything the planner says (Section 31 of the planner task)."""

    read_only: bool = True
    navigation_scope: str = "same_origin"

    @field_validator("navigation_scope")
    @classmethod
    def _valid_scope(cls, v: str) -> str:
        if v not in {"same_origin", "same_domain", "unrestricted"}:
            return "same_origin"
        return v


class TaskPlan(BaseModel):
    """The semantic planner's entire output for one prompt. Deterministic code is the only
    thing that ever turns this into something that executes — the plan itself has no
    execution capability."""

    goal: str
    intent: PlanIntent
    execution_shape: ExecutionShape
    resource_requirements: list[ResourceRequirement] = Field(default_factory=list)
    steps: list[PlannedStep] = Field(default_factory=list)
    constraints: PlanConstraints = Field(default_factory=PlanConstraints)
    result_contract: str = "generic"

    @field_validator("result_contract")
    @classmethod
    def _valid_contract(cls, v: str) -> str:
        if v not in {"generic", "assignment", "research"}:
            return "generic"
        return v


class ResourceSelection(BaseModel):
    """Structured output for the `open_tabs` resolution sub-call (router/resources.py): the
    model selects candidate tab ids, it never rewrites or retypes a URL (open-tab resolution
    Section 9 of the planner task) — same anti-hallucination shape as
    research/discovery.py::LinkSelection."""

    selected_ids: list[int] = Field(default_factory=list)


class ReplanDecisionKind(str, Enum):
    CONTINUE = "continue"
    REVISE = "revise"
    CLARIFY = "clarify"
    FINISH = "finish"


class ReplanDecision(BaseModel):
    """Bounded, single-shot replanning (Section 12-13 of the planner task): called at most
    once, at a clear plan boundary (a sweep/research-shaped plan just produced structured
    findings and the original intent was MIXED) — never a continuous free-form planning
    loop. `finding_ref` indexes into the findings list handed to the replanning prompt
    (id-selection again, never a re-typed URL)."""

    decision: ReplanDecisionKind
    finding_ref: Optional[int] = None
    new_objective: Optional[str] = None
    clarification_question: Optional[str] = None
