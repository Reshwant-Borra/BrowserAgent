"""JobRunner integration tests (Section 29/76): submit -> route -> run -> complete, stop,
approval accept/deny, and manual-login wait/resume — driven directly against JobRunner
(no HTTP layer) so async coordination stays in a single event loop. `tests/integration/
test_ui_app.py` covers the HTTP wiring on top of this with the same deterministic client.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import re

import pytest

from agent.schemas import RiskLevel, classify_risk
from inference.llama_client import CompletionResult
from router.extract import extract_urls
from ui.jobs import JobRunner
from ui.store import UIJobStore

# No module-level `pytestmark = pytest.mark.asyncio`: pyproject.toml sets
# `asyncio_mode = "auto"`, which already collects async defs correctly, and this file (unlike
# its sibling integration tests) mixes in one sync test (`test_classify_risk_...` below) that
# would otherwise get spuriously marked.


class FakeUIClient:
    """Stands in for the local model across both the router's fallback path (only reached
    when json_schema is set) and the agent decision loop (grammar is always set, since
    AgentLoop always loads inference/grammar/action.gbnf). One finish decision by default;
    `script` overrides for multi-step scenarios."""

    endpoint = "fake://ui"

    def __init__(self, script: list | None = None, finish_result: str = '{"relevant": true, "summary": "ok", "findings": []}'):
        self.script = list(script) if script is not None else None
        self.finish_result = finish_result
        self.calls = 0

    async def health_check(self) -> bool:
        return True

    async def complete(self, prompt, grammar=None, max_tokens=256, json_schema=None) -> CompletionResult:
        self.calls += 1
        if grammar is None:
            # Only reached if a prompt couldn't be routed deterministically; none of these
            # tests exercise that path (every prompt carries an explicit fixture URL).
            raise AssertionError("router model fallback should not be needed for these prompts")
        if self.script:
            action = self.script.pop(0)
        elif "about:blank" in prompt:
            # _handle_finish (agent/loop.py) refuses to finish with zero prior verified
            # actions, so the default (no explicit script) two-step shape always opens the
            # target first with an assertion that will actually pass against the local
            # fixture server, then finishes on the next call. This client is shared across
            # every child AgentLoop a batch/workflow spawns (one JobRunner client factory for
            # the whole job), so "first call ever" is the wrong signal for "haven't navigated
            # yet" — a global self.calls counter doesn't reset per child task. Whether the
            # rendered page-state block still shows the blank starting page does, since it's
            # derived from each task's own fresh observation.
            #
            # inference/prompt.py's SYSTEM_BLOCK is STATIC and always comes first (prefix-
            # stability, ARCHITECTURE.md) and itself contains a "http://example.com" example
            # in the output-contract illustration; the real goal/target URL is injected later
            # in the semi-stable/volatile sections. A batch child's goal (batch/orchestrator.py
            # _child_goal) also repeats the whole batch objective (which lists every target)
            # ahead of its own "Target: <url>" line, so even "last URL in the prompt" isn't
            # reliably that child's specific target. Prefer whatever immediately follows a
            # "Target:" marker; only fall back to the last URL in the prompt (still better
            # than the first, static SYSTEM_BLOCK example) when there is no such marker.
            target_match = re.search(r"Target:\s*(\S+)", prompt)
            if target_match:
                url = target_match.group(1)
            else:
                urls = extract_urls(prompt)
                url = urls[-1] if urls else "http://127.0.0.1"
            action = {"action": "open_url", "target": None, "params": {"url": url},
                      "expected_result": {"url_contains": "127.0.0.1"}, "confidence": 0.9}
        else:
            action = {"action": "finish", "target": None, "params": {"result": self.finish_result},
                      "expected_result": {}, "confidence": 0.9}
        return CompletionResult(text=json.dumps(action), total_latency_ms=1.0)


def _patch_model(monkeypatch, client):
    monkeypatch.setattr("agent.loop.create_inference_client", lambda config: client)
    monkeypatch.setattr("ui.jobs.create_inference_client", lambda config: client)


async def _wait_for(store: UIJobStore, job_id: str, statuses: set[str], timeout: float = 10.0) -> dict:
    elapsed = 0.0
    while elapsed < timeout:
        job = store.get(job_id)
        if job and job["status"] in statuses:
            return job
        await asyncio.sleep(0.05)
        elapsed += 0.05
    raise AssertionError(f"job {job_id} did not reach {statuses} within {timeout}s (last: {store.get(job_id)})")


async def test_single_site_job_completes(tmp_config, tmp_path, fixture_site_url, monkeypatch):
    _patch_model(monkeypatch, FakeUIClient(finish_result="the page is a fixture index"))
    store = UIJobStore(tmp_path / "ui_jobs.db")
    runner = JobRunner(tmp_config, store, tmp_path / "runtime")
    job_id = runner.submit(f"Open {fixture_site_url}/index.html and tell me what this is.")
    job = await _wait_for(store, job_id, {"completed", "failed"})
    assert job["status"] == "completed"
    assert job["kind"] == "single_site"
    assert "fixture index" in job["final_result"]["summary"]
    # Regression guard for the "picked up the SYSTEM_BLOCK's example.com instead of the real
    # target" bug class: confirm the task actually navigated to the fixture host, not away.
    from memory.event_store import EventStore, EventType
    from pathlib import Path as _P
    es = EventStore(_P(tmp_config.storage.tasks_dir) / job["task_id"] / "task.db")
    try:
        observations = [e for e in es.all_events(job["task_id"]) if e.type == EventType.OBSERVATION]
        assert any(fixture_site_url in (e.payload.get("url") or "") for e in observations)
    finally:
        es.close()


async def test_sweep_job_completes(tmp_config, tmp_path, fixture_site_url, monkeypatch):
    _patch_model(monkeypatch, FakeUIClient())
    store = UIJobStore(tmp_path / "ui_jobs.db")
    runner = JobRunner(tmp_config, store, tmp_path / "runtime")
    prompt = f"Check {fixture_site_url}/index.html and {fixture_site_url}/docs.html for anything relevant."
    job_id = runner.submit(prompt)
    job = await _wait_for(store, job_id, {"completed", "failed"}, timeout=15.0)
    assert job["status"] == "completed"
    assert job["kind"] == "multisite_sweep"
    assert job["final_result"]["item_count"] == 2
    assert job["final_result"]["completed"] == 2


async def test_stop_mid_job_leaves_state_clean(tmp_config, tmp_path, fixture_site_url, monkeypatch):
    gate = asyncio.Event()

    class GatedClient(FakeUIClient):
        async def complete(self, prompt, grammar=None, max_tokens=256, json_schema=None):
            if self.calls == 0:
                self.calls += 1
                # A real, verifiable first action (not an empty-assertion scroll): if this
                # doesn't record a "pass", _handle_finish (agent/loop.py) rejects the later
                # gated "finish" forever, and the task grinds through the whole recovery
                # ladder for up to 200 real steps before naturally landing on "failed" —
                # technically not a hang, but far too slow for a test that's supposed to
                # verify a *prompt* stop.
                return CompletionResult(text=json.dumps(
                    {"action": "open_url", "target": None, "params": {"url": f"{fixture_site_url}/index.html"},
                     "expected_result": {"url_contains": "127.0.0.1"}, "confidence": 0.9}), total_latency_ms=1.0)
            await gate.wait()
            return CompletionResult(text=json.dumps(
                {"action": "finish", "target": None, "params": {"result": "done"},
                 "expected_result": {}, "confidence": 0.9}), total_latency_ms=1.0)

    _patch_model(monkeypatch, GatedClient())
    store = UIJobStore(tmp_path / "ui_jobs.db")
    runner = JobRunner(tmp_config, store, tmp_path / "runtime")
    job_id = runner.submit(f"Open {fixture_site_url}/index.html and scroll then finish.")

    # Wait for the first (open_url) step to actually land, rather than a fixed sleep guess —
    # avoids a race where stop()/gate.set() fire before the browser has even started.
    elapsed = 0.0
    while elapsed < 10.0:
        job = store.get(job_id)
        if job and job.get("activity", "").startswith("Step 1"):
            break
        await asyncio.sleep(0.05)
        elapsed += 0.05
    else:
        raise AssertionError(f"first step never landed (last: {store.get(job_id)})")

    stopped = runner.stop(job_id)
    assert stopped
    gate.set()  # release the in-flight (gated) call so the driver can observe the stop flag

    job = await _wait_for(store, job_id, {"stopped", "completed", "failed"}, timeout=10.0)
    assert job["status"] == "stopped"


async def test_approval_flow_approve(tmp_config, tmp_path, fixture_site_url, monkeypatch):
    approval_config = dataclasses.replace(
        tmp_config, browser=dataclasses.replace(tmp_config.browser, interactive_approval=True)
    )
    script = [
        {"action": "open_url", "target": None, "params": {"url": f"{fixture_site_url}/wizard_confirm.html"},
         "expected_result": {"url_contains": "wizard_confirm"}, "confidence": 0.9},
        {"action": "click", "target": 1, "params": {}, "expected_result": {"page_contains": "Submitted"}, "confidence": 0.9},
        {"action": "finish", "target": None, "params": {"result": "submitted"}, "expected_result": {}, "confidence": 0.9},
    ]
    _patch_model(monkeypatch, FakeUIClient(script=script))
    store = UIJobStore(tmp_path / "ui_jobs.db")
    runner = JobRunner(approval_config, store, tmp_path / "runtime")
    job_id = runner.submit(f"Open {fixture_site_url}/wizard_confirm.html and submit the application.")

    job = await _wait_for(store, job_id, {"waiting_for_approval"})
    assert job["pending_approval"]["action"] == "click"
    assert "submit" in (job["pending_approval"]["target"] or "").lower()

    assert runner.approve(job_id, True)
    job = await _wait_for(store, job_id, {"completed", "failed"})
    assert job["status"] == "completed"


async def test_approval_flow_deny_blocks_task(tmp_config, tmp_path, fixture_site_url, monkeypatch):
    approval_config = dataclasses.replace(
        tmp_config, browser=dataclasses.replace(tmp_config.browser, interactive_approval=True)
    )
    script = [
        {"action": "open_url", "target": None, "params": {"url": f"{fixture_site_url}/wizard_confirm.html"},
         "expected_result": {"url_contains": "wizard_confirm"}, "confidence": 0.9},
        {"action": "click", "target": 1, "params": {}, "expected_result": {"page_contains": "Submitted"}, "confidence": 0.9},
    ]
    _patch_model(monkeypatch, FakeUIClient(script=script))
    store = UIJobStore(tmp_path / "ui_jobs.db")
    runner = JobRunner(approval_config, store, tmp_path / "runtime")
    job_id = runner.submit(f"Open {fixture_site_url}/wizard_confirm.html and submit the application.")

    await _wait_for(store, job_id, {"waiting_for_approval"})
    assert runner.approve(job_id, False)
    job = await _wait_for(store, job_id, {"completed", "failed"})
    assert job["status"] == "failed"
    assert "declined" in (job["error"] or "").lower()


async def test_manual_login_wait_and_continue(tmp_config, tmp_path, fixture_site_url, monkeypatch):
    script = [
        # _handle_finish (agent/loop.py) refuses to finish with zero prior verified actions,
        # so this needs a real (passing) assertion, not an empty one, even though this step's
        # own result doesn't matter for the scenario being tested.
        {"action": "open_url", "target": None, "params": {"url": f"{fixture_site_url}/workflow_login_required.html"},
         "expected_result": {"url_contains": "127.0.0.1"}, "confidence": 0.9},
        # a login page is observed next -> the driver pauses here without any model call
        {"action": "extract", "target": None, "params": {}, "expected_result": {}, "confidence": 0.9},
        {"action": "finish", "target": None, "params": {"result": "account status active"}, "expected_result": {}, "confidence": 0.9},
    ]
    _patch_model(monkeypatch, FakeUIClient(script=script))
    store = UIJobStore(tmp_path / "ui_jobs.db")
    runner = JobRunner(tmp_config, store, tmp_path / "runtime")
    job_id = runner.submit(f"Open {fixture_site_url}/index.html then go sign in and report the account status.")

    job = await _wait_for(store, job_id, {"waiting_for_login"}, timeout=10.0)
    assert "login" in job["activity"].lower()

    # Stand-in for "the user takes a moment to type credentials in the visible browser
    # window" — the fixture auto-transitions past its login form after ~1.5s (see
    # workflow_login_required.html) so the next observation no longer looks like a login page.
    await asyncio.sleep(2.0)
    assert runner.login_continue(job_id)
    job = await _wait_for(store, job_id, {"completed", "failed"}, timeout=10.0)
    assert job["status"] == "completed"


def test_classify_risk_flags_submit_button_consequential():
    """Sanity check the fixture actually exercises the approval gate this suite relies on."""
    from agent.schemas import ActionType
    assert classify_risk(ActionType.CLICK, "Submit Application") == RiskLevel.CONSEQUENTIAL
