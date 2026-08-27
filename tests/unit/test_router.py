"""Router accuracy benchmark (Section 64-65 of the Phase 5B spec): tuned + holdout prompts
covering single-site read, single-site action, multisite sweep, ordered workflow, research,
and deliberately ambiguous prompts. The deterministic path (`try_deterministic_route`) is
exercised directly, with no model involved — this is the >=95%-accuracy-before-model-fallback
slice; genuinely ambiguous prompts are asserted to correctly defer (return None) rather than
guess, since guessing wrong deterministically would be worse than falling back to Qwen.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from inference.llama_client import CompletionResult
from router.extract import extract_urls, try_deterministic_route
from router.llm_router import route_with_model
from router.schema import SafetyPolicy, TaskType

# ---- tuned set -----------------------------------------------------------

SINGLE_SITE_READ = [
    "Open https://example.com and tell me what this page is about.",
    "Check https://school.example.edu/course/101 for anything due soon.",
    "Go to https://news.example.com and summarize the top story.",
    "Find the assignment on https://canvas.example.edu/course/55.",
    "Look at https://shop.example.com and tell me if it's in stock.",
    "Tell me what's on https://blog.example.com/latest.",
    "Read https://docs.example.com/api and summarize the endpoints.",
    "What is on https://example.org/about?",
]

SINGLE_SITE_ACTION = [
    "Go to https://app.example.com/settings and change the theme to dark.",
    "On https://portal.example.com, toggle notifications on.",
    "Set the display mode to compact on https://example.com/preferences.",
    "Enable weekly emails on https://example.com/account.",
]

MULTISITE_SWEEP = [
    "Check these URLs and tell me which assignments I still have to do: "
    "https://a.example.edu https://b.example.edu https://c.example.edu",
    "Check all these company pages for whether they offer student internships: "
    "https://a.example.com https://b.example.com",
    "Go to these three sites and find whether I have anything due this week: "
    "https://x.example.edu https://y.example.edu https://z.example.edu",
    "Check http://one.test and http://two.test and http://three.test and http://four.test for pricing.",
]

ORDERED_WORKFLOW = [
    "Go to https://a.example.com and set the display mode to Compact. "
    "Then go to https://b.example.com and enable the weekly summary.",
    "First change the theme on https://a.example.com, then toggle alerts on https://b.example.com, "
    "finally verify status on https://c.example.com.",
    "Step 1: enable notifications on https://a.example.com. "
    "Step 2: update the profile setting on https://b.example.com.",
    "On https://a.example.com change the mode to dark. After that, on https://b.example.com turn on SMS alerts.",
]

RESEARCH = [
    "Research college essay advice from a large number of good sources and give me a detailed evidence-backed report.",
    "Research this topic across the web and produce an evidence-backed report.",
    "Investigate best practices for remote onboarding across multiple sources and summarize.",
    "Compare advice from several sources about interview preparation.",
]

AMBIGUOUS = [
    "Do the thing.",
    "Handle my school stuff.",
    "Check my sites.",
    "Help me with this.",
]

# ---- holdout set (unseen phrasing, Section 65) ----------------------------

HOLDOUT_SINGLE_READ = [
    "Please review https://holdout1.example.com and let me know the gist.",
    "I'd like to know what's happening on https://holdout2.example.org today.",
]
HOLDOUT_SINGLE_ACTION = [
    "Could you switch the layout to compact over at https://holdout3.example.com/settings?",
]
HOLDOUT_SWEEP = [
    "Take a look at https://p1.test, https://p2.test, and https://p3.test and report anything notable.",
]
HOLDOUT_WORKFLOW = [
    "Visit https://s1.test and flip dark mode on, and once that's done go turn on alerts at https://s2.test.",
]
HOLDOUT_RESEARCH = [
    "Pull together evidence-backed findings on productivity techniques from a wide range of sources.",
]
HOLDOUT_AMBIGUOUS = [
    "Take care of it.",
]


def _assert_route(prompt: str, expected: TaskType) -> None:
    decision = try_deterministic_route(prompt)
    assert decision is not None, f"expected a deterministic route for: {prompt!r}"
    assert decision.task_type == expected, f"{prompt!r} routed as {decision.task_type}, expected {expected}"


@pytest.mark.parametrize("prompt", SINGLE_SITE_READ + HOLDOUT_SINGLE_READ)
def test_routes_single_site_read(prompt):
    _assert_route(prompt, TaskType.SINGLE_SITE)


@pytest.mark.parametrize("prompt", SINGLE_SITE_ACTION + HOLDOUT_SINGLE_ACTION)
def test_routes_single_site_action_as_reversible(prompt):
    decision = try_deterministic_route(prompt)
    assert decision is not None
    assert decision.task_type == TaskType.SINGLE_SITE
    assert decision.preferred_policy == SafetyPolicy.REVERSIBLE_ACTIONS


@pytest.mark.parametrize("prompt", MULTISITE_SWEEP + HOLDOUT_SWEEP)
def test_routes_multisite_sweep(prompt):
    _assert_route(prompt, TaskType.MULTISITE_SWEEP)


@pytest.mark.parametrize("prompt", ORDERED_WORKFLOW + HOLDOUT_WORKFLOW)
def test_routes_ordered_workflow(prompt):
    decision = try_deterministic_route(prompt)
    assert decision is not None
    assert decision.task_type == TaskType.ORDERED_WORKFLOW
    assert len(decision.workflow_steps) == len(extract_urls(prompt))
    ordinals = [s.ordinal for s in decision.workflow_steps]
    assert ordinals == sorted(ordinals)  # Section 16: explicit, persisted order


@pytest.mark.parametrize("prompt", RESEARCH + HOLDOUT_RESEARCH)
def test_routes_research(prompt):
    decision = try_deterministic_route(prompt)
    assert decision is not None
    assert decision.task_type == TaskType.RESEARCH
    assert decision.requires_discovery is True
    assert decision.targets == []


@pytest.mark.parametrize("prompt", AMBIGUOUS + HOLDOUT_AMBIGUOUS)
def test_ambiguous_prompts_defer_to_model(prompt):
    """A genuinely ambiguous prompt must not be force-classified deterministically —
    Section 10 says only obvious shapes skip the model."""
    assert try_deterministic_route(prompt) is None


def test_router_never_invents_targets_beyond_the_text():
    prompt = "Check http://a.test and http://b.test for pricing"
    decision = try_deterministic_route(prompt)
    assert set(decision.targets) == {"http://a.test", "http://b.test"}


def test_router_accuracy_summary():
    """Aggregate accuracy gate for the deterministic-obvious slice: Section 64 targets
    >=95% on tuned+holdout; this slice (unambiguous-by-construction prompts) should be 100%,
    with the ambiguous prompts correctly deferring rather than being scored as routed."""
    cases = (
        [(p, TaskType.SINGLE_SITE) for p in SINGLE_SITE_READ + SINGLE_SITE_ACTION + HOLDOUT_SINGLE_READ + HOLDOUT_SINGLE_ACTION]
        + [(p, TaskType.MULTISITE_SWEEP) for p in MULTISITE_SWEEP + HOLDOUT_SWEEP]
        + [(p, TaskType.ORDERED_WORKFLOW) for p in ORDERED_WORKFLOW + HOLDOUT_WORKFLOW]
        + [(p, TaskType.RESEARCH) for p in RESEARCH + HOLDOUT_RESEARCH]
    )
    def _matches(prompt: str, expected: TaskType) -> bool:
        decision = try_deterministic_route(prompt)
        return decision is not None and decision.task_type == expected

    correct = sum(1 for prompt, expected in cases if _matches(prompt, expected))
    accuracy = correct / len(cases)
    assert accuracy >= 0.95, f"router accuracy {accuracy:.2%} below 95% gate ({correct}/{len(cases)})"


# ---- Qwen fallback path (Section 11-12): schema validation + anti-hallucination guard ----

class _FakeModelClient:
    def __init__(self, response: dict):
        self.response = response
        self.endpoint = "fake://router"

    async def complete(self, prompt, grammar=None, max_tokens=256, json_schema=None) -> CompletionResult:
        return CompletionResult(text=json.dumps(self.response), total_latency_ms=1.0)

    async def health_check(self) -> bool:
        return True


def test_model_fallback_validates_against_schema():
    client = _FakeModelClient({
        "task_type": "single_site", "objective": "check the page", "targets": ["http://a.test"],
        "requires_discovery": False, "preferred_policy": "read_only", "result_contract": "generic",
    })
    decision = asyncio.run(route_with_model(client, "check http://a.test somehow"))
    assert decision.task_type == TaskType.SINGLE_SITE
    assert decision.targets == ["http://a.test"]


# ---- router bug fixes (Findings 7 & 8 from real-UI testing) --------------

def test_ordered_workflow_steps_have_isolated_objectives():
    """Finding 7: the deterministic router used to assign the *entire* raw prompt as every
    step's objective, so step 1 (site A) would also be told to do step 2's (site B's) work.
    Each step's objective must mention only its own instruction."""
    prompt = (
        "Go to https://a.test and set the display mode to Compact. "
        "Then go to https://b.test and enable the weekly summary."
    )
    decision = try_deterministic_route(prompt)
    assert decision is not None
    assert decision.task_type == TaskType.ORDERED_WORKFLOW
    step_a, step_b = decision.workflow_steps
    assert "compact" in step_a.objective.lower()
    assert "weekly summary" not in step_a.objective.lower()
    assert "weekly summary" in step_b.objective.lower()
    assert "compact" not in step_b.objective.lower()


def test_ordered_workflow_steps_isolated_when_action_precedes_target():
    """Same isolation guarantee, but for the "change X on <url>" phrasing where the verb
    comes before the target instead of after it."""
    prompt = (
        "First change the theme on https://a.test, then toggle alerts on https://b.test, "
        "finally verify status on https://c.test."
    )
    decision = try_deterministic_route(prompt)
    assert decision is not None
    step_a, step_b, step_c = decision.workflow_steps
    assert "theme" in step_a.objective.lower() and "alerts" not in step_a.objective.lower()
    assert "alerts" in step_b.objective.lower() and "theme" not in step_b.objective.lower()
    assert "status" in step_c.objective.lower() and "alerts" not in step_c.objective.lower()


def test_ordered_workflow_cross_site_fact_pass_routes_and_isolates():
    """Finding 8: 'enter' was missing from the action-verb set, so this fell through to
    multisite_sweep instead of ordered_workflow. It must also isolate step objectives."""
    prompt = "Find the code on https://a.test, then enter it on https://b.test."
    decision = try_deterministic_route(prompt)
    assert decision is not None
    assert decision.task_type == TaskType.ORDERED_WORKFLOW
    step_a, step_b = decision.workflow_steps
    assert "find" in step_a.objective.lower()
    assert "enter" in step_b.objective.lower()


def test_ordered_workflow_type_then_select_routes_correctly():
    prompt = "Type the username on https://a.test then select the role on https://b.test."
    decision = try_deterministic_route(prompt)
    assert decision is not None
    assert decision.task_type == TaskType.ORDERED_WORKFLOW
    assert len(decision.workflow_steps) == 2


def test_check_multiple_sites_for_assignments_is_sweep_not_workflow():
    """'check' + 'and' alone (no sequence marker, no action verb) must stay a sweep."""
    prompt = "Check https://a.test and https://b.test for assignments."
    decision = try_deterministic_route(prompt)
    assert decision is not None
    assert decision.task_type == TaskType.MULTISITE_SWEEP


def test_research_multiple_sites_is_not_action_workflow():
    """Broadening the action-verb set must not turn a research-style multi-URL prompt into
    an ordered_workflow just because it mentions multiple targets."""
    prompt = "Research https://a.test and https://b.test and summarize what they say."
    decision = try_deterministic_route(prompt)
    assert decision is not None
    assert decision.task_type in (TaskType.MULTISITE_SWEEP, TaskType.RESEARCH)


def test_model_fallback_drops_hallucinated_targets():
    """Section 12: the router must never invent extra websites/targets."""
    client = _FakeModelClient({
        "task_type": "multisite_sweep", "objective": "check pages",
        "targets": ["http://a.test", "http://evil-not-in-text.test"],
        "requires_discovery": False, "preferred_policy": "read_only", "result_contract": "generic",
    })
    decision = asyncio.run(route_with_model(client, "check http://a.test for anything relevant somehow"))
    assert decision.targets == ["http://a.test"]
