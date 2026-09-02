"""Compact working state — the only thing the agent carries across steps.

The design constraint that shapes this whole module: a Qwen3 8B prompt must stay bounded no
matter how long the task runs (V2 spec §11). So state is *not* an append-only history. It is
a fixed set of bounded slots:

- `facts` / `artifacts`  — task OUTPUT. Never silently dropped; overflow is spilled to disk
  and replaced by a one-line pointer, because losing these loses the answer.
- `completed` / `pending` — the plan, as data (V2 spec §19). Bounded, oldest-first eviction.
- `recent_actions`        — a short sliding window, not the full trace.
- `failures`             — only the last few, and only ones the model still needs to avoid.

`TaskState` is JSON-serializable and written after every step, which is also what makes
crash/resume (V2 spec §32) work without replaying a raw prompt history.
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Optional

from agent.token_budget import count_tokens

MAX_FACT_CHARS = 300
MAX_LINE_CHARS = 200

#: Prefix on a fact the evidence ledger could not tie to the page it was written against.
#: The fact is still kept — the model may be reasoning legitimately — but it is marked, so
#: the model can see which of its own notes will not stand behind a final answer, and so a
#: later restatement of the same fact does not read as a second, independent one.
UNVERIFIED = "(unverified)"


class TaskStatus(str, Enum):
    RUNNING = "running"
    WAITING_FOR_USER = "waiting_for_user"
    DONE = "done"
    FAILED = "failed"


@dataclass
class ActionRecord:
    step: int
    action: str
    target_name: Optional[str]
    detail: str
    url: str
    ok: bool
    note: str = ""
    signature: str = ""
    state_hash: str = ""
    #: Did the page actually change? Repeating an action that changed nothing is the
    #: single most common way a small model stalls, so this is what the loop checks.
    changed: bool = True

    def render(self) -> str:
        mark = "ok" if self.ok else "FAILED"
        target = f' "{self.target_name}"' if self.target_name else ""
        detail = f" {self.detail}" if self.detail else ""
        note = f" — {self.note}" if self.note else ""
        return f"{self.step}. {self.action}{target}{detail} -> {mark}{note}"[:MAX_LINE_CHARS]


@dataclass
class Metrics:
    llm_calls: int = 0
    llm_ms: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    prompt_chars_max: int = 0
    observe_ms: float = 0.0
    browser_ms: float = 0.0
    memory_ms: float = 0.0
    actions_executed: int = 0
    invalid_decisions: int = 0
    verification_failures: int = 0
    recoveries: int = 0
    loop_breaks: int = 0
    memory_hits: int = 0
    procedure_hits: int = 0
    human_interventions: int = 0
    #: Grounding (V2 hardening §2/§4/§9).
    evidence_records: int = 0
    evidence_rejected: int = 0
    resources_observed: int = 0
    computations: int = 0
    compute_errors: int = 0
    grounding_challenges: int = 0
    started_at: float = field(default_factory=time.time)
    total_s: float = 0.0


@dataclass
class TaskState:
    task_id: str
    goal: str
    status: str = TaskStatus.RUNNING.value
    step: int = 0
    facts: list[str] = field(default_factory=list)
    completed: list[str] = field(default_factory=list)
    pending: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    artifacts: dict[str, str] = field(default_factory=dict)
    recent_actions: list[ActionRecord] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    spilled_facts: int = 0
    current_url: str = ""
    current_title: str = ""
    current_tab_id: Optional[int] = None
    #: Every domain this task actually loaded a page from. Not decoration: it is the only
    #: cheap way to tell "reported from a source" apart from "recited from memory".
    domains: list[str] = field(default_factory=list)
    answer: str = ""
    #: Everything in the final answer that the evidence ledger could not account for.
    #: Surfaced to the user rather than silently shipped (see agent_v2.grounding).
    unsupported_claims: list[str] = field(default_factory=list)
    #: FULL_SUCCESS / PARTIAL_GROUNDED / SAFE_FAILURE / UNSUPPORTED_SUCCESS (§29).
    outcome: str = ""
    #: One line of deterministic source accounting, for the run report.
    source_coverage: str = ""
    pause_message: str = ""
    metrics: Metrics = field(default_factory=Metrics)

    # Bounds. Tuned so a full state block stays roughly under 700 tokens even when every
    # slot is saturated; `context_tokens()` is the assertion that keeps that honest.
    max_facts: int = 25
    max_completed: int = 8
    max_pending: int = 8
    max_recent_actions: int = 6
    max_failures: int = 3

    # ---- mutation -------------------------------------------------------------------

    def apply_updates(self, add_facts: list[str], completed: list[str], pending: list[str],
                      spill_path: Optional[Path] = None,
                      verify: Optional[Callable[[str], bool]] = None) -> None:
        """`verify` is the evidence ledger's opinion of each proposed fact: True when the
        page the fact was written against actually showed it. A fact that fails is still
        kept — the model needs its own notes — but it is marked, and no evidence record
        exists for it, so it can never support a final claim."""
        for fact in add_facts:
            self.add_fact(fact, spill_path, verified=True if verify is None else verify(fact))
        for item in completed:
            self.mark_completed(item)
        for item in pending:
            self.add_pending(item)

    def add_fact(self, fact: str, spill_path: Optional[Path] = None,
                 verified: bool = True) -> None:
        fact = _clean(fact, MAX_FACT_CHARS)
        if not fact:
            return
        if not verified and not fact.startswith(UNVERIFIED):
            fact = f"{UNVERIFIED} {fact}"
        existing = _index_of(fact, self.facts)
        if existing is not None:
            # The model went and read the page it had previously only asserted from memory.
            # Upgrading in place is the whole point of marking rather than dropping.
            if verified and self.facts[existing].startswith(UNVERIFIED):
                self.facts[existing] = fact
            return
        self.facts.append(fact)
        if len(self.facts) > self.max_facts:
            # Spill, never drop: facts are the task's actual output (V2 spec §11 —
            # "Do not discard required task outputs"). The oldest goes to disk and stays
            # retrievable in the final answer assembly, just not in the live prompt.
            overflow = self.facts.pop(0)
            self.spilled_facts += 1
            if spill_path is not None:
                _append_line(spill_path, overflow)

    def mark_completed(self, item: str) -> None:
        item = _clean(item, MAX_LINE_CHARS)
        if not item:
            return
        self.pending = [p for p in self.pending if not _same(p, item)]
        if _dupe(item, self.completed):
            return
        self.completed.append(item)
        if len(self.completed) > self.max_completed:
            self.completed.pop(0)

    def add_pending(self, item: str) -> None:
        item = _clean(item, MAX_LINE_CHARS)
        if not item or _dupe(item, self.pending) or _dupe(item, self.completed):
            return
        self.pending.append(item)
        if len(self.pending) > self.max_pending:
            self.pending.pop(0)

    def record_action(self, record: ActionRecord) -> None:
        self.recent_actions.append(record)
        if len(self.recent_actions) > self.max_recent_actions:
            self.recent_actions.pop(0)

    def record_failure(self, text: str) -> None:
        text = _clean(text, MAX_LINE_CHARS)
        if not text:
            return
        if self.failures and _same(self.failures[-1], text):
            return
        self.failures.append(text)
        if len(self.failures) > self.max_failures:
            self.failures.pop(0)

    def note_page(self, url: str, title: str, tab_id: Optional[int]) -> None:
        self.current_url = url
        self.current_title = title
        self.current_tab_id = tab_id

    # ---- rendering ------------------------------------------------------------------

    def render(self) -> str:
        lines = [f"STATUS: {self.status}  STEP: {self.step}"]
        if self.constraints:
            lines.append("CONSTRAINTS:")
            lines.extend(f"- {c}" for c in self.constraints)
        lines.append("FACTS COLLECTED SO FAR:")
        if self.facts:
            lines.extend(f"- {f}" for f in self.facts)
            if self.spilled_facts:
                lines.append(f"- (+{self.spilled_facts} earlier facts saved to the task file)")
        else:
            lines.append("- (none yet)")
        lines.append("DONE:")
        lines.extend(f"- {c}" for c in self.completed or ["(nothing yet)"])
        lines.append("STILL TO DO:")
        lines.extend(f"- {p}" for p in self.pending or ["(decide this yourself)"])
        if self.failures:
            lines.append("RECENT PROBLEMS (do not repeat these):")
            lines.extend(f"- {f}" for f in self.failures)
        return "\n".join(lines)

    def render_recent_actions(self) -> str:
        if not self.recent_actions:
            return "(no actions yet — this is the first step)"
        return "\n".join(r.render() for r in self.recent_actions)

    def context_tokens(self) -> int:
        return count_tokens(self.render()) + count_tokens(self.render_recent_actions())

    # ---- persistence ----------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TaskState":
        recent = [ActionRecord(**r) for r in data.pop("recent_actions", [])]
        metrics = Metrics(**data.pop("metrics", {}))
        known = {f for f in cls.__dataclass_fields__}
        state = cls(**{k: v for k, v in data.items() if k in known})
        state.recent_actions = recent
        state.metrics = metrics
        return state

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")
        tmp.replace(path)

    @classmethod
    def load(cls, path: Path) -> "TaskState":
        return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))


def _clean(text: str, limit: int) -> str:
    return " ".join(str(text or "").split())[:limit]


def _same(a: str, b: str) -> bool:
    return _key(a) == _key(b)


def _dupe(text: str, existing: list[str]) -> bool:
    return _key(text) in {_key(e) for e in existing}


def _index_of(text: str, existing: list[str]) -> Optional[int]:
    key = _key(text)
    for index, item in enumerate(existing):
        if _key(item) == key:
            return index
    return None


def _key(text: str) -> str:
    key = " ".join("".join(c.lower() if c.isalnum() else " " for c in text).split())
    return key.removeprefix("unverified ").strip()


def _append_line(path: Path, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(line + "\n")
