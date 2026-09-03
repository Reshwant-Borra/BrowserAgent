"""A real figure attached to the wrong thing.

The defect these cover, taken from a real `compute_generic` run and reproduced here exactly:

    ev_e7b1d3_1  observed  "The Requiem Red price is £22.65"     <- true
    ev_e7b1d3_5  observed  "The Black Maria price is £22.65"     <- false, and accepted
    ev_e7b1d3_3  derived   "22.65 + 22.65 = 45.30"               <- grounded, and wrong

Every containment check passed. £22.65 really was on the page the note was written against;
"Black Maria" really was a name the task had seen. The ledger knew the page had shown 22.65
and had no way to know *whose* 22.65 it was, so the false record was minted, and then vouched
for the final claim that cited it. The task finished FULL_SUCCESS with an answer of £45.30
against a true total of £74.80 — the worst outcome in the ranking, a confidently certified
wrong number.

What follows is the matrix of ways a sentence can get the identity wrong, and — just as
importantly — the ways a sentence can look wrong and be perfectly true. A check that suppresses
honest findings to catch this one would cost more than the defect does.
"""
from __future__ import annotations

import pytest

from agent_v2.browser_ops import BrowserSession
from agent_v2.grounding import Claim, ClaimKind, check_answer
from agent_v2.ledger import EvidenceLedger

pytestmark = pytest.mark.asyncio


async def _ledger(backend, fixture_site_url, *pages: str) -> EvidenceLedger:
    """A ledger built the way a run builds one: by actually loading pages and observing them."""
    session = BrowserSession(backend)
    ledger = EvidenceLedger(task_id="identity-task")
    for step, page in enumerate(pages, start=1):
        await backend.open_url(f"{fixture_site_url}/{page}")
        ledger.note_observation(await session.observe(), step)
    return ledger


@pytest.fixture
async def catalogue(backend, fixture_site_url):
    return await _ledger(backend, fixture_site_url, "identity_catalogue.html")


# --- the binding itself: what the page put next to what --------------------------------

async def test_each_card_binds_its_own_name_to_its_own_price(catalogue):
    assert catalogue.figures_of("Bolt Carrier") == {"22.65"}
    assert catalogue.figures_of("Gasket Ring") == {"52.15"}
    assert catalogue.figures_of("Spindle Housing") == {"13.99"}


async def test_a_table_row_binds_its_leading_cell(catalogue):
    """The observer already emits a row as one line precisely so its cells stay paired."""
    assert catalogue.figures_of("Northwind Depot") == {"4.62"}
    assert catalogue.figures_of("Eastvale Supply") == {"3.71"}


async def test_a_label_binds_the_value_beneath_it(catalogue):
    """`<dt>`/`<dd>`, or any heading sitting above its number.

    A date binds as its parts — the ledger's figure vocabulary counts numbers, and
    `2024-01-05` is three of them. That is the pre-existing representation and it is the right
    one here: it means a claim of "revised 2024" is as checkable as one of the whole date."""
    assert catalogue.figures_of("Catalogue revised") == {"2024", "01", "05"}
    assert catalogue.figures_of("Schema version") == {"3.12.1", "3"}


async def test_binding_is_domain_neutral(catalogue):
    """One mechanism, and nothing in it knows what a price or a rating or a date is — the
    same three readings carry product/price, supplier/rating and label/date alike."""
    assert catalogue.figures_of("Bolt Carrier")          # product -> price
    assert catalogue.figures_of("Northwind Depot")       # supplier -> rating
    assert catalogue.figures_of("Catalogue revised")     # attribute -> date


# --- the failure, at the point where it was minted --------------------------------------

async def test_the_reported_defect_is_refused(catalogue):
    """"Gasket Ring costs $22.65" — a real figure, a real name, and a false sentence."""
    ref = catalogue.observations["obs_1"]
    record, why = catalogue.record_observed("Gasket Ring price is $22.65", ref, step=1)
    assert record is None
    assert "Bolt Carrier" in why


async def test_the_true_version_of_the_same_sentence_is_kept(catalogue):
    ref = catalogue.observations["obs_1"]
    record, why = catalogue.record_observed("Gasket Ring price is $52.15", ref, step=1)
    assert record is not None, why


async def test_wrong_attribute_of_the_right_entity_is_refused(catalogue):
    """Right supplier, but the rating belongs to the other one."""
    ref = catalogue.observations["obs_1"]
    record, why = catalogue.record_observed("Northwind Depot is rated 3.71", ref, step=1)
    assert record is None
    assert "Eastvale" in why


async def test_a_figure_from_a_sibling_candidate_is_refused(catalogue):
    ref = catalogue.observations["obs_1"]
    record, _ = catalogue.record_observed("Spindle Housing is priced at $52.15", ref, step=1)
    assert record is None


async def test_the_same_value_may_belong_to_two_different_entities(backend, fixture_site_url):
    """Two things genuinely costing the same is ordinary, not a contradiction."""
    ledger = await _ledger(backend, fixture_site_url, "identity_tiers.html")
    ref = ledger.observations["obs_1"]
    # Both plans named "Standard" are bound, so both figures are that name's.
    for figure in ("29.00", "44.00"):
        record, why = ledger.record_observed(f"The Standard plan is {figure} monthly", ref, step=1)
        assert record is not None, why


async def test_two_values_for_one_entity_are_both_allowed(backend, fixture_site_url):
    """A list price and a sale price are both really that product's."""
    ledger = await _ledger(backend, fixture_site_url, "identity_tiers.html")
    ref = ledger.observations["obs_1"]
    for figure in ("88.00", "61.50"):
        record, why = ledger.record_observed(f"Relay Module is $ {figure}", ref, step=1)
        assert record is not None, why


async def test_a_duplicate_label_is_never_silently_resolved(backend, fixture_site_url):
    """Two plans are called "Standard". Nothing may decide which was meant, so neither
    figure is contradicted and neither is invented — the ambiguity is left standing."""
    ledger = await _ledger(backend, fixture_site_url, "identity_tiers.html")
    assert ledger.figures_of("Standard") == {"29", "29.00", "44", "44.00"}
    # …and a figure belonging to neither is still caught by the ordinary containment check.
    ref = ledger.observations["obs_1"]
    record, _ = ledger.record_observed("The Standard plan is 31.00 monthly", ref, step=1)
    assert record is None


# --- the binding survives leaving the page it was read on --------------------------------

async def test_a_binding_read_on_a_listing_still_holds_on_a_detail_page(backend, fixture_site_url):
    """Exactly the shape of the reported run: the listing is read, the agent clicks into one
    product, and the false note about the *other* product is written there. The figure is on
    the page in hand, so the per-page check passes — the catalogue is what knows better."""
    ledger = await _ledger(backend, fixture_site_url,
                           "identity_catalogue.html", "identity_bolt.html")
    detail = ledger.observations["obs_2"]
    record, why = ledger.record_observed("Gasket Ring price is $22.65", detail, step=2)
    assert record is None
    assert "Bolt Carrier" in why


async def test_a_figure_no_one_was_seen_with_is_not_called_misattribution(catalogue):
    """The check accuses only when the figure demonstrably belongs to somebody else. A number
    read somewhere this task has not looked is a different fault, judged by the containment
    checks — mislabelling it here would put the wrong repair in front of the model."""
    assert catalogue.misattribution("Bolt Carrier weighs 4.44 kg") == ""


async def test_a_sentence_naming_several_entities_is_left_alone(catalogue):
    """"A is $22.65 and B is $52.15" attributes correctly, and a sentence naming two bound
    entities cannot be attributed by this mechanism anyway. Abstaining beats guessing."""
    assert catalogue.misattribution(
        "Bolt Carrier is $22.65 and Gasket Ring is $52.15") == ""


async def test_prose_with_no_figures_is_untouched(catalogue):
    assert catalogue.misattribution("Gasket Ring is out of stock at the moment") == ""


# --- the second layer: a claim may not relabel evidence it cites -------------------------

async def test_a_claim_cannot_relabel_the_evidence_it_cites(catalogue):
    """The hole the citation test could not close. The claim points at a real record whose
    figure really is 22.65 — provenance holds perfectly — and it hands that figure to the
    wrong product. Only the page's layout settles it."""
    ref = catalogue.observations["obs_1"]
    record, _ = catalogue.record_observed("Bolt Carrier price is $22.65", ref, step=1)
    assert record is not None

    report = check_answer(
        answer="Gasket Ring costs $22.65.",
        claims=[Claim(text="Gasket Ring costs $22.65", evidence_ids=[record.evidence_id],
                      kind=ClaimKind.SOURCE)],
        ledger=catalogue, goal="what does Gasket Ring cost",
    )
    assert report.unsupported_claims
    assert not report.clean


async def test_the_correctly_attributed_claim_still_passes(catalogue):
    ref = catalogue.observations["obs_1"]
    record, _ = catalogue.record_observed("Bolt Carrier price is $22.65", ref, step=1)
    report = check_answer(
        answer="Bolt Carrier costs $22.65.",
        claims=[Claim(text="Bolt Carrier costs $22.65", evidence_ids=[record.evidence_id],
                      kind=ClaimKind.SOURCE)],
        ledger=catalogue, goal="what does Bolt Carrier cost",
    )
    assert report.clean, report.problems


async def test_a_synthesis_claim_may_not_smuggle_a_misattribution(catalogue):
    report = check_answer(
        answer="Gasket Ring at $22.65 looks like the better buy.",
        claims=[Claim(text="Gasket Ring at $22.65 looks like the better buy",
                      kind=ClaimKind.SYNTHESIS)],
        ledger=catalogue, goal="which is the better buy",
    )
    assert report.unsupported_claims


async def test_a_statement_about_the_run_is_not_about_the_world(catalogue):
    """META claims describe the task, not a page; misattribution cannot apply to them."""
    report = check_answer(
        answer="I opened 1 page.",
        claims=[Claim(text="I opened 1 page", kind=ClaimKind.META)],
        ledger=catalogue, goal="read the catalogue",
    )
    assert not report.unsupported_claims


# --- evidence identity stays inside its own task -----------------------------------------

async def test_evidence_from_another_task_is_still_refused(catalogue):
    """Unchanged by any of this, and re-checked here because the fix touches the same path."""
    other = EvidenceLedger(task_id="a-different-task")
    assert other.key != catalogue.key
    assert not catalogue.resolve(f"ev_{other.key}_1").ok
    assert catalogue.resolve(f"ev_{other.key}_1").reason == "belongs to a different task"


# --- the third layer: computing with the figure you say you are computing with -----------

async def _agent_over(backend, make_agent, fixture_site_url, script):
    agent, client = make_agent(backend, script)
    await backend.open_url(f"{fixture_site_url}/identity_catalogue.html")
    return agent, client


async def test_a_labelled_operand_must_match_what_the_page_showed(
        backend, make_agent, fixture_site_url):
    """The operand contract, built out of the `labels` field that already existed. The model
    names what it is adding; BrowserAgent checks the name against the page before doing the
    arithmetic, so a perfect sum over the wrong operand never becomes a result."""
    agent, _ = await _agent_over(backend, make_agent, fixture_site_url, [
        {"action": "open_url", "url": f"{fixture_site_url}/identity_catalogue.html",
         "reason": "read the catalogue"},
        {"action": "compute", "operation": "add", "operands": ["22.65", "22.65"],
         "labels": ["Bolt Carrier", "Gasket Ring"], "reason": "total the pair"},
        {"action": "finish", "answer": "Could not total them.", "reason": "done"},
    ])
    state = await agent.run("total Bolt Carrier and Gasket Ring")
    assert state.metrics.compute_errors >= 1
    assert not any(r.kind == "derived" and r.grounded for r in agent.ledger.records.values())


async def test_correctly_labelled_operands_compute_normally(
        backend, make_agent, fixture_site_url):
    """The other direction, and the one that matters for not breaking anything: the same
    action with the operands the page really showed goes straight through."""
    agent, _ = await _agent_over(backend, make_agent, fixture_site_url, [
        {"action": "open_url", "url": f"{fixture_site_url}/identity_catalogue.html",
         "reason": "read the catalogue"},
        {"action": "compute", "operation": "add", "operands": ["22.65", "52.15"],
         "labels": ["Bolt Carrier", "Gasket Ring"], "reason": "total the pair"},
        {"action": "finish", "answer": "The pair comes to $74.80.", "reason": "done"},
    ])
    state = await agent.run("total Bolt Carrier and Gasket Ring")
    assert state.metrics.compute_errors == 0
    derived = [r for r in agent.ledger.records.values() if r.kind == "derived"]
    assert derived and derived[0].grounded
    assert "74.8" in derived[0].text


async def test_unlabelled_operands_are_not_refused(backend, make_agent, fixture_site_url):
    """Most computations name nothing. The ledger cannot contradict a name it was not given,
    and turning a missing field into a failed task would cost far more than it caught."""
    agent, _ = await _agent_over(backend, make_agent, fixture_site_url, [
        {"action": "open_url", "url": f"{fixture_site_url}/identity_catalogue.html",
         "reason": "read the catalogue"},
        {"action": "compute", "operation": "add", "operands": ["22.65", "52.15"],
         "reason": "total the pair"},
        {"action": "finish", "answer": "The pair comes to $74.80.", "reason": "done"},
    ])
    state = await agent.run("total the two parts")
    assert state.metrics.compute_errors == 0
