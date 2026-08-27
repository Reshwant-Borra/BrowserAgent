"""Deterministic candidate-link enumeration + Qwen relevance selection for research source
discovery (Phase 5B corrective pass: "research source discovery cannot reliably enumerate
multiple links").

The old approach asked the model to transcribe URLs it saw on a search-results page into a
finish JSON string — fragile (output-token truncation, single-result loops, hallucinated or
duplicate URLs) and unnecessary work for the model: the browser observation
(browser/page_model.py::PageObservation) already knows every link's href and visible text
from one deterministic DOM extraction (browser/observer.py). This module enumerates those
candidates in code and asks the model only to select which ones are relevant, by id — never
to re-type a URL it can already point at.
"""
from __future__ import annotations

import json
import urllib.parse
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field, TypeAdapter, ValidationError

from agent.config import AppConfig
from browser.page_model import PageObservation
from browser.playwright_backend import PlaywrightBackend
from inference.llama_client import InferenceClient
from router.extract import normalize_url

# A single static search-results page rarely has more organic results than this; keep the
# candidate list (and therefore the selection prompt) small and bounded regardless of how
# many links the raw page actually contains (Section 14: bound candidate_links_per_round).
MAX_CANDIDATES_PER_ROUND = 40
DEFAULT_MAX_SELECT = 10

_NAV_DOMAIN_BLOCKLIST = {"duckduckgo.com", "www.duckduckgo.com"}
_NAV_TEXT_BLOCKLIST = {
    "about", "help", "privacy", "terms", "advertise", "contact", "contact us", "settings",
    "duckduckgo lite", "duckduckgo html", "feedback", "sign in", "log in", "sign up",
    "next", "previous", "more results", "images", "videos", "news", "maps", "shopping",
}


class CandidateLink(BaseModel):
    id: int
    url: str
    text: str


class LinkSelection(BaseModel):
    selected_ids: list[int] = Field(default_factory=list)


_SELECTION_SCHEMA = TypeAdapter(LinkSelection).json_schema()
_SELECTION_SCHEMA["title"] = "LinkSelection"

_SELECTION_PROMPT = """You are choosing which search results are worth reading for a research
task. Respond with ONLY a single JSON object matching this shape (no prose, no markdown fences):

{{"selected_ids": [<id>, ...]}}

Rules:
- Choose ids ONLY from the candidate list below — never invent an id that is not listed.
- Choose the most relevant, distinct results for the objective, up to {max_select} ids.
- Prefer results that look like they come from different sources/domains over near-duplicates.
- If none look relevant, return {{"selected_ids": []}}.

Objective: {objective}

Candidates:
{candidates}
"""


class LinkSelectionError(ValueError):
    """Raised when the model's structured selection output fails schema validation."""


def _resolve_redirect(href: str) -> str:
    """DuckDuckGo's no-JS `/html/` endpoint wraps organic result hrefs through
    `duckduckgo.com/l/?uddg=<encoded target>` for click tracking — unwrap that back to the
    real target before any domain filtering runs, or every organic result would be dropped
    as if it were `duckduckgo.com` navigation chrome."""
    parts = urllib.parse.urlsplit(href)
    if parts.netloc.lower().endswith("duckduckgo.com") and parts.path == "/l/":
        target = urllib.parse.parse_qs(parts.query).get("uddg", [None])[0]
        if target:
            return urllib.parse.unquote(target)
    return href


def extract_candidate_links(
    observation: PageObservation,
    exclude_domains: Optional[set[str]] = None,
    max_candidates: int = MAX_CANDIDATES_PER_ROUND,
) -> list[CandidateLink]:
    """Deterministic enumeration straight from the observation's own elements — the model
    never has to transcribe a URL it can already point at by id. Filters out same-site
    navigation chrome (about/privacy/pagination links) and non-http(s) hrefs, resolves
    DuckDuckGo's redirect wrapper, then dedupes by normalized URL, keeping the first
    (topmost, i.e. highest-ranked) occurrence."""
    exclude = {d.lower() for d in (exclude_domains or set())} | _NAV_DOMAIN_BLOCKLIST
    seen: set[str] = set()
    candidates: list[CandidateLink] = []
    for element in observation.elements:
        if element.role != "link" or not element.href:
            continue
        href = _resolve_redirect(element.href.strip())
        if not (href.startswith("http://") or href.startswith("https://")):
            continue
        domain = urllib.parse.urlsplit(href).netloc.lower()
        if domain in exclude:
            continue
        text = (element.name or "").strip()
        if text.lower() in _NAV_TEXT_BLOCKLIST:
            continue
        key = normalize_url(href)
        if key in seen:
            continue
        seen.add(key)
        candidates.append(CandidateLink(id=element.id, url=href, text=text[:160]))
        if len(candidates) >= max_candidates:
            break
    return candidates


async def select_relevant_links(
    client: InferenceClient,
    objective: str,
    candidates: list[CandidateLink],
    max_select: int = DEFAULT_MAX_SELECT,
    max_tokens: int = 300,
) -> list[CandidateLink]:
    """A small, tightly-scoped structured call: the model returns *ids*, never URLs — the
    infrastructure already owns the canonical URL for each id, so there is nothing left for
    the model to mistype, truncate, or hallucinate. Any id outside the candidate set (a
    hallucinated id) is silently dropped rather than trusted, same anti-hallucination
    philosophy as router/llm_router.py's target guard."""
    if not candidates:
        return []
    candidate_block = "\n".join(f'[{c.id}] "{c.text}" -> {c.url}' for c in candidates)
    prompt = _SELECTION_PROMPT.format(objective=objective, candidates=candidate_block, max_select=max_select)
    result = await client.complete(prompt, max_tokens=max_tokens, json_schema=_SELECTION_SCHEMA)
    try:
        raw = json.loads(result.text)
    except json.JSONDecodeError as exc:
        raise LinkSelectionError(f"link selection output was not valid JSON: {exc.msg}") from exc
    try:
        selection = LinkSelection.model_validate(raw)
    except ValidationError as exc:
        raise LinkSelectionError(f"link selection output failed schema validation: {exc}") from exc

    by_id = {c.id: c for c in candidates}
    selected: list[CandidateLink] = []
    seen_urls: set[str] = set()
    for candidate_id in selection.selected_ids:
        candidate = by_id.get(candidate_id)
        if candidate is None:
            continue
        key = normalize_url(candidate.url)
        if key in seen_urls:
            continue
        seen_urls.add(key)
        selected.append(candidate)
        if len(selected) >= max_select:
            break
    return selected


async def discover_sources(
    config: AppConfig,
    client: InferenceClient,
    objective: str,
    profile_dir: Path,
    max_sources: int = DEFAULT_MAX_SELECT,
    search_engine_url: Optional[str] = None,
) -> list[str]:
    """One bounded search round (Section 14 of the corrective pass: this pass validates a
    single round, not an unbounded search loop): open a search-results (or any listing) page,
    enumerate candidate links deterministically, ask the model to select the relevant ones by
    id, and return their real URLs — never model-transcribed text."""
    url = search_engine_url or f"https://duckduckgo.com/html/?q={urllib.parse.quote_plus(objective)}"
    backend = PlaywrightBackend(
        profile_dir, config.browser.headless, config.browser.action_timeout_ms,
        config.context.max_page_chars, config.context.max_visible_text_items,
    )
    await backend.start()
    try:
        await backend.open_url(url)
        observation = await backend.observe()
    finally:
        await backend.close()

    candidates = extract_candidate_links(observation)
    if not candidates:
        return []
    selected = await select_relevant_links(client, objective, candidates, max_select=max_sources)
    return [c.url for c in selected]
