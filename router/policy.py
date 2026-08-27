"""Single entrypoint the UI (and anything else) calls to turn a plain-English prompt into a
validated RouterDecision, plus the small conversions into the existing engine's own policy
types (`batch.models.BatchPolicy`) so callers never have to know the router exists.

Three routing modes (`config.routing.mode`, see agent/config.py's RoutingConfig):

- "legacy": deterministic fast path (router/extract.py), then the old keyword/regex-adjacent
  Qwen fallback (router/llm_router.py) — exactly the pre-semantic-planner behavior, kept
  available as a fallback (Section 32 of the semantic planner task).
- "semantic": deterministic fast path, then the semantic planner (router/semantic_planner.py)
  + resource resolver (router/resources.py) only — no legacy Qwen fallback.
- "hybrid" (default): deterministic fast path, then the semantic planner+resolver, falling
  back to the legacy Qwen router only if planning/resolution itself fails outright (a model
  output/schema error) — never for an unresolvable resource, which produces a NeedsInput
  result instead (Section 14: "no targets found" is not an acceptable failure mode).

This module is also where a TaskPlan becomes an executable RouterDecision. Deterministic
translation only: it never invents a target the resolver didn't produce, and any resource
requirement that can't be resolved surfaces as `NeedsInput` rather than an empty-target
RouterDecision (which is what caused the original "no targets found" failure this exists to
fix). AgentLoop / BatchOrchestrator / WorkflowOrchestrator / the research pipeline are
completely unaware any of this exists — they only ever see a RouterDecision, exactly as
before.
"""
from __future__ import annotations

from dataclasses import dataclass

from batch.models import BatchPolicy, NavigationScope
from inference.llama_client import InferenceClient
from router.extract import extract_urls, try_deterministic_route
from router.llm_router import RouterOutputError, route_with_model
from router.plan_schema import ExecutionShape, PlanIntent, ResourceKind, ResourceRequirement, TaskPlan
from router.resources import ResourceResolutionError, ResourceResolver
from router.schema import RouterDecision, SafetyPolicy, TaskType, WorkflowStepPlan
from router.semantic_planner import PlannerOutputError, plan_task

DEFAULT_ROUTING_MODE = "hybrid"


class RoutingError(ValueError):
    """Raised when neither the deterministic rules nor any model-based path could produce a
    usable route (e.g. the model is unreachable and the prompt genuinely is ambiguous)."""


@dataclass
class NeedsInput:
    """Returned instead of a RouterDecision when the plan requires a resource that could not
    be safely resolved (Section 14 of the semantic planner task). Callers (ui/jobs.py)
    persist this as a `waiting_for_input` job state and, once the user responds, re-route
    with the answer appended to the original prompt rather than starting over."""

    question: str
    plan: TaskPlan


async def route(
    task_text: str,
    client: InferenceClient | None = None,
    config=None,
) -> RouterDecision | NeedsInput:
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

    # Callers that don't pass `config` (e.g. benchmarks/run_phase5b_assignment_sweep_live.py)
    # get exactly the pre-existing legacy behavior — passing config is what opts a caller
    # into the semantic planner at all.
    mode = getattr(getattr(config, "routing", None), "mode", "legacy") if config is not None else "legacy"

    if mode == "legacy":
        return await _legacy_fallback(client, text)

    try:
        plan = await plan_task(client, text)
    except PlannerOutputError as exc:
        if mode == "hybrid":
            return await _legacy_fallback(client, text)
        raise RoutingError(f"semantic planning failed: {exc}") from exc

    try:
        return await _translate_plan(text, plan, client, config)
    except (RoutingError, ResourceResolutionError) as exc:
        if mode == "hybrid":
            return await _legacy_fallback(client, text)
        raise RoutingError(f"semantic plan translation failed: {exc}") from exc


async def _legacy_fallback(client: InferenceClient, text: str) -> RouterDecision:
    try:
        return await route_with_model(client, text)
    except RouterOutputError as exc:
        raise RoutingError(f"model-based routing failed: {exc}") from exc


async def route_with_answer(
    original_text: str,
    answer_text: str,
    client: InferenceClient,
    config,
) -> RouterDecision | NeedsInput:
    """Re-routes after a NeedsInput clarification response. Concatenating the answer onto
    the original prompt (rather than trying to patch just the unresolved resource
    requirement) keeps this simple and stateless: the planner sees the full, now-more-
    specific request and re-derives everything, same as a person re-reading a clarified
    request rather than trying to mentally diff it."""
    combined = f"{original_text}\n\nAdditional information from the user: {answer_text}"
    return await route(combined, client, config)


# ---- plan -> RouterDecision translation ------------------------------------------------

async def _translate_plan(task_text: str, plan: TaskPlan, client: InferenceClient, config) -> RouterDecision | NeedsInput:
    if config is None:
        raise RoutingError("semantic routing requires config (for browser.mode/cdp_endpoint)")
    resolver = ResourceResolver(config, client, task_text)
    preferred_policy = SafetyPolicy.READ_ONLY if plan.constraints.read_only else SafetyPolicy.REVERSIBLE_ACTIONS

    if plan.execution_shape == ExecutionShape.OPEN_RESEARCH:
        return RouterDecision(
            task_type=TaskType.RESEARCH,
            objective=plan.goal,
            targets=[],
            requires_discovery=True,
            preferred_policy=SafetyPolicy.READ_ONLY,
            result_contract="research",
        )
    if plan.execution_shape == ExecutionShape.SINGLE:
        return await _translate_single(plan, resolver, preferred_policy)
    if plan.execution_shape == ExecutionShape.SWEEP:
        return await _translate_sweep(plan, resolver)
    if plan.execution_shape == ExecutionShape.ORDERED_WORKFLOW:
        return await _translate_workflow(plan, resolver, preferred_policy)
    raise RoutingError(f"unsupported execution shape: {plan.execution_shape!r}")


async def _translate_single(plan: TaskPlan, resolver: ResourceResolver, preferred_policy: SafetyPolicy) -> RouterDecision | NeedsInput:
    reqs = plan.resource_requirements
    resolvable = [(i, r) for i, r in enumerate(reqs) if r.kind != ResourceKind.CURRENT_PAGE]
    if not resolvable:
        # No requirement, or only a current_page one: empty targets is the existing, correct
        # "act on whatever page is already open/attached" signal AgentLoop already handles.
        return RouterDecision(
            task_type=TaskType.SINGLE_SITE, objective=plan.goal, targets=[],
            requires_discovery=False, preferred_policy=preferred_policy, result_contract=plan.result_contract,
        )
    index, req = resolvable[0]
    resolved = await resolver.resolve(index, req)
    if resolved is None or not resolved.urls:
        return NeedsInput(question=_clarification_question(req), plan=plan)
    return RouterDecision(
        task_type=TaskType.SINGLE_SITE, objective=plan.goal, targets=[resolved.urls[0]],
        requires_discovery=False, preferred_policy=preferred_policy, result_contract=plan.result_contract,
    )


async def _translate_sweep(plan: TaskPlan, resolver: ResourceResolver) -> RouterDecision | NeedsInput:
    reqs = plan.resource_requirements
    if not reqs:
        raise RoutingError("sweep plan has no resource requirements")
    all_urls: list[str] = []
    first_req: ResourceRequirement | None = None
    for i, req in enumerate(reqs):
        if req.kind == ResourceKind.CURRENT_PAGE:
            continue
        first_req = first_req or req
        resolved = await resolver.resolve(i, req)
        if resolved is None:
            continue
        for u in resolved.urls:
            if u not in all_urls:
                all_urls.append(u)
    if not all_urls:
        return NeedsInput(question=_clarification_question(first_req or reqs[0]), plan=plan)
    return RouterDecision(
        task_type=TaskType.MULTISITE_SWEEP, objective=plan.goal, targets=all_urls,
        requires_discovery=False, preferred_policy=SafetyPolicy.READ_ONLY, result_contract=plan.result_contract,
        mixed_intent_followup=(plan.intent == PlanIntent.MIXED),
    )


async def _translate_workflow(plan: TaskPlan, resolver: ResourceResolver, preferred_policy: SafetyPolicy) -> RouterDecision | NeedsInput:
    reqs = plan.resource_requirements
    ordered_steps = sorted(plan.steps, key=lambda s: s.ordinal)
    if not ordered_steps:
        raise RoutingError("ordered_workflow plan has no steps")
    # extract_urls(text) returns every literal URL in the prompt with no notion of "which
    # step this one belongs to" — resolving each explicit_urls requirement independently
    # would hand every step the exact same full URL list. Assign them positionally instead:
    # the k-th explicit_urls requirement (in resource_requirements order) gets the k-th URL
    # in the text, matching how a person reads "site A ... then ... site B" left to right.
    explicit_url_by_ref = _assign_explicit_urls(reqs, resolver.prompt_text)
    steps_out: list[WorkflowStepPlan] = []
    for step in ordered_steps:
        if step.resource_ref >= len(reqs):
            raise RoutingError(f"workflow step {step.ordinal} references an invalid resource_ref {step.resource_ref}")
        req = reqs[step.resource_ref]
        if req.kind == ResourceKind.EXPLICIT_URLS:
            url = explicit_url_by_ref.get(step.resource_ref)
            resolved_urls = [url] if url else []
        else:
            resolved = await resolver.resolve(step.resource_ref, req)
            resolved_urls = resolved.urls if resolved else []
        if len(resolved_urls) != 1:
            return NeedsInput(question=_clarification_question(req), plan=plan)
        steps_out.append(WorkflowStepPlan(ordinal=step.ordinal, target=resolved_urls[0], objective=step.objective))
    targets = [s.target for s in steps_out]
    return RouterDecision(
        task_type=TaskType.ORDERED_WORKFLOW, objective=plan.goal, targets=targets,
        requires_discovery=False, preferred_policy=preferred_policy, result_contract=plan.result_contract,
        workflow_steps=steps_out,
    )


def _assign_explicit_urls(reqs: list[ResourceRequirement], text: str) -> dict[int, str]:
    urls = iter(extract_urls(text))
    positions: dict[int, str] = {}
    for i, req in enumerate(reqs):
        if req.kind == ResourceKind.EXPLICIT_URLS:
            url = next(urls, None)
            if url:
                positions[i] = url
    return positions


def _clarification_question(req: ResourceRequirement) -> str:
    if req.kind == ResourceKind.OPEN_TABS:
        desc = req.description or "the pages you mean"
        return (
            f"I understand that you want me to work with {desc}, but I don't have any "
            "matching pages open or saved. Open those pages in the persistent browser, "
            "paste their URLs, or tell me where to find them."
        )
    if req.kind == ResourceKind.EXPLICIT_URLS:
        return "I couldn't find any URLs in your request. Please paste the URL(s) you want me to use."
    return f"I need more information to find {req.description or 'the resource you mean'}. Please provide a URL or more detail."


# ---- existing helpers (unchanged) ------------------------------------------------------

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
