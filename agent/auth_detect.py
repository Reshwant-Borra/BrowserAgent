"""Shared login/auth detection heuristic.

Section 49-50 of the Phase 5B spec: BrowserAgent must never automate credential entry, but
it should recognize when it has hit a login wall and hand off to the user rather than
burning the recovery ladder (retry/refresh/deep-recovery/replan) on a page no amount of
retrying will get past. This heuristic was previously only applied post-hoc, over a
finished/blocked child task's event log, in `batch/policies.py:classify_child_failure`.
`agent/loop.py`'s `step()` now also calls it live, right after each observation, so a
single-task or batch/workflow run can short-circuit straight to a `login_required` block
instead of exhausting retries first.
"""
from __future__ import annotations

from browser.page_model import PageObservation

AUTH_KEYWORDS = ("login", "log in", "sign in", "signin", "authentication")


def blob_indicates_auth_required(text: str) -> bool:
    lowered = text.lower()
    return any(keyword in lowered for keyword in AUTH_KEYWORDS)


def looks_like_login_page(observation: PageObservation) -> bool:
    """Live, in-loop check against a fresh observation. Conservative on purpose: a page
    merely mentioning "sign in" in a nav link (e.g. a marketing homepage) is not enough by
    itself — this also requires either a password-type input or the login keyword appearing
    in the page title/heading text, not just anywhere in the visible text."""
    has_password_field = any(el.sensitive for el in observation.elements)
    title_hit = blob_indicates_auth_required(observation.title)
    heading_hit = any(
        blob_indicates_auth_required(text) for text in observation.visible_text[:5]
    )
    return has_password_field or title_hit or heading_hit
