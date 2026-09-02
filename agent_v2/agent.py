"""The V2 loop.

    observe -> retrieve relevant memory -> build a small context -> ask Qwen for one action
    -> validate it -> (approve / hand to the human if needed) -> execute -> re-observe
    -> verify deterministically -> update compact state -> repeat

That is the whole architecture. There is no planner agent, critic agent, router agent or
recovery agent: the plan is `pending`/`completed` inside `TaskState`, recovery is "tell the
model what just failed and let it choose differently", and verification is arithmetic on two
observations. Everything site-specific the agent knows, it learned into `MemoryStore` at
runtime — nothing in this file branches on the identity of a website.
"""
from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from agent.loop_detector import detect_navigation_loop
from agent.schemas import ActionType, RiskLevel, classify_risk
from browser.page_model import PageObservation
from browser.playwright_backend import PlaywrightBackend
from agent_v2.actions import (
    Decision,
    DecisionError,
    RawDecision,
    V2Action,
    decision_json_schema,
    finish_json_schema,
    finish_with_claims_schema,
    plan_json_schema,
    validate_decision,
)
from agent_v2.browser_ops import ActionOutcome, BrowserSession, Verification
from agent_v2.compute import run_compute
from agent_v2.grounding import (
    GroundingReport,
    check_answer,
    classify_outcome,
    source_coverage,
)
from agent_v2.ledger import (
    EvidenceLedger,
    EvidenceRecord,
    ObservationRef,
    all_figure_keys,
    quoted_spans,
    significant_figures,
)
from agent_v2.memory import MemoryStore, domain_of
from agent_v2.prompts import (
    MEMORY_EXTRACTION_SCHEMA,
    ContextBudget,
    build_context,
    build_memory_extraction_prompt,
    render_page,
)
from agent_v2.state import ActionRecord, TaskState, TaskStatus

#: Approval hook for CONSEQUENTIAL actions (ASK mode). Returns True to proceed.
ApprovalFn = Callable[[Decision, PageObservation], Awaitable[bool]]
#: Human-takeover hook. Returns True once the human says they are done and the run resumes.
TakeoverFn = Callable[[str, PageObservation], Awaitable[bool]]


@dataclass
class LoopLimits:
    max_steps: int = 40
    max_repeats: int = 3            # same semantic action back-to-back
    max_no_change: int = 4          # consecutive actions that changed nothing
    max_invalid_per_step: int = 2   # model corrections before the step is abandoned
    max_consecutive_failures: int = 5


class BrowserAgentV2:
    def __init__(
        self,
        *,
        session: BrowserSession,
        client: Any,
        memory: Optional[MemoryStore] = None,
        task_dir: Optional[Path] = None,
        budget: Optional[ContextBudget] = None,
        limits: Optional[LoopLimits] = None,
        max_output_tokens: int = 400,
        approval: Optional[ApprovalFn] = None,
        takeover: Optional[TakeoverFn] = None,
        on_step: Optional[Callable[[dict], None]] = None,
        memory_top_k: int = 6,
        evidence_top_k: int = 6,
    ):
        self.session = session
        self.client = client
        self.memory = memory
        self.task_dir = task_dir
        self.budget = budget or ContextBudget()
        self.limits = limits or LoopLimits()
        self.max_output_tokens = max_output_tokens
        self.approval = approval
        self.takeover = takeover
        self.on_step = on_step
        self.memory_top_k = memory_top_k
        self.evidence_top_k = evidence_top_k
        self.schema = decision_json_schema()
        self.plan_schema = plan_json_schema()
        self.finish_schema = finish_json_schema()
        self._domains_seen: list[str] = []
        self._procedure_id: Optional[int] = None
        self._pushed_back = False
        self._challenges = 0
        self._takeovers_done = 0
        self._refused_repeat_takeover = False
        self._claims_reasked = False
        #: What actually happened, as opposed to what the model says happened. Created per
        #: task; never shared between tasks (V2 hardening §2/§3/§17).
        self.ledger: EvidenceLedger = EvidenceLedger("uninitialised")
        #: The URL the last navigation asked for, so a redirect can be filed as an alias of
        #: the page that actually loaded rather than as a page that was never seen.
        self._requested_url = ""

    # ---- entry points ----------------------------------------------------------------

    async def run(self, goal: str, *, task_id: Optional[str] = None,
                  max_steps: Optional[int] = None) -> TaskState:
        state = TaskState(task_id=task_id or f"v2-{uuid.uuid4().hex[:10]}", goal=goal.strip())
        self.ledger = EvidenceLedger(state.task_id)
        self.ledger.goal = state.goal
        self.ledger.requested_sources = _sources_named_in(state.goal)
        return await self._drive(state, max_steps=max_steps)

    async def resume(self, state: TaskState, *, max_steps: Optional[int] = None) -> TaskState:
        """Resume a paused/crashed task. Nothing but the compact state is needed — no raw
        prompt history is replayed (V2 spec §32)."""
        state.status = TaskStatus.RUNNING.value
        state.pause_message = ""
        if self.ledger.task_id != state.task_id:
            path = self._ledger_path()
            self.ledger = (EvidenceLedger.load(path, state.task_id) if path and path.exists()
                           else EvidenceLedger(state.task_id))
            self.ledger.goal = state.goal
            if not self.ledger.requested_sources:
                self.ledger.requested_sources = _sources_named_in(state.goal)
        return await self._drive(state, max_steps=max_steps)

    # ---- the loop --------------------------------------------------------------------

    async def _drive(self, state: TaskState, max_steps: Optional[int]) -> TaskState:
        limit = max_steps or self.limits.max_steps
        started = time.time()
        pending_hint = ""
        consecutive_failures = 0
        no_change_streak = 0
        steps_this_run = 0
        block_finish = False
        block_need_user = False
        require_claims = False

        await self.session.adopt_existing_tabs()

        while steps_this_run < limit:
            steps_this_run += 1
            state.step += 1

            observe_started = time.monotonic()
            obs = await self.session.observe()
            state.metrics.observe_ms += (time.monotonic() - observe_started) * 1000
            tabs = await self.session.sync_tabs()
            state.note_page(obs.url, obs.title, self.session.current_tab_id())
            self._note_domain(state, obs.url)

            # The one place a resource becomes "observed". Everything downstream — what may
            # be cited, what may be claimed, which sources the answer may name — is derived
            # from this call and never from anything the model asserts (V2 hardening §3).
            ref = self.ledger.note_observation(obs, state.step, requested_url=self._requested_url)
            self._requested_url = ""
            state.metrics.resources_observed = len(self.ledger.resources)

            memory_render = self._retrieve(state, obs)
            hint = _combine(pending_hint, self._page_hints(obs), self._takeover_hint(),
                            _first_turn_hint(state), _budget_hint(limit - steps_this_run))
            pending_hint = ""

            decision = await self._decide(state, obs, memory_render,
                                          BrowserSession.render_tabs(tabs), hint,
                                          block_finish=block_finish,
                                          block_need_user=block_need_user,
                                          require_plan=(state.step == 1 and not state.pending),
                                          require_claims=require_claims)
            block_finish = block_need_user = False
            if decision is None:
                state.record_failure("could not produce a usable action for this page")
                consecutive_failures += 1
                if consecutive_failures >= self.limits.max_consecutive_failures:
                    return await self._fail(state, "the model could not produce a valid action repeatedly", started)
                pending_hint = "Your last replies were not usable actions. Reply with ONE simple JSON action."
                continue

            if decision.action is V2Action.FINISH:
                self._absorb_updates(state, decision, ref)
                if self._premature_finish(state, limit - steps_this_run):
                    # Asked once, never twice: a model that gives up with unfinished plan
                    # items and most of its budget left is usually one nudge away from doing
                    # the rest, but nagging repeatedly would just be a different way to loop.
                    self._pushed_back = True
                    block_finish = True
                    pending_hint = (
                        f"You tried to finish, but STILL TO DO still has {len(state.pending)} item(s) "
                        f"and you have {limit - steps_this_run} steps left. This turn you MUST take a "
                        "browser action instead: open_url or open_tab the page that has the missing "
                        "piece, or click through to it."
                    )
                    continue

                report = check_answer(answer=decision.answer or "", claims=decision.claims,
                                      ledger=self.ledger, goal=state.goal,
                                      meta_figures=self._meta_figures(state))
                self._log(state, {
                    "event": "grounding", "step": state.step,
                    "claims": [{"text": c.text[:120], "evidence_ids": c.evidence_ids,
                                "kind": c.kind} for c in decision.claims],
                    "supported_claims": report.supported_claims,
                    "problems": report.problems[:8],
                })
                if report.problems and self._may_challenge(limit - steps_this_run):
                    # Challenged, not blocked. A small model that guessed will usually go and
                    # look when told exactly which part of its answer has no source; one that
                    # is right about something the checks cannot see gets to say so again, and
                    # then the disclosure below carries the disagreement to the user.
                    #
                    # The shortfall also goes back into the plan. Observed on a three-source
                    # research task: the model marked every plan item complete, invented the
                    # third version, and once challenged had nothing left in STILL TO DO to
                    # remind it what was actually missing on the following turns. An
                    # ungrounded claim *is* unfinished work, so it is filed as such.
                    self._challenges += 1
                    state.metrics.grounding_challenges += 1
                    block_finish = True
                    require_claims = True
                    # …and the "you still have plan items left" nudge is spent here rather
                    # than fired separately for the same shortfall. Both say "you are not
                    # done"; saying it twice costs a step and teaches nothing.
                    self._pushed_back = True
                    for item in self._outstanding(report):
                        state.add_pending(item)
                    pending_hint = self._grounding_hint(report)
                    continue

                state.answer = decision.answer or ""
                if report.problems:
                    # It stood by the answer. The honest outcome is to hand it over with the
                    # unsupported parts named, never to present them as though they had been
                    # read off a page (V2 hardening §29).
                    state.unsupported_claims = report.problems[:5]
                    state.answer += "\n\n" + report.label()
                    state.record_failure("answer contains unsupported claims: "
                                         + "; ".join(report.problems[:3]))
                state.status = TaskStatus.DONE.value
                state.outcome = classify_outcome(done=True, report=report,
                                                 labelled=bool(report.label()) or report.clean)
                self._record(state, decision, ActionOutcome(True, "finished"),
                             Verification(True), obs)
                return await self._finalize(state, started)

            if decision.action is V2Action.NEED_USER and self._takeovers_done and                     not self._refused_repeat_takeover:
                # The human already handed control back once. Interrupting them again for the
                # same thing is a bug, not a request — so it is refused once, in the grammar,
                # rather than merely discouraged in prose. If the model still needs a human on
                # the next turn (a genuine second factor, say), it gets one.
                self._refused_repeat_takeover = True
                block_need_user = True
                pending_hint = _combine(pending_hint, self._takeover_hint())
                continue

            if decision.action is V2Action.NEED_USER:
                # Findings recorded on the way to asking for help are still findings. Dropping
                # them meant a model that read the page and *then* hit a login lost what it
                # had read, and had to go back for it after the human was done.
                self._absorb_updates(state, decision, ref)
                resumed = await self._pause_for_user(state, decision.message or "Your input is needed.", obs)
                if not resumed:
                    state.metrics.total_s = time.time() - started
                    self._save(state)
                    return state
                pending_hint = "The human has finished. Re-read the page before deciding."
                continue

            if decision.action is V2Action.COMPUTE:
                # No browser involved: the model named an operation, BrowserAgent performs it,
                # and the result becomes evidence with lineage back to the prices it used
                # (V2 hardening §10/§12).
                # Facts first, then the computation: the operands' evidence must exist before
                # the record that cites it, and the ids the model sees next turn then read in
                # the order it collected them.
                self._absorb_updates(state, decision, ref)
                outcome, verification = self._compute(state, decision, ref)
                self._record(state, decision, outcome, verification, obs)
                if verification.passed:
                    consecutive_failures = 0
                else:
                    consecutive_failures += 1
                    state.record_failure(f"compute failed — {verification.note}")
                    pending_hint = (f"Your compute did not run: {verification.note}. "
                                    "Fix the operands or choose a different operation.")
                    if consecutive_failures >= self.limits.max_consecutive_failures:
                        return await self._fail(state, "too many consecutive failed actions", started)
                self._save(state)
                continue

            allowed, note = await self._gate(decision, obs)
            if not allowed:
                state.record_failure(f"not allowed: {note}")
                pending_hint = f"That action was not permitted: {note}. Choose a different action."
                continue

            if decision.action in (V2Action.OPEN_URL, V2Action.OPEN_TAB):
                self._requested_url = decision.url or ""
            outcome = await self.session.execute(decision, obs)
            state.metrics.browser_ms += outcome.browser_ms
            state.metrics.actions_executed += 1

            observe_started = time.monotonic()
            after = await self.session.observe()
            state.metrics.observe_ms += (time.monotonic() - observe_started) * 1000
            verification = self.session.verify(decision, outcome, obs, after)

            after_ref = self.ledger.note_observation(after, state.step,
                                                     requested_url=self._requested_url)
            self._requested_url = ""
            state.metrics.resources_observed = len(self.ledger.resources)
            self._absorb_updates(state, decision, ref, after_ref)
            if outcome.data.get("extracted"):
                # Text the browser itself returned. Its provenance is certain by construction,
                # so it is filed against the page it was read from without a containment check.
                text = f"From {domain_of(after.url)}: {outcome.data['extracted'][:280]}"
                record, _why = self.ledger.record_observed(text, after_ref, state.step, trusted=True)
                if record is not None:
                    state.metrics.evidence_records += 1
                state.add_fact(text, self._spill_path(state))
            self._record(state, decision, outcome, verification, after)

            if verification.passed:
                consecutive_failures = 0
            else:
                consecutive_failures += 1
                state.metrics.verification_failures += 1
                state.record_failure(f"{decision.action.value} \"{decision.target_name or ''}\" — {verification.note}")
                pending_hint = (
                    f"Your last action did not work: {verification.note}. "
                    "Do something different — do not repeat it."
                )
                if consecutive_failures >= self.limits.max_consecutive_failures:
                    return await self._fail(state, "too many consecutive failed actions", started)

            no_change_streak = 0 if verification.changed else no_change_streak + 1
            stuck = self._stuck_hint(state, decision, no_change_streak)
            if stuck:
                state.metrics.loop_breaks += 1
                pending_hint = _combine(pending_hint, stuck)
                no_change_streak = 0

            self._save(state)

        return await self._fail(state, f"reached the {limit}-step limit without finishing", started)

    # ---- one model decision ----------------------------------------------------------

    async def _decide(self, state: TaskState, obs: PageObservation, memory_render: str,
                      tabs_render: str, hint: str,
                      block_finish: bool = False, block_need_user: bool = False,
                      require_plan: bool = False,
                      require_claims: bool = False) -> Optional[Decision]:
        """Ask, validate, and give the model up to `max_invalid_per_step` corrections. The
        correction text is the validation error itself, which is why validation errors are
        written as instructions rather than as diagnostics."""
        page_render = render_page(obs, token_budget=self.budget.limit("page"))
        evidence_render = self._evidence_render(state)
        blocked = {V2Action.FINISH} if block_finish else set()
        if block_need_user:
            blocked.add(V2Action.NEED_USER)
        if blocked:
            schema = decision_json_schema(blocked)
        elif require_plan:
            schema = self.plan_schema
        else:
            schema = self.schema
        for attempt in range(self.limits.max_invalid_per_step + 1):
            context = build_context(
                goal=state.goal,
                state=state,
                page_render=page_render,
                memory_render=memory_render,
                tabs_render=tabs_render,
                hint=hint,
                evidence_render=evidence_render,
                budget=self.budget,
            )
            state.metrics.prompt_chars_max = max(state.metrics.prompt_chars_max, len(context.prompt))

            started = time.monotonic()
            try:
                result = await self.client.complete(
                    context.prompt, max_tokens=self.max_output_tokens, json_schema=schema
                )
            except Exception as exc:
                state.record_failure(f"model call failed: {type(exc).__name__}")
                return None
            elapsed = (time.monotonic() - started) * 1000
            state.metrics.llm_calls += 1
            state.metrics.llm_ms += elapsed
            state.metrics.prompt_tokens += result.prompt_tokens or 0
            state.metrics.completion_tokens += result.predicted_tokens or 0

            try:
                raw = RawDecision.model_validate(_parse_json(result.text))
                decision = validate_decision(raw, obs)
            except DecisionError as exc:
                state.metrics.invalid_decisions += 1
                self._log(state, {"event": "rejected", "step": state.step, "kind": exc.kind,
                                  "detail": exc.message[:160], "url": obs.url})
                if exc.kind == "missing_answer":
                    # It wants to stop but left `answer` out. Re-ask once under a schema that
                    # cannot omit it; if even that fails, end the task with the facts already
                    # collected rather than burning the budget arguing about a field.
                    schema = self.finish_schema
                    if attempt == self.limits.max_invalid_per_step:
                        return Decision(action=V2Action.FINISH, reason=raw.reason or "",
                                        answer=_partial_answer(state, "the model omitted its answer"))
                    hint = _combine(hint, "Your finish had no `answer`. Put the complete result "
                                          "for the user in the `answer` field.")
                    continue
                if exc.kind == "sensitive_field":
                    # The model tried to fill a credential field. That is never retried as a
                    # browser action — it becomes a human takeover (V2 spec §7).
                    return Decision(
                        action=V2Action.NEED_USER,
                        reason="credentials required",
                        message="This page needs a password. Please sign in yourself in the browser, then resume.",
                    )
                hint = _combine(hint, f"Your last reply was rejected: {exc.message}")
                if attempt == self.limits.max_invalid_per_step:
                    return None
                continue
            except Exception as exc:
                state.metrics.invalid_decisions += 1
                self._log(state, {"event": "rejected", "step": state.step, "kind": type(exc).__name__,
                                  "detail": str(exc)[:160], "url": obs.url})
                hint = _combine(hint, f"Your last reply was not valid JSON for one action ({type(exc).__name__}). "
                                      "Reply with a single JSON object.")
                if attempt == self.limits.max_invalid_per_step:
                    return None
                continue

            if (require_claims and decision.action is V2Action.FINISH and not decision.claims
                    and not self._claims_reasked and _needs_citation(decision.answer or "")
                    and attempt < self.limits.max_invalid_per_step):
                # Only for an answer that actually asserts something checkable. An honest
                # "I could not reach the third source" needs no citations, and charging it a
                # model call to say so would penalise exactly the outcome §29 prefers.
                self._claims_reasked = True
                # It has already had one answer rejected for grounding. Asking again in prose
                # for citations is advice; a schema that cannot express a finish without them
                # is the thing that actually produces them.
                schema = finish_with_claims_schema()
                hint = _combine(hint, "Your finish listed no claims. Put every factual "
                                      "statement in your answer into `claims` with the "
                                      "evidence id it came from.")
                continue

            repeat = self._pointless_repeat(state, decision)
            if repeat and attempt < self.limits.max_invalid_per_step:
                # Refuse *before* executing rather than executing and complaining afterwards.
                # A small model asked to "not repeat itself" after the fact will usually
                # repeat itself anyway; denying the action and re-asking in the same step is
                # what actually breaks the stall (V2 spec §21).
                state.metrics.loop_breaks += 1
                hint = _combine(hint, repeat)
                continue

            self._log(state, {
                "event": "decision", "step": state.step, "action": decision.action.value,
                "target": decision.target, "target_name": decision.target_name,
                "reason": decision.reason, "llm_ms": round(elapsed, 1),
                "prompt_tokens": result.prompt_tokens, "blocks": context.block_tokens,
                "url": obs.url, "state_hash": obs.state_hash[:12],
            })
            return decision
        return None

    # ---- policy ----------------------------------------------------------------------

    async def _gate(self, decision: Decision, obs: PageObservation) -> tuple[bool, str]:
        """AUTO / ASK / TAKEOVER (V2 spec §7/§30), kept deliberately small: ordinary browsing
        runs unattended, consequential actions ask, credentials are always the human's."""
        legacy = _LEGACY_ACTION.get(decision.action)
        if legacy is None:
            return True, ""
        risk = classify_risk(legacy, decision.target_name)
        if risk is not RiskLevel.CONSEQUENTIAL:
            return True, ""
        if self.approval is None:
            return True, ""
        approved = await self.approval(decision, obs)
        return (True, "") if approved else (False, "the user declined this action")

    async def _pause_for_user(self, state: TaskState, message: str, obs: PageObservation) -> bool:
        state.metrics.human_interventions += 1
        state.status = TaskStatus.WAITING_FOR_USER.value
        state.pause_message = message
        self._save(state)
        self._log(state, {"event": "need_user", "step": state.step, "message": message, "url": obs.url})
        if self.takeover is None:
            return False
        resumed = await self.takeover(message, obs)
        if resumed:
            state.status = TaskStatus.RUNNING.value
            state.pause_message = ""
            self._takeovers_done += 1
            state.mark_completed("the human signed in / completed a step in the browser")
        return resumed

    def _takeover_hint(self) -> str:
        """Interrupting the user once is necessary; interrupting them again for the same
        thing is a bug. Observed on a real login: after the human signed in, the agent
        wandered back to the form and asked a second and third time."""
        if not self._takeovers_done:
            return ""
        return ("The human has ALREADY signed in for you in this browser. Do not use need_user "
                "again for login. If you are looking at a login form, you have navigated to the "
                "wrong page — go to where the task actually needs to be.")

    # ---- memory ----------------------------------------------------------------------

    def _retrieve(self, state: TaskState, obs: PageObservation) -> str:
        if self.memory is None:
            return ""
        started = time.monotonic()
        subgoal = state.pending[0] if state.pending else ""
        result = self.memory.retrieve(
            state.goal, domain=domain_of(obs.url), subgoal=subgoal,
            top_k=self.memory_top_k, token_budget=self.budget.limit("memory"),
        )
        state.metrics.memory_ms += (time.monotonic() - started) * 1000
        state.metrics.memory_hits += len(result.memories)
        if result.procedure is not None:
            state.metrics.procedure_hits += 1
            self._procedure_id = result.procedure.id
        return result.render()

    async def _finalize(self, state: TaskState, started: float) -> TaskState:
        state.metrics.total_s = time.time() - started
        coverage = source_coverage(self.ledger, state.answer)
        state.source_coverage = coverage.render()
        self._log(state, {"event": "source_coverage", "requested": coverage.requested,
                          "visited": coverage.visited, "with_evidence": coverage.with_evidence,
                          "named_in_answer": coverage.named_in_answer,
                          "requested_but_unvisited": coverage.requested_but_unvisited})
        if self._procedure_id is not None and self.memory is not None:
            self.memory.record_procedure_outcome(
                self._procedure_id, state.status == TaskStatus.DONE.value
            )
        await self._extract_memories(state)
        self._save(state)
        self._log(state, {"event": "finished", "status": state.status,
                          "steps": state.step, "metrics": state.metrics.__dict__})
        return state

    async def _extract_memories(self, state: TaskState) -> None:
        """One extra model call per task — not per step (V2 spec §15)."""
        if self.memory is None or state.metrics.actions_executed == 0:
            return
        prompt = build_memory_extraction_prompt(state, self._domains_seen, state.status)
        try:
            result = await self.client.complete(prompt, max_tokens=500,
                                                json_schema=MEMORY_EXTRACTION_SCHEMA)
            payload = _parse_json(result.text)
        except Exception:
            return
        state.metrics.llm_calls += 1
        saved = 0
        for item in (payload.get("memories") or [])[:5]:
            if not isinstance(item, dict):
                continue
            memory_id = self.memory.save(
                str(item.get("type") or "strategy"),
                str(item.get("text") or ""),
                domain=str(item.get("domain") or ""),
                importance=float(item.get("importance") or 0.5),
                source_task=state.task_id,
                goal=state.goal,
            )
            if memory_id:
                saved += 1
            else:
                self._log(state, {"event": "memory_rejected", "step": state.step,
                                  "reason": self.memory.last_rejection})
        procedure = payload.get("procedure")
        if isinstance(procedure, dict) and state.status == TaskStatus.DONE.value:
            self.memory.save_procedure(
                str(procedure.get("goal_pattern") or state.goal),
                [str(s) for s in (procedure.get("steps") or [])],
                domain=str(procedure.get("domain") or ""),
            )
        self._log(state, {"event": "memories_saved", "count": saved})

    # ---- bookkeeping -----------------------------------------------------------------

    def _record(self, state: TaskState, decision: Decision, outcome: ActionOutcome,
                verification: Verification, obs: PageObservation) -> None:
        record = ActionRecord(
            step=state.step,
            action=decision.action.value,
            target_name=decision.target_name,
            detail=_detail(decision),
            url=obs.url,
            ok=verification.passed,
            note=verification.note if not verification.passed else "",
            signature=decision.signature(),
            state_hash=obs.state_hash[:12],
            changed=verification.changed,
        )
        state.record_action(record)
        self._log(state, {
            "event": "step", "step": state.step, "action": decision.action.value,
            "target_name": decision.target_name, "ok": verification.passed,
            "note": verification.note, "changed": verification.changed,
            "url": obs.url, "state_hash": obs.state_hash[:12],
            "browser_ms": round(outcome.browser_ms, 1),
        })
        if self.on_step is not None:
            self.on_step({"step": state.step, "action": decision.action.value,
                          "target": decision.target_name, "reason": decision.reason,
                          "ok": verification.passed, "note": verification.note, "url": obs.url})

    # ---- evidence --------------------------------------------------------------------

    def _absorb_updates(self, state: TaskState, decision: Decision,
                        *refs: ObservationRef) -> None:
        """Apply the model's state edits, turning the ones that hold up into evidence.

        `add_facts` is the whole evidence channel, and it costs nothing extra: the model was
        already writing down what it found, and the only new thing is that BrowserAgent
        checks each note against the page it was written against before minting a record
        (V2 hardening §2). A note that does not hold up is still kept — it may be a legitimate
        inference — but it is marked, and no record exists for it, so it can never support a
        final claim. This is what closes the hole the old check had: a fabrication filed under
        `add_facts` at step 3 used to become its own evidence by step 9.
        """
        def verify(fact: str) -> bool:
            # Both the page the model was reading when it wrote the note and the page it
            # landed on afterwards. A model that writes "Widget A costs $159" on the step that
            # opens Widget A's page is not fabricating — it is a step ahead, and the page it
            # arrives at does show the figure. Either way the record is bound to a real
            # observation, so provenance is unchanged.
            why = "no observation to check against"
            for candidate in refs:
                record, why = self.ledger.record_observed(fact, candidate, state.step)
                if record is not None:
                    state.metrics.evidence_records += 1
                    self._log(state, {"event": "evidence", "step": state.step,
                                      "evidence_id": record.evidence_id,
                                      "source": record.source_url})
                    return True
            state.metrics.evidence_rejected += 1
            self._log(state, {"event": "evidence_rejected", "step": state.step,
                              "reason": why, "fact_chars": len(fact)})
            return False

        state.apply_updates(decision.state_updates.add_facts, decision.state_updates.completed,
                            decision.state_updates.pending, self._spill_path(state),
                            verify=verify)

    def _compute(self, state: TaskState, decision: Decision,
                 ref: ObservationRef) -> tuple[ActionOutcome, Verification]:
        """Run one deterministic operation and file its result as derived evidence."""
        result = run_compute(decision.operation or "", decision.operands, decision.labels)
        state.metrics.computations += 1
        if not result.ok:
            state.metrics.compute_errors += 1
            self._log(state, {"event": "compute", "step": state.step, "ok": False,
                              "operation": decision.operation, "error": result.error})
            return ActionOutcome(False, result.error), Verification(False, result.error)

        sources: list[EvidenceRecord] = []
        for raw in decision.evidence_ids:
            citation = self.ledger.resolve(raw)
            if citation.ok:
                sources.append(citation.record)
        record = self.ledger.record_derived(text=result.text, operation=result.operation,
                                            sources=sources, step=state.step,
                                            operands=result.operands)
        state.metrics.evidence_records += 1
        state.add_fact(f"{result.text} [{record.evidence_id}]", self._spill_path(state))
        self._log(state, {"event": "compute", "step": state.step, "ok": True,
                          "operation": result.operation, "operands": result.operands,
                          "result": result.text, "evidence_id": record.evidence_id,
                          "derived_from": record.derived_from})
        return ActionOutcome(True, result.text), Verification(True, "", changed=False)

    def _evidence_render(self, state: TaskState) -> str:
        """The bounded evidence block. A selection relevant to the goal and the active
        subgoal — never the whole ledger, which is what keeps the prompt flat over a long
        run (V2 hardening §18/§19)."""
        query = " ".join([state.goal, state.pending[0] if state.pending else ""])
        records = self.ledger.select(query=query, limit=self.evidence_top_k,
                                     token_budget=self.budget.limit("evidence"))
        rendered = EvidenceLedger.render(records)
        sources = self.ledger.render_sources()
        if sources:
            rendered += ("\n" if rendered else "") + "PAGES YOU HAVE ACTUALLY OPENED:\n" + sources
        return rendered

    def _meta_figures(self, state: TaskState) -> set[str]:
        """Numbers a statement about the run itself may legitimately use, because
        BrowserAgent knows them exactly."""
        counts = [len(self.ledger.resources), state.step, state.metrics.actions_executed,
                  len(state.facts) + state.spilled_facts, len(state.completed),
                  len(state.pending), len(self.ledger.records)]
        return all_figure_keys(" ".join(str(c) for c in counts)) | {str(c) for c in counts}

    def _may_challenge(self, steps_left: int) -> bool:
        """Challenging an ungrounded answer is worth a step only while there is budget to act
        on the challenge. Two is the floor; a third is allowed when the run still has room,
        because on real multi-source tasks the second challenge is often the one that sends
        the model to the page it skipped. Beyond that it is arguing, not working."""
        if self._challenges < 2:
            return True
        return self._challenges < 3 and steps_left > 4

    @staticmethod
    def _outstanding(report: GroundingReport) -> list[str]:
        """The grounding shortfall, written as plan items the model can work through."""
        items = [f"actually open {source} and read it there"
                 for source in report.unvisited_sources[:2]]
        items += [f"find {figure} on a real page, or drop it from the answer"
                  for figure in report.unsupported_figures[:2]]
        return items[:3]

    @staticmethod
    def _grounding_hint(report: GroundingReport) -> str:
        """The challenge shown to the model. Names the exact defect rather than restating the
        rule, because a small model corrects a specific fault and ignores a general one."""
        parts: list[str] = []
        if report.unsupported_figures:
            parts.append("no page you opened in this task shows "
                         + ", ".join(report.unsupported_figures[:4]))
        if report.unvisited_sources:
            parts.append("you have not opened " + ", ".join(report.unvisited_sources[:3])
                         + ", so you cannot report what it says")
        if report.unsupported_quotes:
            parts.append("these quotations are on no page you opened: "
                         + ", ".join(report.unsupported_quotes[:2]))
        if report.invalid_citations:
            parts.append("these evidence ids do not exist in this task: "
                         + ", ".join(report.invalid_citations[:3]))
        if report.unsupported_claims:
            parts.append("; ".join(report.unsupported_claims[:2]))
        return (
            "Your answer was rejected: " + "; ".join(parts) + ". "
            "This turn you MUST take a browser action — open_url the page that actually has "
            "the missing piece and read it there. If you genuinely cannot get it, finish "
            "instead with only what you did read, say plainly which part is missing, and cite "
            "an evidence id from EVIDENCE for every fact you keep."
        )

    def _ledger_path(self) -> Optional[Path]:
        return (self.task_dir / "evidence.json") if self.task_dir else None

    def _premature_finish(self, state: TaskState, steps_left: int) -> bool:
        """Finishing with plan items outstanding and most of the budget unspent."""
        return (not self._pushed_back and bool(state.pending)
                and steps_left > max(3, self.limits.max_steps // 4))

    def _pointless_repeat(self, state: TaskState, decision: Decision) -> str:
        """"You just did exactly this and the page did not change." Returns the correction to
        show the model, or "" if the action is fine. `finish`/`need_user` are exempt: ending
        the task is always allowed."""
        if decision.action in (V2Action.FINISH, V2Action.NEED_USER):
            return ""
        if not state.recent_actions:
            return ""
        last = state.recent_actions[-1]
        if last.signature != decision.signature() or last.changed:
            return ""
        return (f"You already did '{decision.action.value}"
                f"{' ' + last.target_name if last.target_name else ''}' on the previous step and "
                "the page did not change, so doing it again cannot help. Everything you read is "
                "already in TASK STATE. Either act somewhere else on the page, navigate, or — if "
                "the goal is already answered — finish now.")

    def _stuck_hint(self, state: TaskState, decision: Decision, no_change_streak: int) -> str:
        """Loop prevention (V2 spec §21). All three signals are computed from persisted
        state, so they survive a crash+resume the same way the old loop detectors do."""
        signature = decision.signature()
        repeats = 0
        for record in reversed(state.recent_actions):
            if record.signature == signature:
                repeats += 1
            else:
                break
        if repeats >= self.limits.max_repeats:
            return (f"You have done '{decision.action.value}' on the same thing {repeats} times "
                    "with no progress. That approach is not working — take a different route "
                    "(a different element, a different URL, or finish with what you have).")
        if no_change_streak >= self.limits.max_no_change:
            return ("The page has not changed for several actions. Stop interacting with this "
                    "part of the page: navigate somewhere else, or finish with what you have.")
        if detect_navigation_loop([{"url": r.url} for r in state.recent_actions], 2):
            return ("You are bouncing between the same two pages. Break the cycle: choose a "
                    "different destination or finish.")
        return ""

    def _page_hints(self, obs: PageObservation) -> str:
        """Generic, structural observations about the page — never about which site it is."""
        hints: list[str] = []
        if not obs.elements:
            # A plain-text document, a raw RFC, a rendered PDF. Observed on rfc-editor.org:
            # the model tried to click element 1 sixteen times in a row on a page that has no
            # elements at all, and burned the whole step budget on it. Saying so once is
            # cheaper than sixteen stale-target rejections.
            hints.append("This page has NO elements at all — it is plain text. Do not click, "
                         "type, or extract a target here. Read what is in VISIBLE TEXT, "
                         "scroll for more, or open_url somewhere else.")
        if any(el.sensitive for el in obs.elements):
            hints.append("This page has a password field. You cannot fill it. If signing in is "
                         "required, use need_user.")
        if obs.modal_present:
            hints.append("A dialog is open and is probably blocking the page. Deal with it first.")
        if obs.truncated:
            hints.append("The page listing was truncated — scroll or extract if you need more.")
        return " ".join(hints)

    def _note_domain(self, state: TaskState, url: str) -> None:
        domain = domain_of(url)
        if domain and domain not in self._domains_seen:
            self._domains_seen.append(domain)
        if domain and domain not in state.domains:
            state.domains.append(domain)
            del state.domains[:-12]

    async def _fail(self, state: TaskState, reason: str, started: float) -> TaskState:
        """A failed task still goes through `_finalize`: the lesson from a failure is one of
        the more useful things there is to remember (V2 spec §13), and the user still gets
        whatever was collected before the run gave up."""
        state.status = TaskStatus.FAILED.value
        state.record_failure(reason)
        if not state.answer:
            state.answer = _partial_answer(state, reason)
        # A partial answer is assembled from facts, and an unverified fact carries its marker
        # into it — so the same grounding check applies here as to a model-authored finish.
        report = check_answer(answer=state.answer, claims=[], ledger=self.ledger,
                              goal=state.goal, meta_figures=self._meta_figures(state))
        if report.problems:
            state.unsupported_claims = report.problems[:5]
            state.answer += "\n\n" + report.label()
        state.outcome = classify_outcome(done=False, report=report,
                                         labelled=bool(report.label()) or report.clean)
        return await self._finalize(state, started)

    def _spill_path(self, state: TaskState) -> Optional[Path]:
        return (self.task_dir / "facts.log") if self.task_dir else None

    def _save(self, state: TaskState) -> None:
        if self.task_dir is not None:
            state.save(self.task_dir / "state.json")
            self.ledger.save(self.task_dir / "evidence.json")

    def _log(self, state: TaskState, payload: dict) -> None:
        """Compact JSONL. Never carries page dumps or field values (V2 spec §31)."""
        if self.task_dir is None:
            return
        payload = {"ts": round(time.time(), 3), "task_id": state.task_id, **payload}
        self.task_dir.mkdir(parents=True, exist_ok=True)
        with open(self.task_dir / "steps.jsonl", "a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, default=str) + "\n")


_LEGACY_ACTION = {
    V2Action.CLICK: ActionType.CLICK,
    V2Action.TYPE: ActionType.TYPE,
    V2Action.SELECT: ActionType.SELECT,
    V2Action.OPEN_URL: ActionType.OPEN_URL,
    V2Action.BACK: ActionType.BACK,
    V2Action.SCROLL: ActionType.SCROLL,
    V2Action.EXTRACT: ActionType.EXTRACT,
    V2Action.WAIT: ActionType.WAIT,
}


def build_session(config: Any, *, explicit_target_url: Optional[str] = None) -> PlaywrightBackend:
    """Backend construction is the one place V2 touches lifecycle config. In `cdp_attach`
    mode this attaches to the browser the user already has open and, on close, disconnects
    without touching it."""
    return PlaywrightBackend(
        profile_dir=Path(config.browser.user_data_dir) / "v2_profile",
        headless=config.browser.headless,
        action_timeout_ms=config.browser.action_timeout_ms,
        max_page_chars=config.context.max_page_chars,
        max_visible_text_items=config.context.max_visible_text_items,
        mode=config.browser.mode,
        cdp_endpoint=config.browser.cdp_endpoint,
        explicit_target_url=explicit_target_url,
    )


def _parse_json(text: str) -> dict:
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text.split("\n", 1)[1] if "\n" in text else text
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON object in model output")
    return json.loads(text[start:end + 1])


def _detail(decision: Decision) -> str:
    if decision.action is V2Action.OPEN_URL or decision.action is V2Action.OPEN_TAB:
        return (decision.url or "")[:80]
    if decision.action is V2Action.TYPE:
        return f'"{(decision.text or "")[:40]}"'
    if decision.action is V2Action.SELECT:
        return f'"{(decision.value or "")[:40]}"'
    if decision.action is V2Action.SCROLL:
        return decision.direction
    if decision.action in (V2Action.SWITCH_TAB, V2Action.CLOSE_AGENT_CREATED_TAB):
        return f"tab {decision.tab_id}"
    return ""


def _partial_answer(state: TaskState, reason: str) -> str:
    lines = [f"Did not fully finish ({reason})."]
    if state.facts:
        lines.append("What was found:")
        lines.extend(f"- {f}" for f in state.facts)
    return "\n".join(lines)


#: A URL or bare host written into the goal. These are the sources the *user* asked for; the
#: ledger tracks them purely so the final report can say which of them were actually reached
#: (V2 hardening §8). Naming one here never counts as having visited it.
_GOAL_SOURCE_RE = re.compile(
    r"\b(?:https?://\S+|(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,24}(?:/\S*)?)", re.I)


def _sources_named_in(goal: str) -> list[str]:
    out: list[str] = []
    for match in _GOAL_SOURCE_RE.finditer(goal or ""):
        source = match.group(0).rstrip(".,;:)!?\"'")
        host = source.split("://")[-1].split("/")[0].lower()
        if host.rsplit(".", 1)[-1] in _NOT_A_TLD:
            continue
        if source not in out:
            out.append(source)
    return out[:10]


_NOT_A_TLD = {"js", "py", "ts", "html", "htm", "json", "md", "txt", "css", "pdf", "csv"}


def _needs_citation(answer: str) -> bool:
    """Does this answer assert anything a page could confirm? Figures, quotations and named
    sites do; "I could not find it" does not."""
    from agent_v2.grounding import hosts_mentioned
    return bool(significant_figures(answer) or hosts_mentioned(answer)
                or any(len(q.split()) >= 3 for q in quoted_spans(answer)))


def _first_turn_hint(state: TaskState) -> str:
    """Plan-as-data has to be prompted into existence on turn one — left to itself, a small
    model dives at the first page and never writes a plan, then "finishes" halfway (V2 §19)."""
    if state.step > 1 or state.pending:
        return ""
    return ("This is your first turn, so state_updates.pending is required. Break the goal "
            "into the concrete pieces you must actually collect — one entry per item, site or "
            "question the goal names. If the goal only asks for one thing, one entry is right. "
            "Also: you are already on a page. If it holds any part of the answer, put that in "
            "state_updates.add_facts NOW — once you navigate away it is gone.")


def _budget_hint(steps_left: int) -> str:
    """Running out of steps with an unspoken answer is a worse outcome than a partial one,
    so the model is told when to start wrapping up (V2 spec §21's "graceful failure")."""
    if steps_left <= 1:
        return "This is your LAST step. Reply with finish and the best answer you have."
    if steps_left <= 3:
        return (f"Only {steps_left} steps left. Stop exploring and finish with what you have "
                "unless one more action genuinely completes the goal.")
    return ""


def _combine(*parts: str) -> str:
    return " ".join(p.strip() for p in parts if p and p.strip())
