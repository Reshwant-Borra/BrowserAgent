"""Phase 2 PASS gate (BrowserAgent_General_Autonomous_Agent_Architecture_REVISED.pdf,
section 18): "unseen local tasks requiring at least 3 distinct procedures achieve >= legacy
success with <= 25% model-call overhead; planner schema validity 100%".

Runs three NEW ("unseen" — not reused from Phase 0's benchmarks/general_agent/fixtures/)
three-procedure scenarios through both the plain legacy AgentLoop (one monolithic task, goal
text listing every step, matching how benchmarks/general_agent/run_baseline.py already
measured the legacy baseline) and the GeneralAgentController (decomposed into subgoals by a
live qwen3:8b planner), with a real Ollama backend for every model call on both sides —
model-call counting wraps the real InferenceClient rather than adding counters to production
code, since neither agent/loop.py nor agent/controller.py needs to know a benchmark is
running.
"""
from __future__ import annotations

import argparse
import asyncio
import functools
import json
import sys
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from agent.config import load_config
from agent.controller import GeneralAgentController
from agent.loop import AgentLoop
from agent.planner import PlannerOutputError
from inference.llama_client import CompletionResult, InferenceClient, create_inference_client
from memory.event_store import EventStore, EventType

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "phase2_unseen"

SCENARIOS: list[dict[str, Any]] = [
    {
        "name": "multi_step_registration",
        "entry": "multi_step_registration/signup.html",
        "goal": "Complete account registration: read the invite code on the signup page, "
                "enter that exact code on the verification page to verify it, then go to "
                "the account settings page, enable the weekly summary email toggle, and save.",
        "max_steps_legacy": 25,
        "max_steps_per_subgoal": 12,
    },
    {
        "name": "compare_and_report",
        "entry": "compare_and_report/index.html",
        "goal": "Visit both hosting plans listed in the directory (NimbusHost and "
                "StackForge), find each one's monthly price, and report which one is cheaper "
                "and by how much.",
        "max_steps_legacy": 20,
        "max_steps_per_subgoal": 10,
    },
    {
        "name": "sequential_form_fill",
        "entry": "sequential_form_fill/profile.html",
        "goal": "Read the reference ID on the profile page, enter that exact reference ID on "
                "the confirmation page and confirm it, then report the confirmation number "
                "shown on the resulting receipt page.",
        "max_steps_legacy": 20,
        "max_steps_per_subgoal": 10,
    },
]


class _CountingClient:
    """Wraps a real InferenceClient purely to count .complete() calls for this benchmark's
    model-call-overhead metric — production code never sees this wrapper."""

    def __init__(self, inner: InferenceClient):
        self._inner = inner
        self.endpoint = inner.endpoint
        self.calls = 0
        self.schema_validity: list[bool] = []

    async def complete(self, prompt: str, grammar=None, max_tokens: int = 256, json_schema=None) -> CompletionResult:
        self.calls += 1
        result = await self._inner.complete(prompt, grammar=grammar, max_tokens=max_tokens, json_schema=json_schema)
        if json_schema is not None:
            try:
                json.loads(result.text)
                self.schema_validity.append(True)
            except json.JSONDecodeError:
                self.schema_validity.append(False)
        return result

    async def health_check(self) -> bool:
        return await self._inner.health_check()


def _start_server(root: Path) -> tuple[ThreadingHTTPServer, str]:
    handler = functools.partial(SimpleHTTPRequestHandler, directory=str(root))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


async def run_legacy(scenario: dict[str, Any], base_url: str, output_dir: Path) -> dict[str, Any]:
    config = load_config(None)
    config.browser.headless = True
    config.browser.interactive_approval = False
    run_dir = output_dir / scenario["name"] / "legacy"
    config.storage.runtime_dir = str(run_dir / "runtime")
    config.storage.tasks_dir = str(run_dir / "tasks")
    config.browser.user_data_dir = str(run_dir / "tasks")
    config.logging.dir = str(run_dir / "logs")

    target_url = f"{base_url}/{scenario['entry']}"
    goal = f"Open {target_url} first. {scenario['goal']}"
    loop = AgentLoop.create_new(config, goal, [], explicit_target_url=target_url)
    counting = _CountingClient(loop.llama)
    loop.llama = counting

    start = time.monotonic()
    try:
        state = await loop.run(max_steps=scenario["max_steps_legacy"])
        status, current_step = state.status, state.current_step
    except Exception as exc:  # noqa: BLE001 - a single flaky live browser/model run must
        # never abort the whole repeated-trial matrix (item 2's own "fix the benchmark noise"
        # mandate) — record it as a crashed trial (counted as a failure by every gate below)
        # instead of losing every other already-collected trial to one exception.
        status, current_step = "crashed", loop.state_store.load(loop.task_id).current_step
        print(f"  [legacy crashed] {scenario['name']}: {type(exc).__name__}: {exc}", file=sys.stderr)
    duration = time.monotonic() - start
    return {
        "status": status, "steps": current_step, "model_calls": counting.calls,
        "duration_s": round(duration, 2),
    }


async def run_general(scenario: dict[str, Any], base_url: str, output_dir: Path,
                       strategy: str = "delegated", trial: int = 0) -> dict[str, Any]:
    config = load_config(None)
    config.browser.headless = True
    config.browser.interactive_approval = False
    config.agent.max_steps_per_subgoal = scenario["max_steps_per_subgoal"]
    run_dir = output_dir / scenario["name"] / f"general_{strategy}_trial{trial}"
    config.storage.runtime_dir = str(run_dir / "runtime")
    config.storage.tasks_dir = str(run_dir / "tasks")
    config.browser.user_data_dir = str(run_dir / "tasks")
    config.logging.dir = str(run_dir / "logs")

    target_url = f"{base_url}/{scenario['entry']}"
    controller_client = _CountingClient(create_inference_client(config))
    child_clients: list[_CountingClient] = []

    def child_factory() -> InferenceClient:
        client = _CountingClient(create_inference_client(config))
        child_clients.append(client)
        return client

    controller = GeneralAgentController.create_new(
        config, scenario["goal"], [], llama_client=controller_client,
        child_llama_client_factory=child_factory,
    )
    start = time.monotonic()
    error: Optional[str] = None
    try:
        state = await controller.run(explicit_target_url=target_url, strategy=strategy)
    except PlannerOutputError as exc:
        error = f"PlannerOutputError: {exc}"
        state = controller.state_store.load(controller.control_task_id)
    except Exception as exc:  # noqa: BLE001 - see run_legacy's identical comment: one flaky
        # live browser/model exception must not abort the whole repeated-trial matrix.
        error = f"{type(exc).__name__}: {exc}"
        state = controller.state_store.load(controller.control_task_id)
        if state.status == "running":
            state.status = "crashed"
        print(f"  [{strategy} crashed] {scenario['name']}: {error}", file=sys.stderr)
    duration = time.monotonic() - start

    total_model_calls = controller_client.calls + sum(c.calls for c in child_clients)
    schema_checks = controller_client.schema_validity
    result = {
        "status": state.status, "blocked_reason": state.blocked_reason,
        "completed_subgoals": state.completed_subgoals, "model_calls": total_model_calls,
        "controller_model_calls": controller_client.calls,
        "child_model_calls": sum(c.calls for c in child_clients),
        "subgoal_children": len(child_clients),
        "duration_s": round(duration, 2), "error": error,
        "planner_schema_valid_count": sum(1 for v in schema_checks if v),
        "planner_schema_total_count": len(schema_checks),
    }
    controller.close()
    return result


async def run_scenario_trial(scenario: dict[str, Any], output_dir: Path, trial: int,
                              strategies: list[str]) -> dict[str, Any]:
    server, base_url = _start_server(FIXTURES_DIR)
    try:
        legacy = await run_legacy(scenario, base_url, output_dir)
        general_by_strategy = {}
        for strategy in strategies:
            general_by_strategy[strategy] = await run_general(scenario, base_url, output_dir, strategy, trial)
    finally:
        server.shutdown()
        server.server_close()

    legacy_success = legacy["status"] == "completed"
    result: dict[str, Any] = {"scenario": scenario["name"], "trial": trial,
                               "legacy": legacy, "legacy_success": legacy_success}
    for strategy, general in general_by_strategy.items():
        general_success = general["status"] == "completed"
        overhead_pct = None
        if legacy["model_calls"] > 0:
            overhead_pct = round(100.0 * (general["model_calls"] - legacy["model_calls"]) / legacy["model_calls"], 1)
        result[strategy] = {
            "general": general,
            "general_success": general_success,
            "meets_success_gate": general_success or not legacy_success,  # >= legacy success
            "model_call_overhead_pct": overhead_pct,
            "meets_overhead_gate": (overhead_pct is not None and overhead_pct <= 25.0) if legacy_success else None,
        }
    return result


def _median(values: list[float]) -> Optional[float]:
    if not values:
        return None
    s = sorted(values)
    n = len(s)
    mid = n // 2
    return s[mid] if n % 2 == 1 else (s[mid - 1] + s[mid]) / 2.0


def _percentile(values: list[float], pct: float) -> Optional[float]:
    if not values:
        return None
    s = sorted(values)
    idx = min(len(s) - 1, max(0, round(pct / 100.0 * (len(s) - 1))))
    return s[idx]


def _stats(values: list[float]) -> dict[str, Any]:
    return {
        "n": len(values), "values": values,
        "median": _median(values), "min": min(values) if values else None,
        "max": max(values) if values else None,
        "p25": _percentile(values, 25), "p75": _percentile(values, 75),
    }


def summarize_scenario(scenario_name: str, trials: list[dict[str, Any]], strategies: list[str]) -> dict[str, Any]:
    legacy_calls = [t["legacy"]["model_calls"] for t in trials]
    legacy_actions = [t["legacy"]["steps"] for t in trials]
    legacy_duration = [t["legacy"]["duration_s"] for t in trials]
    legacy_success_rate = sum(1 for t in trials if t["legacy_success"]) / len(trials)

    summary: dict[str, Any] = {
        "scenario": scenario_name,
        "trial_count": len(trials),
        "legacy": {
            "success_rate": legacy_success_rate,
            "model_calls": _stats(legacy_calls),
            "actions": _stats(legacy_actions),
            "duration_s": _stats(legacy_duration),
        },
    }
    for strategy in strategies:
        calls = [t[strategy]["general"]["model_calls"] for t in trials]
        actions = [t[strategy]["general"]["subgoal_children"] for t in trials]
        duration = [t[strategy]["general"]["duration_s"] for t in trials]
        overheads = [t[strategy]["model_call_overhead_pct"] for t in trials if t[strategy]["model_call_overhead_pct"] is not None]
        success_rate = sum(1 for t in trials if t[strategy]["general_success"]) / len(trials)
        median_overhead = _median(overheads)
        summary[strategy] = {
            "success_rate": success_rate,
            "model_calls": _stats(calls),
            "subgoal_children_or_steps": _stats(actions),
            "duration_s": _stats(duration),
            "model_call_overhead_pct": _stats(overheads),
            # Correctness-first robust gate: success must be >= legacy's median-run success,
            # and the *median* overhead (not any single run) must clear the budget — a single
            # favorable/unfavorable run on this local 8B model is not trustworthy on its own
            # (docs/BROWSERAGENT_MASTER_STATUS.md's Phase 2 corrective-pass section).
            "meets_success_gate": success_rate >= legacy_success_rate,
            "meets_overhead_gate": (median_overhead is not None and median_overhead <= 25.0) if legacy_success_rate > 0 else None,
        }
    return summary


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", default=None)
    parser.add_argument("--trials", type=int, default=3, help="independent live trials per scenario (min 3, prefer 5)")
    parser.add_argument("--strategies", default="delegated,continuous",
                         help="comma-separated GeneralAgentController.run(strategy=...) values to benchmark")
    parser.add_argument("--scenarios", default=None, help="comma-separated scenario names (default: all)")
    args = parser.parse_args()
    output_dir = Path(args.output_dir) if args.output_dir else ROOT / "runtime" / "benchmark_runs" / "phase2_controller"
    output_dir.mkdir(parents=True, exist_ok=True)
    strategies = [s.strip() for s in args.strategies.split(",") if s.strip()]
    scenario_names = {s.strip() for s in args.scenarios.split(",")} if args.scenarios else None
    scenarios = [s for s in SCENARIOS if scenario_names is None or s["name"] in scenario_names]

    all_trials: list[dict[str, Any]] = []
    summaries = []
    for scenario in scenarios:
        trials = []
        for trial in range(args.trials):
            print(f"--- {scenario['name']} trial {trial + 1}/{args.trials} ---", file=sys.stderr)
            result = await run_scenario_trial(scenario, output_dir, trial, strategies)
            print(json.dumps(result, indent=2), file=sys.stderr)
            trials.append(result)
            all_trials.append(result)
        summary = summarize_scenario(scenario["name"], trials, strategies)
        print(json.dumps(summary, indent=2), file=sys.stderr)
        summaries.append(summary)

    output = {"trials_per_scenario": args.trials, "strategies": strategies,
              "scenario_summaries": summaries, "raw_trials": all_trials}
    (output_dir / "phase2_repeated_results.json").write_text(json.dumps(output, indent=2), encoding="utf-8")
    print(json.dumps({"scenario_summaries": summaries}, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
