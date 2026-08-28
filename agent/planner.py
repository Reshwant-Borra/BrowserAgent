"""Structured qwen3:8b calls for the general controller's high-level planning (BrowserAgent_
General_Autonomous_Agent_Architecture_REVISED.pdf, section 7). Same inference-client/
json_schema mechanism as router/semantic_planner.py and research/discovery.py — no new HTTP/
retry/diagnostics machinery invented here.

Distinct system prompt from inference/prompt.py's SYSTEM_BLOCK (the click-level executor
prompt AgentLoop uses): this module's prompts explicitly forbid click-level output and only
ever produce a ControllerDecision (subgoal planning/replanning) or a CompletionEvaluation
(boundary check against the workspace). Section 14's decision — "Separate planner model: NO
now" — means the same qwen3:8b client is used, just with a different structured contract.
"""
from __future__ import annotations

import json
from typing import Any

from pydantic import TypeAdapter, ValidationError

from agent.context_builder import build_workspace_summary
from agent.controller_models import CompletionEvaluation, ControllerDecision
from agent.workspace_models import WorkspaceView
from inference.llama_client import InferenceClient

_DECISION_SCHEMA = TypeAdapter(ControllerDecision).json_schema()
_DECISION_SCHEMA["title"] = "ControllerDecision"
_EVALUATION_SCHEMA = TypeAdapter(CompletionEvaluation).json_schema()
_EVALUATION_SCHEMA["title"] = "CompletionEvaluation"

_PLANNER_SYSTEM = """You are the high-level task controller for a local browser-automation
agent called BrowserAgent. You NEVER choose a click, form field, selector, or any other
page-level action — a completely separate executor handles that, one page at a time, and
cannot see this instruction. Your only job is breaking a goal into a short ordered list of
concrete subgoals, and revising that list when something goes wrong. Respond with ONLY a
single JSON object matching this shape (no prose, no markdown fences):

{{"decision": "start_subgoal|revise_plan|finish|ask_user",
 "reason_code": "initial_plan|subgoal_complete|new_constraint|repeated_failure|
resource_missing|evidence_insufficient|completion_satisfied|user_update",
 "active_subgoal": "...", "plan": ["...", "..."], "resource_refs": [],
 "clarification_question": null, "completion_claim": null}}

Rules:
- Only ever answer "start_subgoal" (first planning) or "revise_plan" (replanning) unless told
  otherwise below. "finish" is only correct when the WORKSPACE section below already contains
  evidence that fully satisfies the goal with no further browser action needed at all.
  "ask_user" is only for a goal so ambiguous it cannot be planned into any subgoal.
- Each subgoal must be achievable by browsing, clicking, typing, selecting, scrolling,
  extracting text, or downloading a file on one bounded set of pages — never a subgoal that
  requires running code, calling an API directly, or anything outside a browser.
- Keep each subgoal short and concrete (a sentence, not a paragraph) and distinct from the
  others — not the whole goal restated, and not a click-level instruction naming a button.
- `plan` is the FULL ordered list of subgoals (2 to {max_subgoals} items); `active_subgoal`
  must be `plan[0]` when planning from scratch, or whichever subgoal should run next when
  revising an existing plan.
- Ground every subgoal in the goal text and the workspace summary below; do not restate
  generic advice unrelated to this specific goal.

GOAL
{goal}

COMPLETION CRITERIA
{criteria}

{workspace}
"""

_REPLAN_SUFFIX = """
REPLAN REASON
{reason_hint}

The plan above did not produce this outcome; produce a revised plan (it may keep, reorder, or
replace remaining subgoals) responding with "revise_plan"."""

_EVALUATOR_SYSTEM = """You are the completion evaluator for a local browser-automation agent
called BrowserAgent. You never take or suggest any browser action. Given the original goal,
its completion criteria, and everything the workspace below has actually collected (with
evidence), decide whether the goal is genuinely satisfied. Respond with ONLY a single JSON
object matching this shape (no prose, no markdown fences):

{{"satisfied": true|false, "missing_requirements": ["..."], "unsupported_claims": ["..."],
 "next_recommendation": "finish|continue|replan|ask_user"}}

Rules:
- "satisfied" is true only if every completion criterion is backed by evidence in the
  workspace below — never mark satisfied on a plausible-sounding claim with no evidence.
- List in "missing_requirements" any completion criterion not yet backed by evidence.
- List in "unsupported_claims" any workspace fact/entity that looks asserted without a
  matching evidence entry.
- "next_recommendation" is "finish" only when satisfied is true. If unsatisfied and the
  workspace suggests a different subgoal/approach would help, recommend "replan". If
  unsatisfied only because more of the *existing* plan still needs to run, recommend
  "continue". If unsatisfied and the goal itself is too ambiguous to make progress on,
  recommend "ask_user".

GOAL
{goal}

COMPLETION CRITERIA
{criteria}

{workspace}
"""


class PlannerOutputError(ValueError):
    """Raised when the model's structured controller output fails schema validation."""


def _render_criteria(success_criteria: list[str]) -> str:
    return "\n".join(f"- {c}" for c in success_criteria) if success_criteria else "(none specified)"


def _render_workspace(workspace: WorkspaceView, max_entities: int, max_evidence: int) -> str:
    return build_workspace_summary(workspace, max_entities, max_evidence)


def _cap_plan(decision: ControllerDecision, max_subgoals: int) -> ControllerDecision:
    """Defensive truncation: json_schema-constrained decoding does not enforce a dynamic max
    list length, so a plan longer than requested is trimmed here rather than trusted as-is."""
    if decision.plan and len(decision.plan) > max_subgoals:
        return decision.model_copy(update={"plan": decision.plan[:max_subgoals]})
    return decision


async def initial_plan(
    client: InferenceClient,
    goal: str,
    success_criteria: list[str],
    workspace: WorkspaceView,
    max_entities: int,
    max_evidence: int,
    max_subgoals: int,
    max_tokens: int = 700,
) -> ControllerDecision:
    prompt = _PLANNER_SYSTEM.format(
        goal=goal,
        criteria=_render_criteria(success_criteria),
        workspace=_render_workspace(workspace, max_entities, max_evidence),
        max_subgoals=max_subgoals,
    )
    result = await client.complete(prompt, max_tokens=max_tokens, json_schema=_DECISION_SCHEMA)
    decision = _validate_decision(result.text)
    return _cap_plan(decision, max_subgoals)


async def replan(
    client: InferenceClient,
    goal: str,
    success_criteria: list[str],
    workspace: WorkspaceView,
    reason_hint: str,
    max_entities: int,
    max_evidence: int,
    max_subgoals: int,
    max_tokens: int = 700,
) -> ControllerDecision:
    prompt = _PLANNER_SYSTEM.format(
        goal=goal,
        criteria=_render_criteria(success_criteria),
        workspace=_render_workspace(workspace, max_entities, max_evidence),
        max_subgoals=max_subgoals,
    ) + _REPLAN_SUFFIX.format(reason_hint=reason_hint)
    result = await client.complete(prompt, max_tokens=max_tokens, json_schema=_DECISION_SCHEMA)
    decision = _validate_decision(result.text)
    return _cap_plan(decision, max_subgoals)


async def evaluate_completion(
    client: InferenceClient,
    goal: str,
    success_criteria: list[str],
    workspace: WorkspaceView,
    max_entities: int,
    max_evidence: int,
    max_tokens: int = 500,
) -> CompletionEvaluation:
    prompt = _EVALUATOR_SYSTEM.format(
        goal=goal,
        criteria=_render_criteria(success_criteria),
        workspace=_render_workspace(workspace, max_entities, max_evidence),
    )
    result = await client.complete(prompt, max_tokens=max_tokens, json_schema=_EVALUATION_SCHEMA)
    return _validate_evaluation(result.text)


def _validate_decision(text: str) -> ControllerDecision:
    raw = _parse_json(text)
    try:
        return ControllerDecision.model_validate(raw)
    except ValidationError as exc:
        raise PlannerOutputError(f"controller decision failed schema validation: {exc}") from exc


def _validate_evaluation(text: str) -> CompletionEvaluation:
    raw = _parse_json(text)
    try:
        return CompletionEvaluation.model_validate(raw)
    except ValidationError as exc:
        raise PlannerOutputError(f"completion evaluation failed schema validation: {exc}") from exc


def _parse_json(text: str) -> dict[str, Any]:
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise PlannerOutputError(f"planner output was not valid JSON: {exc.msg}") from exc
