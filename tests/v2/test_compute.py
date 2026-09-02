"""The deterministic compute tool (V2 hardening §9/§10/§11/§22).

The point of this module is that these answers are never wrong, so the tests are the
arithmetic itself — including the cases an 8B model reliably gets wrong (3.14 vs 24, 3.9 vs
3.10) and the cases where refusing is the right answer.
"""
from __future__ import annotations

import pytest

from agent_v2.compute import MAX_OPERANDS, ComputeOp, parse_date, parse_number, parse_version, run_compute


# ---- the comparisons a small model gets wrong -------------------------------------------

def test_a_decimal_is_not_larger_than_an_integer_just_because_it_has_more_digits():
    result = run_compute("compare", ["3.14", "24"])
    assert result.ok
    assert result.value == "less than"


def test_money_comparison():
    result = run_compute("compare", ["$88.99", "$159.99"], ["Widget B", "Widget A"])
    assert result.ok and result.value == "less than"
    assert "Widget B" in result.text and "Widget A" in result.text


def test_version_ordering_is_numeric_not_lexical():
    assert run_compute("version_compare", ["3.9", "3.10"]).value == "older than"
    assert run_compute("version_compare", ["v24.8.0", "3.13.1"]).value == "newer than"
    assert run_compute("version_compare", ["3.12.1", "3.12.1"]).value == "the same version as"


def test_a_prerelease_is_older_than_its_release():
    assert run_compute("version_compare", ["3.13.0-rc1", "3.13.0"]).value == "older than"


# ---- ordering and selection --------------------------------------------------------------

def test_sorting_five_prices():
    result = run_compute("sort_asc", ["$159.00", "$42.50", "$1,299.99", "$89.00", "$7.05"])
    assert result.ok
    assert result.value == ["$7.05", "$42.50", "$89.00", "$159.00", "$1,299.99"]


def test_sorting_descending():
    result = run_compute("sort_desc", ["10", "9", "100"])
    assert result.value == ["100", "10", "9"]


def test_min_and_max_name_the_operand_they_picked():
    lowest = run_compute("min", ["159", "89", "42.50"], ["A", "B", "C"])
    assert lowest.value == 42.5 and "C" in lowest.text
    highest = run_compute("max", ["159", "89", "42.50"], ["A", "B", "C"])
    assert highest.value == 159 and "A" in highest.text


def test_count():
    assert run_compute("count", ["a", "b", "c"]).value == 3.0


# ---- arithmetic ---------------------------------------------------------------------------

@pytest.mark.parametrize("op,operands,expected", [
    ("add", ["1.5", "2.25", "3"], 6.75),
    ("subtract", ["$159.00", "$89.00"], 70.0),
    ("subtract", ["-4", "6"], -10.0),
    ("multiply", ["1.5", "4"], 6.0),
    ("divide", ["10", "4"], 2.5),
])
def test_arithmetic(op, operands, expected):
    result = run_compute(op, operands)
    assert result.ok, result.error
    assert result.value == pytest.approx(expected)


def test_negative_numbers_and_decimals_survive_formatting():
    assert run_compute("subtract", ["1.05", "2.10"]).value == pytest.approx(-1.05)
    assert "-1.05" in run_compute("subtract", ["1.05", "2.10"]).text


def test_percentages():
    assert run_compute("percent_of", ["25", "200"]).value == pytest.approx(12.5)
    change = run_compute("percent_change", ["80", "100"])
    assert change.value == pytest.approx(25.0) and "increase" in change.text
    drop = run_compute("percent_change", ["100", "80"])
    assert drop.value == pytest.approx(-20.0) and "decrease" in drop.text


# ---- dates ---------------------------------------------------------------------------------

def test_date_comparison():
    assert run_compute("date_compare", ["2019-05-01", "2021-01-02"]).value == "earlier than"
    assert run_compute("date_compare", ["5 January 2024", "2023-12-31"]).value == "later than"
    assert run_compute("date_compare", ["2019", "2019-01-01"]).value == "the same date as"


def test_an_ambiguous_slash_date_is_refused_rather_than_guessed():
    result = run_compute("date_compare", ["03/04/2020", "2020-05-05"])
    assert not result.ok
    assert "unambiguous" in result.error and "YYYY-MM-DD" in result.error
    # …but one that cannot be misread is accepted
    assert run_compute("date_compare", ["25/12/2020", "2020-05-05"]).value == "later than"


# ---- refusals ---------------------------------------------------------------------------------

def test_divide_by_zero_is_an_error_not_an_exception():
    result = run_compute("divide", ["10", "0"])
    assert not result.ok and "zero" in result.error


def test_malformed_operands_are_refused_with_something_the_model_can_act_on():
    result = run_compute("subtract", ["about a hundred", "89"])
    assert not result.ok
    assert "about a hundred" in result.error


def test_an_unsupported_operation_lists_the_supported_ones():
    result = run_compute("integrate", ["1", "2"])
    assert not result.ok
    assert "subtract" in result.error and "version_compare" in result.error


def test_arity_is_enforced():
    assert not run_compute("subtract", ["1"]).ok
    assert not run_compute("subtract", ["1", "2", "3"]).ok
    assert not run_compute("compare", []).ok


def test_operand_count_and_length_are_bounded():
    assert not run_compute("add", [str(i) for i in range(MAX_OPERANDS + 1)]).ok
    assert not run_compute("add", ["1", "x" * 200]).ok


def test_there_is_no_way_to_ask_for_code_execution():
    """V2 hardening §11. `operation` is an enum lookup; an operand is a literal that is
    parsed, never evaluated."""
    for hostile in ["eval", "exec", "__import__", "os.system", "1+1", "subprocess"]:
        assert not run_compute(hostile, ["1", "2"]).ok
    # and an operand that looks like an expression is simply not a number
    assert not run_compute("add", ["__import__('os').system('echo hi')", "1"]).ok
    assert not run_compute("add", ["2*3", "1"]).ok


def test_every_declared_operation_is_implemented():
    for op in ComputeOp:
        result = run_compute(op.value, ["2019-01-01", "2020-01-01"] if op is ComputeOp.DATE_COMPARE
                             else (["1.2.3", "1.2.4"] if op is ComputeOp.VERSION_COMPARE
                                   else ["4", "2"]))
        assert result.ok, f"{op.value}: {result.error}"


# ---- parsers ------------------------------------------------------------------------------------

def test_number_parsing():
    assert parse_number("$1,299.99") == pytest.approx(1299.99)
    assert parse_number("12%") == pytest.approx(12.0)
    assert parse_number("-4.5") == pytest.approx(-4.5)
    assert parse_number("") is None
    assert parse_number("many") is None


def test_version_parsing_refuses_the_ambiguous():
    assert parse_version("3.x") is None
    assert parse_version("2024a") is None
    assert parse_version("v3.12") is not None


def test_date_parsing_refuses_the_ambiguous():
    assert parse_date("03/04/2020") is None
    assert parse_date("2020-02-30") is None
    assert parse_date("Jan 5, 2024") is not None
