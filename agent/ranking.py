"""Structured semantic ranking contract (BrowserAgent_General_Autonomous_Agent_Architecture_
REVISED.pdf, section 9.2). Same anti-hallucination shape as router/resources.py::
select_relevant_tabs and research/discovery.py's link selection: the model chooses candidate
*ids* it was already given, never invents a new entity or attribute — deterministic code
(agent/workspace_ops.py) owns what candidates exist; this module only ever orders/selects
among them.

Deterministic-first (section 14: "Use deterministic numeric preprocessing; compare against
hidden labels"): when the request names exactly one numeric preference and no semantic
preference, `rank_candidates` sorts directly via agent/workspace_ops.py and never calls the
model — zero extra cost for the common "cheapest"/"highest-rated" case. A model call only
happens for genuinely semantic comparisons ("best overall", "most suitable for a student").
"""
from __future__ import annotations

import json
from typing import Literal, Optional

from pydantic import BaseModel, Field, TypeAdapter, ValidationError

from agent import workspace_ops
from agent.workspace_models import WorkspaceEntity
from inference.llama_client import InferenceClient


class NumericPreference(BaseModel):
    field: str
    direction: Literal["min", "max"] = "max"
    weight: float = 1.0


class RankRequest(BaseModel):
    entity_ids: list[str]
    objective: str
    hard_constraints: list[str] = Field(default_factory=list)
    numeric_preferences: list[NumericPreference] = Field(default_factory=list)
    semantic_preferences: list[str] = Field(default_factory=list)
    k: int = Field(ge=1, le=20)


class RankResult(BaseModel):
    ranked_entity_ids: list[str]
    rationale_by_entity: dict[str, str] = Field(default_factory=dict)
    missing_information: list[str] = Field(default_factory=list)


class RankingOutputError(ValueError):
    """Raised when the model's ranking output fails schema validation or names no valid
    candidate id — the caller should treat this exactly like "ranking unavailable", never
    fabricate a result."""


_RANK_SCHEMA = TypeAdapter(RankResult).json_schema()
_RANK_SCHEMA["title"] = "RankResult"

_RANK_PROMPT = """You are ranking a fixed set of already-collected candidate records for a
local browser-automation agent called BrowserAgent. You may NEVER invent a candidate, an id,
or an attribute value that is not already listed below — only choose and order among the ids
given. Respond with ONLY a single JSON object (no prose, no markdown fences):

{{"ranked_entity_ids": ["...", "..."], "rationale_by_entity": {{"ent_id": "short reason"}},
 "missing_information": ["..."]}}

Rules:
- Return at most {k} ids, best match first, chosen only from the candidate ids listed below.
- Honor every hard constraint; drop a candidate entirely if it clearly violates one.
- If fewer than {k} candidates are usable, return fewer — never repeat an id to pad the count.
- If something needed to rank confidently is missing from a candidate's data, name it in
  missing_information instead of guessing.

OBJECTIVE
{objective}

HARD CONSTRAINTS
{constraints}

CANDIDATES
{candidates}
"""


def _render_candidates(entities: list[WorkspaceEntity]) -> str:
    lines = []
    for e in entities:
        attrs = ", ".join(f"{k}={v}" for k, v in e.attributes.items())
        lines.append(f'[{e.id}] "{e.name or ""}" ({attrs})')
    return "\n".join(lines) if lines else "(none)"


def _deterministic_only(request: RankRequest) -> bool:
    return len(request.numeric_preferences) == 1 and not request.semantic_preferences


def _deterministic_rank(request: RankRequest, entities: list[WorkspaceEntity]) -> RankResult:
    pref = request.numeric_preferences[0]
    direction = "asc" if pref.direction == "min" else "desc"
    ranked = workspace_ops.select_top_k(entities, request.k, field=pref.field, direction=direction)
    return RankResult(ranked_entity_ids=[e.id for e in ranked])


async def rank_candidates(
    client: InferenceClient,
    request: RankRequest,
    entities: list[WorkspaceEntity],
    max_tokens: int = 500,
) -> RankResult:
    """Select/order up to `request.k` entities from `entities` (must be a superset of
    `request.entity_ids`). Raises RankingOutputError on any schema failure or on a result with
    no valid candidate ids — callers must not silently substitute a guess."""
    by_id = {e.id: e for e in entities if e.id in set(request.entity_ids)}
    if not by_id:
        raise RankingOutputError("no candidate entities available to rank")

    if _deterministic_only(request):
        return _deterministic_rank(request, list(by_id.values()))

    prompt = _RANK_PROMPT.format(
        k=request.k,
        objective=request.objective,
        constraints="\n".join(f"- {c}" for c in request.hard_constraints) or "(none)",
        candidates=_render_candidates(list(by_id.values())),
    )
    result = await client.complete(prompt, max_tokens=max_tokens, json_schema=_RANK_SCHEMA)
    try:
        raw = json.loads(result.text)
    except json.JSONDecodeError as exc:
        raise RankingOutputError(f"ranking output was not valid JSON: {exc.msg}") from exc
    try:
        parsed = RankResult.model_validate(raw)
    except ValidationError as exc:
        raise RankingOutputError(f"ranking output failed schema validation: {exc}") from exc

    seen: set[str] = set()
    valid_ids: list[str] = []
    for entity_id in parsed.ranked_entity_ids:
        if entity_id in by_id and entity_id not in seen:
            seen.add(entity_id)
            valid_ids.append(entity_id)
    if not valid_ids:
        raise RankingOutputError("ranking output named no id from the given candidate set")
    return RankResult(
        ranked_entity_ids=valid_ids[: request.k],
        rationale_by_entity={k: v for k, v in parsed.rationale_by_entity.items() if k in seen},
        missing_information=parsed.missing_information,
    )
