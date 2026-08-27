"""AgentLoop -> PlaywrightBackend wiring: a work item whose BatchRuntimePolicy.is_open_tab is
True must cause the browser backend to receive that exact tab's URL as preferred_tab_url, so
cdp_attach mode can attach to the correct existing tab instead of the default "most recently
active" heuristic (see docs/BROWSERAGENT_MASTER_STATUS.md's open-tab sweep finding)."""
from __future__ import annotations

from agent.loop import AgentLoop
from agent.runtime_policy import BatchRuntimePolicy, NavigationScopePolicy


def test_open_tab_runtime_policy_sets_preferred_tab_url(tmp_config):
    tmp_config.browser.mode = "cdp_attach"
    policy = BatchRuntimePolicy(
        target_url="https://example.com/",
        navigation_scope=NavigationScopePolicy.SAME_ORIGIN,
        is_open_tab=True,
    )
    loop = AgentLoop.create_new(tmp_config, "inspect this tab", [], runtime_policy=policy)
    try:
        assert loop.browser.preferred_tab_url == "https://example.com/"
    finally:
        loop.event_store.close()


def test_plain_url_runtime_policy_leaves_preferred_tab_url_unset(tmp_config):
    tmp_config.browser.mode = "cdp_attach"
    policy = BatchRuntimePolicy(
        target_url="https://example.com/",
        navigation_scope=NavigationScopePolicy.SAME_ORIGIN,
        is_open_tab=False,
    )
    loop = AgentLoop.create_new(tmp_config, "check this page", [], runtime_policy=policy)
    try:
        assert loop.browser.preferred_tab_url is None
    finally:
        loop.event_store.close()


def test_no_runtime_policy_leaves_preferred_tab_url_unset(tmp_config):
    loop = AgentLoop.create_new(tmp_config, "check this page", [])
    try:
        assert loop.browser.preferred_tab_url is None
    finally:
        loop.event_store.close()
