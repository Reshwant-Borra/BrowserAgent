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


def test_subgoal_changed_after_block_reverts_status_to_running():
    """Regression for a real Phase 3 corrective-pass finding (docs/BROWSERAGENT_MASTER_STATUS.md):
    agent/controller.py's continuous strategy resumes a blocked task by appending a
    controller-tagged SUBGOAL_CHANGED event and then *manually* mutating a loaded TaskState's
    `status` back to "running" before saving it — a side-channel mutation, not an event. That
    manual mutation only survives until the very next event is appended, at which point
    TaskStateStore.load()'s staleness check forces a fresh replay_task() call over the *entire*
    history — and without this fix, nothing here ever cleared a `status` that TASK_BLOCKED had
    set earlier, so replay silently reverted it back to "blocked" using the *original*
    blocked_reason, discarding the resume. Live impact: once a continuous-strategy task's own
    bounded "subgoal local attempts exhausted" safety valve fired even once, the very next
    OBSERVATION/ACTION_INTENT event from the resumed session re-triggered ANOTHER replan before
    the model could take a second action, burning the entire replan budget in seconds. A
    SUBGOAL_CHANGED event can only ever be appended by something actively (re)driving the task,
    so replaying one must durably clear any earlier block."""
    events = [
        ev(1, EventType.TASK_BLOCKED, {"reason": "subgoal local attempts exhausted: 'x'"}, id_=1),
        ev(2, EventType.SUBGOAL_CHANGED, {"subgoal": "y", "plan": ["y"], "source": "controller"}, id_=2),
        # Further events (an ordinary browser action) appended after the resuming SUBGOAL_CHANGED
        # — this is exactly what forces TaskStateStore.load() to replay the *whole* history again
        # in the real bug, so the assertion below must hold even (especially) with these present.
        ev(3, EventType.OBSERVATION, {"url": "http://x/y", "page_hash": "h1"}, id_=3),
        ev(3, EventType.VERIFICATION_RESULT, {"action": "click", "target": 1, "action_fingerprint": "click:1:{}",
                                               "url": "http://x/y"}, {"passed": True}, id_=4),
    ]
    state = replay_task("t1", events)
    assert state.status == "running"
    assert state.blocked_reason is None
    assert state.current_subgoal == "y"
