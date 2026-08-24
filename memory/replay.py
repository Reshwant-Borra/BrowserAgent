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
  SUBGOAL_CHANGED    {"subgoal", "plan"}
  TASK_COMPLETED     {"result"}
  TASK_BLOCKED       {"reason"}
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
                "url": action_payload.get("url"),
                "result_data": action_payload.get("result_data") or {},
                "verification": "pass" if passed else "fail",
            })
            state.recent_actions = state.recent_actions[-_RECENT_ACTIONS_CAP:]
            state.retry_count = 0 if passed else state.retry_count + 1

        elif ev.type == EventType.RECOVERY_TRANSITION:
            state.recovery_level = ev.payload.get("to", state.recovery_level)

        elif ev.type == EventType.SUBGOAL_CHANGED:
            state.current_subgoal = ev.payload.get("subgoal", state.current_subgoal)
            state.plan = ev.payload.get("plan", state.plan)
            if state.current_subgoal and state.current_subgoal not in state.completed_subgoals:
                pass  # only appended to completed_subgoals when the *next* subgoal supersedes it
            state.retry_count = 0

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
