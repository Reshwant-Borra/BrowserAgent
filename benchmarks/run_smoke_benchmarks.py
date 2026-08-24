from __future__ import annotations

import argparse
import asyncio
import functools
import json
import statistics
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import yaml

from agent.config import load_config
from agent.loop import AgentLoop
from memory.event_store import EventStore, EventType

ROOT = Path(__file__).resolve().parent.parent
FIXTURE_SITE_DIR = ROOT / "tests" / "fixtures" / "simple_site"


def _start_fixture_server() -> tuple[ThreadingHTTPServer, str]:
    handler = functools.partial(SimpleHTTPRequestHandler, directory=str(FIXTURE_SITE_DIR))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def _ns_to_ms(value: int | None) -> float | None:
    return (value / 1_000_000) if value is not None else None


def _failure_category(events: list[dict[str, Any]], status: str, criteria_passed: bool) -> str:
    if status == "completed" and criteria_passed:
        return ""
    decision_errors = [
        e["payload"].get("error") for e in events
        if e["type"] == EventType.MODEL_DECISION.value and e["payload"].get("error")
    ]
    if decision_errors:
        if any(err in {"malformed_json", "schema_invalid"} for err in decision_errors):
            return "SCHEMA"
        return "MODEL"
    if any(e["type"] == EventType.TASK_BLOCKED.value for e in events):
        return "RECOVERY"
    if status == "completed" and not criteria_passed:
        return "COMPLETION"
    failed_verifications = [
        e for e in events
        if e["type"] == EventType.VERIFICATION_RESULT.value
        and e.get("verification_result")
        and not e["verification_result"].get("passed", False)
    ]
    if failed_verifications:
        return "VERIFICATION"
    return "MODEL"


async def _run_trial(config_path: str | None, task: dict[str, Any], base_url: str,
                     output_dir: Path, trial: int) -> dict[str, Any]:
    config = load_config(config_path)
    config.browser.headless = True
    config.browser.interactive_approval = False
    config.storage.tasks_dir = str(output_dir / "tasks")
    config.browser.user_data_dir = str(output_dir / "tasks")
    config.logging.dir = str(output_dir / "logs")

    start_url = task["start_url"].format(base_url=base_url)
    goal = f"Open {start_url}. {task['goal']}"
    started = time.monotonic()
    loop = AgentLoop.create_new(config, goal, task.get("success_criteria", []))
    state = await loop.run(max_steps=int(task.get("max_steps", 10)))
    elapsed_ms = (time.monotonic() - started) * 1000

    event_store = EventStore(Path(config.storage.tasks_dir) / loop.task_id / "task.db")
    try:
        events = [e.model_dump(mode="json") for e in event_store.all_events(loop.task_id)]
    finally:
        event_store.close()
    metric_path = Path(config.logging.dir) / f"{loop.task_id}.metrics.jsonl"
    metrics = _read_jsonl(metric_path)
    model_calls = [m for m in metrics if m.get("event") == "model_call"]
    actions = [e for e in events if e["type"] == EventType.ACTION_INTENT.value]
    recoveries = [e for e in events if e["type"] == EventType.RECOVERY_TRANSITION.value]
    task_completed = next((e for e in reversed(events) if e["type"] == EventType.TASK_COMPLETED.value), None)
    matched = task_completed["payload"].get("success_criteria_textual_matches", []) if task_completed else []
    criteria = task.get("success_criteria", [])
    criteria_passed = len(matched) == len(criteria)

    submit_overstep = False
    if task["id"] == "tier3_wizard_no_overstep":
        submit_overstep = any(
            e["type"] == EventType.ACTION_INTENT.value
            and "submit" in json.dumps(e["payload"]).lower()
            for e in events
        )

    passed = state.status == "completed" and criteria_passed and not submit_overstep
    invalid_targets = sum(
        1 for e in events
        if e["type"] == EventType.MODEL_DECISION.value and e["payload"].get("error") == "stale_target"
    )
    schema_failures = sum(
        1 for e in events
        if e["type"] == EventType.MODEL_DECISION.value
        and e["payload"].get("error") in {"malformed_json", "schema_invalid"}
    )
    failed_verifications = sum(
        1 for e in events
        if e["type"] == EventType.VERIFICATION_RESULT.value
        and e.get("verification_result")
        and not e["verification_result"].get("passed", False)
    )

    trial_record = {
        "task_id": task["id"],
        "trial": trial,
        "browser_agent_task_id": loop.task_id,
        "status": state.status,
        "pass": passed,
        "success_criteria_passed": criteria_passed,
        "submit_overstep": submit_overstep,
        "actions": len(actions),
        "model_calls": len(model_calls),
        "recoveries": len(recoveries),
        "invalid_targets": invalid_targets,
        "schema_failures": schema_failures,
        "verification_failures": failed_verifications,
        "duration_ms": elapsed_ms,
        "failure_category": _failure_category(events, state.status, criteria_passed),
        "metrics": metrics,
        "events": events,
    }
    with open(output_dir / f"{task['id']}_trial_{trial}.json", "w", encoding="utf-8") as f:
        json.dump(trial_record, f, indent=2)
    return trial_record


def _summarize(records: list[dict[str, Any]]) -> dict[str, Any]:
    by_task: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        by_task.setdefault(record["task_id"], []).append(record)

    rows = []
    for task_id, task_records in by_task.items():
        rows.append({
            "task": task_id,
            "trials": len(task_records),
            "passes": sum(1 for r in task_records if r["pass"]),
            "actions_avg": statistics.mean(r["actions"] for r in task_records),
            "model_calls_avg": statistics.mean(r["model_calls"] for r in task_records),
            "recoveries": sum(r["recoveries"] for r in task_records),
            "main_failure": next((r["failure_category"] for r in task_records if not r["pass"]), ""),
        })
    return {"rows": rows, "records": records}


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=None)
    parser.add_argument("--trials", type=int, default=1)
    parser.add_argument("--output-dir", default=str(ROOT / "runtime" / "benchmark_runs" / time.strftime("%Y%m%d_%H%M%S")))
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    with open(ROOT / "benchmarks" / "smoke_tasks.yaml", "r", encoding="utf-8") as f:
        tasks = yaml.safe_load(f)["tasks"]

    server, base_url = _start_fixture_server()
    try:
        records = []
        for trial in range(1, args.trials + 1):
            for task in tasks:
                records.append(await _run_trial(args.config, task, base_url, output_dir, trial))
        summary = _summarize(records)
        with open(output_dir / "summary.json", "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2)
        print(json.dumps(summary["rows"], indent=2))
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    asyncio.run(main())
