"""The V2 loop's view of the browser: execute one validated action, then say whether it
actually worked.

Wraps `browser/playwright_backend.py` rather than replacing it — the CDP-attach lifecycle
there (connect to the user's already-running Chrome, and on close *disconnect only*, never
touching their process/profile/tabs) is exactly what V2 needs and is already proven by the
existing regression suite.

Two things live here that the old backend has no notion of:

- **Tab ownership.** Every page that exists at attach time is the user's and is never
  closed. Every page that appears afterwards (agent `open_tab`, or a popup a click opened)
  is agent-created and may be closed. That distinction is V2 spec §22 and it is enforced
  here, not left to the model's judgement.
- **Verification.** Deterministic post-checks (V2 spec §20) — URL change, page-state hash
  change, field value, expected text — so a click that silently did nothing is reported as
  a failure instead of being assumed to have worked. No LLM call is involved.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from browser.page_model import PageObservation
from browser.playwright_backend import ElementNotFoundError, PlaywrightBackend
from agent_v2.actions import Decision, V2Action

_BLANK = {"about:blank", ""}


@dataclass
class TabInfo:
    id: int
    url: str
    title: str
    owner: str  # "user" | "agent"
    active: bool = False


@dataclass
class ActionOutcome:
    ok: bool
    detail: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    browser_ms: float = 0.0


@dataclass
class Verification:
    passed: bool
    note: str = ""
    changed: bool = False


class BrowserSession:
    def __init__(self, backend: PlaywrightBackend):
        self.backend = backend
        self._pages: dict[int, Any] = {}
        self._owner: dict[int, str] = {}
        self._next_id = 1
        self._baseline_taken = False

    # ---- tabs -----------------------------------------------------------------------

    async def adopt_existing_tabs(self) -> None:
        """Snapshot ownership once, right after attaching. Everything open now belongs to
        the user and is off-limits for closing for the rest of the run."""
        for page in self._live_pages():
            self._register(page, owner="user")
        self._baseline_taken = True

    async def sync_tabs(self) -> list[TabInfo]:
        """Refresh the id->page map. Pages that appeared after the baseline snapshot are
        agent-created; pages the user closed drop out silently."""
        known = set()
        for page in self._live_pages():
            tab_id = self._id_of(page)
            if tab_id is None:
                tab_id = self._register(page, owner="agent" if self._baseline_taken else "user")
            known.add(tab_id)
        for tab_id in [t for t in self._pages if t not in known]:
            self._pages.pop(tab_id, None)
            self._owner.pop(tab_id, None)
        return await self.tabs()

    async def tabs(self) -> list[TabInfo]:
        out: list[TabInfo] = []
        for tab_id, page in sorted(self._pages.items()):
            if page.is_closed():
                continue
            out.append(TabInfo(
                id=tab_id,
                url=page.url,
                title=await _safe_title(page),
                owner=self._owner.get(tab_id, "user"),
                active=page is self.backend.page,
            ))
        return out

    def current_tab_id(self) -> Optional[int]:
        return self._id_of(self.backend.page)

    async def close_agent_tabs(self) -> int:
        """Close every tab this run opened, leaving the user's exactly as they were.

        Never called by the loop: interactively, a tab the agent opened is a tab the user can
        see and may want, so tidying up behind them would be presumptuous. It is for
        unattended callers — the evaluation harness — which otherwise leave one tab per task
        behind. Ninety task runs against one persistent profile accumulated several hundred
        open tabs and several hundred renderer processes, which is what eventually took the
        browser down mid-suite.
        """
        closed = 0
        for tab_id, owner in list(self._owner.items()):
            if owner != "agent":
                continue
            page = self._pages.get(tab_id)
            self._pages.pop(tab_id, None)
            self._owner.pop(tab_id, None)
            if page is None or page.is_closed():
                continue
            try:
                await page.close()
                closed += 1
            except Exception:
                pass
        remaining = [p for p in self._live_pages() if not p.is_closed()]
        if remaining and (self.backend.page is None or self.backend.page.is_closed()):
            self.backend.page = remaining[0]
            self.backend.context = remaining[0].context
        return closed

    @staticmethod
    def render_tabs(tabs: list[TabInfo], limit: int = 8) -> str:
        if len(tabs) <= 1:
            return ""
        lines = []
        for tab in tabs[:limit]:
            mark = " <- you are here" if tab.active else ""
            owner = "yours" if tab.owner == "agent" else "user's"
            lines.append(f"[{tab.id}] ({owner}) {_short(tab.title, 60)} — {_short(tab.url, 80)}{mark}")
        if len(tabs) > limit:
            lines.append(f"(+{len(tabs) - limit} more)")
        return "\n".join(lines)

    def _live_pages(self) -> list[Any]:
        pages: list[Any] = []
        contexts = []
        if getattr(self.backend, "_browser", None) is not None:
            contexts = self.backend._browser.contexts
        elif self.backend.context is not None:
            contexts = [self.backend.context]
        for context in contexts:
            for page in context.pages:
                if not page.is_closed():
                    pages.append(page)
        if self.backend.page is not None and self.backend.page not in pages and not self.backend.page.is_closed():
            pages.append(self.backend.page)
        return pages

    def _register(self, page: Any, owner: str) -> int:
        tab_id = self._next_id
        self._next_id += 1
        self._pages[tab_id] = page
        self._owner[tab_id] = owner
        return tab_id

    def _id_of(self, page: Any) -> Optional[int]:
        for tab_id, known in self._pages.items():
            if known is page:
                return tab_id
        return None

    # ---- execution ------------------------------------------------------------------

    #: V2 captures more page text than the legacy loop's default (see
    #: browser/observer.py's `max_text_nodes`) because `prompts.render_page` budgets the
    #: rendered page in tokens — it would rather trim a large capture than never see the
    #: paragraph that answers the question.
    MAX_TEXT_NODES = 150

    async def observe(self) -> PageObservation:
        """Observing races navigation: a page that redirects, or JS that replaces the
        document, destroys the execution context mid-`evaluate`. That is a normal event on
        the real web, not a task-ending error, so it is waited out and retried once."""
        from browser.observer import EXTENDED_TEXT_SELECTOR, extract_observation

        for attempt in (1, 2):
            try:
                return await extract_observation(
                    self.backend.page, self.backend.max_page_chars,
                    self.backend.max_visible_text_items, self.MAX_TEXT_NODES,
                    EXTENDED_TEXT_SELECTOR,
                )
            except Exception:
                if attempt == 2:
                    raise
                try:
                    await self.backend.page.wait_for_load_state("domcontentloaded", timeout=5000)
                except Exception:
                    pass
                await asyncio.sleep(0.4)
        raise RuntimeError("unreachable")

    async def execute(self, decision: Decision, obs: PageObservation) -> ActionOutcome:
        started = time.monotonic()
        try:
            outcome = await self._execute(decision, obs)
        except ElementNotFoundError as exc:
            outcome = ActionOutcome(ok=False, detail=f"element vanished before the action ran: {exc}")
        except Exception as exc:  # Playwright timeouts, navigation aborts, detached nodes
            outcome = ActionOutcome(ok=False, detail=_short(_describe(exc), 180))
        outcome.browser_ms = (time.monotonic() - started) * 1000
        return outcome

    async def _execute(self, decision: Decision, obs: PageObservation) -> ActionOutcome:
        action = decision.action
        backend = self.backend

        if action is V2Action.OPEN_URL:
            await self._goto(backend.page, decision.url)
            return ActionOutcome(True, decision.url)

        if action is V2Action.CLICK:
            before = set(id(p) for p in self._live_pages())
            url_before = backend.page.url
            await backend.click(obs, decision.target)
            await self._settle()
            # A click may have opened a popup/target=_blank tab. Register it and follow it —
            # not following is one of the classic "the agent lost the result" failures.
            new_pages = await self._new_pages_since(before, url_before)
            if new_pages:
                page = new_pages[-1]
                self._register(page, owner="agent")
                backend.page = page
                backend.context = page.context
                page.set_default_timeout(backend.action_timeout_ms)
                return ActionOutcome(True, f'clicked "{decision.target_name}" (opened a new tab)')
            return ActionOutcome(True, f'clicked "{decision.target_name}"')

        if action is V2Action.TYPE:
            await backend.type(obs, decision.target, decision.text)
            if decision.submit:
                await backend.page.keyboard.press("Enter")
                await self._settle()
            return ActionOutcome(True, f'typed into "{decision.target_name}"' + (" + Enter" if decision.submit else ""))

        if action is V2Action.SELECT:
            await backend.select(obs, decision.target, decision.value)
            return ActionOutcome(True, f'selected "{decision.value}"')

        if action is V2Action.SCROLL:
            await backend.scroll(decision.direction)
            await backend.page.wait_for_timeout(350)  # let lazy content paint
            return ActionOutcome(True, decision.direction)

        if action is V2Action.BACK:
            await backend.back()
            return ActionOutcome(True, "went back")

        if action is V2Action.EXTRACT:
            text = await backend.extract(obs, decision.target)
            text = " ".join((text or "").split())[:1500]
            return ActionOutcome(True, f"extracted {len(text)} chars", data={"extracted": text})

        if action is V2Action.OPEN_TAB:
            context = backend.page.context if backend.page is not None else backend.context
            page = await context.new_page()
            page.set_default_timeout(backend.action_timeout_ms)
            tab_id = self._register(page, owner="agent")
            await self._goto(page, decision.url)
            backend.page = page
            backend.context = page.context
            return ActionOutcome(True, f"opened tab {tab_id}", data={"tab_id": tab_id})

        if action is V2Action.SWITCH_TAB:
            page = self._pages.get(decision.tab_id)
            if page is None or page.is_closed():
                return ActionOutcome(False, f"tab {decision.tab_id} does not exist")
            await page.bring_to_front()
            backend.page = page
            backend.context = page.context
            page.set_default_timeout(backend.action_timeout_ms)
            return ActionOutcome(True, f"switched to tab {decision.tab_id}")

        if action is V2Action.CLOSE_AGENT_CREATED_TAB:
            page = self._pages.get(decision.tab_id)
            if page is None or page.is_closed():
                return ActionOutcome(False, f"tab {decision.tab_id} does not exist")
            if self._owner.get(decision.tab_id) != "agent":
                # Hard rule, not a preference: the user's own tabs are never closed by the
                # agent, whatever the model asked for (V2 spec §22).
                return ActionOutcome(False, f"tab {decision.tab_id} belongs to the user and will not be closed")
            was_current = page is backend.page
            await page.close()
            self._pages.pop(decision.tab_id, None)
            self._owner.pop(decision.tab_id, None)
            if was_current:
                remaining = [p for p in self._live_pages()]
                if remaining:
                    backend.page = remaining[-1]
                    backend.context = backend.page.context
            return ActionOutcome(True, f"closed tab {decision.tab_id}")

        if action is V2Action.WAIT:
            if decision.text:
                try:
                    await backend.page.get_by_text(decision.text).first.wait_for(
                        state="visible", timeout=min(decision.ms or 1000, 3000)
                    )
                    return ActionOutcome(True, f'"{_short(decision.text, 40)}" appeared')
                except Exception:
                    return ActionOutcome(False, f'"{_short(decision.text, 40)}" did not appear')
            await backend.page.wait_for_timeout(decision.ms or 1000)
            return ActionOutcome(True, f"waited {decision.ms}ms")

        return ActionOutcome(True, "")

    #: Loading a page is not the same kind of operation as clicking a button, and giving both
    #: the same budget makes ordinary slow sites look like failures. Navigation gets its own.
    NAVIGATION_TIMEOUT_MS = 30000

    async def _goto(self, page: Any, url: str) -> None:
        """Navigate, with one retry on a transient failure.

        Real networks produce blips — a dropped handshake, a proxy hiccup, a site that takes
        longer than usual under load. Observed on this machine as an intermittent certificate
        error that succeeded on the very next attempt. Retrying once with `commit` (which
        resolves as soon as the navigation is committed rather than waiting for the document)
        turns a task-ending error into a step that simply took a moment longer.
        """
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=self.NAVIGATION_TIMEOUT_MS)
            return
        except Exception:
            await asyncio.sleep(0.5)
        await page.goto(url, wait_until="commit", timeout=self.NAVIGATION_TIMEOUT_MS)
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=5000)
        except Exception:
            pass

    async def _new_pages_since(self, before: set[int], url_before: str) -> list[Any]:
        """Pages that appeared as a result of the last action.

        Playwright can surface a `target=_blank` page slightly after the click resolves, so a
        single check right afterwards misses it intermittently — and missing it means the
        agent keeps reading the old page while the answer sits in a tab it never noticed.
        The short poll is only paid when the current page did *not* navigate, i.e. exactly
        the case where a popup is the plausible explanation for the click doing nothing here.
        """
        new_pages = [p for p in self._live_pages() if id(p) not in before]
        if new_pages or self.backend.page.url != url_before:
            return new_pages
        for _ in range(6):
            await asyncio.sleep(0.15)
            new_pages = [p for p in self._live_pages() if id(p) not in before]
            if new_pages:
                return new_pages
        return []

    async def _settle(self) -> None:
        """Bounded wait for whatever the last interaction kicked off. Never open-ended: a
        page that streams forever must not stall the loop."""
        try:
            await self.backend.page.wait_for_load_state("domcontentloaded", timeout=5000)
        except Exception:
            pass
        try:
            await self.backend.page.wait_for_timeout(300)
        except Exception:
            pass

    # ---- verification ---------------------------------------------------------------

    def verify(self, decision: Decision, outcome: ActionOutcome,
               before: PageObservation, after: PageObservation) -> Verification:
        """Deterministic only. Ambiguity is reported to the model as an observation, never
        resolved by a second LLM call (V2 spec §20)."""
        if not outcome.ok:
            return Verification(False, outcome.detail or "the action raised an error")

        changed = (before.state_hash != after.state_hash) or (before.url != after.url)
        action = decision.action
        verdict = self._deterministic_verdict(decision, before, after, changed)

        # `expect` is advisory, never the verdict. In practice a model writes a *description*
        # of what it hopes to see ("search results for X") rather than a literal string on
        # the page, so substring-matching it as a pass/fail condition manufactures failures
        # for actions that plainly worked — and those false failures then push the agent into
        # pointless recovery. It is kept as a note, because when it does match it is good
        # confirmation, and when it doesn't the model deserves to know.
        if decision.expect and verdict.passed:
            if _text_present(after, decision.expect):
                return Verification(True, "", changed)
            return Verification(
                True, f'(worked, but "{_short(decision.expect, 40)}" is not visible)', changed)
        return verdict

    def _deterministic_verdict(self, decision: Decision, before: PageObservation,
                               after: PageObservation, changed: bool) -> Verification:
        action = decision.action

        if action in (V2Action.OPEN_URL, V2Action.OPEN_TAB):
            if _same_target(after.url, decision.url or ""):
                return Verification(True, "", changed)
            if changed:
                return Verification(True, f"landed on {_short(after.url, 60)}", changed)
            return Verification(False, f"still on {_short(after.url, 60)}", changed)

        if action is V2Action.TYPE:
            typed = _field_value(after, decision.target_name, decision.text)
            if decision.submit:
                if changed:
                    return Verification(True, "", True)
                return Verification(False, "pressing Enter did not change the page", False)
            if typed is True:
                return Verification(True, "", changed)
            if typed is False:
                return Verification(False, "the field does not contain the text that was typed", changed)
            return Verification(True, "", changed)

        if action is V2Action.SELECT:
            if _field_value(after, decision.target_name, decision.value) is False:
                return Verification(False, "the control does not show the selected value", changed)
            return Verification(True, "", changed)

        if action is V2Action.CLICK:
            if changed:
                return Verification(True, "", True)
            return Verification(False, "the page did not change at all after the click", False)

        if action is V2Action.BACK:
            if before.url != after.url:
                return Verification(True, "", True)
            return Verification(False, "the URL did not change going back", False)

        if action is V2Action.SCROLL:
            if changed:
                return Verification(True, "", True)
            return Verification(True, "nothing new appeared — this is the end of the content", False)

        return Verification(True, "", changed)


def _same_target(current: str, requested: str) -> bool:
    if not requested:
        return False
    from urllib.parse import urlsplit
    a, b = urlsplit(current), urlsplit(requested)
    return a.netloc.lower().removeprefix("www.") == b.netloc.lower().removeprefix("www.")


def _text_present(obs: PageObservation, needle: str) -> bool:
    wanted = _norm(needle)
    if not wanted:
        return False
    haystack = _norm(" ".join(obs.visible_text) + " " + obs.title + " " +
                     " ".join(el.name for el in obs.elements))
    return wanted in haystack


def _field_value(obs: PageObservation, name: Optional[str], expected: Optional[str]):
    """True/False if the field was found and does/doesn't hold the value, None if the field
    is no longer identifiable (a re-render, which is not itself evidence of failure)."""
    if not name or expected is None:
        return None
    for element in obs.elements:
        if element.name == name and not element.sensitive:
            if element.value is None:
                return None
            return _norm(expected) in _norm(element.value)
    return None


def _norm(text: str) -> str:
    return " ".join("".join(c.lower() if c.isalnum() else " " for c in text or "").split())


def _short(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


async def _safe_title(page: Any) -> str:
    try:
        return (await page.title()) or "(untitled)"
    except Exception:
        return "(untitled)"


def _describe(exc: Exception) -> str:
    message = str(exc).split("\n")[0]
    return f"{type(exc).__name__}: {message}" if message else type(exc).__name__
