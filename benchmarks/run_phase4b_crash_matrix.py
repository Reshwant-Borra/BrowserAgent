from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
from pathlib import Path
from typing import Any

from run_smoke_benchmarks import _read_jsonl, _start_fixture_server

ROOT = Path(__file__).resolve().parent.parent
WORKER = ROOT / "benchmarks" / "_phase4b_crash_worker.py"


def decision(action: str, target: int | None = None, params: dict | None = None,
             expected_result: dict | None = None) -> str:
    return json.dumps({
        "action": action,
        "target": target,
        "params": params or {},
        "expected_result": expected_result or {},
        "confidence": 0.9,
    })


def config_script(base_url: str, steps: int, variant: str) -> list[str]:
    url = f"{base_url}/long_config.html?variant={variant}&steps={steps}"
    script = [decision("open_url", params={"url": url}, expected_result={"page_contains": "Requirements"})]
    for step in range(2, steps + 1):
        script.append(decision("click", target=1, expected_result={"page_contains": f"Step {step} of {steps}"}))
    values = {
        "alpha": ("Advanced", "North", "AX-47"),
        "beta": ("Balanced", "West", "BX-19"),
        "gamma": ("Advanced", "South", "GX-22"),
    }[variant]
    script.extend([
        decision("select", target=1, params={"value": values[0]}),
        decision("select", target=2, params={"value": values[1]}),
        decision("type", target=3, params={"text": values[2]}),
        decision("click", target=4, expected_result={"page_contains": "Configuration saved"}),
        decision("finish", params={"result": "Configuration saved"}),
    ])
    return script


def research_script(base_url: str, steps: int, variant: str) -> list[str]:
    url = f"{base_url}/long_research.html?variant={variant}&steps={steps}"
    facts = {
        "alpha": ("Atlas", "Copper", "Delta"),
        "beta": ("Beacon", "Ivory", "Harbor"),
        "gamma": ("Cinder", "Jade", "Orbit"),
    }[variant]
    script = [decision("open_url", params={"url": url}, expected_result={"page_contains": "Fact 1"})]
    for step in range(2, steps + 1):
        script.append(decision("click", target=1, expected_result={"page_contains": f"Step {step} of {steps}"}))
    combined = " ".join(facts)
    script.extend([
        decision("type", target=1, params={"text": combined}),
        decision("click", target=2, expected_result={"page_contains": f"Combined result: {combined}"}),
        decision("finish", params={"result": "Combined result confirmed"}),
    ])
    return script


def download_script(base_url: str, steps: int, variant: str) -> list[str]:
    url = f"{base_url}/long_download.html?variant={variant}&steps={steps}"
    target = {"alpha": 1, "beta": 2, "gamma": 3}[variant]
    artifact = {
        "alpha": "release-notes-v7.txt",
        "beta": "audit-bundle-q3.txt",
        "gamma": "dataset-manifest-r12.txt",
    }[variant]
    script = [decision("open_url", params={"url": url}, expected_result={"page_contains": "required artifact"})]
    for step in range(2, steps + 1):
        script.append(decision("click", target=1, expected_result={"page_contains": f"Step {step} of {steps}"}))
    script.extend([
        decision("download", target=target, expected_result={"page_contains": artifact}),
        decision("finish", params={"result": f"Downloaded {artifact}"}),
    ])
    return script


def run_worker(job: dict[str, Any], job_path: Path, timeout: int = 120) -> int:
    job_path.write_text(json.dumps(job), encoding="utf-8")
    proc = subprocess.run([sys.executable, str(WORKER), str(job_path)], timeout=timeout)
    return proc.returncode


def reap_orphaned_browser_processes(profile_marker: str) -> None:
    try:
        import psutil
    except ImportError:
        return
    marker = profile_marker.replace("\\", "/").lower()
    for proc in psutil.process_iter(["pid", "cmdline"]):
        try:
            cmdline = " ".join(proc.info["cmdline"] or []).replace("\\", "/").lower()
            if marker in cmdline:
                proc.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass


def load_events(db_path: Path, task_id: str):
    from memory.event_store import EventStore

    store = EventStore(db_path)
    try:
        return store, store.all_events(task_id)
    except Exception:
        store.close()
        raise


def evaluate_case(output_dir: Path, name: str, job_base: dict[str, Any], action_count: int,
                  kill_fraction: float, criteria: list[str]) -> dict[str, Any]:
    from memory.event_store import EventStore, EventType
    from memory.replay import replay_task
    from memory.task_memory import TaskMemoryStore
    from memory.task_state import TaskStateStore

    case_dir = output_dir / f"{name}_{int(kill_fraction * 100)}"
    tasks_dir = case_dir / "tasks"
    logs_dir = case_dir / "logs"
    task_id_file = case_dir / "task_id.txt"
    job_path = case_dir / "job.json"
    case_dir.mkdir(parents=True, exist_ok=True)

    kill_after = max(2, min(action_count - 3, math.floor(action_count * kill_fraction)))
    start_job = {
        **job_base,
        "mode": "start",
        "tasks_dir": str(tasks_dir),
        "logs_dir": str(logs_dir),
        "goal": f"Phase 4B crash matrix: {name}",
        "criteria": criteria,
        "task_id_file": str(task_id_file),
        "die_after_action_step": kill_after,
        "max_steps": action_count + 20,
    }
    start_code = run_worker(start_job, job_path)
    if start_code != 137:
        raise AssertionError(f"{name} {kill_fraction}: expected crash 137, got {start_code}")

    task_id = task_id_file.read_text(encoding="utf-8").strip()
    db_path = tasks_dir / task_id / "task.db"
    reap_orphaned_browser_processes(str(tasks_dir / task_id))

    resume_job = {
        **job_base,
        "mode": "resume",
        "tasks_dir": str(tasks_dir),
        "logs_dir": str(logs_dir),
        "task_id": task_id,
        "max_steps": action_count + 30,
    }
    resume_code = run_worker(resume_job, job_path)
    if resume_code != 0:
        raise AssertionError(f"{name} {kill_fraction}: resume failed with {resume_code}")

    event_store = EventStore(db_path)
    try:
        events = event_store.all_events(task_id)
        state = TaskStateStore(event_store).load(task_id)
        replayed = replay_task(task_id, events)
        memory_store = TaskMemoryStore(event_store)
        memory_store.rebuild(task_id, events)
        active_facts = memory_store.active_facts(task_id, token_budget=500)
        summary = memory_store.compact_if_needed(
            task_id,
            event_store.all_events(task_id),
            keep_last_steps=5,
            summary_token_budget=500,
            force=True,
        )
        metrics = _read_jsonl(logs_dir / f"{task_id}.metrics.jsonl")
        model_calls = [m for m in metrics if m.get("event") == "model_call"]
        retrieved_calls = sum(1 for m in model_calls if m.get("retrieved_memory_count", 0) > 0)
        prompt_max = max((m.get("prompt_tokens") or m.get("total_estimated_prompt_tokens") or 0 for m in model_calls), default=0)
        duplicate_consequential = _duplicate_consequential_actions(events)
        result = {
            "case": name,
            "kill_fraction": kill_fraction,
            "kill_after_action": kill_after,
            "task_id": task_id,
            "status": state.status,
            "replay_status": replayed.status,
            "pending_action_cleared": state.pending_action_intent is None,
            "active_fact_count": len(active_facts),
            "summary_present": summary is not None and bool(summary.summary),
            "retrieved_memory_calls": retrieved_calls,
            "prompt_tokens_max": prompt_max,
            "duplicate_consequential": duplicate_consequential,
            "events": len(events),
            "pass": (
                state.status == "completed"
                and replayed.status == "completed"
                and state.pending_action_intent is None
                and len(active_facts) > 0
                and summary is not None
                and retrieved_calls > 0
                and not duplicate_consequential
            ),
        }
    finally:
        event_store.close()
    (case_dir / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


def _duplicate_consequential_actions(events) -> bool:
    from memory.event_store import EventType

    seen: set[str] = set()
    for event in events:
        if event.type != EventType.ACTION_INTENT:
            continue
        if event.payload.get("risk") != "consequential":
            continue
        fingerprint = event.payload.get("action_fingerprint")
        if fingerprint in seen:
            return True
        seen.add(fingerprint)
    return False


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", default=str(ROOT / "runtime" / "benchmark_runs" / "phase4b_crash_matrix"))
    args = parser.parse_args()
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    server, base_url = _start_fixture_server()
    try:
        cases = {
            "config_beta_44": ({
                "workflow": "config",
                "start_url": f"{base_url}/long_config.html?variant=beta&steps=44",
                "open_expected": "Requirements",
                "values": ["Balanced", "West", "BX-19"],
                "download_target": None,
            }, 49, ["Configuration saved"]),
            "research_beta_38": ({
                "workflow": "research",
                "start_url": f"{base_url}/long_research.html?variant=beta&steps=38",
                "open_expected": "Fact 1",
                "values": ["Beacon", "Ivory", "Harbor"],
                "download_target": None,
            }, 42, []),
            "download_beta_88": ({
                "workflow": "download",
                "start_url": f"{base_url}/long_download.html?variant=beta&steps=88",
                "open_expected": "required artifact",
                "values": ["audit-bundle-q3.txt"],
                "download_target": 2,
            }, 90, []),
        }
        results = []
        for name, (job_base, action_count, criteria) in cases.items():
            for fraction in (0.25, 0.5, 0.75):
                results.append(evaluate_case(output_dir, name, job_base, action_count, fraction, criteria))
        summary = {
            "passes": sum(1 for result in results if result["pass"]),
            "total": len(results),
            "rows": results,
        }
        (output_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
        print(json.dumps(summary, indent=2))
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
