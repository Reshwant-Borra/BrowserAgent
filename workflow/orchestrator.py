"""Ordered multi-site action workflows (Section 15-17/33-34 of the Phase 5B spec):
site A -> action -> verify -> site B -> action -> verify -> ... -> final report.

Each step is executed by the *existing* AgentLoop engine (same AgentLoopChildRunner reuse
pattern as batch/orchestrator.py's BatchOrchestrator — no second execution engine). What's
new here is purely sequencing: verify-before-advance, persisted step order (never left to
the model to "remember"), and passing structured facts discovered by one step into the
next step's goal.
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from agent.config import AppConfig
from agent.runtime_policy import BatchRuntimePolicy, NavigationScopePolicy
from batch.orchestrator import AgentLoopChildRunner, ChildRunner, _load_child_state
from batch.policies import classify_child_failure
from memory.event_store import Event, EventType
from workflow.models import WorkflowPolicy, WorkflowStepStatus
from workflow.store import WorkflowStore


class WorkflowOrchestrator:
    def __init__(
        self,
        config: AppConfig,
        store: WorkflowStore,
        workflow_id: str,
        policy: WorkflowPolicy,
        runner: ChildRunner | None = None,
        step_completed_callback: Callable[[dict[str, Any]], None] | None = None,
        approval_callback: Optional[Callable[[Any, Any], Awaitable[bool]]] = None,
    ):
        self.config = config
        self.store = store
        self.workflow_id = workflow_id
        self.policy = policy
        self.runner = runner or AgentLoopChildRunner()
        self.workflow_dir = store.db_path.parent
        self.step_completed_callback = step_completed_callback
        self.approval_callback = approval_callback

    async def run(self) -> dict[str, Any]:
        while True:
            step = self.store.next_pending_step(self.workflow_id)
            if step is None:
                break
            blocked = await self._run_step(dict(step))
            if blocked:
                return self._final_result()
        self.store.complete_workflow(self.workflow_id, self._final_result())
        return self._final_result()

    async def _run_step(self, step: dict[str, Any]) -> bool:
        """Returns True if the workflow is now blocked (stop advancing)."""
        self.store.start_step(step["id"])
        facts = self.store.facts_so_far(self.workflow_id, step["ordinal"])
        goal = self._step_goal(step, facts)
        profile_dir = self.workflow_dir / f"step_{step['ordinal']}_browser_profile"
        runtime_policy = BatchRuntimePolicy(
            target_url=step["target"],
            read_only=self.policy.read_only,
            navigation_scope=NavigationScopePolicy(self.policy.navigation_scope.value),
        )
        try:
            child_task_id = await asyncio.wait_for(
                self.runner.run_child(
                    self.config, self.workflow_id, step, goal, [], profile_dir,
                    self.policy.max_steps_per_step, runtime_policy=runtime_policy,
                    approval_callback=self.approval_callback,
                ),
                timeout=self.policy.max_seconds_per_step,
            )
        except asyncio.TimeoutError:
            return self._handle_step_failure(step, "TIMEOUT", "per-step time budget expired")
        except Exception as exc:  # operational failure running the child loop itself
            return self._handle_step_failure(step, "UNKNOWN", str(exc))

        state, events = _load_child_state(self.config, child_task_id)
        if state is None:
            return self._handle_step_failure(step, "UNKNOWN", f"child task {child_task_id} not found")
        if state.status == "blocked":
            category = classify_child_failure(events, state.status, state.blocked_reason)
            return self._handle_step_failure(step, category.value, state.blocked_reason or "child task blocked",
                                              browser_task_id=child_task_id)
        if state.status != "completed":
            return self._handle_step_failure(step, "MAX_STEPS", "child task did not complete within step budget",
                                              browser_task_id=child_task_id)

        parsed = _extract_step_result(events)
        if not parsed.get("verified"):
            return self._handle_step_failure(
                step, "VERIFICATION",
                parsed.get("evidence") or "step finished without verifiable evidence of the requested end-state",
                browser_task_id=child_task_id,
            )

        self.store.complete_step(
            step["id"], child_task_id, parsed.get("summary") or "", parsed.get("facts") or {}
        )
        self._notify(step["id"])
        return False

    def _handle_step_failure(self, step: dict[str, Any], category: str, error: str, browser_task_id: str | None = None) -> bool:
        retryable = category not in {"BLOCKED_CONSEQUENTIAL", "AUTH_REQUIRED", "SCOPE_BLOCKED", "READ_ONLY_BLOCKED"}
        self.store.fail_step(step["id"], f"[{category}] {error}", retryable, self.policy.max_attempts_per_step)
        self._notify(step["id"])
        current = dict(self.store.get_step(step["id"]))
        if current["status"] == WorkflowStepStatus.FAILED.value:
            self.store.block_workflow(self.workflow_id, f"step {step['ordinal']} ({step['target']}): [{category}] {error}")
            return True
        return False

    def _notify(self, step_id: int) -> None:
        if self.step_completed_callback:
            self.step_completed_callback(dict(self.store.get_step(step_id)))

    def _step_goal(self, step: dict[str, Any], facts: dict[str, Any]) -> str:
        facts_block = json.dumps(facts) if facts else "none"
        return (
            f"Open {step['target']} and complete this objective: {step['objective']}\n"
            f"Known facts from previous workflow steps (already verified, use them exactly "
            f"as given rather than re-discovering them): {facts_block}\n"
            "This step's target page is exactly the one given above. Stay on it and complete "
            "the objective there; do not follow a link to another page unless the objective "
            "explicitly requires navigating within this site to complete it.\n"
            "Before finishing, verify the page now actually shows the requested end-state — "
            "do not report success from memory of the action alone.\n"
            "When finished, put only compact JSON in the finish result, using this exact "
            "shape: {\"done\": true|false, \"verified\": true|false, \"summary\": \"...\", "
            "\"evidence\": \"short exact page excerpt proving the verified end-state, or the "
            "reason verification failed\", \"facts\": {\"key\": \"value\"}}. "
            "Set verified=true only when the current page directly shows the objective's "
            "end-state was achieved (e.g. a dropdown now reads the new value, a toggle is now "
            "on). Put any concrete value this step discovered that a later step might need "
            "into facts (empty object if none). The whole finish result must be one valid JSON "
            "object: never put a literal double-quote character inside a string value (write "
            "evidence like SMS alerts checked, not \\\"SMS alerts\\\" checked); keep evidence short."
        )

    def _final_result(self) -> dict[str, Any]:
        job = dict(self.store.get_job(self.workflow_id))
        steps = [dict(s) for s in self.store.steps(self.workflow_id)]
        return {
            "workflow_id": self.workflow_id,
            "status": job["status"],
            "objective": job["objective"],
            "blocked_reason": job.get("blocked_reason"),
            "steps": [
                {
                    "ordinal": s["ordinal"],
                    "target": s["target"],
                    "objective": s["objective"],
                    "status": s["status"],
                    "verified": bool(s["verified"]) if s["verified"] is not None else None,
                    "summary": s["result_summary"],
                    "last_error": s["last_error"],
                    "facts": json.loads(s["facts_out"] or "{}"),
                }
                for s in steps
            ],
        }


def _extract_step_result(events: list[Event]) -> dict[str, Any]:
    completed = next((e for e in reversed(events) if e.type == EventType.TASK_COMPLETED), None)
    if completed is None:
        return {"verified": False, "evidence": "task finished without a TASK_COMPLETED event"}
    result_text = completed.payload.get("result", "")
    try:
        parsed = json.loads(result_text)
        if not isinstance(parsed, dict):
            return {"verified": False, "evidence": "finish result was JSON but not an object"}
    except json.JSONDecodeError:
        return {"verified": bool(result_text.strip()) and False, "summary": result_text,
                "evidence": "finish result was not valid JSON"}
    parsed.setdefault("summary", "")
    parsed.setdefault("facts", {})
    return parsed
