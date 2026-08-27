"""Semantic planner reliability benchmark (Section 24-25 of the semantic planner task):
tuned + holdout prompts across every required category, run against the real Qwen3-8B/Ollama
endpoint — this is the live-model accuracy slice, not a fake-client unit test (that's
tests/unit/test_semantic_planner.py).

Two grading strategies depending on category:
- Plan-shape categories (single_explicit, current_page, sweep_explicit, ordered_workflow,
  mixed_intent, research): call `router.semantic_planner.plan_task()` directly and grade the
  raw TaskPlan's intent/execution_shape/resource kinds — this is "did the model understand
  what was asked," independent of resource resolution.
- Resolution categories (open_tabs, clarification): call the full `router.policy.route()`
  pipeline in "semantic" mode with a synthetic open-tab pool patched into
  `router.resources.list_open_tabs` (no real browser needed) — open_tabs prompts are graded
  against a pool that DOES contain matching tabs (expect a resolved RouterDecision whose
  targets are a subset of the synthetic pool — the hallucination check), clarification
  prompts against a pool that does NOT (expect NeedsInput, never a hard failure).

Usage:
    python benchmarks/run_semantic_planner_live.py [--output-dir DIR] [--categories a,b,c]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path
from typing import Any, Callable

import httpx

import router.resources as resources_mod
from agent.config import AppConfig, BrowserConfig, load_config
from browser.tabs import list_open_tabs as real_list_open_tabs
from inference.llama_client import create_inference_client
from router.plan_schema import ExecutionShape, PlanIntent, ResourceKind, TaskPlan
from router.policy import NeedsInput, RoutingError, route
from router.schema import RouterDecision
from router.semantic_planner import PlannerOutputError, plan_task

ROOT = Path(__file__).resolve().parent.parent

# ---- synthetic open-tab pools (no real browser needed to grade resource resolution) -------

_COURSE_TABS = [
    {"type": "page", "url": "https://canvas.example.edu/courses/101", "title": "AP Chemistry - Canvas"},
    {"type": "page", "url": "https://canvas.example.edu/courses/205", "title": "AP Calculus - Canvas"},
    {"type": "page", "url": "https://canvas.example.edu/courses/310", "title": "US History - Canvas"},
]
_DISTRACTOR_TABS = [
    {"type": "page", "url": "https://youtube.com/watch?v=abc123", "title": "Funny cat video - YouTube"},
    {"type": "page", "url": "https://mail.example.com/inbox", "title": "Inbox - Personal Email"},
    {"type": "page", "url": "https://shop.example.com/cart", "title": "Shopping Cart"},
]
_POOL_WITH_COURSES = _COURSE_TABS + _DISTRACTOR_TABS
_POOL_WITHOUT_COURSES = _DISTRACTOR_TABS

_active_pool: list[dict[str, Any]] = []


async def _patched_list_open_tabs(cdp_endpoint: str, timeout_s: float = 3.0, transport=None):
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_active_pool)
    return await real_list_open_tabs(cdp_endpoint, timeout_s=timeout_s, transport=httpx.MockTransport(handler))


# ---- prompt sets ---------------------------------------------------------------------------

SINGLE_EXPLICIT = [
    "Open https://school.example.edu/course/101 and tell me what's due.",
    "Check https://news.example.com for today's top story.",
    "Go to https://shop.example.com/cart and tell me what's in it.",
    "Read https://docs.example.com/api and summarize it.",
    "On https://app.example.com/settings, turn on dark mode.",
    "Set the notification preference to weekly on https://example.com/account.",
    "Visit https://blog.example.com/latest and tell me what it's about.",
    "What's on https://example.org/about?",
    "Please check https://portal.example.edu/dashboard for anything new.",
    "Look at https://example.com/pricing and tell me the cost.",
    "Enable two-factor authentication on https://secure.example.com/settings.",
    "Find the deadline listed on https://canvas.example.edu/assignments/55.",
    "Tell me what's on https://example.com/faq.",
]
SINGLE_EXPLICIT_HOLDOUT = [
    "I'd like a quick summary of https://holdout-single.example.com/page.",
    "Could you switch the theme to dark over at https://holdout2.example.com/prefs?",
    "Peek at https://holdout3.example.com and let me know if anything's new.",
]

CURRENT_PAGE = [
    "Tell me what I still need to do on this page.",
    "What is this page about?",
    "Summarize what's on the page I'm currently looking at.",
    "Is there anything due according to this page?",
    "Check this page for anything I'm missing.",
    "What does this current page say about pricing?",
    "Read the page I have open right now and summarize it.",
    "Tell me what this page is about.",
    "Does this page mention a deadline?",
    "Summarize the content currently displayed in my browser.",
    "What's the main point of the page I'm on?",
    "Look at what's currently open and tell me if there's anything urgent.",
    "Check the page in front of me for next steps.",
]
CURRENT_PAGE_HOLDOUT = [
    "What should I take away from what's currently on my screen?",
    "Give me the gist of the page I've got open.",
    "Anything I'm missing on the page I'm looking at right now?",
]

SWEEP_EXPLICIT = [
    "Check https://a.example.edu and https://b.example.edu and tell me what's due.",
    "Go through https://x.example.com, https://y.example.com, and https://z.example.com for pricing info.",
    "Check these pages for internship listings: https://a.example.com https://b.example.com https://c.example.com",
    "Look at https://p1.test, https://p2.test, and https://p3.test and report anything notable.",
    "Visit https://one.test and https://two.test and tell me which have discounts.",
    "Check http://a.test, http://b.test, http://c.test, and http://d.test for open positions.",
    "Go over https://course1.example.edu and https://course2.example.edu for anything due this week.",
    "Review https://shop1.example.com and https://shop2.example.com for the best price.",
    "Check https://siteA.example.com and https://siteB.example.com and tell me what's changed.",
    "Look through https://news1.example.com and https://news2.example.com for headlines about the election.",
    "Check https://a.example.org and https://b.example.org for upcoming events.",
    "Go to https://library1.example.edu and https://library2.example.edu and tell me which books are available.",
    "Check these three: https://r1.test, https://r2.test, https://r3.test for reviews.",
]
SWEEP_EXPLICIT_HOLDOUT = [
    "Take a look through https://hs1.test, https://hs2.test, and https://hs3.test and flag anything notable.",
    "Sweep https://hold-a.example.com and https://hold-b.example.com for job postings.",
    "Compare https://hprice1.test and https://hprice2.test on cost.",
]

OPEN_TABS = [
    "Check all my course pages and tell me what I still need to do this week.",
    "Look through everything I currently have open for school and tell me what's due.",
    "Go through the tabs I've got open for class and tell me what's outstanding.",
    "Check my open course tabs for anything I'm behind on.",
    "Look at what I have open for school right now and summarize what's pending.",
    "Use what I've already got open in the browser to figure out what needs attention this week.",
    "Can you go through whatever school stuff I already have open and figure out what's still pending before Friday?",
    "Check the class-related tabs I have open and tell me what's due soon.",
    "Look through my open course pages for assignments.",
    "Check whatever I have open right now that's related to my classes.",
    "Go through my open tabs for school and tell me what needs to get done.",
    "Look at the pages I've got up for class and consolidate anything unfinished.",
    "Check my currently-open course sites for pending work.",
]
OPEN_TABS_HOLDOUT = [
    "See what's still outstanding across the class tabs I already have open.",
    "Take a look through what I have open for coursework and tell me what's left.",
    "Check the school-related pages already open in my browser for anything due.",
]

ORDERED_WORKFLOW = [
    "Go to https://a.example.com and set the display mode to Compact. Then go to https://b.example.com and enable the weekly summary.",
    "First change the theme on https://a.example.com, then toggle alerts on https://b.example.com.",
    "On https://a.example.com change the mode to dark. After that, on https://b.example.com turn on SMS alerts.",
    "Find the project code on https://a.test, then enter it on https://b.test.",
    "Get the confirmation number from https://a.example.com and put it into the form at https://b.example.com.",
    "Look up the reference number on https://a.test, then type it into the field on https://b.test and verify it saved.",
    "On https://a.example.com find the invoice number. Then go to https://b.example.com and enter that invoice number in the matching field.",
    "Step 1: enable notifications on https://a.example.com. Step 2: update the profile setting on https://b.example.com.",
    "Visit https://s1.test and flip dark mode on, and once that's done go turn on alerts at https://s2.test.",
    "Grab the code shown on https://a.test and then paste it into the box at https://b.test, then confirm it was accepted.",
    "Change the setting on https://a.example.com first, and once that's confirmed, go update the matching setting on https://b.example.com.",
    "Read the account number from https://a.test then enter it on https://b.test and check it was saved correctly.",
    "On https://a.example.com toggle notifications on. Next, on https://b.example.com set the theme to dark.",
]
ORDERED_WORKFLOW_HOLDOUT = [
    "Find the code on the page at https://ha.test, then put that same code into the field at https://hb.test and verify it.",
    "Switch the setting at https://hc.test to on, and once that's done, switch the matching setting at https://hd.test.",
    "Get the number listed at https://he.test and enter it at https://hf.test, then confirm the result.",
]

MIXED_INTENT = [
    "Check all my course pages, find the assignment with the nearest deadline, and open it.",
    "Look through my open school tabs, find whichever assignment is due soonest, and open that one.",
    "Check https://a.example.edu and https://b.example.edu, find the cheapest listed price, and open that page.",
    "Go through my course pages and open the one with something due first.",
    "Check my open tabs for class, find the assignment due soonest, and pull it up.",
    "Look at https://x.test and https://y.test, find whichever has the lowest price, and open it.",
    "Check all my course pages and open whichever one has the most urgent deadline.",
    "Go through https://a.test, https://b.test, and https://c.test, find the one with an open position closest to me, and open that listing.",
    "Check my school tabs, figure out what's due first, and take me to it.",
    "Look through the open tabs for class, find the one with the earliest due date, and open it.",
    "Check https://p1.test and https://p2.test, pick the one with the best rating, and open it.",
    "Go through my course pages, find whatever's most urgent, and open it for me.",
    "Check my open tabs for school and pull up whichever assignment is due first.",
]
MIXED_INTENT_HOLDOUT = [
    "Look through my class tabs, find the assignment closest to its deadline, and open that page.",
    "Check https://hg.test and https://hh.test, find the lower price, and open that one.",
    "Go through my open school tabs and take me straight to whatever's due soonest.",
]

RESEARCH = [
    "Research college essay advice from a number of good sources and give me an evidence-backed report.",
    "Investigate best practices for remote onboarding across multiple sources and summarize.",
    "Compare advice from several sources about interview preparation.",
    "Look across the web for opinions on the best programming language for beginners and summarize the consensus.",
    "Research what nutritionists generally recommend for a balanced breakfast.",
    "Find out what multiple sources say about improving sleep quality and summarize the common themes.",
    "Research the pros and cons of remote work from a variety of sources.",
    "Investigate what experts say about saving for retirement early and summarize the key points.",
    "Pull together evidence-backed findings on productivity techniques from a wide range of sources.",
    "Research how other people have approached learning a new language efficiently.",
    "Look into what's generally recommended for a healthy morning routine, using multiple sources.",
    "Compare a range of opinions on whether remote internships are worthwhile.",
    "Research strong college essay advice from multiple reputable sources and summarize the recurring themes.",
]
RESEARCH_HOLDOUT = [
    "Gather evidence-backed findings on productivity habits from a broad set of sources.",
    "Look across several sources for consensus advice on public speaking.",
    "Research common advice about negotiating a salary, pulling from multiple sources.",
]

CLARIFICATION = [
    "Check all my course pages and tell me what I still need to do this week.",
    "Look through everything I currently have open for school and tell me what's due.",
    "Go through the tabs I've got open for class and tell me what's outstanding.",
    "Check my open course tabs for anything I'm behind on.",
    "Use what I've already got open in the browser for school to figure out what needs attention.",
    "Check the class-related tabs I have open and tell me what's due soon.",
    "Look through my open course pages for assignments.",
    "Check whatever I have open right now that's related to my classes.",
    "Go through my open tabs for school and tell me what needs to get done.",
    "Look at the pages I've got up for class and consolidate anything unfinished.",
    "Check my currently-open course sites for pending work.",
    "See what's still outstanding across the class tabs I already have open.",
    "Take a look through what I have open for coursework and tell me what's left.",
]
CLARIFICATION_HOLDOUT = [
    "Check the school-related pages already open in my browser for anything due.",
    "Go through my open tabs for class and flag what's overdue.",
    "Look through whatever I have open for school right now.",
]


def _shape_check(plan: TaskPlan, expected: ExecutionShape) -> bool:
    return plan.execution_shape == expected


CATEGORIES: dict[str, dict[str, Any]] = {
    "single_explicit": {
        "tuned": SINGLE_EXPLICIT, "holdout": SINGLE_EXPLICIT_HOLDOUT, "mode": "plan",
        "check": lambda p: _shape_check(p, ExecutionShape.SINGLE)
        and any(r.kind == ResourceKind.EXPLICIT_URLS for r in p.resource_requirements),
    },
    "current_page": {
        "tuned": CURRENT_PAGE, "holdout": CURRENT_PAGE_HOLDOUT, "mode": "plan",
        "check": lambda p: _shape_check(p, ExecutionShape.SINGLE)
        and (not p.resource_requirements or all(r.kind == ResourceKind.CURRENT_PAGE for r in p.resource_requirements)),
    },
    "sweep_explicit": {
        "tuned": SWEEP_EXPLICIT, "holdout": SWEEP_EXPLICIT_HOLDOUT, "mode": "plan",
        "check": lambda p: _shape_check(p, ExecutionShape.SWEEP)
        and any(r.kind == ResourceKind.EXPLICIT_URLS for r in p.resource_requirements),
    },
    "ordered_workflow": {
        "tuned": ORDERED_WORKFLOW, "holdout": ORDERED_WORKFLOW_HOLDOUT, "mode": "plan",
        "check": lambda p: _shape_check(p, ExecutionShape.ORDERED_WORKFLOW) and len(p.steps) >= 2,
    },
    "mixed_intent": {
        "tuned": MIXED_INTENT, "holdout": MIXED_INTENT_HOLDOUT, "mode": "plan",
        "check": lambda p: p.intent == PlanIntent.MIXED,
    },
    "research": {
        "tuned": RESEARCH, "holdout": RESEARCH_HOLDOUT, "mode": "plan",
        "check": lambda p: _shape_check(p, ExecutionShape.OPEN_RESEARCH),
    },
    "open_tabs": {
        "tuned": OPEN_TABS, "holdout": OPEN_TABS_HOLDOUT, "mode": "route", "pool": _POOL_WITH_COURSES,
        "check": None,  # graded specially: see _grade_route_result
    },
    "clarification": {
        "tuned": CLARIFICATION, "holdout": CLARIFICATION_HOLDOUT, "mode": "route", "pool": _POOL_WITHOUT_COURSES,
        "check": None,
    },
}


def _grade_route_result(category: str, result: Any) -> tuple[bool, str]:
    valid_urls = {t["url"] for t in _active_pool}
    if category == "open_tabs":
        if not isinstance(result, RouterDecision):
            return False, f"expected a resolved RouterDecision, got {type(result).__name__}"
        if not result.targets:
            return False, "resolved with zero targets"
        invented = [t for t in result.targets if t not in valid_urls]
        if invented:
            return False, f"hallucinated target(s) not in the open-tab pool: {invented}"
        course_targets = [t for t in result.targets if t.startswith("https://canvas.example.edu")]
        if not course_targets:
            return False, "resolved but selected no course-like tabs"
        return True, "ok"
    if category == "clarification":
        if isinstance(result, NeedsInput):
            return True, "ok"
        return False, f"expected NeedsInput (no matching resource), got {type(result).__name__}"
    return False, "unknown category"


async def _run_category(client, config: AppConfig, name: str, spec: dict[str, Any]) -> dict[str, Any]:
    prompts = [(p, "tuned") for p in spec["tuned"]] + [(p, "holdout") for p in spec["holdout"]]
    results = []
    global _active_pool
    if spec["mode"] == "route":
        _active_pool = spec["pool"]
    for prompt, split in prompts:
        started = time.monotonic()
        try:
            if spec["mode"] == "plan":
                plan = await plan_task(client, prompt)
                ok = bool(spec["check"](plan))
                reason = "ok" if ok else f"unexpected plan: intent={plan.intent.value} shape={plan.execution_shape.value} resources={[r.kind.value for r in plan.resource_requirements]}"
                schema_valid = True
                extra = {"intent": plan.intent.value, "execution_shape": plan.execution_shape.value,
                          "resource_kinds": [r.kind.value for r in plan.resource_requirements]}
            else:
                result = await route(prompt, client, config)
                ok, reason = _grade_route_result(name, result)
                schema_valid = True
                extra = {"result_type": type(result).__name__,
                          "targets": getattr(result, "targets", None),
                          "question": getattr(result, "question", None)}
            elapsed_ms = (time.monotonic() - started) * 1000
            results.append({"prompt": prompt, "split": split, "ok": ok, "schema_valid": schema_valid,
                              "reason": reason, "latency_ms": elapsed_ms, **extra})
        except (PlannerOutputError, RoutingError) as exc:
            elapsed_ms = (time.monotonic() - started) * 1000
            results.append({"prompt": prompt, "split": split, "ok": False, "schema_valid": False,
                              "reason": str(exc), "latency_ms": elapsed_ms})
    return {"category": name, "results": results}


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=None)
    parser.add_argument("--categories", default=None, help="comma-separated subset of category names")
    parser.add_argument(
        "--output-dir",
        default=str(ROOT / "runtime" / "benchmark_runs" / f"semantic_planner_live_{time.strftime('%Y%m%d_%H%M%S')}"),
    )
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    config = load_config(args.config)
    config.routing.mode = "semantic"  # no legacy fallback: measure the planner's own accuracy
    # ResourceResolver.get_open_tabs() only ever calls list_open_tabs() in cdp_attach mode
    # (open_tabs resolution is a persistent-browser feature) — the open_tabs/clarification
    # categories need this set even though list_open_tabs itself is patched below, or
    # resolution short-circuits to "no tabs" before the patched function is ever called.
    config.browser.mode = "cdp_attach"
    config.browser.cdp_endpoint = "http://127.0.0.1:9222"
    client = create_inference_client(config)

    if not await client.health_check():
        print(f"Local model endpoint unavailable: {client.endpoint}")
        raise SystemExit(1)

    resources_mod.list_open_tabs = _patched_list_open_tabs

    selected = args.categories.split(",") if args.categories else list(CATEGORIES.keys())
    all_category_results = []
    for name in selected:
        print(f"--- {name} ---")
        cat_result = await _run_category(client, config, name, CATEGORIES[name])
        all_category_results.append(cat_result)
        for r in cat_result["results"]:
            print(f"  [{'OK' if r['ok'] else 'FAIL'}][{r['split']}] {r['prompt'][:70]!r} -> {r['reason']}")

    resources_mod.list_open_tabs = real_list_open_tabs

    summary = _summarize(all_category_results)
    with open(output_dir / "summary.json", "w", encoding="utf-8") as f:
        json.dump({"summary": summary, "categories": all_category_results}, f, indent=2)
    print()
    print(json.dumps(summary, indent=2))
    print(f"\nFull results written to {output_dir / 'summary.json'}")


def _summarize(all_category_results: list[dict[str, Any]]) -> dict[str, Any]:
    per_category = {}
    total_tuned = correct_tuned = 0
    total_holdout = correct_holdout = 0
    total_schema_valid = total_calls = 0
    hallucinations = 0
    for cat in all_category_results:
        tuned = [r for r in cat["results"] if r["split"] == "tuned"]
        holdout = [r for r in cat["results"] if r["split"] == "holdout"]
        tuned_ok = sum(1 for r in tuned if r["ok"])
        holdout_ok = sum(1 for r in holdout if r["ok"])
        per_category[cat["category"]] = {
            "tuned": f"{tuned_ok}/{len(tuned)}", "holdout": f"{holdout_ok}/{len(holdout)}",
        }
        total_tuned += len(tuned)
        correct_tuned += tuned_ok
        total_holdout += len(holdout)
        correct_holdout += holdout_ok
        for r in cat["results"]:
            total_calls += 1
            if r["schema_valid"]:
                total_schema_valid += 1
            if "hallucinated" in (r.get("reason") or ""):
                hallucinations += 1
    return {
        "per_category": per_category,
        "tuned_accuracy": correct_tuned / total_tuned if total_tuned else None,
        "holdout_accuracy": correct_holdout / total_holdout if total_holdout else None,
        "overall_accuracy": (correct_tuned + correct_holdout) / (total_tuned + total_holdout) if (total_tuned + total_holdout) else None,
        "schema_validity_rate": total_schema_valid / total_calls if total_calls else None,
        "hallucinated_target_count": hallucinations,
        "total_prompts": total_tuned + total_holdout,
    }


if __name__ == "__main__":
    asyncio.run(main())
