from __future__ import annotations

from agent.schemas import ExpectedResult
from agent.verifier import check
from browser.page_model import ElementRef, PageObservation, SelectorHint


def obs(url="http://x/assignments/42", title="Assignment Page", elements=None, texts=None) -> PageObservation:
    return PageObservation(url=url, title=title, elements=elements or [], visible_text=texts or [],
                            state_hash="h")


def test_no_assertions_trivially_passes():
    result = check(ExpectedResult(), obs())
    assert result.passed is True
    assert result.checks == []


def test_url_contains_pass_and_fail():
    assert check(ExpectedResult(url_contains="/assignments/"), obs()).passed is True
    assert check(ExpectedResult(url_contains="/nope/"), obs()).passed is False


def test_title_contains():
    assert check(ExpectedResult(title_contains="Assignment"), obs()).passed is True
    assert check(ExpectedResult(title_contains="Dashboard"), obs()).passed is False


def test_page_contains_checks_element_names_and_visible_text():
    o = obs(texts=["Recursive Sort", "Due Sunday"])
    assert check(ExpectedResult(page_contains="recursive sort"), o).passed is True
    assert check(ExpectedResult(page_contains="not present"), o).passed is False


def test_element_present_and_absent():
    o = obs(texts=["Submission Complete"])
    assert check(ExpectedResult(element_present="Submission Complete"), o).passed is True
    assert check(ExpectedResult(element_absent="Login"), o).passed is True
    assert check(ExpectedResult(element_absent="Submission Complete"), o).passed is False


def test_multiple_assertions_all_must_pass():
    o = obs(url="http://x/assignments/42", texts=["Recursive Sort"])
    result = check(ExpectedResult(url_contains="/assignments/", page_contains="Recursive Sort"), o)
    assert result.passed is True
    assert len(result.checks) == 2

    result2 = check(ExpectedResult(url_contains="/assignments/", page_contains="not there"), o)
    assert result2.passed is False
