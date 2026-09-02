"""Phase 5 (architecture doc section 12/17): `security.default_domain_permission` enforced
end to end through the real AgentLoop step pipeline (real Playwright, scripted model) — the
same TASK_BLOCKED/DOMAIN_*_BLOCKED path every other runtime-policy violation already uses.
"""
from __future__ import annotations

from tests.integration.fake_llama import decision


async def test_no_access_blocks_the_very_first_action(make_agent_loop, fixture_site_url):
    """no_access blocks even a plain read (open_url) — the domain isn't just "no writes", it's
    "don't touch this at all"."""
    loop = make_agent_loop(
        "read the page", [],
        [decision("open_url", params={"url": f"{fixture_site_url}/index.html"})],
    )
    loop.config.security.default_domain_permission = "no_access"
    try:
        await loop.start_browser()
        state = await loop.run_steps(3)
        assert state.status == "blocked"
        assert "no_access" in (state.blocked_reason or "")

        from memory.event_store import EventType
        events = loop.event_store.all_events(loop.task_id)
        blocked = [e for e in events if e.type == EventType.TASK_BLOCKED]
        assert any(e.payload.get("failure_category") == "DOMAIN_NO_ACCESS_BLOCKED" for e in blocked)
    finally:
        await loop.aclose()


async def test_read_only_permission_allows_reads_but_blocks_consequential_click(
    make_agent_loop, fixture_site_url,
):
    loop = make_agent_loop(
        "submit the application", [],
        [
            decision("open_url", params={"url": f"{fixture_site_url}/wizard_confirm.html"}),
            decision("click", target=1, expected_result={"page_contains": "Submitted"}),
        ],
    )
    loop.config.security.default_domain_permission = "read_only"
    try:
        await loop.start_browser()
        state = await loop.run_steps(5)
        assert state.status == "blocked"
        assert "read_only" in (state.blocked_reason or "")

        from memory.event_store import EventType
        events = loop.event_store.all_events(loop.task_id)
        # The read (open_url, a _READ_ONLY_ACTIONS member) succeeded — only the consequential
        # click was blocked.
        blocked = [e for e in events if e.type == EventType.TASK_BLOCKED]
        assert any(e.payload.get("failure_category") == "DOMAIN_READ_ONLY_BLOCKED" for e in blocked)
        action_intents = [e for e in events if e.type == EventType.ACTION_INTENT]
        assert any(e.payload.get("action") == "open_url" for e in action_intents)
        assert not any(e.payload.get("action") == "click" for e in action_intents)
    finally:
        await loop.aclose()


async def test_browser_control_default_is_unrestricted(make_agent_loop, fixture_site_url):
    """The default value never adds any restriction beyond the existing approval gate — same
    end-to-end outcome as before Phase 5 existed."""
    loop = make_agent_loop(
        "read the page", [],
        [
            decision("open_url", params={"url": f"{fixture_site_url}/index.html"}),
            decision("finish", params={"result": "read the page"}),
        ],
    )
    assert loop.config.security.default_domain_permission == "browser_control"
    try:
        await loop.start_browser()
        state = await loop.run_steps(5)
        assert state.status == "completed"
    finally:
        await loop.aclose()
