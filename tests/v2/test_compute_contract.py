"""When a challenged answer needs arithmetic, computing has to be one of the ways out.

The regression these cover, from the grounding and compute suites: an answer stating a figure
no page shows was challenged, and the challenge told the model — in as many words — that it
MUST take a browser action and go read the missing piece. For a difference between two prices
there is no such page. The run would re-open pages it had already read, fail to find a number
that only a calculation produces, and settle for doing the sum in its head. Deterministic
computations fell to 2 across a 15-run suite that had previously used 11.

Nothing here forces a computation. The model still decides whether the missing figure is
something to read or something to work out; what is tested is that the second option is put in
front of it when the task is actually holding operands.
"""
from __future__ import annotations

import pytest

from agent_v2.agent import BrowserAgentV2, LoopLimits
from agent_v2.browser_ops import BrowserSession
from agent_v2.grounding import GroundingReport
from agent_v2.ledger import EvidenceLedger


def _ledger_with_figures(count: int) -> EvidenceLedger:
    """A ledger holding `count` observed figures, built the way a run builds one."""
    ledger = EvidenceLedger(task_id="t-compute")
    for index in range(count):
        ledger.records[f"ev_x_{index + 1}"] = _record(f"ev_x_{index + 1}", f"{40 + index}.50")
    return ledger


def _record(evidence_id: str, figure: str):
    from agent_v2.ledger import EvidenceRecord
    return EvidenceRecord(
        evidence_id=evidence_id, task_id="t-compute", kind="observed",
        text=f"The price is £{figure}", step=1, source_url="https://shop.example/item",
        figures={figure},
    )


class _Hint:
    """`_grounding_hint` only needs the agent for its ledger, so the rest is left out
    deliberately — building a whole agent would test the constructor, not the hint."""

    def __init__(self, ledger):
        self.ledger = ledger

    hint = BrowserAgentV2._grounding_hint


def _hint_for(ledger, report: GroundingReport) -> str:
    return _Hint(ledger).hint(report)


def test_compute_is_offered_when_the_task_already_holds_figures():
    report = GroundingReport(unsupported_figures=["1.97"])
    hint = _hint_for(_ledger_with_figures(2), report)

    assert "compute" in hint
    assert "ev_x_1" in hint and "ev_x_2" in hint
    # The old wording made a browser action the only permitted move; that is the bug.
    assert "MUST take a browser action" not in hint


def test_reading_is_still_offered_alongside_computing():
    """Offering compute must not stop the model going to look — most shortfalls really are
    a page it has not opened."""
    hint = _hint_for(_ledger_with_figures(3), GroundingReport(unsupported_figures=["1.97"]))

    assert "open_url" in hint


def test_with_no_figures_in_hand_the_repair_is_to_go_and_read():
    """Nothing to compute from: a missing number can only come off a page, and suggesting
    arithmetic would invite inventing the operands."""
    hint = _hint_for(_ledger_with_figures(0), GroundingReport(unsupported_figures=["51.77"]))

    assert "MUST take a browser action" in hint
    assert "compute" not in hint


def test_one_lone_figure_is_not_enough_to_suggest_arithmetic():
    hint = _hint_for(_ledger_with_figures(1), GroundingReport(unsupported_figures=["51.77"]))

    assert "compute" not in hint


def test_a_missing_source_is_not_an_arithmetic_problem():
    """An unopened site is a page to go and open, whatever figures the task happens to hold."""
    report = GroundingReport(unvisited_sources=["golang.org"])
    hint = _hint_for(_ledger_with_figures(3), report)

    assert "MUST take a browser action" in hint
    assert "compute" not in hint


def test_the_defect_is_still_named_exactly():
    """The challenge's first job is unchanged: say which part of the answer had no source."""
    hint = _hint_for(_ledger_with_figures(2), GroundingReport(unsupported_figures=["1.97"]))

    assert "1.97" in hint
    assert hint.startswith("Your answer was rejected:")


# ---- the ledger side of it ---------------------------------------------------------------

def test_numeric_evidence_offers_only_usable_operands():
    ledger = _ledger_with_figures(2)
    # A record with no figure in it cannot be an operand.
    from agent_v2.ledger import EvidenceRecord
    ledger.records["ev_x_3"] = EvidenceRecord(
        evidence_id="ev_x_3", task_id="t-compute", kind="observed",
        text="Shipping is free", step=2, figures=set())
    # An invalidated record is not evidence at all.
    ledger.records["ev_x_4"] = _record("ev_x_4", "99.00")
    ledger.records["ev_x_4"].valid = False
    # A derived record whose own operands were unaccountable confers nothing.
    ledger.records["ev_x_5"] = _record("ev_x_5", "12.00")
    ledger.records["ev_x_5"].grounded = False

    ids = [r.evidence_id for r in ledger.numeric_evidence()]

    assert ids == ["ev_x_1", "ev_x_2"]


def test_numeric_evidence_is_bounded():
    """The hint quotes these ids inline, so the list cannot grow with the ledger."""
    assert len(_ledger_with_figures(20).numeric_evidence()) <= 4


# ---- the contract the model is shown -----------------------------------------------------

def test_the_action_contract_does_not_tell_the_model_to_finish_on_the_inputs():
    """The "finish early" rule and the "never do arithmetic yourself" rule were in direct
    conflict: a page showing three prices satisfies "already contains what the goal asks for"
    while the goal actually asks for something none of them is."""
    from agent_v2.prompts import SYSTEM

    assert "compute produces the answer" in SYSTEM
    assert "Do not do arithmetic or compare numbers yourself" in SYSTEM
