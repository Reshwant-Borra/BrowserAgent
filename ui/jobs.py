"""Drives one UI job end to end: routes the prompt, then calls straight into the existing
engine (AgentLoop / BatchOrchestrator / WorkflowOrchestrator) — this module never re-executes
anything, it just supervises: publishes progress into UIJobStore after each step/item, bridges
approval/login pauses to HTTP requests, and honors a cooperative stop flag between steps
(Section 22: "stop before next model/browser action where practical... do not corrupt state").
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Optional

from agent.auth_detect import looks_like_login_page
from agent.config import AppConfig, load_config
from agent.loop import AgentLoop
from agent.schemas import ModelDecision
from batch.models import ResultContract
from batch.orchestrator import BatchOrchestrator
from batch.store import BatchStore
from browser.page_model import ElementRef
from inference.llama_client import InferenceClient, create_inference_client
from research.discovery import discover_sources
from router.plan_schema import ReplanDecisionKind
from router.policy import NeedsInput, RoutingError, route, route_with_answer, to_batch_policy
from router.replanner import ReplanOutputError, decide_replan, finding_source_url
from router.schema import RouterDecision, TargetResourceKind, TaskType
from ui.store import UIJobStore
from workflow.models import WorkflowPolicy
from workflow.orchestrator import WorkflowOrchestrator
from workflow.store import WorkflowStore

RESEARCH_MAX_SOURCES_DEFAULT = 20
# Bounds the clarification loop (Section 14 of the semantic planner task): a resource that's
# still unresolved after this many user responses stops asking and fails cleanly instead of
# turning into the "continuous free-form planning loop" Section 12 explicitly forbids.
MAX_CLARIFICATION_ROUNDS = 3

# A job in any of these statuses is done; `stop()` on one of them is always a harmless
# no-op rather than an error, and a job outside this set is always what "non-terminal"
# means for Stop/Run-button purposes.
TERMINAL_JOB_STATUSES = {"completed", "failed", "stopped"}


def _assignment_contract() -> ResultContract:
    from cli.main import _contract_from_name
    return _contract_from_name("assignment")


def _research_contract() -> ResultContract:
    from cli.main import _contract_from_name
    return _contract_from_name("research")


class _JobControl:
    def __init__(self) -> None:
        self.stop_event = asyncio.Event()
        self.login_event = asyncio.Event()
        self.approval_future: Optional[asyncio.Future] = None
        self.clarification_future: Optional[asyncio.Future] = None


class JobRunner:
    def __init__(self, config: AppConfig, store: UIJobStore, runtime_dir: Path,
                 research_max_sources: int = RESEARCH_MAX_SOURCES_DEFAULT):
        self.config = config
        self.store = store
        self.runtime_dir = Path(runtime_dir)
        self.research_max_sources = research_max_sources
        self._control: dict[str, _JobControl] = {}
        self._tasks: dict[str, asyncio.Task] = {}

    # ---- public control surface (called by ui/app.py routes) ---------------

    def submit(self, prompt: str) -> str:
        job_id = self.store.create(prompt)
        self._control[job_id] = _JobControl()
        self._tasks[job_id] = asyncio.create_task(self._drive(job_id, prompt))
        return job_id

    def approve(self, job_id: str, approved: bool) -> bool:
        control = self._control.get(job_id)
        if control is None or control.approval_future is None or control.approval_future.done():
            return False
        control.approval_future.set_result(approved)
        return True

    def login_continue(self, job_id: str) -> bool:
        control = self._control.get(job_id)
        if control is None:
            return False
        control.login_event.set()
        return True

    def clarify(self, job_id: str, answer_text: str) -> bool:
        control = self._control.get(job_id)
        if control is None or control.clarification_future is None or control.clarification_future.done():
            return False
        control.clarification_future.set_result(answer_text)
        return True

    def stop(self, job_id: str) -> bool:
        """Idempotent and safe to call on a job this process has no live `_JobControl` for.

        That "no control" case is not rare: it's exactly what a persisted `waiting_for_input`/
        `waiting_for_login`/`waiting_for_approval` job looks like after the UI server has been
        restarted (`_control` is in-memory only; `UIJobStore` is the durable record — Section
        59/11). Before this fix, `stop()` returned False here, the HTTP layer turned that into
        a 404, the frontend silently ignored it, and the persisted non-terminal job sat there
        forever — which is also exactly what the frontend's own "reconnect to whatever's still
        active on page load" logic (ui/static/index.html's `init()`) picks up, permanently
        disabling Run. Falling back to a direct, idempotent store transition here closes that
        hole without needing the original driving process to still exist.
        """
        control = self._control.get(job_id)
        if control is not None:
            control.stop_event.set()
            control.login_event.set()  # unblock a login wait so the driver can observe stop
            if control.approval_future is not None and not control.approval_future.done():
                control.approval_future.set_result(False)
            if control.clarification_future is not None and not control.clarification_future.done():
                control.clarification_future.set_result(None)
            return True

        job = self.store.get(job_id)
        if job is None:
            return False
        if job["status"] in TERMINAL_JOB_STATUSES:
            return True  # already done: stopping again is a harmless no-op, not an error
        self.store.update(
            job_id, status="stopped", activity="Stopped",
            pending_approval=None, pending_clarification=None,
        )
        return True

    # ---- driver --------------------------------------------------------

    async def _drive(self, job_id: str, prompt: str) -> None:
        control = self._control[job_id]
        try:
            client = create_inference_client(self.config)
            self.store.update(job_id, activity="Understanding task...")
            try:
                decision = await self._route_with_clarification(job_id, prompt, client, control)
            except RoutingError as exc:
                self.store.update(job_id, status="failed", error=str(exc), activity="Could not route task")
                return
            if decision is None:  # stopped while waiting on a clarification, or rounds exhausted
                return
            self.store.update(
                job_id, kind=decision.task_type.value, router_decision=decision.model_dump(mode="json"),
                status="running", activity=f"Routed as {decision.task_type.value}",
            )
            if control.stop_event.is_set():
                self.store.update(job_id, status="stopped", activity="Stopped before starting")
                return

            if decision.task_type == TaskType.SINGLE_SITE:
                await self._run_single(job_id, decision, control)
            elif decision.task_type == TaskType.MULTISITE_SWEEP:
                await self._run_sweep(job_id, decision, control, client)
            elif decision.task_type == TaskType.ORDERED_WORKFLOW:
                await self._run_workflow(job_id, decision, control)
            elif decision.task_type == TaskType.RESEARCH:
                await self._run_research(job_id, decision, control, client)
        except Exception as exc:  # last-resort: never leave a job stuck "running" forever
            self.store.update(job_id, status="failed", error=str(exc), activity="Unexpected error")
        finally:
            self._control.pop(job_id, None)
            self._tasks.pop(job_id, None)

    async def _route_with_clarification(
        self, job_id: str, original_prompt: str, client: InferenceClient, control: _JobControl,
    ) -> Optional[RouterDecision]:
        """Routes the prompt, looping on NeedsInput (Section 14): rather than failing with
        "no targets found", persists a `waiting_for_input` state describing exactly what's
        missing, waits for the user's answer via `clarify()`, and re-routes with that answer
        appended (router.policy.route_with_answer) — bounded by MAX_CLARIFICATION_ROUNDS so
        this can never become the open-ended planning loop Section 12 forbids. Returns None
        if the job was stopped mid-wait or the round budget was exhausted (both already
        persist their own terminal status before returning)."""
        result = await route(original_prompt, client, self.config)
        rounds = 0
        while isinstance(result, NeedsInput):
            rounds += 1
            if rounds > MAX_CLARIFICATION_ROUNDS:
                self.store.update(
                    job_id, status="failed",
                    error="could not resolve the requested resource after repeated clarification",
                    activity="Needs more specific input than provided",
                )
                return None
            future: asyncio.Future = asyncio.get_running_loop().create_future()
            control.clarification_future = future
            self.store.update(
                job_id, status="waiting_for_input", activity="Waiting for input...",
                pending_clarification={"question": result.question},
            )
            answer = await self._wait_clarification_or_stop(control, future)
            control.clarification_future = None
            if control.stop_event.is_set() or answer is None:
                self.store.update(job_id, status="stopped", activity="Stopped while waiting for input",
                                   pending_clarification=None)
                return None
            self.store.update(job_id, status="running", pending_clarification=None,
                               activity="Got it, continuing...")
            result = await route_with_answer(original_prompt, answer, client, self.config)
        return result

    async def _wait_clarification_or_stop(self, control: _JobControl, future: asyncio.Future) -> Optional[str]:
        stop_wait = asyncio.ensure_future(control.stop_event.wait())
        answer_wait = asyncio.ensure_future(future)
        try:
            done, _pending = await asyncio.wait({stop_wait, answer_wait}, return_when=asyncio.FIRST_COMPLETED)
            if answer_wait in done:
                return answer_wait.result()
            return None
        finally:
            for t in (stop_wait, answer_wait):
                if not t.done():
                    t.cancel()

    # ---- single-site -----------------------------------------------------

    async def _run_single(self, job_id: str, decision: RouterDecision, control: _JobControl) -> None:
        # decision.targets holds the resolved explicit URL for a single-site task with a known
        # target (empty for a current-page task — see router/policy.py's _translate_single).
        # Passing it through as explicit_target_url is what stops a fresh task like "Open
        # https://example.com" from silently attaching to and answering from an unrelated
        # stale tab a previous job left open in cdp_attach mode (PlaywrightBackend picks a
        # matching existing tab, or a fresh blank one — never the "most recently active" tab).
        loop = AgentLoop.create_new(
            self.config, decision.objective, [],
            explicit_target_url=decision.targets[0] if decision.targets else None,
            approval_callback=self._make_approval_callback(job_id, control),
        )
        self.store.update(job_id, task_id=loop.task_id, activity="Starting browser...")
        await loop.start_browser()
        try:
            state = loop.state_store.load(loop.task_id)
            state = await loop._reconcile_pending_intent(state)
            for _ in range(200):
                if control.stop_event.is_set():
                    self.store.update(job_id, status="stopped", activity="Stopped")
                    return
                state = loop.state_store.load(loop.task_id)
                if state.status != "running":
                    break

                # Peek at the page *before* calling loop.step() rather than reacting to a
                # recorded TASK_BLOCKED event: replay_task (memory/replay.py) treats "blocked"
                # as terminal and has no concept of "manually unblocked by the user logging
                # in" — any TaskState.load() after one more event gets appended (step() itself
                # does several) re-derives status="blocked" from that event on the next replay,
                # silently discarding an in-place status="running" override. Checking here,
                # before the block would ever be recorded, sidesteps the event-sourcing
                # invariant entirely instead of fighting it.
                observation = await loop.browser.observe()
                if looks_like_login_page(observation):
                    self.store.update(job_id, status="waiting_for_login",
                                       activity=f"Login required at {observation.url}")
                    await self._wait_login_or_stop(control)
                    if control.stop_event.is_set():
                        self.store.update(job_id, status="stopped", activity="Stopped during login wait")
                        return
                    control.login_event.clear()
                    self.store.update(job_id, status="running", activity="Resuming after manual login...")
                    continue  # re-check next loop iteration; no TaskState was ever touched

                state = await loop.step(state)
                self.store.update(job_id, activity=self._activity_from_state(state))
        finally:
            await loop.aclose()

        final_state = _reload_state(self.config, loop.task_id)
        self._finish_single(job_id, final_state)

    def _finish_single(self, job_id: str, state) -> None:
        if state is None:
            self.store.update(job_id, status="failed", error="task state not found", activity="Failed")
            return
        if state.status == "completed":
            result_text = _last_finish_result(self.config, state.task_id)
            self.store.update(job_id, status="completed", activity="Completed",
                               final_result={"summary": result_text})
        elif state.status == "blocked":
            self.store.update(job_id, status="failed", error=state.blocked_reason, activity="Blocked",
                               final_result={"blocked_reason": state.blocked_reason})
        else:
            self.store.update(job_id, status="failed", error="step budget exhausted", activity="Did not finish")

    def _activity_from_state(self, state) -> str:
        last = state.recent_actions[-1] if state.recent_actions else None
        if last:
            return f"Step {state.current_step}: {last.get('action')} — {state.current_url or ''}"
        return f"Step {state.current_step}: observing {state.current_url or ''}"

    async def _wait_login_or_stop(self, control: _JobControl) -> None:
        stop_wait = asyncio.create_task(control.stop_event.wait())
        login_wait = asyncio.create_task(control.login_event.wait())
        try:
            await asyncio.wait({stop_wait, login_wait}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in (stop_wait, login_wait):
                if not t.done():
                    t.cancel()

    def _make_approval_callback(self, job_id: str, control: _JobControl):
        async def _approve(decision: ModelDecision, element: Optional[ElementRef]) -> bool:
            future: asyncio.Future = asyncio.get_running_loop().create_future()
            control.approval_future = future
            self.store.update(
                job_id, status="waiting_for_approval", activity="Waiting for approval...",
                pending_approval={
                    "action": decision.action.value,
                    "target": element.name if element else None,
                    "reason": decision.reason,
                },
            )
            approved = await future
            control.approval_future = None
            if control.stop_event.is_set():
                # A denial caused by Stop (not a real user Deny) — Section 5: no consequential
                # action executes either way, but the job's own status must land on "stopped"
                # rather than the misleading "running" a plain Deny would report, since nothing
                # is continuing.
                self.store.update(job_id, status="stopped", activity="Stopped while waiting for approval",
                                   pending_approval=None)
            else:
                self.store.update(job_id, status="running", pending_approval=None,
                                   activity="Approved, continuing..." if approved else "Denied, continuing...")
            return approved
        return _approve

    # ---- multisite sweep (batch) ------------------------------------------

    async def _run_sweep(self, job_id: str, decision: RouterDecision, control: _JobControl,
                          client: Optional[InferenceClient] = None) -> None:
        if not decision.targets:
            self.store.update(job_id, status="failed", error="no targets found in the task text",
                               activity="Failed")
            return
        batch_dir = self.runtime_dir / "batches" / job_id
        store = BatchStore.for_batch_dir(batch_dir)
        try:
            contract = _assignment_contract() if decision.result_contract == "assignment" else \
                (_research_contract() if decision.result_contract == "research" else ResultContract())
            policy = to_batch_policy(decision)
            tab_resources = {
                tr.url: {"tab_id": tr.tab_id, "title": tr.title}
                for tr in decision.target_resources
                if tr.kind == TargetResourceKind.OPEN_TAB
            }
            store.create_batch(decision.objective, decision.targets, contract, policy, batch_id=job_id,
                                target_resources=tab_resources)
            self.store.update(job_id, batch_id=job_id, activity=f"Checking 0 / {len(decision.targets)}")

            def _on_item(item: dict[str, Any]) -> None:
                progress = store.progress(job_id)
                self.store.update(
                    job_id,
                    activity=f"Checked {progress['completed']} / {progress['item_count']} "
                             f"(failed {progress['failed']}, blocked {progress['blocked']}) — last: {item['target']}",
                )

            orchestrator = BatchOrchestrator(self.config, store, job_id, policy, contract,
                                              item_completed_callback=_on_item)
            final = await self._run_cancelable(orchestrator.run(), control)
            if final is None:
                self.store.update(job_id, status="stopped", activity="Stopped")
                return
            self.store.update(job_id, status="completed", activity="Completed", final_result=final)
        finally:
            store.close()

        if decision.mixed_intent_followup and not control.stop_event.is_set():
            await self._maybe_replan(job_id, decision, final, control, client)

    async def _maybe_replan(self, job_id: str, decision: RouterDecision, final: dict[str, Any],
                             control: _JobControl, client: Optional[InferenceClient]) -> None:
        """One bounded replan round (Section 12-13): only reached when the originating plan
        was intent=mixed (e.g. "find the nearest deadline and open it"). Never loops — either
        this single schema-constrained call finds a deterministic follow-up worth running, or
        the sweep's own findings remain the job's final result."""
        if client is None:
            return
        findings = final.get("findings") or []
        if not findings:
            return
        self.store.update(job_id, activity="Checking whether a follow-up action is needed...")
        try:
            replan = await decide_replan(client, decision.objective, findings)
        except ReplanOutputError:
            return  # best-effort: keep the sweep's own completed result untouched
        if replan.decision != ReplanDecisionKind.REVISE or replan.finding_ref is None:
            return
        target_url = finding_source_url(findings, replan.finding_ref)
        if not target_url:
            return
        followup = RouterDecision(
            task_type=TaskType.SINGLE_SITE,
            objective=replan.new_objective or decision.objective,
            targets=[target_url],
            requires_discovery=False,
            preferred_policy=decision.preferred_policy,
            result_contract="generic",
        )
        self.store.update(job_id, activity=f"Following up: {followup.objective}")
        await self._run_single(job_id, followup, control)

    async def _run_cancelable(self, awaitable, control: _JobControl):
        """Cooperative stop: BatchOrchestrator/WorkflowOrchestrator only check policy budgets
        internally, so a hard stop request cancels the underlying task; state already
        persisted up to the last completed item/step is untouched (both stores commit after
        every item), matching Section 22's "no corrupted state" requirement."""
        task = asyncio.ensure_future(awaitable)
        stop_wait = asyncio.ensure_future(control.stop_event.wait())
        done, _pending = await asyncio.wait({task, stop_wait}, return_when=asyncio.FIRST_COMPLETED)
        if task in done:
            stop_wait.cancel()
            return task.result()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return None

    # ---- ordered workflow --------------------------------------------------

    async def _run_workflow(self, job_id: str, decision: RouterDecision, control: _JobControl) -> None:
        if not decision.workflow_steps:
            self.store.update(job_id, status="failed", error="no ordered steps found in the task text",
                               activity="Failed")
            return
        workflow_dir = self.runtime_dir / "workflows" / job_id
        store = WorkflowStore.for_workflow_dir(workflow_dir)
        try:
            policy = WorkflowPolicy(
                read_only=decision.preferred_policy.value == "read_only",
            )
            steps = [s.model_dump() for s in decision.workflow_steps]
            store.create_workflow(decision.objective, steps, policy, workflow_id=job_id)
            self.store.update(job_id, workflow_id=job_id, activity=f"Starting step 1 / {len(steps)}")

            def _on_step(step: dict[str, Any]) -> None:
                self.store.update(
                    job_id,
                    activity=f"Step {step['ordinal']} / {len(steps)} ({step['target']}): {step['status']}",
                )

            orchestrator = WorkflowOrchestrator(
                self.config, store, job_id, policy,
                step_completed_callback=_on_step,
                approval_callback=self._make_approval_callback(job_id, control),
            )
            final = await self._run_cancelable(orchestrator.run(), control)
            if final is None:
                self.store.update(job_id, status="stopped", activity="Stopped")
                return
            status = "completed" if final["status"] == "completed" else "failed"
            self.store.update(job_id, status=status, activity=final["status"],
                               error=final.get("blocked_reason"), final_result=final)
        finally:
            store.close()

    # ---- research (Section 37-42) ------------------------------------------

    async def _run_research(self, job_id: str, decision: RouterDecision, control: _JobControl,
                             client: InferenceClient) -> None:
        max_sources = self.research_max_sources
        self.store.update(job_id, activity="Searching for sources...")
        urls = await self._discover_sources(job_id, decision.objective, max_sources, control, client)
        if control.stop_event.is_set():
            self.store.update(job_id, status="stopped", activity="Stopped during source discovery")
            return
        if not urls:
            self.store.update(job_id, status="failed", error="no sources discovered",
                               activity="Failed to discover sources")
            return
        self.store.update(job_id, activity=f"Discovered {len(urls)} sources, researching...")
        sweep_decision = decision.model_copy(update={"targets": urls, "result_contract": "research"})
        await self._run_sweep(job_id, sweep_decision, control)

    async def _discover_sources(self, job_id: str, objective: str, max_sources: int,
                                 control: _JobControl, client: InferenceClient) -> list[str]:
        """Deterministic candidate-link enumeration + Qwen id-selection (research/discovery.py)
        instead of asking the model to transcribe URLs into a finish JSON string — see that
        module's docstring for why. Wrapped in the same cancelable pattern as the batch/
        workflow paths so a stop request during discovery still leaves no dangling browser."""
        profile_dir = self.runtime_dir / "research" / job_id / "discovery_browser_profile"
        urls = await self._run_cancelable(
            discover_sources(self.config, client, objective, profile_dir, max_sources=min(max_sources, 10)),
            control,
        )
        return urls or []


def _reload_state(config: AppConfig, task_id: str):
    from memory.event_store import EventStore
    from memory.task_state import TaskStateStore

    db_path = Path(config.storage.tasks_dir) / task_id / "task.db"
    if not db_path.exists():
        return None
    es = EventStore(db_path)
    try:
        return TaskStateStore(es).load(task_id)
    finally:
        es.close()


def _last_finish_result(config: AppConfig, task_id: str) -> str:
    from memory.event_store import EventStore, EventType

    db_path = Path(config.storage.tasks_dir) / task_id / "task.db"
    if not db_path.exists():
        return ""
    es = EventStore(db_path)
    try:
        events = es.all_events(task_id)
        completed = next((e for e in reversed(events) if e.type == EventType.TASK_COMPLETED), None)
        return completed.payload.get("result", "") if completed else ""
    finally:
        es.close()
