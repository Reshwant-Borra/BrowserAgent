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


def test_explicit_target_url_param_reaches_backend(tmp_config):
    """A fresh single-site task's resolved target URL (ui/jobs.py's decision.targets[0], or
    cli/main.py's extracted goal URL) must reach PlaywrightBackend as explicit_target_url so
    cdp_attach mode never falls back to the 'most recently active tab' heuristic for it."""
    tmp_config.browser.mode = "cdp_attach"
    loop = AgentLoop.create_new(tmp_config, "Open https://example.com/", [],
                                 explicit_target_url="https://example.com/")
    try:
        assert loop.browser.explicit_target_url == "https://example.com/"
        assert loop.browser.preferred_tab_url is None
    finally:
        loop.event_store.close()


def test_current_page_task_leaves_explicit_target_url_unset(tmp_config):
    """A current-page task (no explicit_target_url, no runtime_policy) must leave both fields
    unset so PlaywrightBackend keeps using the 'act on whatever's already open' heuristic."""
    loop = AgentLoop.create_new(tmp_config, "tell me what this page is about", [])
    try:
        assert loop.browser.explicit_target_url is None
        assert loop.browser.preferred_tab_url is None
    finally:
        loop.event_store.close()


def test_batch_plain_target_runtime_policy_sets_explicit_target_url(tmp_config):
    """A non-open-tab batch/workflow work item's target_url must also reach
    explicit_target_url (not just preferred_tab_url), so a batch child attaching in
    cdp_attach mode doesn't land on whatever tab a sibling child's own navigation left
    behind before it ever gets to its own open_url step."""
    tmp_config.browser.mode = "cdp_attach"
    policy = BatchRuntimePolicy(
        target_url="https://example.com/",
        navigation_scope=NavigationScopePolicy.SAME_ORIGIN,
        is_open_tab=False,
    )
    loop = AgentLoop.create_new(tmp_config, "check this page", [], runtime_policy=policy)
    try:
        assert loop.browser.explicit_target_url == "https://example.com/"
        assert loop.browser.preferred_tab_url is None
    finally:
        loop.event_store.close()


def test_resume_preserves_last_known_current_url(tmp_config):
    """Resume must preserve the resumed job's own page identity: if the task's last recorded
    current_url is set, it becomes explicit_target_url rather than leaving tab selection to
    the default 'most recently active tab' heuristic."""
    from memory.event_store import EventType

    tmp_config.browser.mode = "cdp_attach"
    created = AgentLoop.create_new(tmp_config, "check this page", [])
    task_id = created.task_id
    # Real current_url tracking is event-sourced (memory/replay.py derives it from an
    # OBSERVATION event's payload) — TaskStateStore.load() rebuilds from events whenever the
    # materialized row's last_event_id is behind, so a directly-saved TaskState wouldn't stick.
    created.event_store.append(task_id, 1, EventType.OBSERVATION,
                                {"url": "https://example.com/dashboard", "page_hash": "h"})
    created.event_store.close()

    resumed = AgentLoop.resume(tmp_config, task_id)
    try:
        assert resumed.browser.explicit_target_url == "https://example.com/dashboard"
        assert resumed.browser.preferred_tab_url is None
    finally:
        resumed.event_store.close()
