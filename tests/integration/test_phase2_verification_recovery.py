"""Phase 2 exit gate: verification is deterministic, failures are distinguishable from
successes, no-op/navigation-loop detection actually fires, and a consequential action that
already failed once is never automatically retried."""
from __future__ import annotations

import pytest

from agent.schemas import RecoveryLevel
from memory.event_store import EventType
from tests.integration.fake_llama import decision
from tests.integration.helpers import read_events

pytestmark = pytest.mark.asyncio


async def test_noop_button_triggers_recovery_escalation(make_agent_loop, fixture_site_url):
    loop = make_agent_loop("Trigger the no-op button and notice nothing happens", [], [
        decision("open_url", params={"url": fixture_site_url + "/noop.html"}),
        decision("click", target=1, expected_result={"element_present": "nothing happened confirmation"}),
        decision("finish", params={"result": "confirmed no-op"}),
    ])
    state = await loop.run(max_steps=10)
    assert state.status == "completed"
    # The no-op + failed verification must have escalated recovery away from "normal",
    # and that escalation must have survived all the way to task completion (finish does
    # not silently reset recovery_level).
    assert state.recovery_level != RecoveryLevel.NORMAL.value

    events = read_events(loop)
    verifications = [e for e in events if e.type == EventType.VERIFICATION_RESULT]
    assert any(e.verification_result and not e.verification_result["passed"] for e in verifications)
    transitions = [e for e in events if e.type == EventType.RECOVERY_TRANSITION]
    assert any(t.payload["to"] != RecoveryLevel.NORMAL.value for t in transitions)


async def test_delayed_navigation_is_awaited_correctly(make_agent_loop, fixture_site_url):
    loop = make_agent_loop("Continue past the delayed-navigation page", [], [
        decision("open_url", params={"url": fixture_site_url + "/delayed.html"}),
        decision("click", target=1),
        decision("wait", params={"url_contains": "delayed_target"},
                  expected_result={"url_contains": "delayed_target"}),
        decision("finish", params={"result": "arrived after the delay"}),
    ])
    state = await loop.run(max_steps=10)
    assert state.status == "completed"
    events = read_events(loop)
    verifications = [e for e in events if e.type == EventType.VERIFICATION_RESULT]
    assert all(e.verification_result["passed"] for e in verifications if e.verification_result)


async def test_wrong_navigation_is_caught_by_verifier(make_agent_loop, fixture_site_url):
    loop = make_agent_loop("Continue on the wrong-nav page", [], [
        decision("open_url", params={"url": fixture_site_url + "/wrongnav.html"}),
        decision("click", target=1, expected_result={"url_contains": "wrongnav_next.html"}),
        decision("finish", params={"result": "done"}),
    ])
    state = await loop.run(max_steps=10)
    assert state.status == "completed"
    events = read_events(loop)
    verifications = [e for e in events if e.type == EventType.VERIFICATION_RESULT]
    failed = [e for e in verifications if e.verification_result and not e.verification_result["passed"]]
    assert len(failed) == 1
    assert state.recovery_level != RecoveryLevel.NORMAL.value


async def test_navigation_loop_is_detected(make_agent_loop, fixture_site_url):
    loop = make_agent_loop("Bounce between the loop pages", [], [
        decision("open_url", params={"url": fixture_site_url + "/loop_a.html"}),
        decision("click", target=1),  # a -> b
        decision("click", target=1),  # b -> a
        decision("click", target=1),  # a -> b
        decision("click", target=1),  # b -> a
        decision("finish", params={"result": "done"}),
    ])
    state = await loop.run(max_steps=10)
    assert state.status == "completed"
    transitions = [e for e in read_events(loop) if e.type == EventType.RECOVERY_TRANSITION]
    assert any(t.payload["reason"] == "loop_detected" for t in transitions)


async def test_consequential_action_is_not_auto_retried_after_failure(make_agent_loop, fixture_site_url):
    loop = make_agent_loop("Submit the application", [], [
        decision("open_url", params={"url": fixture_site_url + "/wizard_confirm.html"}),
        decision("click", target=1, expected_result={"page_contains": "confirmation received"}),  # will fail
        decision("click", target=1, expected_result={"page_contains": "confirmation received"}),  # must be blocked, not re-executed
    ])
    state = await loop.run(max_steps=10)
    assert state.status == "blocked"
    assert "consequential" in (state.blocked_reason or "").lower()

    events = read_events(loop)
    action_results = [e for e in events if e.type == EventType.ACTION_RESULT
                       and e.payload.get("action_fingerprint") == "click:1:{}"]
    assert len(action_results) == 1  # the second attempt never reached execution
