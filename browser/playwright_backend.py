"""Playwright execution layer — the only module allowed to touch a live `Page`.

Two lifecycle modes (browser.mode in config, see agent/config.py's BrowserConfig):

- "launch" (default, used by tests/fixtures): `launch_persistent_context` per task, so
  cookies/login state survive process restarts (ARCHITECTURE.md's crash-recovery
  requirement). BrowserAgent owns the browser process; `close()` shuts it down.
- "cdp_attach" (everyday use): `connect_over_cdp` to an already-running Chromium instance
  the user started separately. BrowserAgent's lifetime is decoupled from the browser's —
  `close()` disconnects only; the browser process, profile, and open tabs are left exactly
  as they were (see docs/BROWSERAGENT_MASTER_STATUS.md Section 8).

Element identity is always re-resolved from the *current* PageObservation's selector_hint
right before acting — this module never stores a locator/handle across calls, in either mode.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit

from playwright.async_api import Browser, Locator, Page, async_playwright

from browser.page_model import PageObservation
from browser.observer import extract_observation

_BLANK_URLS = {"about:blank", ""}


def _normalize_url_for_match(url: str) -> str:
    parts = urlsplit((url or "").strip())
    path = parts.path.rstrip("/")
    return f"{parts.scheme.lower()}://{parts.netloc.lower()}{path}?{parts.query}"


def urls_match(a: str, b: str) -> bool:
    """Public identity comparison for two page URLs, same normalization this module already
    uses internally for tab reuse (_select_page_by_url) — exposed so a caller that needs to
    decide "is the browser already on the resource it needs" (agent/controller.py's
    deterministic subgoal reorientation) can reuse the exact same rule instead of a second,
    possibly-inconsistent one."""
    return _normalize_url_for_match(a) == _normalize_url_for_match(b)


class ElementNotFoundError(Exception):
    pass


class BrowserAttachError(Exception):
    """Raised when cdp_attach mode can't reach a running Chromium instance. Always carries a
    user-facing message with the exact startup command (Section 20 of the master status
    task) — callers (ui/jobs.py, cli/main.py) surface str(exc) directly, no wrapping needed.
    """


class PlaywrightBackend:
    def __init__(self, profile_dir: Path, headless: bool, action_timeout_ms: int,
                 max_page_chars: int, max_visible_text_items: int,
                 mode: str = "launch", cdp_endpoint: str = "http://127.0.0.1:9222",
                 preferred_tab_url: Optional[str] = None,
                 explicit_target_url: Optional[str] = None):
        self.profile_dir = profile_dir
        self.headless = headless
        self.action_timeout_ms = action_timeout_ms
        self.max_page_chars = max_page_chars
        self.max_visible_text_items = max_visible_text_items
        self.mode = mode
        self.cdp_endpoint = cdp_endpoint
        # Set only for a semantic open-tab batch work item (see agent/runtime_policy.py's
        # BatchRuntimePolicy.is_open_tab) — the exact URL of the existing tab the resolver
        # picked out. cdp_attach's default page-selection heuristic ("most recently active
        # tab") has no notion of *which* tab a given batch child is actually supposed to
        # inspect, so without this a child can attach to whatever a sibling child's own
        # navigation left behind and then get SCOPE_BLOCKED trying to reach its own target.
        self.preferred_tab_url = preferred_tab_url
        # Set whenever the task/job already knows the concrete page it's going to operate on
        # (a fresh single-site task's resolved URL, a batch/workflow item's plain — not
        # open-tab — target, or a resumed job's last-known current_url) but that page is *not*
        # necessarily an existing tab. Unlike preferred_tab_url, an unmatched explicit_target_url
        # must NOT fall through to the "most recently active tab" heuristic below — that
        # heuristic is what let a brand-new "Open https://example.com" task silently attach to
        # and answer from an unrelated stale tab (e.g. goodhousekeeping.com) a previous task
        # left open. Left unset only for tasks with no known target of their own (a plain
        # "tell me what this page is about" current-page task), where reusing the currently
        # attached/most-recently-active tab is the intended behavior.
        self.explicit_target_url = explicit_target_url
        self._pw = None
        self._browser: Optional[Browser] = None  # only set in cdp_attach mode
        self.context = None
        self.page: Optional[Page] = None
        # The page `start()` had to create because no existing tab could serve the target,
        # or None when an existing tab was reused. Only this object knows the difference:
        # by the time anything else looks at the browser, a tab created here is
        # indistinguishable from one the user opened a moment earlier. Recording it is what
        # lets a caller tell "the user's tab" from "the tab that exists because of this task"
        # without resorting to URL guesswork. Nothing in the V1 loop reads it.
        self.created_page: Optional[Page] = None

    async def start(self) -> None:
        if self.mode == "cdp_attach":
            await self._start_cdp_attach()
        else:
            await self._start_launch()

    async def _start_launch(self) -> None:
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        downloads_dir = self.profile_dir / "downloads"
        downloads_dir.mkdir(parents=True, exist_ok=True)
        self._pw = await async_playwright().start()
        self.context = await self._pw.chromium.launch_persistent_context(
            user_data_dir=str(self.profile_dir),
            headless=self.headless,
            accept_downloads=True,
            downloads_path=str(downloads_dir),
        )
        if self.context.pages:
            self.page = self.context.pages[0]
        else:
            self.page = await self.context.new_page()
            self.created_page = self.page
        self.page.set_default_timeout(self.action_timeout_ms)

    async def _start_cdp_attach(self) -> None:
        self._pw = await async_playwright().start()
        try:
            self._browser = await self._pw.chromium.connect_over_cdp(self.cdp_endpoint)
        except Exception as exc:
            await self._pw.stop()
            self._pw = None
            raise BrowserAttachError(
                f"Persistent browser is not running at {self.cdp_endpoint}.\n"
                "Start it first, e.g.:\n"
                '  chrome.exe --remote-debugging-port=9222 '
                f'--user-data-dir="{self.profile_dir}"\n'
                "See docs/USING_BROWSERAGENT.md for the exact command for your machine."
            ) from exc

        if self.preferred_tab_url:
            # Open-tab-semantic target: exact match only, but a miss (tab closed between
            # resolution and this child starting) is a normal, recoverable case — fall through
            # to the default "most recently active" heuristic exactly as before.
            page = self._select_preferred_page(self._browser) or self._select_page(self._browser)
        elif self.explicit_target_url:
            # A known plain-URL target (a fresh single-site task's resolved URL, a non-open-tab
            # batch/workflow item, or a resumed job's last-known page): reuse a tab already
            # sitting on that exact URL if one exists, otherwise leave `page` unset so a fresh
            # blank page gets created below — never the "most recently active tab" heuristic,
            # which has no idea this task has a target at all and would just hand the model
            # whatever unrelated page a previous task left open (the stale-tab bug this guards
            # against). The model's own open_url step then navigates the blank page there.
            page = self._select_page_by_url(self._browser, self.explicit_target_url)
        else:
            # No known target at all (a current-page task) -> the existing "most recently
            # active tab" heuristic is exactly the intended behavior: act on whatever the user
            # already has open.
            page = self._select_page(self._browser)
        if page is None:
            # No usable page anywhere on the attached browser (Section 11), or an explicit
            # target with no matching tab: create exactly one page in an existing context
            # rather than launching a separate browser.
            context = self._browser.contexts[0] if self._browser.contexts else await self._browser.new_context()
            page = await context.new_page()
            self.created_page = page
        self.page = page
        self.context = self.page.context
        self.page.set_default_timeout(self.action_timeout_ms)

    def _select_page_by_url(self, browser: Browser, url: str) -> Optional[Page]:
        target_key = _normalize_url_for_match(url)
        all_pages = [p for ctx in browser.contexts for p in ctx.pages]
        for page in all_pages:
            if _normalize_url_for_match(page.url) == target_key:
                return page
        return None

    def _select_preferred_page(self, browser: Browser) -> Optional[Page]:
        """Attach to the exact existing tab a semantic open-tab resolution picked out
        (`preferred_tab_url`), never navigate a different tab to reach it — this is what a
        batch "look through my open tabs" work item requires (Section 6 of the open-tab
        sweep fix). Returns None (falls through to the default heuristic) when unset, or
        when the tab isn't found among the attached browser's current pages — e.g. the user
        closed it between resolution and this child starting, a normal, recoverable case
        rather than a hard failure."""
        if not self.preferred_tab_url:
            return None
        return self._select_page_by_url(browser, self.preferred_tab_url)

    def _select_page(self, browser: Browser) -> Page:
        """Page-selection policy for attaching to a browser that may already have tabs open
        (Section 10 of the master status task): reuse an existing usable page rather than
        opening a new one, never close or reorder the user's tabs.

        - One usable page -> use it.
        - Multiple -> prefer the most recently active one. CDP attach doesn't expose true
          focus timestamps, so this uses `context.pages` order as a deterministic proxy:
          Chromium appends newly opened/navigated tabs to the end of that list, so the last
          non-blank page is the best available approximation of "most recently active."
          Documented limitation: a tab opened long ago but manually re-focused most recently
          may not be detected as such.
        - None usable (no contexts, or only blank tabs) -> create exactly one new page in the
          first existing context (or a new context if the browser has none) rather than
          launching a separate browser.
        """
        all_pages = [p for ctx in browser.contexts for p in ctx.pages]
        non_blank = [p for p in all_pages if p.url not in _BLANK_URLS]
        if non_blank:
            return non_blank[-1]
        if all_pages:
            return all_pages[0]
        return None  # signals _start_cdp_attach's caller path below to create one

    async def close(self) -> None:
        if self.mode == "cdp_attach":
            # Disconnect only: never call browser.close()/context.close() here. Per
            # Playwright's own docs, Browser.close() on a CDP-attached browser "clears all
            # created contexts... and disconnects" — since we never know for certain whether
            # that also touches the pre-existing default context, the only way to guarantee
            # the user's Chromium process, profile, and tabs are left untouched is to not
            # issue any close command at all and just tear down our local driver connection.
            if self._pw is not None:
                await self._pw.stop()
            return
        if self.context is not None:
            await self.context.close()
        if self._pw is not None:
            await self._pw.stop()

    async def observe(self) -> PageObservation:
        return await extract_observation(self.page, self.max_page_chars, self.max_visible_text_items)

    def _resolve(self, obs: PageObservation, target_id: int) -> Locator:
        el = obs.element_by_id(target_id)
        if el is None:
            raise ElementNotFoundError(f"target id {target_id} not present in current observation")
        return self.page.locator(el.selector_hint.css).nth(el.selector_hint.nth)

    async def open_url(self, url: str) -> None:
        await self.page.goto(url, wait_until="domcontentloaded")

    async def click(self, obs: PageObservation, target_id: int) -> None:
        locator = self._resolve(obs, target_id)
        await locator.click()

    async def type(self, obs: PageObservation, target_id: int, text: str) -> None:
        locator = self._resolve(obs, target_id)
        await locator.fill(text)

    async def select(self, obs: PageObservation, target_id: int, value: str) -> None:
        locator = self._resolve(obs, target_id)
        await locator.select_option(label=value)

    async def scroll(self, direction: str = "down", amount_px: int = 600) -> None:
        delta = amount_px if direction == "down" else -amount_px
        await self.page.mouse.wheel(0, delta)

    async def back(self) -> None:
        await self.page.go_back(wait_until="domcontentloaded")

    async def extract(self, obs: PageObservation, target_id: Optional[int]) -> str:
        if target_id is None:
            return "\n".join(obs.visible_text)
        locator = self._resolve(obs, target_id)
        return (await locator.text_content()) or ""

    async def download(self, obs: PageObservation, target_id: int) -> dict[str, Any]:
        locator = self._resolve(obs, target_id)
        async with self.page.expect_download() as download_info:
            await locator.click()
        download = await download_info.value
        saved_path = await download.path()
        return {"suggested_filename": download.suggested_filename, "path": str(saved_path)}

    async def wait(self, params: dict[str, Any]) -> None:
        if "for_text" in params:
            await self.page.get_by_text(params["for_text"]).first.wait_for(state="visible")
        elif "url_contains" in params:
            await self.page.wait_for_url(f"**{params['url_contains']}**")
        else:
            ms = min(int(params.get("ms", 1000)), 3000)  # never an open-ended sleep
            await self.page.wait_for_timeout(ms)
