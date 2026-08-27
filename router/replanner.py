"""Bounded, single-shot replanning (Section 12-13 of the semantic planner task): called at
most once, after a `multisite_sweep` execution finishes with structured findings, and only
when the originating plan's intent was "mixed" (`RouterDecision.mixed_intent_followup`) —
e.g. "find the assignment with the nearest deadline and open it." This is deliberately not a
loop: one schema-constrained call decides whether a deterministic follow-up single-site task
is warranted, and if so, which finding it applies to (by id, never a re-typed URL — same
anti-hallucination shape as router/resources.py's tab selection and research/discovery.py's
link selection).
"""
from __future__ import annotations

import json
from typing import Any

from pydantic import TypeAdapter, ValidationError

from inference.llama_client import InferenceClient
from router.plan_schema import ReplanDecision

_REPLAN_SCHEMA = TypeAdapter(ReplanDecision).json_schema()
_REPLAN_SCHEMA["title"] = "ReplanDecision"

_REPLAN_PROMPT = """You just finished checking several pages for a task and found these
results. Decide whether a deterministic follow-up action is needed. Respond with ONLY a
single JSON object matching this shape (no prose, no markdown fences):

{{"decision": "continue|revise|clarify|finish", "finding_ref": <id or null>,
 "new_objective": "..." or null, "clarification_question": "..." or null}}

Rules:
- Use "revise" only if the original goal asked you to select ONE specific result (e.g. the
  nearest deadline, the cheapest option, the most relevant item) and act further on it — set
  finding_ref to the id of the ONE finding that best satisfies that selection, and
  new_objective to a short instruction for what to do with it (e.g. "open this assignment").
- Use "finish" if the findings already fully answer the goal with no further action needed.
- Use "clarify" only if you genuinely cannot tell which finding satisfies the goal — set
  clarification_question to a short question for the user.
- NEVER invent a finding id that is not listed below.

Original goal: {goal}

Findings:
{findings}
"""


class ReplanOutputError(ValueError):
    """Raised when the model's structured replan output fails schema validation."""


async def decide_replan(
    client: InferenceClient, goal: str, findings: list[dict[str, Any]], max_tokens: int = 300
) -> ReplanDecision:
    findings_block = "\n".join(
        f"[{i}] {json.dumps(f.get('finding', f))[:300]}" for i, f in enumerate(findings[:20])
    )
    prompt = _REPLAN_PROMPT.format(goal=goal, findings=findings_block)
    result = await client.complete(prompt, max_tokens=max_tokens, json_schema=_REPLAN_SCHEMA)
    try:
        raw = json.loads(result.text)
    except json.JSONDecodeError as exc:
        raise ReplanOutputError(f"replan output was not valid JSON: {exc.msg}") from exc
    try:
        return ReplanDecision.model_validate(raw)
    except ValidationError as exc:
        raise ReplanOutputError(f"replan output failed schema validation: {exc}") from exc


def finding_source_url(findings: list[dict[str, Any]], finding_ref: int) -> str | None:
    if finding_ref is None or not (0 <= finding_ref < len(findings)):
        return None
    finding = findings[finding_ref].get("finding") or {}
    return finding.get("source_url")
