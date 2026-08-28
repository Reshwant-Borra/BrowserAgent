"""cdp_attach lifecycle mode: BrowserAgent attaching to an already-running Chromium instead
of launching its own (Section 6-13 of the master status task). The "user's persistent
browser" is simulated with its own headless `chromium.launch(args=["--remote-debugging-port=..."])`
— a separate Playwright connection from the one PlaywrightBackend makes — so these tests
exercise the same attach/detach path a real user's manually-started Chrome would.

NOTE: on this machine, running Playwright-backed tests through pytest (this file included)
has a documented stall (see docs/BROWSERAGENT_MASTER_STATUS.md, Section 15/19 "Known
Limitations") that predates this change. The scenarios below were validated by direct
script execution (not pytest) before being committed here; run standalone with
`python -m pytest tests/integration/test_cdp_attach.py -x` if pytest itself doesn't stall
in your environment, otherwise treat this file as the durable/reviewable spec for behavior
already verified out-of-band.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from playwright.async_api import async_playwright

from browser.playwright_backend import BrowserAttachError, PlaywrightBackend

pytestmark = pytest.mark.asyncio

CDP_PORT = 9334


@pytest.fixture
async def user_browser():
    """Simulates a user's manually-started persistent Chromium with remote debugging on."""
    pw = await async_playwright().start()
    browser = await pw.chromium.launch(headless=True, args=[f"--remote-debugging-port={CDP_PORT}"])
    yield browser
    if browser.is_connected():
        await browser.close()
    await pw.stop()


def _make_backend(cdp_endpoint: str = f"http://127.0.0.1:{CDP_PORT}",
                   explicit_target_url: str | None = None) -> PlaywrightBackend:
    return PlaywrightBackend(Path("./_unused"), True, 5000, 3000, 12,
                              mode="cdp_attach", cdp_endpoint=cdp_endpoint,
                              explicit_target_url=explicit_target_url)


async def test_attach_to_unavailable_endpoint_raises_clear_error():
    backend = _make_backend(cdp_endpoint="http://127.0.0.1:1")  # reserved/unused port
    with pytest.raises(BrowserAttachError, match="remote-debugging-port"):
        await backend.start()


async def test_attach_reuses_existing_non_blank_tab(user_browser, fixture_site_url):
    context = user_browser.contexts[0] if user_browser.contexts else await user_browser.new_context()
    page = context.pages[0] if context.pages else await context.new_page()
    await page.goto(f"{fixture_site_url}/workflow_site_a.html")

    backend = _make_backend()
    await backend.start()
    try:
        assert backend.page.url.endswith("workflow_site_a.html")
        obs = await backend.observe()
        assert obs.title or obs.visible_text
    finally:
        await backend.close()


async def test_close_disconnects_without_closing_users_browser(user_browser, fixture_site_url):
    context = user_browser.contexts[0] if user_browser.contexts else await user_browser.new_context()
    page = context.pages[0] if context.pages else await context.new_page()
    await page.goto(f"{fixture_site_url}/workflow_site_a.html")

    backend = _make_backend()
    await backend.start()
    await backend.close()

    assert user_browser.is_connected()
    assert not page.is_closed()
    assert page.url.endswith("workflow_site_a.html")


async def test_reconnect_finds_same_tab_and_can_read_again(user_browser, fixture_site_url):
    context = user_browser.contexts[0] if user_browser.contexts else await user_browser.new_context()
    page = context.pages[0] if context.pages else await context.new_page()
    await page.goto(f"{fixture_site_url}/workflow_site_a.html")

    first = _make_backend()
    await first.start()
    await first.close()

    second = _make_backend()
    await second.start()
    try:
        assert second.page.url.endswith("workflow_site_a.html")
        obs = await second.observe()
        assert obs.title or obs.visible_text
    finally:
        await second.close()


async def test_empty_browser_creates_one_page_without_launching_new_browser(user_browser):
    context = user_browser.contexts[0] if user_browser.contexts else await user_browser.new_context()
    for p in list(context.pages):
        await p.close()

    backend = _make_backend()
    await backend.start()
    try:
        assert backend.page is not None
        assert not backend.page.is_closed()
    finally:
        await backend.close()
    assert user_browser.is_connected()  # still the same browser process, never a second one


async def test_current_page_task_reuses_most_recently_active_tab(user_browser, fixture_site_url):
    """No explicit_target_url (a 'tell me what this page is about' current-page task) ->
    the existing 'most recently active tab' heuristic is the intended, unchanged behavior."""
    context = user_browser.contexts[0] if user_browser.contexts else await user_browser.new_context()
    page = context.pages[0] if context.pages else await context.new_page()
    await page.goto(f"{fixture_site_url}/workflow_site_a.html")

    backend = _make_backend()
    await backend.start()
    try:
        assert backend.page.url.endswith("workflow_site_a.html")
    finally:
        await backend.close()


async def test_explicit_target_url_reuses_matching_existing_tab(user_browser, fixture_site_url):
    """A fresh single-site task whose resolved target already matches an open tab reuses it,
    even when a different, unrelated tab is more 'recently active'."""
    context = user_browser.contexts[0] if user_browser.contexts else await user_browser.new_context()
    stale = context.pages[0] if context.pages else await context.new_page()
    await stale.goto(f"{fixture_site_url}/workflow_site_b.html")
    matching = await context.new_page()
    await matching.goto(f"{fixture_site_url}/workflow_site_a.html")

    backend = _make_backend(explicit_target_url=f"{fixture_site_url}/workflow_site_a.html")
    await backend.start()
    try:
        assert backend.page.url.endswith("workflow_site_a.html")
    finally:
        await backend.close()


async def test_explicit_target_url_never_reuses_stale_unrelated_tab(user_browser, fixture_site_url):
    """The core stale-tab-reuse bug: a brand-new task with an explicit target ('Open
    https://example.com') must never silently attach to and answer from an unrelated tab a
    previous task left open — even though that stale tab is the 'most recently active' one
    the old default heuristic would have picked. No tab matches the target here, so the
    backend must land on a fresh blank page instead, ready for the model's own open_url step."""
    context = user_browser.contexts[0] if user_browser.contexts else await user_browser.new_context()
    stale = context.pages[0] if context.pages else await context.new_page()
    await stale.goto(f"{fixture_site_url}/workflow_site_b.html")  # e.g. a stale goodhousekeeping.com tab

    backend = _make_backend(explicit_target_url=f"{fixture_site_url}/workflow_site_a.html")
    await backend.start()
    try:
        assert not backend.page.url.endswith("workflow_site_b.html")
        assert backend.page.url in ("about:blank", "")
    finally:
        await backend.close()


async def test_explicit_target_url_reconnect_finds_same_tab(user_browser, fixture_site_url):
    """Reconnect regression: a resumed job's explicit_target_url (its last-known current_url)
    must reattach to that same tab across a disconnect/reconnect cycle, not whatever else is
    open at reconnect time."""
    context = user_browser.contexts[0] if user_browser.contexts else await user_browser.new_context()
    target_page = context.pages[0] if context.pages else await context.new_page()
    await target_page.goto(f"{fixture_site_url}/workflow_site_a.html")
    other = await context.new_page()
    await other.goto(f"{fixture_site_url}/workflow_site_b.html")

    target_url = f"{fixture_site_url}/workflow_site_a.html"
    first = _make_backend(explicit_target_url=target_url)
    await first.start()
    await first.close()

    second = _make_backend(explicit_target_url=target_url)
    await second.start()
    try:
        assert second.page.url.endswith("workflow_site_a.html")
    finally:
        await second.close()


async def test_launch_mode_still_works_unchanged(tmp_path, fixture_site_url):
    """Regression guard: default mode ("launch") must behave exactly as before — this is what
    tests/fixtures/benchmarks rely on, and Section 7 requires it stay the default."""
    backend = PlaywrightBackend(tmp_path / "profile", True, 5000, 3000, 12)  # mode defaults to "launch"
    assert backend.mode == "launch"
    await backend.start()
    try:
        await backend.open_url(f"{fixture_site_url}/workflow_site_a.html")
        obs = await backend.observe()
        assert obs.title or obs.visible_text
    finally:
        await backend.close()
