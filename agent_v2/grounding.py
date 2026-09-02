"""What may be presented as supported.

The final answer used to be whatever prose the model produced. It is now checked against the
`EvidenceLedger` before the user ever sees it, deterministically and without a second model
call (V2 hardening §7: if entailment cannot be proven, do not pretend that it can — make the
contract conservative enough that *provenance* can be).

Two independent layers, because the model must not be able to opt out of either:

1. **Citations.** `finish` may carry a list of claims, each naming the evidence ids it rests
   on. Every id is resolved against the ledger; unknown, foreign-task and invalidated ids are
   rejected, and the figures inside a claim must appear in the evidence it cites — which is
   what catches a correct price cited for the wrong product.

2. **The answer prose itself.** Nothing obliges the model to declare a claim, so the answer
   text is checked whether or not it declared anything: every significant figure must be one
   the task can account for, every quoted span must be on a page it actually loaded, and
   every source it names must be one it actually observed. Quotation marks are irrelevant
   here — `"The price is $99."`, `The price is $99.` and `price = 99` are the same claim and
   are treated identically (V2 hardening §5).

Synthesis stays possible (V2 hardening §6). "Widget B looks like the better value" needs no
evidence id, because it asserts nothing about the world that a page could confirm; what it
may not do is smuggle in a figure or a source that no page produced.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from agent_v2.ledger import (
    EvidenceLedger,
    EvidenceRecord,
    all_figure_keys,
    content_terms,
    figure_keys,
    normalize,
    quoted_spans,
    significant_figures,
)


class ClaimKind:
    """How a claim relates to the world, and therefore what would make it supportable."""

    SOURCE = "source"        # read off a page — needs evidence
    DERIVED = "derived"      # computed by BrowserAgent — needs a derived record
    SYNTHESIS = "synthesis"  # the model's own judgement — premises must be grounded
    META = "meta"            # a statement about the task itself — checked against task state

    ALL = (SOURCE, DERIVED, SYNTHESIS, META)


@dataclass
class Claim:
    text: str
    evidence_ids: list[str] = field(default_factory=list)
    kind: str = ClaimKind.SOURCE


@dataclass
class ClaimVerdict:
    claim: Claim
    supported: bool
    reason: str = ""
    records: list[EvidenceRecord] = field(default_factory=list)


@dataclass
class GroundingReport:
    """Everything deterministically wrong with a proposed final answer."""

    invalid_citations: list[str] = field(default_factory=list)
    unsupported_claims: list[str] = field(default_factory=list)
    unsupported_figures: list[str] = field(default_factory=list)
    unsupported_quotes: list[str] = field(default_factory=list)
    unvisited_sources: list[str] = field(default_factory=list)
    verdicts: list[ClaimVerdict] = field(default_factory=list)

    @property
    def supported_claims(self) -> int:
        return sum(1 for v in self.verdicts if v.supported)

    @property
    def problems(self) -> list[str]:
        """One flat list, phrased so it can be shown to the model verbatim."""
        out: list[str] = []
        out += [f"cited evidence {c}" for c in self.invalid_citations]
        out += [f"the claim {c}" for c in self.unsupported_claims]
        out += [f"the figure {f}" for f in self.unsupported_figures]
        out += [f"the quotation {q}" for q in self.unsupported_quotes]
        out += [f"the source {s}" for s in self.unvisited_sources]
        return out

    @property
    def clean(self) -> bool:
        return not self.problems

    def label(self, limit: int = 5) -> str:
        """The disclosure appended to an answer that still has problems after a challenge.

        An unsupported claim is never silently shipped as though it had been read off a page
        (V2 hardening §29): either the model fixes it, or the user is told which parts of the
        answer this task could not stand behind."""
        parts: list[str] = []
        if self.unsupported_figures:
            parts.append("figures not found on any page this task opened: "
                         + ", ".join(self.unsupported_figures[:limit]))
        if self.unsupported_quotes:
            parts.append("quotations not found on any page this task opened: "
                         + ", ".join(self.unsupported_quotes[:limit]))
        if self.unvisited_sources:
            parts.append("sources this task never opened: "
                         + ", ".join(self.unvisited_sources[:limit]))
        if self.unsupported_claims:
            parts.append("claims with no supporting evidence: "
                         + "; ".join(self.unsupported_claims[:limit]))
        if self.invalid_citations:
            parts.append("citations that do not resolve: "
                         + ", ".join(self.invalid_citations[:limit]))
        if not parts:
            return ""
        return "[NOT VERIFIED — " + "; ".join(parts) + "]"


# --------------------------------------------------------------------------------------
# outcome classification (V2 hardening §29)
# --------------------------------------------------------------------------------------

FULL_SUCCESS = "FULL_SUCCESS"
PARTIAL_GROUNDED = "PARTIAL_GROUNDED"
SAFE_FAILURE = "SAFE_FAILURE"
UNSUPPORTED_SUCCESS = "UNSUPPORTED_SUCCESS"


def classify_outcome(*, done: bool, report: GroundingReport, labelled: bool) -> str:
    """`UNSUPPORTED_SUCCESS` is the only outcome the release gate forbids, and it is reachable
    only by presenting an ungrounded claim *without* disclosure. The loop always labels, so
    the category exists to be measured at zero rather than to be hoped about."""
    if report.clean:
        return FULL_SUCCESS if done else SAFE_FAILURE
    if not labelled:
        return UNSUPPORTED_SUCCESS
    return PARTIAL_GROUNDED


# --------------------------------------------------------------------------------------
# source mentions
# --------------------------------------------------------------------------------------

#: Anything shaped like a host. Deliberately not an allowlist of TLDs: an unknown TLD must
#: still be flagged, because failing to flag an unvisited source is the dangerous direction.
_HOST_RE = re.compile(
    r"\b(?:https?://)?((?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,24})\b", re.I)

#: Dotted things that are not hosts. File extensions and library names ("Node.js", "index.html",
#: "config.json") would otherwise read as sources the task failed to visit.
_NOT_HOSTS = {
    "js", "ts", "jsx", "tsx", "py", "rb", "go", "rs", "java", "php", "html", "htm", "css",
    "json", "yaml", "yml", "xml", "csv", "tsv", "md", "txt", "pdf", "png", "jpg", "jpeg",
    "gif", "svg", "webp", "zip", "tar", "gz", "exe", "dll", "sh", "bat", "ini", "toml",
    "log", "sql", "env", "lock", "cfg", "conf", "g", "e", "al", "etc", "ai",
}


def hosts_mentioned(text: str) -> list[str]:
    """Hosts named in a piece of prose, in order, deduplicated."""
    out: list[str] = []
    for match in _HOST_RE.finditer(str(text or "")):
        host = match.group(1).lower().removeprefix("www.")
        if host.rsplit(".", 1)[-1] in _NOT_HOSTS:
            continue
        if len(host.split(".")) < 2:
            continue
        if host not in out:
            out.append(host)
    return out


#: Words that turn "nodejs.org" from a claim into an admission. An answer that says it could
#: not reach a source is exactly the honest outcome V2 hardening §29 asks for, and annotating
#: it as unverified would be noise.
_NEGATION_RE = re.compile(
    r"\b(could ?n[o']t|cannot|can ?not|did ?n[o']t|do ?n[o']t|was ?n[o']t|were ?n[o']t|"
    r"unable|never|failed|missing|unverified|not verified|not able|no access|blocked|"
    r"without (?:opening|visiting|checking)|did not (?:open|visit|check|reach))\b", re.I)


def _disclaimed(answer: str, needle: str, window: int = 110) -> bool:
    """Is this mention wrapped in an admission that it was not actually reached?"""
    if not needle:
        return False
    lowered = answer.lower()
    start = 0
    while True:
        index = lowered.find(needle.lower(), start)
        if index == -1:
            return False
        left = max(0, index - window)
        if _NEGATION_RE.search(answer[left: index + len(needle) + window]):
            return True
        start = index + len(needle)


# --------------------------------------------------------------------------------------
# claim checking
# --------------------------------------------------------------------------------------

def check_claim(claim: Claim, ledger: EvidenceLedger, *, goal_figures: set[str],
                goal_text: str, meta_figures: set[str]) -> tuple[ClaimVerdict, list[str]]:
    """One claim against the ledger. Returns the verdict and any invalid citations found."""
    invalid: list[str] = []
    records: list[EvidenceRecord] = []
    for raw in claim.evidence_ids[:12]:
        citation = ledger.resolve(raw)
        if citation.ok:
            records.append(citation.record)
        else:
            invalid.append(f"{citation.raw!r} — {citation.reason}")

    kind = claim.kind if claim.kind in ClaimKind.ALL else ClaimKind.SOURCE
    figures = {k for token in significant_figures(claim.text) for k in figure_keys(token)}
    figures -= goal_figures

    if kind == ClaimKind.META:
        # "I checked three pages" is not about the world, it is about the run — and the run is
        # something BrowserAgent knows exactly.
        if figures and not (figures & meta_figures) and not ledger.supports_figures(figures):
            return ClaimVerdict(claim, False, "states a number the run does not support"), invalid
        return ClaimVerdict(claim, True), invalid

    if kind == ClaimKind.SYNTHESIS:
        # Judgement is allowed; the facts it rests on are not exempt.
        if figures and not ledger.supports_figures(figures):
            return ClaimVerdict(claim, False, "rests on a figure no page showed"), invalid
        return ClaimVerdict(claim, True, records=records), invalid

    if not records:
        return ClaimVerdict(claim, False, "cites no usable evidence"), invalid

    if kind == ClaimKind.DERIVED and not any(r.kind == "derived" for r in records):
        return ClaimVerdict(claim, False,
                            "is presented as a calculation but cites no computed result"), invalid

    # The provenance test that matters: the numbers in the sentence must be in the evidence
    # the sentence points at. A real price attached to the wrong product fails here.
    cited_figures: set[str] = set()
    lineage = [related for record in records for related in ledger.lineage(record)]
    for related in lineage:
        cited_figures |= related.figures
    unmatched = sorted(
        token for token in significant_figures(claim.text)
        if not (figure_keys(token) & (cited_figures | goal_figures)))
    if unmatched:
        return ClaimVerdict(
            claim, False,
            f"states {', '.join(unmatched[:3])}, which the evidence it cites does not show",
            records), invalid

    # Only assembled when there is actually a quotation to check — the cited resources' page
    # text runs to tens of kilobytes each, and most claims quote nothing.
    spans = [s for s in quoted_spans(claim.text) if len(normalize(s).split()) >= 3]
    cited_text = ""
    if spans:
        parts = [normalize(r.text) for r in lineage]
        parts += [ledger.resources[r.resource_id].text for r in lineage
                  if r.resource_id in ledger.resources]
        cited_text = " ".join(parts)
    for span in spans:
        needle = normalize(span)
        if needle not in cited_text and needle not in normalize(goal_text):
            return ClaimVerdict(claim, False,
                                f'quotes "{span[:48]}", which its evidence does not contain',
                                records), invalid

    return ClaimVerdict(claim, True, records=records), invalid


def check_answer(*, answer: str, claims: list[Claim], ledger: EvidenceLedger, goal: str,
                 meta_figures: Optional[set[str]] = None) -> GroundingReport:
    """The whole contract, applied to one proposed finish."""
    report = GroundingReport()
    goal_figures = all_figure_keys(goal)
    meta_figures = meta_figures or set()

    for claim in claims[:20]:
        verdict, invalid = check_claim(claim, ledger, goal_figures=goal_figures,
                                       goal_text=goal, meta_figures=meta_figures)
        report.verdicts.append(verdict)
        report.invalid_citations.extend(invalid)
        if not verdict.supported:
            report.unsupported_claims.append(f'"{claim.text[:90]}" ({verdict.reason})')

    # --- the answer prose, checked whether or not any claim was declared ----------------
    # Deliberately *without* `meta_figures`: the run's own counters would otherwise be a small
    # laundry — a task on step 12 would account for "$12". Statements about the run do not
    # need the exemption anyway, since a bare small integer is never a significant figure.
    known = ledger.known_figures() | goal_figures
    for token in significant_figures(answer):
        if not (figure_keys(token) & known) and token not in report.unsupported_figures:
            report.unsupported_figures.append(token)

    goal_normalized = normalize(goal)
    for span in quoted_spans(answer):
        needle = normalize(span)
        if len(needle.split()) < 3 or len(needle) < 12:
            continue
        if ledger.supports_span(span) or needle in goal_normalized:
            continue
        quoted = f'"{span[:60]}"'
        if quoted not in report.unsupported_quotes:
            report.unsupported_quotes.append(quoted)

    visited_hosts = ledger.hosts()
    for host in hosts_mentioned(answer):
        if host in visited_hosts or any(host == h or h.endswith("." + host) for h in visited_hosts):
            continue
        if _disclaimed(answer, host):
            continue
        report.unvisited_sources.append(host)

    return report


# --------------------------------------------------------------------------------------
# source coverage (V2 hardening §8)
# --------------------------------------------------------------------------------------

@dataclass
class SourceCoverage:
    requested: list[str] = field(default_factory=list)
    visited: list[str] = field(default_factory=list)
    with_evidence: list[str] = field(default_factory=list)
    named_in_answer: list[str] = field(default_factory=list)
    requested_but_unvisited: list[str] = field(default_factory=list)
    visited_without_evidence: list[str] = field(default_factory=list)

    def render(self) -> str:
        lines = [f"visited {len(self.visited)} source(s), {len(self.with_evidence)} of them "
                 "contributed evidence"]
        if self.requested_but_unvisited:
            lines.append("not opened: " + ", ".join(self.requested_but_unvisited))
        return "; ".join(lines)


def source_coverage(ledger: EvidenceLedger, answer: str = "") -> SourceCoverage:
    """Which sources were asked for, reached, used, and represented in the answer.

    Nothing here trusts a declaration: `visited` comes from observations, `with_evidence`
    from records bound to those observations."""
    visited = sorted({r.host for r in ledger.resources.values() if r.host})
    used_resources = {r.resource_id for r in ledger.records.values()
                      if r.valid and r.kind == "observed" and r.resource_id}
    with_evidence = sorted({ledger.resources[rid].host for rid in used_resources
                            if rid in ledger.resources and ledger.resources[rid].host})
    requested = list(ledger.requested_sources)
    unvisited = [s for s in requested if not ledger.reached(s)]
    return SourceCoverage(
        requested=requested,
        visited=visited,
        with_evidence=with_evidence,
        named_in_answer=hosts_mentioned(answer),
        requested_but_unvisited=unvisited,
        visited_without_evidence=[h for h in visited if h not in with_evidence],
    )


def relevant_terms(*parts: str) -> set[str]:
    """Query terms for bounded evidence retrieval."""
    return content_terms(" ".join(p for p in parts if p))
