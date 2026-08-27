from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent.config import load_config
from batch.models import BatchPolicy, ResultContract
from batch.orchestrator import BatchOrchestrator
from batch.store import BatchStore
from memory.event_store import EventStore, EventType
from memory.models import TaskState
from memory.task_state import TaskStateStore
from tests.fixtures.multisite.generate_multisite import generate_assignment_fixture


class SlowFixtureRunner:
    def __init__(self, marker_path: Path, delay_s: float):
        self.marker_path = marker_path
        self.delay_s = delay_s

    async def run_child(
        self,
        config,
        batch_id,
        work_item,
        child_goal,
        success_criteria,
        profile_dir,
        max_steps,
        resume_task_id=None,
        runtime_policy=None,
    ):
        task_id = resume_task_id or uuid.uuid4().hex[:12]
        self._mark({"event": "child_started", "work_item_id": work_item["id"], "ordinal": work_item["ordinal"], "task_id": task_id})
        await asyncio.sleep(self.delay_s)
        result = {
            "relevant": True,
            "summary": f"completed item {work_item['ordinal']}",
            "findings": [{
                "type": "assignment",
                "course": "Crash",
                "title": f"Item {work_item['ordinal']}",
                "due_date": "Sep 30",
                "status": "upcoming",
                "actionable": True,
                "source_url": work_item["target"],
                "evidence": f"Item {work_item['ordinal']} due Sep 30",
            }],
        }
        _write_completed_task(config, task_id, work_item["target"], result)
        self._mark({"event": "child_completed", "work_item_id": work_item["id"], "ordinal": work_item["ordinal"], "task_id": task_id})
        return task_id

    def _mark(self, row: dict[str, Any]) -> None:
        self.marker_path.parent.mkdir(parents=True, exist_ok=True)
        with self.marker_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({**row, "time": time.time()}) + "\n")


def _write_completed_task(config, task_id: str, target: str, result: dict[str, Any]) -> None:
    db_path = Path(config.storage.tasks_dir) / task_id / "task.db"
    store = EventStore(db_path)
    try:
        store.create_task(task_id, f"fixture child {target}", [])
        store.append(task_id, 0, EventType.TASK_CREATED, {"goal": f"fixture child {target}", "success_criteria": []})
        store.append(task_id, 1, EventType.OBSERVATION, {"url": target, "title": "Fixture", "page_hash": "fixture", "visible_text": [json.dumps(result)[:300]]})
        store.append(task_id, 2, EventType.TASK_COMPLETED, {"result": json.dumps(result), "final_url": target, "final_title": "Fixture", "final_text_excerpt": json.dumps(result)[:500]})
        TaskStateStore(store).save(TaskState(task_id=task_id, current_step=2, status="completed", last_event_id=store.max_event_id(task_id)))
    finally:
        store.close()


def _config(output_dir: Path):
    config = load_config(None)
    config.storage.runtime_dir = str(output_dir / "runtime")
    config.storage.tasks_dir = str(output_dir / "runtime" / "tasks")
    config.browser.user_data_dir = str(output_dir / "runtime" / "tasks")
    config.logging.dir = str(output_dir / "runtime" / "logs")
    return config


def _contract() -> ResultContract:
    return ResultContract(
        name="assignment",
        description="Crash-resume assignment fixture.",
        required_fields=["course", "title", "due_date", "status", "actionable", "source_url", "evidence"],
    )


async def _worker(args: argparse.Namespace) -> None:
    output_dir = Path(args.output_dir)
    config = _config(output_dir)
    policy = BatchPolicy(work_item_max_attempts=2, max_steps_per_item=3, max_seconds_per_item=30)
    contract = _contract()
    batch_dir = output_dir / "runtime" / "batches" / args.batch_id
    store = BatchStore.for_batch_dir(batch_dir)
    try:
        if args.create:
            fixture_dir = output_dir / "site"
            truth = generate_assignment_fixture(fixture_dir, args.targets)
            targets = [str(fixture_dir / target) for target in truth["targets"]]
            store.create_batch("Phase 5 crash-resume validation", targets, contract, policy, batch_id=args.batch_id)
        final = await BatchOrchestrator(
            config,
            store,
            args.batch_id,
            policy,
            contract,
            runner=SlowFixtureRunner(output_dir / "markers.jsonl", args.child_delay),
        ).run()
        (output_dir / "worker_final.json").write_text(json.dumps(final, indent=2), encoding="utf-8")
    finally:
        store.close()


def _progress(db_path: Path, batch_id: str) -> dict[str, Any]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        ready = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'batch_work_items'"
        ).fetchone()
        if ready is None:
            raise RuntimeError("batch schema is not ready")
        counts = {row["status"]: row["count"] for row in conn.execute(
            "SELECT status, COUNT(*) AS count FROM batch_work_items WHERE batch_job_id = ? GROUP BY status",
            (batch_id,),
        )}
        running = conn.execute(
            "SELECT id, ordinal, target, browser_task_id, attempt_count FROM batch_work_items WHERE batch_job_id = ? AND status = 'running' ORDER BY ordinal",
            (batch_id,),
        ).fetchall()
        results = conn.execute("SELECT COUNT(*) AS count FROM batch_results WHERE batch_job_id = ?", (batch_id,)).fetchone()["count"]
        items = conn.execute("SELECT id, ordinal, status, result_id, attempt_count FROM batch_work_items WHERE batch_job_id = ? ORDER BY ordinal", (batch_id,)).fetchall()
        return {
            "completed": counts.get("completed", 0),
            "running": [dict(r) for r in running],
            "pending": counts.get("pending", 0),
            "failed_retryable": counts.get("failed_retryable", 0),
            "failed_final": counts.get("failed_final", 0),
            "blocked": counts.get("blocked", 0),
            "persisted_results": results,
            "items": [dict(r) for r in items],
        }
    finally:
        conn.close()


def _read_markers(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _kill(proc: subprocess.Popen) -> None:
    if os.name == "nt":
        proc.kill()
    else:
        os.kill(proc.pid, signal.SIGKILL)


def _run_case(percent: int, targets: int, base_output: Path, child_delay: float) -> dict[str, Any]:
    output_dir = base_output / f"crash_{percent}"
    output_dir.mkdir(parents=True, exist_ok=True)
    batch_id = f"crash_{percent}"
    kill_after = max(1, int(targets * percent / 100))
    db_path = output_dir / "runtime" / "batches" / batch_id / "batch.db"
    cmd = [sys.executable, str(Path(__file__)), "--worker", "--create", "--output-dir", str(output_dir), "--batch-id", batch_id, "--targets", str(targets), "--child-delay", str(child_delay)]
    proc = subprocess.Popen(cmd, cwd=str(ROOT))
    before = None
    try:
        deadline = time.time() + max(120, targets * child_delay * 4)
        while time.time() < deadline:
            if db_path.exists():
                try:
                    progress = _progress(db_path, batch_id)
                except RuntimeError:
                    time.sleep(0.05)
                    continue
                if progress["completed"] >= kill_after and len(progress["running"]) == 1:
                    before = {k: v for k, v in progress.items() if k != "items"}
                    break
            if proc.poll() is not None:
                raise RuntimeError(f"worker exited before kill point with code {proc.returncode}")
            time.sleep(0.05)
        if before is None:
            raise TimeoutError(f"did not reach {percent}% kill point")
        _kill(proc)
        proc.wait(timeout=10)
    finally:
        if proc.poll() is None:
            _kill(proc)
    pre_completed_ids = {row["id"] for row in _progress(db_path, batch_id)["items"] if row["status"] == "completed"}
    running_ids = {row["id"] for row in before["running"]}

    resume_cmd = [sys.executable, str(Path(__file__)), "--worker", "--output-dir", str(output_dir), "--batch-id", batch_id, "--targets", str(targets), "--child-delay", str(child_delay)]
    resumed = subprocess.run(resume_cmd, cwd=str(ROOT), timeout=max(120, targets * child_delay * 6))
    after = _progress(db_path, batch_id)
    markers = _read_markers(output_dir / "markers.jsonl")
    starts: dict[int, int] = {}
    for marker in markers:
        if marker.get("event") == "child_started":
            starts[int(marker["work_item_id"])] = starts.get(int(marker["work_item_id"]), 0) + 1
    completed_reruns = sum(1 for item_id in pre_completed_ids if starts.get(item_id, 0) != 1)
    duplicate_results = after["persisted_results"] - len({row["id"] for row in after["items"] if row["result_id"]})
    lost_results = sum(1 for row in after["items"] if row["status"] == "completed" and not row["result_id"])
    running_reconciled = all(
        row["status"] == "completed" and row["result_id"] and row["attempt_count"] >= 2
        for row in after["items"] if row["id"] in running_ids
    )
    final = json.loads((output_dir / "worker_final.json").read_text(encoding="utf-8"))
    return {
        "percent": percent,
        "targets": targets,
        "kill_after_completed": kill_after,
        "process_exit_code": proc.returncode,
        "resume_exit_code": resumed.returncode,
        "before_kill": before,
        "after_resume": {k: v for k, v in after.items() if k != "items"},
        "completed_items_rerun": completed_reruns,
        "duplicate_results": max(0, duplicate_results),
        "lost_results": lost_results,
        "running_item_reconciled": running_reconciled,
        "pending_continued": after["completed"] == targets,
        "final_status": final["status"],
        "final_completed": final["completed"],
        "final_failed": final["failed"],
        "final_blocked": final["blocked"],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--create", action="store_true")
    parser.add_argument("--output-dir", default=str(ROOT / "runtime" / "benchmark_runs" / "phase5_crash_resume"))
    parser.add_argument("--batch-id", default="crash")
    parser.add_argument("--targets", type=int, default=50)
    parser.add_argument("--child-delay", type=float, default=0.25)
    parser.add_argument("--percents", nargs="*", type=int, default=[25, 50, 75])
    args = parser.parse_args()
    if args.worker:
        asyncio.run(_worker(args))
        return
    base_output = Path(args.output_dir)
    base_output.mkdir(parents=True, exist_ok=True)
    rows = [_run_case(percent, args.targets, base_output, args.child_delay) for percent in args.percents]
    summary = {"rows": rows}
    (base_output / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
