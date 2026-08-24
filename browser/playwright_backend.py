"""Playwright execution layer — the only module allowed to touch a live `Page`.

Uses a persistent browser context (`launch_persistent_context`) per task so cookies/login
state survive process restarts (ARCHITECTURE.md's crash-recovery requirement). Element
identity is always re-resolved from the *current* PageObservation's selector_hint right
before acting — this module never stores a locator/handle across calls.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from playwright.async_api import Locator, Page, async_playwright

from browser.page_model import PageObservation
from browser.observer import extract_observation


class ElementNotFoundError(Exception):
    pass


class PlaywrightBackend:
    def __init__(self, profile_dir: Path, headless: bool, action_timeout_ms: int,
                 max_page_chars: int, max_visible_text_items: int):
        self.profile_dir = profile_dir
        self.headless = headless
        self.action_timeout_ms = action_timeout_ms
        self.max_page_chars = max_page_chars
        self.max_visible_text_items = max_visible_text_items
        self._pw = None
        self.context = None
        self.page: Optional[Page] = None

    async def start(self) -> None:
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
        self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
        self.page.set_default_timeout(self.action_timeout_ms)

    async def close(self) -> None:
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
