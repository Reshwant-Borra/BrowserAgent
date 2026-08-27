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
import dataclasses
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
        # Turning on the deterministic active-fact-constraint guard only for steps that
        # actually carry a verified incoming fact keeps this scoped to workflows (single-site/
        # batch tasks never set this) and to the exact steps where a contradiction is possible.
        step_config = self.config
        if facts:
            step_config = dataclasses.replace(
                self.config,
                context=dataclasses.replace(self.config.context, enforce_active_fact_constraints=True),
            )
        try:
            child_task_id = await asyncio.wait_for(
                self.runner.run_child(
                    step_config, self.workflow_id, step, goal, [], profile_dir,
                    self.policy.max_steps_per_step, runtime_policy=runtime_policy,
                    approval_callback=self.approval_callback,
                    seed_facts=facts or None,
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
        if facts:
            facts_lines = "\n".join(f"  {key} = {value}" for key, value in facts.items())
            facts_block = (
                "VERIFIED WORKFLOW INPUTS (already confirmed by an earlier step in this "
                "workflow — use these exact values rather than re-discovering or guessing "
                "them):\n" + facts_lines
            )
        else:
            facts_block = "VERIFIED WORKFLOW INPUTS: none yet."
        return (
            f"Open {step['target']} and complete this objective: {step['objective']}\n"
            f"{facts_block}\n"
            "When a verified workflow input's name matches a control on the current page "
            "(e.g. a field labeled with that same name), use that exact verified value for "
            "it instead of typing something else or leaving it blank.\n"
            "This step's target page is exactly the one given above. Stay on it and complete "
            "the objective there; do not follow a link to another page unless the objective "
            "explicitly requires navigating within this site to complete it.\n"
            "Before finishing, verify the page now actually shows the requested end-state — "
            "do not report success from memory of the action alone. When you call finish: "
            "set result to a short human-readable summary of what happened and whether it "
            "satisfies the objective; set verified to true only when the current page "
            "directly shows the objective's end-state was achieved (e.g. a dropdown now "
            "reads the new value, a toggle is now on) and to false otherwise; put any "
            "concrete value this step discovered on the page that a later step might need "
            "into outputs as a list of objects shaped {\"key\": \"short name\", \"value\": "
            "\"the value\", \"evidence\": \"short exact page excerpt showing it\"} — leave "
            "outputs empty if this step discovered nothing reusable, and never invent a "
            "value that is not actually shown on the page."
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
    """Reads the structured `verified`/`outputs` fields the model set directly on the finish
    action (agent/schemas.py::FinishAction, persisted onto the TASK_COMPLETED event by
    agent/loop.py::_handle_finish) — no JSON-in-a-string parsing of the free-form `result`
    text. A missing/omitted `verified` is treated as not-verified, never coerced to true:
    the model not stating a value is not evidence of success."""
    completed = next((e for e in reversed(events) if e.type == EventType.TASK_COMPLETED), None)
    if completed is None:
        return {"verified": False, "summary": "", "evidence": "task finished without a TASK_COMPLETED event", "facts": {}}
    payload = completed.payload
    summary = str(payload.get("result") or "")
    verified_raw = payload.get("verified")
    facts: dict[str, Any] = {}
    for item in payload.get("outputs") or []:
        if isinstance(item, dict) and item.get("key"):
            facts[str(item["key"])] = str(item.get("value", ""))
    if verified_raw is None:
        evidence = "model did not report a verified value on finish"
    else:
        evidence = summary or "step finished without a human-readable summary"
    return {
        "verified": bool(verified_raw),
        "summary": summary,
        "evidence": evidence,
        "facts": facts,
    }
