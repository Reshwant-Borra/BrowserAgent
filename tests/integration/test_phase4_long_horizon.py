from __future__ import annotations

from pathlib import Path

from memory.event_store import EventType
from memory.task_state import TaskStateStore
from tests.integration.fake_llama import decision
from tests.integration.helpers import read_events


async def test_verified_download_auto_completes_without_repeating(make_agent_loop, fixture_site_url):
    loop = make_agent_loop(
        "Download the sample.txt file and finish after confirming the download.",
        ["sample.txt"],
        [
            decision("open_url", params={"url": fixture_site_url + "/download.html"}),
            decision("download", target=1),
            decision("download", target=1),
        ],
    )

    state = await loop.run(max_steps=5)
    events = read_events(loop)

    assert state.status == "completed"
    download_intents = [
        event for event in events
        if event.type == EventType.ACTION_INTENT and event.payload.get("action") == "download"
    ]
    assert len(download_intents) == 1
    completed = next(event for event in events if event.type == EventType.TASK_COMPLETED)
    assert completed.payload["auto_completed_after_verified_download"] is True


async def test_long_horizon_context_reconstructs_after_summary_events(make_agent_loop, fixture_site_url):
    script = [decision("open_url", params={"url": fixture_site_url + "/products.html"})]
    script.extend(decision("wait", params={"ms": 1}) for _ in range(8))
    script.append(decision("finish", params={"result": "done"}))
    loop = make_agent_loop(
        "Open products, wait through a long workflow, and finish.",
        ["Products"],
        script,
    )
    loop.config.context.recent_actions = 3
    loop.config.context.summary_tokens = 160

    state = await loop.run(max_steps=12)
    events = read_events(loop)

    assert state.status == "completed"
    assert any(event.type == EventType.COMPACTION_COMMITTED for event in events)
    reopened = loop.event_store.__class__(Path(loop.config.storage.tasks_dir) / loop.task_id / "task.db")
    try:
        reloaded = TaskStateStore(reopened).load(loop.task_id)
        assert reloaded.last_event_id == reopened.max_event_id(loop.task_id)
        assert len(reloaded.recent_actions) >= 3
    finally:
        reopened.close()
