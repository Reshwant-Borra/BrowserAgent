"""Deterministic tests for research/discovery.py (Phase 5B corrective pass, Section 16 of
docs/PHASE5B_REPORT.md's fact/link test matrix): candidate-link enumeration must survive
5/20/50 links, duplicates, irrelevant nav chrome, DuckDuckGo's redirect wrapper, and non-http
hrefs before any model ranking happens; selection must never trust an id outside the
candidate set it was given.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from browser.page_model import ElementRef, PageObservation, SelectorHint
from inference.llama_client import CompletionResult
from research.discovery import (
    LinkSelectionError,
    extract_candidate_links,
    select_relevant_links,
)


def _link(id_: int, href: str, name: str = "Result") -> ElementRef:
    return ElementRef(id=id_, role="link", name=name, href=href, selector_hint=SelectorHint(css="a", nth=id_))


def _observation(elements: list[ElementRef]) -> PageObservation:
    return PageObservation(url="https://duckduckgo.com/html/?q=x", title="x at DuckDuckGo", elements=elements)


# ---- candidate enumeration ------------------------------------------------

def test_extract_candidate_links_basic_count():
    elements = [_link(i, f"https://site{i}.example.com/article", f"Article {i}") for i in range(1, 6)]
    candidates = extract_candidate_links(_observation(elements))
    assert len(candidates) == 5
    assert {c.url for c in candidates} == {e.href for e in elements}


def test_extract_candidate_links_handles_20_and_50_links():
    for count in (20, 50):
        elements = [_link(i, f"https://site{i}.example.com/article", f"Article {i}") for i in range(1, count + 1)]
        candidates = extract_candidate_links(_observation(elements), max_candidates=100)
        assert len(candidates) == count


def test_extract_candidate_links_caps_at_max_candidates():
    elements = [_link(i, f"https://site{i}.example.com/article") for i in range(1, 51)]
    candidates = extract_candidate_links(_observation(elements), max_candidates=10)
    assert len(candidates) == 10


def test_extract_candidate_links_dedupes_duplicate_urls():
    elements = [
        _link(1, "https://example.com/article", "First mention"),
        _link(2, "https://example.com/article", "Same link again"),
    ]
    candidates = extract_candidate_links(_observation(elements))
    assert len(candidates) == 1
    assert candidates[0].text == "First mention"  # keeps the first (topmost/highest-ranked) occurrence


def test_extract_candidate_links_dedupes_fragment_only_variants():
    elements = [
        _link(1, "https://example.com/article#section1", "A"),
        _link(2, "https://example.com/article#section2", "B"),
    ]
    candidates = extract_candidate_links(_observation(elements))
    assert len(candidates) == 1


def test_extract_candidate_links_filters_nav_chrome():
    elements = [
        _link(1, "https://duckduckgo.com/about", "About"),
        _link(2, "https://duckduckgo.com/privacy", "Privacy"),
        _link(3, "https://example.com/real-result", "Next"),  # blocklisted text, even off-domain
        _link(4, "https://example.com/real-article", "A real article"),
    ]
    candidates = extract_candidate_links(_observation(elements))
    assert [c.url for c in candidates] == ["https://example.com/real-article"]


def test_extract_candidate_links_resolves_ddg_redirect():
    wrapped = "https://duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Farticle&rut=abc123"
    elements = [_link(1, wrapped, "A real result")]
    candidates = extract_candidate_links(_observation(elements))
    assert len(candidates) == 1
    assert candidates[0].url == "https://example.com/article"


def test_extract_candidate_links_drops_non_http_hrefs():
    elements = [
        _link(1, "javascript:void(0)", "JS link"),
        _link(2, "mailto:someone@example.com", "Email"),
        _link(3, "/settings", "Relative nav link"),
        _link(4, "https://example.com/real", "Real result"),
    ]
    candidates = extract_candidate_links(_observation(elements))
    assert [c.url for c in candidates] == ["https://example.com/real"]


def test_extract_candidate_links_excludes_caller_specified_domains():
    elements = [
        _link(1, "https://blocked.example.com/a", "Blocked"),
        _link(2, "https://ok.example.com/a", "OK"),
    ]
    candidates = extract_candidate_links(_observation(elements), exclude_domains={"blocked.example.com"})
    assert [c.url for c in candidates] == ["https://ok.example.com/a"]


def test_extract_candidate_links_ignores_non_link_roles():
    elements = [
        ElementRef(id=1, role="button", name="Search", href=None, selector_hint=SelectorHint(css="button", nth=0)),
        _link(2, "https://example.com/real", "Real result"),
    ]
    candidates = extract_candidate_links(_observation(elements))
    assert [c.url for c in candidates] == ["https://example.com/real"]


# ---- id-based selection (never trust a re-typed URL) ----------------------

class _FakeSelectionClient:
    def __init__(self, response_text: str):
        self.response_text = response_text
        self.endpoint = "fake://research"
        self.last_prompt: str | None = None

    async def complete(self, prompt, grammar=None, max_tokens=256, json_schema=None) -> CompletionResult:
        self.last_prompt = prompt
        return CompletionResult(text=self.response_text, total_latency_ms=1.0)

    async def health_check(self) -> bool:
        return True


def _candidates():
    elements = [
        _link(11, "https://a.example.com/1", "A"),
        _link(22, "https://b.example.com/1", "B"),
        _link(33, "https://c.example.com/1", "C"),
    ]
    return extract_candidate_links(_observation(elements))


def test_select_relevant_links_returns_selected_candidates():
    client = _FakeSelectionClient(json.dumps({"selected_ids": [22, 11]}))
    selected = asyncio.run(select_relevant_links(client, "find B and A", _candidates()))
    assert [c.id for c in selected] == [22, 11]
    assert [c.url for c in selected] == ["https://b.example.com/1", "https://a.example.com/1"]


def test_select_relevant_links_drops_hallucinated_ids():
    client = _FakeSelectionClient(json.dumps({"selected_ids": [22, 999, 11]}))
    selected = asyncio.run(select_relevant_links(client, "find things", _candidates()))
    assert [c.id for c in selected] == [22, 11]  # 999 was never a real candidate id


def test_select_relevant_links_caps_at_max_select():
    client = _FakeSelectionClient(json.dumps({"selected_ids": [11, 22, 33]}))
    selected = asyncio.run(select_relevant_links(client, "find things", _candidates(), max_select=2))
    assert len(selected) == 2


def test_select_relevant_links_empty_candidates_skips_model_call():
    client = _FakeSelectionClient(json.dumps({"selected_ids": []}))
    selected = asyncio.run(select_relevant_links(client, "find things", []))
    assert selected == []
    assert client.last_prompt is None  # never called the model with an empty candidate list


def test_select_relevant_links_none_relevant_returns_empty():
    client = _FakeSelectionClient(json.dumps({"selected_ids": []}))
    selected = asyncio.run(select_relevant_links(client, "find things", _candidates()))
    assert selected == []


def test_select_relevant_links_malformed_json_raises():
    client = _FakeSelectionClient("not json")
    with pytest.raises(LinkSelectionError):
        asyncio.run(select_relevant_links(client, "find things", _candidates()))


def test_select_relevant_links_schema_invalid_raises():
    client = _FakeSelectionClient(json.dumps({"selected_ids": "not-a-list"}))
    with pytest.raises(LinkSelectionError):
        asyncio.run(select_relevant_links(client, "find things", _candidates()))


def test_select_relevant_links_prompt_uses_ids_not_urls_only():
    client = _FakeSelectionClient(json.dumps({"selected_ids": [11]}))
    asyncio.run(select_relevant_links(client, "find things", _candidates()))
    assert "[11]" in client.last_prompt and "[22]" in client.last_prompt and "[33]" in client.last_prompt
