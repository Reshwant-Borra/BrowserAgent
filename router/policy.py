"""Single entrypoint the UI (and anything else) calls to turn a plain-English prompt into
a validated RouterDecision, plus the small conversions into the existing engine's own
policy types (`batch.models.BatchPolicy`) so callers never have to know the router exists —
Section 8/9 of the Phase 5B spec: "translate the user's plain-English task into existing
BrowserAgent execution primitives," nothing more.
"""
from __future__ import annotations

from batch.models import BatchPolicy, NavigationScope
from inference.llama_client import InferenceClient
from router.extract import try_deterministic_route
from router.llm_router import RouterOutputError, route_with_model
from router.schema import RouterDecision, SafetyPolicy, TaskType


class RoutingError(ValueError):
    """Raised when neither the deterministic rules nor the model fallback could produce a
    usable route (e.g. the model is unreachable and the prompt genuinely is ambiguous)."""


async def route(task_text: str, client: InferenceClient | None = None) -> RouterDecision:
    text = (task_text or "").strip()
    if not text:
        raise RoutingError("task text is empty")

    deterministic = try_deterministic_route(text)
    if deterministic is not None:
        return deterministic

    if client is None:
        raise RoutingError(
            "task could not be routed deterministically and no model client was provided "
            "for the natural-language fallback"
        )
    try:
        return await route_with_model(client, text)
    except RouterOutputError as exc:
        raise RoutingError(f"model-based routing failed: {exc}") from exc


def to_batch_policy(decision: RouterDecision, **overrides) -> BatchPolicy:
    """Only meaningful for multisite_sweep/research decisions (both execute through
    BatchOrchestrator)."""
    kwargs = dict(
        read_only=decision.preferred_policy == SafetyPolicy.READ_ONLY,
        navigation_scope=NavigationScope.SAME_ORIGIN,
    )
    kwargs.update(overrides)
    return BatchPolicy(**kwargs)


def is_sequential(decision: RouterDecision) -> bool:
    return decision.task_type == TaskType.ORDERED_WORKFLOW
