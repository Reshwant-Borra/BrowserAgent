"""Deterministic verification: expected_result vs. actual PageObservation.

This is ordinary code, not a second LLM call — ARCHITECTURE.md's central verification
principle is that the model never gets to decide whether its own action worked. Every
check here is a plain string/substring comparison against the freshly re-observed page.
"""
from __future__ import annotations

from agent.schemas import CheckResult, ExpectedResult, VerificationResult
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
