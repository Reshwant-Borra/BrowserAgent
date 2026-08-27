from __future__ import annotations

import json

from batch.models import BatchEventType, BatchPolicy, FailureCategory, NavigationScope, ResultContract, WorkItemStatus
from batch.policies import in_navigation_scope, normalize_target_url, read_only_allows
from batch.store import BatchStore
from agent.runtime_policy import (
    BatchRuntimePolicy,
    NavigationScopePolicy,
    post_navigation_violation,
    pre_action_violation,
    url_in_scope,
)
from agent.schemas import ActionType, ModelDecision
from browser.page_model import ElementRef, SelectorHint


def test_create_batch_deduplicates_targets(tmp_path):
    store = BatchStore(tmp_path / "batch.db")
    try:
        batch_id = store.create_batch(
            "check assignments",
            ["HTTP://Example.COM/a/#frag", "http://example.com/a", "http://example.com/a?course=2"],
            ResultContract(name="assignment"),
            BatchPolicy(),
            batch_id="b1",
        )
        progress = store.progress(batch_id)
        items = store.items(batch_id)
        events = store.events(batch_id)
        assert progress["item_count"] == 2
        assert progress["duplicates"] == 1
        assert [item["target_key"] for item in items] == ["http://example.com/a", "http://example.com/a?course=2"]
        assert any(event["type"] == BatchEventType.WORK_ITEM_DEDUPED.value for event in events)
    finally:
        store.close()


def test_claim_complete_and_progress_counts(tmp_path):
    store = BatchStore(tmp_path / "batch.db")
    try:
        batch_id = store.create_batch("goal", ["http://a.test", "http://b.test"], ResultContract(), BatchPolicy(), "b1")
        item = store.claim_next_item(batch_id, "worker-1", lease_seconds=60)
        assert item["status"] == WorkItemStatus.RUNNING.value
        assert item["attempt_count"] == 1
        result_id = store.upsert_result(
            batch_id,
            item["id"],
            item["target"],
            "completed",
            "done",
            {"findings": []},
            item["target"],
            item["target"],
            [],
            "task1",
            [],
        )
        store.complete_item(item["id"], result_id)
        progress = store.progress(batch_id)
        assert progress["completed"] == 1
        assert progress["pending"] == 1
        assert store.get_item(item["id"])["result_id"] == result_id
    finally:
        store.close()


def test_bounded_retry_then_final_failure(tmp_path):
    store = BatchStore(tmp_path / "batch.db")
    policy = BatchPolicy(work_item_max_attempts=2)
    try:
        batch_id = store.create_batch("goal", ["http://a.test"], ResultContract(), policy, "b1")
        first = store.claim_next_item(batch_id, "worker", policy.lease_seconds)
        store.fail_item(first["id"], FailureCategory.TIMEOUT.value, "timeout", retryable=first["attempt_count"] < policy.work_item_max_attempts)
        assert store.get_item(first["id"])["status"] == WorkItemStatus.FAILED_RETRYABLE.value
        assert store.requeue_retryable(batch_id) == 1
        second = store.claim_next_item(batch_id, "worker", policy.lease_seconds)
        store.fail_item(second["id"], FailureCategory.TIMEOUT.value, "timeout", retryable=second["attempt_count"] < policy.work_item_max_attempts)
        failed = store.get_item(second["id"])
        assert failed["attempt_count"] == 2
        assert failed["status"] == WorkItemStatus.FAILED_FINAL.value
        assert store.progress(batch_id)["status"] == "completed_with_failures"
    finally:
        store.close()


def test_result_upsert_is_idempotent(tmp_path):
    store = BatchStore(tmp_path / "batch.db")
    try:
        batch_id = store.create_batch("goal", ["http://a.test"], ResultContract(), BatchPolicy(), "b1")
        item = store.claim_next_item(batch_id, "worker", 60)
        args = (
            batch_id,
            item["id"],
            item["target"],
            "completed",
            "summary",
            {"findings": [{"title": "A", "value": "Due Sep 14"}]},
            item["target"],
            item["target"],
            [{"evidence": "Assignment A due Sep 14"}],
            "task1",
            [3],
        )
        one = store.upsert_result(*args)
        two = store.upsert_result(*args)
        assert one == two
        assert len(store.results(batch_id)) == 1
    finally:
        store.close()


def test_domain_and_read_only_policies():
    assert normalize_target_url("HTTPS://Example.COM/path/#section") == "https://example.com/path"
    assert in_navigation_scope("https://school.example.edu/a", "https://school.example.edu/b", scope=NavigationScope.SAME_ORIGIN)
    assert not in_navigation_scope("https://school.example.edu/a", "https://other.example.edu/b", scope=NavigationScope.SAME_ORIGIN)
    harmless = ModelDecision(action=ActionType.CLICK, target=1)
    consequential = ModelDecision(action=ActionType.CLICK, target=1)
    assert read_only_allows(harmless, "Expand details")
    assert not read_only_allows(consequential, "Submit Assignment")


def test_runtime_policy_blocks_consequential_read_only_action():
    policy = BatchRuntimePolicy(target_url="https://school.example.edu/course/1", read_only=True)
    decision = ModelDecision(action=ActionType.CLICK, target=1)
    element = ElementRef(
        id=1,
        role="button",
        name="Submit Assignment",
        selector_hint=SelectorHint(css="button", nth=0),
    )
    violation = pre_action_violation(policy, decision, element)
    assert violation is not None
    assert violation.category == "READ_ONLY_BLOCKED"


def test_runtime_policy_allows_harmless_read_only_interactions():
    policy = BatchRuntimePolicy(target_url="https://school.example.edu/course/1", read_only=True)
    decision = ModelDecision(action=ActionType.CLICK, target=1)
    element = ElementRef(
        id=1,
        role="button",
        name="Expand details",
        selector_hint=SelectorHint(css="button", nth=0),
    )
    assert pre_action_violation(policy, decision, element) is None


def test_runtime_policy_blocks_cross_origin_open_url():
    policy = BatchRuntimePolicy(
        target_url="https://school.example.edu/course/1",
        navigation_scope=NavigationScopePolicy.SAME_ORIGIN,
    )
    decision = ModelDecision(action=ActionType.OPEN_URL, params={"url": "https://evil.example.com/"})
    violation = pre_action_violation(policy, decision, None)
    assert violation is not None
    assert violation.category == "SCOPE_BLOCKED"


def test_runtime_policy_scope_modes():
    source = "https://school.example.edu/course/1"
    assert url_in_scope(source, "https://school.example.edu/course/2", NavigationScopePolicy.SAME_ORIGIN)
    assert not url_in_scope(source, "https://portal.example.edu/course/2", NavigationScopePolicy.SAME_ORIGIN)
    assert url_in_scope(source, "https://portal.example.edu/course/2", NavigationScopePolicy.SAME_DOMAIN)
    assert url_in_scope(source, "https://external.test/path", NavigationScopePolicy.UNRESTRICTED)
    violation = post_navigation_violation(
        BatchRuntimePolicy(target_url=source, navigation_scope=NavigationScopePolicy.SAME_ORIGIN),
        "https://portal.example.edu/course/2",
    )
    assert violation is not None
    assert violation.category == "SCOPE_BLOCKED"
