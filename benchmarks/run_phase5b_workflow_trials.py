"""Phase 5B ordered-workflow validation slice (Section 31-34): runs WorkflowOrchestrator
against real Qwen3-8B/Ollama over the tuned + holdout + cross-site-dependency fixtures in
tests/fixtures/simple_site/workflow_*.html. This is the REDUCED live slice (3-5 trials, not
the full 10+10 gate) — see docs/PHASE5B_REPORT.md for the follow-up command to run the full
10+10 gate later.

Usage:
    python benchmarks/run_phase5b_workflow_trials.py [--output-dir DIR]
"""
from __future__ import annotations

import argparse
import asyncio
import functools
import json
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from agent.config import load_config
from workflow.models import WorkflowPolicy, WorkflowStepStatus
from workflow.orchestrator import WorkflowOrchestrator
from workflow.store import WorkflowStore

ROOT = Path(__file__).resolve().parent.parent
FIXTURE_SITE_DIR = ROOT / "tests" / "fixtures" / "simple_site"


def _start_fixture_server() -> tuple[ThreadingHTTPServer, str]:
    handler = functools.partial(SimpleHTTPRequestHandler, directory=str(FIXTURE_SITE_DIR))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def _scenarios(base_url: str) -> list[dict[str, Any]]:
    return [
        {
            "id": "tuned_3site_reversible",
            "objective": "Change the display mode to Compact, enable the weekly summary emails, then verify the account status.",
            "steps": [
                {"ordinal": 1, "target": f"{base_url}/workflow_site_a.html", "objective": "Change the Mode dropdown to Compact."},
                {"ordinal": 2, "target": f"{base_url}/workflow_site_b.html", "objective": "Enable the weekly summary email checkbox."},
                {"ordinal": 3, "target": f"{base_url}/workflow_site_c.html", "objective": "Verify the account status is Active."},
            ],
        },
        {
            "id": "cross_site_dependency",
            "objective": "Find the project code, then enter it into the configuration and save.",
            "steps": [
                {"ordinal": 1, "target": f"{base_url}/workflow_dep_a.html", "objective": "Find the project code shown on this page."},
                {"ordinal": 2, "target": f"{base_url}/workflow_dep_b.html", "objective": "Enter the project code found on the previous page into the Project code field and click Save configuration."},
            ],
        },
        {
            "id": "holdout_3site_different_order",
            "objective": "Check the system status, then switch to Dark theme, then enable SMS alerts.",
            "steps": [
                {"ordinal": 1, "target": f"{base_url}/workflow_holdout_status.html", "objective": "Note the current system status."},
                {"ordinal": 2, "target": f"{base_url}/workflow_holdout_theme.html", "objective": "Change the Theme dropdown to Dark."},
                {"ordinal": 3, "target": f"{base_url}/workflow_holdout_alerts.html", "objective": "Turn on SMS alerts."},
            ],
        },
    ]


async def _run_scenario(config_path: str | None, scenario: dict[str, Any], output_dir: Path, trial: int) -> dict[str, Any]:
    config = load_config(config_path)
    config.browser.headless = True
    config.browser.interactive_approval = False
    workflow_dir = output_dir / f"{scenario['id']}_trial{trial}"
    config.storage.tasks_dir = str(workflow_dir / "tasks")
    config.browser.user_data_dir = str(workflow_dir / "tasks")
    config.logging.dir = str(workflow_dir / "logs")

    store = WorkflowStore.for_workflow_dir(workflow_dir)
    started = time.monotonic()
    try:
        policy = WorkflowPolicy(max_attempts_per_step=2, max_steps_per_step=12, max_seconds_per_step=90.0)
        workflow_id = store.create_workflow(scenario["objective"], scenario["steps"], policy)
        final = await WorkflowOrchestrator(config, store, workflow_id, policy).run()
    finally:
        store.close()
    elapsed_s = time.monotonic() - started

    step_results = final["steps"]
    all_verified = all(s["status"] == WorkflowStepStatus.COMPLETED.value and s["verified"] for s in step_results)
    record = {
        "scenario_id": scenario["id"],
        "trial": trial,
        "workflow_status": final["status"],
        "full_workflow_success": all_verified,
        "step_count": len(step_results),
        "steps_completed": sum(1 for s in step_results if s["status"] == WorkflowStepStatus.COMPLETED.value),
        "steps_verified": sum(1 for s in step_results if s["verified"]),
        "duration_s": elapsed_s,
        "steps": step_results,
    }
    with open(output_dir / f"{scenario['id']}_trial{trial}.json", "w", encoding="utf-8") as f:
        json.dump(record, f, indent=2)
    return record


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=None)
    parser.add_argument("--output-dir", default=str(ROOT / "runtime" / "benchmark_runs" / f"phase5b_workflow_{time.strftime('%Y%m%d_%H%M%S')}"))
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    server, base_url = _start_fixture_server()
    try:
        scenarios = _scenarios(base_url)
        records = [await _run_scenario(args.config, s, output_dir, trial=1) for s in scenarios]
        summary = {
            "trials": len(records),
            "full_workflow_successes": sum(1 for r in records if r["full_workflow_success"]),
            "total_steps": sum(r["step_count"] for r in records),
            "steps_verified": sum(r["steps_verified"] for r in records),
            "records": records,
        }
        with open(output_dir / "summary.json", "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2)
        print(json.dumps({k: v for k, v in summary.items() if k != "records"}, indent=2))
        for r in records:
            print(f"{r['scenario_id']}: workflow_status={r['workflow_status']} "
                  f"full_success={r['full_workflow_success']} "
                  f"steps_verified={r['steps_verified']}/{r['step_count']} "
                  f"duration={r['duration_s']:.1f}s")
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    asyncio.run(main())
