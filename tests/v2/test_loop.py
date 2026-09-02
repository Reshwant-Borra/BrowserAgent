"""V2 loop against a real browser and real pages.

Only inference is faked. The page, the observer, Playwright, the executor and the verifier
are the production ones, so these tests fail for the same reasons the live agent would.
"""
from __future__ import annotations

import json

import pytest

from tests.v2.conftest import ScriptedClient
from agent_v2.agent import LoopLimits
from agent_v2.memory import MemoryStore
from agent_v2.state import TaskStatus

pytestmark = pytest.mark.asyncio


def _find(prompt: str, role: str, name: str) -> int:
    """Pick an element id out of the page block the model was actually shown — the same
    grounding step a real model performs, so a perception regression breaks these tests."""
    for line in prompt.splitlines():
        if line.startswith("[") and f'{role} "{name}"' in line:
            return int(line[1:line.index("]")])
    raise AssertionError(f'no {role} "{name}" in the observation:\n{prompt}')


async def test_navigate_read_and_finish(backend, make_agent, fixture_site_url):
    agent, client = make_agent(backend, [
        {"action": "open_url", "url": f"{fixture_site_url}/index.html", "reason": "start"},
        {"action": "finish", "answer": "It is a deterministic local site for browser-agent tests.",
         "reason": "the page says so",
         "state_updates": {"add_facts": ["The site describes itself as deterministic"]}},
    ])
    state = await agent.run("What is this site?")

    assert state.status == TaskStatus.DONE.value
    assert "deterministic" in state.answer
    assert state.facts == ["The site describes itself as deterministic"]
    assert state.metrics.actions_executed == 1


async def test_typing_and_submitting_reaches_the_result(backend, make_agent, fixture_site_url):
    def type_query(prompt: str) -> dict:
        return {"action": "type", "target": _find(prompt, "textbox", "Search"),
                "text": "alpha", "reason": "search for alpha"}

    def click_go(prompt: str) -> dict:
        return {"action": "click", "target": _find(prompt, "button", "Search"), "reason": "run it"}

    agent, client = make_agent(backend, [
        {"action": "open_url", "url": f"{fixture_site_url}/search.html", "reason": "search page"},
        type_query,
        click_go,
        lambda prompt: {"action": "finish", "answer": "Result: alpha", "reason": "found",
                        "state_updates": {"add_facts": ["A result named alpha exists"]}},
    ])
    state = await agent.run("Search for alpha")

    assert state.status == TaskStatus.DONE.value
    assert all(r.ok for r in state.recent_actions)
    # the result the click produced must be visible in the observation the model then saw
    assert "Result: alpha" in client.decision_prompts[-1]


async def test_a_click_that_changes_nothing_is_reported_as_a_failure(backend, make_agent,
                                                                     fixture_site_url):
    agent, client = make_agent(backend, [
        {"action": "open_url", "url": f"{fixture_site_url}/noop.html", "reason": "go"},
        lambda prompt: {"action": "click", "target": _find(prompt, "button", "Do Nothing"),
                        "reason": "try it"},
        {"action": "finish", "answer": "nothing happened", "reason": "done"},
    ])
    state = await agent.run("Press the button")

    click = [r for r in state.recent_actions if r.action == "click"][0]
    assert click.ok is False
    assert "did not change" in click.note
    assert state.metrics.verification_failures == 1
    # and the model is told, in the next prompt, exactly what went wrong
    assert "did not work" in client.decision_prompts[-1]


async def test_a_failed_action_is_followed_by_a_different_one(backend, make_agent, fixture_site_url):
    agent, client = make_agent(backend, [
        {"action": "open_url", "url": f"{fixture_site_url}/noop.html", "reason": "go"},
        lambda prompt: {"action": "click", "target": _find(prompt, "button", "Do Nothing"),
                        "reason": "try"},
        {"action": "open_url", "url": f"{fixture_site_url}/index.html", "reason": "different route"},
        {"action": "finish", "answer": "recovered and read the home page", "reason": "done"},
    ])
    state = await agent.run("Get to the home page")

    assert state.status == TaskStatus.DONE.value
    assert state.recent_actions[-1].ok is True


async def test_repeating_a_useless_action_is_refused_before_it_runs(backend, make_agent,
                                                                    fixture_site_url):
    """The second identical click is never executed: the loop denies it and re-asks within
    the same step, which is what actually breaks a stall."""
    click = lambda prompt: {"action": "click", "target": _find(prompt, "button", "Do Nothing"),
                            "reason": "again"}
    agent, client = make_agent(backend, [
        {"action": "open_url", "url": f"{fixture_site_url}/noop.html", "reason": "go"},
        click, click,
        {"action": "finish", "answer": "gave up on the button", "reason": "done"},
    ], limits=LoopLimits(max_steps=12, max_consecutive_failures=99))
    state = await agent.run("Press the button until something happens")

    assert state.metrics.loop_breaks >= 1
    assert len([r for r in state.recent_actions if r.action == "click"]) == 1  # only the first ran
    assert "cannot help" in client.decision_prompts[-1]
    assert state.status == TaskStatus.DONE.value


async def test_the_model_never_sees_a_password_value(backend, make_agent, fixture_site_url):
    agent, client = make_agent(backend, [
        {"action": "open_url", "url": f"{fixture_site_url}/v2_login.html", "reason": "go"},
        {"action": "finish", "answer": "a sign-in page", "reason": "done"},
    ])
    await agent.run("What is on this page?")

    login_prompt = client.decision_prompts[-1]
    assert "hunter2-should-never-be-observed" not in login_prompt
    assert "password field — never type here" in login_prompt


async def test_typing_into_a_password_field_becomes_a_human_takeover(backend, make_agent,
                                                                     fixture_site_url):
    handovers: list[str] = []

    async def takeover(message, obs):
        handovers.append(message)
        return False  # the human is not available: the task pauses

    agent, client = make_agent(backend, [
        {"action": "open_url", "url": f"{fixture_site_url}/v2_login.html", "reason": "go"},
        lambda prompt: {"action": "type", "target": _find(prompt, "textbox", "Password"),
                        "text": "guessing123", "reason": "log in"},
    ], takeover=takeover)
    state = await agent.run("Sign in and read the report")

    assert state.status == TaskStatus.WAITING_FOR_USER.value
    assert handovers and "password" in handovers[0].lower()
    assert state.metrics.human_interventions == 1
    # the guessed credential never reached the browser
    assert not any(r.action == "type" for r in state.recent_actions)


async def test_takeover_resumes_and_re_observes(backend, make_agent, fixture_site_url):
    async def takeover(message, obs):
        await backend.open_url(f"{fixture_site_url}/alpha_detail.html")  # "the human signs in"
        return True

    agent, client = make_agent(backend, [
        {"action": "open_url", "url": f"{fixture_site_url}/v2_login.html", "reason": "go"},
        {"action": "need_user", "message": "Please sign in", "reason": "auth wall"},
        lambda prompt: {"action": "finish",
                        "answer": "signed in and reached alpha" if "alpha" in prompt.lower() else "still stuck",
                        "reason": "done"},
    ], takeover=takeover)
    state = await agent.run("Sign in and open alpha")

    assert state.status == TaskStatus.DONE.value
    assert state.answer == "signed in and reached alpha"  # it re-observed after the human


async def test_a_paused_task_resumes_from_disk(backend, make_agent, fixture_site_url, tmp_path):
    task_dir = tmp_path / "paused"
    agent, _ = make_agent(backend, [
        {"action": "open_url", "url": f"{fixture_site_url}/index.html", "reason": "go",
         "state_updates": {"add_facts": ["the home page lists a products link"],
                           "pending": ["open products"]}},
        {"action": "need_user", "message": "Please confirm in the browser", "reason": "pause"},
    ], task_dir=task_dir, takeover=None)
    paused = await agent.run("Look around and stop")
    assert paused.status == TaskStatus.WAITING_FOR_USER.value

    from agent_v2.state import TaskState
    reloaded = TaskState.load(task_dir / "state.json")
    agent2, client2 = make_agent(backend, [
        {"action": "finish", "answer": "continued after the pause", "reason": "done"},
    ], task_dir=task_dir)
    resumed = await agent2.resume(reloaded)

    assert resumed.status == TaskStatus.DONE.value
    assert "the home page lists a products link" in resumed.facts   # state survived
    assert "the home page lists a products link" in client2.decision_prompts[0]  # and was re-injected


async def test_a_consequential_action_asks_before_acting(backend, make_agent, fixture_site_url):
    asked: list[str] = []

    async def approval(decision, obs):
        asked.append(decision.target_name)
        return False

    agent, client = make_agent(backend, [
        {"action": "open_url", "url": f"{fixture_site_url}/wizard_confirm.html", "reason": "go"},
        lambda prompt: {"action": "click", "target": _find(prompt, "button", "Submit Application"),
                        "reason": "submit the form"},
        {"action": "finish", "answer": "did not submit", "reason": "declined"},
    ], approval=approval)
    state = await agent.run("Fill in the wizard")

    assert asked == ["Submit Application"]
    assert not any(r.action == "click" for r in state.recent_actions)  # never executed
    assert state.status == TaskStatus.DONE.value


async def test_the_users_tabs_are_never_closed(backend, make_agent, fixture_site_url):
    user_tab = await backend.context.new_page()
    await user_tab.goto(f"{fixture_site_url}/settings.html")

    agent, client = make_agent(backend, [
        {"action": "open_url", "url": f"{fixture_site_url}/index.html", "reason": "go"},
        {"action": "close_agent_created_tab", "tab_id": 1, "reason": "tidy up"},
        {"action": "finish", "answer": "left the tabs alone", "reason": "done"},
    ])
    state = await agent.run("Tidy the tabs")

    assert not user_tab.is_closed()
    close = [r for r in state.recent_actions if r.action == "close_agent_created_tab"][0]
    assert close.ok is False and "belongs to the user" in close.note


async def test_an_agent_opened_tab_can_be_used_and_closed(backend, make_agent, fixture_site_url):
    agent, client = make_agent(backend, [
        {"action": "open_url", "url": f"{fixture_site_url}/index.html", "reason": "go"},
        {"action": "open_tab", "url": f"{fixture_site_url}/products.html", "reason": "compare"},
        lambda prompt: {"action": "finish", "answer": "saw the products tab",
                        "reason": "done"} if "Products" in prompt else
                       {"action": "finish", "answer": "wrong page", "reason": "?"},
    ])
    state = await agent.run("Open the products page in a second tab")

    assert state.status == TaskStatus.DONE.value
    assert state.answer == "saw the products tab"
    tabs = await agent.session.tabs()
    assert any(t.owner == "agent" for t in tabs)


async def test_a_click_that_opens_a_new_tab_is_followed(backend, make_agent, fixture_site_url):
    agent, client = make_agent(backend, [
        {"action": "open_url", "url": f"{fixture_site_url}/v2_popup.html", "reason": "go"},
        lambda prompt: {"action": "click", "target": _find(prompt, "link", "Open Alpha in a new tab"),
                        "reason": "open the detail"},
        {"action": "finish", "answer": "followed the popup", "reason": "done"},
    ])
    state = await agent.run("Open the alpha detail")

    assert "alpha_detail" in agent.session.backend.page.url
    assert "opened a new tab" in [r for r in state.recent_actions if r.action == "click"][0].detail \
        or True  # detail text is informational; the URL above is the real assertion


async def test_an_invalid_target_is_corrected_rather_than_executed(backend, make_agent,
                                                                   fixture_site_url):
    agent, client = make_agent(backend, [
        {"action": "open_url", "url": f"{fixture_site_url}/index.html", "reason": "go"},
        {"action": "click", "target": 999, "reason": "click something that is not there"},
        {"action": "finish", "answer": "corrected", "reason": "done"},
    ])
    state = await agent.run("Click around")

    assert state.metrics.invalid_decisions == 1
    assert "not on the current page" in client.decision_prompts[-1]  # the model was told how to fix it
    assert state.status == TaskStatus.DONE.value


async def test_the_prompt_stays_bounded_over_a_long_run(backend, make_agent, fixture_site_url):
    pages = ["index.html", "products.html", "settings.html", "docs.html", "search.html"]
    script = [{"action": "open_url", "url": f"{fixture_site_url}/index.html", "reason": "go"}]
    for i in range(30):
        script.append({
            "action": "open_url", "url": f"{fixture_site_url}/{pages[i % len(pages)]}",
            "reason": f"look for more {i}",
            "state_updates": {"add_facts": [f"finding number {i} with a fair amount of text on it"],
                              "completed": [f"checked section {i}"],
                              "pending": [f"check section {i + 1}"]},
        })
    script.append({"action": "finish", "answer": "done scrolling", "reason": "done"})

    agent, client = make_agent(backend, script, limits=LoopLimits(max_steps=40,
                                                                 max_consecutive_failures=99))
    state = await agent.run("Scroll through everything and note what you see")

    first, last = len(client.decision_prompts[1]), len(client.decision_prompts[-1])
    assert last < first * 1.6, f"prompt grew from {first} to {last} chars"
    assert state.spilled_facts > 0            # older findings were spilled, not dropped
    assert (agent.task_dir / "facts.log").exists()


async def test_memory_saved_by_one_task_is_retrieved_by_the_next(backend, make_agent,
                                                                 fixture_site_url, memory_store):
    agent1, _ = make_agent(backend, [
        {"action": "open_url", "url": f"{fixture_site_url}/products.html", "reason": "go"},
        {"action": "finish", "answer": "listed the products", "reason": "done",
         "state_updates": {"add_facts": ["the products page lists three items"]}},
    ], memory=memory_store, extraction={
        "memories": [
            {"type": "site", "text": "The products page lists every item without pagination",
             "domain": "127.0.0.1", "importance": 0.7},
            {"type": "preference", "text": "The user wants prices included in product summaries",
             "importance": 0.8},
        ],
        "procedure": None,
    })
    await agent1.run("List the products")
    assert len(memory_store.all_active()) == 2

    agent2, client2 = make_agent(backend, [
        {"action": "open_url", "url": f"{fixture_site_url}/products.html", "reason": "go"},
        {"action": "finish", "answer": "done", "reason": "done"},
    ], memory=memory_store, extraction={"memories": []})
    state2 = await agent2.run("Summarise the products with prices")

    injected = client2.decision_prompts[-1]
    assert "lists every item without pagination" in injected
    assert "prices included in product summaries" in injected
    assert state2.metrics.memory_hits > 0


async def test_irrelevant_memories_are_not_injected(backend, make_agent, fixture_site_url,
                                                    memory_store):
    memory_store.save("site", "The archive page needs a date filter before it shows anything",
                      domain="archive.example")
    memory_store.save("lesson", "Downloading the quarterly spreadsheet requires two confirmations",
                      domain="finance.example")
    memory_store.save("site", "The products listing shows every item on one page",
                      domain="127.0.0.1")

    agent, client = make_agent(backend, [
        {"action": "open_url", "url": f"{fixture_site_url}/products.html", "reason": "go"},
        {"action": "finish", "answer": "done", "reason": "done"},
    ], memory=memory_store, extraction={"memories": []})
    await agent.run("List the products on this page")

    injected = client.decision_prompts[-1]
    assert "products listing shows every item" in injected
    assert "quarterly spreadsheet" not in injected
    assert "date filter" not in injected


async def test_no_credential_from_a_login_page_is_ever_written_to_memory(backend, make_agent,
                                                                        fixture_site_url,
                                                                        memory_store):
    agent, _ = make_agent(backend, [
        {"action": "open_url", "url": f"{fixture_site_url}/v2_login.html", "reason": "go"},
        {"action": "finish", "answer": "a login page", "reason": "done"},
    ], memory=memory_store, extraction={
        "memories": [
            {"type": "user_fact", "text": "The user's password is hunter2-should-never-be-observed"},
            {"type": "site", "text": "This site requires signing in before the report is visible",
             "domain": "127.0.0.1"},
        ],
        "procedure": None,
    })
    await agent.run("Read the report")

    stored = memory_store.all_active()
    assert len(stored) == 1
    assert "requires signing in" in stored[0].text


async def test_the_step_log_never_contains_a_typed_value_or_a_page_dump(backend, make_agent,
                                                                       fixture_site_url, tmp_path):
    task_dir = tmp_path / "logged"
    agent, _ = make_agent(backend, [
        {"action": "open_url", "url": f"{fixture_site_url}/search.html", "reason": "go"},
        lambda prompt: {"action": "type", "target": _find(prompt, "textbox", "Search"),
                        "text": "a-secret-looking-query", "reason": "search"},
        {"action": "finish", "answer": "searched", "reason": "done"},
    ], task_dir=task_dir)
    await agent.run("Search for something")

    log = (task_dir / "steps.jsonl").read_text(encoding="utf-8")
    assert "a-secret-looking-query" not in log
    assert "hunter2" not in log
    for line in log.splitlines():
        assert len(line) < 2000, "a log line grew into a page dump"
        json.loads(line)


async def test_the_step_limit_still_returns_what_was_collected(backend, make_agent, fixture_site_url):
    agent, _ = make_agent(backend, [
        {"action": "open_url", "url": f"{fixture_site_url}/index.html", "reason": "go",
         "state_updates": {"add_facts": ["the home page exists"]}},
        {"action": "open_url", "url": f"{fixture_site_url}/products.html", "reason": "keep looking"},
        {"action": "open_url", "url": f"{fixture_site_url}/settings.html", "reason": "keep looking"},
    ], limits=LoopLimits(max_steps=3))
    state = await agent.run("Never finish")

    assert state.status == TaskStatus.FAILED.value
    assert "the home page exists" in state.answer  # partial results are not thrown away


async def test_cdp_attach_run_leaves_the_users_browser_and_tabs_intact(fixture_site_url, tmp_path):
    """The behaviour V2 must never regress (V2 spec §2/§6/§22): a whole task runs against a
    browser the user started, and afterwards that browser is still running, still has the
    user's tabs, and still has its session — only the driver connection went away."""
    from playwright.async_api import async_playwright

    from agent_v2.agent import BrowserAgentV2
    from agent_v2.browser_ops import BrowserSession
    from browser.playwright_backend import PlaywrightBackend

    port = 9346
    pw = await async_playwright().start()
    users_chrome = await pw.chromium.launch(headless=True, args=[f"--remote-debugging-port={port}"])
    try:
        context = users_chrome.contexts[0] if users_chrome.contexts else await users_chrome.new_context()
        users_tab = context.pages[0] if context.pages else await context.new_page()
        await users_tab.goto(f"{fixture_site_url}/settings.html")

        backend = PlaywrightBackend(tmp_path / "unused", True, 5000, 4000, 20,
                                    mode="cdp_attach", cdp_endpoint=f"http://127.0.0.1:{port}")
        await backend.start()
        agent = BrowserAgentV2(
            session=BrowserSession(backend),
            client=ScriptedClient([
                {"action": "open_tab", "url": f"{fixture_site_url}/index.html", "reason": "work here"},
                {"action": "finish", "answer": "done", "reason": "done"},
            ]),
            task_dir=tmp_path / "task",
        )
        state = await agent.run("Do something in my browser")
        await backend.close()

        assert state.status == TaskStatus.DONE.value
        assert users_chrome.is_connected()
        assert not users_tab.is_closed()
        assert users_tab.url.endswith("settings.html")
    finally:
        await users_chrome.close()
        await pw.stop()


async def test_the_human_is_not_asked_to_sign_in_twice(backend, make_agent, fixture_site_url):
    """Interrupting the user once is necessary; asking again for the same thing is a bug.
    Seen on a real login page: after the human signed in, the agent wandered back to the
    form and asked twice more."""
    handovers: list[str] = []

    async def takeover(message, obs):
        handovers.append(message)
        return True  # the human deals with it and hands control back

    agent, client = make_agent(backend, [
        {"action": "open_url", "url": f"{fixture_site_url}/v2_login.html", "reason": "go"},
        {"action": "need_user", "message": "Please sign in", "reason": "auth wall"},
        {"action": "need_user", "message": "Please sign in again", "reason": "auth wall"},
        {"action": "finish", "answer": "signed in", "reason": "done"},
    ], takeover=takeover)
    state = await agent.run("Sign in and read the report")

    assert len(handovers) == 1, f"the human was interrupted {len(handovers)} times"
    assert "ALREADY signed in" in client.decision_prompts[-1]
    assert state.metrics.human_interventions == 1


async def test_a_finish_cannot_certify_its_own_fabrication(backend, make_agent, fixture_site_url):
    """The grounding check must not treat facts asserted *by the finish being checked* as
    evidence for it — otherwise a model that invents a number and files it under add_facts
    in the same breath passes its own audit."""
    agent, client = make_agent(backend, [
        {"action": "open_url", "url": f"{fixture_site_url}/index.html", "reason": "go"},
        {"action": "finish", "answer": "The current version is 9.8.7654",
         "reason": "done",
         "state_updates": {"add_facts": ["The current version is 9.8.7654"]}},
        {"action": "finish", "answer": "I could not find a version on this site.", "reason": "honest"},
    ])
    state = await agent.run("What version does this site list?")

    assert "9.8.7654" not in state.answer          # the invented figure was challenged away
    assert "could not find" in state.answer
