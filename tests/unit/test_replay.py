from __future__ import annotations

from memory.event_store import Event, EventType
from memory.replay import replay_task


def ev(step, type_, payload, verification_result=None, id_=None) -> Event:
    return Event(id=id_, task_id="t1", step=step, timestamp="2026-01-01T00:00:00Z",
                 type=type_, payload=payload, verification_result=verification_result)


def test_replay_reconstructs_basic_progress():
    events = [
        ev(0, EventType.TASK_CREATED, {"goal": "g"}, id_=1),
        ev(1, EventType.OBSERVATION, {"url": "http://x/1", "page_hash": "h1"}, id_=2),
        ev(1, EventType.VERIFICATION_RESULT, {"action": "click", "target": 1, "action_fingerprint": "click:1:{}",
                                               "url": "http://x/2"}, {"passed": True}, id_=3),
    ]
    state = replay_task("t1", events)
    assert state.current_step == 1
    assert state.last_event_id == 3
    assert state.recent_actions[-1]["verification"] == "pass"
    assert state.pending_action_intent is None


def test_replay_detects_pending_action_intent_after_crash():
    events = [
        ev(0, EventType.TASK_CREATED, {"goal": "g"}, id_=1),
        ev(1, EventType.OBSERVATION, {"url": "http://x/1", "page_hash": "h1"}, id_=2),
        ev(1, EventType.MODEL_DECISION, {"decision": {}}, id_=3),
        ev(1, EventType.ACTION_INTENT, {"action": "click", "target": 1, "action_fingerprint": "click:1:{}",
                                         "pre_state_hash": "h1", "risk": "low_risk_write",
                                         "expected_result": {"page_contains": "ok"}}, id_=4),
        # process crashed here: no ACTION_RESULT / VERIFICATION_RESULT follows
    ]
    state = replay_task("t1", events)
    assert state.pending_action_intent is not None
    assert state.pending_action_intent["action_fingerprint"] == "click:1:{}"


def test_action_result_clears_pending_intent():
    events = [
        ev(1, EventType.ACTION_INTENT, {"action": "click", "target": 1, "action_fingerprint": "fp"}, id_=1),
        ev(1, EventType.ACTION_RESULT, {"action_fingerprint": "fp", "post_state_hash": "h2"}, id_=2),
    ]
    state = replay_task("t1", events)
    assert state.pending_action_intent is None


def test_recovery_transition_updates_level():
    events = [
        ev(1, EventType.RECOVERY_TRANSITION, {"from": "normal", "to": "retry", "reason": "x"}, id_=1),
    ]
    state = replay_task("t1", events)
    assert state.recovery_level == "retry"


def test_task_blocked_and_completed():
    blocked = replay_task("t1", [ev(1, EventType.TASK_BLOCKED, {"reason": "stuck"}, id_=1)])
    assert blocked.status == "blocked"
    assert blocked.blocked_reason == "stuck"

    completed = replay_task("t1", [ev(1, EventType.TASK_COMPLETED, {"result": "done"}, id_=1)])
    assert completed.status == "completed"
