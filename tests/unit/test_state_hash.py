from __future__ import annotations

from browser.page_model import ElementRef, SelectorHint
from browser.state_hash import compute_state_hash


def el(id_, role, name, disabled=False, selected=False, checked=None) -> ElementRef:
    return ElementRef(id=id_, role=role, name=name, disabled=disabled, selected=selected,
                       checked=checked, selector_hint=SelectorHint(css="a", nth=id_))


def test_identical_state_hashes_identically():
    a = [el(1, "link", "Home"), el(2, "button", "Go")]
    b = [el(9, "link", "Home"), el(3, "button", "Go")]  # different numeric ids, same identity
    h1 = compute_state_hash("http://x/1", "T", a, ["hi"])
    h2 = compute_state_hash("http://x/1", "T", b, ["hi"])
    assert h1 == h2  # numeric element ids must not affect the hash


def test_different_url_changes_hash():
    a = [el(1, "link", "Home")]
    h1 = compute_state_hash("http://x/1", "T", a, [])
    h2 = compute_state_hash("http://x/2", "T", a, [])
    assert h1 != h2


def test_disabled_state_changes_hash():
    a = [el(1, "button", "Go", disabled=False)]
    b = [el(1, "button", "Go", disabled=True)]
    h1 = compute_state_hash("http://x", "T", a, [])
    h2 = compute_state_hash("http://x", "T", b, [])
    assert h1 != h2


def test_visible_text_changes_hash():
    a = [el(1, "link", "Home")]
    h1 = compute_state_hash("http://x", "T", a, ["foo"])
    h2 = compute_state_hash("http://x", "T", a, ["bar"])
    assert h1 != h2
