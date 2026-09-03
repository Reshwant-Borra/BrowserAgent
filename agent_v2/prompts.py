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
compute     {"action":"compute","operation":"subtract","operands":["159.00","89.00"],
             "labels":["Widget A","Widget B"],"evidence_ids":["ev_1","ev_2"]}
            BrowserAgent does the arithmetic and hands you the exact answer. Never work a
            number out in your head — ask for it. Operations: compare, min, max, sort_asc,
            sort_desc, add, subtract, multiply, divide, percent_of, percent_change, count,
            date_compare, version_compare.
need_user   {"action":"need_user","message":"what the human must do"}
finish      {"action":"finish","answer":"the complete answer for the user",
             "claims":[{"text":"Widget A costs $159.00","evidence_ids":["ev_1"],"kind":"source"}]}

EVERY action also takes:
  "reason"  one short clause on why this action now
  "expect"  optional: text you expect to see afterwards, used to verify the action worked
  "state_updates": {"add_facts":[],"completed":[],"pending":[]}
    add_facts = concrete findings worth keeping. THIS IS THE ONLY MEMORY YOU HAVE between
    steps, so record every result the goal asks for the moment you see it. Write them so
    they match the page you are looking at: a fact BrowserAgent cannot find on that page is
    kept, but marked (unverified), and cannot support your final answer.

EVIDENCE
Each line under EVIDENCE is something BrowserAgent recorded from a page you really opened,
or a calculation it really performed, and starts with the id it was given. In finish.claims
you may cite those ids and only those ids. You cannot invent an id, and an id from another
task will be rejected. kind is one of:
  source     — read off a page. Needs evidence_ids.
  derived    — a compute result. Cite the compute evidence id.
  synthesis  — your own judgement ("B looks like better value"). No id needed, but any
               number in it must still come from a page or a compute result.
  meta       — about the run itself ("two of the three pages loaded").

RULES
- FIRST, every turn: if CURRENT PAGE or FACTS already contain what the goal asks for,
  finish NOW with that content in "answer". Do not navigate, extract or scroll to re-find
  something you can already read. Finishing early is correct, not lazy. But a figure the
  goal wants worked out — a difference, a total, a percentage, a ranking, a span of time —
  is not on the page: the page holds the inputs and compute produces the answer. Having the
  inputs in front of you means you are ready to compute, not that you are ready to finish.
- Never invent a URL. open_url only with a URL taken from the page, one you are certain
  exists, or a search engine query URL. If you are looking for something, search for it.
- Only report what you have actually SEEN on a page during this task. You may not know what
  you think you know: versions, prices, dates and names change. If the goal needs a second
  source, go and open it — never fill the gap from your own knowledge.
- Every number, price, version, date, quotation and website name in "answer" must come from
  a page you opened in this task or from a compute result. Naming a site you did not open,
  as though you had read it, is the worst thing you can do here — worse than an incomplete
  answer. If you could not get something, say plainly which part is missing and why.
- Do not do arithmetic or compare numbers yourself. Use compute and report its result.
- Only use target ids that appear in INTERACTIVE right now. Ids change every turn.
- Prefer typing a query and submit:true over hunting for a search button.
- If the page has nothing useful, scroll or open_url somewhere better instead of repeating.
- Never type into a password field, and never guess a credential, code, or CAPTCHA answer.
  Use need_user for login, 2FA, passkeys, CAPTCHAs, and anything irreversible you were not
  explicitly asked to do (purchases, deletions, sending messages, account changes).
- Do not repeat an action that just failed; do something different.
- If the goal asks for several things, list them in state_updates.pending on your FIRST
  turn, and tick them off with state_updates.completed as you get them. STILL TO DO is your
  plan; work through it.
- finish only when the goal is actually satisfied, and put the real content in "answer" —
  the user sees "answer" and nothing else.
- Never finish with a placeholder, an apology, or "not found, please check X yourself".
  If you know where to look, go there. Only finish short if you have genuinely run out of
  ways to get the rest, and then say plainly what is missing and why.
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
    "evidence": 340,
    "recent": 260,
    "tabs": 140,
    "page": 1500,
    "hint": 260,
}


@dataclass
class ContextBudget:
    """Per-block ceilings, plus a total ceiling that is enforced by squeezing the page block
    (the only block that is both large and reconstructible on the next observation).

    The evidence block is a *selection* — never the whole ledger. Grounding must not be
    bought by letting the prompt grow with the run (V2 hardening §18/§19), so the ceiling
    here is what keeps a fifteen-step task's last prompt the same size as its third."""

    max_total_tokens: int = 3900
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
    evidence_render: str = "",
    budget: Optional[ContextBudget] = None,
) -> BuiltContext:
    budget = budget or ContextBudget()

    sections: list[tuple[str, str, str]] = [
        ("goal", "GOAL", goal.strip()),
        ("memory", "WHAT YOU LEARNED IN EARLIER TASKS", memory_render.strip()),
        ("state", "TASK STATE", state.render()),
        ("evidence", "EVIDENCE YOU MAY CITE", evidence_render.strip()),
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

    order = ["goal", "memory", "state", "evidence", "recent", "tabs", "hint", "page"]
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
  preference / user_fact — something lasting about THE USER (not about you)
  site      — how a website behaves ("results only load when you scroll")
  strategy  — an approach that worked and would work again
  lesson    — a failure and what to do instead

Never write a sentence about yourself. "I successfully retrieved X", "I can compare version
numbers", "I need to open both pages" are all worthless later: they are true of every task and
tell a future task nothing. Write about the user, the site, or the technique instead.
Never write down a value you happened to read — a price, a count, a version number, a date,
a search result. Those change, and a memory that has quietly gone stale is worse than no
memory at all. "Python 3.14.7 is the latest" is wrong within months; "python.org lists the
latest version on the downloads page" stays true.
Never write anything containing a password, code, or personal secret.
Return an empty list if nothing here is durable — that is a normal and correct answer.

Also return "procedure" (or null) if a repeatable sequence worked: a short goal_pattern, the
domain, and 3-6 generic steps.

{json.dumps({"memories": [{"type": "site", "text": "...", "domain": "example.com", "importance": 0.6}], "procedure": None})}
"""
