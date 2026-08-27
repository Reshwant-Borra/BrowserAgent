"""Semantic planner control-plane tests: schema validity, resource resolution, plan ->
RouterDecision translation, clarification (NeedsInput), routing-mode selection, and the
bounded replan mechanism. Mirrors tests/unit/test_router.py's `_FakeModelClient` pattern —
the model is always faked (schema-title-routed), so this is the deterministic-code-under-
model-output-shapes slice; live-model accuracy is covered separately by
benchmarks/run_semantic_planner_live.py.
"""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from pydantic import ValidationError

from agent.config import AppConfig, BrowserConfig
from browser.tabs import TabCandidate, list_open_tabs
from inference.llama_client import CompletionResult
from router.plan_schema import (
    ExecutionShape,
    PlanIntent,
    PlannedStep,
    ReplanDecision,
    ReplanDecisionKind,
    ResourceKind,
    ResourceRequirement,
    TaskPlan,
)
from router.policy import NeedsInput, RoutingError, _translate_plan, route, route_with_answer
from router.replanner import ReplanOutputError, decide_replan, finding_source_url
from router.resources import ResourceResolutionError, select_relevant_tabs
from router.schema import RouterDecision, TaskType
from router.semantic_planner import PlannerOutputError, plan_task

class _FakeMultiSchemaClient:
    """Routes canned responses by the requested schema's `title`, so one fake can stand in
    for a full route() call that may hit the planner, the resource-selection sub-call, and
    (in hybrid fallback) the legacy router in sequence."""

    def __init__(self, responses: dict[str, dict | list]):
        self.responses = responses
        self.endpoint = "fake://planner"
        self.calls: list[str] = []

    async def complete(self, prompt, grammar=None, max_tokens=256, json_schema=None) -> CompletionResult:
        title = (json_schema or {}).get("title", "")
        self.calls.append(title)
        if title not in self.responses:
            raise AssertionError(f"unexpected schema requested: {title!r} (prompt: {prompt[:80]!r})")
        return CompletionResult(text=json.dumps(self.responses[title]), total_latency_ms=1.0)

    async def health_check(self) -> bool:
        return True


def _config(mode: str = "hybrid", browser_mode: str = "launch", cdp_endpoint: str = "http://127.0.0.1:9222") -> AppConfig:
    config = AppConfig(browser=BrowserConfig(mode=browser_mode, cdp_endpoint=cdp_endpoint))
    config.routing.mode = mode
    return config


# ---- schema validity -------------------------------------------------------------------

def test_task_plan_schema_validates_minimal():
    plan = TaskPlan(goal="check the page", intent=PlanIntent.READ, execution_shape=ExecutionShape.SINGLE)
    assert plan.resource_requirements == []
    assert plan.constraints.read_only is True


def test_task_plan_rejects_invalid_intent():
    with pytest.raises(ValidationError):
        TaskPlan(goal="x", intent="not_a_real_intent", execution_shape=ExecutionShape.SINGLE)


def test_task_plan_result_contract_falls_back_to_generic():
    plan = TaskPlan(goal="x", intent=PlanIntent.READ, execution_shape=ExecutionShape.SINGLE, result_contract="nonsense")
    assert plan.result_contract == "generic"


def test_planned_step_requires_positive_ordinal():
    with pytest.raises(ValidationError):
        PlannedStep(ordinal=0, resource_ref=0, objective="x")


# ---- plan_task() ------------------------------------------------------------------------

async def test_plan_task_validates_schema():
    client = _FakeMultiSchemaClient({
        "TaskPlan": {
            "goal": "check the page", "intent": "read", "execution_shape": "single",
            "resource_requirements": [], "steps": [], "constraints": {"read_only": True},
            "result_contract": "generic",
        },
    })
    plan = await plan_task(client, "tell me what's on this page")
    assert plan.execution_shape == ExecutionShape.SINGLE
    assert plan.intent == PlanIntent.READ


async def test_plan_task_rejects_invalid_json():
    class _BadClient:
        endpoint = "fake://bad"
        async def complete(self, prompt, grammar=None, max_tokens=256, json_schema=None):
            return CompletionResult(text="not json at all", total_latency_ms=1.0)
        async def health_check(self):
            return True

    with pytest.raises(PlannerOutputError):
        await plan_task(_BadClient(), "do something")


async def test_plan_task_rejects_schema_violation():
    client = _FakeMultiSchemaClient({"TaskPlan": {"goal": "x"}})  # missing required fields
    with pytest.raises(PlannerOutputError):
        await plan_task(client, "do something")


# ---- tab selection (router/resources.py) -------------------------------------------------

async def test_select_relevant_tabs_drops_hallucinated_ids():
    tabs = [TabCandidate(id=1, title="AP Chem - Canvas", url="https://canvas.example/chem"),
            TabCandidate(id=2, title="YouTube", url="https://youtube.com/watch")]
    client = _FakeMultiSchemaClient({"ResourceSelection": {"selected_ids": [1, 99]}})
    selected = await select_relevant_tabs(client, "course pages", tabs)
    assert [t.id for t in selected] == [1]  # id 99 doesn't exist -> dropped, never trusted


async def test_select_relevant_tabs_empty_candidates_short_circuits():
    client = _FakeMultiSchemaClient({})  # would raise if called
    assert await select_relevant_tabs(client, "anything", []) == []


async def test_select_relevant_tabs_rejects_invalid_output():
    tabs = [TabCandidate(id=1, title="x", url="https://x.test")]
    client = _FakeMultiSchemaClient({"ResourceSelection": {"selected_ids": "not-a-list"}})
    with pytest.raises(ResourceResolutionError):
        await select_relevant_tabs(client, "x", tabs)


# ---- list_open_tabs (browser/tabs.py) ----------------------------------------------------

async def test_list_open_tabs_filters_non_page_and_blank():
    targets = [
        {"type": "page", "url": "https://a.test/course", "title": "Course A"},
        {"type": "page", "url": "about:blank", "title": ""},
        {"type": "background_page", "url": "chrome-extension://x/bg.html", "title": "ext"},
        {"type": "page", "url": "chrome://intro/", "title": "intro"},
        {"type": "page", "url": "https://b.test/course", "title": "Course B"},
    ]

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=targets)

    tabs = await list_open_tabs("http://127.0.0.1:9222", transport=httpx.MockTransport(handler))
    assert [t.url for t in tabs] == ["https://a.test/course", "https://b.test/course"]
    assert [t.id for t in tabs] == [1, 2]  # deterministic sequential ids


async def test_list_open_tabs_unavailable_endpoint_raises():
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    from browser.tabs import TabListUnavailable
    with pytest.raises(TabListUnavailable):
        await list_open_tabs("http://127.0.0.1:1", transport=httpx.MockTransport(handler))


# ---- plan -> RouterDecision translation --------------------------------------------------

async def test_translate_single_current_page_yields_empty_targets():
    plan = TaskPlan(goal="what's on this page", intent=PlanIntent.READ, execution_shape=ExecutionShape.SINGLE,
                     resource_requirements=[ResourceRequirement(kind=ResourceKind.CURRENT_PAGE)])
    result = await _translate_plan("tell me what this page is about", plan, _FakeMultiSchemaClient({}), _config())
    assert isinstance(result, RouterDecision)
    assert result.task_type == TaskType.SINGLE_SITE
    assert result.targets == []


async def test_translate_single_no_requirements_yields_empty_targets():
    plan = TaskPlan(goal="what's on this page", intent=PlanIntent.READ, execution_shape=ExecutionShape.SINGLE)
    result = await _translate_plan("tell me about this", plan, _FakeMultiSchemaClient({}), _config())
    assert result.task_type == TaskType.SINGLE_SITE
    assert result.targets == []


async def test_translate_single_explicit_url_resolves_from_text():
    plan = TaskPlan(goal="check the page", intent=PlanIntent.READ, execution_shape=ExecutionShape.SINGLE,
                     resource_requirements=[ResourceRequirement(kind=ResourceKind.EXPLICIT_URLS)])
    text = "check http://a.test and tell me what's new (mentioned above)"
    result = await _translate_plan(text, plan, _FakeMultiSchemaClient({}), _config())
    assert result.task_type == TaskType.SINGLE_SITE
    assert result.targets == ["http://a.test"]


async def test_translate_sweep_open_tabs_resolves_via_selection():
    plan = TaskPlan(
        goal="check my course pages", intent=PlanIntent.READ, execution_shape=ExecutionShape.SWEEP,
        resource_requirements=[ResourceRequirement(kind=ResourceKind.OPEN_TABS, description="the user's course pages")],
    )
    tab_targets = [
        {"type": "page", "url": "https://canvas.example/chem", "title": "AP Chem"},
        {"type": "page", "url": "https://youtube.com/watch", "title": "YouTube"},
        {"type": "page", "url": "https://canvas.example/math", "title": "AP Calc"},
    ]

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=tab_targets)

    config = _config(browser_mode="cdp_attach", cdp_endpoint="http://127.0.0.1:9222")
    client = _FakeMultiSchemaClient({"ResourceSelection": {"selected_ids": [1, 3]}})

    import router.resources as resources_mod
    original = resources_mod.list_open_tabs

    async def patched(endpoint, timeout_s=3.0, transport=None):
        return await original(endpoint, timeout_s=timeout_s, transport=httpx.MockTransport(handler))
    resources_mod.list_open_tabs = patched
    try:
        result = await _translate_plan("check my course pages", plan, client, config)
    finally:
        resources_mod.list_open_tabs = original

    assert isinstance(result, RouterDecision)
    assert result.task_type == TaskType.MULTISITE_SWEEP
    assert set(result.targets) == {"https://canvas.example/chem", "https://canvas.example/math"}
    # Regression: resolved open tabs must preserve canonical tab identity (id/title), not
    # collapse to bare URLs — see docs/BROWSERAGENT_MASTER_STATUS.md's open-tab sweep finding.
    by_url = {tr.url: tr for tr in result.target_resources}
    assert set(by_url) == {"https://canvas.example/chem", "https://canvas.example/math"}
    assert all(tr.kind.value == "open_tab" for tr in by_url.values())
    assert by_url["https://canvas.example/chem"].tab_id == 1
    assert by_url["https://canvas.example/chem"].title == "AP Chem"
    assert by_url["https://canvas.example/math"].tab_id == 3
    assert by_url["https://canvas.example/math"].title == "AP Calc"


async def test_translate_sweep_no_matching_tabs_yields_needs_input():
    plan = TaskPlan(
        goal="check my course pages", intent=PlanIntent.READ, execution_shape=ExecutionShape.SWEEP,
        resource_requirements=[ResourceRequirement(kind=ResourceKind.OPEN_TABS, description="the user's course pages")],
    )
    config = _config(browser_mode="launch")  # no cdp_attach -> no tabs ever available
    client = _FakeMultiSchemaClient({})
    result = await _translate_plan("check my course pages", plan, client, config)
    assert isinstance(result, NeedsInput)
    assert "course pages" in result.question


async def test_translate_workflow_two_explicit_url_steps_assigned_positionally():
    """Two explicit_urls requirements must NOT both resolve to the full URL list found in
    the text (which would silently give every step the same target) — each gets the URL at
    its position among explicit_urls requirements, left to right, matching how a person
    reads "site A ... then ... site B"."""
    plan = TaskPlan(
        goal="find the code then enter it", intent=PlanIntent.ACT, execution_shape=ExecutionShape.ORDERED_WORKFLOW,
        resource_requirements=[
            ResourceRequirement(kind=ResourceKind.EXPLICIT_URLS, description="site with the code"),
            ResourceRequirement(kind=ResourceKind.EXPLICIT_URLS, description="site with the field"),
        ],
        steps=[
            PlannedStep(ordinal=1, resource_ref=0, objective="find the project code"),
            PlannedStep(ordinal=2, resource_ref=1, objective="enter the code"),
        ],
    )
    text = "Find the code on http://a.test then enter it on http://b.test"
    result = await _translate_plan(text, plan, _FakeMultiSchemaClient({}), _config())
    assert isinstance(result, RouterDecision)
    assert result.task_type == TaskType.ORDERED_WORKFLOW
    assert [s.target for s in result.workflow_steps] == ["http://a.test", "http://b.test"]


async def test_translate_workflow_missing_second_url_yields_needs_input():
    plan = TaskPlan(
        goal="find the code then enter it", intent=PlanIntent.ACT, execution_shape=ExecutionShape.ORDERED_WORKFLOW,
        resource_requirements=[
            ResourceRequirement(kind=ResourceKind.EXPLICIT_URLS, description="site with the code"),
            ResourceRequirement(kind=ResourceKind.EXPLICIT_URLS, description="site with the field"),
        ],
        steps=[
            PlannedStep(ordinal=1, resource_ref=0, objective="find the project code"),
            PlannedStep(ordinal=2, resource_ref=1, objective="enter the code"),
        ],
    )
    # Only one URL in the text -> the second requirement has nothing to claim -> NeedsInput,
    # never silently reusing the first URL for both steps.
    result = await _translate_plan("find the code on http://a.test", plan, _FakeMultiSchemaClient({}), _config())
    assert isinstance(result, NeedsInput)


async def test_translate_workflow_single_shared_explicit_url_requirement():
    """A one-URL-per-requirement contract still needs a text with a distinct URL per
    requirement to resolve cleanly; using the same EXPLICIT_URLS kind twice against text with
    exactly one URL always leaves the second step unresolved by design (see test above) — so
    this test instead confirms the ordinal-preserving happy path using a resource_ref that
    only ever needs ONE resolvable requirement (single-step workflow)."""
    plan = TaskPlan(
        goal="find the code", intent=PlanIntent.ACT, execution_shape=ExecutionShape.ORDERED_WORKFLOW,
        resource_requirements=[ResourceRequirement(kind=ResourceKind.EXPLICIT_URLS)],
        steps=[PlannedStep(ordinal=1, resource_ref=0, objective="find the project code")],
    )
    result = await _translate_plan("find the code on http://a.test", plan, _FakeMultiSchemaClient({}), _config())
    assert isinstance(result, RouterDecision)
    assert result.task_type == TaskType.ORDERED_WORKFLOW
    assert len(result.workflow_steps) == 1
    assert result.workflow_steps[0].target == "http://a.test"
    assert result.workflow_steps[0].ordinal == 1


async def test_translate_open_research_sets_requires_discovery():
    plan = TaskPlan(goal="research essay advice", intent=PlanIntent.RESEARCH, execution_shape=ExecutionShape.OPEN_RESEARCH,
                     resource_requirements=[ResourceRequirement(kind=ResourceKind.WEB_DISCOVERY, description="essay advice")])
    result = await _translate_plan("research essay advice", plan, _FakeMultiSchemaClient({}), _config())
    assert result.task_type == TaskType.RESEARCH
    assert result.requires_discovery is True
    assert result.targets == []


async def test_mixed_intent_flag_only_set_for_mixed_sweep():
    read_plan = TaskPlan(goal="x", intent=PlanIntent.READ, execution_shape=ExecutionShape.SWEEP,
                          resource_requirements=[ResourceRequirement(kind=ResourceKind.EXPLICIT_URLS)])
    mixed_plan = read_plan.model_copy(update={"intent": PlanIntent.MIXED})
    text = "check http://a.test and http://b.test"
    r1 = await _translate_plan(text, read_plan, _FakeMultiSchemaClient({}), _config())
    r2 = await _translate_plan(text, mixed_plan, _FakeMultiSchemaClient({}), _config())
    assert r1.mixed_intent_followup is False
    assert r2.mixed_intent_followup is True


# ---- routing-mode selection (route()) -----------------------------------------------------

async def test_hybrid_mode_falls_back_to_legacy_on_planner_error():
    client = _FakeMultiSchemaClient({
        # planner call fails schema validation (missing execution_shape)...
        "TaskPlan": {"goal": "x", "intent": "read"},
        # ...so hybrid mode falls back to the legacy router, which must succeed:
        "RouterDecision": {
            "task_type": "single_site", "objective": "check the page", "targets": [],
            "requires_discovery": False, "preferred_policy": "read_only", "result_contract": "generic",
        },
    })
    result = await route("do something ambiguous about the page", client, _config(mode="hybrid"))
    assert isinstance(result, RouterDecision)
    assert client.calls == ["TaskPlan", "RouterDecision"]


async def test_semantic_mode_raises_on_planner_error_without_fallback():
    client = _FakeMultiSchemaClient({"TaskPlan": {"goal": "x", "intent": "read"}})
    with pytest.raises(RoutingError):
        await route("do something ambiguous", client, _config(mode="semantic"))


async def test_legacy_mode_never_calls_planner():
    client = _FakeMultiSchemaClient({
        "RouterDecision": {
            "task_type": "single_site", "objective": "x", "targets": [],
            "requires_discovery": False, "preferred_policy": "read_only", "result_contract": "generic",
        },
    })
    result = await route("do something ambiguous", client, _config(mode="legacy"))
    assert isinstance(result, RouterDecision)
    assert client.calls == ["RouterDecision"]


async def test_no_config_defaults_to_legacy_behavior():
    """Callers that don't pass config (e.g. the assignment-sweep benchmark) get exactly the
    pre-existing behavior — passing config is what opts a caller into the planner."""
    client = _FakeMultiSchemaClient({
        "RouterDecision": {
            "task_type": "single_site", "objective": "x", "targets": [],
            "requires_discovery": False, "preferred_policy": "read_only", "result_contract": "generic",
        },
    })
    result = await route("do something ambiguous", client)  # no config at all
    assert isinstance(result, RouterDecision)
    assert client.calls == ["RouterDecision"]


async def test_deterministic_fast_path_skips_planner_entirely():
    client = _FakeMultiSchemaClient({})  # would raise if the planner were ever called
    result = await route("Open https://example.com", client, _config(mode="hybrid"))
    assert result.task_type == TaskType.SINGLE_SITE
    assert client.calls == []


# ---- clarification round-trip --------------------------------------------------------------

async def test_route_with_answer_reroutes_with_combined_text():
    client = _FakeMultiSchemaClient({})
    result = await route_with_answer(
        "check my course pages", "https://canvas.example/course/1", client, _config(mode="hybrid"),
    )
    # the combined text now contains a literal URL -> deterministic fast path wins, planner
    # never called at all, proving the "no targets found" failure mode is fully closed.
    assert isinstance(result, RouterDecision)
    assert result.targets == ["https://canvas.example/course/1"]
    assert client.calls == []


# ---- replanner ------------------------------------------------------------------------------

async def test_decide_replan_schema_validates():
    client = _FakeMultiSchemaClient({
        "ReplanDecision": {"decision": "revise", "finding_ref": 0, "new_objective": "open it"},
    })
    findings = [{"finding": {"source_url": "https://a.test/assign/1", "title": "Essay"}}]
    decision = await decide_replan(client, "find nearest deadline and open it", findings)
    assert decision.decision == ReplanDecisionKind.REVISE
    assert decision.finding_ref == 0


async def test_decide_replan_rejects_invalid_json():
    class _BadClient:
        endpoint = "fake://bad"
        async def complete(self, prompt, grammar=None, max_tokens=256, json_schema=None):
            return CompletionResult(text="nope", total_latency_ms=1.0)
        async def health_check(self):
            return True
    with pytest.raises(ReplanOutputError):
        await decide_replan(_BadClient(), "x", [])


def test_finding_source_url_bounds_checked():
    findings = [{"finding": {"source_url": "https://a.test"}}]
    assert finding_source_url(findings, 0) == "https://a.test"
    assert finding_source_url(findings, 5) is None
    assert finding_source_url(findings, None) is None
