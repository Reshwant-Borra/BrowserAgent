"""Qwen3-8B schema-constrained semantic planning call (docs/SEMANTIC_PLANNER.md). Produces a
TaskPlan from a plain-English prompt using the exact same inference-client/json_schema
mechanism as router/llm_router.py's legacy fallback and research/discovery.py's link
selection — no new HTTP/retry/diagnostics machinery invented here.

This module's only job is turning "what does the user mean" into a validated TaskPlan.
Turning that plan into something executable (resolving resources, deciding the concrete
RouterDecision) is entirely router/resources.py's and router/policy.py's job — this module
never touches a browser and never resolves a resource itself.
"""
from __future__ import annotations

import json
from typing import Any

from pydantic import TypeAdapter, ValidationError

from inference.llama_client import InferenceClient
from router.extract import extract_urls
from router.plan_schema import TaskPlan

_PLAN_SCHEMA = TypeAdapter(TaskPlan).json_schema()
_PLAN_SCHEMA["title"] = "TaskPlan"

_PLANNER_PROMPT = """You are a semantic task planner for a local browser-automation agent
called BrowserAgent. Read the user's plain-English request and produce a structured plan.
Respond with ONLY a single JSON object matching this shape (no prose, no markdown fences):

{{"goal": "...", "intent": "read|act|research|mixed",
 "execution_shape": "single|sweep|ordered_workflow|open_research",
 "resource_requirements": [{{"kind": "explicit_urls|current_page|open_tabs|web_discovery", "description": "..."}}],
 "steps": [{{"ordinal": 1, "resource_ref": 0, "objective": "..."}}],
 "constraints": {{"read_only": true|false, "navigation_scope": "same_origin|same_domain|unrestricted"}},
 "result_contract": "generic|assignment|research"}}

Resource kinds — choose based on what the user actually said, never invent a URL yourself:
- explicit_urls: the user already wrote one or more literal URLs in their request. URLs
  already found in this request: {urls}
- current_page: the request refers to a SINGLE unnamed page with nothing suggesting a
  different, separate page is also involved — "this page", "the page I'm on". No URL is ever
  supplied for this kind, leave `description` empty. Only use this when there is exactly one
  implicit target in the whole request.
- open_tabs: the request refers to something already open in the browser without giving a
  literal URL — this covers BOTH explicit phrasing ("my course pages", "the tabs I opened")
  AND implicit references to a page identified by what it contains rather than "this page"
  ("the page where the code is listed", "the configuration page", "whichever page has the
  invoice number"). Set `description` to a short phrase distinguishing which open tab is
  meant, so it can be matched by content. IMPORTANT: whenever a request implies TWO OR MORE
  distinct pages with no explicit URLs (e.g. an ordered_workflow with 2+ steps, each on a
  different unnamed page), use open_tabs for EACH of them with a distinguishing description —
  never current_page, since "current page" can only ever mean one single page at a time and
  cannot refer to two different pages in the same request.
- web_discovery: the user wants information from the open web with no specific site given
  (a research/investigation request). Set `description` to the research objective.

Example: "Find the project code on the page where it's listed, then put that same code into
the configuration page and verify it." -> two open_tabs requirements (NOT current_page):
[{{"kind": "open_tabs", "description": "the page listing the project code"}},
 {{"kind": "open_tabs", "description": "the configuration/settings page"}}], with steps
referencing resource_ref 0 and 1 respectively.

Execution shape — decide using these rules, in order, and stop at the first one that applies:
1. Multiple targets with NO sequencing language (no "then", "after that", "next", "once ...
   done", "first ... finally", "step 1"/"step 2") and no step needing a fact discovered by
   another step: always "sweep" — even if the request asks you to compare the targets or pick
   a "best"/"cheapest"/"nearest"/"most urgent" one afterward. Picking a favorite among
   independently-read targets is still one sweep, never a multi-step sequence, because there
   is nothing to do differently at each target — you just read all of them the same way.
   Example: "Check A and B and tell me which is cheaper" -> sweep, NOT ordered_workflow.
2. Multiple targets WITH explicit sequencing language, or a later target's action genuinely
   depends on a fact discovered at an earlier target (e.g. "find the code on A, then enter it
   on B"): "ordered_workflow". Fill `steps` in order; each step's `resource_ref` is the index
   into `resource_requirements` for the requirement that step targets, and that requirement
   must resolve to exactly one target.
3. One target, or none (referring to the current page): "single".
4. Information wanted from the open web with no specific site given: "open_research" (use
   resource kind web_discovery).

Intent — decide using these rules, in order, and stop at the first one that applies:
1. "mixed": the request finds/checks/compares something (across one or many targets) AND THEN
   takes a further action on the ONE thing selected from what was found. The pattern is
   "gather or compare, then act on the winner." Examples: "find the assignment with the
   nearest deadline and open it", "check A and B, find the cheapest, and open it", "check my
   tabs and pull up whichever is most urgent". These are "mixed" even though the execution
   shape is "sweep" per rule 1 above — the follow-up action happens after the sweep finishes,
   it is not a second step within it.
2. "act": the request directly performs a change/action with no prior selection step —
   e.g. "turn on dark mode", "enter this code", "enable notifications". If the request also
   involves finding/comparing something first (see "mixed" above), use "mixed", not "act".
3. "research": the request wants an evidence-backed report/summary of what multiple external
   sources say, with no specific site given.
4. "read": everything else — checking, finding, or summarizing without changing anything and
   without a further action on a specific selected result afterward.

Rules:
- NEVER invent a URL. Only use `explicit_urls` when the user's text actually contains one.
- `constraints.read_only` is false only if the request explicitly asks to change, set,
  toggle, enable, configure, submit, enter, fill, or type something; otherwise true.
- `result_contract` is "assignment" only for assignment/homework/due-date requests,
  "research" only when execution_shape is open_research, otherwise "generic".
- Keep `resource_requirements` minimal — one entry per distinct thing that needs resolving.

User request: {task}
"""


class PlannerOutputError(ValueError):
    """Raised when the model's structured plan output fails schema validation."""


async def plan_task(client: InferenceClient, task_text: str, max_tokens: int = 600) -> TaskPlan:
    urls = extract_urls(task_text)
    prompt = _PLANNER_PROMPT.format(task=task_text, urls=json.dumps(urls))
    result = await client.complete(prompt, max_tokens=max_tokens, json_schema=_PLAN_SCHEMA)
    try:
        raw = json.loads(result.text)
    except json.JSONDecodeError as exc:
        raise PlannerOutputError(f"planner output was not valid JSON: {exc.msg}") from exc
    return _validate(raw)


def _validate(raw: dict[str, Any]) -> TaskPlan:
    try:
        return TaskPlan.model_validate(raw)
    except ValidationError as exc:
        raise PlannerOutputError(f"planner output failed schema validation: {exc}") from exc
