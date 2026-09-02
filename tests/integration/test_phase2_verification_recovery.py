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
        # RC-1 (docs/BROWSERAGENT_MASTER_STATUS.md's FINAL ACCEPTANCE section): finish no
        # longer accepts an unrelated earlier structural pass as completion evidence, so the
        # declared outcome now needs its own evidence — a fresh passing action would instead
        # reset recovery_level to NORMAL (next_recovery_level's own reset rule), defeating
        # this test's actual point, so the finish carries its own structured_result findings.
        decision("finish", params={
            "result": "confirmed no-op",
            "structured_result": {"findings": [{
                "field": "outcome", "value": "no-op confirmed",
                "evidence": "Do Nothing button still present; no page state change occurred",
            }]},
        }),
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
    # The click's own default "meaningful_state_change" check (RC-1's test-harness realism
    # fix: verification_mode now defaults to "action_default", matching real model output)
    # correctly reports no *immediate* DOM change — the button's effect is a delayed
    # setTimeout navigation, not a synchronous one — so it need not pass on its own; what
    # actually matters is that the explicit `wait` step's own real assertion did.
    wait_checks = [
        c for e in verifications if e.verification_result
        for c in e.verification_result["checks"] if c["type"] == "url_contains"
    ]
    assert wait_checks and all(c["passed"] for c in wait_checks)


async def test_wrong_navigation_is_caught_by_verifier(make_agent_loop, fixture_site_url):
    loop = make_agent_loop("Continue on the wrong-nav page", [], [
        decision("open_url", params={"url": fixture_site_url + "/wrongnav.html"}),
        decision("click", target=1, expected_result={"url_contains": "wrongnav_next.html"}),
        # See RC-1 note above: finish needs its own evidence now, not a resettable fresh pass.
        decision("finish", params={
            "result": "done",
            "structured_result": {"findings": [{
                "field": "outcome", "value": "landed on unexpected page",
                "evidence": "This is not where you expected to land",
            }]},
        }),
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
        # See RC-1 note above: finish needs its own evidence now, not a resettable fresh pass.
        decision("finish", params={
            "result": "done",
            "structured_result": {"findings": [{
                "field": "outcome", "value": "loop between pages A and B",
                "evidence": "Loop Page A / Loop Page B link back and forth to each other",
            }]},
        }),
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
        # See RC-1 note above: finish needs its own evidence now, not a resettable fresh pass.
        decision("finish", params={
            "result": "report-v3.zip",
            "structured_result": {"findings": [{
                "field": "build_filename", "value": "report-v3.zip",
                "evidence": "Build filename: report-v3.zip",
            }]},
        }),
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
        # See RC-1 note above: finish needs its own evidence now, not a resettable fresh pass.
        decision("finish", params={
            "result": "report-v3.zip",
            "structured_result": {"findings": [{
                "field": "build_filename", "value": "report-v3.zip",
                "evidence": "Build filename: report-v3.zip",
            }]},
        }),
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
        # See RC-1 note above: finish needs its own evidence now, not a resettable fresh pass.
        decision("finish", params={
            "result": "done waiting",
            "structured_result": {"findings": [{
                "field": "build_filename", "value": "report-v3.zip",
                "evidence": "Build filename: report-v3.zip",
            }]},
        }),
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


async def test_finish_after_only_weak_verification_is_rejected(make_agent_loop, fixture_site_url):
    """Acceptance-test finding (docs/BROWSERAGENT_MASTER_STATUS.md's FINAL ACCEPTANCE
    section, RC-1): a `click` with no expected_result only gets `check_action_default`'s
    weak "meaningful_state_change" check (did the page change at all, regardless of whether
    the change was good or bad) — this used to be enough, on its own, to satisfy `finish`'s
    "no success_criteria, but *some* prior action verified pass" fallback. Two independent
    live acceptance tasks exploited exactly this: the model clicked a button whose only
    effect was to render an on-page error message, and `finish` was accepted anyway because
    that click had structurally "passed". noop.html's button changes nothing structurally
    (see test_noop_button_triggers_recovery_escalation above), so it doesn't reproduce this;
    this uses wizard_confirm.html's Submit button, which does change the page (adds visible
    status text) without the model ever stating what a *correct* outcome should contain."""
    loop = make_agent_loop("Submit the application and confirm it went through", [], [
        decision("open_url", params={"url": fixture_site_url + "/wizard_confirm.html"}),
        decision("click", target=1),  # no expected_result -> weak "meaningful_state_change" only
        decision("finish", params={"result": "submitted successfully"}),  # must be rejected
        decision("extract", params={}),
        decision("finish", params={
            "result": "submitted successfully",
            "structured_result": {"findings": [{
                "field": "outcome", "value": "submitted",
                "evidence": "Submitted 1 time(s)",
            }]},
        }),
    ])
    state = await loop.run(max_steps=10)
    events = read_events(loop)
    decision_errors = [
        e for e in events
        if e.type == EventType.MODEL_DECISION and e.payload.get("error") == "model_completion_error"
    ]
    assert decision_errors, "finish after only a weak (structural-only) verification must be rejected"
    assert state.status == "completed"


async def test_finish_after_weak_last_action_is_rejected_even_with_earlier_strong_pass(
    make_agent_loop, fixture_site_url,
):
    """Negative control distinguishing RC-1's actual fix (check the LAST action, and require
    it to be a genuine assertion) from a weaker fix that would have merely required *some*
    strong pass anywhere in history. The task's own first action (open_url) is a real,
    strong-checked pass — under the old `any(...)` gate this alone was enough to wave through
    a `finish` issued after a later, purely-structural click, exactly reproducing the live
    A4/A7 shape (type/open_url succeed, then a click that changes the page to show an error,
    then an unwarranted finish)."""
    loop = make_agent_loop("Submit the application and confirm it went through", [], [
        decision("open_url", params={"url": fixture_site_url + "/wizard_confirm.html"}),  # strong pass
        decision("click", target=1),  # weak pass — this is the LAST action before finish
        decision("finish", params={"result": "submitted successfully"}),  # must be rejected
        decision("extract", params={}),
        decision("finish", params={
            "result": "submitted successfully",
            "structured_result": {"findings": [{
                "field": "outcome", "value": "submitted",
                "evidence": "Submitted 1 time(s)",
            }]},
        }),
    ])
    state = await loop.run(max_steps=10)
    events = read_events(loop)
    decision_errors = [
        e for e in events
        if e.type == EventType.MODEL_DECISION and e.payload.get("error") == "model_completion_error"
    ]
    assert decision_errors, (
        "an earlier strong pass must not excuse a finish issued right after a later, "
        "purely structural (weak) verification"
    )
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
