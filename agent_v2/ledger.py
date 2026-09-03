"""What actually happened: the resources this task really observed, and the evidence that
really exists.

This module is the deterministic half of the V2 contract. The model decides *what* to do and
*what is worth recording*; nothing it says is taken as proof that it did anything. Two
registries, both written only by BrowserAgent:

- **Visited resources.** A resource exists here only because `BrowserSession.observe()`
  returned an observation of it. Discovering a link does not create one. Mentioning a URL
  does not create one. A redirect adds the requested URL as an *alias* of the resource that
  was actually loaded, so a later citation of either URL resolves to the same page.

- **Evidence records.** Every record is bound to one real observation and carries an id that
  BrowserAgent generated. The model never authors an evidence id, and a text it proposes only
  becomes a record if that text is actually supported by the observation it was written
  against (`supports()` below). A derived record — the output of `agent_v2.compute` — carries
  the ids of the evidence it was computed from, so arithmetic keeps its lineage.

The ledger is per task. Evidence ids embed a short key derived from the task id, which is
what makes "this id belongs to a different task" a structural rejection rather than a
convention. Records stay compact on purpose (V2 hardening §2): a bounded snippet, never a
page dump.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from agent.token_budget import count_tokens

#: Longest snippet kept per evidence record. Enough for a price line, a version string or a
#: sentence; far short of a page.
EVIDENCE_MAX_CHARS = 240
#: Normalized page text kept per resource, for quoted-span checks. Bounded per resource and
#: in total so a long crawl cannot grow the ledger without limit.
RESOURCE_TEXT_CHARS = 40_000
LEDGER_TEXT_CHARS = 2_000_000
#: How much of each resource's text is written to disk, so a crash+resume can still check a
#: quotation against a page it read before the crash.
PERSISTED_TEXT_CHARS = 8_000

#: Query parameters that identify a click, not a document. Dropped when canonicalizing so the
#: same page reached from a search result and from a link is one resource.
_TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "utm_id",
    "fbclid", "gclid", "msclkid", "mc_cid", "mc_eid", "igshid", "ref_src", "ref_url",
    "_ga", "yclid", "dclid", "wt_mc", "s_kwcid",
}


# --------------------------------------------------------------------------------------
# text primitives (shared with agent_v2.grounding)
# --------------------------------------------------------------------------------------

def normalize(text: str) -> str:
    """Lowercase, alphanumeric-only, single-spaced. Punctuation, case and whitespace are not
    part of whether something was on the page (V2 hardening §5)."""
    return " ".join("".join(c.lower() if c.isalnum() else " " for c in str(text or "")).split())


#: A numeric token together with whatever gives it meaning: a currency sign in front, a
#: percent sign or a unit word after.
_NUMBER_RE = re.compile(r"([$£€¥₹]\s?)?(\d[\d,]*(?:\.\d+)*)\s*(%|[A-Za-z]{2,7}\b)?")

#: Unit words that turn a small bare number into a specification. "36 months" and "1200
#: watts" are claims about a product; "3 pages" and "10 results" are claims about the run,
#: and flagging those would be crying wolf. Deliberately only unambiguous multi-letter units —
#: "3 in the morning" must not read as three inches.
_UNITS = {
    "months", "month", "years", "year", "days", "day", "hours", "hour", "minutes", "minute",
    "seconds", "second", "weeks", "week", "percent", "watts", "watt", "kw", "kwh", "volts",
    "amps", "hz", "khz", "mhz", "ghz", "kb", "mb", "gb", "tb", "kg", "lbs", "oz", "mm", "cm",
    "km", "inch", "inches", "feet", "mph", "kph", "rpm", "dpi", "ppi", "psi", "litres",
    "liters", "ml", "megapixels", "cores", "threads", "pixels", "stars",
}

_STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "at", "is", "are", "was", "were",
    "be", "been", "it", "its", "this", "that", "these", "those", "for", "with", "from", "by",
    "as", "has", "have", "had", "but", "not", "you", "your", "i", "we", "they", "he", "she",
    "there", "their", "which", "what", "when", "where", "how", "than", "then", "so", "if",
}


def significant_figures(text: str) -> list[str]:
    """Numeric tokens specific enough to be worth checking.

    A bare small integer ("3 pages", "top 5") is far too common to be evidence of anything and
    flagging it would manufacture false alarms on ordinary prose. A figure counts when it is

    - three digits or longer (159, 1889, 1200), or
    - decimal / thousands-grouped (3.14, 42.50, 8,848, 26.8.1), or
    - carrying a currency sign ($70) or a percent sign (12%).

    This is deliberately punctuation-independent in the other direction: `"$99"`, `$99` and
    `price = 99.00` all reduce to the same key, because quotation marks are not a grounding
    boundary (V2 hardening §5).
    """
    out: list[str] = []
    for match in _NUMBER_RE.finditer(str(text or "")):
        currency, number, suffix = match.group(1), match.group(2), match.group(3)
        digits = number.replace(",", "").replace(".", "")
        if not digits:
            continue
        unit = bool(suffix) and (suffix == "%" or suffix.lower() in _UNITS)
        if len(digits) >= 3 or "." in number or "," in number or currency or unit:
            out.append(number)
    return out


def figure_keys(token: str) -> set[str]:
    """Every spelling of one figure that means the same number.

    "89.00" and "89" are the same price; "3.10" and "3.1" are the same version written two
    ways. Both forms are emitted and a figure counts as supported when *any* of its keys
    matches any key the page produced — a miss here reads as a fabrication, so the comparison
    errs towards agreeing."""
    raw = str(token or "").replace(",", "").strip()
    if not raw:
        return set()
    keys = {raw}
    if raw.count(".") == 1:
        head, tail = raw.split(".")
        trimmed = tail.rstrip("0")
        keys.add(f"{head}.{trimmed}" if trimmed else head)
    elif raw.count(".") > 1:
        # A dotted version also stands for its major component: a page showing "v26.8.1" has
        # shown 26, and comparing major versions is one of the things `compute` exists for.
        # Restricted to two-or-more dots so that a decimal price stays a single figure —
        # "$42.50" must not quietly make a claim of "$42" accountable.
        keys.add(raw.split(".")[0])
    return keys


def all_figure_keys(text: str) -> set[str]:
    keys: set[str] = set()
    for token in significant_figures(text):
        keys |= figure_keys(token)
    return keys


def numeric_keys(text: str) -> set[str]:
    """*Every* number in the text, significant or not.

    The two directions are deliberately asymmetric. What a page is recorded as having shown
    is maximal — if "70" was printed anywhere, the task can account for 70. What an answer is
    *checked* for is minimal — only figures specific enough that inventing one would be a
    real claim. Erring maximally on what was seen and minimally on what is challenged is what
    keeps the mechanism from crying wolf over ordinary prose.
    """
    keys: set[str] = set()
    for match in _NUMBER_RE.finditer(str(text or "")):
        keys |= figure_keys(match.group(2))
    return keys


#: Quotation marks a model actually uses, straight and curly, single and double.
#:
#: The lookarounds are what keep an apostrophe from opening one. Observed on a real run: an
#: answer reading "1. It's Only the Himalayas - £45.17 … 2. Full Moon over Noah's Ark" was
#: reported as containing an unverified *quotation* running from the apostrophe in "It's" to
#: the one in "Noah's" — a fabricated defect in a perfectly good answer, which is the one kind
#: of false alarm this mechanism cannot afford. A real quotation opens at a word boundary.
_QUOTE_RE = re.compile(r"(?<!\w)[\"“‘']([^\"“”‘’']{8,200})[\"”’'](?!\w)")


def quoted_spans(text: str) -> list[str]:
    return [m.group(1).strip() for m in _QUOTE_RE.finditer(str(text or ""))]


def content_terms(text: str) -> set[str]:
    return {w for w in normalize(text).split() if len(w) >= 2 and w not in _STOPWORDS}


#: A capitalised word, an acronym, or anything carrying digits — the tokens a fabrication has
#: to invent. Ordinary prose can be paraphrased freely without touching any of them.
_DISTINCTIVE_RE = re.compile(r"\b(?:[A-Z][A-Za-z0-9]{2,}|[A-Z]{2,}|[A-Za-z]*\d[A-Za-z0-9]*)\b")

#: A capital letter at the start of a sentence says nothing about whether the word is a name —
#: English puts one there regardless. Counting it cost a real holdout task: the model's own
#: note "Visited repository overview page" was rejected because the *verb* "Visited" appeared
#: nowhere on the page, and the challenges that followed used up its step budget.
_SENTENCE_START_RE = re.compile(r"(?:^|(?<=[.!?;:]\s)|(?<=[.!?;:]\s\s))\s*([A-Z][A-Za-z0-9]*)")


def distinctive_terms(text: str) -> set[str]:
    """The names and coined tokens in a sentence.

    Whether a *paraphrase* of a page is faithful is not something containment can decide, and
    pretending otherwise would flag honest wording as invention (V2 hardening §7). What
    containment can decide is whether the sentence introduces a proper noun, product name or
    identifier that the page never contained — which is what an invented product looks like.
    Common words, and words capitalised only because a sentence began, are ignored.
    """
    text = str(text or "")
    sentence_starts = {m.group(1).lower() for m in _SENTENCE_START_RE.finditer(text)}
    out: set[str] = set()
    for token in _DISTINCTIVE_RE.findall(text):
        word = token.lower()
        if len(word) < 3 or word in _STOPWORDS:
            continue
        # …unless it is an acronym or carries a digit, neither of which happens by accident
        # at the start of a sentence.
        if word in sentence_starts and token[1:].islower() and not any(c.isdigit() for c in token):
            continue
        out.add(word)
    return out


def canonical_url(url: str) -> str:
    """A conservative identity for a web resource.

    Scheme and host lowercased, `www.` and the default port dropped, fragment dropped,
    tracking parameters dropped, remaining query sorted, one trailing slash removed. The path
    keeps its case and is never truncated: two pages on one host are two resources, and
    nothing here merges them just because the domain matches (V2 hardening §3).
    """
    raw = str(url or "").strip()
    if not raw:
        return ""
    parts = urlsplit(raw)
    if not parts.scheme:
        parts = urlsplit("https://" + raw)
    host = parts.netloc.lower()
    if "@" in host:
        host = host.rsplit("@", 1)[1]
    for scheme, port in (("http", ":80"), ("https", ":443")):
        if parts.scheme.lower() == scheme and host.endswith(port):
            host = host[: -len(port)]
    host = host.removeprefix("www.")
    query = urlencode(sorted(
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k.lower() not in _TRACKING_PARAMS
    ))
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), host, path, query, ""))


def host_of(url: str) -> str:
    return urlsplit(canonical_url(url)).netloc


# --------------------------------------------------------------------------------------
# records
# --------------------------------------------------------------------------------------

@dataclass
class VisitedResource:
    """One web page this task actually loaded and observed."""

    resource_id: str
    canonical_url: str
    url: str
    title: str
    first_seen_step: int
    last_seen_step: int
    observation_ids: list[str] = field(default_factory=list)
    #: Other URLs that resolved here — redirect sources, tracking variants.
    aliases: set[str] = field(default_factory=set)
    figures: set[str] = field(default_factory=set)
    text: str = ""
    #: Page-state hashes already folded into `text`/`figures`. Re-observing an unchanged page
    #: every step would otherwise spend the text budget on the same page over and over.
    seen_hashes: set[str] = field(default_factory=set)

    @property
    def host(self) -> str:
        return urlsplit(self.canonical_url).netloc

    def to_dict(self) -> dict:
        return {
            "resource_id": self.resource_id, "canonical_url": self.canonical_url,
            "url": self.url, "title": self.title, "first_seen_step": self.first_seen_step,
            "last_seen_step": self.last_seen_step, "observation_ids": list(self.observation_ids),
            "aliases": sorted(self.aliases), "figures": sorted(self.figures),
            "text": self.text[:PERSISTED_TEXT_CHARS],
        }


@dataclass
class ObservationRef:
    observation_id: str
    resource_id: str
    step: int
    url: str
    title: str
    text: str
    figures: set[str]


@dataclass
class EvidenceRecord:
    """One checkable thing, bound to where it came from.

    `kind` is "observed" (read off a page) or "derived" (computed by `agent_v2.compute` from
    other records). A derived record's `derived_from` is the lineage that lets a final claim
    about a difference or a ranking be traced back to the two prices it came from.
    """

    evidence_id: str
    task_id: str
    kind: str
    text: str
    step: int
    observation_id: str = ""
    resource_id: str = ""
    source_url: str = ""
    source_title: str = ""
    derived_from: list[str] = field(default_factory=list)
    operation: str = ""
    valid: bool = True
    invalid_reason: str = ""
    #: A derived record whose operands were not themselves accountable. It exists and is
    #: shown to the model, but it does not confer support on anything.
    grounded: bool = True
    figures: set[str] = field(default_factory=set)

    def render(self) -> str:
        where = self.source_title or self.source_url or self.operation or "computed"
        return f"{self.evidence_id} ({where}): {self.text}"

    def to_dict(self) -> dict:
        return {
            "evidence_id": self.evidence_id, "task_id": self.task_id, "kind": self.kind,
            "text": self.text, "step": self.step, "observation_id": self.observation_id,
            "resource_id": self.resource_id, "source_url": self.source_url,
            "source_title": self.source_title, "derived_from": list(self.derived_from),
            "operation": self.operation, "valid": self.valid, "grounded": self.grounded,
            "invalid_reason": self.invalid_reason,
        }


@dataclass
class Citation:
    """The outcome of resolving one evidence id the model wrote down."""

    raw: str
    record: Optional[EvidenceRecord] = None
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.record is not None


# --------------------------------------------------------------------------------------
# the ledger
# --------------------------------------------------------------------------------------

_ID_RE = re.compile(r"^\s*(?:ev[_\-]?)?(?:(?P<key>[0-9a-f]{4,8})[_\-])?(?P<n>\d{1,6})\s*$", re.I)


class EvidenceLedger:
    """Per-task. Created by the loop, written only by the loop, never by the model."""

    def __init__(self, task_id: str):
        self.task_id = task_id
        self.key = hashlib.sha1(task_id.encode("utf-8")).hexdigest()[:6]
        self.resources: dict[str, VisitedResource] = {}
        self.observations: dict[str, ObservationRef] = {}
        self.records: dict[str, EvidenceRecord] = {}
        self._by_canonical: dict[str, str] = {}
        self._next_resource = 1
        self._next_observation = 1
        self._next_evidence = 1
        self._text_chars = 0
        #: Every word this task has actually seen, across all pages. Maintained incrementally
        #: because it is consulted on every proposed fact.
        self._known_terms: set[str] = set()
        #: The user's own wording. A name the user supplied is not an invention.
        self.goal = ""
        #: Sources the task set out to use but has not observed. Populated by the loop from
        #: the goal; used for source-coverage reporting, never as permission to claim one.
        self.requested_sources: list[str] = []

    # ---- observing ------------------------------------------------------------------

    def note_observation(self, obs, step: int, requested_url: str = "") -> ObservationRef:
        """Register that BrowserAgent actually observed a page. This is the *only* way a
        resource comes into existence.

        `requested_url` is what the agent asked for. When it differs from where it landed —
        a redirect, a canonical rewrite, a tracking-stripped link — it is filed as an alias
        of the resource that was really loaded, so citing either URL later resolves here.
        """
        canonical = canonical_url(obs.url)
        text = normalize(
            " ".join(getattr(obs, "visible_text", []) or []) + " " + (obs.title or "") + " "
            + " ".join(getattr(el, "name", "") or "" for el in getattr(obs, "elements", []) or [])
        )
        figures = numeric_keys(
            " ".join(getattr(obs, "visible_text", []) or []) + " " + (obs.title or "") + " "
            + " ".join(getattr(el, "name", "") or "" for el in getattr(obs, "elements", []) or [])
        )

        resource_id = self._by_canonical.get(canonical)
        if resource_id is None:
            resource_id = f"src_{self._next_resource}"
            self._next_resource += 1
            self.resources[resource_id] = VisitedResource(
                resource_id=resource_id, canonical_url=canonical, url=obs.url,
                title=obs.title or "", first_seen_step=step, last_seen_step=step,
            )
            self._by_canonical[canonical] = resource_id
        resource = self.resources[resource_id]
        resource.last_seen_step = step
        if obs.title and not resource.title:
            resource.title = obs.title
        state_hash = str(getattr(obs, "state_hash", "") or "")[:16] or f"step{step}"
        if state_hash not in resource.seen_hashes:
            resource.seen_hashes.add(state_hash)
            resource.figures |= figures
            if len(resource.text) < RESOURCE_TEXT_CHARS and self._text_chars < LEDGER_TEXT_CHARS:
                addition = text[: RESOURCE_TEXT_CHARS - len(resource.text)]
                resource.text = (resource.text + " " + addition).strip() if resource.text else addition
                self._text_chars += len(addition)
            self._known_terms |= set(text.split())

        requested_canonical = canonical_url(requested_url) if requested_url else ""
        if requested_canonical and requested_canonical != canonical:
            resource.aliases.add(requested_canonical)
            self._by_canonical.setdefault(requested_canonical, resource_id)

        observation_id = f"obs_{self._next_observation}"
        self._next_observation += 1
        ref = ObservationRef(observation_id=observation_id, resource_id=resource_id, step=step,
                             url=obs.url, title=obs.title or "", text=text, figures=figures)
        self.observations[observation_id] = ref
        resource.observation_ids.append(observation_id)
        del resource.observation_ids[:-40]
        return ref

    def observed(self, url: str) -> bool:
        """Did this task actually load this URL (or something it redirected to)?"""
        return canonical_url(url) in self._by_canonical

    def reached(self, source: str) -> bool:
        """Did this task reach a source the user named?

        A goal names a *site* ("check nodejs.org") far more often than an exact document, so
        a bare host counts as reached once any page on it has been observed. A source given
        with a path is held to that path."""
        if self.observed(source):
            return True
        parts = urlsplit(canonical_url(source))
        if parts.path not in ("", "/") or not parts.netloc:
            return False
        return parts.netloc in self.hosts()

    def hosts(self) -> set[str]:
        hosts = {r.host for r in self.resources.values() if r.host}
        for resource in self.resources.values():
            hosts |= {urlsplit(a).netloc for a in resource.aliases if urlsplit(a).netloc}
        return hosts

    def known_figures(self) -> set[str]:
        """Every figure this task can account for: what pages showed, plus what BrowserAgent
        itself computed from them. A difference of $70 was on no page, but it is not a
        fabrication — it has a derivation (V2 hardening §12)."""
        seen: set[str] = set()
        for resource in self.resources.values():
            seen |= resource.figures
        for record in self.records.values():
            if record.valid and record.grounded and record.kind == "derived":
                seen |= record.figures
        return seen

    def supports_figures(self, keys: Iterable[str]) -> bool:
        """Can this task account for every one of these figures?"""
        wanted = set(keys)
        return not wanted or wanted <= self.known_figures()

    def numeric_evidence(self, limit: int = 4) -> list[EvidenceRecord]:
        """Evidence records carrying a figure — the operands a computation could be built
        from, oldest first.

        Used to tell two shortfalls apart when an answer states a figure no page shows. If
        the task holds no figures at all, the missing number can only be got by reading a
        page. If it already holds several, the number may instead be something that follows
        *from* them, and saying "go and open a page" is then advice for a page that does not
        exist. Which of the two it is stays the model's call; this only decides whether
        computing is worth naming as an option.
        """
        return [record for record in self.records.values()
                if record.valid and record.grounded and record.figures][:limit]

    def supports_span(self, span: str) -> bool:
        needle = normalize(span)
        if not needle:
            return False
        return any(needle in resource.text for resource in self.resources.values())

    # ---- recording ------------------------------------------------------------------

    def supports(self, text: str, ref: ObservationRef) -> tuple[bool, str]:
        """Is this proposed text actually supported by that observation?

        Four containment checks, and containment is all they claim to be. Entailment cannot
        be proven deterministically, so this does not try (V2 hardening §7); what it proves is
        that the checkable parts of the sentence — its numbers, its quotations, the sites it
        names and the names it coins — came from the page rather than from the model.
        Ordinary prose is free to be reworded, because rewording is not the failure mode.
        """
        missing = [token for token in significant_figures(text)
                   if not (figure_keys(token) & ref.figures)]
        if missing:
            return False, f"figure(s) {', '.join(sorted(set(missing))[:4])} are not on this page"
        for span in quoted_spans(text):
            if len(normalize(span).split()) >= 3 and normalize(span) not in ref.text:
                return False, f'the quoted "{span[:48]}" is not on this page'
        from agent_v2.grounding import hosts_mentioned  # local: grounding imports this module
        visited = self.hosts()
        for host in hosts_mentioned(text):
            if host not in visited and not any(h.endswith("." + host) for h in visited):
                return False, f"{host} is not a site this task has opened"
        names = distinctive_terms(text)
        if names:
            # Names are checked against everything the task has seen, not against this page
            # alone: a product name read off a listing and carried to the detail page is
            # ordinary navigation, not invention. The *figures* above are what stay pinned to
            # the page in hand, and they are what a fabrication has to get past.
            known = self._known_terms | content_terms(self.goal)
            if len(names & known) / len(names) < 0.5:
                return False, "names something that appears nowhere in this task"
        return True, ""

    def record_observed(self, text: str, ref: ObservationRef, step: int,
                        *, trusted: bool = False) -> tuple[Optional[EvidenceRecord], str]:
        """Turn a proposed finding into evidence, or refuse.

        `trusted=True` is for text the browser itself returned (an `extract`), whose
        provenance is certain by construction. Everything else is text the model wrote and
        must be checked against the observation it was written against.
        """
        text = " ".join(str(text or "").split())[:EVIDENCE_MAX_CHARS]
        if not text:
            return None, "empty"
        if not trusted:
            ok, why = self.supports(text, ref)
            if not ok:
                return None, why
        resource = self.resources[ref.resource_id]
        record = EvidenceRecord(
            evidence_id=self._mint(), task_id=self.task_id, kind="observed", text=text,
            step=step, observation_id=ref.observation_id, resource_id=ref.resource_id,
            source_url=ref.url, source_title=resource.title,
            figures=all_figure_keys(text),
        )
        self.records[record.evidence_id] = record
        return record, ""

    def record_derived(self, *, text: str, operation: str, sources: list[EvidenceRecord],
                       step: int, operands: Optional[list[str]] = None) -> EvidenceRecord:
        """A deterministic computation's result.

        A derived figure inherits the standing of its inputs: it was on no page, but it
        follows from pages by arithmetic BrowserAgent performed itself. That inheritance has
        to be earned, which is why the operands are checked. Otherwise `add(42.50, 0)` would
        be a laundry: an invented price goes in as an operand and comes out as an
        authoritative computed result. An ungrounded computation still runs and is still
        shown to the model — it is simply not evidence, and a claim citing it fails.
        """
        sources = [s for s in sources if s is not None]
        operand_keys: set[str] = set()
        for operand in (operands or []):
            operand_keys |= numeric_keys(operand)
        accountable = self.known_figures() | numeric_keys(self.goal)
        grounded = operand_keys <= accountable
        record = EvidenceRecord(
            evidence_id=self._mint(), task_id=self.task_id, kind="derived",
            text=" ".join(str(text or "").split())[:EVIDENCE_MAX_CHARS], step=step,
            operation=operation, derived_from=[s.evidence_id for s in sources],
            source_title="computed by BrowserAgent",
            figures=numeric_keys(text) if grounded else set(),
            grounded=grounded,
            invalid_reason="" if grounded else "computed from figures no page showed",
        )
        self.records[record.evidence_id] = record
        return record

    def invalidate(self, evidence_id: str, reason: str) -> None:
        record = self.records.get(evidence_id)
        if record is not None:
            record.valid = False
            record.invalid_reason = reason

    def _mint(self) -> str:
        evidence_id = f"ev_{self.key}_{self._next_evidence}"
        self._next_evidence += 1
        return evidence_id

    # ---- resolving citations ---------------------------------------------------------

    def resolve(self, raw: str) -> Citation:
        """Look up one evidence id the model wrote. Never creates anything.

        A bare number or `ev_7` is read as shorthand for this task's seventh record — a small
        model garbling its own citation should be a lookup, not a fabrication. A *qualified*
        id carrying a different task's key is rejected outright: that is the one form in
        which cross-task evidence could otherwise be smuggled in (V2 hardening §4).
        """
        text = str(raw or "").strip()
        match = _ID_RE.match(text)
        if match is None:
            return Citation(raw=text, reason="not an evidence id BrowserAgent issued")
        key = (match.group("key") or "").lower()
        if key and key != self.key:
            return Citation(raw=text, reason="belongs to a different task")
        evidence_id = f"ev_{self.key}_{int(match.group('n'))}"
        record = self.records.get(evidence_id)
        if record is None:
            return Citation(raw=text, reason="no such evidence in this task")
        if not record.valid:
            return Citation(raw=text, reason=f"invalidated ({record.invalid_reason})")
        if not record.grounded:
            return Citation(raw=text, reason=record.invalid_reason or "not grounded in any page")
        if record.resource_id and record.resource_id not in self.resources:
            return Citation(raw=text, reason="its source was never observed")
        return Citation(raw=text, record=record)

    def lineage(self, record: EvidenceRecord) -> list[EvidenceRecord]:
        """A derived record plus, transitively, the observed records it came from."""
        out: list[EvidenceRecord] = []
        stack = [record]
        seen: set[str] = set()
        while stack:
            current = stack.pop()
            if current.evidence_id in seen:
                continue
            seen.add(current.evidence_id)
            out.append(current)
            for parent_id in current.derived_from:
                parent = self.records.get(parent_id)
                if parent is not None:
                    stack.append(parent)
        return out

    # ---- bounded selection for the prompt --------------------------------------------

    def select(self, *, query: str = "", limit: int = 6, token_budget: int = 320,
               ) -> list[EvidenceRecord]:
        """The evidence the model is shown this turn.

        Never the whole ledger — that would trade the bounded prompt for grounding, which is
        the trade V2 hardening §18 forbids. Ranking is lexical overlap with the goal and the
        active subgoal, plus a recency bias, plus a standing preference for derived records
        (a computed answer is usually the thing a final claim needs to cite). No embeddings
        and no second agent: the deterministic mechanism goes in first and only loses its
        place if a measured retrieval failure says it must.
        """
        wanted = content_terms(query)
        scored: list[tuple[float, EvidenceRecord]] = []
        for record in self.records.values():
            if not record.valid:
                continue
            overlap = len(wanted & content_terms(record.text)) if wanted else 0
            score = float(overlap)
            score += record.step * 0.05
            if record.kind == "derived":
                score += 1.5
            scored.append((score, record))
        scored.sort(key=lambda item: (-item[0], -item[1].step))

        selected: list[EvidenceRecord] = []
        used = 0
        for _score, record in scored:
            cost = count_tokens(record.render())
            if selected and used + cost > token_budget:
                break
            selected.append(record)
            used += cost
            if len(selected) >= limit:
                break
        selected.sort(key=lambda r: r.step)
        return selected

    @staticmethod
    def render(records: list[EvidenceRecord]) -> str:
        if not records:
            return ""
        return "\n".join(f"- {r.render()}" for r in records)

    def render_sources(self, limit: int = 8) -> str:
        if not self.resources:
            return ""
        lines = []
        for resource in sorted(self.resources.values(), key=lambda r: r.first_seen_step)[:limit]:
            lines.append(f"- {resource.title or resource.host or resource.url}"
                         f" ({resource.host or resource.url})")
        if len(self.resources) > limit:
            lines.append(f"- (+{len(self.resources) - limit} more)")
        return "\n".join(lines)

    # ---- persistence -----------------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "key": self.key,
            "requested_sources": list(self.requested_sources),
            "resources": [r.to_dict() for r in self.resources.values()],
            "evidence": [r.to_dict() for r in self.records.values()],
        }

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")
        tmp.replace(path)

    @classmethod
    def load(cls, path: Path, task_id: str) -> "EvidenceLedger":
        """Restore the ledger a crashed run had built. Resuming with an empty ledger would be
        safe but wasteful: every source would have to be re-observed before anything could be
        cited again. A ledger whose `task_id` does not match is ignored rather than adopted —
        evidence does not travel between tasks (V2 hardening §17)."""
        ledger = cls(task_id)
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return ledger
        if data.get("task_id") != task_id:
            return ledger
        ledger.requested_sources = list(data.get("requested_sources") or [])
        for row in data.get("resources") or []:
            resource = VisitedResource(
                resource_id=row["resource_id"], canonical_url=row["canonical_url"],
                url=row.get("url", ""), title=row.get("title", ""),
                first_seen_step=int(row.get("first_seen_step") or 0),
                last_seen_step=int(row.get("last_seen_step") or 0),
                observation_ids=list(row.get("observation_ids") or []),
                aliases=set(row.get("aliases") or []),
                figures=set(row.get("figures") or []),
                text=row.get("text", ""),
            )
            ledger.resources[resource.resource_id] = resource
            # Without this, a resumed run would treat every name it had already read as an
            # invention, because the vocabulary the name check consults lives only in memory.
            ledger._known_terms |= set(resource.text.split())
            ledger._by_canonical[resource.canonical_url] = resource.resource_id
            for alias in resource.aliases:
                ledger._by_canonical.setdefault(alias, resource.resource_id)
            ledger._next_resource = max(ledger._next_resource,
                                        int(resource.resource_id.rsplit("_", 1)[-1]) + 1)
        for row in data.get("evidence") or []:
            record = EvidenceRecord(
                evidence_id=row["evidence_id"], task_id=row["task_id"], kind=row["kind"],
                text=row.get("text", ""), step=int(row.get("step") or 0),
                observation_id=row.get("observation_id", ""),
                resource_id=row.get("resource_id", ""), source_url=row.get("source_url", ""),
                source_title=row.get("source_title", ""),
                derived_from=list(row.get("derived_from") or []),
                operation=row.get("operation", ""), valid=bool(row.get("valid", True)),
                grounded=bool(row.get("grounded", True)),
                invalid_reason=row.get("invalid_reason", ""),
                figures=(numeric_keys(row.get("text", "")) if row["kind"] == "derived"
                         else all_figure_keys(row.get("text", ""))),
            )
            ledger.records[record.evidence_id] = record
            ledger._next_evidence = max(ledger._next_evidence,
                                        int(record.evidence_id.rsplit("_", 1)[-1]) + 1)
        return ledger
