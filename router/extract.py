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
    r"\bthen\b|\bafter that\b|\bnext\b|\bfirst\b.*\bsecond\b|\bstep\s*\d+\b|"
    r"\bfinally\b|\bonce\s+(?:that|done|complete)\b",
    re.IGNORECASE,
)

_ACTION_VERBS = re.compile(
    r"\b(change|set|toggle|select|enable|disable|update|configure|switch|turn on|turn off)\b",
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
    return url_count >= 2 and bool(_SEQUENCE_MARKERS.search(text)) and bool(_ACTION_VERBS.search(text))


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
        steps = [
            WorkflowStepPlan(ordinal=i + 1, target=url, objective=stripped)
            for i, url in enumerate(urls)
        ]
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
