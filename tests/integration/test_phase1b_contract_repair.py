from __future__ import annotations

from memory.event_store import EventType
from tests.integration.fake_llama import decision
from tests.integration.helpers import read_events


async def test_contract_repair_retries_once_for_missing_target(make_agent_loop, fixture_site_url):
    loop = make_agent_loop("Open Products from the home page", ["Products"], [
        decision("open_url", params={"url": fixture_site_url + "/index.html"}),
        '{"action":"click"}',
        '{"action":"click","target":1}',
        decision("finish", params={"result": "Products page is visible"}),
    ])
    state = await loop.run(max_steps=5)
    assert state.status == "completed"

    events = read_events(loop)
    invalid_decisions = [
        e for e in events
        if e.type == EventType.MODEL_DECISION and e.payload.get("contract_error")
    ]
    assert len(invalid_decisions) == 1
    assert invalid_decisions[0].payload["repair_attempted"] is True

    action_intents = [e for e in events if e.type == EventType.ACTION_INTENT]
    assert any(e.payload["action"] == "click" and e.payload["target"] == 1 for e in action_intents)
