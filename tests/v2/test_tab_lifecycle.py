"""Tab ownership across the whole attach->work->detach lifecycle.

The user's persistent browser is simulated the same way `tests/integration/test_cdp_attach.py`
does it — a separate headless Chromium launched with `--remote-debugging-port`, connected to
over a *different* Playwright connection than the one PlaywrightBackend makes — so these
exercise the real attach path rather than a stand-in.

The invariant under test, in both directions:

- a tab that existed before the task started is the user's and is never closed automatically;
- a tab that exists because of the task is BrowserAgent's and must not outlive it.

Ownership is decided by *when a page came into existence relative to the attach*, never by
what its URL looks like — a redirected agent tab is still an agent tab.
"""
from __future__ import annotations

import asyncio
import json
import time
import urllib.request
from pathlib import Path

import pytest
from playwright.async_api import async_playwright

from agent_v2.browser_ops import BrowserSession
from browser.playwright_backend import PlaywrightBackend

pytestmark = pytest.mark.asyncio

CDP_PORT = 9336
CDP_ENDPOINT = f"http://127.0.0.1:{CDP_PORT}"


def cdp_tab_count() -> int:
    """Tabs as the *browser* sees them, not as one Playwright connection sees them.

    This distinction is what made the leak invisible for so long: a page created over
    BrowserAgent's `connect_over_cdp` connection never appears in `context.pages` on the
    connection that launched the browser, so counting through the user's own handle reports
    no growth while the browser fills up. Every count assertion here goes through
    `/json/list`, which is the same thing a human counts by looking at the tab strip.
    """
    with urllib.request.urlopen(f"{CDP_ENDPOINT}/json/list", timeout=5) as response:
        targets = json.load(response)
    return len([t for t in targets if t.get("type") == "page"])


@pytest.fixture
async def user_browser():
    """Stands in for the user's own Chrome: started by them, outlives every task, and must
    come out of all of this with exactly the tabs it went in with."""
    pw = await async_playwright().start()
    browser = await pw.chromium.launch(headless=True,
                                       args=[f"--remote-debugging-port={CDP_PORT}"])
    yield browser
    if browser.is_connected():
        await browser.close()
    await pw.stop()


def _backend(*, explicit_target_url: str | None = None,
             preferred_tab_url: str | None = None) -> PlaywrightBackend:
    return PlaywrightBackend(Path("./_unused"), True, 5000, 3000, 12,
                             mode="cdp_attach", cdp_endpoint=CDP_ENDPOINT,
                             explicit_target_url=explicit_target_url,
                             preferred_tab_url=preferred_tab_url)


async def _user_context(user_browser):
    return user_browser.contexts[0] if user_browser.contexts else await user_browser.new_context()


async def _open_user_tab(user_browser, url: str):
    context = await _user_context(user_browser)
    page = context.pages[0] if context.pages else await context.new_page()
    await page.goto(url)
    return context, page


async def _settle(session: BrowserSession, expected: int, timeout_s: float = 5.0) -> None:
    """Wait until the *agent's* connection has noticed a tab the user closed.

    A page closed over the user's own Playwright connection reaches BrowserAgent's connection
    as a CDP event, so for a short window the two disagree about how many tabs are open. Every
    guard in `close_agent_tabs` reasons about liveness as its own connection sees it, which is
    the only view it has; asserting on that behaviour before the event has landed tests the
    propagation delay rather than the guard, and fails about one run in three.
    """
    deadline = time.monotonic() + timeout_s
    while len(session._live_pages()) != expected and time.monotonic() < deadline:
        await asyncio.sleep(0.02)


async def _run_task(*, explicit_target_url: str | None = None,
                    preferred_tab_url: str | None = None, body=None) -> int:
    """One whole task lifecycle: attach, adopt, (do something), clean up, detach. Returns the
    number of tabs cleaned up, mirroring exactly what `evals/run_realweb.py` does per task."""
    backend = _backend(explicit_target_url=explicit_target_url,
                       preferred_tab_url=preferred_tab_url)
    await backend.start()
    session = BrowserSession(backend)
    await session.adopt_existing_tabs()
    try:
        if body is not None:
            await body(session)
    finally:
        closed = await session.close_agent_tabs()
        await backend.close()
    return closed


# --- the reported leak -----------------------------------------------------------------

async def test_target_tab_created_at_attach_is_cleaned_up(user_browser, fixture_site_url):
    """The reported leak, at its smallest: a task whose target is not already open makes the
    backend create a tab during `start()`. That tab exists because of the task, so it is the
    task's to close — being present by the time ownership is snapshotted must not launder it
    into a user tab."""
    context, user_page = await _open_user_tab(user_browser, f"{fixture_site_url}/index.html")
    before = cdp_tab_count()

    closed = await _run_task(explicit_target_url=f"{fixture_site_url}/workflow_site_a.html")

    assert closed == 1
    assert cdp_tab_count() == before
    assert not user_page.is_closed()


async def test_repeated_tasks_do_not_accumulate_tabs(user_browser, fixture_site_url):
    """The failure as the suite actually met it: tab count must not be a function of how many
    tasks have run."""
    context, user_page = await _open_user_tab(user_browser, f"{fixture_site_url}/index.html")
    baseline = cdp_tab_count()

    counts = []
    for i in range(6):
        await _run_task(explicit_target_url=f"{fixture_site_url}/ground_{'abc'[i % 3]}.html")
        counts.append(cdp_tab_count())

    assert counts == [baseline] * 6, f"tab count drifted: {counts}"
    assert not user_page.is_closed()


# --- the other side of the invariant: user tabs are never taken -------------------------

async def test_pre_existing_matching_tab_is_reused_and_preserved(user_browser, fixture_site_url):
    """When the target is already open, the backend attaches to that tab rather than making
    one. It is the user's, so it survives — and its URL is untouched."""
    target = f"{fixture_site_url}/workflow_site_a.html"
    context, user_page = await _open_user_tab(user_browser, target)
    before = cdp_tab_count()

    closed = await _run_task(explicit_target_url=target)

    assert closed == 0
    assert cdp_tab_count() == before
    assert not user_page.is_closed()
    assert user_page.url.endswith("workflow_site_a.html")


async def test_current_page_task_preserves_the_user_tab(user_browser, fixture_site_url):
    """A task with no target of its own acts on whatever the user has open. Nothing was
    created, so nothing may be closed."""
    context, user_page = await _open_user_tab(user_browser, f"{fixture_site_url}/index.html")
    before = cdp_tab_count()

    closed = await _run_task()

    assert closed == 0
    assert cdp_tab_count() == before
    assert not user_page.is_closed()


async def test_preferred_tab_is_never_closed(user_browser, fixture_site_url):
    """An open-tab work item points at one specific existing tab. That tab is the user's."""
    context, user_page = await _open_user_tab(user_browser, f"{fixture_site_url}/index.html")
    extra = await context.new_page()
    await extra.goto(f"{fixture_site_url}/docs.html")
    before = cdp_tab_count()

    closed = await _run_task(preferred_tab_url=f"{fixture_site_url}/docs.html")

    assert closed == 0
    assert cdp_tab_count() == before
    assert not extra.is_closed()
    assert not user_page.is_closed()
    await extra.close()


async def test_several_user_tabs_all_survive_a_task_that_creates_one(user_browser, fixture_site_url):
    context, user_page = await _open_user_tab(user_browser, f"{fixture_site_url}/index.html")
    others = []
    for name in ("docs.html", "ground_a.html"):
        page = await context.new_page()
        await page.goto(f"{fixture_site_url}/{name}")
        others.append(page)
    before = cdp_tab_count()

    closed = await _run_task(explicit_target_url=f"{fixture_site_url}/ground_b.html")

    assert closed == 1
    assert cdp_tab_count() == before
    assert not user_page.is_closed()
    assert all(not page.is_closed() for page in others)
    for page in others:
        await page.close()


# --- tabs the task creates while it works -----------------------------------------------

async def test_tab_opened_during_the_task_is_cleaned_up(user_browser, fixture_site_url):
    context, user_page = await _open_user_tab(user_browser, f"{fixture_site_url}/index.html")
    before = cdp_tab_count()

    async def body(session: BrowserSession):
        page = await session.backend.page.context.new_page()
        await page.goto(f"{fixture_site_url}/docs.html")
        await session.sync_tabs()

    closed = await _run_task(explicit_target_url=f"{fixture_site_url}/ground_a.html", body=body)

    assert closed == 2  # the attach-created target tab, and the one opened mid-task
    assert cdp_tab_count() == before
    assert not user_page.is_closed()


async def test_popup_opened_by_a_click_is_cleaned_up(user_browser, fixture_site_url):
    """A popup is agent-created even though the agent never asked for a tab: it exists because
    of an action the task took."""
    context, user_page = await _open_user_tab(user_browser, f"{fixture_site_url}/index.html")
    before = cdp_tab_count()

    async def body(session: BrowserSession):
        page = session.backend.page
        await page.goto(f"{fixture_site_url}/docs.html")
        async with page.context.expect_page() as popup_info:
            await page.evaluate(
                "url => window.open(url, '_blank')", f"{fixture_site_url}/ground_a.html")
        await popup_info.value
        await session.sync_tabs()

    closed = await _run_task(explicit_target_url=f"{fixture_site_url}/docs.html", body=body)

    assert closed == 2  # the attach-created tab and the popup it spawned
    assert cdp_tab_count() == before
    assert not user_page.is_closed()


async def test_redirected_agent_tab_stays_agent_owned(user_browser, fixture_site_url):
    """Ownership is about origin, not about where the tab ended up — a tab that redirects
    somewhere unrelated is still the task's tab."""
    context, user_page = await _open_user_tab(user_browser, f"{fixture_site_url}/index.html")
    before = cdp_tab_count()

    async def body(session: BrowserSession):
        await session.backend.page.goto(f"{fixture_site_url}/ground_redirect.html")
        await session.backend.page.wait_for_url("**/ground_b.html", timeout=5000)
        await session.sync_tabs()

    closed = await _run_task(explicit_target_url=f"{fixture_site_url}/ground_redirect.html",
                             body=body)

    assert closed == 1
    assert cdp_tab_count() == before
    assert not user_page.is_closed()


# --- failure paths ----------------------------------------------------------------------

async def test_cleanup_still_happens_when_the_task_raises(user_browser, fixture_site_url):
    """A task that blows up must not be the one that leaks — the harness cleans up in a
    `finally`, and the accounting has to survive the exception."""
    context, user_page = await _open_user_tab(user_browser, f"{fixture_site_url}/index.html")
    before = cdp_tab_count()

    async def body(session: BrowserSession):
        page = await session.backend.page.context.new_page()
        await page.goto(f"{fixture_site_url}/docs.html")
        await session.sync_tabs()
        raise RuntimeError("task exploded")

    with pytest.raises(RuntimeError, match="task exploded"):
        await _run_task(explicit_target_url=f"{fixture_site_url}/ground_a.html", body=body)

    assert cdp_tab_count() == before
    assert not user_page.is_closed()


async def test_cleanup_is_idempotent(user_browser, fixture_site_url):
    context, user_page = await _open_user_tab(user_browser, f"{fixture_site_url}/index.html")
    before = cdp_tab_count()

    backend = _backend(explicit_target_url=f"{fixture_site_url}/ground_a.html")
    await backend.start()
    session = BrowserSession(backend)
    await session.adopt_existing_tabs()
    assert await session.close_agent_tabs() == 1
    assert await session.close_agent_tabs() == 0
    await backend.close()

    assert cdp_tab_count() == before
    assert not user_page.is_closed()


async def test_user_closing_the_agent_tab_first_is_not_an_error(user_browser, fixture_site_url):
    context, user_page = await _open_user_tab(user_browser, f"{fixture_site_url}/index.html")
    before = cdp_tab_count()

    async def body(session: BrowserSession):
        await session.backend.page.close()  # the user shut it themselves

    closed = await _run_task(explicit_target_url=f"{fixture_site_url}/ground_a.html", body=body)

    assert closed == 0  # nothing left to close; not a failure
    assert cdp_tab_count() == before
    assert not user_page.is_closed()


async def test_last_remaining_tab_is_kept_so_the_browser_survives(user_browser, fixture_site_url):
    """Chromium exits when its last tab closes. If the agent's tab is the only one left,
    keeping the user's browser alive beats reclaiming one tab."""
    context, user_page = await _open_user_tab(user_browser, f"{fixture_site_url}/index.html")

    backend = _backend(explicit_target_url=f"{fixture_site_url}/ground_a.html")
    await backend.start()
    session = BrowserSession(backend)
    await session.adopt_existing_tabs()
    await user_page.close()  # now the agent's tab is the only one in the browser
    await _settle(session, 1)

    closed = await session.close_agent_tabs()
    await backend.close()

    assert closed == 0
    assert cdp_tab_count() == 1
    assert user_browser.is_connected()


async def test_detach_leaves_the_user_browser_running(user_browser, fixture_site_url):
    """The guarantee that makes all of the above safe to run against a real Chrome."""
    context, user_page = await _open_user_tab(user_browser, f"{fixture_site_url}/index.html")

    await _run_task(explicit_target_url=f"{fixture_site_url}/ground_a.html")

    assert user_browser.is_connected()
    assert not user_page.is_closed()
    assert user_page.url.endswith("index.html")
    assert cdp_tab_count() >= 1


# --- the failure at the scale it was actually met ----------------------------------------

async def test_thirty_sequential_tasks_leave_the_browser_exactly_as_they_found_it(
        user_browser, fixture_site_url):
    """The stress the leak was found by, at the length that made it obvious.

    Six tasks is enough to show a drift; it is not enough to show that the drift is zero. The
    reported failure only became visible over a long unattended run — ninety tasks against one
    persistent profile left several hundred tabs and as many renderer processes behind, and
    the browser fell over partway through the suite. So the invariant is asserted after every
    single task rather than only at the end: a count that returns to baseline having wandered
    in between is a different bug that happens to net out.

    Every count goes through /json/list, the way a human counts by looking at the tab strip.
    """
    context, user_page = await _open_user_tab(user_browser, f"{fixture_site_url}/index.html")
    second = await context.new_page()
    await second.goto(f"{fixture_site_url}/docs.html")
    baseline = cdp_tab_count()
    user_urls = {user_page.url, second.url}

    counts: list[int] = []
    for index in range(30):
        # A mix of the two shapes that decide ownership: a target that has to be opened, and
        # one of the user's own tabs already sitting on the target.
        target = (f"{fixture_site_url}/ground_{'abc'[index % 3]}.html" if index % 5
                  else f"{fixture_site_url}/index.html")
        await _run_task(explicit_target_url=target)
        counts.append(cdp_tab_count())

    assert counts == [baseline] * 30, f"tab count drifted: {counts}"
    assert not user_page.is_closed(), "the user's first tab was taken"
    assert not second.is_closed(), "the user's second tab was taken"
    assert {user_page.url, second.url} == user_urls, "a user tab was navigated away"
    assert user_browser.is_connected(), "the user's browser did not survive"
