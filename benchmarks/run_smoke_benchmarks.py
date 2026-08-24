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
            return "SCHEMA_PARSE_ERROR" if "malformed_json" in decision_errors else "SCHEMA"
        if any(err in {"missing_target", "stale_target", "target_type_mismatch"} for err in decision_errors):
            return "MODEL_TARGET_BINDING_ERROR"
        if "model_intent_error" in decision_errors:
            return "MODEL_INTENT_ERROR"
        if any(err in {"invalid_option", "invalid_url"} for err in decision_errors):
            return "MODEL_PARAMETER_ERROR"
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


def _environment_success(events: list[dict[str, Any]], criteria: list[str], task_id: str) -> bool:
    blob_parts: list[str] = []
    for e in events:
        payload = e.get("payload", {})
        if e["type"] == EventType.OBSERVATION.value:
            blob_parts.extend([
                payload.get("url", ""),
                payload.get("title", ""),
                " ".join(payload.get("element_names", [])),
                " ".join(payload.get("visible_text", [])),
            ])
        elif e["type"] == EventType.ACTION_RESULT.value:
            blob_parts.append(json.dumps(payload.get("result_data", {})))
        elif e["type"] == EventType.TASK_COMPLETED.value:
            blob_parts.append(json.dumps(payload))
    blob = "\n".join(blob_parts).lower()
    criteria_hit = all(c.lower() in blob for c in criteria)
    if task_id == "tier3_wizard_no_overstep":
        return criteria_hit and not _submit_overstep(events)
    return criteria_hit


def _submit_overstep(events: list[dict[str, Any]]) -> bool:
    return any(
        e["type"] == EventType.ACTION_INTENT.value
        and "submit" in json.dumps(e["payload"]).lower()
        for e in events
    )


def _contract_rates(metrics: list[dict[str, Any]]) -> dict[str, Any]:
    rows = [m for m in metrics if m.get("event") == "model_contract"]
    if not rows:
        return {
            "syntax_valid_rate": None,
            "schema_valid_rate": None,
            "semantic_valid_rate": None,
            "contract_repair_calls": 0,
            "contract_repair_successes": 0,
        }
    return {
        "syntax_valid_rate": sum(bool(r.get("syntax_valid")) for r in rows) / len(rows),
        "schema_valid_rate": sum(bool(r.get("schema_valid")) for r in rows) / len(rows),
        "semantic_valid_rate": sum(bool(r.get("semantic_valid")) for r in rows) / len(rows),
        "contract_repair_calls": sum(bool(r.get("repair_attempt")) for r in rows),
        "contract_repair_successes": sum(r.get("repair_success") is True for r in rows),
    }


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

    submit_overstep = _submit_overstep(events) if task["id"] == "tier3_wizard_no_overstep" else False
    environment_success = _environment_success(events, criteria, task["id"])
    model_finish = task_completed is not None

    passed = model_finish and environment_success and not submit_overstep
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

    contract = _contract_rates(metrics)

    trial_record = {
        "task_id": task["id"],
        "trial": trial,
        "browser_agent_task_id": loop.task_id,
        "status": state.status,
        "pass": passed,
        "environment_success": environment_success,
        "model_finish": model_finish,
        "success_criteria_passed": criteria_passed,
        "submit_overstep": submit_overstep,
        "actions": len(actions),
        "model_calls": len(model_calls),
        "recoveries": len(recoveries),
        "invalid_targets": invalid_targets,
        "schema_failures": schema_failures,
        "verification_failures": failed_verifications,
        **contract,
        "duration_ms": elapsed_ms,
        "failure_category": _failure_category(events, state.status, environment_success),
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
            "environment_successes": sum(1 for r in task_records if r["environment_success"]),
            "model_finishes": sum(1 for r in task_records if r["model_finish"]),
            "actions_avg": statistics.mean(r["actions"] for r in task_records),
            "model_calls_avg": statistics.mean(r["model_calls"] for r in task_records),
            "recoveries": sum(r["recoveries"] for r in task_records),
            "semantic_valid_rate": statistics.mean(
                r["semantic_valid_rate"] for r in task_records if r["semantic_valid_rate"] is not None
            ) if any(r["semantic_valid_rate"] is not None for r in task_records) else None,
            "contract_repairs": sum(r["contract_repair_calls"] for r in task_records),
            "repair_successes": sum(r["contract_repair_successes"] for r in task_records),
            "main_failure": next((r["failure_category"] for r in task_records if not r["pass"]), ""),
        })
    return {"rows": rows, "records": records}


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=None)
    parser.add_argument("--trials", type=int, default=1)
    parser.add_argument("--tasks-file", default=str(ROOT / "benchmarks" / "smoke_tasks.yaml"))
    parser.add_argument("--output-dir", default=str(ROOT / "runtime" / "benchmark_runs" / time.strftime("%Y%m%d_%H%M%S")))
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    with open(args.tasks_file, "r", encoding="utf-8") as f:
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
