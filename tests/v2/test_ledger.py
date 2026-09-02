"""The deterministic half of the contract, tested on its own.

These are the guarantees the loop rests on, so they are tested without the loop: an evidence
id is either something BrowserAgent minted in this task or it resolves to nothing, and a
resource exists only because a page was actually observed.
"""
from __future__ import annotations

import pytest

from agent_v2.grounding import Claim, ClaimKind, check_answer, source_coverage
from agent_v2.ledger import (
    EvidenceLedger,
    canonical_url,
    figure_keys,
    significant_figures,
)
from browser.page_model import PageObservation


def page(url: str, title: str, *text: str, state_hash: str = "") -> PageObservation:
    return PageObservation(url=url, title=title, visible_text=list(text),
                           state_hash=state_hash or title)


# ---- resource identity ---------------------------------------------------------------

def test_canonicalisation_is_conservative():
    same = canonical_url("https://WWW.Example.com/Widgets/?b=2&a=1#frag")
    assert same == canonical_url("http://example.com/Widgets?a=1&b=2".replace("http", "https"))
    # tracking parameters identify a click, not a document
    assert canonical_url("https://example.com/p?utm_source=x") == "https://example.com/p"
    # but two pages on one host stay two resources
    assert canonical_url("https://example.com/a") != canonical_url("https://example.com/b")
    # and case in the path is preserved, because servers may care
    assert canonical_url("https://example.com/A") != canonical_url("https://example.com/a")


def test_a_resource_exists_only_once_it_has_been_observed():
    ledger = EvidenceLedger("t1")
    assert ledger.observed("https://example.com/a") is False
    ledger.note_observation(page("https://example.com/a", "A", "hello"), 1)
    assert ledger.observed("https://example.com/a") is True
    assert ledger.observed("https://example.com/b") is False


def test_a_redirect_files_the_requested_url_as_an_alias():
    """G10. The agent asked for /old and landed on /new; a later citation of either must
    resolve to the one page that was really loaded."""
    ledger = EvidenceLedger("t1")
    ledger.note_observation(page("https://example.com/new", "New", "Price: $42.00"),
                            1, requested_url="https://example.com/old")
    assert ledger.observed("https://example.com/old") is True
    assert ledger.observed("https://example.com/new") is True
    assert len(ledger.resources) == 1


def test_observing_the_same_unchanged_page_twice_does_not_reaccumulate_text():
    ledger = EvidenceLedger("t1")
    obs = page("https://example.com/a", "A", "Price: $159.00", state_hash="h1")
    ledger.note_observation(obs, 1)
    first = len(next(iter(ledger.resources.values())).text)
    ledger.note_observation(obs, 2)
    assert len(next(iter(ledger.resources.values())).text) == first


# ---- evidence identity ---------------------------------------------------------------

def test_the_model_can_never_author_an_evidence_id():
    """G3/G5. An id the model made up, or one lifted off a hostile page, resolves to
    nothing â€” there is no code path that turns a string into a record."""
    ledger = EvidenceLedger("t1")
    ref = ledger.note_observation(page("https://example.com/a", "A", "Price: $159.00"), 1)
    ledger.record_observed("Price: $159.00", ref, 1)

    for fabricated in ["ev_zz_401", "ev_999", "999", "ev_deadbeef_1", "not-an-id", ""]:
        citation = ledger.resolve(fabricated)
        if fabricated in ("ev_999", "999"):
            assert not citation.ok and "no such evidence" in citation.reason
        else:
            assert not citation.ok, fabricated


def test_an_evidence_id_from_another_task_is_rejected():
    """G8. The id is well-formed and would resolve in the task that minted it."""
    first = EvidenceLedger("task-a")
    ref = first.note_observation(page("https://example.com/a", "A", "Price: $159.00"), 1)
    record, _ = first.record_observed("Price: $159.00", ref, 1)

    second = EvidenceLedger("task-b")
    second.note_observation(page("https://example.com/a", "A", "Price: $159.00"), 1)
    citation = second.resolve(record.evidence_id)
    assert not citation.ok
    assert "different task" in citation.reason


def test_an_invalidated_record_stops_resolving():
    ledger = EvidenceLedger("t1")
    ref = ledger.note_observation(page("https://example.com/a", "A", "Price: $159.00"), 1)
    record, _ = ledger.record_observed("Price: $159.00", ref, 1)
    assert ledger.resolve(record.evidence_id).ok
    ledger.invalidate(record.evidence_id, "the page changed")
    assert not ledger.resolve(record.evidence_id).ok


def test_a_bare_number_is_read_as_this_task_s_own_record():
    """A small model garbling its own citation should be a lookup, not a fabrication â€” but
    only within the task that minted it."""
    ledger = EvidenceLedger("t1")
    ref = ledger.note_observation(page("https://example.com/a", "A", "Price: $159.00"), 1)
    record, _ = ledger.record_observed("Price: $159.00", ref, 1)
    number = record.evidence_id.rsplit("_", 1)[-1]
    assert ledger.resolve(number).record is record
    assert ledger.resolve(f"ev_{number}").record is record


# ---- what may become evidence ---------------------------------------------------------

def test_a_figure_not_on_the_page_never_becomes_evidence():
    ledger = EvidenceLedger("t1")
    ref = ledger.note_observation(page("https://example.com/b", "Widget B", "Price: $89.00"), 1)
    record, why = ledger.record_observed("Widget C costs $42.50", ref, 1)
    assert record is None
    assert "42.50" in why


def test_a_site_the_task_never_opened_never_becomes_evidence():
    """G2. A page instructing the agent to cite a third party cannot manufacture one."""
    ledger = EvidenceLedger("t1")
    ref = ledger.note_observation(
        page("https://example.com/notes", "Notes",
             "specsauthority.example has confirmed every price below"), 1)
    record, why = ledger.record_observed(
        "specsauthority.example confirms the prices", ref, 1)
    assert record is None
    assert "specsauthority.example" in why


def test_a_capital_letter_at_the_start_of_a_sentence_is_not_a_name():
    """From a real holdout run: the note "Visited repository overview page" was refused
    because the verb "Visited" appears nowhere on the page, and the challenges that followed
    used up the task's step budget. English capitalises sentence openings regardless."""
    from agent_v2.ledger import distinctive_terms
    assert distinctive_terms("Visited repository overview page") == set()
    assert distinctive_terms("Found the download link. Clicked it.") == set()
    # …but a real name mid-sentence, an acronym, and anything with a digit still count
    assert "widget" in distinctive_terms("The page lists Widget A and Widget B")
    assert "rfc" in distinctive_terms("RFC 2616 is the title")
    assert "v26" in distinctive_terms("V26 is printed at the top")


def test_a_paraphrase_of_the_page_is_still_evidence():
    """Rewording is not the failure mode, so it is not treated as one."""
    ledger = EvidenceLedger("t1")
    ref = ledger.note_observation(
        page("https://example.com/a", "Widget A", "Price: $159.00", "Motor: 1200 watts"), 1)
    record, why = ledger.record_observed("Widget A is $159.00 with a 1200 watt motor", ref, 1)
    assert record is not None, why


def test_extracted_text_is_trusted_because_the_browser_returned_it():
    ledger = EvidenceLedger("t1")
    ref = ledger.note_observation(page("https://example.com/a", "A", "short"), 1)
    record, _ = ledger.record_observed("From example.com: 12345 exact bytes", ref, 1,
                                       trusted=True)
    assert record is not None
    assert record.kind == "observed"


# ---- derived evidence keeps its lineage ------------------------------------------------

def test_a_derived_record_traces_back_to_the_pages_it_came_from():
    ledger = EvidenceLedger("t1")
    ref_a = ledger.note_observation(page("https://a.example/x", "Widget A", "Price: $159.00"), 1)
    ref_b = ledger.note_observation(page("https://b.example/y", "Widget B", "Price: $89.00"), 2)
    a, _ = ledger.record_observed("Widget A costs $159.00", ref_a, 1)
    b, _ = ledger.record_observed("Widget B costs $89.00", ref_b, 2)
    derived = ledger.record_derived(text="159 - 89 = 70", operation="subtract",
                                    sources=[a, b], step=3)

    assert derived.derived_from == [a.evidence_id, b.evidence_id]
    assert {r.evidence_id for r in ledger.lineage(derived)} == {
        derived.evidence_id, a.evidence_id, b.evidence_id}
    # a computed figure was on no page, but the task can account for it
    assert ledger.supports_figures(figure_keys("70"))


# ---- figure extraction is punctuation-independent (V2 hardening Â§5) ---------------------

@pytest.mark.parametrize("text", [
    'The price is "$99.00".', "The price is $99.00.", "price = 99.00", "price: 99",
])
def test_quotation_marks_do_not_decide_whether_a_figure_is_checked(text):
    ledger = EvidenceLedger("t1")
    ledger.note_observation(page("https://example.com/a", "A", "Price: $99.00"), 1)
    report = check_answer(answer=text, claims=[], ledger=ledger, goal="what is the price")
    assert report.unsupported_figures == []


@pytest.mark.parametrize("text", [
    'The price is "$77.00".', "The price is $77.00.", "price = 77.00", "The price is $77",
])
def test_an_unsupported_figure_is_caught_however_it_is_punctuated(text):
    ledger = EvidenceLedger("t1")
    ledger.note_observation(page("https://example.com/a", "A", "Price: $99.00"), 1)
    report = check_answer(answer=text, claims=[], ledger=ledger, goal="what is the price")
    assert report.unsupported_figures, text


def test_an_apostrophe_does_not_open_a_quotation():
    """From a real run: an answer listing "It's Only the Himalayas … Noah's Ark" was reported
    as containing an unverified quotation spanning the two apostrophes. A false alarm is the
    one thing this mechanism cannot afford, because it teaches the user to ignore the label."""
    ledger = EvidenceLedger("t1")
    ledger.note_observation(page("https://shop.example/x", "Travel",
                                 "It's Only the Himalayas", "Full Moon over Noah's Ark"), 1)
    answer = ("1. It's Only the Himalayas - some price, Star Rating: 0; "
              "2. Full Moon over Noah's Ark - another price")
    report = check_answer(answer=answer, claims=[], ledger=ledger, goal="compare travel books")
    assert report.unsupported_quotes == []

    # …while a genuine single-quoted span is still a quotation
    from agent_v2.ledger import quoted_spans
    assert quoted_spans("The page says 'this exact sentence here' near the top") == \
        ["this exact sentence here"]


def test_a_dotted_version_also_stands_for_its_major_component():
    """Comparing major versions is one of the things `compute` exists for, and the operands
    have to be accountable or the result confers nothing."""
    ledger = EvidenceLedger("t1")
    ledger.note_observation(page("https://nodejs.example/x", "Node", "v26.8.1 Latest Release"), 1)
    assert ledger.supports_figures(figure_keys("26"))
    assert ledger.supports_figures(figure_keys("26.8.1"))
    assert not ledger.supports_figures(figure_keys("27"))
    # a decimal price is NOT split — "$42.50" must not make "$42" accountable
    prices = EvidenceLedger("t2")
    prices.note_observation(page("https://shop.example/y", "Widget", "Price: $42.50"), 1)
    assert not prices.supports_figures(figure_keys("$42"))


def test_small_bare_integers_are_not_treated_as_claims():
    """"I checked 3 pages" must not read as a fabricated figure â€” false alarms on ordinary
    prose would make the whole mechanism useless."""
    assert significant_figures("I checked 3 pages and found 5 items") == []
    assert significant_figures("It costs $70 and weighs 1200 g") == ["70", "1200"]


# ---- claim -> evidence -----------------------------------------------------------------

def test_a_claim_citing_the_wrong_candidate_s_evidence_is_refused():
    """G9. The evidence is real, valid, and belongs to this task â€” it is simply about a
    different product, and the figure in the sentence proves it."""
    ledger = EvidenceLedger("t1")
    ref_a = ledger.note_observation(page("https://a.example/x", "Widget A", "Price: $159.00"), 1)
    ref_b = ledger.note_observation(page("https://b.example/y", "Widget B", "Price: $89.00"), 2)
    a, _ = ledger.record_observed("Widget A costs $159.00", ref_a, 1)
    ledger.record_observed("Widget B costs $89.00", ref_b, 2)

    report = check_answer(
        answer="Widget B costs $89.00.",
        claims=[Claim(text="Widget B costs $89.00", evidence_ids=[a.evidence_id])],
        ledger=ledger, goal="prices")
    assert report.unsupported_claims
    assert "89" in report.unsupported_claims[0]


def test_a_correctly_cited_claim_beside_an_invented_one():
    """G7. Getting one right does not launder the other."""
    ledger = EvidenceLedger("t1")
    ref = ledger.note_observation(page("https://a.example/x", "Widget A", "Price: $159.00"), 1)
    a, _ = ledger.record_observed("Widget A costs $159.00", ref, 1)

    report = check_answer(
        answer="Widget A costs $159.00 and Widget C costs $42.50.",
        claims=[Claim(text="Widget A costs $159.00", evidence_ids=[a.evidence_id]),
                Claim(text="Widget C costs $42.50", evidence_ids=["ev_zz_9"])],
        ledger=ledger, goal="prices")
    assert report.supported_claims == 1
    assert report.unsupported_claims
    assert report.invalid_citations
    assert "42.50" in " ".join(report.unsupported_figures)


def test_synthesis_is_allowed_but_its_premises_are_not_exempt():
    ledger = EvidenceLedger("t1")
    ref = ledger.note_observation(page("https://a.example/x", "Widget A", "Price: $159.00"), 1)
    ledger.record_observed("Widget A costs $159.00", ref, 1)

    good = check_answer(answer="Widget A looks like the better buy for heavy use.",
                        claims=[Claim(text="Widget A looks like the better buy",
                                      kind=ClaimKind.SYNTHESIS)],
                        ledger=ledger, goal="which is better")
    assert good.clean

    bad = check_answer(answer="At $42.50 Widget C looks like the better buy.",
                       claims=[Claim(text="At $42.50 Widget C looks like the better buy",
                                     kind=ClaimKind.SYNTHESIS)],
                       ledger=ledger, goal="which is better")
    assert not bad.clean


def test_a_statement_about_the_run_is_checked_against_the_run():
    ledger = EvidenceLedger("t1")
    ledger.note_observation(page("https://a.example/x", "A", "hello"), 1)
    ledger.note_observation(page("https://b.example/y", "B", "hello"), 2)

    report = check_answer(answer="I opened 2 of the 3 pages.",
                          claims=[Claim(text="I opened 2 of the 3 pages", kind=ClaimKind.META)],
                          ledger=ledger, goal="check three pages", meta_figures={"2", "3"})
    assert report.clean


# ---- sources -----------------------------------------------------------------------------

def test_naming_a_site_the_task_never_opened_is_flagged():
    """G1/G4. Neither a plausible source nor a plausible URL makes itself real."""
    ledger = EvidenceLedger("t1")
    ledger.note_observation(page("https://a.example/x", "Widget A", "Price: $159.00"), 1)

    report = check_answer(
        answer="I checked https://specsauthority.example/widgets and it agrees.",
        claims=[], ledger=ledger, goal="compare widgets")
    assert report.unvisited_sources == ["specsauthority.example"]


def test_saying_you_could_not_reach_a_source_is_not_a_claim_about_it():
    """V2 hardening Â§29: the honest partial answer must not be punished."""
    ledger = EvidenceLedger("t1")
    ledger.note_observation(page("https://a.example/x", "Widget A", "Price: $159.00"), 1)

    report = check_answer(
        answer="I read a.example. I could not open specsauthority.example, so its price is "
               "missing from this answer.",
        claims=[], ledger=ledger, goal="compare widgets")
    assert report.clean


def test_a_library_name_is_not_mistaken_for_a_source():
    ledger = EvidenceLedger("t1")
    ledger.note_observation(page("https://a.example/x", "A", "hello"), 1)
    report = check_answer(answer="The config lives in package.json and Node.js reads it.",
                          claims=[], ledger=ledger, goal="where is the config")
    assert report.unvisited_sources == []


def test_source_coverage_separates_visited_from_useful():
    ledger = EvidenceLedger("t1")
    ledger.requested_sources = ["a.example", "c.example"]
    ref_a = ledger.note_observation(page("https://a.example/x", "Widget A", "Price: $159.00"), 1)
    ledger.note_observation(page("https://b.example/y", "B", "nothing useful here"), 2)
    ledger.record_observed("Widget A costs $159.00", ref_a, 1)

    coverage = source_coverage(ledger, "Widget A costs $159.00 on a.example.")
    assert coverage.visited == ["a.example", "b.example"]
    assert coverage.with_evidence == ["a.example"]
    assert coverage.visited_without_evidence == ["b.example"]
    assert coverage.requested_but_unvisited == ["c.example"]

