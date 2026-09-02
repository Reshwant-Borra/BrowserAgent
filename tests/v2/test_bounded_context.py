"""The prompt must not grow with the run (V2 hardening §18/§19/§25), and the trace must be
able to reconstruct it (§20).

Grounding was the easy thing to buy with context: pour the whole evidence ledger into every
prompt and every claim is citable. That trade is what these tests exist to prevent.
"""
from __future__ import annotations

import json

import pytest

from agent.token_budget import count_tokens
from agent_v2.agent import LoopLimits

pytestmark = pytest.mark.asyncio

#: Fixture pages with enough distinct content to make a long run meaningful.
PAGES = ["index.html", "products.html", "docs.html", "settings.html", "search.html",
         "ground_hub.html", "ground_a.html", "ground_b.html", "candidates_hub.html",
         "candidate_alpha.html", "candidate_beta.html", "candidate_gamma.html",
         "alpha_detail.html", "beta_detail.html", "long_research.html", "long_config.html"]


async def test_a_long_run_does_not_grow_its_prompt(backend, make_agent, fixture_site_url):
    """Sixteen navigations, each recording a fact, then a finish. The last prompt must be no
    larger than the early ones — the evidence block is a bounded selection, not an archive."""
    script = []
    for index, page in enumerate(PAGES):
        script.append({
            "action": "open_url", "url": f"{fixture_site_url}/{page}", "reason": f"step {index}",
            "state_updates": {
                "add_facts": [f"page {index} has something worth keeping on it"],
                "pending": [f"look at page {index + 1}"] if index == 0 else [],
            },
        })
    script.append({"action": "finish", "answer": "Visited every page in the set.",
                   "reason": "done",
                   "claims": [{"text": "I opened every page in the set", "kind": "meta"}]})

    agent, client = make_agent(backend, script, limits=LoopLimits(max_steps=25))
    state = await agent.run("Walk the whole fixture site and note something from each page.")

    prompts = client.decision_prompts
    assert state.step >= 15, f"only reached step {state.step}"
    assert len(prompts) >= 15

    tokens = [count_tokens(p) for p in prompts]
    early = max(tokens[3:6])
    late = max(tokens[-3:])
    # Bounded, not merely sublinear: the tail must not exceed the early plateau by more than
    # the natural variation between one page's rendering and another's.
    assert late <= early * 1.25, f"prompt grew from {early} to {late} tokens over {len(tokens)} steps"
    assert max(tokens) <= agent.budget.max_total_tokens * 1.2, f"peak prompt {max(tokens)} tokens"

    # …while the ledger behind it did grow. Storage is allowed to; the prompt is not.
    assert len(agent.ledger.records) >= 10
    assert len(agent.ledger.resources) >= 12


async def test_the_evidence_block_is_a_selection_not_the_whole_ledger(
        backend, make_agent, fixture_site_url):
    script = []
    for index, page in enumerate(PAGES):
        script.append({"action": "open_url", "url": f"{fixture_site_url}/{page}",
                       "reason": "walk",
                       "state_updates": {"add_facts": [
                           f"page {index} has content worth keeping"]}})
    script.append({"action": "finish", "answer": "done walking", "reason": "done"})

    agent, client = make_agent(backend, script, limits=LoopLimits(max_steps=25))
    await agent.run("Walk the site.")

    last = client.decision_prompts[-1]
    block = last.split("EVIDENCE YOU MAY CITE", 1)[1].split("\n\n", 1)[0]
    shown = [line for line in block.splitlines() if line.startswith("- ev_")]
    assert 0 < len(shown) <= agent.evidence_top_k
    assert len(shown) < len(agent.ledger.records)
    assert count_tokens(block) <= agent.budget.limit("evidence") * 2


async def test_the_trace_can_reconstruct_a_step(backend, make_agent, fixture_site_url, tmp_path):
    """V2 hardening §20. Everything needed to explain one step is in the existing JSONL —
    no second logging system was added for grounding."""
    task_dir = tmp_path / "traced"
    agent, client = make_agent(backend, [
        {"action": "open_url", "url": f"{fixture_site_url}/ground_a.html", "reason": "A"},
        {"action": "open_url", "url": f"{fixture_site_url}/ground_b.html", "reason": "B",
         "state_updates": {"add_facts": ["Widget A costs $159.00", "Widget C costs $42.50"]}},
        {"action": "compute", "operation": "subtract", "operands": ["159.00", "89.00"],
         "labels": ["A", "B"], "reason": "difference",
         "state_updates": {"add_facts": ["Widget B costs $89.00"]}},
        {"action": "finish", "reason": "done",
         "answer": "Widget A costs $159.00, Widget B costs $89.00, a difference of $70.",
         "claims": [{"text": "the difference is $70", "evidence_ids": ["3"], "kind": "derived"}]},
    ], task_dir=task_dir)
    await agent.run("Compare Widget A and Widget B.")

    events = [json.loads(line) for line in
              (task_dir / "steps.jsonl").read_text(encoding="utf-8").splitlines()]
    kinds = {e["event"] for e in events}
    for required in ("decision", "step", "evidence", "evidence_rejected", "compute",
                     "grounding", "source_coverage", "finished"):
        assert required in kinds, f"{required} missing from the trace"

    evidence = [e for e in events if e["event"] == "evidence"]
    assert all(e["evidence_id"].startswith("ev_") and e["source"] for e in evidence)

    compute = [e for e in events if e["event"] == "compute"][0]
    assert compute["operation"] == "subtract" and compute["ok"] is True
    assert compute["result"].endswith("= 70")

    grounding = [e for e in events if e["event"] == "grounding"][0]
    assert grounding["claims"][0]["kind"] == "derived"
    assert grounding["supported_claims"] == 1

    coverage = [e for e in events if e["event"] == "source_coverage"][0]
    assert coverage["visited"] and coverage["with_evidence"]

    # the rejected fabrication is recorded as rejected, with the reason it failed but not
    # the sentence itself — the trace explains the decision without republishing the claim
    rejected = [e for e in events if e["event"] == "evidence_rejected"]
    assert rejected
    assert "Widget C costs" not in json.dumps(rejected)
    assert "not on this page" in rejected[0]["reason"]

    # the ledger is on disk beside the state, so a finished run is auditable afterwards
    ledger = json.loads((task_dir / "evidence.json").read_text(encoding="utf-8"))
    assert ledger["task_id"] and ledger["resources"] and ledger["evidence"]


async def test_a_resumed_task_keeps_the_evidence_it_had_already_collected(
        backend, make_agent, fixture_site_url, tmp_path):
    task_dir = tmp_path / "resumed"
    agent, _ = make_agent(backend, [
        {"action": "open_url", "url": f"{fixture_site_url}/ground_a.html", "reason": "A"},
        {"action": "need_user", "message": "please confirm", "reason": "pause",
         "state_updates": {"add_facts": ["Widget A costs $159.00"]}},
    ], task_dir=task_dir, takeover=None)
    paused = await agent.run("What does Widget A cost?", task_id="resume-me")
    assert paused.status == "waiting_for_user"
    minted = len(agent.ledger.records)
    assert minted >= 1

    from agent_v2.state import TaskState
    reloaded = TaskState.load(task_dir / "state.json")
    agent2, client2 = make_agent(backend, [
        {"action": "finish", "answer": "Widget A costs $159.00.", "reason": "done",
         "claims": [{"text": "Widget A costs $159.00", "evidence_ids": ["1"],
                     "kind": "source"}]},
    ], task_dir=task_dir)
    resumed = await agent2.resume(reloaded)

    assert len(agent2.ledger.records) >= minted        # the ledger came back off disk
    assert resumed.unsupported_claims == []            # so the citation still resolves
    assert "NOT VERIFIED" not in resumed.answer


async def test_a_ledger_belonging_to_another_task_is_not_adopted(
        backend, make_agent, fixture_site_url, tmp_path):
    """V2 hardening §17. A stale `evidence.json` in a reused directory must not become this
    task's evidence."""
    task_dir = tmp_path / "shared"
    agent, _ = make_agent(backend, [
        {"action": "open_url", "url": f"{fixture_site_url}/ground_a.html", "reason": "A"},
        {"action": "need_user", "message": "pause", "reason": "pause",
         "state_updates": {"add_facts": ["Widget A costs $159.00"]}},
    ], task_dir=task_dir, takeover=None)
    await agent.run("What does Widget A cost?", task_id="task-one")

    stolen = next(iter(agent.ledger.records))

    from agent_v2.state import TaskState
    other = TaskState(task_id="task-two", goal="Something else entirely")
    # Stated until the loop stops challenging; the surplus entries are never read.
    finish = {"action": "finish", "answer": "Widget A costs $159.00.", "reason": "done",
              "claims": [{"text": "Widget A costs $159.00", "evidence_ids": [stolen],
                          "kind": "source"}]}
    agent2, _ = make_agent(backend, [dict(finish) for _ in range(6)], task_dir=task_dir)
    resumed = await agent2.resume(other)

    assert agent2.ledger.records == {}, "task-two adopted task-one's ledger"
    assert stolen in " ".join(resumed.unsupported_claims)
    assert "different task" in " ".join(resumed.unsupported_claims)
