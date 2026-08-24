"""Deterministic verification: expected_result vs. actual PageObservation.

This is ordinary code, not a second LLM call — ARCHITECTURE.md's central verification
principle is that the model never gets to decide whether its own action worked. Every
check here is a plain string/substring comparison against the freshly re-observed page.
"""
from __future__ import annotations

from typing import Any

from agent.schemas import ActionType, CheckResult, ExpectedResult, ModelDecision, VerificationResult
from browser.page_model import PageObservation


def _page_text_blob(obs: PageObservation) -> str:
    parts = [el.name for el in obs.elements] + obs.visible_text
    return "\n".join(parts).lower()


def check(expected: ExpectedResult, observation: PageObservation) -> VerificationResult:
    checks: list[CheckResult] = []

    if expected.url_contains is not None:
        actual = observation.url
        checks.append(CheckResult(
            type="url_contains", expected=expected.url_contains, actual=actual,
            passed=expected.url_contains.lower() in actual.lower(),
        ))

    if expected.title_contains is not None:
        actual = observation.title
        checks.append(CheckResult(
            type="title_contains", expected=expected.title_contains, actual=actual,
            passed=expected.title_contains.lower() in actual.lower(),
        ))

    if expected.page_contains is not None:
        blob = _page_text_blob(observation)
        checks.append(CheckResult(
            type="page_contains", expected=expected.page_contains, actual=blob[:200],
            passed=expected.page_contains.lower() in blob,
        ))

    if expected.element_present is not None:
        blob = _page_text_blob(observation)
        checks.append(CheckResult(
            type="element_present", expected=expected.element_present, actual=blob[:200],
            passed=expected.element_present.lower() in blob,
        ))

    if expected.element_absent is not None:
        blob = _page_text_blob(observation)
        present = expected.element_absent.lower() in blob
        checks.append(CheckResult(
            type="element_absent", expected=expected.element_absent, actual=blob[:200],
            passed=not present,
        ))

    if not checks:
        # No assertions given (typical for read-only actions like scroll/extract/wait) —
        # trivially passes but callers can see `checks == []` to know nothing was actually asserted.
        return VerificationResult(passed=True, checks=[])

    return VerificationResult(passed=all(c.passed for c in checks), checks=checks)


def check_action_default(
    decision: ModelDecision,
    before: PageObservation,
    after: PageObservation,
    result_data: dict[str, Any] | None = None,
    error: str | None = None,
) -> VerificationResult:
    """Action-derived verification used when the model did not provide assertions."""
    checks: list[CheckResult] = []
    result_data = result_data or {}

    if error:
        checks.append(CheckResult(
            type="execution_error", expected="no execution error", actual=error, passed=False,
        ))
        return VerificationResult(passed=False, checks=checks)

    if decision.action == ActionType.OPEN_URL:
        requested = decision.params.get("url", "")
        actual = after.url
        if isinstance(requested, str) and requested.startswith("/"):
            passed = requested in actual
        else:
            passed = actual.startswith(requested) if isinstance(requested, str) else False
        checks.append(CheckResult(type="open_url", expected=str(requested), actual=actual, passed=passed))

    elif decision.action == ActionType.TYPE:
        element = after.element_by_id(decision.target) if decision.target is not None else None
        actual = element.value if element is not None else None
        expected = decision.params.get("text")
        checks.append(CheckResult(
            type="type_value", expected=str(expected), actual=str(actual), passed=actual == expected,
        ))

    elif decision.action == ActionType.SELECT:
        element = after.element_by_id(decision.target) if decision.target is not None else None
        actual = element.value if element is not None else None
        expected = decision.params.get("value")
        checks.append(CheckResult(
            type="select_value", expected=str(expected), actual=str(actual), passed=actual == expected,
        ))

    elif decision.action == ActionType.CLICK:
        changed = before.state_hash != after.state_hash or before.url != after.url
        checks.append(CheckResult(
            type="meaningful_state_change",
            expected="page state hash or URL changes",
            actual=f"before={before.state_hash} after={after.state_hash} before_url={before.url} after_url={after.url}",
            passed=changed,
        ))

    elif decision.action == ActionType.DOWNLOAD:
        actual = result_data.get("suggested_filename") or result_data.get("path")
        checks.append(CheckResult(
            type="download_event", expected="download event/file", actual=str(actual), passed=bool(actual),
        ))

    elif decision.action == ActionType.BACK:
        changed = before.url != after.url
        checks.append(CheckResult(
            type="back_navigation", expected="URL changes", actual=f"{before.url} -> {after.url}", passed=changed,
        ))

    if not checks:
        return VerificationResult(passed=True, checks=[])
    return VerificationResult(passed=all(c.passed for c in checks), checks=checks)


def check_hybrid(
    decision: ModelDecision,
    before: PageObservation,
    after: PageObservation,
    result_data: dict[str, Any] | None = None,
    error: str | None = None,
) -> VerificationResult:
    if decision.verification_mode == "legacy" and decision.expected_result.is_empty():
        return check(decision.expected_result, after)
    if not decision.expected_result.is_empty():
        if error:
            return VerificationResult(passed=False, checks=[CheckResult(
                type="execution_error", expected="no execution error", actual=error, passed=False,
            )])
        return check(decision.expected_result, after)
    return check_action_default(decision, before, after, result_data, error)
