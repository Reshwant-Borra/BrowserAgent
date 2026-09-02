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
from typing import Awaitable, Callable, Optional

from agent import ranking
from agent import workspace_ops
from agent.config import AppConfig
from agent.context_builder import build_workspace_summary
from agent.controller_models import CompletionEvaluation, ControllerDecision
from agent.loop import AgentLoop
from agent.planner import PlannerOutputError
from agent import planner as planner_mod
from agent.schemas import ActionType, ModelDecision, RecoveryLevel, ValidationErrorKind
from agent.workspace_models import (
    EvidenceRef,
    WorkspaceEntity,
    WorkspaceEntityPatch,
    WorkspaceFact,
    WorkspacePatch,
    WorkspaceView,
)
from batch.models import BatchPolicy, ResultContract
from batch.orchestrator import BatchOrchestrator, ChildRunner
from batch.store import BatchStore
from browser.page_model import ElementRef, PageObservation
from browser.playwright_backend import urls_match
from inference.llama_client import InferenceClient, create_inference_client
from memory.event_store import Event, EventStore, EventType
from memory.models import TaskRecord, TaskState
from memory.task_state import TaskStateStore
from memory.workspace_store import WorkspaceStore
from research import discovery as research_discovery
from router.extract import extract_urls
from router.policy import NeedsInput, RoutingError, route
from router.schema import TaskType
from workflow.models import WorkflowPolicy
from workflow.orchestrator import WorkflowOrchestrator
from workflow.store import WorkflowStore

# Blocked reasons agent/loop.py's own step()/recovery ladder — or this controller's own
# per-subgoal local-attempt limit (_subgoal_local_attempts) — can produce that reflect a
# capability/reliability limit rather than a safety gate — eligible for the continuous
# strategy's controller-level replan-and-retry below:
#   - "repeated failures exhausted automatic recovery": agent/loop.py::_advance_recovery's
#     RecoveryLevel.USER_REQUIRED.
#   - "refusing to auto-retry a consequential action...": the idempotency guard that refuses
#     to blindly re-attempt a CONSEQUENTIAL action fingerprint that already failed once.
#   - "subgoal local attempts exhausted": this controller's own _subgoal_local_attempts limit
#     — agent/loop.py's own internal low-level replan rescued the same subgoal
#     max_subgoal_attempts times with no forward progress (the diagnosed root cause of the 0/5
#     multi_step_registration failure — see docs/BROWSERAGENT_MASTER_STATUS.md's Phase 2
#     corrective-pass section).
# A controller replan produces a genuinely different subgoal/approach, not a repeat of the
# same fingerprinted action — the fingerprint-based guard itself stays fully intact regardless
# of how many times the controller replans, so retrying *via a replan* here never bypasses it.
# This also brings the continuous strategy's retry semantics in line with the delegated
# strategy's existing max_subgoal_attempts, which already retries a fresh, hub-grounded child
# (any blocked reason included) up to that budget before ever calling _replan_or_block —
# unlike delegated mode's fresh-child-per-attempt state, the continuous strategy shares one
# TaskState/recent_actions history across attempts, so it must explicitly re-open this door
# rather than getting it "for free."
#
# Every other blocked reason (login_required, a declined consequential action, a runtime-
# policy/scope violation) is a deliberate stop that must never be silently retried past — see
# docs/BROWSERAGENT_MASTER_STATUS.md's Phase 2 corrective-pass section, item 12.
_REPLAN_ELIGIBLE_BLOCK_PREFIXES = (
    "repeated failures exhausted automatic recovery",
    "refusing to auto-retry a consequential action that previously failed",
    "subgoal local attempts exhausted",
)


def _is_replan_eligible_block(blocked_reason: Optional[str]) -> bool:
    return bool(blocked_reason) and any(blocked_reason.startswith(p) for p in _REPLAN_ELIGIBLE_BLOCK_PREFIXES)


_HUB_URL_FACT_KEY = "_hub_url"

# Generic entity_type used for a source URL discovered by _discover_sources (Phase 4:
# architecture doc section 18 "Delegation to Existing Batch / Workflow / Research
# Capabilities" / section 12 "Research subsystem: KEEP + REFACTOR as capability"). Never a
# domain type — just "a candidate resource the controller itself found," consumed by a later
# delegate_batch the same way an explicit URL in the goal text is.
_DISCOVERED_SOURCE_ENTITY_TYPE = "discovered_source"


class GeneralAgentController:
    def __init__(
        self,
        config: AppConfig,
        control_task_id: Optional[str] = None,
        llama_client: Optional[InferenceClient] = None,
        child_llama_client_factory: Optional[Callable[[], InferenceClient]] = None,
        child_runner: Optional[ChildRunner] = None,
        approval_callback: Optional[Callable[[ModelDecision, Optional[ElementRef]], Awaitable[bool]]] = None,
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
        # Test-only seam (Phase 4): the same idea, one level up — lets a test give a
        # delegate_batch/delegate_workflow's own BatchOrchestrator/WorkflowOrchestrator a
        # scripted `ChildRunner` (batch/orchestrator.py's own Protocol, already used by
        # tests/unit/test_batch_orchestrator.py) instead of the real AgentLoopChildRunner.
        # None (the default, every pre-existing caller) is unchanged: both orchestrators
        # already default to AgentLoopChildRunner() themselves when `runner` is None.
        self._child_runner = child_runner
        # Phase 5: threaded into every subgoal's AgentLoop and every delegate's own
        # orchestrator (agent/loop.py's `_request_approval` falls back to a blocking `input()`
        # prompt when this is None — never safe for a controller driven from an async UI
        # server). None (every pre-existing caller/test) is unchanged.
        self._approval_callback = approval_callback
        self._replans_used = 0

    def close(self) -> None:
        self.event_store.close()

    # ---- lifecycle ----------------------------------------------------------------

    @classmethod
    def create_new(cls, config: AppConfig, goal: str, success_criteria: list[str],
                    llama_client: Optional[InferenceClient] = None,
                    child_llama_client_factory: Optional[Callable[[], InferenceClient]] = None,
                    child_runner: Optional[ChildRunner] = None,
                    approval_callback: Optional[Callable[[ModelDecision, Optional[ElementRef]], Awaitable[bool]]] = None,
                    ) -> "GeneralAgentController":
        controller = cls(config, llama_client=llama_client,
                          child_llama_client_factory=child_llama_client_factory,
                          child_runner=child_runner, approval_callback=approval_callback)
        controller.event_store.create_task(controller.control_task_id, goal, success_criteria)
        controller.event_store.append(controller.control_task_id, 0, EventType.TASK_CREATED,
                                       {"goal": goal, "success_criteria": success_criteria})
        controller.state_store.save(TaskState(task_id=controller.control_task_id, status="running"))
        return controller

    @classmethod
    def resume(cls, config: AppConfig, control_task_id: str,
               llama_client: Optional[InferenceClient] = None,
               child_llama_client_factory: Optional[Callable[[], InferenceClient]] = None,
               child_runner: Optional[ChildRunner] = None,
               approval_callback: Optional[Callable[[ModelDecision, Optional[ElementRef]], Awaitable[bool]]] = None,
               ) -> "GeneralAgentController":
        controller = cls(config, control_task_id=control_task_id, llama_client=llama_client,
                          child_llama_client_factory=child_llama_client_factory,
                          child_runner=child_runner, approval_callback=approval_callback)
        if not controller.event_store.task_exists(control_task_id):
            raise ValueError(f"no such control task: {control_task_id}")
        return controller

    # ---- entry point ----------------------------------------------------------------

    async def run(self, explicit_target_url: Optional[str] = None, strategy: str = "delegated") -> TaskState:
        """Receding-horizon control loop (section 7.1/7.2).

        `strategy="delegated"` (default, unchanged from Phase 2's original landing): every
        subgoal spawns its own fresh child AgentLoop task (own task_id/event log), ingesting
        that child's result into the workspace before advancing or replanning. This is what
        every existing test/caller uses and its behavior is completely unchanged below.

        `strategy="continuous"` (Phase 2 corrective-pass candidate — see docs/BROWSERAGENT_
        MASTER_STATUS.md): the diagnosed subgoal-boundary cost (each child re-earning its own
        orientation from a fresh task/browser relaunch, plus a completion-evaluation model
        call after every intermediate subgoal) is paid once per *subgoal*, not once per task.
        This strategy instead runs ONE continuous AgentLoop task across every subgoal —
        `_run_continuous` below — reusing agent/loop.py's own step()/verifier/event-sourcing
        machinery unmodified except for one additive `finish_intercept` hook.
        """
        if strategy == "continuous":
            return await self._run_continuous(explicit_target_url)
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
        deterministic = self._deterministic_initial_decision(task)
        if deterministic is not None:
            return await self._apply_controller_decision(task, deterministic)
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
        return await self._apply_controller_decision(task, decision)

    def _deterministic_initial_decision(self, task: TaskRecord) -> Optional[ControllerDecision]:
        """Section 8.1's "deterministic substrate selection before using the LLM": when the
        goal text itself already lists at least `batch_delegation_min_targets` literal target
        URLs, delegating to BatchOrchestrator is mechanical — each target needs the same
        independent look, exactly the "a subgoal contains N resolved independent URLs...
        BatchOrchestrator is the obvious substrate" case the doc names — so no planning call is
        spent deciding that. Returns None (fall through to the normal planner call, which can
        still choose delegate_batch/delegate_workflow/discover_sources itself) for every other
        goal shape, including one with fewer explicit URLs than the threshold."""
        urls = extract_urls(task.goal)
        if len(urls) < self.config.agent.batch_delegation_min_targets:
            return None
        active = f"Look at each of the {len(urls)} target pages and report what's relevant to the goal."
        return ControllerDecision(
            decision="delegate_batch", reason_code="independent_targets",
            active_subgoal=active, plan=[active], resource_refs=[],
        )

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
        return await self._apply_controller_decision(task, decision)

    async def _apply_controller_decision(self, task: TaskRecord, decision: ControllerDecision) -> TaskState:
        if decision.decision in ("start_subgoal", "revise_plan"):
            plan = decision.plan or ([decision.active_subgoal] if decision.active_subgoal else [])
            active = decision.active_subgoal or (plan[0] if plan else None)
            if not plan or active is None:
                return self._block("planner returned an empty plan with nothing to do")
            if active not in plan:
                # Deterministic self-heal, not a planner-prompt fix (live forensic finding,
                # Phase 3 continued-validation pass): Qwen3-8B's replan call can return an
                # active_subgoal that its own plan list omits (observed live: a replan kept
                # revising `plan`'s earlier collection steps while `active_subgoal` still named
                # the final "find the N cheapest..." synthesis step, which the returned plan no
                # longer contained at all). Persisting that mismatch as-is permanently desyncs
                # _make_continuous_finish_intercept's `subgoal not in plan` check the next time
                # the model (correctly) finishes for `active`'s own exact text — with no pending
                # plan item left to recover onto, every subsequent replan just reproduces the same
                # unrecognized subgoal, burning the entire replan budget (live evidence: two
                # `desynced_subgoal` trials, both traced to exactly this invariant violation, never
                # to the model actually finishing for a genuinely different/stale subgoal).
                # Appending is always safe: `active` is what the planner itself just chose to work
                # on, so it belongs in the plan by definition regardless of what else the planner
                # forgot to carry forward.
                plan = plan + [active]
            self._append(EventType.SUBGOAL_CHANGED, {"subgoal": active, "plan": plan, "source": "controller"})
            return self._reset_recovery_for_new_subgoal()
        if decision.decision == "delegate_batch":
            return await self._delegate_batch(task, decision)
        if decision.decision == "delegate_workflow":
            return await self._delegate_workflow(task, decision)
        if decision.decision == "discover_sources":
            return await self._discover_sources(task, decision)
        if decision.decision == "finish":
            return await self._finish(CompletionEvaluation(
                satisfied=True, next_recommendation="finish",
            ), completion_claim=decision.completion_claim)
        if decision.decision == "ask_user":
            return self._block(decision.clarification_question or "clarification needed to proceed",
                                kind="ask_user")
        # Defensive only: every literal value of ControllerDecisionType is handled above: this
        # is unreachable via a schema-valid decision, kept as a fail-safe rather than a silent
        # no-op if the shared contract (agent/controller_models.py) ever grows a new value.
        return self._block(f"planner requested {decision.decision!r}, which this controller does not implement")

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
            return await self._finish(evaluation)
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
            return await self._finish(evaluation)
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
            approval_callback=self._approval_callback,
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
            patch = self._build_result_patch(
                subgoal, result_text, structured_result, result_event_id, child_state.current_url,
            )
            self.workspace_store.apply_patch(self.control_task_id, patch)

    def _build_result_patch(
        self, subgoal: str, result_text: str, structured_result: Optional[dict],
        result_event_id: int, source_url: Optional[str], preceding_subgoal: Optional[str] = None,
    ) -> WorkspacePatch:
        """Shared by both the delegated and continuous ingest paths (Phase 3: generic entity
        collection, section 9). Always keeps the pre-existing plain-text subgoal_result fact
        (unchanged from Phase 2); additionally, when `structured_result` carries at least one
        valued finding, also materializes one generic WorkspaceEntity with per-attribute
        evidence via agent/workspace_ops.py::entity_patch_from_findings — the "workspace
        entities plus deterministic query/computation primitives" fix for the Amazon-class
        multi-candidate gap, never a domain-specific ranker class. Skips entity materialization
        when the *subgoal's own text* already names a top-k pattern (agent/workspace_ops.py::
        parse_requested_top_k, content-based rather than plan-position-based so it works
        regardless of whether the planner made "report the top-k" its own subgoal or folded it
        into the last collection subgoal): a subgoal that is itself asking to identify/report
        already-collected candidates is synthesizing, not describing a new one — without this
        guard its own finish could add a spurious extra "entity" for the report itself. The
        plain-text fact is still recorded either way."""
        add_facts: list[WorkspaceFact] = []
        add_evidence: list[EvidenceRef] = []
        add_entities = []
        if result_text or structured_result:
            add_facts.append(WorkspaceFact(key=f"subgoal_result::{subgoal}", value=result_text))
            excerpt = result_text or json.dumps(structured_result)[:500]
            add_evidence.append(EvidenceRef(
                fact_key=f"subgoal_result::{subgoal}",
                source_event_id=result_event_id,
                source_url=source_url,
                excerpt=excerpt,
            ))
        ingest_entity = workspace_ops.parse_requested_top_k(subgoal) is None
        entity_patch = workspace_ops.entity_patch_from_findings(
            workspace_ops.coerce_structured_result(structured_result, result_text),
            entity_id=f"ent_{uuid.uuid4().hex[:10]}",
            subgoal=subgoal,
            source_event_id=result_event_id,
            source_url=source_url,
            preceding_subgoal=preceding_subgoal,
        ) if ingest_entity else None
        update_entities: list[WorkspaceEntityPatch] = []
        if entity_patch is not None:
            new_entity = entity_patch.add_entities[0]
            # A controller-level replan can re-run an already-completed candidate subgoal
            # (the planner isn't always perfectly informed by the workspace summary about what
            # it already collected) — merge into the existing entity by name instead of
            # creating a second one for the same real-world candidate. Falls back to matching by
            # source URL when naming alone can't tell: a live planner sometimes atomizes one
            # candidate into several nameless subgoals ("Extract price_usd from the page"), whose
            # own text never names the candidate at all — but if some *other* entity already has
            # evidence from this exact page, that is by far the strongest signal this finding
            # belongs to it too, not a new candidate. Scoped to `preceding_subgoal is not None`
            # (the continuous strategy's own signal, never set by the delegated strategy's call
            # site) — delegated-mode child tasks are independent AgentLoop runs that can
            # legitimately share a URL across genuinely different candidates in some test/degenerate
            # setups, where this heuristic would wrongly merge unrelated entities.
            existing = self._find_active_entity_by_name(new_entity.name) or (
                self._find_active_entity_by_source_url(source_url)
                if source_url and preceding_subgoal is not None else None
            )
            if existing is not None:
                update_entities = [WorkspaceEntityPatch(
                    id=existing.id, attributes={**existing.attributes, **new_entity.attributes},
                )]
                add_evidence = add_evidence + [
                    ev.model_copy(update={"entity_id": existing.id}) for ev in entity_patch.add_evidence
                ]
            else:
                add_entities = entity_patch.add_entities
                add_evidence = add_evidence + entity_patch.add_evidence
        return WorkspacePatch(
            add_facts=add_facts, add_evidence=add_evidence,
            add_entities=add_entities, update_entities=update_entities,
        )

    def _find_active_entity_by_name(self, name: Optional[str]) -> Optional[WorkspaceEntity]:
        if not name:
            return None
        norm = name.strip().lower()
        workspace = self.workspace_store.load(self.control_task_id)
        for e in workspace.entities:
            if e.status == "active" and (e.name or "").strip().lower() == norm:
                return e
        # Fallback: fuzzy (substring, case-insensitive) match — a controller-level replan can
        # re-run an already-completed candidate subgoal under a slightly different finish-text
        # label (e.g. a subgoal that overshoots into recording data names the resulting entity
        # "SwiftBook Air details" while its own dedicated later subgoal names it "SwiftBook
        # Air" — same real-world candidate, imprecise label, not two candidates). Same
        # imprecise-label reasoning as workspace_ops.names_plausibly_match uses elsewhere; only
        # applied when nothing matched exactly above, and only ever merges into an existing
        # entity, never creates a false one.
        for e in workspace.entities:
            if e.status == "active" and workspace_ops.names_plausibly_match(e.name, name):
                return e
        return None

    def _find_active_entity_by_source_url(self, source_url: Optional[str]) -> Optional[WorkspaceEntity]:
        """Naming-independent dedupe fallback for `_build_result_patch`: an existing active
        entity that already has evidence sourced from this exact URL is almost certainly the
        same real-world candidate, regardless of what either subgoal's own text says — the
        strongest available signal when a nameless follow-up subgoal's own name-inference has
        nothing to go on. Deliberately only consulted as a fallback (after name matching), and
        only reachable once agent/loop.py's own `_evidence_source_is_stale`-guarded finish has
        already passed — so a genuinely different candidate's page was never accepted as
        evidence here in the first place."""
        if not source_url:
            return None
        workspace = self.workspace_store.load(self.control_task_id)
        active_by_id = {e.id: e for e in workspace.entities if e.status == "active"}
        for ev in workspace.evidence:
            if ev.source_url == source_url and ev.entity_id in active_by_id:
                return active_by_id[ev.entity_id]
        return None

    async def _maybe_select_top_k_entities(self, task: TaskRecord) -> Optional[str]:
        """Deterministic top-k completion path (architecture doc sections 9/13: "exactly the
        requested top-k... no unsupported final entity"). Only engages when the goal text
        itself names a top-k pattern (agent/workspace_ops.py::parse_requested_top_k — a
        generic phrase detector, never a domain keyword) AND the workspace already holds at
        least k comparable ("active", not yet selected/rejected) entities; otherwise returns
        None and _finish falls back to its prior plain completion_claim text, unchanged from
        Phase 2 — this never blocks or forces a replan itself, since a still-incomplete
        candidate set should already be caught by the completion evaluator's own "not
        satisfied, continue/replan" recommendation before _finish is ever reached."""
        k = workspace_ops.parse_requested_top_k(task.goal)
        if k is None:
            return None
        workspace = self.workspace_store.load(self.control_task_id)
        candidates = [e for e in workspace.entities if e.status == "active"]
        if len(candidates) < k:
            return None
        request = ranking.RankRequest(entity_ids=[e.id for e in candidates], objective=task.goal, k=k)
        try:
            result = await ranking.rank_candidates(self.llama, request, candidates)
        except ranking.RankingOutputError:
            return None
        if len(result.ranked_entity_ids) < k:
            return None
        selected_ids = result.ranked_entity_ids[:k]
        by_id = {e.id: e for e in candidates}
        self.workspace_store.apply_patch(self.control_task_id, WorkspacePatch(
            update_entities=(
                [WorkspaceEntityPatch(id=eid, status="selected") for eid in selected_ids]
                + [WorkspaceEntityPatch(id=e.id, status="rejected") for e in candidates if e.id not in selected_ids]
            ),
        ))
        selected_entities = sorted((by_id[eid] for eid in selected_ids), key=lambda e: selected_ids.index(e.id))
        return workspace_ops.render_entities_report(selected_entities, result.rationale_by_entity)

    def _advance_subgoal(self) -> TaskState:
        state = self.state_store.load(self.control_task_id)
        plan = state.plan
        idx = plan.index(state.current_subgoal) if state.current_subgoal in plan else -1
        next_subgoal = plan[idx + 1] if 0 <= idx < len(plan) - 1 else None
        self._append(EventType.SUBGOAL_CHANGED, {"subgoal": next_subgoal, "plan": plan, "source": "controller"})
        return self.state_store.load(self.control_task_id)

    # ---- delegation to Batch/Workflow/Research capabilities (Phase 4) ----------------
    #
    # Section 18 "Delegation to Existing Batch / Workflow / Research Capabilities": the
    # controller chooses an efficient, already-proven substrate instead of serially driving
    # every independent target or every ordered cross-site step through its own single
    # AgentLoop subgoal. BatchOrchestrator/WorkflowOrchestrator/research.discovery are reused
    # completely unmodified in their own execution logic — this section only ever resolves
    # *which real resources* a delegate should see (never inventing a URL: literal ones come
    # from router/extract.py's own extract_urls() over the goal text, exactly like the
    # semantic-planner resolver already does for router/ tasks; discovered ones come from
    # research/discovery.py's own id-based, anti-hallucination selection) and ingests the
    # delegate's verified output back into the same generic WorkspaceEntity/EvidenceRef shape
    # every other ingestion path in this file already uses. A delegate is treated exactly like
    # one atomic subgoal: marked active, run to completion, ingested, then advanced — so
    # _finish/_maybe_select_top_k_entities/_planner_evaluate_completion all apply to its output
    # with zero additional code.

    def _mark_subgoal_active(self, active: str) -> None:
        self._append(EventType.SUBGOAL_CHANGED, {"subgoal": active, "plan": [active], "source": "controller"})
        self._reset_recovery_for_new_subgoal()

    async def _finish_delegate_step(self, task: TaskRecord, success: bool,
                                     failure_reason: Optional[str] = None) -> TaskState:
        """Shared tail for every delegate branch below — mirrors _handle_subgoal_child_result's
        own advance-or-don't-advance shape for a plain AgentLoop child. On success, the
        delegate's own 1-item plan (`_mark_subgoal_active`) always advances straight to None
        (section 7.2's "boundary reached"), so run()'s own current_subgoal-is-None branch
        performs the mandatory completion check next — never skipped, never doubled."""
        if not success:
            return await self._replan_or_block(task, reason_hint=failure_reason or "repeated_failure: delegate did not complete")
        state = self._advance_subgoal()
        if state.current_subgoal is not None and self.config.agent.completion_check_after_subgoal and state.status == "running":
            state = await self._maybe_early_exit(task)
        return state

    def _resolve_batch_targets(self, task: TaskRecord) -> list[str]:
        """The sole target resolution for delegate_batch — code-owned, never model-invented
        (repository invariant, architecture doc section 22.1): literal URLs already in the
        goal text (extract_urls, order-preserving) unioned with any not-yet-consumed
        `discovered_source` entities a prior discover_sources call added to the workspace."""
        urls = extract_urls(task.goal)
        workspace = self.workspace_store.load(self.control_task_id)
        discovered = [
            e.attributes.get("url") for e in workspace.entities
            if e.entity_type == _DISCOVERED_SOURCE_ENTITY_TYPE and e.status == "active" and e.attributes.get("url")
        ]
        seen: set[str] = set()
        targets: list[str] = []
        for url in urls + discovered:
            if url and url not in seen:
                seen.add(url)
                targets.append(url)
        return targets

    async def _delegate_batch(self, task: TaskRecord, decision: ControllerDecision) -> TaskState:
        active = decision.active_subgoal or task.goal
        self._mark_subgoal_active(active)
        targets = self._resolve_batch_targets(task)
        if not targets:
            return await self._finish_delegate_step(
                task, success=False,
                failure_reason="resource_missing: delegate_batch requested but no resolved "
                               "independent targets were found in the goal text or workspace",
            )
        batch_id = f"{self.control_task_id}_batch_{uuid.uuid4().hex[:8]}"
        batch_dir = self.tasks_dir / self.control_task_id / "delegates" / batch_id
        self._append(EventType.DELEGATE_STARTED, {
            "substrate": "batch", "subgoal": active, "child_task_id": batch_id,
            "target_count": len(targets), "delegate_dir": str(batch_dir),
        })
        store = BatchStore.for_batch_dir(batch_dir)
        try:
            store.create_batch(active, targets, ResultContract(), BatchPolicy(), batch_id=batch_id)
        finally:
            store.close()
        final = await self._run_batch_delegate(batch_dir, batch_id)
        self._ingest_batch_result(active, batch_id, final)
        return await self._finish_delegate_step(task, success=True)

    async def _run_batch_delegate(self, batch_dir: Path, batch_id: str) -> dict:
        store = BatchStore.for_batch_dir(batch_dir)
        try:
            orchestrator = BatchOrchestrator(
                self.config, store, batch_id, BatchPolicy(), ResultContract(),
                runner=self._child_runner, parent_task_id=self.control_task_id,
                approval_callback=self._approval_callback,
            )
            return await orchestrator.run()
        finally:
            store.close()

    def _ingest_batch_result(self, subgoal: str, batch_id: str, final: dict) -> None:
        result_event_id = self._append(EventType.DELEGATE_RESULT, {
            "substrate": "batch", "subgoal": subgoal, "child_task_id": batch_id,
            "status": final.get("status"), "item_count": final.get("item_count"),
            "completed": final.get("completed"), "failed": final.get("failed"),
            "blocked": final.get("blocked"), "deduplicated_findings": final.get("deduplicated_findings"),
        })
        add_entities: list[WorkspaceEntity] = []
        add_evidence: list[EvidenceRef] = []
        for item in final.get("findings") or []:
            finding = item.get("finding") or {}
            provenance = (item.get("provenance") or [{}])[0]
            value = finding.get("value") or finding.get("title") or finding.get("assignment")
            if not str(value or "").strip():
                continue
            entity_id = f"ent_{uuid.uuid4().hex[:10]}"
            name = finding.get("title") or str(value)
            attributes = {k: v for k, v in finding.items() if k != "evidence" and v not in (None, "")}
            add_entities.append(WorkspaceEntity(
                id=entity_id, entity_type=str(finding.get("type") or "batch_finding"),
                name=str(name)[:200], attributes=attributes,
            ))
            excerpt = finding.get("evidence") or str(value)
            add_evidence.append(EvidenceRef(
                entity_id=entity_id, source_event_id=result_event_id,
                source_url=provenance.get("final_url") or provenance.get("source_url") or finding.get("source_url"),
                excerpt=str(excerpt)[:500],
            ))
        # Any discovered_source entity that fed this batch is now resolved — never
        # redelegated by a later delegate_batch call in the same task.
        workspace = self.workspace_store.load(self.control_task_id)
        update_entities = [
            WorkspaceEntityPatch(id=e.id, status="resolved")
            for e in workspace.entities
            if e.entity_type == _DISCOVERED_SOURCE_ENTITY_TYPE and e.status == "active"
        ]
        if add_entities or add_evidence or update_entities:
            self.workspace_store.apply_patch(self.control_task_id, WorkspacePatch(
                add_entities=add_entities, add_evidence=add_evidence, update_entities=update_entities,
            ))

    async def _delegate_workflow(self, task: TaskRecord, decision: ControllerDecision) -> TaskState:
        urls = extract_urls(task.goal)
        objectives = decision.plan or ([decision.active_subgoal] if decision.active_subgoal else [])
        active = decision.active_subgoal or (objectives[0] if objectives else task.goal)
        self._mark_subgoal_active(active)
        n = min(len(urls), len(objectives))
        if n < 2:
            return await self._finish_delegate_step(
                task, success=False,
                failure_reason="resource_missing: delegate_workflow requested but fewer than "
                               "2 ordered target URLs with matching step objectives were "
                               "found in the goal text",
            )
        steps = [{"ordinal": i + 1, "target": urls[i], "objective": objectives[i]} for i in range(n)]
        workflow_id = f"{self.control_task_id}_wf_{uuid.uuid4().hex[:8]}"
        workflow_dir = self.tasks_dir / self.control_task_id / "delegates" / workflow_id
        self._append(EventType.DELEGATE_STARTED, {
            "substrate": "workflow", "subgoal": active, "child_task_id": workflow_id,
            "step_count": n, "delegate_dir": str(workflow_dir),
        })
        store = WorkflowStore.for_workflow_dir(workflow_dir)
        try:
            store.create_workflow(task.goal, steps, WorkflowPolicy(), workflow_id=workflow_id)
        finally:
            store.close()
        final = await self._run_workflow_delegate(workflow_dir, workflow_id)
        self._ingest_workflow_result(active, workflow_id, final)
        if final.get("status") != "completed":
            return await self._finish_delegate_step(
                task, success=False,
                failure_reason=f"repeated_failure: workflow delegate blocked ({final.get('blocked_reason')})",
            )
        return await self._finish_delegate_step(task, success=True)

    async def _run_workflow_delegate(self, workflow_dir: Path, workflow_id: str) -> dict:
        store = WorkflowStore.for_workflow_dir(workflow_dir)
        try:
            policy = WorkflowPolicy()
            orchestrator = WorkflowOrchestrator(
                self.config, store, workflow_id, policy,
                runner=self._child_runner, parent_task_id=self.control_task_id,
                approval_callback=self._approval_callback,
            )
            return await orchestrator.run()
        finally:
            store.close()

    def _ingest_workflow_result(self, subgoal: str, workflow_id: str, final: dict) -> None:
        result_event_id = self._append(EventType.DELEGATE_RESULT, {
            "substrate": "workflow", "subgoal": subgoal, "child_task_id": workflow_id,
            "status": final.get("status"), "blocked_reason": final.get("blocked_reason"),
        })
        add_facts: list[WorkspaceFact] = []
        add_evidence: list[EvidenceRef] = []
        for step in final.get("steps") or []:
            if not step.get("summary") and not step.get("facts"):
                continue
            key = f"workflow_step_result::{workflow_id}::{step['ordinal']}"
            add_facts.append(WorkspaceFact(key=key, value=step.get("summary") or ""))
            add_evidence.append(EvidenceRef(
                fact_key=key, source_event_id=result_event_id, source_url=step.get("target"),
                excerpt=(step.get("summary") or json.dumps(step.get("facts") or {}))[:500],
            ))
            for fact_key, fact_value in (step.get("facts") or {}).items():
                wf_key = f"workflow_fact::{fact_key}"
                add_facts.append(WorkspaceFact(key=wf_key, value=fact_value))
                add_evidence.append(EvidenceRef(
                    fact_key=wf_key, source_event_id=result_event_id, source_url=step.get("target"),
                    excerpt=str(fact_value)[:500],
                ))
        if add_facts:
            self.workspace_store.apply_patch(self.control_task_id, WorkspacePatch(
                add_facts=add_facts, add_evidence=add_evidence,
            ))

    async def _discover_sources(self, task: TaskRecord, decision: ControllerDecision) -> TaskState:
        active = decision.active_subgoal or task.goal
        self._mark_subgoal_active(active)
        if self.config.browser.mode == "cdp_attach":
            resolved = await self._resolve_against_known_resources(task, active)
            if resolved is not None:
                return resolved
        self._append(EventType.DELEGATE_STARTED, {
            "substrate": "research_discovery", "subgoal": active, "child_task_id": self.control_task_id,
            "objective": active,
        })
        return await self._run_discovery_and_replan(task, active, active)

    async def _resolve_against_known_resources(self, task: TaskRecord, objective: str) -> Optional[TaskState]:
        """Invariant: a subgoal must never reach for a live open-web search (`research_
        discovery`) before first checking whether it actually refers to a resource
        BrowserAgent already knows about — the currently attached page, or one of the user's
        other open tabs (persistent-browser mode only; `launch` mode has no such inventory to
        check). Live acceptance-test evidence (docs/BROWSERAGENT_MASTER_STATUS.md's FINAL
        ACCEPTANCE section, RC-2): three independent tasks phrased around the user's own
        context ("my open course pages", "this page", "the widget pages I have open") all
        skipped straight to `discover_sources`, and in one case navigated to and requested
        approval to click into a real, unrelated third party's website — a direct violation
        of the documented "never invents a URL" promise, since nothing ever checked the
        actually-open tabs first or asked when none matched.

        Reuses `router/policy.py::route()` verbatim — the exact deterministic
        classify-then-resolve pipeline (`router/semantic_planner.py` + `router/resources.py`)
        the legacy/hybrid dispatch path already relies on for this same decision — rather than
        reimplementing resource resolution here. That pipeline never invents a target: an
        unresolved open-tab/current-page reference becomes `NeedsInput` (clarification),
        exactly like every other dispatch path in this codebase. Returns None only when the
        classification itself determined this is a genuine open-web research need (an
        `OPEN_RESEARCH`-shaped plan with no open-tab/current-page resource requirement), in
        which case the caller proceeds to the pre-existing live web search unchanged — this is
        what keeps a real "research this topic" request (no first-person/current-context
        reference at all) working exactly as before.
        """
        try:
            result = await route(objective, self.llama, self.config)
        except RoutingError:
            return None  # classification itself failed — fall back to the pre-existing behavior
        if isinstance(result, NeedsInput):
            return self._block(result.question, kind="ask_user")
        if result.task_type == TaskType.RESEARCH and result.requires_discovery and not result.targets:
            return None  # genuine open-web research need — let the caller run discover_sources
        if result.task_type == TaskType.SINGLE_SITE and not result.targets:
            # CURRENT_PAGE (router/resources.py's own documented shape: "no requirement, or
            # only a current_page one: empty targets is the existing, correct signal AgentLoop
            # already handles"). Nothing to discover at all — the very next subgoal should
            # just work with whatever's already attached, exactly like every other current-
            # page task in this codebase. Fixing this bug found during the acceptance-fix
            # rerun (docs/BROWSERAGENT_MASTER_STATUS.md's FINAL ACCEPTANCE section): the
            # earlier `if not result.targets: return None` below treated this identically to
            # "nothing resolved," incorrectly falling through to a live web search anyway.
            return await self._replan_or_block(
                task, reason_hint=f"resource_discovered: {objective!r} refers to the current page; no discovery needed",
            )
        if not result.targets:
            return None  # defensive: nothing resolved and not a NeedsInput either

        result_event_id = self._append(EventType.DELEGATE_RESULT, {
            "substrate": "known_resource", "subgoal": objective, "child_task_id": self.control_task_id,
            "status": "completed", "resolved_targets": result.targets,
        })
        workspace = self.workspace_store.load(self.control_task_id)
        add_facts: list[WorkspaceFact] = []
        add_entities: list[WorkspaceEntity] = []
        add_evidence: list[EvidenceRef] = []
        if result.task_type == TaskType.MULTISITE_SWEEP:
            # Several real, already-open tabs matched — the same shape as several web-
            # discovered sources, so later subgoals can inspect/compare them as entities.
            for url in result.targets:
                entity_id = f"ent_{uuid.uuid4().hex[:10]}"
                add_entities.append(WorkspaceEntity(
                    id=entity_id, entity_type=_DISCOVERED_SOURCE_ENTITY_TYPE, name=url,
                    attributes={"url": url},
                ))
                add_evidence.append(EvidenceRef(
                    entity_id=entity_id, source_event_id=result_event_id, source_url=url,
                    excerpt=f"resolved from the user's own open browser tabs for: {objective}",
                ))
        else:
            # SINGLE_SITE: one already-open/current resource, not a list of candidates to
            # compare — anchor it as the task's hub so subsequent subgoals' AgentLoop
            # delegation actually attaches there (mirrors _remember_hub_url's idempotency).
            target_url = result.targets[0]
            if _HUB_URL_FACT_KEY not in workspace.facts:
                add_facts.append(WorkspaceFact(key=_HUB_URL_FACT_KEY, value=target_url))
            fact_key = f"known_resource::{objective}"
            add_facts.append(WorkspaceFact(key=fact_key, value=target_url))
            add_evidence.append(EvidenceRef(
                fact_key=fact_key, source_event_id=result_event_id, source_url=target_url,
                excerpt=f"resolved from the user's own current browser context for: {objective}",
            ))
        self.workspace_store.apply_patch(self.control_task_id, WorkspacePatch(
            add_facts=add_facts, add_entities=add_entities, add_evidence=add_evidence,
        ))
        return await self._replan_or_block(
            task,
            reason_hint=f"resource_discovered: resolved {len(result.targets)} known resource(s) for {objective!r}",
        )

    async def _run_discovery_and_replan(self, task: TaskRecord, subgoal: str, objective: str) -> TaskState:
        profile_dir = self.tasks_dir / self.control_task_id / "delegates" / "discovery_browser_profile"
        urls = await research_discovery.discover_sources(
            self.config, self.llama, objective, profile_dir,
            max_sources=self.config.agent.research_discovery_max_sources,
        )
        result_event_id = self._append(EventType.DELEGATE_RESULT, {
            "substrate": "research_discovery", "subgoal": subgoal, "child_task_id": self.control_task_id,
            "status": "completed", "source_count": len(urls),
        })
        if urls:
            add_entities: list[WorkspaceEntity] = []
            add_evidence: list[EvidenceRef] = []
            for url in urls:
                entity_id = f"ent_{uuid.uuid4().hex[:10]}"
                add_entities.append(WorkspaceEntity(
                    id=entity_id, entity_type=_DISCOVERED_SOURCE_ENTITY_TYPE, name=url,
                    attributes={"url": url},
                ))
                add_evidence.append(EvidenceRef(
                    entity_id=entity_id, source_event_id=result_event_id, source_url=url,
                    excerpt=f"discovered via search for: {objective}",
                ))
            self.workspace_store.apply_patch(self.control_task_id, WorkspacePatch(
                add_entities=add_entities, add_evidence=add_evidence,
            ))
        reason = (
            f"resource_discovered: found {len(urls)} candidate source(s) for {objective!r}"
            if urls else f"resource_missing: discovery for {objective!r} found no candidate sources"
        )
        return await self._replan_or_block(task, reason_hint=reason)

    # ---- continuous strategy (Phase 2 corrective pass) -------------------------------
    #
    # One AgentLoop task/event-log/browser session for the whole run instead of one per
    # subgoal. agent/loop.py's step() is completely unmodified except for the additive
    # `finish_intercept` hook it now offers a `finish` decision to before its own terminal
    # `_handle_finish` runs (see agent/loop.py's docstring on that param). Every action
    # decision, verification, safety gate (classify_risk/approval/runtime_policy), and
    # recovery escalation still goes through exactly the same code as every other AgentLoop
    # task — this section only ever touches the `finish` branch, never the action pipeline.

    async def _run_continuous(self, explicit_target_url: Optional[str] = None) -> TaskState:
        cfg = self.config.agent
        task = self.state_store.get_task_record(self.control_task_id)
        if task is None:
            raise ValueError(f"no such control task: {self.control_task_id}")

        await self._remember_hub_url(explicit_target_url)
        hub_url = self.workspace_store.load(self.control_task_id).facts.get(_HUB_URL_FACT_KEY)
        self._replans_used = self._count_replan_events()

        state = self.state_store.load(self.control_task_id)
        if not state.plan and state.status == "running" and state.current_subgoal is None:
            state = await self._initial_plan(task)

        # Bounded restart count for "loop.run() itself blocked because its own low-level
        # recovery ladder ran out of retries" — mirrors max_subgoal_attempts/max_replans'
        # existing budgets rather than inventing a new one; each restart still only happens
        # via _replan_or_block, so it shares max_replans' single budget below.
        for _ in range(cfg.max_replans + 2):
            state = self.state_store.load(self.control_task_id)
            if state.status != "running":
                break

            # explicit_target_url alone never navigates the browser (docs/BROWSERAGENT_MASTER_
            # STATUS.md's Phase 0 section: "the model's own open_url step then navigates the
            # blank page there") — every fresh browser session (the first one, and any restart
            # after a low-level-recovery-exhausted/consequential-refusal replan below) starts
            # on about:blank with no other grounding, exactly like a delegated-mode child would
            # if _subgoal_child_goal's "Open {target_url} first." intro were skipped. Mirror
            # that intro here via `goal_override` — a prompt-only override, never persisted —
            # rather than mutating current_subgoal/plan text (an earlier version of this fix
            # did that and corrupted completed_subgoals: memory/replay.py's SUBGOAL_CHANGED
            # handling treats any change to current_subgoal's *text* as "the old one is done,
            # superseded" even when it is really the same logical subgoal with a hint prepended).
            goal_override = f"Open {hub_url} first. {task.goal}" if hub_url else None
            loop = AgentLoop(
                self.config, self.control_task_id, explicit_target_url=hub_url,
                event_store=self.event_store, state_store=self.state_store,
                goal_override=goal_override, approval_callback=self._approval_callback,
            )
            if self._child_llama_client_factory is not None:
                loop.llama = self._child_llama_client_factory()
            loop.finish_intercept = self._make_continuous_finish_intercept(task)

            remaining_subgoals = max(1, len(state.plan) - len(state.completed_subgoals))
            step_budget = cfg.max_steps_per_subgoal * (remaining_subgoals + 1)
            state = await self._drive_continuous_session(loop, step_budget)

            if state.status == "blocked" and _is_replan_eligible_block(state.blocked_reason):
                state = await self._replan_or_block(
                    task, reason_hint=f"repeated_failure: subgoal {state.current_subgoal!r} "
                                      f"blocked ({state.blocked_reason})",
                )
                if state.status != "running":
                    return state
                continue  # rebuild a fresh AgentLoop bound to the same task_id and resume
            return state

        return self.state_store.load(self.control_task_id)

    async def _drive_continuous_session(self, loop: AgentLoop, step_budget: int) -> TaskState:
        """Runs `loop` one step at a time (agent/loop.py::AgentLoop.run_steps(1) repeatedly)
        instead of one big `loop.run(max_steps=step_budget)` call, so this controller regains
        control after every single action — needed to enforce `_subgoal_local_attempts`'
        per-subgoal limit below at the point it actually needs to fire, not only at the coarse
        "the whole session ended" boundary a single big `run()` call would offer. Starts/closes
        the browser exactly once, same as one `loop.run()` call would."""
        cfg = self.config.agent
        await loop.start_browser()
        # Every fresh browser session here (the first one, and any restart after a
        # controller-level replan) starts on about:blank with no other grounding
        # (agent/loop.py's own docstring on `explicit_target_url`: it "never navigates the
        # browser" by itself) — this used to be left entirely to the model's own `open_url`
        # decision, nudged only by `goal_override`'s text hint. Live forensic evidence (docs/
        # BROWSERAGENT_MASTER_STATUS.md's Phase 3 corrective pass, and the continued-validation
        # pass's own event-log forensics) showed Qwen3-8B does not reliably comply, both on a
        # session restart and — the dominant failure mode — on an ordinary in-session subgoal
        # ADVANCE, where nothing here previously navigated the browser at all: the very next
        # `run_steps(1)` call handed the model whatever page the *previous* subgoal happened to
        # leave open, with only a prompt hint (inference/prompt.py::render_subgoal_block) asking
        # it to notice and self-correct. `_reorient_for_subgoal` below replaces that reliance on
        # the model with the same deterministic-positioning discipline this codebase already uses
        # for CDP tab selection (browser/playwright_backend.py's own explicit_target_url/
        # preferred_tab_url handling) — resolved via `_resolve_subgoal_resource`, never invented
        # by the model, and a no-op whenever the browser is already positioned correctly (no
        # blind reload, no new tab: single-page backend throughout this controller).
        state = self.state_store.load(self.control_task_id)
        await self._reorient_for_subgoal(loop, state.current_subgoal)
        prev_subgoal = state.current_subgoal
        try:
            for _ in range(step_budget):
                state = await loop.run_steps(1)
                if state.status != "running":
                    return state
                if state.current_subgoal != prev_subgoal:
                    await self._reorient_for_subgoal(loop, state.current_subgoal)
                    prev_subgoal = state.current_subgoal
                if state.current_subgoal is not None and self._subgoal_local_attempts() >= cfg.max_subgoal_attempts:
                    # agent/loop.py's own internal low-level replan (build_replan_prompt, fired
                    # when the action-level recovery ladder reaches REPLAN_REQUIRED) has already
                    # rescued the active subgoal `max_subgoal_attempts` times with no forward
                    # progress recorded against it (docs/BROWSERAGENT_MASTER_STATUS.md's Phase 2
                    # corrective-pass section: this was the diagnosed root cause of the 0/5
                    # multi_step_registration failure — that internal replan mechanism has no
                    # bound of its own, and continuous mode's much larger multi-subgoal step
                    # budget gave it far more room to cycle than any single-subgoal task ever
                    # would). Block with a reason _is_replan_eligible_block recognizes, so the
                    # outer loop in _run_continuous re-grounds via a real controller-level
                    # replan and a fresh, hub-grounded browser session next — never silently
                    # keep burning the shared step budget on a subgoal that keeps needing
                    # rescuing without ever actually finishing.
                    return self._block(
                        f"subgoal local attempts exhausted: {state.current_subgoal!r} needed "
                        f"agent/loop.py's own internal replan {cfg.max_subgoal_attempts} "
                        "time(s) with no forward progress"
                    )
            return self.state_store.load(self.control_task_id)
        finally:
            await loop.aclose()

    # ---- deterministic resource-bound subgoal reorientation (Phase 3 continued-validation) --
    #
    # The model creates the procedure (what to do); the controller owns resource identity
    # (what page that procedure actually applies to); the browser deterministically positions
    # itself there before the model is ever asked to act. Built entirely from existing,
    # already-proven identity machinery — workspace_ops's name-matching/inference (already used
    # by _recover_desynced_subgoal and _evidence_source_is_stale) and browser/playwright_
    # backend.py's own URL-identity helper (already used for CDP tab reuse) — no new ID scheme,
    # no ResourceResolver/CDP dependency beyond what already exists, no domain vocabulary
    # anywhere below. Recomputed fresh from persisted workspace evidence on every call, never
    # cached in memory, so a controller-level replan that merely paraphrases a subgoal's text
    # (Section 5's "replan invariant") preserves the binding automatically as long as the
    # paraphrase still names (or, via `preceding`, is immediately preceded by a step naming) the
    # same real-world candidate — exactly the same imprecise-label tolerance
    # names_plausibly_match already provides everywhere else in this file.

    async def _reorient_for_subgoal(self, loop: AgentLoop, subgoal: Optional[str]) -> None:
        """Deterministic browser reorientation (no LLM call) before `subgoal` is attempted.
        Resolves `subgoal`'s bound resource (`_resolve_subgoal_resource`) and navigates there
        only when the browser isn't already positioned on it — never a blind reload of the
        current URL, never a new tab (this controller's PlaywrightBackend is single-page in
        both `launch` and `cdp_attach` mode). Exceptions are swallowed exactly like the prior
        session-start-only hub navigation this generalizes already did: worst case, the model's
        own recovery ladder/prompt-hint fallback (inference/prompt.py::render_subgoal_block)
        handles it from wherever the browser already is — never worse than before this fix."""
        if subgoal is None or loop.browser.page is None:
            return
        state = self.state_store.load(self.control_task_id)
        # Falls back to the loop's own explicit_target_url (the pre-existing, simpler
        # session-start-only guarantee this generalizes) whenever the resolver itself has
        # nothing to go on yet — e.g. no _hub_url workspace fact recorded at all, the shape
        # every caller outside of _run_continuous's own normal flow (which always calls
        # _remember_hub_url first) is in.
        target = self._resolve_subgoal_resource(subgoal, state.plan) or loop.browser.explicit_target_url
        if not target:
            return
        current = loop.browser.page.url
        if current and urls_match(current, target):
            return
        try:
            await loop.browser.open_url(target)
        except Exception:
            pass

    def _resolve_subgoal_resource(self, subgoal: str, plan: list[str]) -> Optional[str]:
        """The deterministic resource binding for `subgoal` (Section 2: "a subgoal should
        contain or reference the canonical resolved resource identity where available").

        A candidate name is inferred from `subgoal`'s own text — falling back to the
        immediately preceding plan item's text when `subgoal` itself names no candidate (e.g.
        "record structured finding"; the same reasoning entity_patch_from_findings's own
        `preceding_subgoal` parameter already documents: a live planner sometimes atomizes one
        candidate into several subgoals whose later ones never repeat its name). If that name
        plausibly matches an already-collected active entity (`_find_active_entity_by_name` —
        same fuzzy match used for desync recovery), this is a REVISIT of an already-discovered
        candidate: resolve straight to that entity's own last-known non-hub evidence URL, never
        back through the hub. Otherwise — an unvisited candidate, a directory/navigation
        subgoal, or a synthesis subgoal naming no single candidate — resolve to the task's hub
        URL, the safe deterministic entry point every candidate is reachable from. Returns None
        only when there is no hub URL at all (a task with no explicit_target_url), meaning no
        deterministic action is possible here."""
        workspace = self.workspace_store.load(self.control_task_id)
        hub_url = workspace.facts.get(_HUB_URL_FACT_KEY)
        idx = plan.index(subgoal) if subgoal in plan else -1
        preceding = plan[idx - 1] if idx > 0 else None
        candidate_name = (
            workspace_ops.infer_entity_name(None, subgoal)
            or (workspace_ops.infer_entity_name(None, preceding) if preceding else None)
        )
        if candidate_name:
            entity = self._find_active_entity_by_name(candidate_name)
            if entity is not None:
                resource_url = self._latest_non_hub_evidence_url(entity, hub_url, workspace)
                if resource_url:
                    return resource_url
        return hub_url

    def _latest_non_hub_evidence_url(
        self, entity: WorkspaceEntity, hub_url: Optional[str], workspace: WorkspaceView,
    ) -> Optional[str]:
        """The most recent page URL this entity's own evidence was actually sourced from,
        excluding the hub (the hub/listing page never carries any one candidate's own
        attributes in this architecture's hub-and-branch shape — see
        `_evidence_source_is_stale`'s own docstring for the same point). None when this entity
        has no such evidence yet (nothing deterministic to resolve to beyond the hub)."""
        candidates = [
            ev for ev in workspace.evidence
            if ev.entity_id == entity.id and ev.source_url and ev.source_url != hub_url
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda ev: ev.source_event_id).source_url

    def _subgoal_local_attempts(self) -> int:
        """How many times agent/loop.py's own internal low-level replan has already rescued
        the active subgoal since this controller itself last set it. The continuous strategy's
        per-subgoal analogue of the delegated strategy's `_attempts_for_current_subgoal` (which
        counts fresh-child `DELEGATE_STARTED` attempts instead, since delegated mode has no
        shared low-level recovery ladder to reuse: retry_count/recovery_level restart at
        NORMAL/0 for every fresh child task already).

        Deliberately NOT keyed by subgoal *text* equality: `agent/loop.py::_replan()` produces
        a freshly model-worded subgoal string every time it fires (its own prompt asks for one
        in free text), so consecutive low-level replans essentially never share an identical
        subgoal string — a text-matched counter would (and, in an earlier version of this fix,
        did) never accumulate past 1, since "attempt #2" always lands on a *different* string
        than "attempt #1". Instead, every SUBGOAL_CHANGED this controller itself appends (see
        `_apply_controller_decision`/`_advance_subgoal`/the finish intercept's advance branch)
        is additively tagged `"source": "controller"` — a harmless extra payload key
        memory/replay.py already ignores — so this counts RECOVERY_TRANSITION events with
        `reason == "replanned"` (the one place in the codebase agent/loop.py::_replan() appends
        one) after the most recent *controller-tagged* SUBGOAL_CHANGED, regardless of how many
        different-text subgoals agent/loop.py's own mechanism produced in between. Recomputed
        fresh from the event log every call — no in-memory counter, so a crash mid-subgoal and
        a resumed `_run_continuous` reconstruct the exact same count from persisted events
        alone (item 7)."""
        events = self.event_store.all_events(self.control_task_id)
        last_controller_change_id = 0
        for e in events:
            if e.type == EventType.SUBGOAL_CHANGED and e.payload.get("source") == "controller":
                last_controller_change_id = e.id
        return sum(
            1 for e in events
            if e.type == EventType.RECOVERY_TRANSITION
            and e.id > last_controller_change_id
            and e.payload.get("reason") in ("replanned", "premature_finish_rejected")
        )

    def _reject_premature_finish(self, decision: ModelDecision) -> TaskState:
        """Reject a `finish` that lacks completion evidence for the *current* subgoal (Phase 3
        fix — see the finish intercept's call site for why this no longer falls through to
        agent/loop.py's own _handle_finish). Appends a failing VERIFICATION_RESULT (so the
        rejection is visible in the next step's RECENT ACTIONS context, nudging the model
        toward a different action) and tags it as a RECOVERY_TRANSITION with reason
        "premature_finish_rejected" — counted by _subgoal_local_attempts alongside
        agent/loop.py's own internal "replanned" transitions, so repeated premature finishes on
        the same subgoal are bounded by the exact same max_subgoal_attempts budget and
        eventually force a controller-level replan rather than looping forever. Status stays
        "running": the outer run_steps loop in _drive_continuous_session simply continues."""
        self._append(EventType.VERIFICATION_RESULT, {
            "action": ActionType.FINISH.value, "target": None, "action_fingerprint": "finish",
            "result_data": {"result": decision.params.get("result", "")},
        })
        self._append(EventType.MODEL_DECISION, {
            "error": ValidationErrorKind.MODEL_COMPLETION_ERROR.value,
            "message": "finish rejected: no structured evidence yet for THIS subgoal specifically "
                       "(not a prior one). The current page may not be the right one for this "
                       "subgoal — if so, navigate to it first (e.g. use the directory/listing "
                       "page's own link), then extract the requested data before finishing again.",
        })
        self._append(EventType.RECOVERY_TRANSITION, {"reason": "premature_finish_rejected"})
        return self.state_store.load(self.control_task_id)

    def _make_continuous_finish_intercept(
        self, task: TaskRecord,
    ) -> Callable[[ModelDecision, TaskState, PageObservation], Awaitable[Optional[TaskState]]]:
        async def intercept(decision: ModelDecision, state: TaskState, observation: PageObservation) -> Optional[TaskState]:
            subgoal = state.current_subgoal
            plan = state.plan
            if subgoal is None or subgoal not in plan:
                # agent/loop.py's own internal low-level replan (build_replan_prompt, fired on
                # repeated action failure, unrelated to this controller) can rename
                # current_subgoal/plan out from under this controller's own plan. Live forensic
                # evidence (docs/BROWSERAGENT_MASTER_STATUS.md's Phase 3 corrective pass) showed
                # this is not rare: it was the dominant failure mode for 2 of 5 holdout domains,
                # and a real planner replan call here cannot see what the model just
                # accomplished — it was observed live to just re-propose the same unrecognized
                # subgoal text every time, burning the entire replan budget while discarding
                # perfectly good, evidence-backed findings. Before paying for that call, try a
                # deterministic reconciliation first: does this finish's own evidence identify
                # one of the controller's own still-pending plan items by name? If so, that item
                # actually did just get completed, under a different bookkeeping label — credit
                # it directly (below, exactly like a non-desynced finish) instead of replanning
                # from scratch. Only ever recovers to an existing plan item's *exact* text, so a
                # genuinely ambiguous/novel desync still falls through to a real replan.
                recovered = self._recover_desynced_subgoal(plan, state.completed_subgoals, decision)
                if recovered is None:
                    return await self._replan_or_block(
                        task, reason_hint=f"desynced_subgoal: model called finish for {subgoal!r}, "
                                          "which this controller's own current plan does not recognize",
                    )
                subgoal = recovered

            idx = plan.index(subgoal)
            is_last = idx == len(plan) - 1
            if not self._continuous_subgoal_has_evidence(subgoal, decision, observation.url, plan):
                # No verifiable completion evidence for *this* subgoal yet (item 9: a subgoal
                # is not done just because the model says so). Deliberately does NOT fall
                # through to agent/loop.py's own _handle_finish here (an earlier version of
                # this code did, returning None) — Phase 3's live benchmark surfaced a real bug
                # in that plan: _handle_finish's "any prior verified action already passed"
                # escape hatch was built for a single self-contained task, so the very first
                # subgoal's own opening navigation (already verified) let a premature,
                # evidence-less finish silently complete the WHOLE multi-subgoal continuous
                # task instead of just failing this one subgoal (see docs/BROWSERAGENT_MASTER_
                # STATUS.md's Phase 3 section for the reproduction). Rejecting here instead
                # keeps the task running and gives this subgoal specifically another chance.
                return self._reject_premature_finish(decision)

            preceding_subgoal = plan[idx - 1] if idx > 0 else None
            self._ingest_continuous_subgoal_result(subgoal, decision, observation, preceding_subgoal)

            # Always advance via SUBGOAL_CHANGED (mirroring the delegated strategy's own
            # _advance_subgoal) — including moving to None on the last subgoal, which is what
            # makes memory/replay.py fold `subgoal` into completed_subgoals. A direct _finish()
            # without this step would silently drop the final subgoal from completed_subgoals.
            next_subgoal = plan[idx + 1] if not is_last else None
            self._append(EventType.SUBGOAL_CHANGED, {"subgoal": next_subgoal, "plan": plan, "source": "controller"})
            state = self._reset_recovery_for_new_subgoal()
            if next_subgoal is not None:
                return state

            # Final subgoal believed done: one mandatory whole-goal completion check — the
            # same single boundary _evaluate_and_replan_or_finish already performs when the
            # (delegated-mode) plan is exhausted, not a new call type, and not paid for any
            # *intermediate* subgoal (item 8: no planner call after every subgoal).
            workspace = self.workspace_store.load(self.control_task_id)
            try:
                evaluation = await self._planner_evaluate_completion(task, workspace)
            except PlannerOutputError as exc:
                return self._block(f"completion evaluation failed schema validation: {exc}")
            self._append(EventType.COMPLETION_EVALUATED, evaluation.model_dump())
            if evaluation.satisfied:
                return await self._finish(evaluation, completion_claim=decision.params.get("result"))
            if evaluation.next_recommendation == "ask_user":
                return self._block("; ".join(evaluation.missing_requirements) or "clarification needed")
            reason = "completion_rejection: " + (
                "; ".join(evaluation.missing_requirements) or "final subgoal complete but goal unsatisfied"
            )
            return await self._replan_or_block(task, reason_hint=reason)

        return intercept

    def _recover_desynced_subgoal(
        self, plan: list[str], completed_subgoals: list[str], decision: ModelDecision,
    ) -> Optional[str]:
        """Deterministic recovery for the desynced-subgoal case above: match the finish
        decision's own evidence to one of the controller's own still-pending plan items by
        name — pure string comparison, no LLM call. Returns that plan item's exact text (so the
        caller's normal advance-the-plan logic can treat it identically to a non-desynced
        finish), or None when nothing in the plan plausibly and unambiguously matches (still
        needs a real replan)."""
        result_text = str(decision.params.get("result", ""))
        structured = workspace_ops.coerce_structured_result(decision.params.get("structured_result"), result_text)
        candidate_name = workspace_ops.infer_entity_name(structured if isinstance(structured, dict) else None, result_text)
        if not candidate_name:
            return None
        # A raw identity-field value can carry noisy trailing prose (e.g. a kv-fallback "name"
        # of "DataForge Analytics Intern details recorded") that breaks a plain substring match
        # against a pending plan item's cleaner subgoal text — strip to the core capitalized
        # phrase first, falling back to the raw value if nothing capitalized was found.
        refined_name = workspace_ops.extract_title_phrase(candidate_name) or candidate_name
        pending = [s for s in plan if s not in completed_subgoals]
        matches = [s for s in pending if workspace_ops.names_plausibly_match(refined_name, s)]
        return matches[0] if len(matches) == 1 else None

    def _continuous_subgoal_has_evidence(
        self, subgoal: str, decision: ModelDecision, observation_url: Optional[str] = None,
        plan: Optional[list[str]] = None,
    ) -> bool:
        """Deterministic subgoal-completion evidence check (item 9/18: no new LLM call merely
        to judge this) — same evidence bar agent/loop.py's own _has_evidence_backed_structured_
        result uses for a batch child's finish, plus "a verified action happened since this
        subgoal became active" for interactive (non-extraction) subgoals."""
        structured = workspace_ops.coerce_structured_result(
            decision.params.get("structured_result"), str(decision.params.get("result", "")),
        )
        if isinstance(structured, dict):
            findings = structured.get("findings")
            has_findings = isinstance(findings, list) and any(
                isinstance(f, dict) and str(f.get("evidence") or "").strip() and str(f.get("value") or "").strip()
                for f in findings
            )
            fields = structured.get("fields")
            has_found_fields = isinstance(fields, dict) and any(
                isinstance(f, dict) and f.get("status") == "found" and str(f.get("evidence") or "").strip()
                for f in fields.values()
            )
            if has_findings or has_found_fields:
                if self._evidence_source_is_stale(subgoal, structured, observation_url, plan):
                    return False
                return True
        # A synthesis-only subgoal (Section 9's generic top-k detector, workspace_ops::
        # parse_requested_top_k — the same one _build_result_patch already uses to tell a
        # "report the top-k" step apart from a new-candidate-collection step) has no page of its
        # own to extract evidence from — it is pure reasoning over candidates already collected
        # and evidenced by their OWN earlier subgoals. Live forensic finding (Phase 3 continued-
        # validation pass): holding it to the same extraction-evidence bar as every other subgoal
        # rejected a verbatim-correct finish forever (the model correctly named the top-k
        # candidates from already-recorded facts, produced no `structured_result`/fresh
        # verification since nothing on the current page needed acting on, and every replan just
        # re-proposed the identical unsatisfiable bar) — exhausting the entire replan budget on a
        # subgoal that was already answerable from persisted state. Accepting it here only lets
        # the subgoal ADVANCE to the whole-goal completion check below; it never itself selects or
        # fabricates a final entity — that stays exclusively _finish -> _maybe_select_top_k_
        # entities's own deterministic (or, failing that, id-only anti-hallucination) path.
        k = workspace_ops.parse_requested_top_k(subgoal)
        if k is not None:
            workspace = self.workspace_store.load(self.control_task_id)
            if sum(1 for e in workspace.entities if e.status == "active") >= k:
                return True
        events = self.event_store.all_events(self.control_task_id)
        last_change_id = 0
        for e in events:
            if e.type == EventType.SUBGOAL_CHANGED and e.payload.get("subgoal") == subgoal:
                last_change_id = e.id
        return any(
            e.type == EventType.VERIFICATION_RESULT and e.id > last_change_id
            and bool(e.verification_result and e.verification_result.get("passed"))
            for e in events
        )

    def _evidence_source_is_stale(
        self, subgoal: str, structured: dict, observation_url: Optional[str],
        plan: Optional[list[str]] = None,
    ) -> bool:
        """Grounding check for a per-candidate subgoal's finish (Phase 3 corrective pass, live
        forensic finding): a plausible-looking `structured_result` is not enough on its own —
        it must actually have been sourced from a page belonging to *this* subgoal's own
        candidate, not stale data left visible from a sibling candidate's page the browser
        never navigated away from. Live evidence: a passing trial still produced one entity
        with another entity's exact attributes copied verbatim because the model finished
        without navigating; the existing evidence check had no way to catch it since the
        `structured_result` itself looked perfectly well-formed. Three checks, all
        deterministic and entity-generic (no product/domain vocabulary):

        1. The directory/listing (hub) page itself never carries any one candidate's own
           attributes in this architecture's hub-and-branch shape — findings "sourced" from it
           are never valid evidence for a specific candidate.
        2. When `plan` is given and this subgoal already resolves (`_resolve_subgoal_resource`)
           to a KNOWN, previously-discovered candidate page (i.e. this is a revisit, not this
           candidate's first visit), the current observation must be exactly that page — a hard
           identity check, not a name heuristic. Live forensic finding (Phase 3 continued-
           validation pass): the weaker check #3 below is symmetric-blind to a *forward*
           overshoot — a subgoal for a candidate that already has its own known page can still
           get "credited" with a finding sourced from a page belonging to a *sibling* candidate
           it had not visited before (so check #3's "already claimed by someone else" lookup
           finds nothing), permanently mislabeling that finding under the wrong entity and, worse,
           poisoning check #3 for every later, genuinely correct visit to the real owner of that
           URL (observed live: this exact chain blocked a domain's entire replan budget on a
           candidate whose own page was never actually revisited, only ever borrowed by a
           sibling's earlier overshoot).
        3. If this exact URL was already used as evidence for a *different*-named entity, this
           finish is reusing a sibling's page rather than having visited its own — unless the
           subgoal's own implied candidate name plausibly matches that entity (a legitimate
           re-confirmation of the same real-world candidate, not a conflict).
        """
        if not observation_url:
            return False
        workspace = self.workspace_store.load(self.control_task_id)
        hub_url = workspace.facts.get(_HUB_URL_FACT_KEY)
        expected_name = workspace_ops.infer_entity_name(structured, subgoal)
        if not expected_name:
            return False
        if hub_url and observation_url == hub_url:
            return True
        if plan is not None:
            bound = self._resolve_subgoal_resource(subgoal, plan)
            if bound and bound != hub_url and not urls_match(bound, observation_url):
                return True
        entities_by_id = {e.id: e for e in workspace.entities}
        for ev in workspace.evidence:
            if ev.source_url != observation_url or not ev.entity_id:
                continue
            entity = entities_by_id.get(ev.entity_id)
            if entity is None or workspace_ops.names_plausibly_match(entity.name, expected_name):
                continue
            return True
        return False

    def _ingest_continuous_subgoal_result(
        self, subgoal: str, decision: ModelDecision, observation: PageObservation,
        preceding_subgoal: Optional[str] = None,
    ) -> None:
        result_text = str(decision.params.get("result", ""))
        structured_result = decision.params.get("structured_result")
        result_event_id = self._append(EventType.DELEGATE_RESULT, {
            "substrate": "continuous_step", "subgoal": subgoal, "child_task_id": self.control_task_id,
            "status": "completed", "result": result_text, "final_url": observation.url,
            "structured_result": structured_result, "blocked_reason": None,
        })
        if result_text or structured_result:
            patch = self._build_result_patch(
                subgoal, result_text, structured_result, result_event_id, observation.url,
                preceding_subgoal,
            )
            self.workspace_store.apply_patch(self.control_task_id, patch)

    def _reset_recovery_for_new_subgoal(self) -> TaskState:
        """A subgoal transition (advance or replan) is a natural point to reset the local
        action-level recovery ladder — a repeated-click failure on the *previous* subgoal's
        page must not immediately re-trip DEEP_RECOVERY/USER_REQUIRED on the *next* subgoal's
        first action. Never clears completed_subgoals/workspace facts/plan history (item 13) —
        only the transient recovery_level/retry_count/status fields agent/loop.py's own
        recovery ladder owns. A no-op for delegated-mode callers (a fresh child TaskState
        already starts at NORMAL/0), so this is safe to share between both strategies."""
        state = self.state_store.load(self.control_task_id)
        state.status = "running"
        state.recovery_level = RecoveryLevel.NORMAL.value
        state.retry_count = 0
        self.state_store.save(state)
        return state

    # ---- crash recovery ---------------------------------------------------------------

    async def _reconcile_dangling_delegate(self, task: TaskRecord) -> Optional[TaskState]:
        """A DELEGATE_STARTED with no matching DELEGATE_RESULT means the process died while a
        delegate was running (or between the delegate finishing and this controller ingesting
        its result). Resume that exact delegate — never start a fresh duplicate — before doing
        anything else, mirroring AgentLoop's own `_reconcile_pending_intent`. Branches on the
        persisted `substrate` (Phase 4): a plain AgentLoop child resumes via `AgentLoop.resume`
        exactly as before; a batch/workflow delegate reopens its own already-durable store at
        the persisted `delegate_dir`/id and simply calls `.run()` again — BatchOrchestrator/
        WorkflowOrchestrator already reconcile their own in-flight work items on `.run()`, so
        no new resume mechanism is needed here, only reconstructing the same object with the
        same on-disk identity. A dangling research_discovery delegate has no partial state of
        its own (one bounded, read-only search round) — resumption simply retries the exact
        same objective."""
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
        substrate = event.payload.get("substrate", "agent_loop")
        if substrate == "batch":
            return await self._resume_batch_delegate(task, subgoal, event)
        if substrate == "workflow":
            return await self._resume_workflow_delegate(task, subgoal, event)
        if substrate == "research_discovery":
            objective = event.payload.get("objective", subgoal)
            return await self._run_discovery_and_replan(task, subgoal, objective)
        loop = AgentLoop.resume(self.config, child_task_id, approval_callback=self._approval_callback)
        if self._child_llama_client_factory is not None:
            loop.llama = self._child_llama_client_factory()
        child_state = await loop.run(max_steps=self.config.agent.max_steps_per_subgoal)
        return await self._handle_subgoal_child_result(task, subgoal, child_task_id, child_state)

    async def _resume_batch_delegate(self, task: TaskRecord, subgoal: str, event: Event) -> TaskState:
        batch_id = event.payload["child_task_id"]
        batch_dir = Path(event.payload["delegate_dir"])
        final = await self._run_batch_delegate(batch_dir, batch_id)
        self._ingest_batch_result(subgoal, batch_id, final)
        return await self._finish_delegate_step(task, success=True)

    async def _resume_workflow_delegate(self, task: TaskRecord, subgoal: str, event: Event) -> TaskState:
        workflow_id = event.payload["child_task_id"]
        workflow_dir = Path(event.payload["delegate_dir"])
        final = await self._run_workflow_delegate(workflow_dir, workflow_id)
        self._ingest_workflow_result(subgoal, workflow_id, final)
        if final.get("status") != "completed":
            return await self._finish_delegate_step(
                task, success=False,
                failure_reason=f"repeated_failure: workflow delegate blocked ({final.get('blocked_reason')})",
            )
        return await self._finish_delegate_step(task, success=True)

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
        # Controller-tagged only (see `_subgoal_local_attempts`'s docstring): in the continuous
        # strategy, agent/loop.py's own internal low-level replan also appends untagged
        # SUBGOAL_CHANGED events onto this same shared event log — those are not a controller-
        # level replan and must never count against max_replans' budget, or the controller
        # would exhaust its own replan budget purely from the executor's low-level recovery
        # ladder doing its own, unrelated thing.
        events = self.event_store.all_events(self.control_task_id)
        subgoal_changes = sum(
            1 for e in events
            if e.type == EventType.SUBGOAL_CHANGED and e.payload.get("source") == "controller"
        )
        return max(0, subgoal_changes - 1)

    def _append(self, event_type: EventType, payload: dict) -> int:
        state = self.state_store.load(self.control_task_id)
        return self.event_store.append(self.control_task_id, state.current_step + 1, event_type, payload)

    async def _finish(self, evaluation: CompletionEvaluation, completion_claim: Optional[str] = None) -> TaskState:
        prior = self.state_store.load(self.control_task_id)
        task = self.state_store.get_task_record(self.control_task_id)
        entity_report = await self._maybe_select_top_k_entities(task) if task is not None else None
        result = entity_report or completion_claim or "; ".join(prior.completed_subgoals) or "goal satisfied"
        self._append(EventType.TASK_COMPLETED, {
            "result": result,
            "missing_requirements": evaluation.missing_requirements,
            "unsupported_claims": evaluation.unsupported_claims,
        })
        state = self.state_store.load(self.control_task_id)
        state.status = "completed"
        self.state_store.save(state)
        return state

    def _block(self, reason: str, kind: str = "failure") -> TaskState:
        """`kind` (Phase 5, ui/jobs.py's own general-mode driver): additive TASK_BLOCKED
        payload key distinguishing an `ask_user` block (the planner genuinely wants a
        clarifying answer — the UI should offer a text box, mirroring the router's own
        NeedsInput flow) from every other block (a real failure/exhausted-budget/declined-
        approval stop — the UI should just report it). memory/replay.py already ignores
        unrecognized payload keys, so this is inert for every reader that predates it; only
        ui/jobs.py's new `_is_ask_user_block` helper reads it, straight from the event log."""
        self._append(EventType.TASK_BLOCKED, {"reason": reason, "kind": kind})
        state = self.state_store.load(self.control_task_id)
        state.status = "blocked"
        state.blocked_reason = reason
        self.state_store.save(state)
        return state

    def resume_after_clarification(self, answer: str) -> None:
        """Phase 5: the general-controller analogue of router/policy.py's own
        `route_with_answer` — records the user's answer as a workspace fact (so the next
        planner call sees it in the WORKSPACE summary exactly like any other discovered fact)
        and clears the block so the caller's next `run()` call resumes normally. Never resets
        plan/completed_subgoals/replan budget — a clarification answer informs the *next*
        planning decision, it does not restart the task."""
        self.workspace_store.apply_patch(self.control_task_id, WorkspacePatch(
            add_facts=[WorkspaceFact(key=f"user_clarification_{uuid.uuid4().hex[:6]}", value=answer)],
        ))
        state = self.state_store.load(self.control_task_id)
        state.status = "running"
        state.blocked_reason = None
        self.state_store.save(state)
