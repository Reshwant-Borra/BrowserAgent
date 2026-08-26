"""Phase 1 exit gate: the full observe -> decide -> validate -> execute -> re-observe loop
against a real Playwright browser and the static fixture site, driven by a scripted (not
live) model so the deterministic machinery is what's actually under test."""
from __future__ import annotations

import pytest

from inference.llama_client import CompletionResult
from memory.event_store import EventType
from tests.integration.fake_llama import decision
from tests.integration.helpers import read_events

pytestmark = pytest.mark.asyncio


async def test_navigate_click_and_read_price(make_agent_loop, fixture_site_url):
    loop = make_agent_loop("Find the price of the Basic plan", ["Basic plan"], [
        decision("open_url", params={"url": fixture_site_url + "/index.html"}),
        decision("click", target=1, expected_result={"page_contains": "Products"}),
        decision("finish", params={"result": "Basic plan is $9/month"}),
    ])
    state = await loop.run(max_steps=10)
    assert state.status == "completed"


async def test_search_fill_select_and_open_result(make_agent_loop, fixture_site_url):
    loop = make_agent_loop("Search for alpha and open its detail page", [], [
        decision("open_url", params={"url": fixture_site_url + "/search.html"}),
        decision("type", target=1, params={"text": "alpha"}),
        decision("click", target=2, expected_result={"element_present": "Result: alpha"}),
        decision("click", target=3, expected_result={"url_contains": "alpha_detail"}),
        decision("finish", params={"result": "opened alpha detail page"}),
    ])
    state = await loop.run(max_steps=10)
    assert state.status == "completed"
    assert state.recovery_level == "normal"


async def test_settings_dropdown_reveals_advanced_section(make_agent_loop, fixture_site_url):
    loop = make_agent_loop("Switch settings to Advanced mode", [], [
        decision("open_url", params={"url": fixture_site_url + "/settings.html"}),
        decision("select", target=1, params={"value": "Advanced"},
                  expected_result={"element_present": "Advanced settings"}),
        decision("finish", params={"result": "advanced mode enabled"}),
    ])
    state = await loop.run(max_steps=10)
    assert state.status == "completed"
    assert state.recovery_level == "normal"


async def test_download_action_saves_a_file(make_agent_loop, fixture_site_url):
    loop = make_agent_loop("Download sample.txt", [], [
        decision("open_url", params={"url": fixture_site_url + "/download.html"}),
        decision("download", target=1),
        decision("finish", params={"result": "downloaded"}),
    ])
    state = await loop.run(max_steps=10)
    assert state.status == "completed"

    events = read_events(loop)
    action_results = [e for e in events if e.type == EventType.ACTION_RESULT]
    download_result = next(e for e in action_results if e.payload.get("result_data", {}).get("path"))
    assert download_result.payload["result_data"]["suggested_filename"] == "sample.txt"


async def test_wizard_multi_page_navigation_without_submitting(make_agent_loop, fixture_site_url):
    loop = make_agent_loop("Navigate the wizard to the confirmation page without submitting", [], [
        decision("open_url", params={"url": fixture_site_url + "/wizard_step1.html"}),
        decision("click", target=1, expected_result={"url_contains": "wizard_step2"}),
        decision("click", target=1, expected_result={"url_contains": "wizard_confirm"}),
        decision("finish", params={"result": "reached confirmation page without submitting"}),
    ])
    state = await loop.run(max_steps=10)
    assert state.status == "completed"

    events = read_events(loop)
    action_intents = [e for e in events if e.type == EventType.ACTION_INTENT]
    assert all(e.payload["action"] != "click" or "submit" not in (e.payload.get("params") or {}).get("text", "")
               for e in action_intents)
    assert not any(e.payload.get("action") == "click" and "Submit" in str(e.payload) for e in action_intents)


async def test_inference_retry_does_not_duplicate_action_intent(make_agent_loop, fixture_site_url):
    class RetriedDecisionClient:
        endpoint = "scripted://retry"

        async def complete(self, prompt, grammar=None, max_tokens=256):
            return CompletionResult(
                text=decision("open_url", params={"url": fixture_site_url + "/index.html"}),
                prompt_tokens=len(prompt) // 4,
                predicted_tokens=8,
                total_latency_ms=1.0,
                attempt_count=2,
                inference_attempts=[
                    {
                        "request_id": "r1",
                        "attempt": 1,
                        "model": "scripted",
                        "failure_category": "READ_TIMEOUT",
                        "retryable": True,
                        "active_requests_at_start": 1,
                        "active_requests_at_end": 0,
                    },
                    {
                        "request_id": "r1",
                        "attempt": 2,
                        "model": "scripted",
                        "failure_category": None,
                        "http_status": 200,
                        "retryable": False,
                        "active_requests_at_start": 1,
                        "active_requests_at_end": 0,
                    },
                ],
            )

        async def health_check(self):
            return True

    loop = make_agent_loop("Open the fixture home page", [], [])
    loop.llama = RetriedDecisionClient()
    state = await loop.run(max_steps=1)

    events = read_events(loop)
    action_intents = [e for e in events if e.type == EventType.ACTION_INTENT]
    assert state.current_step == 1
    assert len(action_intents) == 1
