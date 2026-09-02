"""Durable, cross-task memory. Lives entirely outside the model.

Why this is a new store rather than `memory/task_memory.py`: that store is *task-scoped*
(every row carries a `task_id` and is only ever searched within that task) and it derives a
memory from essentially every observation — precisely the "clicked element 23 / scrolled
600px" junk V2 spec §13 rules out. What V2 needs is the opposite: a small number of durable
rows that survive the task that produced them and are retrievable by a *different, later*
task. The one thing worth carrying over is the technique — SQLite + FTS5 + bm25, no vector
DB (V2 spec §14) — so that is what is reused here.

Two tables:
  memories   — preferences, user facts, site experiences, failure lessons, procedures-as-prose
  procedures — a repeated strategy that worked, with success/failure counts (V2 spec §24)

Supersession (V2 spec §16) is deterministic: a new memory that is near-identical to an
existing one of the same type and domain marks the old row superseded rather than adding a
second, contradictory row.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional
from urllib.parse import urlsplit

from agent.token_budget import count_tokens

MEMORY_TYPES = (
    "preference",     # a durable user preference
    "user_fact",      # a durable fact about the user
    "site",           # how a website behaves (learned, not hardcoded)
    "strategy",       # an approach that worked
    "lesson",         # a failure and what to do instead
    "procedure",      # a general procedure worth repeating
)

#: Never persisted, whatever the model proposes. Credentials/OTPs are the one class of text
#: that must not survive a task (V2 spec §7/§15/§29).
_SECRET_PATTERNS = [
    # The credential itself ("password is x", "passcode: x") — not the mere noun, so
    # "the site asks for a password" stays storable, which is genuinely useful knowledge.
    re.compile(r"\bpass(word|phrase|code)\b\s*(?:[:=]|\bis\b|\bwas\b)[:=]?\s*[\"']?\S{3,}", re.I),
    re.compile(r"\b(otp|2fa|two[- ]factor|verification|auth|login|security)\s*code\b[^.\n]{0,12}\d", re.I),
    re.compile(r"\b\d{4,8}\b\s*(is|was)\s+the\s+(code|otp|pin)\b", re.I),
    re.compile(r"\b(api[_ ]?key|secret|bearer|token|pin)\b\s*(?:[:=]|\bis\b|\bwas\b)[:=]?\s*[\"']?\S{4,}", re.I),
    re.compile(r"\bcvv\b|\bcard number\b|\bssn\b", re.I),
]

#: Things that were true of one page at one moment. Long-term memory is for how the world
#: works, not for what it happened to say (V2 hardening §14). Each pattern below was written
#: against a row the extractor actually produced during the dev and holdout suites — see
#: `evals/audit_memory.py`, which is what the thresholds here are answerable to.
_TRANSIENT_PATTERNS = [
    # A price. Prices are the single most common thing the extractor tried to make durable,
    # and the one least likely to still be true.
    (re.compile(r"[$£€¥₹]\s?\d"), "a price"),
    (re.compile(r"\b\d+(?:[.,]\d+)?\s*(?:usd|eur|gbp|dollars?|euros?|pounds?)\b", re.I), "a price"),
    # The result of one search.
    (re.compile(r"\b\d+\s+(results?|items?|products?|options?|listings?|entries|rows|"
                r"matches|stories|articles|hits)\b", re.I), "a one-off count"),
    # "the latest X is 3.13.1" — true today, wrong next quarter, and stated with the same
    # confidence either way. The `v?` matters: "v26.8.1" is how half the web writes a version,
    # and without it the audit found this rule silently missing every Node release.
    (re.compile(r"\b(current|latest|newest|stable|released?|version)\b[^.]{0,40}?"
                r"\bv?\d+\.\d+", re.I), "a version number that will change"),
    (re.compile(r"\bv?\d+\.\d+(?:\.\d+)?\b[^.]{0,30}?\b(is|was)\s+(the\s+)?"
                r"(current|latest|newest|stable)\b", re.I), "a version number that will change"),
    # A specific date or time this task happened to see.
    (re.compile(r"\b(as of|on)\s+\d{1,2}\s+\w+\s+\d{4}\b", re.I), "a point-in-time reading"),
    (re.compile(r"\b\d{4}-\d{2}-\d{2}\b"), "a point-in-time reading"),
    # Where something sat in one listing on one day. The spec names candidate rankings
    # explicitly, and this is the shape they take: an ordinal pointing at page furniture.
    (re.compile(r"\b(first|second|third|fourth|fifth|top|last|next)\s+"
                r"(row|result|item|entry|option|candidate|listing|link|match|hit|product)s?\b",
                re.I), "where something sat in one listing"),
]

#: The run narrating itself. "I clicked Search and found seven results" is a description of
#: one episode; nothing in it will help a different task weeks from now. The `(?:\w+\s+){0,2}`
#: is there because the extractor overwhelmingly writes "I successfully retrieved…" rather
#: than "I retrieved…", and the adverb was enough to slip the whole class past the filter.
_EPISODIC_PATTERNS = [
    re.compile(r"\b(i|we)\s+(?:\w+\s+){0,2}(found|clicked|opened|navigated|searched|typed|"
               r"selected|scrolled|extracted|visited|checked|read|saw|used|located|"
               r"discovered|confirmed|identified|retrieved|completed|reported|quoted|"
               r"verified|compared|obtained)\b", re.I),
    # Capability and intent narration: "I can compare version numbers", "I need to open both
    # download pages". True of every task and therefore informative about none.
    re.compile(r"\b(i|we)\s+(can|could|will|should|must|need to|needed to|am able|are able|"
               r"was able|were able|have to|had to)\b", re.I),
    re.compile(r"\bthe (search|page|site|query|click|task)\s+(returned|showed|gave|listed|"
               r"produced|yielded)\b", re.I),
    re.compile(r"\b(in|for|during|after)\s+(this|the current|the last)\s+task\b", re.I),
    re.compile(r"\bthe (user'?s?\s+)?(goal|task|objective|request)\s+"
               r"(was|is|required|involved|asked|needed)\b", re.I),
]


class WritePolicy:
    """Why a proposed memory was or was not made durable."""

    ACCEPT = "accept"
    SECRET = "secret"
    TRANSIENT = "transient"
    EPISODIC = "episodic"
    LOW_INFORMATION = "low_information"
    GOAL_ECHO = "goal_echo"


def classify_write(text: str, *, type: str = "", goal: str = "") -> tuple[str, str]:
    """Deterministic long-term-memory admission. Returns `(verdict, reason)`.

    Called on every write, whatever the model asked for. The extraction prompt asks for
    durable knowledge and mostly complies; this is what happens when it does not. The rules
    are generic — none of them names a website — and each rejects a *shape* of statement
    rather than a topic, so a genuinely reusable fact about prices ("this shop shows prices
    only after you choose a country") still gets through while "the vacuum costs $159" does
    not.
    """
    text = " ".join(str(text or "").split())
    if not text:
        return WritePolicy.LOW_INFORMATION, "empty"
    if contains_secret(text):
        return WritePolicy.SECRET, "contains a credential, code or card detail"
    # Shape before size. "The latest release is 3.13.1" is short *and* transient, and the
    # reason a row was refused is what the audit reads, so the more specific verdict wins.
    for pattern, why in _TRANSIENT_PATTERNS:
        if pattern.search(text):
            return WritePolicy.TRANSIENT, f"records {why}"
    for pattern in _EPISODIC_PATTERNS:
        if pattern.search(text):
            return WritePolicy.EPISODIC, "describes what happened in one task"
    if len(text) < 20 or len(_terms(text)) < 3:
        return WritePolicy.LOW_INFORMATION, "too short to be useful later"
    # A near-restatement of the goal is the extractor summarizing the task rather than
    # learning from it. Only rejected when it also carries a figure, so a real lesson that
    # happens to share the goal's vocabulary survives.
    goal_terms = _terms(goal)
    if goal_terms:
        own = _terms(text)
        overlap = len(own & goal_terms) / max(len(own), 1)
        if overlap >= 0.7 and re.search(r"\d", text):
            return WritePolicy.GOAL_ECHO, "restates this task's goal and its numbers"
    return WritePolicy.ACCEPT, ""


@dataclass(frozen=True)
class Memory:
    id: int
    type: str
    text: str
    domain: str
    importance: float
    created_at: str
    use_count: int = 0

    def render(self) -> str:
        scope = f"[{self.type}" + (f"/{self.domain}]" if self.domain else "]")
        return f"{scope} {self.text}"


@dataclass(frozen=True)
class Procedure:
    id: int
    domain: str
    goal_pattern: str
    context: str
    steps: list[str]
    success_count: int
    failure_count: int

    @property
    def confidence(self) -> float:
        total = self.success_count + self.failure_count
        if total == 0:
            return 0.0
        return self.success_count / total

    def render(self) -> str:
        steps = " -> ".join(self.steps[:6])
        return f"[procedure/{self.domain or 'any'}] when {self.goal_pattern}: {steps}"


@dataclass
class RetrievalResult:
    memories: list[Memory] = field(default_factory=list)
    procedure: Optional[Procedure] = None
    considered: int = 0
    elapsed_ms: float = 0.0

    def render(self) -> str:
        lines: list[str] = []
        if self.procedure is not None:
            lines.append(self.procedure.render())
        lines.extend(m.render() for m in self.memories)
        return "\n".join(f"- {line}" for line in lines)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY,
    type TEXT NOT NULL,
    text TEXT NOT NULL,
    domain TEXT NOT NULL DEFAULT '',
    importance REAL NOT NULL DEFAULT 0.5,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_used_at TEXT,
    use_count INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1,
    superseded_by INTEGER,
    source_task TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_memories_active ON memories(active, type);
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(text, type, domain);
CREATE TABLE IF NOT EXISTS procedures (
    id INTEGER PRIMARY KEY,
    domain TEXT NOT NULL DEFAULT '',
    goal_pattern TEXT NOT NULL,
    context TEXT NOT NULL DEFAULT '',
    steps TEXT NOT NULL,
    success_count INTEGER NOT NULL DEFAULT 0,
    failure_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1
);
"""


class MemoryStore:
    """Small enough to read in one sitting; that is the point (V2 spec §36)."""

    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.row_factory = sqlite3.Row
        #: Why writes were refused, by verdict. Read by `evals/audit_memory.py`; never
        #: written to disk, since a refused memory is exactly the thing not to persist.
        self.rejections: dict[str, int] = {}
        self.last_rejection: str = ""
        with self.conn:
            self.conn.executescript(_SCHEMA)

    def close(self) -> None:
        self.conn.close()

    # ---- writing --------------------------------------------------------------------

    def save(self, type: str, text: str, *, domain: str = "", importance: float = 0.5,
             source_task: str = "", goal: str = "") -> Optional[int]:
        """Store one durable memory, or refuse.

        Returns the new row id, or None when `classify_write` declined it. The refusal is the
        boundary the credential and transient-data guarantees rest on: it is enforced here,
        on the way in, not audited afterwards on the database (V2 hardening §14/§15).
        """
        text = " ".join(str(text or "").split())[:400]
        verdict, reason = classify_write(text, type=type, goal=goal)
        self.last_rejection = "" if verdict == WritePolicy.ACCEPT else f"{verdict}: {reason}"
        if verdict != WritePolicy.ACCEPT:
            self.rejections[verdict] = self.rejections.get(verdict, 0) + 1
            return None
        if type not in MEMORY_TYPES:
            type = "site" if domain else "strategy"
        domain = normalize_domain(domain)
        now = _now()

        superseded = self._supersede_similar(type, domain, text)
        with self.conn:
            cur = self.conn.execute(
                """INSERT INTO memories (type, text, domain, importance, created_at, updated_at, source_task)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (type, text, domain, max(0.0, min(1.0, importance)), now, now, source_task),
            )
            memory_id = cur.lastrowid
            self.conn.execute(
                "INSERT INTO memories_fts(rowid, text, type, domain) VALUES (?, ?, ?, ?)",
                (memory_id, text, type, domain),
            )
            if superseded:
                self.conn.executemany(
                    "UPDATE memories SET active = 0, superseded_by = ?, updated_at = ? WHERE id = ?",
                    [(memory_id, now, old_id) for old_id in superseded],
                )
                self.conn.executemany(
                    "DELETE FROM memories_fts WHERE rowid = ?", [(old_id,) for old_id in superseded]
                )
        return memory_id

    def _supersede_similar(self, type: str, domain: str, text: str) -> list[int]:
        """Same type + same domain + high token overlap => the older row is stale, not a
        second opinion. Preferences additionally supersede on *subject* overlap alone, since
        "prefers X" followed by "prefers Y" is a change of mind, not two facts."""
        rows = self.conn.execute(
            "SELECT id, text FROM memories WHERE active = 1 AND type = ? AND domain = ?",
            (type, domain),
        ).fetchall()
        new_terms = _terms(text)
        if not new_terms:
            return []
        threshold = 0.5 if type in ("preference", "user_fact") else 0.7
        stale: list[int] = []
        for row in rows:
            old_terms = _terms(row["text"])
            if not old_terms:
                continue
            overlap = len(new_terms & old_terms) / max(len(new_terms | old_terms), 1)
            if overlap >= threshold:
                stale.append(row["id"])
        return stale

    def supersede(self, memory_id: int, replacement_id: Optional[int] = None) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE memories SET active = 0, superseded_by = ?, updated_at = ? WHERE id = ?",
                (replacement_id, _now(), memory_id),
            )
            self.conn.execute("DELETE FROM memories_fts WHERE rowid = ?", (memory_id,))

    # ---- retrieval ------------------------------------------------------------------

    def retrieve(self, goal: str, *, domain: str = "", subgoal: str = "",
                 top_k: int = 6, token_budget: int = 400) -> RetrievalResult:
        """Selective retrieval (V2 spec §14): a handful of rows, never the whole DB.

        Scoring blends bm25 relevance with importance, domain match, and recency. Domain is
        an *additive* signal, never a filter — a generally useful lesson learned on one site
        must still be able to surface on another."""
        started = time.monotonic()
        domain = normalize_domain(domain)
        query_terms = _bounded(_terms(f"{goal} {subgoal}"), 14)

        candidates: dict[int, tuple[sqlite3.Row, float]] = {}
        if query_terms:
            # Prefix matching, so "flight" finds "flights" — FTS5 does not stem, and a
            # singular/plural miss is a retrieval failure that looks exactly like a memory
            # that was never saved. The match is scoped to the `text` column: an unscoped
            # match would let the query's own words hit the indexed `domain` column and pull
            # back rows with nothing relevant in them.
            match = "text : (" + " OR ".join(f'"{t}"*' for t in query_terms) + ")"
            for row in self.conn.execute(
                """SELECT m.*, bm25(memories_fts) AS rank
                   FROM memories_fts JOIN memories m ON m.id = memories_fts.rowid
                   WHERE memories_fts MATCH ? AND m.active = 1
                   ORDER BY rank ASC LIMIT ?""",
                (match, max(top_k * 6, 30)),
            ).fetchall():
                candidates[row["id"]] = (row, -float(row["rank"]))  # bm25: lower is better
        if domain:
            # Anything learned about the site we are actually on is a candidate even when its
            # wording shares nothing with the goal.
            for row in self.conn.execute(
                "SELECT * FROM memories WHERE active = 1 AND domain = ? ORDER BY id DESC LIMIT 20",
                (domain,),
            ).fetchall():
                candidates.setdefault(row["id"], (row, 0.0))

        scored: list[tuple[float, sqlite3.Row]] = []
        for row, relevance in candidates.values():
            score = relevance
            score += 2.0 * float(row["importance"])
            if domain and row["domain"] == domain:
                score += 2.5
            elif row["domain"] and domain and row["domain"] != domain:
                score -= 1.5                                  # another site's specifics
            score += min(float(row["use_count"]), 5) * 0.1
            score += _recency_bonus(row["created_at"])
            scored.append((score, row))
        scored.sort(key=lambda item: -item[0])
        rows = [row for _score, row in scored]

        selected: list[Memory] = []
        used = 0
        for _score, row in scored:
            memory = Memory(
                id=row["id"], type=row["type"], text=row["text"], domain=row["domain"],
                importance=row["importance"], created_at=row["created_at"], use_count=row["use_count"],
            )
            cost = count_tokens(memory.render())
            if selected and used + cost > token_budget:
                break
            if cost > token_budget:
                continue
            selected.append(memory)
            used += cost
            if len(selected) >= top_k:
                break

        if selected:
            now = _now()
            with self.conn:
                self.conn.executemany(
                    "UPDATE memories SET use_count = use_count + 1, last_used_at = ? WHERE id = ?",
                    [(now, m.id) for m in selected],
                )
        return RetrievalResult(
            memories=selected,
            procedure=self.best_procedure(goal, domain),
            considered=len(rows),
            elapsed_ms=(time.monotonic() - started) * 1000,
        )

    def all_active(self) -> list[Memory]:
        rows = self.conn.execute(
            "SELECT * FROM memories WHERE active = 1 ORDER BY id"
        ).fetchall()
        return [
            Memory(id=r["id"], type=r["type"], text=r["text"], domain=r["domain"],
                   importance=r["importance"], created_at=r["created_at"], use_count=r["use_count"])
            for r in rows
        ]

    # ---- procedures (V2 spec §24) ----------------------------------------------------

    def save_procedure(self, goal_pattern: str, steps: list[str], *, domain: str = "",
                       context: str = "") -> Optional[int]:
        goal_pattern = " ".join(str(goal_pattern or "").split())[:200]
        steps = [" ".join(s.split())[:160] for s in steps if str(s or "").strip()][:8]
        if not goal_pattern or not steps or contains_secret(" ".join(steps)):
            return None
        domain = normalize_domain(domain)
        existing = self._matching_procedure(goal_pattern, domain)
        now = _now()
        if existing is not None:
            with self.conn:
                self.conn.execute(
                    "UPDATE procedures SET steps = ?, success_count = success_count + 1, updated_at = ? WHERE id = ?",
                    (json.dumps(steps), now, existing.id),
                )
            return existing.id
        with self.conn:
            cur = self.conn.execute(
                """INSERT INTO procedures (domain, goal_pattern, context, steps, success_count,
                                           failure_count, created_at, updated_at)
                   VALUES (?, ?, ?, ?, 1, 0, ?, ?)""",
                (domain, goal_pattern, context, json.dumps(steps), now, now),
            )
        return cur.lastrowid

    def best_procedure(self, goal: str, domain: str = "", min_confidence: float = 0.6) -> Optional[Procedure]:
        """Highest-overlap procedure for this goal, only if it has actually worked before.
        Returned as a *hint* for the prompt — the loop never replays steps blindly (see
        agent.py); it verifies every action it takes either way."""
        domain = normalize_domain(domain)
        rows = self.conn.execute("SELECT * FROM procedures WHERE active = 1").fetchall()
        goal_terms = _terms(goal)
        best: Optional[tuple[float, Procedure]] = None
        for row in rows:
            proc = _procedure_from_row(row)
            if proc.confidence < min_confidence:
                continue
            overlap = len(goal_terms & _terms(proc.goal_pattern))
            if overlap == 0:
                continue
            score = overlap + (2.0 if domain and proc.domain == domain else 0.0)
            if best is None or score > best[0]:
                best = (score, proc)
        return best[1] if best else None

    def record_procedure_outcome(self, procedure_id: int, success: bool) -> None:
        column = "success_count" if success else "failure_count"
        with self.conn:
            self.conn.execute(
                f"UPDATE procedures SET {column} = {column} + 1, updated_at = ? WHERE id = ?",
                (_now(), procedure_id),
            )
            # A procedure that keeps failing stops being offered rather than being deleted —
            # the record of it having failed is itself useful.
            self.conn.execute(
                "UPDATE procedures SET active = 0 WHERE id = ? AND failure_count >= 3 "
                "AND failure_count > success_count",
                (procedure_id,),
            )

    def _matching_procedure(self, goal_pattern: str, domain: str) -> Optional[Procedure]:
        rows = self.conn.execute(
            "SELECT * FROM procedures WHERE active = 1 AND domain = ?", (domain,)
        ).fetchall()
        wanted = _terms(goal_pattern)
        for row in rows:
            existing = _terms(row["goal_pattern"])
            if not existing or not wanted:
                continue
            if len(wanted & existing) / max(len(wanted | existing), 1) >= 0.6:
                return _procedure_from_row(row)
        return None


def contains_secret(text: str) -> bool:
    return any(pattern.search(text) for pattern in _SECRET_PATTERNS)


def normalize_domain(value: str) -> str:
    """A single bare host, or "".

    Asked which domain a memory belongs to, the model quite reasonably answers
    "python.org,nodejs.org" for a task that used both — and that string can never equal a
    real domain, so the memory becomes unreachable by the domain signal that would surface
    it. The first host is kept; the rest of the text stays in the memory itself.
    """
    value = (value or "").strip().lower()
    if not value:
        return ""
    if "://" in value:
        value = urlsplit(value).netloc
    for separator in (",", ";", " and ", "/", " "):
        if separator in value:
            value = value.split(separator)[0].strip()
    return value[4:] if value.startswith("www.") else value


def domain_of(url: str) -> str:
    return normalize_domain(urlsplit(url or "").netloc)


def _procedure_from_row(row: sqlite3.Row) -> Procedure:
    return Procedure(
        id=row["id"], domain=row["domain"], goal_pattern=row["goal_pattern"],
        context=row["context"], steps=json.loads(row["steps"]),
        success_count=row["success_count"], failure_count=row["failure_count"],
    )


_STOPWORDS = {
    "the", "and", "for", "with", "from", "that", "this", "into", "onto", "was", "are",
    "you", "your", "its", "not", "but", "any", "all", "can", "has", "have", "get", "got",
    "use", "using", "then", "than", "there", "their", "what", "when", "where", "which",
    "page", "site", "website", "www", "com", "http", "https",
}


def _terms(text: str) -> set[str]:
    out: set[str] = set()
    for raw in re.split(r"[^A-Za-z0-9_]+", text or ""):
        word = raw.lower()
        if len(word) >= 3 and word not in _STOPWORDS:
            out.add(word)
    return out


def _bounded(terms: Iterable[str], limit: int) -> list[str]:
    return sorted(terms)[:limit]


def _recency_bonus(created_at: str) -> float:
    try:
        created = datetime.fromisoformat(created_at)
    except ValueError:
        return 0.0
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    age_days = (datetime.now(timezone.utc) - created).total_seconds() / 86400
    if age_days < 1:
        return 0.6
    if age_days < 7:
        return 0.3
    if age_days < 30:
        return 0.1
    return 0.0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
