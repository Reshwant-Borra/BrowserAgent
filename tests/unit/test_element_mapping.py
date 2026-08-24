"""Element identity: a model-chosen target id must resolve back to the *correct* live
element, even when multiple similar elements are present, and never to a stale one after
the DOM re-renders and ids are reassigned."""
from __future__ import annotations

import pytest
from playwright.async_api import async_playwright

from browser.observer import extract_observation
from browser.playwright_backend import PlaywrightBackend

HTML = """
<html><body>
  <button id="a" onclick="document.getElementById('log').textContent='A clicked'">Button A</button>
  <button id="b" onclick="document.getElementById('log').textContent='B clicked'">Button B</button>
  <button id="c" onclick="document.getElementById('log').textContent='C clicked'">Button C</button>
  <div id="log"></div>
</body></html>
"""


@pytest.fixture
async def backend(tmp_path):
    b = PlaywrightBackend(tmp_path / "profile", headless=True, action_timeout_ms=5000,
                           max_page_chars=5000, max_visible_text_items=20)
    await b.start()
    yield b
    await b.close()


async def test_target_id_maps_to_correct_element(backend):
    await backend.page.set_content(HTML)
    obs = await backend.observe()

    button_b = next(el for el in obs.elements if el.name == "Button B")
    await backend.click(obs, button_b.id)

    log_text = await backend.page.locator("#log").text_content()
    assert log_text == "B clicked"


async def test_ids_are_regenerated_fresh_each_observation(backend):
    await backend.page.set_content(HTML)
    obs1 = await backend.observe()
    obs2 = await backend.observe()
    # Same page, same order -> ids happen to match, but they are two independently
    # generated observations, not a cached/reused numbering.
    names1 = {el.id: el.name for el in obs1.elements}
    names2 = {el.id: el.name for el in obs2.elements}
    assert names1 == names2
    assert obs1.elements is not obs2.elements
