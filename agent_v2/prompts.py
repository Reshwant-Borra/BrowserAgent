"""Prompt assembly under a hard budget.

Every model call gets exactly seven blocks and nothing else (V2 spec §11):
system rules, the goal, retrieved memory, compact task state, a short recent-action window,
the tab list, and the current page. No conversation history, no prior observations, no
full memory dump. `build_context` returns the prompt *and* the per-block token counts, so
"is the prompt growing?" is a measured number rather than a hope.

The system block is written to be the same bytes on every call, which also makes it the
stable prefix Ollama's prompt cache can reuse across steps (V2 spec §23 Optimization D/E).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Optional

from agent.token_budget import count_tokens, trim_to_token_budget
from agent_v2.state import TaskState

SYSTEM = """You are BrowserAgent, driving a real web browser to finish the user's goal.

Each turn you see the current page and reply with ONE JSON action. No prose, no markdown.

ACTIONS
open_url    {"action":"open_url","url":"https://..."}          navigate this tab
click       {"action":"click","target":N}                       N is an id from INTERACTIVE
type        {"action":"type","target":N,"text":"...","submit":true}  submit=true presses Enter
select      {"action":"select","target":N,"value":"option label"}
scroll      {"action":"scroll","direction":"down"}              reveals lazy/paginated content
back        {"action":"back"}
extract     {"action":"extract","target":N}                     ONLY to read an element whose
            text is cut off in VISIBLE TEXT. Everything under VISIBLE TEXT you can already
            read — extracting it again tells you nothing and wastes a step.
open_tab    {"action":"open_tab","url":"https://..."}           new tab, keeps this one
switch_tab  {"action":"switch_tab","tab_id":N}                  N is an id from TABS
close_agent_created_tab {"action":"close_agent_created_tab","tab_id":N}   only tabs you opened
wait        {"action":"wait","ms":1000}                         only after something is loading
need_user   {"action":"need_user","message":"what the human must do"}
finish      {"action":"finish","answer":"the complete answer for the user"}

EVERY action also takes:
  "reason"  one short clause on why this action now
  "expect"  optional: text you expect to see afterwards, used to verify the action worked
  "state_updates": {"add_facts":[],"completed":[],"pending":[]}
    add_facts = concrete findings worth keeping. THIS IS THE ONLY MEMORY YOU HAVE between
    steps, so record every result the goal asks for the moment you see it.

RULES
- FIRST, every turn: if CURRENT PAGE or FACTS already contain what the goal asks for,
  finish NOW with that content in "answer". Do not navigate, extract or scroll to re-find
  something you can already read. Finishing early is correct, not lazy.
- Never invent a URL. open_url only with a URL taken from the page, one you are certain
  exists, or a search engine query URL. If you are looking for something, search for it.
- Only use target ids that appear in INTERACTIVE right now. Ids change every turn.
- Prefer typing a query and submit:true over hunting for a search button.
- If the page has nothing useful, scroll or open_url somewhere better instead of repeating.
- Never type into a password field, and never guess a credential, code, or CAPTCHA answer.
  Use need_user for login, 2FA, passkeys, CAPTCHAs, and anything irreversible you were not
  explicitly asked to do (purchases, deletions, sending messages, account changes).
- Do not repeat an action that just failed; do something different.
- finish only when the goal is actually satisfied, and put the real content in "answer" —
  the user sees "answer" and nothing else.
"""

def render_page(obs, *, token_budget: int = 1500, element_share: float = 0.55) -> str:
    """The page as the model sees it.

    Not `PageObservation.render_compact` + a trim: trimming a rendered page top-down keeps
    the element list and throws away the visible text, which is usually the part that
    actually answers the question. So the two are budgeted separately, and each says how
    much it dropped — "there is more below" is information the model needs in order to
    decide to scroll rather than conclude.
    """
    header = ["PAGE", f"title: {obs.title}", f"url: {obs.url}"]
    if obs.modal_present:
        header.append("a dialog is open and probably blocks the rest of the page")

    element_budget = int(token_budget * element_share)
    text_budget = token_budget - element_budget

    # Real pages repeat the same link in a top nav, a sidebar and a footer. Listing all
    # three spends the element budget on choices that do the same thing and pushes the page
    # text out of the prompt entirely — so identical (role, name) pairs collapse to their
    # first occurrence, which is also the one highest on the page.
    element_lines: list[str] = []
    used = 0
    shown = 0
    seen: set[tuple[str, str]] = set()
    for element in obs.elements:
        key = (element.role, element.name.strip().lower())
        if key in seen and not element.options and not element.value:
            continue
        seen.add(key)
        line = _element_line(element)
        cost = count_tokens(line)
        if used + cost > element_budget:
            element_lines.append(f"... {len(obs.elements) - shown} more elements not shown")
            break
        element_lines.append(line)
        shown += 1
        used += cost

    text_lines: list[str] = []
    used = 0
    for text in obs.visible_text:
        cost = count_tokens(text)
        if used + cost > text_budget:
            text_lines.append("... more text below — scroll to see it")
            break
        text_lines.append(f'"{text}"')
        used += cost

    return "\n".join(header + ["", "INTERACTIVE"] + (element_lines or ["(nothing clickable)"])
                     + ["", "VISIBLE TEXT"] + (text_lines or ["(no text)"]))


def _element_line(element) -> str:
    flags = []
    if element.disabled:
        flags.append("disabled")
    if element.selected:
        flags.append("selected")
    if element.checked is True:
        flags.append("checked")
    suffix = f" ({', '.join(flags)})" if flags else ""
    if element.options:
        suffix += f" options={element.options[:12]}"
    if element.sensitive:
        suffix += " [password field — never type here]"
    elif element.value:
        suffix += f' value="{element.value[:60]}"'
    return f'[{element.id}] {element.role} "{element.name}"{suffix}'


_DEFAULT_BUDGETS = {
    "goal": 120,
    "memory": 380,
    "state": 700,
    "recent": 260,
    "tabs": 140,
    "page": 1500,
    "hint": 260,
}


@dataclass
class ContextBudget:
    """Per-block ceilings, plus a total ceiling that is enforced by squeezing the page block
    (the only block that is both large and reconstructible on the next observation)."""

    max_total_tokens: int = 3600
    blocks: dict[str, int] = field(default_factory=lambda: dict(_DEFAULT_BUDGETS))

    def limit(self, name: str) -> int:
        return self.blocks.get(name, 200)


@dataclass
class BuiltContext:
    prompt: str
    block_tokens: dict[str, int]

    @property
    def total_tokens(self) -> int:
        return sum(self.block_tokens.values())


def build_context(
    *,
    goal: str,
    state: TaskState,
    page_render: str,
    memory_render: str = "",
    tabs_render: str = "",
    hint: str = "",
    budget: Optional[ContextBudget] = None,
) -> BuiltContext:
    budget = budget or ContextBudget()

    sections: list[tuple[str, str, str]] = [
        ("goal", "GOAL", goal.strip()),
        ("memory", "WHAT YOU LEARNED IN EARLIER TASKS", memory_render.strip()),
        ("state", "TASK STATE", state.render()),
        ("recent", "YOUR LAST FEW ACTIONS", state.render_recent_actions()),
        ("tabs", "TABS", tabs_render.strip()),
        ("hint", "IMPORTANT", hint.strip()),
        ("page", "CURRENT PAGE", page_render.strip()),
    ]

    rendered: dict[str, str] = {}
    block_tokens: dict[str, int] = {"system": count_tokens(SYSTEM)}
    for name, heading, body in sections:
        if not body:
            continue
        trimmed = trim_to_token_budget(body, budget.limit(name))
        rendered[name] = f"{heading}\n{trimmed}"
        block_tokens[name] = count_tokens(rendered[name])

    # Total enforcement: if we are over, the page block absorbs the difference. Everything
    # else in the prompt is either irreplaceable (state/goal) or already tiny.
    overflow = sum(block_tokens.values()) - budget.max_total_tokens
    if overflow > 0 and "page" in rendered:
        allowed = max(300, block_tokens["page"] - overflow)
        rendered["page"] = trim_to_token_budget(rendered["page"], allowed)
        block_tokens["page"] = count_tokens(rendered["page"])

    order = ["goal", "memory", "state", "recent", "tabs", "hint", "page"]
    body = "\n\n".join(rendered[name] for name in order if name in rendered)
    prompt = f"{SYSTEM}\n\n{body}\n\nReply with one JSON action.\n"
    return BuiltContext(prompt=prompt, block_tokens=block_tokens)


# --- end-of-task memory extraction (V2 spec §15) --------------------------------------

MEMORY_EXTRACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "memories": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "type": {"type": "string",
                             "enum": ["preference", "user_fact", "site", "strategy", "lesson"]},
                    "text": {"type": "string"},
                    "domain": {"type": ["string", "null"]},
                    "importance": {"type": ["number", "null"]},
                },
                "required": ["type", "text"],
            },
        },
        "procedure": {
            "type": ["object", "null"],
            "properties": {
                "goal_pattern": {"type": "string"},
                "domain": {"type": ["string", "null"]},
                "steps": {"type": "array", "items": {"type": "string"}},
            },
        },
    },
    "required": ["memories"],
}


def build_memory_extraction_prompt(state: TaskState, domains: list[str], outcome: str) -> str:
    """One call, at the end of a task. Asks for durable knowledge only — the wording is
    deliberately about *future unrelated tasks*, because that is the filter that keeps
    "clicked element 23" out of long-term memory (V2 spec §13/§15)."""
    facts = "\n".join(f"- {f}" for f in state.facts[:12]) or "- (none)"
    steps = "\n".join(f"- {c}" for c in state.completed[:8]) or "- (none)"
    problems = "\n".join(f"- {f}" for f in state.failures[:3]) or "- (none)"
    return f"""You just finished a browser task. Extract at most 4 things worth remembering
for a COMPLETELY DIFFERENT task weeks from now. Reply with JSON only.

GOAL: {state.goal}
OUTCOME: {outcome}
SITES USED: {", ".join(domains[:6]) or "(none)"}
WHAT WAS FOUND:
{facts}
WHAT WAS DONE:
{steps}
WHAT WENT WRONG:
{problems}

Keep ONLY durable knowledge:
  preference / user_fact — something lasting about the user
  site      — how a website behaves ("results only load when you scroll")
  strategy  — an approach that worked and would work again
  lesson    — a failure and what to do instead
Reject anything task-specific ("found 7 results", "clicked Search"), anything about page
mechanics that will not recur, and anything containing a password, code, or personal secret.
Return an empty list if nothing here is durable — that is a normal and correct answer.

Also return "procedure" (or null) if a repeatable sequence worked: a short goal_pattern, the
domain, and 3-6 generic steps.

{json.dumps({"memories": [{"type": "site", "text": "...", "domain": "example.com", "importance": 0.6}], "procedure": None})}
"""
