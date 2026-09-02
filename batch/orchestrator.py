from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional, Protocol

from agent.config import AppConfig
from agent.runtime_policy import BatchRuntimePolicy, NavigationScopePolicy
from batch.models import BatchEventType, BatchPolicy, FailureCategory, ResultContract, SessionMode, WorkItemStatus
from batch.policies import classify_child_failure, finding_dedupe_key
from batch.result_quality import normalize_structured_result
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
        runtime_policy: BatchRuntimePolicy | None = None,
        approval_callback: Optional[Callable[[Any, Any], Awaitable[bool]]] = None,
        seed_facts: Optional[dict[str, str]] = None,
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
        runtime_policy: BatchRuntimePolicy | None = None,
        approval_callback: Optional[Callable[[Any, Any], Awaitable[bool]]] = None,
        seed_facts: Optional[dict[str, str]] = None,
    ) -> str:
        from agent.loop import AgentLoop
        from memory.task_memory import TaskMemoryStore

        loop = (
            AgentLoop.resume(config, resume_task_id, profile_dir=profile_dir, runtime_policy=runtime_policy,
                              approval_callback=approval_callback)
            if resume_task_id
            else AgentLoop.create_new(
                config,
                child_goal,
                success_criteria,
                profile_dir=profile_dir,
                runtime_policy=runtime_policy,
                approval_callback=approval_callback,
            )
        )
        # Only meaningful on a fresh task (a resumed task already has whatever active facts
        # survived its own history) — seeds verified cross-step workflow facts (Section 33/34)
        # into the *same* Phase 4B active-fact-constraint machinery already validated for
        # single-task memory (memory/task_memory.py), so a high-confidence verified fact from
        # an earlier workflow step gets the same deterministic contradiction guard as a fact
        # discovered earlier in one task's own history — no second guard mechanism invented.
        if seed_facts and not resume_task_id:
            memory_store = TaskMemoryStore(loop.event_store)
            source_event_id = loop.event_store.max_event_id(loop.task_id) or 0
            for key, value in seed_facts.items():
                normalized_key = key.replace("_", " ").replace("-", " ").strip()
                if not normalized_key or not str(value).strip():
                    continue
                memory_store.write_active_fact(
                    loop.task_id,
                    ("requirement", normalized_key, str(value),
                     f"verified workflow input: {normalized_key} = {value}", 1.0),
                    source_event_id,
                )
        await loop.run(max_steps=max_steps)
        return loop.task_id


class StructuredResultContractError(ValueError):
    """Raised when a batch child completed without valid structured result JSON."""


class BatchOrchestrator:
    def __init__(
        self,
        config: AppConfig,
        store: BatchStore,
        batch_id: str,
        policy: BatchPolicy,
        result_contract: ResultContract,
        runner: ChildRunner | None = None,
        item_completed_callback: Callable[[dict[str, Any]], None] | None = None,
        parent_task_id: str | None = None,
        approval_callback: Optional[Callable[[Any, Any], Awaitable[bool]]] = None,
    ):
        self.config = config
        self.store = store
        self.batch_id = batch_id
        self.policy = policy
        self.result_contract = result_contract
        self.runner = runner or AgentLoopChildRunner()
        self.batch_dir = store.db_path.parent
        self.item_completed_callback = item_completed_callback
        # Set by a general-controller delegate_batch decision (agent/controller.py, Phase 4)
        # so the synthesized result carries which control task it was run on behalf of; every
        # pre-existing caller (ui/jobs.py, benchmarks) leaves this None and gets identical
        # output to before this field existed.
        self.parent_task_id = parent_task_id
        # Phase 5: threaded into every child's own AgentLoop exactly like
        # WorkflowOrchestrator's own approval_callback already is (agent/loop.py's
        # `_request_approval` falls back to a blocking `input()` prompt when this is None —
        # never safe inside an async UI server, so a caller driving batch children from a live
        # UI/general-controller context must supply one). None (every pre-existing caller,
        # since batch items already default to read_only=True) is unchanged.
        self.approval_callback = approval_callback

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
                try:
                    result_id = self._persist_child_result(item, task_id, events)
                    self.store.complete_item(item["id"], result_id)
                    self.store.append_event(
                        self.batch_id,
                        item["id"],
                        BatchEventType.WORK_ITEM_RECONCILED,
                        {"browser_task_id": task_id, "status": "completed"},
                    )
                except StructuredResultContractError as exc:
                    retryable = int(item["attempt_count"]) < self.policy.work_item_max_attempts
                    self.store.fail_item(
                        item["id"],
                        FailureCategory.CONTRACT.value,
                        str(exc),
                        retryable,
                        clear_browser_task=retryable,
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
                    runtime_policy=self._runtime_policy(item),
                    approval_callback=self.approval_callback,
                ),
                timeout=self.policy.max_seconds_per_item,
            )
            self.store.set_item_browser_task(item["id"], child_task_id)
            state, events = _load_child_state(self.config, child_task_id)
            if state is not None and state.status == "completed":
                result_id = self._persist_child_result(item, child_task_id, events)
                self.store.complete_item(item["id"], result_id)
                self._after_item(dict(self.store.get_item(item["id"])))
            elif state is not None and state.status == "blocked":
                category = classify_child_failure(events, state.status, state.blocked_reason)
                self.store.block_item(item["id"], category.value, state.blocked_reason or "child task blocked")
                self._after_item(dict(self.store.get_item(item["id"])))
            else:
                category = classify_child_failure(events if state else [], state.status if state else "unknown")
                retryable = int(item["attempt_count"]) < self.policy.work_item_max_attempts
                self.store.fail_item(item["id"], category.value, "child task did not complete within step budget", retryable)
                self._after_item(dict(self.store.get_item(item["id"])))
        except asyncio.TimeoutError:
            retryable = int(item["attempt_count"]) < self.policy.work_item_max_attempts
            self.store.fail_item(item["id"], FailureCategory.TIMEOUT.value, "per-item time budget expired", retryable)
            self._after_item(dict(self.store.get_item(item["id"])))
        except StructuredResultContractError as exc:
            retryable = int(item["attempt_count"]) < self.policy.work_item_max_attempts
            self.store.fail_item(
                item["id"],
                FailureCategory.CONTRACT.value,
                str(exc),
                retryable,
                clear_browser_task=retryable,
            )
            self._after_item(dict(self.store.get_item(item["id"])))
            if not self.policy.continue_on_failure:
                raise
        except Exception as exc:
            retryable = int(item["attempt_count"]) < self.policy.work_item_max_attempts
            category = classify_child_failure([], "unknown", str(exc))
            self.store.fail_item(item["id"], category.value, str(exc), retryable)
            self._after_item(dict(self.store.get_item(item["id"])))
            if not self.policy.continue_on_failure:
                raise

    def _after_item(self, item: dict[str, Any]) -> None:
        if self.item_completed_callback:
            self.item_completed_callback(item)

    def _persist_child_result(self, item: dict[str, Any], task_id: str, events: list[Event]) -> int:
        parsed = _extract_structured_result(
            events,
            item["target"],
            task_id,
            require_json=_requires_structured_result_json(self.result_contract),
        )
        parsed["structured_data"], parsed["quality"] = normalize_structured_result(
            parsed["structured_data"],
            self.result_contract,
            parsed.get("final_url") or item["target"],
            parsed.get("page_excerpt") or "",
        )
        parsed["evidence"] = _evidence_from_structured(
            parsed["structured_data"],
            parsed.get("summary") or "",
            parsed.get("page_excerpt") or "",
            parsed.get("final_url") or item["target"],
            task_id,
            parsed["source_event_ids"][0] if parsed["source_event_ids"] else None,
        )
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
                if self.result_contract.name == "assignment" and finding.get("actionable") is not True:
                    continue
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
        quality = _aggregate_quality(rows)
        return {
            "batch_id": self.batch_id,
            "parent_task_id": self.parent_task_id,
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
            "result_quality": quality,
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
        field_defs = "; ".join(
            f"{name}: {description}" for name, description in self.result_contract.field_definitions.items()
        )
        contract_details = f"\nRequested field meanings: {field_defs}" if field_defs else ""
        finding_guide = self._finding_field_guide()
        intro = self._target_intro(item, target)
        return (
            f"{intro}\n"
            f"Batch goal: {self.store.get_job(self.batch_id)['goal']}\n"
            f"Target: {target}\n"
            f"Required result contract: {self.result_contract.description}\n"
            f"Required fields: {fields}.{contract_details}\n"
            "When finished, call finish with a short one-sentence plain-text `result` summary, and fill in "
            "the structured `structured_result` field directly (relevant, summary, findings) — it is a real "
            "typed field, never write JSON text into `result`. "
            f"{finding_guide} "
            "Leave structured_result.findings empty when nothing relevant is present. "
            "Do not inspect unrelated websites unless navigation within this target site is needed."
        )

    def _target_intro(self, item: dict[str, Any], target: str) -> str:
        payload = _parse_target_payload(item)
        if payload.get("type") == "open_tab":
            return (
                f"This is an already-open browser tab at {target} (one of several tabs you were asked to "
                "look through) — it is already the active page. Do not navigate away from it or open a new "
                "tab; read its current content directly and determine whether it contains information "
                "relevant to the batch goal."
            )
        return "Open this target page and determine whether it contains information relevant to the batch goal."

    def _finding_field_guide(self) -> str:
        if self.result_contract.name == "assignment":
            return (
                "Each finding should set: type=\"assignment\", course, title, due_date, "
                "status (upcoming|current_incomplete|completed|closed|past_archived|unknown), "
                "actionable (true only if it still requires action), source_url, and evidence (a short exact "
                "page excerpt proving the status). Do not mark completed, submitted, graded, closed, "
                "archived, or historical assignments actionable. If status is not reliable, use "
                "status=unknown and actionable=false."
            )
        if self.result_contract.name == "research":
            fields = ", ".join(
                f for f in self.result_contract.required_fields
                if f not in {"relevant", "source", "source_url", "evidence"}
            )
            return (
                f"Each finding should set: field (one of the requested field names: {fields}), value, "
                "source_url, and evidence (a short exact page excerpt). Add at most one finding per "
                "requested field that was actually found on this page. Do not output generic labels such "
                "as 'Relevant source' as facts."
            )
        return (
            "Each finding should set: type, title, value, source_url, and evidence (a short exact page "
            "excerpt)."
        )

    def _success_criteria(self) -> list[str]:
        return []

    def _profile_dir(self, item: dict[str, Any]) -> Path | None:
        if self.policy.session_mode == SessionMode.SHARED:
            return self.batch_dir / "browser_profile"
        return None

    def _runtime_policy(self, item: dict[str, Any]) -> BatchRuntimePolicy:
        payload = _parse_target_payload(item)
        return BatchRuntimePolicy(
            target_url=item["target"],
            read_only=self.policy.read_only,
            navigation_scope=NavigationScopePolicy(self.policy.navigation_scope.value),
            batch_id=self.batch_id,
            work_item_id=int(item["id"]),
            is_open_tab=payload.get("type") == "open_tab",
        )


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


def _requires_structured_result_json(contract: ResultContract) -> bool:
    return contract.name != "generic" or bool(contract.required_fields) or bool(contract.field_definitions)


def _parse_target_payload(item: dict[str, Any]) -> dict[str, Any]:
    raw = item.get("target_payload")
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _extract_structured_result(
    events: list[Event],
    target: str,
    task_id: str,
    require_json: bool = False,
) -> dict[str, Any]:
    completed = next((e for e in reversed(events) if e.type == EventType.TASK_COMPLETED), None)
    observations = [e for e in events if e.type == EventType.OBSERVATION]
    final_url = completed.payload.get("final_url") if completed else (observations[-1].payload.get("url") if observations else target)
    result_text = completed.payload.get("result", "") if completed else ""
    typed_structured = completed.payload.get("structured_result") if completed else None
    structured: dict[str, Any]
    if isinstance(typed_structured, dict):
        # The model filled the typed FinishAction.structured_result field directly (a real,
        # grammar-constrained object/array) — no JSON-in-a-string to re-parse, and therefore
        # no "not valid JSON" failure mode possible for this path at all.
        structured = dict(typed_structured)
    else:
        try:
            parsed = json.loads(result_text)
            if require_json and not isinstance(parsed, dict):
                raise StructuredResultContractError(
                    f"structured batch result for task {task_id} was JSON but not an object"
                )
            structured = parsed if isinstance(parsed, dict) else {"relevant": bool(parsed), "findings": []}
        except json.JSONDecodeError as exc:
            if require_json:
                raise StructuredResultContractError(
                    f"structured batch result for task {task_id} was not valid JSON: {exc.msg}"
                ) from exc
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
        "page_excerpt": evidence_text,
        "evidence": evidence[:20],
        "source_event_ids": [completed.id] if completed and completed.id is not None else [],
    }


def _aggregate_quality(rows: list[dict[str, Any]]) -> dict[str, Any]:
    totals = {
        "structured_outputs_attempted": 0,
        "schema_valid_results": 0,
        "evidence_backed_findings": 0,
        "unsupported_findings_rejected": 0,
        "status_conflicts": 0,
    }
    for row in rows:
        data = json.loads(row["structured_data"])
        quality = data.get("_quality") or {}
        for key in totals:
            totals[key] += int(quality.get(key) or 0)
    attempted = totals["structured_outputs_attempted"]
    evidence_backed = totals["evidence_backed_findings"]
    rejected = totals["unsupported_findings_rejected"]
    finding_attempts = evidence_backed + rejected
    return {
        **totals,
        "schema_valid_pct": (totals["schema_valid_results"] / attempted * 100.0) if attempted else 100.0,
        "evidence_backed_pct": (evidence_backed / finding_attempts * 100.0) if finding_attempts else 100.0,
    }


def _evidence_from_structured(
    structured: dict[str, Any],
    summary: str,
    page_excerpt: str,
    final_url: str | None,
    task_id: str,
    source_event_id: int | None,
) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    if page_excerpt:
        evidence.append({
            "field": "summary",
            "value": summary,
            "evidence": page_excerpt[:500],
            "source_url": final_url,
            "source_event_id": source_event_id,
            "browser_task_id": task_id,
        })
    for finding in structured.get("findings", []):
        if isinstance(finding, dict) and finding.get("evidence"):
            evidence.append({
                "field": finding.get("field") or finding.get("type") or "finding",
                "value": finding.get("value") or finding.get("title") or finding.get("assignment"),
                "evidence": str(finding.get("evidence"))[:500],
                "source_url": finding.get("source_url") or final_url,
                "source_event_id": source_event_id,
                "browser_task_id": task_id,
            })
    return evidence[:20]
