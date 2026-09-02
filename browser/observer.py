"""Compact browser-state extraction.

One `page.evaluate()` round trip gathers every interactive element and a bounded set of
visible text snippets in a single JS pass (cheaper and simpler than N Playwright
round trips). The same CANONICAL_INTERACTIVE_SELECTOR string is used here to enumerate
elements and later by `playwright_backend.py` to re-resolve `(selector, nth)` back to a
live locator — the two sides only ever agree on element identity because they query the
same DOM with the same selector, never because a handle was cached across observations.
"""
from __future__ import annotations

from typing import Any

from browser.page_model import ElementRef, PageObservation, SelectorHint
from browser.state_hash import compute_state_hash

CANONICAL_INTERACTIVE_SELECTOR = (
    "a[href], button, input:not([type=hidden]), select, textarea, "
    "[role=button], [role=link], [role=checkbox], [role=radio], "
    "[role=tab], [role=menuitem], [role=combobox], [role=textbox]"
)

CANONICAL_TEXT_SELECTOR = "h1, h2, h3, h4, h5, h6, p, li, span, div"

#: Adds the block elements real content sites put facts in but the canonical selector never
#: matched — most importantly table cells, which meant every table on the web was invisible
#: to the agent except one `extract` call at a time. Kept as a separate constant, and passed
#: explicitly by agent_v2, so the legacy loop's observations stay byte-for-byte unchanged.
EXTENDED_TEXT_SELECTOR = (CANONICAL_TEXT_SELECTOR +
                           ", tr, td, th, caption, dd, dt, blockquote, figcaption, pre")

CANONICAL_MODAL_SELECTOR = '[role="dialog"]'

# Executed in the page context. Returns plain-JSON-serializable data only.
_EXTRACTION_JS = """
([interactiveSel, textSel, modalSel, maxTextNodes]) => {
  function isVisible(el) {
    if (el.hasAttribute('hidden')) return false;
    const style = window.getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    if (el.offsetParent === null && style.position !== 'fixed') return false;
    return true;
  }

  function accessibleName(el) {
    const ariaLabel = el.getAttribute('aria-label');
    if (ariaLabel) return ariaLabel.trim();

    const labelledBy = el.getAttribute('aria-labelledby');
    if (labelledBy) {
      const parts = labelledBy.split(/\\s+/).map(id => {
        const ref = document.getElementById(id);
        return ref ? ref.textContent.trim() : '';
      }).filter(Boolean);
      if (parts.length) return parts.join(' ');
    }

    if (el.id) {
      const label = document.querySelector(`label[for="${el.id}"]`);
      if (label && label.textContent.trim()) return label.textContent.trim();
    }
    const parentLabel = el.closest('label');
    if (parentLabel && parentLabel.textContent.trim()) return parentLabel.textContent.trim();

    const placeholder = el.getAttribute('placeholder');
    if (placeholder) return placeholder.trim();

    const title = el.getAttribute('title');
    if (title) return title.trim();

    if (el.tagName === 'INPUT' && (el.type === 'image')) {
      const alt = el.getAttribute('alt');
      if (alt) return alt.trim();
    }

    if (el.tagName === 'INPUT' && ['submit', 'button'].includes(el.type) && el.value) {
      return el.value.trim();
    }

    const text = (el.textContent || '').replace(/\\s+/g, ' ').trim();
    if (text) return text.slice(0, 120);

    return el.tagName.toLowerCase();
  }

  function roleOf(el) {
    const explicit = el.getAttribute('role');
    if (explicit) return explicit;
    const tag = el.tagName.toLowerCase();
    if (tag === 'a') return 'link';
    if (tag === 'button') return 'button';
    if (tag === 'select') return 'select';
    if (tag === 'textarea') return 'textbox';
    if (tag === 'input') {
      const t = (el.getAttribute('type') || 'text').toLowerCase();
      if (t === 'checkbox') return 'checkbox';
      if (t === 'radio') return 'radio';
      if (['submit', 'button', 'image'].includes(t)) return 'button';
      return 'textbox';
    }
    return tag;
  }

  const interactiveNodes = Array.from(document.querySelectorAll(interactiveSel));
  const elements = [];
  interactiveNodes.forEach((el, idx) => {
    if (!isVisible(el)) return;
    const role = roleOf(el);
    const disabled = el.disabled === true || el.getAttribute('aria-disabled') === 'true';
    const checked = (role === 'checkbox' || role === 'radio') ? !!el.checked : null;
    const selected = el.getAttribute('aria-selected') === 'true';
    let options = null;
    let value = null;
    if (el.tagName === 'SELECT') {
      options = Array.from(el.options).map(o => o.textContent.trim());
      const sel = el.options[el.selectedIndex];
      value = sel ? sel.textContent.trim() : null;
    } else if (el.tagName === 'INPUT' && el.type === 'password') {
      value = null; // never surface password field contents
    } else if (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') {
      value = el.value || '';
    }
    const sensitive = el.tagName === 'INPUT' && el.type === 'password';
    elements.push({
      nth: idx,
      role,
      name: accessibleName(el),
      href: el.tagName === 'A' ? el.getAttribute('href') : null,
      disabled,
      checked,
      selected,
      options,
      value,
      sensitive,
    });
  });

  const interactiveSet = new Set(interactiveNodes);
  const visibleText = [];
  const seen = new Set();

  // Content-first ordering. On a real site the first N text nodes in raw DOM order are
  // almost always navigation chrome, so the article/answer never reaches the model. When
  // the page declares a main-content landmark (a web standard, not a per-site rule) that
  // subtree is read first and the surrounding chrome second. Pages without landmarks —
  // including every test fixture — take the `document.body` path, which is exactly the
  // original single-pass behavior.
  const mainRoot = document.querySelector('main, [role="main"], article');
  const chromeSel = 'nav, header, footer, aside, [role="navigation"], [role="banner"], [role="contentinfo"]';

  // A table read as a flat list of cells is a table with its rows shuffled: the model sees
  // every name and every number but nothing tying one to the other, and confidently pairs
  // the wrong ones. So a <tr> is emitted as a single "cell | cell | cell" line and its own
  // cells are then suppressed. Only reachable when the caller's selector includes `tr`.
  function rowLine(el) {
    const cells = Array.from(el.children).filter(c => c.tagName === 'TD' || c.tagName === 'TH');
    if (!cells.length) return null;
    const texts = cells.map(c => c.textContent.replace(/\\s+/g, ' ').trim());
    for (const text of texts) { if (text) seen.add(text); }
    const row = texts.filter(Boolean).join(' | ');
    return row || null;
  }

  function collect(root, skipChrome, limit) {
    for (const el of Array.from(root.querySelectorAll(textSel))) {
      if (visibleText.length >= limit) return;
      if (!isVisible(el)) continue;
      if (skipChrome && el.closest(chromeSel)) continue;
      if (el.tagName === 'TR') {
        const row = rowLine(el);
        if (row && !seen.has(row)) { seen.add(row); visibleText.push(row.slice(0, 300)); }
        continue;
      }
      // A plain-text document (an RFC, a changelog, a log file) is one enormous <pre>, and
      // collapsing it to a single 200-character snippet keeps the header and throws away
      // everything the reader actually wants. Inside <pre> the newlines are the structure,
      // so each line becomes its own item.
      if (el.tagName === 'PRE') {
        for (const rawLine of (el.textContent || '').split('\\n')) {
          if (visibleText.length >= limit) return;
          const line = rawLine.replace(/\\s+/g, ' ').trim();
          if (!line || seen.has(line)) continue;
          seen.add(line);
          visibleText.push(line.slice(0, 200));
        }
        continue;
      }
      let insideInteractive = false;
      let p = el.parentElement;
      while (p) {
        if (interactiveSet.has(p)) { insideInteractive = true; break; }
        p = p.parentElement;
      }
      if (insideInteractive) continue;
      const direct = Array.from(el.childNodes)
        .filter(n => n.nodeType === Node.TEXT_NODE)
        .map(n => n.textContent.trim())
        .join(' ')
        .trim();
      const text = direct || el.textContent.replace(/\\s+/g, ' ').trim();
      if (!text || seen.has(text)) continue;
      seen.add(text);
      visibleText.push(text.slice(0, 200));
    }
  }

  if (mainRoot) {
    collect(mainRoot, true, maxTextNodes);
    collect(document.body, true, maxTextNodes);
  }
  collect(document.body, false, maxTextNodes);

  const modalPresent = !!document.querySelector(modalSel);

  return {
    url: window.location.href,
    title: document.title,
    elements,
    visibleText,
    modalPresent,
  };
}
"""


DEFAULT_MAX_TEXT_NODES = 40


async def extract_observation(page: Any, max_chars: int, max_visible_text_items: int,
                              max_text_nodes: int = DEFAULT_MAX_TEXT_NODES,
                              text_selector: str = CANONICAL_TEXT_SELECTOR) -> PageObservation:
    """`page` is a playwright.async_api.Page. Returns a fully-populated PageObservation,
    including its state_hash, computed here so callers never forget to hash.

    `max_text_nodes` bounds how much page text is *captured*, as distinct from how much is
    *rendered* (`max_visible_text_items`). It defaults to the historical 40 so the legacy
    loop's observations, memories and state hashes are byte-identical; agent_v2 raises it,
    because V2 budgets the rendered page in tokens and would rather trim a large capture
    than never see the paragraph that answers the question."""
    raw: dict = await page.evaluate(
        _EXTRACTION_JS,
        [CANONICAL_INTERACTIVE_SELECTOR, text_selector, CANONICAL_MODAL_SELECTOR,
         max_text_nodes],
    )

    elements: list[ElementRef] = []
    for i, e in enumerate(raw["elements"], start=1):
        elements.append(ElementRef(
            id=i,
            role=e["role"],
            name=e["name"],
            value=e.get("value"),
            disabled=e["disabled"],
            selected=e["selected"],
            checked=e["checked"],
            options=e["options"],
            href=e["href"],
            sensitive=e.get("sensitive", False),
            selector_hint=SelectorHint(css=CANONICAL_INTERACTIVE_SELECTOR, nth=e["nth"]),
        ))

    visible_text = raw["visibleText"]
    state_hash = compute_state_hash(raw["url"], raw["title"], elements, visible_text)

    obs = PageObservation(
        url=raw["url"],
        title=raw["title"],
        elements=elements,
        visible_text=visible_text,
        modal_present=raw["modalPresent"],
        state_hash=state_hash,
        element_count=len(elements),
    )
    rendered = obs.render_compact(max_chars, max_visible_text_items)
    obs.char_count = len(rendered)
    obs.truncated = rendered.endswith("...[truncated]")
    return obs
