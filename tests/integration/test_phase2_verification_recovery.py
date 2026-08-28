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


async def test_repeated_readonly_action_with_no_state_change_triggers_recovery_escalation(
    make_agent_loop, fixture_site_url,
):
    """Phase 5B corrective pass: a fact-finding workflow step observed live re-opening the
    same URL (or re-extracting the same element) many times in a row without ever reaching
    `finish`, because open_url/extract have no `expected_result` to fail verification against
    — `noop` (which requires an *expected* change) never fires, so the repeated-action guard
    stayed silent. `open_url` to a page whose content never changes must now be caught by the
    same repeated-action loop guard even though each individual open_url trivially "passes"."""
    url = fixture_site_url + "/workflow_multi_fact_a.html"
    loop = make_agent_loop("Find the build filename on this page", [], [
        decision("open_url", params={"url": url}),
        decision("open_url", params={"url": url}),
        decision("open_url", params={"url": url}),
        decision("finish", params={"result": "report-v3.zip"}),
    ])
    state = await loop.run(max_steps=10)
    assert state.status == "completed"
    events = read_events(loop)
    transitions = [e for e in events if e.type == EventType.RECOVERY_TRANSITION]
    assert any(t.payload["reason"] == "loop_detected" for t in transitions)


async def test_repeated_readonly_action_with_changing_hash_still_triggers_recovery_escalation(
    make_agent_loop, fixture_site_url,
):
    """Root cause reconstructed from a real Amazon "find the 3 best vacuum cleaners" run
    (runtime/tasks/bf2ea8beb1c0): the page above's fix (test_repeated_readonly_action_with_
    no_state_change_triggers_recovery_escalation) only covers a page whose content never
    changes. Amazon's best-sellers page re-renders different carousel/ad content on every
    load of the *identical* URL, so pre_hash != post_hash on every single open_url — the old
    `stalled_readonly_repeat = action in ELIGIBLE and pre_hash == post_hash` gate never fired,
    and the live task spent 14 consecutive steps re-opening the same URL before exhausting its
    step budget. dynamic_listing.html reproduces that: it renders a new random value into the
    DOM on every load via client-side JS, so its hash changes each time even though the agent
    is making zero real progress."""
    url = fixture_site_url + "/dynamic_listing.html"
    loop = make_agent_loop("Find the build filename on this ever-changing page", [], [
        decision("open_url", params={"url": url}),
        decision("open_url", params={"url": url}),
        decision("open_url", params={"url": url}),
        decision("finish", params={"result": "report-v3.zip"}),
    ])
    state = await loop.run(max_steps=10)
    assert state.status == "completed"
    events = read_events(loop)
    transitions = [e for e in events if e.type == EventType.RECOVERY_TRANSITION]
    assert any(t.payload["reason"] == "loop_detected" for t in transitions)


async def test_repeated_wait_with_no_state_change_does_not_trigger_loop_escalation(
    make_agent_loop, fixture_site_url,
):
    """The fix above is scoped to open_url/extract only — `wait` is legitimately repeated
    while polling for a page to change (see test_phase4_long_horizon.py's long-horizon
    scenario) and must not be swept into the same loop guard."""
    url = fixture_site_url + "/workflow_multi_fact_a.html"
    loop = make_agent_loop("Wait a bit then finish", [], [
        decision("open_url", params={"url": url}),
        decision("wait", params={"ms": 1}),
        decision("wait", params={"ms": 1}),
        decision("wait", params={"ms": 1}),
        decision("wait", params={"ms": 1}),
        decision("finish", params={"result": "done waiting"}),
    ])
    state = await loop.run(max_steps=10)
    assert state.status == "completed"
    events = read_events(loop)
    transitions = [e for e in events if e.type == EventType.RECOVERY_TRANSITION]
    assert not any(t.payload["reason"] == "loop_detected" for t in transitions)


async def test_finish_with_evidence_backed_findings_completes_with_no_prior_verified_action(
    make_agent_loop_at_url, fixture_site_url,
):
    """Root cause reconstructed from a real Amazon "find the 3 best vacuum cleaners" run
    (runtime/tasks/bf2ea8beb1c0): a batch work item's child task starts already on its target
    page (see make_agent_loop_at_url), so its first model decision need not be a navigating
    open_url. When that first decision was instead `finish` with a `structured_result`
    containing real, evidence-backed findings, `_handle_finish`'s fallback gate — "no
    success_criteria (always true for batch/research children) and no prior action verified
    pass" — rejected it outright without ever looking at the structured_result, discarding 7
    correctly extracted prices and forcing the task into a replan/recovery spiral that never
    recovered (it degraded into 14 consecutive same-URL open_url calls before exhausting its
    step budget). The fix: evidence-backed structured_result counts as completion evidence in
    its own right, same as a passing action would."""
    url = fixture_site_url + "/workflow_multi_fact_a.html"
    loop = make_agent_loop_at_url("Find the build filename on this page", [], [
        decision("finish", params={
            "result": "found the build filename",
            "structured_result": {"findings": [{
                "field": "item_name", "value": "report-v3.zip",
                "source_url": url, "evidence": "Build filename: report-v3.zip",
            }]},
        }),
    ], explicit_target_url=url)
    state = await loop.run(max_steps=10)
    assert state.status == "completed"
    events = read_events(loop)
    assert not any(
        e.type == EventType.MODEL_DECISION and e.payload.get("error") == "model_completion_error"
        for e in events
    )


async def test_finish_with_empty_structured_result_and_no_prior_action_is_still_rejected(
    make_agent_loop_at_url, fixture_site_url,
):
    """Negative control for the fix above: a `finish` with no success_criteria, no prior
    verified action, and no evidence-backed structured_result must still be rejected — the
    evidence bypass only accepts genuine findings, not an empty/absent structured_result."""
    url = fixture_site_url + "/workflow_multi_fact_a.html"
    loop = make_agent_loop_at_url("Find the build filename on this page", [], [
        decision("finish", params={"result": "I looked but found nothing"}),
        decision("extract", params={}),
        decision("finish", params={"result": "done", "structured_result": {"findings": [
            {"field": "item_name", "value": "report-v3.zip", "source_url": url,
             "evidence": "Build filename: report-v3.zip"},
        ]}}),
    ], explicit_target_url=url)
    state = await loop.run(max_steps=10)
    events = read_events(loop)
    decision_errors = [
        e for e in events
        if e.type == EventType.MODEL_DECISION and e.payload.get("error") == "model_completion_error"
    ]
    assert decision_errors, "the unevidenced finish should have been rejected at least once"
    assert state.status == "completed"


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
