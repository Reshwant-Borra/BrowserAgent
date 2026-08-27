"""Open-tab enumeration for `cdp_attach` mode (Section 8/17 of the semantic planner task):
"Look through everything I currently have open" needs a structured list of the user's open
tabs before Qwen can semantically select which ones are relevant.

Uses the Chrome DevTools HTTP JSON API (`GET {cdp_endpoint}/json/list`) directly rather than
opening a full Playwright `connect_over_cdp()` connection just to read tab titles/URLs — it's
the same information, without spinning up a driver subprocess for a read that takes one HTTP
call. This mirrors browser/playwright_backend.py's own "disconnect, never touch the user's
tabs" philosophy: this module never opens, closes, or navigates any tab, it only lists them.
"""
from __future__ import annotations

from typing import Optional

import httpx
from pydantic import BaseModel

_BLANK_URLS = {"about:blank", ""}


class TabCandidate(BaseModel):
    id: int
    title: str
    url: str


class TabListUnavailable(Exception):
    """Raised when the CDP endpoint can't be reached to enumerate tabs. Carries a user-facing
    message — callers (router/resources.py) turn this into a clarification, never a hard
    failure, since "no persistent browser running" is a normal, recoverable state."""


async def list_open_tabs(
    cdp_endpoint: str,
    timeout_s: float = 3.0,
    transport: httpx.AsyncBaseTransport | None = None,
) -> list[TabCandidate]:
    """Real, addressable http(s) pages only — excludes blank tabs, extension background
    pages, service workers, and internal chrome://* pages, none of which are meaningful
    "resources" a user would refer to in a prompt. `transport` exists only so tests can
    inject an `httpx.MockTransport` instead of hitting a real CDP endpoint."""
    endpoint = cdp_endpoint.rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=timeout_s, transport=transport) as client:
            resp = await client.get(f"{endpoint}/json/list")
            resp.raise_for_status()
            targets = resp.json()
    except Exception as exc:
        raise TabListUnavailable(
            f"Could not list open tabs — persistent browser not reachable at {endpoint}."
        ) from exc

    candidates: list[TabCandidate] = []
    next_id = 1
    for target in targets:
        if target.get("type") != "page":
            continue
        url = (target.get("url") or "").strip()
        if url in _BLANK_URLS or not (url.startswith("http://") or url.startswith("https://")):
            continue
        candidates.append(TabCandidate(id=next_id, title=(target.get("title") or "").strip(), url=url))
        next_id += 1
    return candidates


def find_tab_by_url(candidates: list[TabCandidate], url: str) -> Optional[TabCandidate]:
    return next((c for c in candidates if c.url == url), None)
