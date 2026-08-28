"""cdp_attach tab-selection unit tests (no real Playwright/Chrome needed — _select_page and
_select_preferred_page only ever touch browser.contexts / context.pages / page.url, so plain
duck-typed fakes exercise the exact same logic).

Regression coverage for the real 3-tab open-tab sweep failure (2026-08-27): a batch child
attached to whatever tab was "most recently active" rather than the specific tab its own
work item's target actually was, causing a same-origin scope block when that tab turned out
to belong to a *different* work item's target."""
from __future__ import annotations

from pathlib import Path

from browser.playwright_backend import PlaywrightBackend, _normalize_url_for_match


class FakePage:
    def __init__(self, url: str):
        self.url = url


class FakeContext:
    def __init__(self, pages):
        self.pages = pages


class FakeBrowser:
    def __init__(self, contexts):
        self.contexts = contexts


def _backend(preferred_tab_url=None, explicit_target_url=None) -> PlaywrightBackend:
    return PlaywrightBackend(
        Path("/tmp/profile"), headless=True, action_timeout_ms=1000,
        max_page_chars=1000, max_visible_text_items=10,
        mode="cdp_attach", cdp_endpoint="http://127.0.0.1:9222",
        preferred_tab_url=preferred_tab_url,
        explicit_target_url=explicit_target_url,
    )


def test_normalize_url_for_match_ignores_trailing_slash_and_case():
    assert _normalize_url_for_match("https://Example.com/Path/") == _normalize_url_for_match("https://example.com/Path")


def test_select_preferred_page_attaches_to_exact_matching_tab():
    iana = FakePage("https://www.iana.org/")
    python = FakePage("https://www.python.org/")
    example = FakePage("https://example.com/")
    browser = FakeBrowser([FakeContext([iana, python, example])])

    backend = _backend(preferred_tab_url="https://example.com/")
    selected = backend._select_preferred_page(browser)
    assert selected is example


def test_select_preferred_page_does_not_pick_wrong_tab_even_if_last_active():
    """This is the exact shape of the real bug: the previous work item's own navigation left
    python.org as the 'most recently active' tab in context.pages order, but this work
    item's target is example.com — the default heuristic alone would have picked python.org."""
    iana = FakePage("https://www.iana.org/")
    example = FakePage("https://example.com/")
    python = FakePage("https://www.python.org/")  # last in list => "most recently active"
    browser = FakeBrowser([FakeContext([iana, example, python])])

    backend = _backend(preferred_tab_url="https://example.com/")
    selected = backend._select_preferred_page(browser)
    assert selected is example
    assert selected is not python


def test_select_preferred_page_returns_none_when_unset():
    example = FakePage("https://example.com/")
    browser = FakeBrowser([FakeContext([example])])
    backend = _backend(preferred_tab_url=None)
    assert backend._select_preferred_page(browser) is None


def test_select_preferred_page_falls_back_to_none_when_tab_closed():
    """The tab may have been closed by the user between resolution and this child starting —
    a normal, recoverable case, not a hard failure: the caller falls through to the default
    'most recently active' heuristic rather than erroring."""
    other = FakePage("https://other.example/")
    browser = FakeBrowser([FakeContext([other])])
    backend = _backend(preferred_tab_url="https://example.com/")
    assert backend._select_preferred_page(browser) is None


def test_select_page_by_url_finds_exact_match_even_when_not_most_recently_active():
    """Regression for the stale-tab-reuse bug: a fresh explicit-URL task's target must be
    matched by URL, not by 'last in context.pages order' (the most-recently-active proxy)."""
    stale = FakePage("https://goodhousekeeping.com/")  # last => "most recently active"
    target = FakePage("https://example.com/")
    browser = FakeBrowser([FakeContext([target, stale])])

    backend = _backend(explicit_target_url="https://example.com/")
    selected = backend._select_page_by_url(browser, backend.explicit_target_url)
    assert selected is target
    assert selected is not stale


def test_select_page_by_url_returns_none_when_no_tab_matches():
    """No open tab matches the explicit target -> None, so the caller creates a fresh blank
    page rather than falling back to an unrelated stale tab."""
    stale = FakePage("https://goodhousekeeping.com/")
    browser = FakeBrowser([FakeContext([stale])])

    backend = _backend(explicit_target_url="https://example.com/")
    assert backend._select_page_by_url(browser, backend.explicit_target_url) is None
