"""Fold the append-only event log into a TaskState. This is the authoritative reconstruction
path: if the materialized `task_state` row is missing, stale, or suspected corrupt, this is
what rebuilds it — not a cached in-memory object, not an LLM's memory of what happened.

Event-payload conventions this module understands (see memory/event_store.py EventType):
  OBSERVATION        {"url", "page_hash"}
  MODEL_DECISION     {"decision": <ModelDecision dict>}
  ACTION_INTENT      {"action", "target", "params", "pre_state_hash", "action_fingerprint", "risk"}
  ACTION_RESULT      {"action_fingerprint", "post_state_hash", "result_data", "error"}
  VERIFICATION_RESULT payload {"action_fingerprint"}; verification_result column holds the checks
  RECOVERY_TRANSITION {"from", "to", "reason"}
  SUBGOAL_CHANGED    {"subgoal", "plan"} — "subgoal": null is a deliberate clear, distinct
                     from the key being absent (see below)
  TASK_COMPLETED     {"result"}
  TASK_BLOCKED       {"reason"}

WORKSPACE_MUTATED, DELEGATE_STARTED, DELEGATE_RESULT, and COMPLETION_EVALUATED (general-
controller migration, see agent/controller.py) intentionally have no bespoke handling here —
they still advance current_step/last_event_id via the loop below, same as CHECKPOINT/
COMPACTION_* already do, but carry no TaskState field of their own. Their own state lives in
WorkspaceStore's projection (memory/workspace_store.py) and the event log itself.
"""
from __future__ import annotations

from typing import Optional

from memory.event_store import Event, EventType
from memory.models import TaskState

_RECENT_ACTIONS_CAP = 20


def replay_task(task_id: str, events: list[Event]) -> TaskState:
    state = TaskState(task_id=task_id)
    pending_intent: Optional[dict] = None

    for ev in events:
        state.current_step = max(state.current_step, ev.step)
        state.last_event_id = ev.id

        if ev.type == EventType.OBSERVATION:
            state.current_url = ev.payload.get("url", state.current_url)
            state.current_page_hash = ev.payload.get("page_hash", state.current_page_hash)

        elif ev.type == EventType.ACTION_INTENT:
            pending_intent = ev.payload

        elif ev.type == EventType.ACTION_RESULT:
            pending_intent = None  # the intent this resolves is no longer pending

        elif ev.type == EventType.VERIFICATION_RESULT:
            passed = bool(ev.verification_result and ev.verification_result.get("passed"))
            action_payload = ev.payload
            state.recent_actions.append({
                "step": ev.step,
                "action": action_payload.get("action"),
                "target": action_payload.get("target"),
                "action_fingerprint": action_payload.get("action_fingerprint"),
                "semantic_action_signature": action_payload.get("semantic_action_signature"),
                "url": action_payload.get("url"),
                "result_data": action_payload.get("result_data") or {},
                "verification": "pass" if passed else "fail",
            })
            state.recent_actions = state.recent_actions[-_RECENT_ACTIONS_CAP:]
            state.retry_count = 0 if passed else state.retry_count + 1

        elif ev.type == EventType.RECOVERY_TRANSITION:
            state.recovery_level = ev.payload.get("to", state.recovery_level)

        elif ev.type == EventType.SUBGOAL_CHANGED:
            # A payload with no "subgoal" key at all means "leave it as-is" (matches the old
            # default-preserving behavior); a payload with "subgoal": null is a deliberate
            # clear (e.g. the general controller's plan is exhausted) and must actually move
            # current_subgoal to None rather than being coerced back to the old value.
            new_subgoal = ev.payload["subgoal"] if "subgoal" in ev.payload else state.current_subgoal
            if (state.current_subgoal and state.current_subgoal != new_subgoal
                    and state.current_subgoal not in state.completed_subgoals):
                # The subgoal this event supersedes is done (whether by completion or by a
                # replan moving past it) — record it before overwriting current_subgoal below.
                state.completed_subgoals = state.completed_subgoals + [state.current_subgoal]
            state.current_subgoal = new_subgoal
            state.plan = ev.payload.get("plan", state.plan)
            state.retry_count = 0
            # A SUBGOAL_CHANGED event can only ever be appended while something is actively
            # (re)driving this task toward a subgoal — every call site (agent/loop.py's own
            # _advance_subgoal/_replan, and every agent/controller.py path that reaches
            # _apply_controller_decision) only fires this after deciding the task should keep
            # running. Before this fix, a *manual* state.status="running" mutation (e.g.
            # agent/controller.py::_reset_recovery_for_new_subgoal, called right after a
            # controller-level replan resumes a blocked continuous-strategy task) only survived
            # until the next event was appended: TaskStateStore.load()'s staleness check then
            # forced a fresh replay_task() call over the *entire* history, which — with no code
            # here to ever clear a status TASK_BLOCKED had set earlier — silently reverted status
            # back to "blocked" using the *original* blocked_reason, discarding the reset. Live
            # evidence (docs/BROWSERAGENT_MASTER_STATUS.md's Phase 3 corrective pass): once a
            # continuous-strategy task's own bounded, expected "subgoal local attempts exhausted"
            # safety valve fired even once, every later real replan was immediately re-detected
            # as still "blocked" on the very next event (an OBSERVATION, an ACTION_INTENT — any of
            # them), instantly re-triggering another replan call before the model could ever take
            # a second action, burning the *entire* replan budget in seconds regardless of how
            # well-grounded the browser session actually was. Resetting status here — during
            # *replay itself*, not a side-channel mutation — makes "replanned, now running again"
            # durable the same way every other TaskState field already is.
            state.status = "running"
            state.blocked_reason = None

        elif ev.type == EventType.TASK_COMPLETED:
            state.status = "completed"
            state.blocked_reason = None

        elif ev.type == EventType.TASK_BLOCKED:
            state.status = "blocked"
            state.blocked_reason = ev.payload.get("reason")

    # A pending ACTION_INTENT with no matching ACTION_RESULT means the process died between
    # "we decided to act" and "we recorded what happened" — the classic event-sourcing
    # crash window. Surface it; the caller (agent/loop.py resume path) must reconcile this
    # against a fresh observation before doing anything else, and must never blindly replay it.
    state.pending_action_intent = pending_intent
    return state
