"""Phase 6 falsification: Gate 1 (richer AXTree/semantic regions) and Gate 2 (vision fallback).

Architecture doc (BrowserAgent_General_Autonomous_Agent_Architecture_REVISED.pdf, section 18,
Phase 6 table) gates these two optional components as:

  Gate 1 - richer observation: "Build only if current PageObservation misses required
  controls/text on a statistically meaningful fixture set and richer observation improves
  success >10% without unacceptable token/latency cost. Do not build if most failures are
  planning/completion, not observation."

  Gate 2 - vision fallback: "Build only if visual-only/canvas/layout tasks fail in DOM mode
  and targeted crops improve success materially. Do not build if VLM cost/VRAM/latency
  outweighs gains or target tasks are rare."

This script gathers NEW, real evidence (real headless Chromium via Playwright, real
Qwen3-8B/Ollama — not scripted) against two purpose-built fixtures designed to stress exactly
the failure modes each gate names:

  - tests/fixtures/simple_site/phase6_table_regions.html: a table whose Tuesday/Widgets value
    is only meaningful via row/column header association, plus two identically-named "Details"
    buttons distinguished only by which <section>/<h2> region they sit in.
  - tests/fixtures/simple_site/phase6_canvas_only.html: a canvas-rendered color swatch with the
    color deliberately never named in any DOM text/aria attribute/alt text anywhere on the page
    — a genuinely vision-only task, unanswerable from the accessibility tree by construction.

Deterministic step (no model): render browser/observer.py's actual compact observation for
both fixtures and inspect it directly for whether the required information is textually
present at all (Gate 1's own "misses required controls/text" wording) before ever asking
whether the live model can act on it.
"""
from __future__ import annotations

import asyncio
import functools
import json
import sys
import threading
from datetime import date
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agent.config import load_config
from agent.loop import AgentLoop
from browser.observer import extract_observation
from memory.event_store import EventType

RESULTS_DIR = Path(__file__).resolve().parent / "results"
SIMPLE_SITE_DIR = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "simple_site"


def _serve(directory: Path):
    handler = functools.partial(SimpleHTTPRequestHandler, directory=str(directory))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{port}"


async def deterministic_observation_check(base_url: str) -> dict[str, Any]:
    """No model involved: does browser/observer.py's actual production extraction already
    surface the information each gate worries a flat DOM/accessibility view would miss?"""
    from playwright.async_api import async_playwright

    findings: dict[str, Any] = {}
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()

        await page.goto(f"{base_url}/phase6_table_regions.html")
        obs = await extract_observation(page, max_chars=6000, max_visible_text_items=40)
        rendered = obs.render_compact(6000, 40)
        findings["table_tuesday_widgets_value_present"] = "47" in rendered
        # Region disambiguation: can the two "Details" buttons be told apart from the
        # rendered text alone? Flat visible_text is order-preserving, so proximity in the
        # rendered string is the only signal available (no structural grouping).
        product_a_idx = rendered.find("Product A")
        product_b_idx = rendered.find("Product B")
        button_indices = [i for i in range(len(rendered)) if rendered.startswith("Details", i)]
        findings["product_headings_present_in_text"] = product_a_idx != -1 and product_b_idx != -1
        findings["details_button_count_in_observation"] = sum(
            1 for el in obs.elements if el.name.strip().lower() == "details"
        )
        findings["region_disambiguation_requires_ordinal_reasoning"] = (
            findings["details_button_count_in_observation"] == 2
            and findings["product_headings_present_in_text"]
        )

        await page.goto(f"{base_url}/phase6_canvas_only.html")
        obs2 = await extract_observation(page, max_chars=6000, max_visible_text_items=40)
        rendered2 = obs2.render_compact(6000, 40)
        color_words = ["red", "green", "blue", "sea green", "#2e8b57"]
        findings["canvas_color_word_leaked_into_observation"] = any(
            w in rendered2.lower() for w in color_words
        )
        findings["canvas_task_rendered_text"] = rendered2

        await browser.close()
    return findings


async def _run_live_task(goal: str, target_url: str, max_steps: int) -> dict[str, Any]:
    config = load_config(None)
    config.browser.headless = True
    config.browser.interactive_approval = False
    loop = AgentLoop.create_new(config, goal, [], explicit_target_url=target_url)
    await loop.start_browser()
    try:
        # Pre-navigate deterministically (bypassing the model) so this gate's evidence isolates
        # observation/vision reasoning from navigation reliability, which is already separately
        # proven (Phase 1B/5B) and not what Gate 1/Gate 2 are testing.
        await loop.browser.open_url(target_url)
        state = await loop.run_steps(max_steps)
        events = loop.event_store.all_events(loop.task_id)
        result_text = ""
        for e in events:
            if e.type == EventType.TASK_COMPLETED:
                result_text = e.payload.get("result", "")
        final_obs = await loop.browser.observe()
        return {
            "status": state.status,
            "blocked_reason": state.blocked_reason,
            "steps": state.current_step,
            "result_text": result_text,
            "final_title": final_obs.title,
            "final_url": final_obs.url,
        }
    finally:
        await loop.aclose()


async def gate1_live_region_task(base_url: str) -> dict[str, Any]:
    run = await _run_live_task(
        goal=(
            "You are on the Weekly Inventory Report page. Click the 'Details' button that is "
            "under the 'Product A' heading, not the one under 'Product B'. Then finish."
        ),
        target_url=f"{base_url}/phase6_table_regions.html",
        max_steps=6,
    )
    run["scenario"] = "gate1_region_disambiguation"
    run["correct"] = run["final_title"] == "Product A Details Opened"
    return run


async def gate1_live_table_task(base_url: str) -> dict[str, Any]:
    run = await _run_live_task(
        goal=(
            "You are on the Weekly Inventory Report page. Read the inventory table and "
            "determine how many Widgets were recorded on Tuesday. Finish with exactly that "
            "number as your result."
        ),
        target_url=f"{base_url}/phase6_table_regions.html",
        max_steps=5,
    )
    run["scenario"] = "gate1_table_value_extraction"
    run["correct"] = "47" in (run["result_text"] or "")
    return run


async def gate2_live_canvas_task(base_url: str) -> dict[str, Any]:
    run = await _run_live_task(
        goal=(
            "You are on the Color Picker Challenge page. Click the button matching the color "
            "of the square shown, then finish."
        ),
        target_url=f"{base_url}/phase6_canvas_only.html",
        max_steps=5,
    )
    run["scenario"] = "gate2_canvas_only_color_match"
    # The fixture deliberately never names the color anywhere in DOM/text/aria, so any
    # apparent "success" here is a 1-in-3 guess, not genuine task understanding — see report.
    run["chose"] = run["final_title"]
    return run


async def main() -> dict[str, Any]:
    server, base_url = _serve(SIMPLE_SITE_DIR)
    try:
        deterministic = await deterministic_observation_check(base_url)
        live_region = await gate1_live_region_task(base_url)
        live_table = await gate1_live_table_task(base_url)
        live_canvas = await gate2_live_canvas_task(base_url)
    finally:
        server.shutdown()
        server.server_close()

    gate1_verdict = {
        "required_info_present_in_observation": (
            deterministic["table_tuesday_widgets_value_present"]
            and deterministic["product_headings_present_in_text"]
        ),
        "live_region_task_correct": live_region["correct"],
        "live_table_task_correct": live_table["correct"],
        "build_condition_met": False,  # set below after evaluating the doc's own gate text
    }
    # Gate text: "build only if current PageObservation MISSES required controls/text ... and
    # richer observation improves success." If the deterministic check shows the info IS
    # present (both facts render into the flat text/element list), any live-task failure is a
    # planning/reasoning failure over already-available information, not a missing-observation
    # failure — the doc's own explicit "do not build if" condition.
    gate1_verdict["build_condition_met"] = (
        not gate1_verdict["required_info_present_in_observation"]
    )

    gate2_verdict = {
        "canvas_content_absent_from_observation": not deterministic[
            "canvas_color_word_leaked_into_observation"
        ],
        "live_task_status": live_canvas["status"],
        "live_task_chose": live_canvas["chose"],
    }

    report = {
        "deterministic_observation_check": deterministic,
        "live_tasks": [live_region, live_table, live_canvas],
        "gate1_richer_observation": gate1_verdict,
        "gate2_vision_fallback": gate2_verdict,
    }
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = RESULTS_DIR / f"phase6_gate1_gate2_{date.today().isoformat()}.json"
    out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    print(f"\nWritten to {out_path}")
    return report


if __name__ == "__main__":
    asyncio.run(main())
