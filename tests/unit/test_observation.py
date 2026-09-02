"""Compact observation extraction against static fixture HTML (page.set_content — no
navigation, no fixture-site server needed). Covers button/link/textbox/select/heading/
hidden/disabled elements per the Phase 1 test plan."""
from __future__ import annotations

import pytest
from playwright.async_api import async_playwright

from browser.observer import extract_observation

FIXTURE_HTML = """
<html><head><title>Obs Test</title></head><body>
  <h1>Welcome</h1>
  <a href="/x">A Link</a>
  <button>A Button</button>
  <button disabled>Disabled Button</button>
  <input type="text" placeholder="Search here" />
  <select><option>One</option><option>Two</option></select>
  <div hidden>Should never appear</div>
  <p>Some visible paragraph text.</p>
</body></html>
"""


@pytest.fixture
async def page():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        pg = await browser.new_page()
        yield pg
        await browser.close()


async def test_extraction_finds_all_expected_elements(page):
    await page.set_content(FIXTURE_HTML)
    obs = await extract_observation(page, max_chars=5000, max_visible_text_items=20)

    roles = {(el.role, el.name) for el in obs.elements}
    assert ("link", "A Link") in roles
    assert ("button", "A Button") in roles
    assert ("textbox", "Search here") in roles
    assert ("select", "One") not in roles  # selects are one element, not their options

    select_elements = [el for el in obs.elements if el.role == "select"]
    assert len(select_elements) == 1
    assert select_elements[0].options == ["One", "Two"]


async def test_disabled_element_is_flagged_not_excluded(page):
    await page.set_content(FIXTURE_HTML)
    obs = await extract_observation(page, max_chars=5000, max_visible_text_items=20)
    disabled = [el for el in obs.elements if el.name == "Disabled Button"]
    assert len(disabled) == 1
    assert disabled[0].disabled is True


async def test_hidden_element_excluded(page):
    await page.set_content(FIXTURE_HTML)
    obs = await extract_observation(page, max_chars=5000, max_visible_text_items=20)
    rendered = obs.render_compact(5000, 20)
    assert "Should never appear" not in rendered


async def test_heading_and_text_appear_in_visible_text(page):
    await page.set_content(FIXTURE_HTML)
    obs = await extract_observation(page, max_chars=5000, max_visible_text_items=20)
    assert "Welcome" in obs.visible_text
    assert any("visible paragraph" in t for t in obs.visible_text)


async def test_div_status_text_appears_in_visible_text(page):
    """Acceptance-test finding (docs/BROWSERAGENT_MASTER_STATUS.md's FINAL ACCEPTANCE
    section, RC-3): CANONICAL_TEXT_SELECTOR used to omit `<div>`, the single most common
    real-world container for status/confirmation text. A live task correctly `select`ed a
    dropdown option, but could never observe the resulting "Current mode: Compact" status
    div, so it endlessly oscillated between the wrong action (click) and the right one
    (select) until its step budget was exhausted. This reproduces the minimal shape: a status
    div with no other wrapping heading/paragraph/span/list element around its text."""
    await page.set_content(
        '<html><body><h1>Preferences</h1>'
        '<div id="mode-status">Current mode: Compact</div>'
        '</body></html>'
    )
    obs = await extract_observation(page, max_chars=5000, max_visible_text_items=20)
    assert any("Current mode: Compact" in t for t in obs.visible_text)


async def test_state_hash_present_and_stable_across_identical_content(page):
    await page.set_content(FIXTURE_HTML)
    obs1 = await extract_observation(page, max_chars=5000, max_visible_text_items=20)
    obs2 = await extract_observation(page, max_chars=5000, max_visible_text_items=20)
    assert obs1.state_hash == obs2.state_hash
    assert len(obs1.state_hash) == 64  # sha256 hex
