from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent.config import load_config
from batch.models import BatchPolicy, FailureCategory, NavigationScope, ResultContract
from batch.orchestrator import BatchOrchestrator
from batch.store import BatchStore
from memory.event_store import EventStore, EventType
from memory.models import TaskState
from memory.task_state import TaskStateStore


TARGETS = [
    "fixture://healthy/alpha",
    "fixture://healthy/beta",
    "fixture://healthy/gamma",
    "fixture://timeout/slow",
    "fixture://auth/login-required",
    "fixture://unsupported/malformed-target",
    "fixture://scope/off-origin",
    "fixture://transient/service-unavailable-once",
]


class FailureMixRunner:
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
        target = work_item["target"]
        task_id = resume_task_id or uuid.uuid4().hex[:12]
        attempt = int(work_item["attempt_count"])
        if "timeout" in target:
            await asyncio.sleep(3.0)
            return task_id
        if "transient" in target and attempt == 1:
            raise RuntimeError("service_unavailable transient model endpoint")
        if "auth" in target:
            _write_blocked_task(config, task_id, target, "Login required to view this page")
            return task_id
        if "unsupported" in target:
            _write_blocked_task(config, task_id, target, "Unsupported malformed target")
            return task_id
        if "scope" in target:
            _write_blocked_task(config, task_id, target, "SCOPE_BLOCKED navigation_scope=same_origin blocked navigation")
            return task_id
        _write_completed_task(config, task_id, target, _result(target))
        return task_id


def _result(target: str) -> dict[str, Any]:
    name = Path(target).name or target.rsplit("/", 1)[-1]
    return {
        "relevant": True,
        "summary": f"healthy fixture {name}",
        "findings": [{
            "type": "fixture",
            "title": name,
            "value": "ok",
            "source_url": target,
            "evidence": f"{name} healthy fixture evidence",
        }],
    }


def _write_completed_task(config, task_id: str, target: str, result: dict[str, Any]) -> None:
    db_path = Path(config.storage.tasks_dir) / task_id / "task.db"
    store = EventStore(db_path)
    try:
        store.create_task(task_id, f"failure mix child {target}", [])
        store.append(task_id, 0, EventType.TASK_CREATED, {"goal": f"failure mix child {target}", "success_criteria": []})
        store.append(task_id, 1, EventType.OBSERVATION, {"url": target, "title": "Fixture", "page_hash": "fixture", "visible_text": [json.dumps(result)[:300]]})
        store.append(task_id, 2, EventType.TASK_COMPLETED, {"result": json.dumps(result), "final_url": target, "final_title": "Fixture", "final_text_excerpt": json.dumps(result)[:500]})
        TaskStateStore(store).save(TaskState(task_id=task_id, current_step=2, status="completed", last_event_id=store.max_event_id(task_id)))
    finally:
        store.close()


def _write_blocked_task(config, task_id: str, target: str, reason: str) -> None:
    db_path = Path(config.storage.tasks_dir) / task_id / "task.db"
    store = EventStore(db_path)
    try:
        store.create_task(task_id, f"failure mix child {target}", [])
        store.append(task_id, 0, EventType.TASK_CREATED, {"goal": f"failure mix child {target}", "success_criteria": []})
        store.append(task_id, 1, EventType.OBSERVATION, {"url": target, "title": "Blocked", "page_hash": "fixture", "visible_text": [reason]})
        store.append(task_id, 2, EventType.TASK_BLOCKED, {"reason": reason})
        TaskStateStore(store).save(TaskState(task_id=task_id, current_step=2, status="blocked", blocked_reason=reason, last_event_id=store.max_event_id(task_id)))
    finally:
        store.close()


async def run(output_dir: Path) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    config = load_config(None)
    config.storage.runtime_dir = str(output_dir / "runtime")
    config.storage.tasks_dir = str(output_dir / "runtime" / "tasks")
    config.browser.user_data_dir = str(output_dir / "runtime" / "tasks")
    config.logging.dir = str(output_dir / "runtime" / "logs")
    policy = BatchPolicy(
        continue_on_failure=True,
        work_item_max_attempts=2,
        max_steps_per_item=3,
        max_seconds_per_item=1.0,
        read_only=True,
        navigation_scope=NavigationScope.SAME_ORIGIN,
    )
    contract = ResultContract(name="generic", description="Failure-mix fixture result.")
    batch_id = "failure_mix"
    store = BatchStore.for_batch_dir(output_dir / "runtime" / "batches" / batch_id)
    try:
        store.create_batch("Phase 5 failure-mix validation", TARGETS, contract, policy, batch_id=batch_id)
        started = time.monotonic()
        final = await BatchOrchestrator(config, store, batch_id, policy, contract, runner=FailureMixRunner()).run()
        duration = time.monotonic() - started
        items = [dict(row) for row in store.items(batch_id)]
        retry_events = [json.loads(row["payload"]) for row in store.events(batch_id) if row["type"] == "WORK_ITEM_RETRY_SCHEDULED"]
        by_category: dict[str, int] = {}
        for item in items:
            category = item.get("failure_category") or "NONE"
            by_category[category] = by_category.get(category, 0) + 1
        summary = {
            "duration_s": duration,
            "status": final["status"],
            "completed": final["completed"],
            "failed": final["failed"],
            "blocked": final["blocked"],
            "healthy_completed": sum(1 for item in items if "healthy" in item["target"] and item["status"] == "completed"),
            "transient_completed": sum(1 for item in items if "transient" in item["target"] and item["status"] == "completed"),
            "timeout_failed_final": sum(1 for item in items if "timeout" in item["target"] and item["status"] == "failed_final" and item["failure_category"] == FailureCategory.TIMEOUT.value),
            "auth_blocked": sum(1 for item in items if "auth" in item["target"] and item["status"] == "blocked" and item["failure_category"] == FailureCategory.AUTH_REQUIRED.value),
            "unsupported_blocked": sum(1 for item in items if "unsupported" in item["target"] and item["status"] == "blocked" and item["failure_category"] == FailureCategory.UNSUPPORTED_PAGE.value),
            "scope_blocked": sum(1 for item in items if "scope" in item["target"] and item["status"] == "blocked" and item["failure_category"] == FailureCategory.SCOPE_BLOCKED.value),
            "retry_events": len(retry_events),
            "retry_categories": sorted(payload["failure_category"] for payload in retry_events),
            "attempts_by_target": {item["target"]: item["attempt_count"] for item in items},
            "category_counts": by_category,
            "items": items,
        }
        (output_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
        print(json.dumps(summary, indent=2))
        return summary
    finally:
        store.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", default=str(ROOT / "runtime" / "benchmark_runs" / "phase5_failure_mix"))
    args = parser.parse_args()
    asyncio.run(run(Path(args.output_dir)))


if __name__ == "__main__":
    main()
