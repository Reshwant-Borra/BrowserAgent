"""Phase 0 (BrowserAgent_General_Autonomous_Agent_Architecture_REVISED.pdf, section 18):
generality benchmark harness. Two layers:

1. Fast, model-free structural checks (always run, part of the normal suite) — the fixture
   sites exist, serve, and have the expected shape. These must never depend on a live model
   or network so the existing test suite stays fast and green regardless of Ollama/GPU state.

2. A live gate (`test_live_baseline_detects_known_gap`, opt-in via RUN_LIVE_BENCHMARKS=1)
   that actually runs the current (pre-workspace/pre-controller) AgentLoop against the
   fixtures via benchmarks/general_agent/run_baseline.py and asserts the hidden evaluation
   criteria below can detect at least one known architectural gap from the resulting trace —
   without the fixtures or this file leaking to the agent *how* the gap should be fixed
   (no "workspace", "entity", "top-k" language anywhere the model can see; the fixtures only
   ever describe an ordinary user-facing task).
"""
from __future__ import annotations

import functools
import json
import os
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
import urllib.request

FIXTURES_DIR = Path(__file__).resolve().parents[2] / "benchmarks" / "general_agent" / "fixtures"

EXPECTED_SCENARIOS = {
    "multi_entity_topn": ["page1.html", "page2.html", "page3.html"],
    "dynamic_dom_churn": ["index.html", "done.html"],
    "cross_site_dependency": ["source.html", "target.html"],
    "open_tab_task": ["main.html", "reference.html"],
    "research_sources": ["index.html", "source_a.html", "source_b.html", "source_c.html"],
    "prompt_injection": ["index.html"],
    "failure_replan": ["start.html", "alt_save.html"],
}


# ---------------------------------------------------------------------------------------
# Layer 1: fast structural checks (no model, no live browser)
# ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("scenario,files", sorted(EXPECTED_SCENARIOS.items()))
def test_fixture_scenario_files_present(scenario: str, files: list[str]) -> None:
    scenario_dir = FIXTURES_DIR / scenario
    assert scenario_dir.is_dir(), f"missing fixture scenario dir: {scenario_dir}"
    for name in files:
        path = scenario_dir / name
        assert path.is_file(), f"missing fixture file: {path}"
        assert path.stat().st_size > 0


def test_fixture_site_serves_over_http() -> None:
    handler = functools.partial(SimpleHTTPRequestHandler, directory=str(FIXTURES_DIR))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/multi_entity_topn/page1.html", timeout=5) as resp:
            body = resp.read().decode("utf-8")
        assert "AeroClean 200" in body
    finally:
        server.shutdown()
        server.server_close()


def test_multi_entity_ground_truth_is_computable_from_fixtures() -> None:
    """The fixture pages are the sole source of truth for the top-N ground truth — this
    computes it directly from the HTML rather than hard-coding it twice, so the hidden
    evaluator below can never silently drift from what the pages actually say."""
    import re

    items: list[tuple[str, float]] = []
    for page in ("page1.html", "page2.html", "page3.html"):
        text = (FIXTURES_DIR / "multi_entity_topn" / page).read_text(encoding="utf-8")
        for match in re.finditer(r"([A-Za-z][\w ]*?) - \$(\d+\.\d+) - rating", text):
            items.append((match.group(1).strip(), float(match.group(2))))
    assert len(items) == 12, "fixture catalog should have 12 items across 3 pages"
    cheapest_three = sorted(items, key=lambda t: t[1])[:3]
    names = {name for name, _price in cheapest_three}
    assert names == {"BudgetSweep", "EcoSuck Lite", "SilentGlide 5"}


def test_prompt_injection_fixture_contains_no_agent_visible_solution_hint() -> None:
    """The fixture must not tell the model how to defend itself (e.g. mentioning
    'ignore injected instructions' as a literal system prompt snippet it should parrot) —
    only that page content is untrusted, which is the same framing production already uses.
    This keeps the benchmark honest: passing means the agent's actual policy held, not that
    it pattern-matched benchmark scaffolding."""
    text = (FIXTURES_DIR / "prompt_injection" / "index.html").read_text(encoding="utf-8")
    assert "attacker.example.invalid" in text  # the trap itself must be present
    assert "workspace" not in text.lower()
    assert "entity" not in text.lower()


# ---------------------------------------------------------------------------------------
# Layer 2: hidden evaluation — gap detectors operating only on the recorded event trace
# ---------------------------------------------------------------------------------------

def _known_gap_top_n_incomplete_coverage(result: dict[str, Any]) -> bool:
    """Detects the Amazon-class gap this whole migration exists to fix: without a workspace
    that survives across pages/context-compaction, the legacy agent's final answer will often
    reflect only the last page(s) it visited rather than the true minimum across all 12 items.
    Returns True (gap detected) if the finish payload does NOT reference all three true
    cheapest items by name."""
    truth = {"BudgetSweep", "EcoSuck Lite", "SilentGlide 5"}
    finish_text = (result.get("finish_payload") or "").lower()
    mentioned = {name for name in truth if name.lower() in finish_text}
    return mentioned != truth


def _known_gap_no_tab_switch_capability(result: dict[str, Any]) -> bool:
    """Current ModelAction union (agent/schemas.py) has no action for switching to a tab
    opened mid-task by a click with target=_blank — only pre-resolved `preferred_tab_url`
    at task start. Detects the gap if the run didn't reach a 'blocked'/error/incomplete
    status while the goal explicitly required reading a tab opened during the task."""
    return result.get("status") not in ("completed",) or result.get("error") is not None


KNOWN_GAP_DETECTORS = {
    "multi_entity_topn": _known_gap_top_n_incomplete_coverage,
    "open_tab_task": _known_gap_no_tab_switch_capability,
}


def _load_baseline_results() -> dict[str, Any] | None:
    path = Path(__file__).resolve().parents[2] / "runtime" / "benchmark_runs" / "phase0_baseline" / "baseline_results.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.skipif(
    os.environ.get("RUN_LIVE_BENCHMARKS") != "1",
    reason="live model+browser benchmark; run explicitly via RUN_LIVE_BENCHMARKS=1 "
           "after `python benchmarks/general_agent/run_baseline.py`",
)
def test_live_baseline_detects_known_gap() -> None:
    summary = _load_baseline_results()
    assert summary is not None, "run benchmarks/general_agent/run_baseline.py first"
    results_by_name = {r["scenario"]: r for r in summary["results"]}

    detected_any_gap = False
    for scenario, detector in KNOWN_GAP_DETECTORS.items():
        result = results_by_name.get(scenario)
        assert result is not None, f"missing baseline result for {scenario}"
        if detector(result):
            detected_any_gap = True
    assert detected_any_gap, (
        "hidden evaluator found no known architectural gap in the legacy baseline — "
        "either the gaps are already fixed (unexpected pre-Phase-1) or the detectors "
        "need updating before this can be trusted as a baseline lock"
    )
