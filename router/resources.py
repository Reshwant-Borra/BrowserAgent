"""Deterministic resolution of the semantic planner's abstract ResourceRequirements into
concrete URLs (Section 7 of the semantic planner task). This is the layer that owns "what
resources actually exist" — the planner only ever proposes *what kind* of resource is needed
plus a semantic description; this module decides the real answer, and is the only place a
"could not resolve" (clarification-needed) outcome originates from.

Only `explicit_urls` and `open_tabs` requirements go through real resolution here.
`current_page` and `web_discovery` are structurally understood by the plan translator
(router/policy.py) without needing this module at all — see that module's docstring.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from pydantic import TypeAdapter, ValidationError

from agent.config import AppConfig
from browser.tabs import TabCandidate, TabListUnavailable, list_open_tabs
from inference.llama_client import InferenceClient
from router.extract import extract_urls, normalize_url
from router.plan_schema import ResourceKind, ResourceRequirement, ResourceSelection

_TAB_SELECTION_SCHEMA = TypeAdapter(ResourceSelection).json_schema()
_TAB_SELECTION_SCHEMA["title"] = "ResourceSelection"

_TAB_SELECTION_PROMPT = """You are selecting which of the user's currently open browser tabs
match a description. Respond with ONLY a single JSON object matching this shape (no prose, no
markdown fences):

{{"selected_ids": [<id>, ...]}}

Rules:
- Choose ids ONLY from the tab list below — never invent an id that is not listed.
- Select every tab that clearly and specifically matches the description, not just the single
  best match — but be conservative: a tab is only a match if its title/URL genuinely relates
  to the description, not merely because it's the "closest available guess."
- If NO tab is a clear match, return {{"selected_ids": []}} — do not select unrelated tabs
  (e.g. email, video, or shopping tabs) just because nothing better is available. An empty
  result is the correct, expected answer when the described resource isn't actually open.

Description of what's needed: {description}

Open tabs:
{tabs}
"""


class ResourceResolutionError(ValueError):
    """Raised only for a genuine model-output-shape violation (schema validation failure on
    the tab-selection call). A resource simply not being findable is NOT an error — it's an
    unresolved result, since the caller offers clarification rather than crashing."""


@dataclass
class ResolvedResource:
    requirement_index: int
    urls: list[str]
    labels: dict[str, str] = field(default_factory=dict)  # url -> title, UI/debug only
    # url -> canonical open-tab id, only ever populated for OPEN_TABS resolutions. This is
    # what lets the plan translator (router/policy.py) preserve "this is an existing browser
    # tab" identity instead of collapsing the resolved tab down to a bare URL.
    tab_ids: dict[str, int] = field(default_factory=dict)


@dataclass
class ResourceResolution:
    resolved: list[ResolvedResource]
    unresolved: list[int]  # requirement indices that resolved to nothing

    def all_urls(self) -> list[str]:
        seen: set[str] = set()
        urls: list[str] = []
        for r in self.resolved:
            for u in r.urls:
                if u not in seen:
                    seen.add(u)
                    urls.append(u)
        return urls

    def urls_for(self, requirement_index: int) -> list[str]:
        for r in self.resolved:
            if r.requirement_index == requirement_index:
                return r.urls
        return []


class ResourceResolver:
    """One instance per prompt/routing call — caches the open-tab enumeration so a plan with
    several `open_tabs` requirements only lists tabs once."""

    def __init__(self, config: AppConfig, client: InferenceClient, prompt_text: str):
        self.config = config
        self.client = client
        self.prompt_text = prompt_text
        self._tab_cache: list[TabCandidate] | None = None

    async def resolve(
        self, requirement_index: int, req: ResourceRequirement
    ) -> ResolvedResource | None:
        """Returns None when nothing could be resolved (caller treats the requirement index
        as unresolved)."""
        if req.kind == ResourceKind.EXPLICIT_URLS:
            urls = extract_urls(self.prompt_text)
            return ResolvedResource(requirement_index, urls) if urls else None
        if req.kind == ResourceKind.OPEN_TABS:
            return await self._resolve_open_tabs(requirement_index, req)
        # current_page / web_discovery: nothing to resolve here (see module docstring) —
        # the translator never routes these kinds through resolve() in the first place, but
        # this stays a safe no-op rather than raising if it ever is.
        return None

    async def get_open_tabs(self) -> list[TabCandidate]:
        if self._tab_cache is not None:
            return self._tab_cache
        if self.config.browser.mode != "cdp_attach":
            self._tab_cache = []
            return []
        try:
            self._tab_cache = await list_open_tabs(self.config.browser.cdp_endpoint)
        except TabListUnavailable:
            self._tab_cache = []
        return self._tab_cache

    async def _resolve_open_tabs(
        self, requirement_index: int, req: ResourceRequirement
    ) -> ResolvedResource | None:
        tabs = await self.get_open_tabs()
        if not tabs:
            return None
        selected = await select_relevant_tabs(self.client, req.description or "relevant to the task", tabs)
        if not selected:
            return None
        return ResolvedResource(
            requirement_index,
            [t.url for t in selected],
            labels={t.url: t.title for t in selected},
            tab_ids={t.url: t.id for t in selected},
        )


async def select_relevant_tabs(
    client: InferenceClient,
    description: str,
    tabs: list[TabCandidate],
    max_tokens: int = 300,
) -> list[TabCandidate]:
    """Same anti-hallucination shape as research/discovery.py::select_relevant_links: the
    model returns tab *ids*, never URLs — nothing for it to mistype or invent. Any id outside
    the candidate set is dropped rather than trusted."""
    if not tabs:
        return []
    tab_block = "\n".join(f'[{t.id}] "{t.title}" -> {t.url}' for t in tabs)
    prompt = _TAB_SELECTION_PROMPT.format(description=description, tabs=tab_block)
    result = await client.complete(prompt, max_tokens=max_tokens, json_schema=_TAB_SELECTION_SCHEMA)
    try:
        raw = json.loads(result.text)
    except json.JSONDecodeError as exc:
        raise ResourceResolutionError(f"tab selection output was not valid JSON: {exc.msg}") from exc
    try:
        selection = ResourceSelection.model_validate(raw)
    except ValidationError as exc:
        raise ResourceResolutionError(f"tab selection output failed schema validation: {exc}") from exc

    by_id = {t.id: t for t in tabs}
    selected: list[TabCandidate] = []
    seen_urls: set[str] = set()
    for tab_id in selection.selected_ids:
        tab = by_id.get(tab_id)
        if tab is None:
            continue
        key = normalize_url(tab.url)
        if key in seen_urls:
            continue
        seen_urls.add(key)
        selected.append(tab)
    return selected
