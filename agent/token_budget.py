"""Deterministic token budgeting helpers.

Live model responses provide the authoritative total prompt token count when the backend
returns it. These helpers provide stable per-block estimates so long-horizon runs can see
which prompt section is growing without introducing a tokenizer dependency or a remote
tokenization call into the critical path.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

_TOKEN_RE = re.compile(r"\w+|[^\w\s]", re.UNICODE)


def count_tokens(text: str) -> int:
    if not text:
        return 0
    return len(_TOKEN_RE.findall(text))


def trim_to_token_budget(text: str, max_tokens: int) -> str:
    if max_tokens <= 0 or not text:
        return ""
    if count_tokens(text) <= max_tokens:
        return text
    kept: list[str] = []
    used = 0
    for line in text.splitlines():
        line_tokens = count_tokens(line)
        if used + line_tokens > max_tokens:
            break
        kept.append(line)
        used += line_tokens
    if not kept:
        tokens = _TOKEN_RE.findall(text)
        return " ".join(tokens[:max_tokens])
    rendered = "\n".join(kept)
    if count_tokens(rendered) > max_tokens:
        return " ".join(_TOKEN_RE.findall(rendered)[:max_tokens])
    return rendered


@dataclass(frozen=True)
class PromptBlock:
    name: str
    text: str

    @property
    def tokens(self) -> int:
        return count_tokens(self.text)
