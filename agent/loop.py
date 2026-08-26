"""The agent loop: one step = observe -> decide -> validate -> (approve) -> act -> verify ->
detect failures -> advance recovery -> checkpoint. Every field the model or the recovery
logic needs is re-derived from `TaskState` (persisted) + a fresh `PageObservation` on every
call — this class holds live handles (browser, DB connection) but never holds anything that
would make the task incorrect if this process died and a new one loaded the same task_id.
"""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Optional

from agent.config import AppConfig
from agent.context_builder import build_contract_repair_prompt, build_replan_prompt, build_tiered_context
from agent.decision import parse_model_output, validate_against_observation
from agent.logging_utils import JsonlLogger, get_logger
from agent.loop_detector import (
    action_fingerprint,
    detect_modal_obstruction,
    detect_navigation_loop,
    detect_noop,
    detect_repeated_action,
    detect_repeated_semantic_action,
    semantic_action_signature,
)
from agent.recovery import RetryDecision, idempotency_decision, next_recovery_level, requires_approval
from agent.schemas import (
    ActionType,
    DecisionValidationError,
    ExpectedResult,
    ModelDecision,
    RecoveryLevel,
    RiskLevel,
    ValidationErrorKind,
    classify_risk,
)
from agent import verifier as verifier_mod
from browser.page_model import ElementRef, PageObservation
from browser.playwright_backend import PlaywrightBackend
from inference.llama_client import ModelUnavailableError, create_inference_client
from memory.event_store import EventStore, EventType
from memory.models import TaskRecord, TaskState
from memory.task_memory import ActiveFact, normalize_fact_key
from memory.task_state import TaskStateStore

GRAMMAR_PATH = Path(__file__).resolve().parent.parent / "inference" / "grammar" / "action.gbnf"


class AgentLoop:
    def __init__(self, config: AppConfig, task_id: str, profile_dir: Path | None = None):
        self.config = config
        self.task_id = task_id
        self.tasks_dir = Path(config.storage.tasks_dir)
        self.db_path = self.tasks_dir / task_id / "task.db"
        self.profile_dir = profile_dir or self.tasks_dir / task_id / "browser_profile"

        self.event_store = EventStore(self.db_path)
        self.state_store = TaskStateStore(self.event_store)
        self.llama = create_inference_client(config)
        self.browser = PlaywrightBackend(
            self.profile_dir, config.browser.headless, config.browser.action_timeout_ms,
            config.context.max_page_chars, config.context.max_visible_text_items,
        )
        self.grammar = GRAMMAR_PATH.read_text(encoding="utf-8")
        self.log = get_logger("agent.loop", config.logging.level)
        self.metrics = JsonlLogger(Path(config.logging.dir) / f"{task_id}.metrics.jsonl")

    # ---- lifecycle ---------------------------------------------------------

    @classmethod
    def create_new(
        cls,
        config: AppConfig,
        goal: str,
        success_criteria: list[str],
        profile_dir: Path | None = None,
    ) -> "AgentLoop":
        task_id = uuid.uuid4().hex[:12]
        loop = cls(config, task_id, profile_dir=profile_dir)
        loop.event_store.create_task(task_id, goal, success_criteria)
        loop.event_store.append(task_id, 0, EventType.TASK_CREATED,
                                 {"goal": goal, "success_criteria": success_criteria})
        loop.state_store.save(TaskState(task_id=task_id, status="running"))
        return loop

    @classmethod
    def resume(cls, config: AppConfig, task_id: str, profile_dir: Path | None = None) -> "AgentLoop":
        loop = cls(config, task_id, profile_dir=profile_dir)
        if not loop.event_store.task_exists(task_id):
            raise ValueError(f"no such task: {task_id}")
        return loop

    async def start_browser(self) -> None:
        await self.browser.start()

    async def aclose(self) -> None:
        await self.browser.close()
        self.event_store.close()

    # ---- run loop ------------------------------------------------------------

    async def run(self, max_steps: int = 200) -> TaskState:
        await self.start_browser()
        try:
            state = self.state_store.load(self.task_id)
            state = await self._reconcile_pending_intent(state)
            if state.status in ("completed", "blocked"):
                return state

            for _ in range(max_steps):
                state = self.state_store.load(self.task_id)
                if state.status != "running":
                    break
                state = await self.step(state)
            return state
        finally:
            await self.aclose()

    async def _reconcile_pending_intent(self, state: TaskState) -> TaskState:
        """Resume-time atomicity check: an ACTION_INTENT with no matching ACTION_RESULT
        means the process died mid-action. We never blindly replay it — a fresh observation
        plus the original expected_result decides what actually happened."""
        if not state.pending_action_intent:
            return state
        intent = state.pending_action_intent
        self.log.warning(f"resuming with an ambiguous pending action: {intent}")

        # A fresh persistent-context browser starts on a blank page — cookies/storage
        # persist across a restart, but the previously-open tab and its live DOM do not.
        # Re-establish a sensible position before judging what happened: for an open_url
        # intent, its destination is itself a plain idempotent GET, safe to re-visit purely
        # to inspect state (this is not "retrying the action" in the risky sense — it's
        # how any read of that URL would look regardless of whether the crash happened).
        # For any other action type, the best available position is the last URL we
        # actually observed *before* the ambiguous action (state.current_url) — this can
        # still leave a same-page, storage-less JS mutation unrecoverable (see
        # docs/PHASE3_REPORT.md), which is exactly why non-idempotent actions fall through
        # to the conservative CONSEQUENTIAL/blocked path below when it can't be confirmed.
        if intent.get("action") == ActionType.OPEN_URL.value and intent.get("params", {}).get("url"):
            await self.browser.open_url(intent["params"]["url"])
        elif state.current_url:
            await self.browser.open_url(state.current_url)

        observation = await self.browser.observe()
        risk = RiskLevel(intent.get("risk", RiskLevel.LOW_RISK_WRITE.value))
        expected = intent.get("expected_result") or {}
        verification = verifier_mod.check(ExpectedResult(**expected), observation)

        # An ACTION_RESULT (even though nothing was freshly executed) is what clears
        # pending_action_intent on replay — the intent is resolved (one way or another),
        # not left dangling as "still ambiguous" forever.
        self.event_store.append(self.task_id, state.current_step, EventType.ACTION_RESULT, {
            "action_fingerprint": intent.get("action_fingerprint"), "post_state_hash": observation.state_hash,
            "result_data": {}, "error": None, "resolved_via": "resume_reconciliation",
        })

        self.event_store.append(self.task_id, state.current_step, EventType.VERIFICATION_RESULT, {
            "action": intent.get("action"), "target": intent.get("target"),
            "action_fingerprint": intent.get("action_fingerprint"), "url": observation.url,
            "note": "post-resume reconciliation of an interrupted action",
        }, verification_result=verification.model_dump())

        if verification.passed:
            self.log.info("pending action appears to have succeeded before the crash; not repeating it")
        elif risk == RiskLevel.CONSEQUENTIAL:
            reason = "ambiguous consequential action after crash; needs human review"
            self.log.warning(reason)
            self.event_store.append(self.task_id, state.current_step, EventType.TASK_BLOCKED, {"reason": reason})
            state.status = "blocked"
            state.blocked_reason = reason
        else:
            self.log.info("pending action's outcome is ambiguous but low-risk; forcing a full state "
                           "refresh and re-deciding rather than auto-repeating it")
            self.event_store.append(self.task_id, state.current_step, EventType.RECOVERY_TRANSITION,
                                     {"from": state.recovery_level, "to": RecoveryLevel.REFRESH_STATE.value,
                                      "reason": "resume reconciliation"})
            state.recovery_level = RecoveryLevel.REFRESH_STATE.value

        self.state_store.save(state)
        return self.state_store.load(self.task_id)

    # ---- one step --------------------------------------------------------

    async def step(self, state: TaskState) -> TaskState:
        task = self.state_store.get_task_record(self.task_id)
        step_no = state.current_step + 1

        max_chars = self.config.context.max_page_chars
        max_tokens = self.config.model.max_output_tokens
        if state.recovery_level == RecoveryLevel.DEEP_RECOVERY.value:
            max_chars = self.config.context.max_page_chars_deep_recovery
            max_tokens = self.config.model.max_output_tokens_deep_recovery

        observation = await self.browser.observe()
        self._append_observation_event(step_no, observation, phase="pre_decision")
        self.metrics.log(event="observation", task_id=self.task_id, step=step_no,
                          element_count=observation.element_count, char_count=observation.char_count,
                          truncated=observation.truncated)

        if state.recovery_level == RecoveryLevel.REPLAN_REQUIRED.value:
            return await self._replan(task, state, step_no)

        recent_failures = [
            f"step {r['step']}: {r['action']} target={r.get('target')} failed verification"
            for r in state.recent_actions if r.get("verification") == "fail"
        ][-3:]

        context = build_tiered_context(
            task,
            state,
            observation,
            self.config.context,
            max_chars,
            self.config.context.max_visible_text_items,
            self.event_store,
            recent_failures,
        )
        prompt = context.prompt

        completion = await self.llama.complete(prompt, grammar=self.grammar, max_tokens=max_tokens)
        self.metrics.log(event="model_call", task_id=self.task_id, step=step_no,
                          input_chars=len(prompt), prompt_tokens=completion.prompt_tokens,
                          output_tokens=completion.predicted_tokens, prompt_ms=completion.prompt_ms,
                          predicted_ms=completion.predicted_ms, total_latency_ms=completion.total_latency_ms,
                          recovery_level=state.recovery_level, page_element_count=observation.element_count,
                          page_char_count=observation.char_count, endpoint=self.llama.endpoint,
                          model_backend=self.config.model.backend, model_name=self.config.model.model_name,
                          context_block_tokens=context.block_tokens,
                          context_block_chars=context.block_chars,
                          total_estimated_prompt_tokens=context.total_estimated_tokens,
                          retrieved_memory_count=context.retrieved_memory_count,
                          running_summary_tokens=context.running_summary_tokens,
                          retrieval_query_terms=context.retrieval_query_terms,
                          candidate_memory_ids=context.candidate_memory_ids,
                          selected_memory_ids=context.selected_memory_ids,
                          selected_memory_kinds=context.selected_memory_kinds,
                          selected_memory_source_event_ids=context.selected_memory_source_event_ids,
                          selected_memory_tokens=context.selected_memory_tokens,
                          active_fact_ids=[fact.id for fact in context.active_facts],
                          active_fact_source_event_ids=[fact.source_event_id for fact in context.active_facts],
                          active_fact_tokens=context.active_fact_tokens,
                          event_load_ms=context.timing_ms.get("event_load_ms"),
                          memory_ingest_ms=context.timing_ms.get("memory_ingest_ms"),
                          summary_update_ms=context.timing_ms.get("summary_update_ms"),
                          retrieval_query_build_ms=context.timing_ms.get("retrieval_query_build_ms"),
                          fts_search_ms=context.timing_ms.get("fts_search_ms"),
                          context_render_ms=context.timing_ms.get("context_render_ms"),
                          derived_memory_inserted=context.timing_ms.get("derived_memory_inserted"))

        decision = await self._parse_validate_or_repair(
            task, state, step_no, prompt, completion.text, observation, max_chars,
            self.config.context.max_visible_text_items, max_tokens,
        )
        if decision is None:
            state = self.state_store.load(self.task_id)
            return self._advance_recovery(
                state, step_no, verification_passed=False, loop_detected=False,
                reason_override="MODEL_CONTRACT_ERROR",
            )

        self.event_store.append(self.task_id, step_no, EventType.MODEL_DECISION,
                                 {"decision": decision.model_dump(mode="json")})

        element: Optional[ElementRef] = observation.element_by_id(decision.target) if decision.target is not None else None
        risk = classify_risk(decision.action, element.name if element else None)
        semantic_signature = semantic_action_signature(
            decision.action.value,
            element.name if element else None,
            decision.params,
        )
        memory_application = self._memory_application_check(context.active_facts, decision, element, observation)
        self.metrics.log(event="memory_application", task_id=self.task_id, step=step_no,
                          action=decision.action.value, target=decision.target,
                          target_name=element.name if element else None,
                          semantic_action_signature=semantic_signature,
                          memory_action_value_match=memory_application["value_match"],
                          memory_value_conflict=memory_application["value_conflict"],
                          matched_fact_ids=memory_application["matched_fact_ids"],
                          conflicting_fact_ids=memory_application["conflicting_fact_ids"],
                          unresolved_fact_ids=memory_application["unresolved_fact_ids"],
                          mapped_fact_ids=memory_application["mapped_fact_ids"])

        if (
            self.config.context.enforce_active_fact_constraints
            and (memory_application["value_conflict"] or memory_application["unresolved_fact_ids"])
        ):
            correction = self._constraint_guard_correction(
                context.active_facts,
                memory_application,
                decision,
                observation,
            )
            if correction is None:
                self.event_store.append(self.task_id, step_no, EventType.MODEL_DECISION, {
                    "error": "active_fact_constraint_conflict",
                    "message": "proposed action conflicts with a high-confidence active fact",
                    "conflicting_fact_ids": memory_application["conflicting_fact_ids"],
                    "unresolved_fact_ids": memory_application["unresolved_fact_ids"],
                    "semantic_action_signature": semantic_signature,
                })
                state = self.state_store.load(self.task_id)
                return self._advance_recovery(
                    state,
                    step_no,
                    verification_passed=False,
                    loop_detected=True,
                    reason_override="MEMORY_APPLICATION_ERROR",
                )
            self.event_store.append(self.task_id, step_no, EventType.MODEL_DECISION, {
                "constraint_guard_override": True,
                "original_decision": decision.model_dump(mode="json"),
                "corrected_decision": correction.model_dump(mode="json"),
                "conflicting_fact_ids": memory_application["conflicting_fact_ids"],
                "unresolved_fact_ids": memory_application["unresolved_fact_ids"],
            })
            self.metrics.log(event="constraint_guard", task_id=self.task_id, step=step_no,
                              original_action=decision.action.value,
                              corrected_action=correction.action.value,
                              corrected_target=correction.target,
                              conflicting_fact_ids=memory_application["conflicting_fact_ids"],
                              unresolved_fact_ids=memory_application["unresolved_fact_ids"])
            decision = correction
            validate_against_observation(decision, observation)
            element = observation.element_by_id(decision.target) if decision.target is not None else None
            risk = classify_risk(decision.action, element.name if element else None)
            semantic_signature = semantic_action_signature(
                decision.action.value,
                element.name if element else None,
                decision.params,
            )
            memory_application = self._memory_application_check(
                context.active_facts,
                decision,
                element,
                observation,
            )
            self.metrics.log(event="memory_application", task_id=self.task_id, step=step_no,
                              action=decision.action.value, target=decision.target,
                              target_name=element.name if element else None,
                              semantic_action_signature=semantic_signature,
                              memory_action_value_match=memory_application["value_match"],
                              memory_value_conflict=memory_application["value_conflict"],
                              matched_fact_ids=memory_application["matched_fact_ids"],
                              conflicting_fact_ids=memory_application["conflicting_fact_ids"],
                              unresolved_fact_ids=memory_application["unresolved_fact_ids"],
                              mapped_fact_ids=memory_application["mapped_fact_ids"],
                              constraint_guard_corrected=True)

        if decision.action == ActionType.FINISH:
            return await self._handle_finish(task, state, step_no, decision, observation)

        if requires_approval(risk, self.config.browser.interactive_approval):
            if not self._prompt_for_approval(decision, element):
                state = self.state_store.load(self.task_id)
                state.status = "blocked"
                state.blocked_reason = (f"user declined consequential action: {decision.action.value} "
                                         f"on '{element.name if element else None}'")
                self.event_store.append(self.task_id, step_no, EventType.TASK_BLOCKED,
                                         {"reason": state.blocked_reason})
                self.state_store.save(state)
                return state

        fingerprint = action_fingerprint(decision.action.value, decision.target, decision.params)
        pre_hash = observation.state_hash

        last_same = next((r for r in reversed(state.recent_actions)
                           if r.get("action_fingerprint") == fingerprint), None)
        if last_same and last_same.get("verification") == "fail" and risk == RiskLevel.CONSEQUENTIAL:
            # state_changed is irrelevant here: idempotency_decision's Case C returns
            # NEVER_RETRY_CONSEQUENTIAL unconditionally for CONSEQUENTIAL risk regardless of
            # this argument (we don't track a per-attempt pre/post hash pair to compute it
            # precisely, and for this risk tier it wouldn't change the outcome anyway).
            policy = idempotency_decision(risk, state_changed=False, verification_passed=False)
            if policy == RetryDecision.NEVER_RETRY_CONSEQUENTIAL:
                state = self.state_store.load(self.task_id)
                state.status = "blocked"
                state.blocked_reason = f"refusing to auto-retry a consequential action that previously failed: {fingerprint}"
                self.event_store.append(self.task_id, step_no, EventType.TASK_BLOCKED,
                                         {"reason": state.blocked_reason})
                self.state_store.save(state)
                return state

        self.event_store.append(self.task_id, step_no, EventType.ACTION_INTENT, {
            "action": decision.action.value, "target": decision.target,
            "params": self._safe_params(decision, element),
            "pre_state_hash": pre_hash, "action_fingerprint": fingerprint,
            "semantic_action_signature": semantic_signature, "risk": risk.value,
            "expected_result": decision.expected_result.model_dump(),
        })

        action_start = time.monotonic()
        error: Optional[str] = None
        result_data: dict = {}
        try:
            result_data = await self._execute(decision, observation)
        except Exception as e:  # action execution failures are expected operational events, not bugs
            error = str(e)
        action_latency_ms = (time.monotonic() - action_start) * 1000

        new_observation = await self.browser.observe()
        post_hash = new_observation.state_hash
        self._append_observation_event(step_no, new_observation, phase="post_action")

        self.event_store.append(self.task_id, step_no, EventType.ACTION_RESULT, {
            "action_fingerprint": fingerprint, "post_state_hash": post_hash,
            "result_data": result_data, "error": error,
        })

        verification = verifier_mod.check_hybrid(decision, observation, new_observation, result_data, error)
        self.event_store.append(self.task_id, step_no, EventType.VERIFICATION_RESULT, {
            "action": decision.action.value, "target": decision.target, "action_fingerprint": fingerprint,
            "semantic_action_signature": semantic_signature,
            "url": new_observation.url, "result_data": self._safe_result_data(decision, result_data),
            "memory_application": memory_application,
        }, verification_result=verification.model_dump())

        self.metrics.log(event="action", task_id=self.task_id, step=step_no, action=decision.action.value,
                          target_signature=fingerprint, pre_state_hash=pre_hash, post_state_hash=post_hash,
                          semantic_action_signature=semantic_signature,
                          verification_passed=verification.passed, latency_ms=action_latency_ms,
                          recovery_level=state.recovery_level, retry_number=state.retry_count, risk=risk.value)

        state = self.state_store.load(self.task_id)
        if self._should_complete_after_verified_download(task, decision, verification.passed):
            return self._complete_from_download(task, state, step_no, decision, new_observation)
        if self._should_complete_after_verified_action(task, decision, verification.passed, new_observation):
            return self._complete_after_verified_success(task, state, step_no, decision, new_observation)

        noop = detect_noop(pre_hash, post_hash, expected_change=not decision.expected_result.is_empty())
        repeated_action_loop = (
            detect_repeated_action(state.recent_actions, fingerprint, self.config.recovery.identical_action_limit)
            or detect_repeated_semantic_action(
                state.recent_actions,
                semantic_signature,
                self.config.recovery.identical_action_limit,
            )
        ) and (not verification.passed or noop)
        loop_signal = (
            repeated_action_loop
            or detect_navigation_loop(state.recent_actions, self.config.recovery.navigation_cycle_limit)
            or detect_modal_obstruction(new_observation.modal_present, last_action_targeted_modal=False)
            or (noop and not verification.passed)
        )

        return self._advance_recovery(
            state,
            step_no,
            verification.passed,
            loop_signal,
            reason_override="MEMORY_APPLICATION_ERROR" if (
                (memory_application["value_conflict"] or memory_application["unresolved_fact_ids"])
                and not verification.passed
            ) else None,
        )

    # ---- helpers -----------------------------------------------------------

    def _memory_application_check(
        self,
        facts: list[ActiveFact],
        decision: ModelDecision,
        element: Optional[ElementRef],
        observation: Optional[PageObservation] = None,
    ) -> dict:
        mapped: list[int] = []
        matched: list[int] = []
        conflicted: list[int] = []
        unresolved: list[int] = []
        action_value = self._decision_value(decision, element)
        for fact in facts:
            if fact.confidence < 0.95 or not self._fact_maps_to_element(fact, decision, element):
                continue
            mapped.append(fact.id)
            if action_value is None:
                continue
            if self._values_match(fact.value, action_value):
                matched.append(fact.id)
            elif decision.action in {ActionType.TYPE, ActionType.SELECT, ActionType.DOWNLOAD}:
                conflicted.append(fact.id)
        combined_expected = self._combined_collected_value(facts)
        combined_ids = [fact.id for fact in self._ordered_collected_facts(facts)]
        if combined_expected and element is not None and self._is_combined_facts_control(element):
            mapped.extend(combined_ids)
            if action_value is not None and self._values_match(combined_expected, action_value):
                matched.extend(combined_ids)
            elif decision.action == ActionType.TYPE:
                conflicted.extend(combined_ids)
        if decision.action == ActionType.CLICK and self._looks_like_commit_control(element) and observation is not None:
            combined_control = self._combined_facts_control(observation)
            if combined_expected and combined_control is not None:
                mapped.extend(combined_ids)
                if self._values_match(combined_expected, str(combined_control.value or "")):
                    matched.extend(combined_ids)
                else:
                    unresolved.extend(combined_ids)
            for fact in facts:
                if fact.confidence < 0.95:
                    continue
                control = self._mapped_control_for_fact(fact, observation)
                if control is None:
                    continue
                mapped.append(fact.id)
                if self._control_value_matches(fact, control):
                    matched.append(fact.id)
                else:
                    unresolved.append(fact.id)
        return {
            "mapped_fact_ids": sorted(set(mapped)),
            "matched_fact_ids": sorted(set(matched)),
            "conflicting_fact_ids": sorted(set(conflicted)),
            "unresolved_fact_ids": sorted(set(unresolved)),
            "value_match": bool(matched),
            "value_conflict": bool(conflicted),
        }

    def _decision_value(self, decision: ModelDecision, element: Optional[ElementRef]) -> Optional[str]:
        if decision.action == ActionType.SELECT:
            return str(decision.params.get("value", ""))
        if decision.action == ActionType.TYPE:
            return str(decision.params.get("text", ""))
        if decision.action == ActionType.DOWNLOAD and element is not None:
            return " ".join(part for part in [element.name, element.href or ""] if part)
        return None

    def _constraint_guard_correction(
        self,
        facts: list[ActiveFact],
        memory_application: dict,
        original: ModelDecision,
        observation: PageObservation,
    ) -> Optional[ModelDecision]:
        candidate_ids = (
            memory_application.get("conflicting_fact_ids", [])
            + memory_application.get("unresolved_fact_ids", [])
        )
        if not candidate_ids:
            return None
        if original.action == ActionType.DOWNLOAD:
            nav = self._next_navigation_control(observation)
            if nav is not None:
                return ModelDecision(
                    action=ActionType.CLICK,
                    target=nav.id,
                    verification_mode="action_default",
                )
        facts_by_id = {fact.id: fact for fact in facts}
        combined_value = self._combined_collected_value(facts)
        if combined_value and any(
            fact_id in {fact.id for fact in self._ordered_collected_facts(facts)}
            for fact_id in candidate_ids
        ):
            control = self._combined_facts_control(observation)
            if control is not None:
                return ModelDecision(
                    action=ActionType.TYPE,
                    target=control.id,
                    params={"text": combined_value},
                    verification_mode="action_default",
                )
        for fact_id in candidate_ids:
            fact = facts_by_id.get(fact_id)
            if fact is None:
                continue
            control = self._mapped_control_for_fact(fact, observation)
            if control is None:
                continue
            if control.role in {"select", "combobox"}:
                if control.options is not None and fact.value not in control.options:
                    continue
                return ModelDecision(
                    action=ActionType.SELECT,
                    target=control.id,
                    params={"value": fact.value},
                    verification_mode="action_default",
                )
            if control.role == "textbox":
                return ModelDecision(
                    action=ActionType.TYPE,
                    target=control.id,
                    params={"text": fact.value},
                    verification_mode="action_default",
                )
        return None

    def _next_navigation_control(self, observation: PageObservation) -> Optional[ElementRef]:
        for candidate in observation.elements:
            if candidate.role not in {"button", "link"} or candidate.disabled:
                continue
            name = normalize_fact_key(candidate.name)
            if name in {"continue", "next", "next step"}:
                return candidate
        return None

    def _fact_maps_to_element(
        self,
        fact: ActiveFact,
        decision: ModelDecision,
        element: Optional[ElementRef],
    ) -> bool:
        if decision.action == ActionType.DOWNLOAD:
            key = normalize_fact_key(fact.key)
            return fact.kind == "artifact_target" or key in {"artifact", "file", "filename", "download"}
        if element is None or decision.action not in {ActionType.TYPE, ActionType.SELECT}:
            return False
        fact_key = normalize_fact_key(fact.key)
        element_name = normalize_fact_key(element.name)
        element_terms = set(element_name.split())
        return fact_key in element_terms or fact_key == element_name

    def _mapped_control_for_fact(
        self,
        fact: ActiveFact,
        observation: PageObservation,
    ) -> Optional[ElementRef]:
        fact_key = normalize_fact_key(fact.key)
        for candidate in observation.elements:
            if candidate.role not in {"textbox", "select", "combobox"}:
                continue
            name = normalize_fact_key(candidate.name)
            if fact_key == name or fact_key in set(name.split()):
                return candidate
        return None

    def _combined_facts_control(self, observation: PageObservation) -> Optional[ElementRef]:
        for candidate in observation.elements:
            if candidate.role == "textbox" and self._is_combined_facts_control(candidate):
                return candidate
        return None

    def _is_combined_facts_control(self, element: ElementRef) -> bool:
        name = normalize_fact_key(element.name)
        return "combined" in name.split() and "facts" in name.split()

    def _ordered_collected_facts(self, facts: list[ActiveFact]) -> list[ActiveFact]:
        collected = [fact for fact in facts if normalize_fact_key(fact.key).startswith("fact ")]

        def order_key(fact: ActiveFact) -> tuple[int, int]:
            suffix = normalize_fact_key(fact.key).replace("fact", "").strip()
            return (int(suffix) if suffix.isdigit() else 9999, fact.source_event_id)

        return sorted(collected, key=order_key)

    def _combined_collected_value(self, facts: list[ActiveFact]) -> str:
        return " ".join(fact.value for fact in self._ordered_collected_facts(facts))

    def _control_value_matches(self, fact: ActiveFact, element: ElementRef) -> bool:
        return element.value is not None and self._values_match(fact.value, str(element.value))

    def _looks_like_commit_control(self, element: Optional[ElementRef]) -> bool:
        if element is None:
            return False
        name = (element.name or "").lower()
        return any(word in name for word in ("save", "submit", "apply", "confirm"))

    def _values_match(self, expected: str, actual: str) -> bool:
        expected_norm = expected.strip().lower()
        actual_norm = actual.strip().lower()
        return expected_norm == actual_norm or expected_norm in actual_norm

    def _append_observation_event(self, step_no: int, observation: PageObservation, phase: str) -> None:
        self.event_store.append(self.task_id, step_no, EventType.OBSERVATION, {
            "url": observation.url,
            "title": observation.title,
            "page_hash": observation.state_hash,
            "element_count": observation.element_count,
            "char_count": observation.char_count,
            "phase": phase,
            "element_names": [e.name for e in observation.elements[:30]],
            "visible_text": observation.visible_text[:30],
        })

    async def _parse_validate_or_repair(
        self,
        task: TaskRecord,
        state: TaskState,
        step_no: int,
        prompt: str,
        raw_text: str,
        observation: PageObservation,
        max_chars: int,
        max_visible_text_items: int,
        max_tokens: int,
    ) -> Optional[ModelDecision]:
        try:
            decision = parse_model_output(raw_text)
            validate_against_observation(decision, observation)
            self.metrics.log(event="model_contract", task_id=self.task_id, step=step_no,
                              syntax_valid=True, schema_valid=True, semantic_valid=True,
                              repair_attempt=False, repair_success=None)
            return decision
        except DecisionValidationError as first_error:
            syntax_valid = first_error.kind != ValidationErrorKind.MALFORMED_JSON
            schema_valid = first_error.kind not in {
                ValidationErrorKind.MALFORMED_JSON,
                ValidationErrorKind.SCHEMA_INVALID,
            }
            self.event_store.append(self.task_id, step_no, EventType.MODEL_DECISION, {
                "raw": raw_text[:2000],
                "error": first_error.kind.value,
                "message": first_error.message,
                "contract_error": True,
                "repair_attempted": True,
            })

            repair_prompt = build_contract_repair_prompt(
                task, observation, raw_text, first_error, max_chars, max_visible_text_items,
            )
            repair_completion = await self.llama.complete(
                repair_prompt, grammar=self.grammar, max_tokens=max_tokens,
            )
            self.metrics.log(event="model_call", task_id=self.task_id, step=step_no,
                              input_chars=len(repair_prompt), prompt_tokens=repair_completion.prompt_tokens,
                              output_tokens=repair_completion.predicted_tokens,
                              prompt_ms=repair_completion.prompt_ms,
                              predicted_ms=repair_completion.predicted_ms,
                              total_latency_ms=repair_completion.total_latency_ms,
                              recovery_level=state.recovery_level, page_element_count=observation.element_count,
                              page_char_count=observation.char_count, endpoint=self.llama.endpoint,
                              model_backend=self.config.model.backend, model_name=self.config.model.model_name,
                              contract_repair=True)
            try:
                repaired = parse_model_output(repair_completion.text)
                validate_against_observation(repaired, observation)
                self.metrics.log(event="model_contract", task_id=self.task_id, step=step_no,
                                  syntax_valid=syntax_valid, schema_valid=schema_valid,
                                  semantic_valid=True, repair_attempt=True, repair_success=True)
                return repaired
            except DecisionValidationError as repair_error:
                self.metrics.log(event="model_contract", task_id=self.task_id, step=step_no,
                                  syntax_valid=syntax_valid, schema_valid=schema_valid,
                                  semantic_valid=False, repair_attempt=True, repair_success=False)
                self.event_store.append(self.task_id, step_no, EventType.MODEL_DECISION, {
                    "raw": repair_completion.text[:2000],
                    "error": repair_error.kind.value,
                    "message": repair_error.message,
                    "contract_error": True,
                    "repair_attempt": True,
                    "repair_success": False,
                })
                return None

    def _advance_recovery(self, state: TaskState, step_no: int, verification_passed: bool,
                           loop_detected: bool, reason_override: Optional[str] = None) -> TaskState:
        current = RecoveryLevel(state.recovery_level)
        new_level = next_recovery_level(current, verification_passed, loop_detected,
                                         state.retry_count, self.config.recovery.max_action_retries)
        if new_level != current:
            reason = reason_override or (
                "loop_detected" if loop_detected else ("verification_failed" if not verification_passed else "recovered")
            )
            self.event_store.append(self.task_id, step_no, EventType.RECOVERY_TRANSITION,
                                     {"from": current.value, "to": new_level.value, "reason": reason})
        state.recovery_level = new_level.value
        if new_level == RecoveryLevel.USER_REQUIRED:
            state.status = "blocked"
            state.blocked_reason = state.blocked_reason or "repeated failures exhausted automatic recovery"
            self.event_store.append(self.task_id, step_no, EventType.TASK_BLOCKED, {"reason": state.blocked_reason})
        self.state_store.save(state)
        return state

    async def _execute(self, decision: ModelDecision, observation: PageObservation) -> dict:
        a = decision.action
        if a == ActionType.OPEN_URL:
            await self.browser.open_url(decision.params["url"])
            return {}
        if a == ActionType.CLICK:
            await self.browser.click(observation, decision.target)
            return {}
        if a == ActionType.TYPE:
            await self.browser.type(observation, decision.target, decision.params["text"])
            return {}
        if a == ActionType.SELECT:
            await self.browser.select(observation, decision.target, decision.params["value"])
            return {}
        if a == ActionType.SCROLL:
            await self.browser.scroll(decision.params.get("direction", "down"))
            return {}
        if a == ActionType.BACK:
            await self.browser.back()
            return {}
        if a == ActionType.EXTRACT:
            text = await self.browser.extract(observation, decision.target)
            return {"extracted": text[:1000]}
        if a == ActionType.DOWNLOAD:
            return await self.browser.download(observation, decision.target)
        if a == ActionType.WAIT:
            await self.browser.wait(decision.params)
            return {}
        raise ValueError(f"unhandled action for execution: {a}")

    def _safe_params(self, decision: ModelDecision, element: Optional[ElementRef]) -> dict:
        if decision.action == ActionType.TYPE and element is not None and element.sensitive:
            return {**decision.params, "text": "***REDACTED***"}
        return decision.params

    def _safe_result_data(self, decision: ModelDecision, result_data: dict) -> dict:
        if decision.action == ActionType.DOWNLOAD:
            return {"suggested_filename": result_data.get("suggested_filename")}
        if decision.action == ActionType.EXTRACT:
            return {"extracted": result_data.get("extracted")}
        return {}

    def _prompt_for_approval(self, decision: ModelDecision, element: Optional[ElementRef]) -> bool:
        name = element.name if element else "(no target)"
        print(f"\n[APPROVAL REQUIRED] action={decision.action.value} target='{name}' reason={decision.reason}")
        answer = input("Approve this consequential action? [y/N]: ").strip().lower()
        return answer == "y"

    async def _handle_finish(self, task: TaskRecord, state: TaskState, step_no: int,
                              decision: ModelDecision, observation: PageObservation) -> TaskState:
        result_text = decision.params.get("result", "")
        blob = self._finish_evidence_blob(observation)
        matched = [c for c in task.success_criteria if c.lower() in blob]
        if not task.success_criteria and not any(r.get("verification") == "pass" for r in state.recent_actions):
            self.event_store.append(self.task_id, step_no, EventType.VERIFICATION_RESULT, {
                "action": ActionType.FINISH.value,
                "target": None,
                "action_fingerprint": "finish",
                "url": observation.url,
                "result_data": {"result": result_text},
            }, verification_result={
                "passed": False,
                "checks": [{
                    "type": "finish_terminal_evidence",
                    "expected": "verified action or explicit completion evidence before finish",
                    "actual": "no prior verified action evidence",
                    "passed": False,
                }],
            })
            self.event_store.append(self.task_id, step_no, EventType.MODEL_DECISION, {
                "error": ValidationErrorKind.MODEL_COMPLETION_ERROR.value,
                "message": "finish requested with no explicit criteria and no verified task evidence",
            })
            state = self.state_store.load(self.task_id)
            return self._advance_recovery(
                state,
                step_no,
                verification_passed=False,
                loop_detected=False,
                reason_override="MODEL_COMPLETION_ERROR",
            )
        if task.success_criteria and len(matched) != len(task.success_criteria):
            self.event_store.append(self.task_id, step_no, EventType.VERIFICATION_RESULT, {
                "action": ActionType.FINISH.value,
                "target": None,
                "action_fingerprint": "finish",
                "url": observation.url,
                "result_data": {"result": result_text, "matched_success_criteria": matched},
            }, verification_result={
                "passed": False,
                "checks": [{
                    "type": "finish_success_criteria",
                    "expected": "; ".join(task.success_criteria),
                    "actual": blob[:200],
                    "passed": False,
                }],
            })
            self.event_store.append(self.task_id, step_no, EventType.MODEL_DECISION, {
                "error": ValidationErrorKind.MODEL_COMPLETION_ERROR.value,
                "message": "finish requested before success criteria were evidenced",
                "matched_success_criteria": matched,
            })
            state = self.state_store.load(self.task_id)
            return self._advance_recovery(
                state,
                step_no,
                verification_passed=False,
                loop_detected=False,
                reason_override="MODEL_COMPLETION_ERROR",
            )
        self.event_store.append(self.task_id, step_no, EventType.TASK_COMPLETED, {
            "result": result_text, "final_url": observation.url, "final_title": observation.title,
            "final_text_excerpt": "\n".join(observation.visible_text[:5]),
            "success_criteria_textual_matches": matched, "success_criteria_total": len(task.success_criteria),
        })
        self.log.info(f"task {self.task_id} reports completion: {result_text!r} "
                       f"({len(matched)}/{len(task.success_criteria)} success criteria textually matched)")
        state = self.state_store.load(self.task_id)
        state.status = "completed"
        self.state_store.save(state)
        return state

    def _finish_evidence_blob(self, observation: PageObservation) -> str:
        current = self._observation_blob(observation)
        events = [e.model_dump(mode="json") for e in self.event_store.all_events(self.task_id)]
        return (current + "\n" + self._event_evidence_blob(events)).lower()

    def _observation_blob(self, observation: PageObservation) -> str:
        return "\n".join(
            [observation.url, observation.title]
            + [element.name for element in observation.elements]
            + observation.visible_text
        ).lower()

    def _should_complete_after_verified_download(
        self,
        task: TaskRecord,
        decision: ModelDecision,
        verification_passed: bool,
    ) -> bool:
        if decision.action != ActionType.DOWNLOAD or not verification_passed:
            return False
        if not task.success_criteria:
            return True
        events = [e.model_dump(mode="json") for e in self.event_store.all_events(self.task_id)]
        return self._criteria_seen_in_events(events, task.success_criteria)

    def _should_complete_after_verified_action(
        self,
        task: TaskRecord,
        decision: ModelDecision,
        verification_passed: bool,
        observation: PageObservation,
    ) -> bool:
        if not verification_passed or not task.success_criteria:
            return False
        if decision.action in {
            ActionType.OPEN_URL,
            ActionType.DOWNLOAD,
            ActionType.FINISH,
            ActionType.WAIT,
            ActionType.EXTRACT,
            ActionType.SCROLL,
            ActionType.BACK,
        }:
            return False
        blob = self._observation_blob(observation)
        return all(criterion.lower() in blob for criterion in task.success_criteria)

    def _complete_after_verified_success(
        self,
        task: TaskRecord,
        state: TaskState,
        step_no: int,
        decision: ModelDecision,
        observation: PageObservation,
    ) -> TaskState:
        blob = self._observation_blob(observation)
        matched = [criterion for criterion in task.success_criteria if criterion.lower() in blob]
        self.event_store.append(self.task_id, step_no, EventType.TASK_COMPLETED, {
            "result": f"Success criteria satisfied after verified {decision.action.value}.",
            "final_url": observation.url,
            "final_title": observation.title,
            "final_text_excerpt": "\n".join(observation.visible_text[:5]),
            "success_criteria_textual_matches": matched,
            "success_criteria_total": len(task.success_criteria),
            "auto_completed_after_verified_action": True,
        })
        state = self.state_store.load(self.task_id)
        state.status = "completed"
        self.state_store.save(state)
        return state

    def _complete_from_download(
        self,
        task: TaskRecord,
        state: TaskState,
        step_no: int,
        decision: ModelDecision,
        observation: PageObservation,
    ) -> TaskState:
        result_text = decision.reason or "Verified download completed and success criteria are satisfied."
        events = [e.model_dump(mode="json") for e in self.event_store.all_events(self.task_id)]
        matched = [
            criterion for criterion in task.success_criteria
            if criterion.lower() in self._event_evidence_blob(events)
        ]
        self.event_store.append(self.task_id, step_no, EventType.TASK_COMPLETED, {
            "result": result_text,
            "final_url": observation.url,
            "final_title": observation.title,
            "final_text_excerpt": "\n".join(observation.visible_text[:5]),
            "success_criteria_textual_matches": matched,
            "success_criteria_total": len(task.success_criteria),
            "auto_completed_after_verified_download": True,
        })
        state = self.state_store.load(self.task_id)
        state.status = "completed"
        self.state_store.save(state)
        return state

    def _criteria_seen_in_events(self, events: list[dict], criteria: list[str]) -> bool:
        blob = self._event_evidence_blob(events)
        return all(criterion.lower() in blob for criterion in criteria)

    def _event_evidence_blob(self, events: list[dict]) -> str:
        parts: list[str] = []
        for event in events:
            payload = event.get("payload", {})
            if event.get("type") == EventType.OBSERVATION.value:
                parts.extend([
                    payload.get("url", ""),
                    payload.get("title", ""),
                    " ".join(payload.get("element_names", [])),
                    " ".join(payload.get("visible_text", [])),
                ])
            elif event.get("type") == EventType.ACTION_RESULT.value:
                parts.append(json.dumps(payload.get("result_data", {})))
            elif event.get("type") == EventType.VERIFICATION_RESULT.value:
                if payload.get("action") in {ActionType.DOWNLOAD.value, ActionType.EXTRACT.value}:
                    parts.append(json.dumps(payload.get("result_data", {})))
            elif event.get("type") == EventType.TASK_COMPLETED.value:
                parts.append(json.dumps(payload))
        return "\n".join(parts).lower()

    async def _replan(self, task: TaskRecord, state: TaskState, step_no: int) -> TaskState:
        prompt = build_replan_prompt(task, state)
        completion = await self.llama.complete(prompt, grammar=None,
                                                max_tokens=self.config.model.max_output_tokens_deep_recovery)
        subgoal, plan = state.current_subgoal, state.plan
        try:
            data = json.loads(completion.text.strip())
            subgoal = data.get("subgoal", subgoal)
            plan = data.get("plan", plan)
        except (json.JSONDecodeError, AttributeError):
            self.log.warning("replan call returned unparseable output; keeping previous plan")

        self.event_store.append(self.task_id, step_no, EventType.SUBGOAL_CHANGED, {"subgoal": subgoal, "plan": plan})
        self.event_store.append(self.task_id, step_no, EventType.RECOVERY_TRANSITION,
                                 {"from": RecoveryLevel.REPLAN_REQUIRED.value, "to": RecoveryLevel.NORMAL.value,
                                  "reason": "replanned"})
        state = self.state_store.load(self.task_id)
        state.current_subgoal = subgoal
        state.plan = plan
        state.recovery_level = RecoveryLevel.NORMAL.value
        state.retry_count = 0
        self.state_store.save(state)
        return state
