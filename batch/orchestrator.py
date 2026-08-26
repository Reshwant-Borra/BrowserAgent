from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any, Protocol

from agent.config import AppConfig
from batch.models import BatchEventType, BatchPolicy, FailureCategory, ResultContract, SessionMode, WorkItemStatus
from batch.policies import classify_child_failure, finding_dedupe_key
from batch.store import BatchStore
from memory.event_store import Event, EventStore, EventType
from memory.task_state import TaskStateStore


class ChildRunner(Protocol):
    async def run_child(
        self,
        config: AppConfig,
        batch_id: str,
        work_item: dict[str, Any],
        child_goal: str,
        success_criteria: list[str],
        profile_dir: Path | None,
        max_steps: int,
        resume_task_id: str | None = None,
    ) -> str:
        ...


class AgentLoopChildRunner:
    async def run_child(
        self,
        config: AppConfig,
        batch_id: str,
        work_item: dict[str, Any],
        child_goal: str,
        success_criteria: list[str],
        profile_dir: Path | None,
        max_steps: int,
        resume_task_id: str | None = None,
    ) -> str:
        from agent.loop import AgentLoop

        loop = (
            AgentLoop.resume(config, resume_task_id, profile_dir=profile_dir)
            if resume_task_id
            else AgentLoop.create_new(config, child_goal, success_criteria, profile_dir=profile_dir)
        )
        await loop.run(max_steps=max_steps)
        return loop.task_id


class BatchOrchestrator:
    def __init__(
        self,
        config: AppConfig,
        store: BatchStore,
        batch_id: str,
        policy: BatchPolicy,
        result_contract: ResultContract,
        runner: ChildRunner | None = None,
    ):
        self.config = config
        self.store = store
        self.batch_id = batch_id
        self.policy = policy
        self.result_contract = result_contract
        self.runner = runner or AgentLoopChildRunner()
        self.batch_dir = store.db_path.parent

    async def run(self) -> dict[str, Any]:
        started = time.monotonic()
        self.reconcile_running_items()
        while True:
            if self.policy.max_total_seconds and time.monotonic() - started > self.policy.max_total_seconds:
                break
            self.store.requeue_retryable(self.batch_id)
            item = self.store.claim_next_item(self.batch_id, self.policy.worker_id, self.policy.lease_seconds)
            if item is None:
                break
            await self._run_item(dict(item))
        self.store.refresh_job_counts(self.batch_id)
        final = self.synthesize()
        self.store.save_final_result(self.batch_id, final)
        return final

    def reconcile_running_items(self) -> None:
        for row in self.store.running_items(self.batch_id):
            item = dict(row)
            task_id = item.get("browser_task_id")
            if not task_id:
                retryable = int(item["attempt_count"]) < self.policy.work_item_max_attempts
                self.store.fail_item(item["id"], FailureCategory.UNKNOWN.value, "running item had no child task id", retryable)
                continue
            state, events = _load_child_state(self.config, task_id)
            if state is None:
                retryable = int(item["attempt_count"]) < self.policy.work_item_max_attempts
                self.store.fail_item(item["id"], FailureCategory.UNKNOWN.value, f"child task {task_id} not found", retryable)
                continue
            if state.status == "completed":
                result_id = self._persist_child_result(item, task_id, events)
                self.store.complete_item(item["id"], result_id)
                self.store.append_event(
                    self.batch_id,
                    item["id"],
                    BatchEventType.WORK_ITEM_RECONCILED,
                    {"browser_task_id": task_id, "status": "completed"},
                )
            elif state.status == "blocked":
                category = classify_child_failure(events, state.status, state.blocked_reason)
                self.store.block_item(item["id"], category.value, state.blocked_reason or "child task blocked")
            else:
                retryable = int(item["attempt_count"]) < self.policy.work_item_max_attempts
                self.store.fail_item(item["id"], FailureCategory.UNKNOWN.value, "stale running child task", retryable)

    async def _run_item(self, item: dict[str, Any]) -> None:
        profile_dir = self._profile_dir(item)
        task_id = item.get("browser_task_id")
        child_goal = self._child_goal(item)
        try:
            child_task_id = await asyncio.wait_for(
                self.runner.run_child(
                    self.config,
                    self.batch_id,
                    item,
                    child_goal,
                    self._success_criteria(),
                    profile_dir,
                    self.policy.max_steps_per_item,
                    resume_task_id=task_id,
                ),
                timeout=self.policy.max_seconds_per_item,
            )
            self.store.set_item_browser_task(item["id"], child_task_id)
            state, events = _load_child_state(self.config, child_task_id)
            if state is not None and state.status == "completed":
                result_id = self._persist_child_result(item, child_task_id, events)
                self.store.complete_item(item["id"], result_id)
            elif state is not None and state.status == "blocked":
                category = classify_child_failure(events, state.status, state.blocked_reason)
                self.store.block_item(item["id"], category.value, state.blocked_reason or "child task blocked")
            else:
                category = classify_child_failure(events if state else [], state.status if state else "unknown")
                retryable = int(item["attempt_count"]) < self.policy.work_item_max_attempts
                self.store.fail_item(item["id"], category.value, "child task did not complete within step budget", retryable)
        except asyncio.TimeoutError:
            retryable = int(item["attempt_count"]) < self.policy.work_item_max_attempts
            self.store.fail_item(item["id"], FailureCategory.TIMEOUT.value, "per-item time budget expired", retryable)
        except Exception as exc:
            retryable = int(item["attempt_count"]) < self.policy.work_item_max_attempts
            self.store.fail_item(item["id"], FailureCategory.UNKNOWN.value, str(exc), retryable)
            if not self.policy.continue_on_failure:
                raise

    def _persist_child_result(self, item: dict[str, Any], task_id: str, events: list[Event]) -> int:
        parsed = _extract_structured_result(events, item["target"], task_id)
        dedupe_key = None
        findings = parsed["structured_data"].get("findings") or []
        if findings:
            dedupe_key = finding_dedupe_key(findings[0], parsed.get("final_url") or item["target"])
        return self.store.upsert_result(
            self.batch_id,
            item["id"],
            item["target"],
            "completed",
            parsed["summary"],
            parsed["structured_data"],
            item["target"],
            parsed.get("final_url"),
            parsed["evidence"],
            task_id,
            parsed["source_event_ids"],
            dedupe_key,
        )

    def synthesize(self) -> dict[str, Any]:
        self.store.append_event(self.batch_id, None, BatchEventType.SYNTHESIS_STARTED, {})
        rows = [dict(r) for r in self.store.results(self.batch_id)]
        deduped: dict[str, dict[str, Any]] = {}
        raw_findings = 0
        for row in rows:
            data = json.loads(row["structured_data"])
            for finding in data.get("findings", []):
                raw_findings += 1
                key = finding_dedupe_key(finding, row.get("final_url") or row.get("source_url"))
                provenance = {
                    "result_id": row["id"],
                    "work_item_id": row["work_item_id"],
                    "source_url": row["source_url"],
                    "final_url": row["final_url"],
                    "browser_task_id": row["browser_task_id"],
                    "evidence": json.loads(row["evidence"])[:3],
                }
                if key not in deduped:
                    deduped[key] = {"finding": finding, "provenance": [provenance]}
                else:
                    deduped[key]["provenance"].append(provenance)
        failures = [
            dict(item) for item in self.store.items(self.batch_id)
            if item["status"] in {WorkItemStatus.FAILED_FINAL.value, WorkItemStatus.BLOCKED.value}
        ]
        progress = self.store.progress(self.batch_id)
        return {
            "batch_id": self.batch_id,
            "status": progress["status"],
            "goal": progress["goal"],
            "item_count": progress["item_count"],
            "completed": progress["completed"],
            "failed": progress["failed"],
            "blocked": progress["blocked"],
            "duplicate_inputs": progress["duplicates"],
            "raw_results": len(rows),
            "raw_findings": raw_findings,
            "deduplicated_findings": len(deduped),
            "findings": list(deduped.values()),
            "failures": [
                {
                    "work_item_id": item["id"],
                    "target": item["target"],
                    "status": item["status"],
                    "failure_category": item["failure_category"],
                    "last_error": item["last_error"],
                }
                for item in failures
            ],
        }

    def export_json(self, destination: Path) -> None:
        payload = {
            "job": dict(self.store.get_job(self.batch_id)),
            "items": [dict(r) for r in self.store.items(self.batch_id)],
            "results": [dict(r) for r in self.store.results(self.batch_id)],
            "final": json.loads(self.store.get_job(self.batch_id)["final_result"] or "{}"),
        }
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def _child_goal(self, item: dict[str, Any]) -> str:
        target = item["target"]
        fields = ", ".join(self.result_contract.required_fields) or "relevance, findings, evidence"
        return (
            f"Open this target page and determine whether it contains information relevant to the batch goal.\n"
            f"Batch goal: {self.store.get_job(self.batch_id)['goal']}\n"
            f"Target: {target}\n"
            f"Required result contract: {self.result_contract.description}\n"
            f"Required fields: {fields}.\n"
            "Return concise structured findings with evidence in the finish result. "
            "Do not inspect unrelated websites unless navigation within this target site is needed."
        )

    def _success_criteria(self) -> list[str]:
        return []

    def _profile_dir(self, item: dict[str, Any]) -> Path | None:
        if self.policy.session_mode == SessionMode.SHARED:
            return self.batch_dir / "browser_profile"
        return None


def _load_child_state(config: AppConfig, task_id: str):
    db_path = Path(config.storage.tasks_dir) / task_id / "task.db"
    if not db_path.exists():
        return None, []
    store = EventStore(db_path)
    try:
        state = TaskStateStore(store).load(task_id)
        events = store.all_events(task_id)
        return state, events
    finally:
        store.close()


def _extract_structured_result(events: list[Event], target: str, task_id: str) -> dict[str, Any]:
    completed = next((e for e in reversed(events) if e.type == EventType.TASK_COMPLETED), None)
    observations = [e for e in events if e.type == EventType.OBSERVATION]
    final_url = completed.payload.get("final_url") if completed else (observations[-1].payload.get("url") if observations else target)
    result_text = completed.payload.get("result", "") if completed else ""
    structured: dict[str, Any]
    try:
        parsed = json.loads(result_text)
        structured = parsed if isinstance(parsed, dict) else {"relevant": bool(parsed), "findings": []}
    except json.JSONDecodeError:
        structured = {"relevant": bool(result_text.strip()), "summary": result_text, "findings": []}
    structured.setdefault("findings", [])
    summary = structured.get("summary") or result_text or "Completed without a textual summary."
    evidence_text = completed.payload.get("final_text_excerpt", "") if completed else ""
    evidence = []
    if evidence_text:
        evidence.append({
            "field": "summary",
            "value": summary,
            "evidence": evidence_text[:500],
            "source_url": final_url,
            "source_event_id": completed.id,
            "browser_task_id": task_id,
        })
    for finding in structured.get("findings", []):
        if isinstance(finding, dict) and finding.get("evidence"):
            evidence.append({
                "field": finding.get("type") or finding.get("field") or "finding",
                "value": finding.get("value") or finding.get("title") or finding.get("assignment"),
                "evidence": str(finding.get("evidence"))[:500],
                "source_url": finding.get("source_url") or final_url,
                "source_event_id": completed.id if completed else None,
                "browser_task_id": task_id,
            })
    return {
        "summary": str(summary),
        "structured_data": structured,
        "final_url": final_url,
        "evidence": evidence[:20],
        "source_event_ids": [completed.id] if completed and completed.id is not None else [],
    }
