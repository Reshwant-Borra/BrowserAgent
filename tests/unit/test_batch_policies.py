from __future__ import annotations

from memory.event_store import Event, EventType

from batch.models import FailureCategory
from batch.policies import classify_child_failure


def _event(type_: EventType, payload: dict, step: int = 1) -> Event:
    return Event(task_id="t1", step=step, timestamp="2026-01-01T00:00:00+00:00", type=type_, payload=payload)


def test_scope_blocked_not_misclassified_as_auth_required():
    """Regression for the real 3-tab sweep failure (2026-08-27): the Example item was
    blocked by navigation_scope (attached to the wrong tab, which had python.org loaded
    with a page mentioning 'Sign In' in its nav), but the UI reported AUTH_REQUIRED. The
    blob-wide auth-keyword heuristic in classify_child_failure matched an unrelated page's
    OBSERVATION text before the explicit, structural SCOPE_BLOCKED category (already
    recorded verbatim on the TASK_BLOCKED event by agent/loop.py's _block_by_runtime_policy)
    ever got a chance to win."""
    events = [
        _event(EventType.OBSERVATION, {
            "url": "https://www.python.org/",
            "title": "Welcome to Python.org",
            "page_hash": "h1",
            "visible_text": ["Notice: While JavaScript is not essential...", "Sign In to PyPI", "Docs"],
        }, step=1),
        _event(EventType.TASK_BLOCKED, {
            "reason": "batch navigation_scope=same_origin blocked navigation to https://www.python.org/",
            "failure_category": "SCOPE_BLOCKED",
            "runtime_policy_block": True,
        }, step=2),
    ]
    category = classify_child_failure(events, "blocked", "batch navigation_scope=same_origin blocked navigation to https://www.python.org/")
    assert category == FailureCategory.SCOPE_BLOCKED


def test_read_only_blocked_not_misclassified_as_auth_required():
    events = [
        _event(EventType.OBSERVATION, {
            "url": "https://example.test/account",
            "title": "Account Sign In",
            "page_hash": "h1",
            "visible_text": ["Please Log In to continue"],
        }, step=1),
        _event(EventType.TASK_BLOCKED, {
            "reason": "batch read_only policy blocked consequential action click on 'Submit'",
            "failure_category": "READ_ONLY_BLOCKED",
            "runtime_policy_block": True,
        }, step=2),
    ]
    category = classify_child_failure(events, "blocked", "batch read_only policy blocked consequential action click on 'Submit'")
    assert category == FailureCategory.READ_ONLY_BLOCKED


def test_step_budget_exhaustion_on_page_with_signin_nav_link_not_misclassified_as_auth_required():
    """Regression for the real Amazon "find the 3 best vacuum cleaners" run
    (runtime/tasks/bf2ea8beb1c0, batch ae5045ccc976, work item 1): the child task ran out of
    its step budget (status stays "running", no TASK_BLOCKED event at all — there was no auth
    wall, just a completion-gate bug forcing a replan/recovery spiral) but the batch orchestrator
    reported failure_category=AUTH_REQUIRED anyway. The old blob-wide scan matched "sign in"
    inside an OBSERVATION's `element_names` (Amazon's persistent "Hello, sign in Account &
    Lists" nav item, present on literally every Amazon page) even though it never appeared in
    the title or the top-of-page heading text. With no TASK_BLOCKED/explicit failure_category
    and no auth-indicating last_error, this must fall through to MAX_STEPS."""
    events = [
        _event(EventType.OBSERVATION, {
            "url": "https://www.amazon.com/Best-Sellers-Vacuum-Cleaners-Floor-Care/zgbs/home-garden/510106",
            "title": "Amazon Best Sellers: Best Vacuum Cleaners & Floor Care",
            "page_hash": "h1",
            "element_names": ["main content", "Search, alt, forward slash", "Cart, shift, alt, c",
                               "Hello, sign in Account & Lists", "Returns & Orders"],
            "visible_text": ["Skip to", "Main content", "Keyboard shortcuts", "Search alt + /",
                              "Cart shift + alt + C", "Delivering to Tampa 33647 Update location"],
        }, step=1),
    ]
    category = classify_child_failure(events, "running")
    assert category == FailureCategory.MAX_STEPS


def test_genuine_login_wall_still_classified_as_auth_required():
    """A real login wall (agent/loop.py's live looks_like_login_page short-circuit) never
    sets an explicit failure_category on its TASK_BLOCKED event — the heuristic fallback
    must still catch it."""
    events = [
        _event(EventType.OBSERVATION, {
            "url": "https://example.test/login",
            "title": "Sign In",
            "page_hash": "h1",
            "visible_text": ["Please sign in to continue"],
        }, step=1),
        _event(EventType.TASK_BLOCKED, {"reason": "login_required", "url": "https://example.test/login"}, step=2),
    ]
    category = classify_child_failure(events, "blocked", "login_required")
    assert category == FailureCategory.AUTH_REQUIRED
