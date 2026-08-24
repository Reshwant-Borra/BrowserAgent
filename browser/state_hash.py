"""Normalized page-state fingerprint.

Deliberately does NOT hash raw HTML (volatile attributes/whitespace/comment noise would
make the hash useless for no-op/loop detection) and does NOT include numeric element IDs
(those are positional and reassigned every observation, so two truly-identical page states
observed twice must hash the same even if unrelated re-renders reordered internal DOM
bookkeeping). It hashes url + title + an ordered (role, name, disabled, selected, checked)
tuple per element + a sample of visible text.
"""
from __future__ import annotations

import hashlib

from browser.page_model import ElementRef, PageObservation


def _element_signature(el: ElementRef) -> str:
    return f"{el.role}|{el.name}|{el.disabled}|{el.selected}|{el.checked}"


def compute_state_hash(url: str, title: str, elements: list[ElementRef],
                        visible_text: list[str], text_sample_size: int = 20) -> str:
    parts = [url, title]
    parts.extend(_element_signature(el) for el in elements)
    parts.extend(visible_text[:text_sample_size])
    normalized = "\x1f".join(parts)  # unit-separator: won't collide with real content
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def hash_observation(obs: PageObservation) -> str:
    return compute_state_hash(obs.url, obs.title, obs.elements, obs.visible_text)
