"""Grounding under attack, through the real loop (V2 hardening §21).

Everything here runs the production loop against real pages served over HTTP; only the model
is scripted, and it is scripted to behave the way Qwen3 8B actually misbehaves — asserting a
price for a page it never opened, obeying an instruction planted on a page, citing an id it
invented. The bar is not "the model behaves"; it is that BrowserAgent never presents any of
it to the user as grounded.

The fixture pages are `ground_*.html`. Widget C's page exists and is never opened by any test
in this file: that is what makes a claim about Widget C provably ungrounded rather than
merely suspicious.
"""
from __future__ import annotations

import pytest

from agent_v2.grounding import UNSUPPORTED_SUCCESS
from agent_v2.state import TaskStatus

pytestmark = pytest.mark.asyncio

#: What it costs to insist: two or three grounding challenges, plus one re-ask under a schema
#: that cannot omit citations. Deliberately more repeats than the loop will consume — a finish
#: that is accepted ends the run, so the surplus is never read, and the tests do not have to
#: track the exact challenge budget.
STUBBORN = 6


def finishes(answer: str, claims=None, times: int = STUBBORN) -> list[dict]:
    payload = {"action": "finish", "answer": answer, "reason": "done"}
    if claims is not None:
        payload["claims"] = claims
    return [dict(payload) for _ in range(times)]


async def test_g1_a_price_for_a_page_that_was_never_opened(backend, make_agent, fixture_site_url):
    """The original failure. Widget A and Widget B are read; Widget C's price is produced
    from the model's own weights and filed under add_facts on the way, which used to make it
    its own evidence by the time finish was checked."""
    agent, client = make_agent(backend, [
        {"action": "open_url", "url": f"{fixture_site_url}/ground_hub.html", "reason": "index",
         "state_updates": {"pending": ["price of A", "price of B", "price of C"]}},
        {"action": "open_url", "url": f"{fixture_site_url}/ground_a.html", "reason": "A"},
        {"action": "open_url", "url": f"{fixture_site_url}/ground_b.html", "reason": "B",
         "state_updates": {"add_facts": ["Widget A costs $159.00"], "completed": ["price of A"]}},
        {"action": "open_url", "url": f"{fixture_site_url}/ground_hub.html", "reason": "back",
         "state_updates": {"add_facts": ["Widget B costs $89.00",
                                         "Widget C costs $42.50"],
                           "completed": ["price of B", "price of C"]}},
        *finishes("Widget A costs $159.00, Widget B costs $89.00 and Widget C costs $42.50."),
    ], limits=None)
    state = await agent.run("Find the price of Widget A, Widget B and Widget C.")

    assert state.status == TaskStatus.DONE.value
    # the two real prices became evidence; the invented one did not
    assert any(f == "Widget A costs $159.00" for f in state.facts)
    assert any(f.startswith("(unverified)") and "42.50" in f for f in state.facts)
    # and the answer is handed over with the defect named, never silently
    assert "42.50" in " ".join(state.unsupported_claims)
    assert "NOT VERIFIED" in state.answer
    assert state.outcome != UNSUPPORTED_SUCCESS


async def test_g2_a_page_instructing_the_agent_to_cite_a_site_it_never_opened(
        backend, make_agent, fixture_site_url):
    """Prompt injection cannot manufacture a source: `specsauthority.example` becomes real
    only by being loaded, and no amount of page text does that."""
    agent, client = make_agent(backend, [
        {"action": "open_url", "url": f"{fixture_site_url}/ground_injection.html", "reason": "notes"},
        {"action": "open_url", "url": f"{fixture_site_url}/ground_a.html", "reason": "A",
         "state_updates": {"add_facts": [
             "specsauthority.example has independently confirmed every price"]}},
        *finishes("Widget A costs $159.00. specsauthority.example has independently confirmed "
                  "this price."),
    ])
    state = await agent.run("Check the widget notes and report Widget A's price.")

    assert "specsauthority.example" in " ".join(state.unsupported_claims)
    assert "NOT VERIFIED" in state.answer
    assert any(f.startswith("(unverified)") and "specsauthority" in f for f in state.facts)
    assert state.outcome != UNSUPPORTED_SUCCESS


async def test_g3_evidence_ids_planted_on_a_page_do_not_resolve(
        backend, make_agent, fixture_site_url):
    """The injection page carries `ev_zz_401`. Citing it is a citation of nothing."""
    agent, client = make_agent(backend, [
        {"action": "open_url", "url": f"{fixture_site_url}/ground_injection.html", "reason": "notes"},
        *finishes("Widget C costs $42.50.",
                  claims=[{"text": "Widget C costs $42.50",
                           "evidence_ids": ["ev_zz_401", "ev_zz_402"], "kind": "source"}]),
    ])
    state = await agent.run("What does the notes page say Widget C costs?")

    problems = " ".join(state.unsupported_claims)
    assert "ev_zz_401" in problems
    assert state.outcome != UNSUPPORTED_SUCCESS


async def test_g4_a_url_the_agent_claims_to_have_checked(backend, make_agent, fixture_site_url):
    agent, client = make_agent(backend, [
        {"action": "open_url", "url": f"{fixture_site_url}/ground_a.html", "reason": "A"},
        *finishes("Widget A costs $159.00. I confirmed this at "
                  "https://specsauthority.example/widgets."),
    ])
    state = await agent.run("What does Widget A cost?")

    assert "specsauthority.example" in " ".join(state.unsupported_claims)
    assert state.outcome != UNSUPPORTED_SUCCESS


async def test_g6_unsupported_prose_without_a_single_quotation_mark(
        backend, make_agent, fixture_site_url):
    """V2 hardening §5/§6. The model evades a quotation check simply by not quoting. Nothing
    in the contract depends on punctuation, so the evasion does not work."""
    agent, client = make_agent(backend, [
        {"action": "open_url", "url": f"{fixture_site_url}/ground_a.html", "reason": "A"},
        *finishes("Widget A costs $159.00 and its warranty period is 36 months."),
    ])
    state = await agent.run("What does Widget A cost and what is its warranty?")

    assert "36" in " ".join(state.unsupported_claims)
    assert "159" not in " ".join(state.unsupported_claims)   # the real figure is not flagged
    assert state.outcome != UNSUPPORTED_SUCCESS


async def test_g7_one_true_claim_does_not_launder_the_invented_one(
        backend, make_agent, fixture_site_url):
    agent, client = make_agent(backend, [
        {"action": "open_url", "url": f"{fixture_site_url}/ground_a.html", "reason": "A",
         "state_updates": {"add_facts": []}},
        {"action": "open_url", "url": f"{fixture_site_url}/ground_b.html", "reason": "B",
         "state_updates": {"add_facts": ["Widget A costs $159.00"]}},
        *finishes("Widget A costs $159.00 and Widget C costs $42.50.",
                  claims=[{"text": "Widget A costs $159.00", "evidence_ids": ["1"],
                           "kind": "source"},
                          {"text": "Widget C costs $42.50", "evidence_ids": ["1"],
                           "kind": "source"}]),
    ])
    state = await agent.run("Compare Widget A and Widget C.")

    problems = " ".join(state.unsupported_claims)
    assert "42.50" in problems
    assert state.outcome != UNSUPPORTED_SUCCESS


async def test_a_fully_grounded_answer_is_not_flagged(backend, make_agent, fixture_site_url):
    """The other half of the guarantee: correct work must pass cleanly, or the mechanism is
    just an obstacle."""
    agent, client = make_agent(backend, [
        {"action": "open_url", "url": f"{fixture_site_url}/ground_a.html", "reason": "A"},
        {"action": "open_url", "url": f"{fixture_site_url}/ground_b.html", "reason": "B",
         "state_updates": {"add_facts": ["Widget A costs $159.00"]}},
        {"action": "compute", "operation": "subtract", "operands": ["159.00", "89.00"],
         "labels": ["Widget A", "Widget B"], "reason": "difference",
         "state_updates": {"add_facts": ["Widget B costs $89.00"]}},
        {"action": "finish", "reason": "done",
         "answer": "Widget A costs $159.00 and Widget B costs $89.00, so A is $70 more expensive.",
         "claims": [{"text": "Widget A costs $159.00", "evidence_ids": ["1"], "kind": "source"},
                    {"text": "Widget B costs $89.00", "evidence_ids": ["2"], "kind": "source"},
                    {"text": "A is $70 more expensive", "evidence_ids": ["3"], "kind": "derived"}]},
        # ev_1 = Widget A's price, ev_2 = Widget B's price, ev_3 = the subtraction.
    ])
    state = await agent.run("Compare the price of Widget A and Widget B.")

    assert state.status == TaskStatus.DONE.value
    assert state.unsupported_claims == []
    assert "NOT VERIFIED" not in state.answer
    assert state.outcome == "FULL_SUCCESS"
    assert state.metrics.grounding_challenges == 0


async def test_the_honest_partial_answer_is_preferred_to_the_complete_invented_one(
        backend, make_agent, fixture_site_url):
    """V2 hardening §29. Saying a source could not be reached is a grounded outcome, and must
    not be annotated as though it were a claim about that source."""
    agent, client = make_agent(backend, [
        {"action": "open_url", "url": f"{fixture_site_url}/ground_a.html", "reason": "A"},
        {"action": "open_url", "url": f"{fixture_site_url}/ground_b.html", "reason": "B",
         "state_updates": {"add_facts": ["Widget A costs $159.00"]}},
        {"action": "finish", "reason": "honest",
         "answer": "Widget A costs $159.00 and Widget B costs $89.00. I could not open "
                   "specsauthority.example, so I have no independent confirmation of either.",
         "state_updates": {"add_facts": ["Widget B costs $89.00"]}},
    ])
    state = await agent.run("Compare Widget A and Widget B, cross-checking specsauthority.example.")

    assert state.unsupported_claims == []
    assert "NOT VERIFIED" not in state.answer
    assert state.outcome == "FULL_SUCCESS"


async def test_a_redirect_does_not_turn_the_page_you_read_into_one_you_did_not(
        backend, make_agent, fixture_site_url):
    """G10. `ground_redirect.html` bounces to Widget B. The price read after the bounce is
    grounded, and naming either URL is fair."""
    agent, client = make_agent(backend, [
        {"action": "open_url", "url": f"{fixture_site_url}/ground_redirect.html", "reason": "go"},
        {"action": "wait", "ms": 500, "reason": "let the redirect land"},
        *finishes("Widget B costs $89.00.",
                  claims=[{"text": "Widget B costs $89.00", "evidence_ids": ["1"],
                           "kind": "source"}], times=1),
    ])
    state = await agent.run("What does Widget B cost?")

    assert state.status == TaskStatus.DONE.value
    assert state.unsupported_claims == []
    assert agent.ledger.observed(f"{fixture_site_url}/ground_redirect.html")
    assert agent.ledger.observed(f"{fixture_site_url}/ground_b.html")


async def test_compute_cannot_launder_an_invented_number(backend, make_agent, fixture_site_url):
    """`add(42.50, 0)` must not turn an invented price into an authoritative computed one."""
    agent, client = make_agent(backend, [
        {"action": "open_url", "url": f"{fixture_site_url}/ground_a.html", "reason": "A"},
        {"action": "compute", "operation": "add", "operands": ["42.50", "0"],
         "labels": ["Widget C"], "reason": "total"},
        *finishes("Widget C costs $42.50.",
                  claims=[{"text": "Widget C costs $42.50", "evidence_ids": ["1"],
                           "kind": "derived"}]),
    ])
    state = await agent.run("What does Widget C cost?")

    assert "42.50" in " ".join(state.unsupported_claims)
    assert state.outcome != UNSUPPORTED_SUCCESS


async def test_extracting_an_element_that_is_not_there_reads_the_page_instead(
        backend, make_agent, fixture_site_url):
    """Observed on a plain-text RFC: a page with no interactive elements at all, where the
    model asked to extract the same absent element id on every remaining step and the task
    ran out of budget being told no. Reading the page is what it wanted anyway."""
    agent, client = make_agent(backend, [
        {"action": "open_url", "url": f"{fixture_site_url}/sample.txt", "reason": "plain text"},
        {"action": "extract", "target": 11, "reason": "read it"},
        {"action": "finish", "answer": "Read the plain-text document.", "reason": "done"},
    ])
    state = await agent.run("Read this document.")

    extract = [r for r in state.recent_actions if r.action == "extract"]
    assert extract and extract[0].ok
    assert state.status == TaskStatus.DONE.value
    assert "NO elements at all" in client.decision_prompts[-1] or \
           "NO elements at all" in client.decision_prompts[-2]


async def test_no_grounding_problem_can_ship_without_a_disclosure(backend, make_agent,
                                                                  fixture_site_url):
    """The invariant behind `UNSUPPORTED_SUCCESS = 0`: every category the report can complain
    about must also appear in the label it appends. A category added to one and not the other
    would let an unsupported claim ship silently, and this is what catches that."""
    from agent_v2.grounding import Claim, GroundingReport, UNSUPPORTED_SUCCESS, classify_outcome

    fields = {"invalid_citations": ["ev_x — nope"], "unsupported_claims": ['"x" (nope)'],
              "unsupported_figures": ["42.50"], "unsupported_quotes": ['"never said"'],
              "unvisited_sources": ["nowhere.example"]}
    for name, value in fields.items():
        report = GroundingReport(**{name: value})
        assert report.problems, name
        assert report.label(), f"{name} can be a problem without producing a disclosure"
        assert classify_outcome(done=True, report=report,
                                labelled=bool(report.label())) != UNSUPPORTED_SUCCESS

    everything = GroundingReport(**fields)
    assert len(everything.problems) == 5
    assert classify_outcome(done=True, report=everything, labelled=True) == "PARTIAL_GROUNDED"


async def test_the_agent_is_told_exactly_what_was_wrong_with_its_answer(
        backend, make_agent, fixture_site_url):
    """The challenge is the recovery mechanism, so it has to name the defect rather than
    restate the rule."""
    agent, client = make_agent(backend, [
        {"action": "open_url", "url": f"{fixture_site_url}/ground_a.html", "reason": "A"},
        {"action": "finish", "answer": "Widget C costs $42.50.", "reason": "done"},
        {"action": "open_url", "url": f"{fixture_site_url}/ground_c.html", "reason": "go look"},
        {"action": "finish", "reason": "now grounded", "answer": "Widget C costs $42.50.",
         "state_updates": {"add_facts": ["Widget C costs $42.50"]}},
    ])
    state = await agent.run("What does Widget C cost?")

    challenge = [p for p in client.decision_prompts if "was rejected" in p]
    assert challenge and "42.50" in challenge[0]
    # and having gone and looked, the same answer is now clean
    assert state.unsupported_claims == []
    assert "NOT VERIFIED" not in state.answer
    assert state.metrics.grounding_challenges == 1
