from __future__ import annotations

import argparse
import asyncio
import json
import time
import uuid
from pathlib import Path
from typing import Any

import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent.config import load_config
from batch.models import BatchPolicy, ResultContract
from batch.orchestrator import BatchOrchestrator
from batch.store import BatchStore
from memory.event_store import EventStore, EventType
from memory.models import TaskState
from memory.task_state import TaskStateStore
from tests.fixtures.multisite.generate_multisite import generate_assignment_fixture, generate_research_fixture


class FixtureRunner:
    def __init__(self, truth: dict[str, Any], fixture_kind: str):
        self.truth = truth
        self.fixture_kind = fixture_kind
        self.calls = 0

    async def run_child(self, config, batch_id, work_item, child_goal, success_criteria, profile_dir, max_steps, resume_task_id=None):
        self.calls += 1
        task_id = resume_task_id or uuid.uuid4().hex[:12]
        target_name = Path(work_item["target"]).name
        if self.fixture_kind == "assignment":
            findings = [
                {
                    "type": "assignment",
                    "title": row["assignment"],
                    "course": row["course"],
                    "value": row["due_date"],
                    "status": row["status"],
                    "evidence": row["evidence"],
                    "source_url": work_item["target"],
                }
                for row in self.truth["assignments"]
                if row["source"] == target_name
            ]
        else:
            findings = [
                {"type": "research_fact", "fact": row["fact"], "value": row["fact"], "evidence": row["evidence"], "source_url": work_item["target"]}
                for row in self.truth["facts"]
                if row["source"] == target_name
            ]
        result = {"relevant": bool(findings), "summary": f"{len(findings)} findings", "findings": findings}
        _write_completed_task(config, task_id, work_item["target"], result)
        return task_id


async def run_size(kind: str, count: int, output_dir: Path) -> dict[str, Any]:
    fixture_dir = output_dir / "site"
    truth = generate_assignment_fixture(fixture_dir, count) if kind == "assignment" else generate_research_fixture(fixture_dir, count)
    targets = [str(fixture_dir / target) for target in truth["targets"]]
    config = load_config(None)
    config.storage.runtime_dir = str(output_dir / "runtime")
    config.storage.tasks_dir = str(output_dir / "runtime" / "tasks")
    config.browser.user_data_dir = str(output_dir / "runtime" / "tasks")
    config.logging.dir = str(output_dir / "runtime" / "logs")
    policy = BatchPolicy(max_steps_per_item=5, max_seconds_per_item=30)
    contract = ResultContract(
        name=kind,
        required_fields=["course", "assignment", "due_date", "status", "evidence"] if kind == "assignment" else ["relevant", "fact", "evidence"],
    )
    batch_id = f"{kind}_{count}"
    store = BatchStore.for_batch_dir(output_dir / "runtime" / "batches" / batch_id)
    try:
        store.create_batch(f"Phase 5 {kind} sweep", targets, contract, policy, batch_id=batch_id)
        started = time.monotonic()
        final = await BatchOrchestrator(config, store, batch_id, policy, contract, FixtureRunner(truth, kind)).run()
        duration = time.monotonic() - started
        return {
            "kind": kind,
            "targets": count,
            "completed": final["completed"],
            "failed": final["failed"],
            "blocked": final["blocked"],
            "raw_findings": final["raw_findings"],
            "deduplicated_findings": final["deduplicated_findings"],
            "duration_s": duration,
            "batch_id": batch_id,
        }
    finally:
        store.close()


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


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", default=str(ROOT / "runtime" / "benchmark_runs" / "phase5_queue"))
    parser.add_argument("--kind", choices=["assignment", "research"], default="assignment")
    parser.add_argument("--sizes", nargs="*", type=int, default=[10, 25, 50, 100])
    args = parser.parse_args()
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    for size in args.sizes:
        rows.append(await run_size(args.kind, size, output_dir / f"{args.kind}_{size}"))
    summary = {"rows": rows}
    (output_dir / f"{args.kind}_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
