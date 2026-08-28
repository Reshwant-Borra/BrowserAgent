"""GeneralAgentController — Phase 2 of the general-controller migration (BrowserAgent_
General_Autonomous_Agent_Architecture_REVISED.pdf, section 18: "General Controller in
Shadow/Fixture Mode"). Owns task-level intent, subgoals, and workspace state; delegates
every subgoal to the existing, unmodified AgentLoop as its execution substrate (section 8:
"AgentLoop | One page/site or one bounded interactive subgoal"). No batch/workflow
delegation and no click-level decisions happen here — agent/planner.py's system prompt
enforces the hard boundary between this module's high-level decisions and AgentLoop's
low-level ones, and agent/loop.py itself is completely untouched by this phase.

"Shadow/fixture mode" means this controller is not wired into router/ or the live UI in this
phase — it is a fully working, independently invokable capability (see benchmarks/
general_agent and tests/integration/test_general_controller*) that a later phase can route
real user tasks through once its own holdouts pass (config.agent.control_mode stays
"legacy" everywhere else in the system).

Task identity: one control task (this controller's own task_id, created directly via
EventStore — no browser, no AgentLoop) owns the plan/subgoal/workspace state via the exact
same task_state/workspace_store machinery every other task in the repo already uses. Each
subgoal spawns its own, completely ordinary AgentLoop child task (own task_id, own event
log/db — mirroring how batch/orchestrator.py already spawns child AgentLoops) that shares
only a browser profile directory with its siblings (session continuity), never an event log.
Delegation and its outcome are themselves recorded as DELEGATE_STARTED/DELEGATE_RESULT
events on the control task, which is what makes "crash between two subgoals" and "crash
mid-subgoal" both cleanly resumable from persisted state alone.
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Callable, Optional

from agent.config import AppConfig
from agent.context_builder import build_workspace_summary
from agent.controller_models import CompletionEvaluation, ControllerDecision
from agent.loop import AgentLoop
from agent.planner import PlannerOutputError
from agent import planner as planner_mod
from agent.workspace_models import EvidenceRef, WorkspaceFact, WorkspacePatch, WorkspaceView
from inference.llama_client import InferenceClient, create_inference_client
from memory.event_store import EventStore, EventType
from memory.models import TaskRecord, TaskState
from memory.task_state import TaskStateStore
from memory.workspace_store import WorkspaceStore


_HUB_URL_FACT_KEY = "_hub_url"


class GeneralAgentController:
    def __init__(
        self,
        config: AppConfig,
        control_task_id: Optional[str] = None,
        llama_client: Optional[InferenceClient] = None,
        child_llama_client_factory: Optional[Callable[[], InferenceClient]] = None,
    ):
        self.config = config
        self.control_task_id = control_task_id or uuid.uuid4().hex[:12]
        self.tasks_dir = Path(config.storage.tasks_dir)
        self.db_path = self.tasks_dir / self.control_task_id / "task.db"
        self.event_store = EventStore(self.db_path)
        self.state_store = TaskStateStore(self.event_store)
        self.workspace_store = WorkspaceStore(self.event_store)
        self.llama = llama_client or create_inference_client(config)
        # Test-only seam: lets a test give every spawned child AgentLoop a scripted model
        # client (mirroring tests/integration/conftest.py's `loop.llama = ScriptedLlamaClient
        # (...)` pattern) without agent/loop.py itself needing to know a controller exists.
        self._child_llama_client_factory = child_llama_client_factory
        self._replans_used = 0

    def close(self) -> None:
        self.event_store.close()

    # ---- lifecycle ----------------------------------------------------------------

    @classmethod
    def create_new(cls, config: AppConfig, goal: str, success_criteria: list[str],
                    llama_client: Optional[InferenceClient] = None,
                    child_llama_client_factory: Optional[Callable[[], InferenceClient]] = None,
                    ) -> "GeneralAgentController":
        controller = cls(config, llama_client=llama_client,
                          child_llama_client_factory=child_llama_client_factory)
        controller.event_store.create_task(controller.control_task_id, goal, success_criteria)
        controller.event_store.append(controller.control_task_id, 0, EventType.TASK_CREATED,
                                       {"goal": goal, "success_criteria": success_criteria})
        controller.state_store.save(TaskState(task_id=controller.control_task_id, status="running"))
        return controller

    @classmethod
    def resume(cls, config: AppConfig, control_task_id: str,
               llama_client: Optional[InferenceClient] = None,
               child_llama_client_factory: Optional[Callable[[], InferenceClient]] = None,
               ) -> "GeneralAgentController":
        controller = cls(config, control_task_id=control_task_id, llama_client=llama_client,
                          child_llama_client_factory=child_llama_client_factory)
        if not controller.event_store.task_exists(control_task_id):
            raise ValueError(f"no such control task: {control_task_id}")
        return controller

    # ---- entry point ----------------------------------------------------------------

    async def run(self, explicit_target_url: Optional[str] = None) -> TaskState:
        """Receding-horizon control loop (section 7.1/7.2): plan once, execute the active
        subgoal via AgentLoop, ingest its result into the workspace, advance or replan at
        boundaries, evaluate completion, repeat until finished/blocked/iteration budget."""
        cfg = self.config.agent
        task = self.state_store.get_task_record(self.control_task_id)
        if task is None:
            raise ValueError(f"no such control task: {self.control_task_id}")

        reconciled = await self._reconcile_dangling_delegate(task)
        state = reconciled if reconciled is not None else self.state_store.load(self.control_task_id)
        self._replans_used = self._count_replan_events()
        await self._remember_hub_url(explicit_target_url)

        if not state.plan and state.status == "running":
            state = await self._initial_plan(task)

        # Defensive hard cap only — never a normal control-flow trigger. Sized generously
        # above what max_replans/max_subgoal_attempts/planner_max_subgoals could legitimately
        # produce, purely so a logic bug here fails as "stopped early" instead of hanging.
        iteration_budget = (
            (cfg.planner_max_subgoals + cfg.max_replans) * max(cfg.max_subgoal_attempts, 1) + cfg.max_replans + 10
        )
        for _ in range(iteration_budget):
            if state.status != "running":
                break

            if state.current_subgoal is None:
                state = await self._evaluate_and_replan_or_finish(task)
                continue

            attempts = self._attempts_for_current_subgoal(state.current_subgoal)
            if attempts >= cfg.max_subgoal_attempts:
                state = await self._replan_or_block(
                    task,
                    reason_hint=(f"repeated_failure: subgoal {state.current_subgoal!r} failed "
                                 f"{attempts} consecutive time(s)"),
                )
                continue

            current_subgoal = state.current_subgoal
            child_task_id, child_state = await self._run_subgoal(task, current_subgoal)
            state = await self._handle_subgoal_child_result(task, current_subgoal, child_task_id, child_state)

        return self.state_store.load(self.control_task_id)

    # ---- planning -------------------------------------------------------------------

    async def _initial_plan(self, task: TaskRecord) -> TaskState:
        cfg = self.config.agent
        workspace = self.workspace_store.load(self.control_task_id)
        try:
            decision = await planner_mod.initial_plan(
                self.llama, task.goal, task.success_criteria, workspace,
                max_entities=cfg.max_workspace_entities_in_context,
                max_evidence=cfg.max_workspace_evidence_in_context,
                max_subgoals=cfg.planner_max_subgoals,
            )
        except PlannerOutputError as exc:
            return self._block(f"initial planning failed schema validation: {exc}")
        return self._apply_controller_decision(decision)

    async def _replan_or_block(self, task: TaskRecord, reason_hint: str) -> TaskState:
        cfg = self.config.agent
        if self._replans_used >= cfg.max_replans:
            return self._block(
                f"replan budget exhausted ({self._replans_used}/{cfg.max_replans}): {reason_hint}",
            )
        workspace = self.workspace_store.load(self.control_task_id)
        try:
            decision = await planner_mod.replan(
                self.llama, task.goal, task.success_criteria, workspace, reason_hint,
                max_entities=cfg.max_workspace_entities_in_context,
                max_evidence=cfg.max_workspace_evidence_in_context,
                max_subgoals=cfg.planner_max_subgoals,
            )
        except PlannerOutputError as exc:
            return self._block(f"replan failed schema validation: {exc}")
        self._replans_used += 1
        return self._apply_controller_decision(decision)

    def _apply_controller_decision(self, decision: ControllerDecision) -> TaskState:
        if decision.decision in ("start_subgoal", "revise_plan"):
            plan = decision.plan or ([decision.active_subgoal] if decision.active_subgoal else [])
            active = decision.active_subgoal or (plan[0] if plan else None)
            if not plan or active is None:
                return self._block("planner returned an empty plan with nothing to do")
            self._append(EventType.SUBGOAL_CHANGED, {"subgoal": active, "plan": plan})
            return self.state_store.load(self.control_task_id)
        if decision.decision == "finish":
            return self._finish(CompletionEvaluation(
                satisfied=True, next_recommendation="finish",
            ), completion_claim=decision.completion_claim)
        if decision.decision == "ask_user":
            return self._block(decision.clarification_question or "clarification needed to proceed")
        # delegate_batch / delegate_workflow / discover_sources: valid values in the shared
        # ControllerDecision contract (section 7), but Phase 2 explicitly implements direct-
        # subgoal execution only (section 18) — fail safe with a clear reason rather than
        # silently dropping the decision or half-implementing an unfinished substrate.
        return self._block(
            f"planner requested {decision.decision!r}, which this phase (direct-subgoal "
            "execution only) does not implement yet",
        )

    # ---- completion evaluation --------------------------------------------------------

    async def _planner_evaluate_completion(self, task: TaskRecord, workspace: WorkspaceView) -> CompletionEvaluation:
        cfg = self.config.agent
        return await planner_mod.evaluate_completion(
            self.llama, task.goal, task.success_criteria, workspace,
            max_entities=cfg.max_workspace_entities_in_context,
            max_evidence=cfg.max_workspace_evidence_in_context,
        )

    async def _maybe_early_exit(self, task: TaskRecord) -> TaskState:
        """Called after a subgoal completes, only when completion_check_after_subgoal is on:
        lets the controller finish as soon as the goal is satisfied even with subgoals still
        left in the plan, instead of always grinding through the rest of it."""
        try:
            workspace = self.workspace_store.load(self.control_task_id)
            evaluation = await self._planner_evaluate_completion(task, workspace)
        except PlannerOutputError as exc:
            return self._block(f"completion evaluation failed schema validation: {exc}")
        self._append(EventType.COMPLETION_EVALUATED, evaluation.model_dump())
        if evaluation.satisfied:
            return self._finish(evaluation)
        if evaluation.next_recommendation == "ask_user":
            return self._block("; ".join(evaluation.missing_requirements) or "clarification needed")
        return self.state_store.load(self.control_task_id)

    async def _evaluate_and_replan_or_finish(self, task: TaskRecord) -> TaskState:
        """Called when the plan is exhausted (current_subgoal is None) — a mandatory boundary,
        unlike _maybe_early_exit's optional per-subgoal check. Not satisfied here is the doc's
        "completion_rejection" replan trigger, since there is no next subgoal to fall through to."""
        try:
            workspace = self.workspace_store.load(self.control_task_id)
            evaluation = await self._planner_evaluate_completion(task, workspace)
        except PlannerOutputError as exc:
            return self._block(f"completion evaluation failed schema validation: {exc}")
        self._append(EventType.COMPLETION_EVALUATED, evaluation.model_dump())
        if evaluation.satisfied:
            return self._finish(evaluation)
        if evaluation.next_recommendation == "ask_user":
            return self._block("; ".join(evaluation.missing_requirements) or "clarification needed")
        reason = "completion_rejection: " + ("; ".join(evaluation.missing_requirements) or "plan exhausted without satisfying the goal")
        return await self._replan_or_block(task, reason_hint=reason)

    # ---- delegation to AgentLoop ------------------------------------------------------

    async def _run_subgoal(self, task: TaskRecord, subgoal: str) -> tuple[str, TaskState]:
        workspace = self.workspace_store.load(self.control_task_id)
        hub_url = workspace.facts.get(_HUB_URL_FACT_KEY)
        goal_text = self._subgoal_child_goal(task.goal, subgoal, hub_url, workspace)
        profile_dir = self.tasks_dir / self.control_task_id / "subgoal_browser_profile"
        loop = AgentLoop.create_new(
            self.config, goal_text, [], profile_dir=profile_dir, explicit_target_url=hub_url,
        )
        if self._child_llama_client_factory is not None:
            loop.llama = self._child_llama_client_factory()
        self._append(EventType.DELEGATE_STARTED, {
            "substrate": "agent_loop", "subgoal": subgoal, "child_task_id": loop.task_id,
            "target_url": hub_url,
        })
        child_state = await loop.run(max_steps=self.config.agent.max_steps_per_subgoal)
        return loop.task_id, child_state

    def _subgoal_child_goal(self, task_goal: str, subgoal: str, target_url: Optional[str],
                             workspace: WorkspaceView) -> str:
        intro = f"Open {target_url} first. " if target_url else ""
        workspace_hint = build_workspace_summary(
            workspace,
            self.config.agent.max_workspace_entities_in_context,
            self.config.agent.max_workspace_evidence_in_context,
        )
        return (
            f"{intro}Overall goal: {task_goal}\n"
            f"Current subgoal — do only this, not the rest of the overall goal: {subgoal}\n"
            f"{workspace_hint}\n"
            "When this subgoal is done, call finish with a short one-sentence plain-text "
            "`result` summary, and fill in the structured `structured_result` field directly "
            "(relevant, summary, findings) — it is a real typed field, never JSON text inside "
            "`result`. Do not attempt the rest of the overall goal in this subgoal."
        )

    async def _handle_subgoal_child_result(self, task: TaskRecord, subgoal: str,
                                            child_task_id: str, child_state: TaskState) -> TaskState:
        self._ingest_subgoal_result(subgoal, child_task_id, child_state)
        if child_state.status != "completed":
            return self.state_store.load(self.control_task_id)
        state = self._advance_subgoal()
        # Only an *optional* early-exit check when subgoals remain — if the plan just became
        # exhausted (current_subgoal now None), skip straight back to run()'s own
        # current_subgoal-is-None branch, which evaluates completion exactly once and, unlike
        # this early-exit path, knows how to replan on rejection instead of just returning.
        if state.current_subgoal is not None and self.config.agent.completion_check_after_subgoal and state.status == "running":
            state = await self._maybe_early_exit(task)
        return state

    def _ingest_subgoal_result(self, subgoal: str, child_task_id: str, child_state: TaskState) -> None:
        child_db_path = self.tasks_dir / child_task_id / "task.db"
        child_event_store = EventStore(child_db_path)
        try:
            completed = [e for e in child_event_store.all_events(child_task_id) if e.type == EventType.TASK_COMPLETED]
        finally:
            child_event_store.close()
        result_text = str(completed[-1].payload.get("result", "")) if completed else ""
        structured_result = completed[-1].payload.get("structured_result") if completed else None

        result_event_id = self._append(EventType.DELEGATE_RESULT, {
            "substrate": "agent_loop", "subgoal": subgoal, "child_task_id": child_task_id,
            "status": child_state.status, "result": result_text, "final_url": child_state.current_url,
            "structured_result": structured_result, "blocked_reason": child_state.blocked_reason,
        })

        if child_state.status == "completed" and (result_text or structured_result):
            excerpt = result_text or json.dumps(structured_result)[:500]
            patch = WorkspacePatch(
                add_facts=[WorkspaceFact(key=f"subgoal_result::{subgoal}", value=result_text)],
                add_evidence=[EvidenceRef(
                    fact_key=f"subgoal_result::{subgoal}",
                    source_event_id=result_event_id,
                    source_url=child_state.current_url,
                    excerpt=excerpt,
                )],
            )
            self.workspace_store.apply_patch(self.control_task_id, patch)

    def _advance_subgoal(self) -> TaskState:
        state = self.state_store.load(self.control_task_id)
        plan = state.plan
        idx = plan.index(state.current_subgoal) if state.current_subgoal in plan else -1
        next_subgoal = plan[idx + 1] if 0 <= idx < len(plan) - 1 else None
        self._append(EventType.SUBGOAL_CHANGED, {"subgoal": next_subgoal, "plan": plan})
        return self.state_store.load(self.control_task_id)

    # ---- crash recovery ---------------------------------------------------------------

    async def _reconcile_dangling_delegate(self, task: TaskRecord) -> Optional[TaskState]:
        """A DELEGATE_STARTED with no matching DELEGATE_RESULT means the process died while a
        child AgentLoop was running (or between the child finishing and this controller
        ingesting its result). Resume that exact child task — never start a fresh duplicate —
        before doing anything else, mirroring AgentLoop's own `_reconcile_pending_intent`."""
        events = self.event_store.all_events(self.control_task_id)
        started = {e.payload.get("child_task_id"): e for e in events if e.type == EventType.DELEGATE_STARTED}
        resolved = {e.payload.get("child_task_id") for e in events if e.type == EventType.DELEGATE_RESULT}
        dangling = [cid for cid in started if cid and cid not in resolved]
        if not dangling:
            return None
        # Multiple dangling entries should never happen (delegation is sequential, one child
        # at a time) — but if it did, only the most recent is actually still relevant.
        dangling.sort(key=lambda cid: started[cid].id)
        child_task_id = dangling[-1]
        event = started[child_task_id]
        subgoal = event.payload["subgoal"]
        loop = AgentLoop.resume(self.config, child_task_id)
        if self._child_llama_client_factory is not None:
            loop.llama = self._child_llama_client_factory()
        child_state = await loop.run(max_steps=self.config.agent.max_steps_per_subgoal)
        return await self._handle_subgoal_child_result(task, subgoal, child_task_id, child_state)

    async def _remember_hub_url(self, explicit_target_url: Optional[str]) -> None:
        """Persist the task's starting page once, so every subgoal's child AgentLoop returns
        to it rather than continuing from wherever the *previous* subgoal happened to end up.

        Earlier version of this controller threaded the previous subgoal's final URL forward
        as the next subgoal's start point — correct for a strictly sequential fact-passing
        chain, but wrong for "hub and branch" plans (e.g. a directory page linking to several
        independent detail pages): a subgoal like "extract StackForge's price" would start on
        whatever page the *previous* subgoal ("extract NimbusHost's price") ended on — a
        different site's detail page — with no path back to the directory. Diagnosed via
        benchmarks/general_agent/run_phase2_controller.py's compare_and_report scenario, which
        regressed below the legacy baseline until this fix (see docs/BROWSERAGENT_MASTER_
        STATUS.md's Phase 2 section for the before/after numbers). Always-return-to-hub is a
        safe general default without resource-aware subgoal targeting (ControllerDecision's
        resource_refs / a real ResourceResolver are deliberately out of Phase 2's scope, per
        the router/resources.py "KEEP + EXTEND" note in the architecture doc's section 15) —
        a child can still navigate multiple hops from the hub within its own subgoal run."""
        if explicit_target_url is None:
            return
        workspace = self.workspace_store.load(self.control_task_id)
        if _HUB_URL_FACT_KEY in workspace.facts:
            return
        self.workspace_store.apply_patch(self.control_task_id, WorkspacePatch(
            add_facts=[WorkspaceFact(key=_HUB_URL_FACT_KEY, value=explicit_target_url)],
        ))

    # ---- bookkeeping helpers ------------------------------------------------------------

    def _attempts_for_current_subgoal(self, subgoal: str) -> int:
        events = self.event_store.all_events(self.control_task_id)
        last_subgoal_change_id = 0
        for e in events:
            if e.type == EventType.SUBGOAL_CHANGED and e.payload.get("subgoal") == subgoal:
                last_subgoal_change_id = e.id
        return sum(
            1 for e in events
            if e.type == EventType.DELEGATE_STARTED
            and e.id > last_subgoal_change_id
            and e.payload.get("subgoal") == subgoal
        )

    def _count_replan_events(self) -> int:
        events = self.event_store.all_events(self.control_task_id)
        subgoal_changes = sum(1 for e in events if e.type == EventType.SUBGOAL_CHANGED)
        return max(0, subgoal_changes - 1)

    def _append(self, event_type: EventType, payload: dict) -> int:
        state = self.state_store.load(self.control_task_id)
        return self.event_store.append(self.control_task_id, state.current_step + 1, event_type, payload)

    def _finish(self, evaluation: CompletionEvaluation, completion_claim: Optional[str] = None) -> TaskState:
        prior = self.state_store.load(self.control_task_id)
        result = completion_claim or "; ".join(prior.completed_subgoals) or "goal satisfied"
        self._append(EventType.TASK_COMPLETED, {
            "result": result,
            "missing_requirements": evaluation.missing_requirements,
            "unsupported_claims": evaluation.unsupported_claims,
        })
        state = self.state_store.load(self.control_task_id)
        state.status = "completed"
        self.state_store.save(state)
        return state

    def _block(self, reason: str) -> TaskState:
        self._append(EventType.TASK_BLOCKED, {"reason": reason})
        state = self.state_store.load(self.control_task_id)
        state.status = "blocked"
        state.blocked_reason = reason
        self.state_store.save(state)
        return state
