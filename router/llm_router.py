"""Qwen3-8B structured-output fallback for prompts `router/extract.py` couldn't route
deterministically. Reuses the exact same inference-client/JSON-schema mechanism as the
agent decision loop (`inference/llama_client.py`'s `OllamaClient.complete(json_schema=...)`,
added specifically so this module doesn't need its own HTTP/retry/diagnostics code) —
this is a Section 11 requirement: "use strict structured output... Validate task type,
targets, policy, objective before execution," and never trust prose parsing.
"""
from __future__ import annotations

import json
from typing import Any

from pydantic import TypeAdapter, ValidationError

from inference.llama_client import InferenceClient
from router.extract import extract_urls
from router.schema import RouterDecision

_ROUTER_SCHEMA = TypeAdapter(RouterDecision).json_schema()
_ROUTER_SCHEMA["title"] = "RouterDecision"

_PROMPT_TEMPLATE = """You are a task router for a local browser-automation agent. Read the
user's plain-English task and classify it. Respond with ONLY a single JSON object matching
this shape (no prose, no markdown fences):

{{"task_type": "single_site|multisite_sweep|ordered_workflow|research",
 "objective": "the user's goal, restated concisely",
 "targets": ["exact URLs copied verbatim from the task text, in the order given"],
 "requires_discovery": true|false,
 "preferred_policy": "read_only|reversible_actions",
 "result_contract": "generic|assignment|research",
 "workflow_steps": [{{"ordinal": 1, "target": "url", "objective": "what to do on this site"}}]}}

Rules:
- NEVER invent a URL that is not already present in the task text. Copy them exactly.
- "targets" MUST be a subset of this exact URL list already found in the text: {urls}
- If the task names one URL and asks to read/find/check something on it: single_site.
- If the task lists multiple independent URLs to check/sweep (no site depends on another
  and there's no explicit "then"/ordered sequence): multisite_sweep.
- If the task describes an ordered sequence across 2+ sites (site A then site B, do X then
  Y): ordered_workflow, and fill workflow_steps in order using only the given URLs.
- If the task asks for research/investigation across sources and gives NO explicit URLs:
  research, requires_discovery=true, targets=[].
- preferred_policy is "reversible_actions" only if the task explicitly asks to change,
  set, toggle, enable, or configure something; otherwise "read_only".
- result_contract is "assignment" only if the task is about assignments/homework/due dates,
  "research" only for the research task_type, otherwise "generic".

Task: {task}
"""


class RouterOutputError(ValueError):
    """Raised when the model's structured router output fails schema validation."""


async def route_with_model(client: InferenceClient, task_text: str, max_tokens: int = 400) -> RouterDecision:
    urls = extract_urls(task_text)
    prompt = _PROMPT_TEMPLATE.format(task=task_text, urls=json.dumps(urls))
    result = await client.complete(prompt, max_tokens=max_tokens, json_schema=_ROUTER_SCHEMA)
    try:
        raw = json.loads(result.text)
    except json.JSONDecodeError as exc:
        raise RouterOutputError(f"router model output was not valid JSON: {exc.msg}") from exc
    decision = _validate(raw)
    return _guard_against_hallucinated_targets(decision, urls)


def _validate(raw: dict[str, Any]) -> RouterDecision:
    try:
        return RouterDecision.model_validate(raw)
    except ValidationError as exc:
        raise RouterOutputError(f"router model output failed schema validation: {exc}") from exc


def _guard_against_hallucinated_targets(decision: RouterDecision, urls_in_text: list[str]) -> RouterDecision:
    """Section 12: the router must never invent extra websites/targets. Any target the model
    produced that wasn't already in the user's text is dropped rather than trusted — except
    when requires_discovery is true (research mode is explicitly allowed to have no fixed
    targets yet, since discovery happens later in the research pipeline)."""
    allowed = set(urls_in_text)
    filtered_targets = [t for t in decision.targets if t in allowed]
    filtered_steps = [s for s in decision.workflow_steps if s.target in allowed]
    return decision.model_copy(update={"targets": filtered_targets, "workflow_steps": filtered_steps})
