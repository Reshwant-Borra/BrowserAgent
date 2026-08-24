"""In-memory representation of one browser observation.

Element identity invariant: `ElementRef.id` is only valid for the `PageObservation` it was
generated in. IDs are reassigned fresh on every observation from a positional (selector,
nth-index) pair — never a cached Playwright handle, which would go stale the moment the
DOM re-renders. The executor always re-resolves `selector_hint` against the *current* page
right before acting on it.
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class SelectorHint(BaseModel):
    """How to re-find this element live: `page.locator(css).nth(nth)`."""
    css: str
    nth: int


class ElementRef(BaseModel):
    id: int
    role: str
    name: str
    text: Optional[str] = None
    value: Optional[str] = None
    disabled: bool = False
    selected: bool = False
    checked: Optional[bool] = None
    options: Optional[list[str]] = None
    href: Optional[str] = None
    sensitive: bool = False  # e.g. a password field — never log/persist its value or typed text
    selector_hint: SelectorHint


class PageObservation(BaseModel):
    url: str
    title: str
    elements: list[ElementRef] = Field(default_factory=list)
    visible_text: list[str] = Field(default_factory=list)
    modal_present: bool = False
    state_hash: str = ""
    char_count: int = 0
    element_count: int = 0
    truncated: bool = False

    def element_by_id(self, element_id: int) -> Optional[ElementRef]:
        for el in self.elements:
            if el.id == element_id:
                return el
        return None

    def render_compact(self, max_chars: int, max_visible_text_items: int) -> str:
        """The exact text the model sees. Deterministic given the same observation +
        budget — no randomness, no hidden state."""
        lines = [
            "PAGE",
            f"title: {self.title}",
            f"url: {self.url}",
        ]
        if self.modal_present:
            lines.append("modal: present (a dialog is currently open and likely blocks other interaction)")
        lines.append("")
        lines.append("INTERACTIVE")
        for el in self.elements:
            flags = []
            if el.disabled:
                flags.append("disabled")
            if el.selected:
                flags.append("selected")
            if el.checked is True:
                flags.append("checked")
            flag_str = f" ({', '.join(flags)})" if flags else ""
            extra = ""
            if el.options:
                extra = f" options={el.options}"
            lines.append(f'[{el.id}] {el.role} "{el.name}"{flag_str}{extra}')
        lines.append("")
        lines.append("VISIBLE TEXT")
        for t in self.visible_text[:max_visible_text_items]:
            lines.append(f'"{t}"')

        rendered = "\n".join(lines)
        truncated = False
        if len(rendered) > max_chars:
            rendered = rendered[:max_chars]
            truncated = True
        return rendered if not truncated else rendered + "\n...[truncated]"
