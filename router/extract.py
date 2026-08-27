"""Deterministic URL extraction and routing. Section 10 of the Phase 5B spec: do not call
Qwen when the shape of the prompt is already obvious from explicit URLs and simple keywords.
`route()` in `router/policy.py` calls `try_deterministic_route()` first and only falls back
to `router/llm_router.py` when this returns None.
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit, urlunsplit

from router.schema import RouterDecision, SafetyPolicy, TaskType, WorkflowStepPlan

_URL_RE = re.compile(r"https?://[^\s<>\"')\]]+", re.IGNORECASE)

_SEQUENCE_MARKERS = re.compile(
    r"\bthen\b|\bafter that\b|\bafterwards\b|\bnext\b|\bfirst\b.*\bsecond\b|\bstep\s*\d+\b|"
    r"\bfinally\b|\bonce\s+(?:that|it|done|complete)\b",
    re.IGNORECASE,
)

# Boundaries used to split an ordered-workflow prompt into one clause per step (Section 3 of
# the master status task: "decompose ordered multi-site prompts into distinct per-target/
# sub-step objectives"). Kept separate from _SEQUENCE_MARKERS above, which only decides
# *whether* a prompt looks sequential — this is used to slice it once it does.
_STEP_SPLIT_RE = re.compile(
    r"\band\s+then\b|\bafter\s+that\b|\bafterwards\b|\bthen\b|\bnext\b|\bfinally\b|"
    r"\bonce\s+(?:that|it)(?:\s*'s|\s+is)?\s+(?:done|complete)\b|\bstep\s*\d+\s*:?",
    re.IGNORECASE,
)

# Connector/filler words that surround a target URL inside a single clause ("go to <url> and
# set...", "...change the mode on <url>") but carry no step-specific meaning once the clause
# has already been isolated by _STEP_SPLIT_RE and the URL itself has been removed.
_STEP_FILLER_ALTS = (
    r"and|then|next|finally|first|after\s+that|afterwards|"
    r"once\s+(?:that|it)(?:'s|\s+is)?\s+(?:done|complete)|"
    r"go\s+to|visit|navigate\s+to|open|on|at"
)
_LEAD_FILLER_RE = re.compile(rf"^(?:{_STEP_FILLER_ALTS})\b[\s,]*", re.IGNORECASE)
_TRAIL_FILLER_RE = re.compile(rf"[\s,]*\b(?:{_STEP_FILLER_ALTS})\s*$", re.IGNORECASE)

# Policy inference stays conservative (Section 18 in schema docstring: only a *default*,
# real gating happens in agent/schemas.classify_risk). "click"/"open" are deliberately
# excluded here — they're common in pure read prompts ("Open X and tell me...") and would
# mislabel reads as reversible actions.
_ACTION_VERBS = re.compile(
    r"\b(change|set|toggle|select|enable|disable|update|configure|switch|turn on|turn off|"
    r"enter|type|fill|choose|submit)\b",
    re.IGNORECASE,
)

# Broader verb set used only for ordered-workflow *routing* (Finding 8: "enter"/"type"/"fill"
# were missing, so cross-site fact-passing prompts like "find X on A, then enter it on B" fell
# through to multisite_sweep). Gated by _looks_sequential also requiring >=2 targets AND an
# explicit sequence marker, so adding generic verbs like "click"/"open" here does not turn
# ordinary prose into an ordered workflow.
_ROUTING_ACTION_VERBS = re.compile(
    r"\b(change|set|toggle|select|choose|enable|disable|update|configure|switch|"
    r"turn on|turn off|enter|type|fill|click|open|submit)\b",
    re.IGNORECASE,
)

_READ_VERBS = re.compile(
    r"\b(find|check|tell me|look for|see if|review|read|list|verify|report|show me)\b",
    re.IGNORECASE,
)

_RESEARCH_VERBS = re.compile(
    r"\b(research|investigate|compile|survey|compare\s+(?:sources|advice)|"
    r"evidence[- ]backed|across\s+(?:the\s+web|sources|the\s+internet))\b",
    re.IGNORECASE,
)


def extract_urls(text: str) -> list[str]:
    """Extracts URLs exactly as written (trailing punctuation trimmed), preserving the
    original order and de-duplicating. Never invents or normalizes away the user's targets
    (Section 12) — normalization for scope/dedupe purposes happens later in batch/policies.py."""
    seen: set[str] = set()
    urls: list[str] = []
    for match in _URL_RE.finditer(text):
        url = match.group(0).rstrip(").,;:!?\"'")
        if url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


def normalize_url(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, parts.query, ""))


def infer_policy(text: str) -> SafetyPolicy:
    """Section 18: conservative keyword inference. Consequential actions remain gated
    separately by agent/schemas.py's classify_risk() regardless of this result — this only
    decides the *default* runtime_policy.read_only flag for batch/workflow policies."""
    if _ACTION_VERBS.search(text):
        return SafetyPolicy.REVERSIBLE_ACTIONS
    return SafetyPolicy.READ_ONLY


def _looks_sequential(text: str, url_count: int) -> bool:
    return url_count >= 2 and bool(_SEQUENCE_MARKERS.search(text)) and bool(_ROUTING_ACTION_VERBS.search(text))


def _clean_step_objective(segment: str, url: str) -> str:
    """Strips the target URL and surrounding connector/filler words from one clause of an
    ordered-workflow prompt, leaving just that step's objective."""
    text = segment.replace(url, " ")
    text = re.sub(r"\s+", " ", text).strip(" ,.;:")
    prev = None
    while prev != text and text:
        prev = text
        text = _LEAD_FILLER_RE.sub("", text, count=1).strip(" ,.;:")
    prev = None
    while prev != text and text:
        prev = text
        text = _TRAIL_FILLER_RE.sub("", text, count=1).strip(" ,.;:")
    if not text:
        text = segment.replace(url, "").strip(" ,.;:")
    return text[0].upper() + text[1:] if text else text


def _split_ordered_steps(text: str, urls: list[str]) -> list[WorkflowStepPlan] | None:
    """Slices an ordered-workflow prompt into one WorkflowStepPlan per target, in order.
    Returns None (caller falls back to the Qwen router) if the clause boundaries don't line
    up 1:1 with the targets — better to defer than to invent/mis-assign a step's objective."""
    segments = [s for s in _STEP_SPLIT_RE.split(text) if s.strip()]
    if len(segments) != len(urls):
        return None
    steps = []
    for i, (segment, url) in enumerate(zip(segments, urls)):
        if url not in segment:
            return None
        objective = _clean_step_objective(segment, url)
        if not objective:
            return None
        steps.append(WorkflowStepPlan(ordinal=i + 1, target=url, objective=objective))
    return steps


def _looks_research(text: str, url_count: int) -> bool:
    return bool(_RESEARCH_VERBS.search(text)) and url_count == 0


def try_deterministic_route(text: str) -> RouterDecision | None:
    """Returns a RouterDecision when the prompt shape is unambiguous, else None (caller
    should fall back to the Qwen router). Never invents targets: `targets` is always a
    subset of `extract_urls(text)`, exactly as written."""
    urls = extract_urls(text)
    stripped = text.strip()

    if _looks_research(stripped, len(urls)):
        return RouterDecision(
            task_type=TaskType.RESEARCH,
            objective=stripped,
            targets=[],
            requires_discovery=True,
            preferred_policy=SafetyPolicy.READ_ONLY,
            result_contract="research",
        )

    if _looks_sequential(stripped, len(urls)):
        steps = _split_ordered_steps(stripped, urls)
        if steps is None:
            return None  # sequential language detected but clause boundaries are ambiguous
        return RouterDecision(
            task_type=TaskType.ORDERED_WORKFLOW,
            objective=stripped,
            targets=urls,
            requires_discovery=False,
            preferred_policy=infer_policy(stripped),
            result_contract="generic",
            workflow_steps=steps,
        )

    if len(urls) == 1:
        return RouterDecision(
            task_type=TaskType.SINGLE_SITE,
            objective=stripped,
            targets=urls,
            requires_discovery=False,
            preferred_policy=infer_policy(stripped),
            result_contract="generic",
        )

    if len(urls) >= 2:
        return RouterDecision(
            task_type=TaskType.MULTISITE_SWEEP,
            objective=stripped,
            targets=urls,
            requires_discovery=False,
            preferred_policy=SafetyPolicy.READ_ONLY,
            result_contract=_guess_sweep_contract(stripped),
        )

    return None  # no URLs, not research-shaped -> ambiguous, needs the Qwen fallback


def _guess_sweep_contract(text: str) -> str:
    lowered = text.lower()
    if "assignment" in lowered or "due" in lowered or "homework" in lowered:
        return "assignment"
    return "generic"
