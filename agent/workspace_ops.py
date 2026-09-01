"""Deterministic query/sort/filter/dedupe/date/numeric operations on TaskWorkspace entities
(BrowserAgent_General_Autonomous_Agent_Architecture_REVISED.pdf, section 9.1: "Safe
computation surface" — "Do not let Qwen write Python. Add deterministic workspace operations
owned by code"). Every function here is pure and entity-generic: nothing in this module knows
about products, colleges, hotels, internships, or papers — callers supply the field names.

Also owns the generic entity-ingest transform (section 9: "The fix is not a ProductRanker. It
is workspace entities plus deterministic query/computation primitives") that turns a finished
subgoal's structured findings into one comparable WorkspaceEntity with evidence, and the
generic "top-k" phrase parser used by agent/controller.py's completion path — both are plain
text/data transforms, never a task-specific class.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any, Optional

from agent.workspace_models import EvidenceRef, WorkspaceEntity, WorkspaceFact, WorkspacePatch

DEFAULT_ENTITY_TYPE = "candidate"


# --------------------------------------------------------------------------- numeric/date helpers

def _as_number(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        match = re.search(r"-?\d+(?:\.\d+)?", value.replace(",", ""))
        if match:
            try:
                return float(match.group(0))
            except ValueError:
                return None
    return None


def _as_date(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        return None
    text = value.strip()
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y/%m/%d", "%m/%d/%Y", "%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


# --------------------------------------------------------------------------- filter / sort / count

_COMPARATORS = {
    "eq": lambda a, b: a == b,
    "ne": lambda a, b: a != b,
    "contains": lambda a, b: str(b).lower() in str(a).lower(),
    "in": lambda a, b: a in (b or []),
}
_NUMERIC_COMPARATORS = {
    "gt": lambda a, b: a > b,
    "gte": lambda a, b: a >= b,
    "lt": lambda a, b: a < b,
    "lte": lambda a, b: a <= b,
}


def filter_entities(entities: list[WorkspaceEntity], field: str, op: str, value: Any) -> list[WorkspaceEntity]:
    """Generic predicate filter. `field` is looked up in `attributes` first, falling back to
    `name`/`entity_type`/`status` for the handful of top-level entity fields. Entities missing
    the field are dropped (never raise) — a query over incomplete data returns what it can."""
    if op in _NUMERIC_COMPARATORS:
        target = _as_number(value)
        fn = _NUMERIC_COMPARATORS[op]
        out = []
        for e in entities:
            n = _as_number(_field(e, field))
            if n is not None and target is not None and fn(n, target):
                out.append(e)
        return out
    if op not in _COMPARATORS:
        raise ValueError(f"unknown filter op: {op!r}")
    fn = _COMPARATORS[op]
    return [e for e in entities if _has_field(e, field) and fn(_field(e, field), value)]


def sort_entities(
    entities: list[WorkspaceEntity], field: str, direction: str = "asc", *, numeric: bool = True,
) -> list[WorkspaceEntity]:
    """Stable sort by `field`. Entities missing a comparable value sort last, preserving their
    relative order — never dropped."""
    reverse = direction == "desc"

    def key(e: WorkspaceEntity) -> tuple[int, float]:
        raw = _field(e, field)
        n = _as_number(raw) if numeric else None
        if n is None:
            return (1, 0.0)
        return (0, n)

    with_value = [e for e in entities if key(e)[0] == 0]
    without_value = [e for e in entities if key(e)[0] == 1]
    with_value.sort(key=lambda e: key(e)[1], reverse=reverse)
    return with_value + without_value


def numeric_min(entities: list[WorkspaceEntity], field: str) -> Optional[WorkspaceEntity]:
    ranked = sort_entities(entities, field, "asc")
    return ranked[0] if ranked and _as_number(_field(ranked[0], field)) is not None else None


def numeric_max(entities: list[WorkspaceEntity], field: str) -> Optional[WorkspaceEntity]:
    ranked = sort_entities(entities, field, "desc")
    return ranked[0] if ranked and _as_number(_field(ranked[0], field)) is not None else None


def date_min(entities: list[WorkspaceEntity], field: str) -> Optional[WorkspaceEntity]:
    dated = [(e, _as_date(_field(e, field))) for e in entities]
    dated = [(e, d) for e, d in dated if d is not None]
    if not dated:
        return None
    return min(dated, key=lambda pair: pair[1])[0]


def date_max(entities: list[WorkspaceEntity], field: str) -> Optional[WorkspaceEntity]:
    dated = [(e, _as_date(_field(e, field))) for e in entities]
    dated = [(e, d) for e, d in dated if d is not None]
    if not dated:
        return None
    return max(dated, key=lambda pair: pair[1])[0]


def dedupe_entities(entities: list[WorkspaceEntity], keys: list[str]) -> list[WorkspaceEntity]:
    """First-wins dedupe by the tuple of `keys` (looked up via `_field`), order-preserving."""
    seen: set[tuple[Any, ...]] = set()
    out = []
    for e in entities:
        fingerprint = tuple(_field(e, k) for k in keys)
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        out.append(e)
    return out


def count(entities: list[WorkspaceEntity]) -> int:
    return len(entities)


def group_by(entities: list[WorkspaceEntity], field: str) -> dict[str, list[WorkspaceEntity]]:
    groups: dict[str, list[WorkspaceEntity]] = {}
    for e in entities:
        key = str(_field(e, field))
        groups.setdefault(key, []).append(e)
    return groups


def select_top_k(
    entities: list[WorkspaceEntity], k: int, field: Optional[str] = None, direction: str = "asc",
) -> list[WorkspaceEntity]:
    """Deterministic top-k, no model call. When `field` is given, sorts numerically first
    (section 9.1's "sort_entities" + "select_top_k" composed, section 14's "use deterministic
    numeric preprocessing" for the unambiguous case). Without a field, preserves collection
    order (a degenerate fallback — callers with a semantic ranking need should prefer
    agent/ranking.py instead)."""
    ordered = sort_entities(entities, field, direction) if field else list(entities)
    return ordered[: max(0, k)]


def _has_field(e: WorkspaceEntity, field: str) -> bool:
    if field in e.attributes:
        return True
    return field in ("name", "entity_type", "status", "id")


def _field(e: WorkspaceEntity, field: str) -> Any:
    if field in e.attributes:
        return e.attributes[field]
    return getattr(e, field, None)


# --------------------------------------------------------------------------- generic entity ingest

_IDENTITY_FIELDS = ("item_name", "name", "title")

_TITLE_PHRASE_RE = re.compile(r"\b[A-Z][A-Za-z0-9]*(?:\s+[A-Z0-9][A-Za-z0-9]*){1,4}\b")


def _guess_label_from_text(text: str) -> Optional[str]:
    """Generic capitalized-multi-word-phrase heuristic (no domain vocabulary) used only as a
    last-resort entity-name fallback, e.g. pulling "AeroClean 200" out of a subgoal like
    "Navigate to the detail page for AeroClean 200 and record its price_usd and rating." — the
    longest match is preferred since short 1-2 letter capitalized fragments at a sentence start
    ("Navigate to...") never satisfy the 2+ word minimum this pattern requires anyway."""
    matches = _TITLE_PHRASE_RE.findall(text)
    return max(matches, key=len) if matches else None


def extract_title_phrase(text: Optional[str]) -> Optional[str]:
    """Public entry point to the same capitalized-multi-word-phrase heuristic above, used by
    agent/controller.py's desync-subgoal recovery to strip a noisy free-text candidate name
    (e.g. "DataForge Analytics Intern details recorded", where the trailing prose came from
    `_extract_kv_findings`'s leading-text-becomes-name fallback) down to just the core name
    ("DataForge Analytics Intern") before fuzzy-matching it against a pending plan item's own
    (cleaner) subgoal text — without this, the trailing prose breaks the substring match this
    codebase's imprecise-label matching otherwise relies on everywhere else."""
    if not text:
        return None
    return _guess_label_from_text(text)


_KV_PATTERN = re.compile(r'([A-Za-z_][A-Za-z0-9_]*)\s*[:=]\s*(?:"([^"]*)"|([^,;\n]+?))\s*(?=,|;|$)')

# URL schemes are the one common false-positive shape for _KV_PATTERN ("visited http://host/path"
# reads as field="http", value="//host/path") — excluded by name, not by disabling ":" matching
# generally, since "field: value" with a real word before the colon is exactly the shape this
# fallback exists to catch.
_KV_FIELD_BLOCKLIST = {"http", "https", "ftp", "ftps", "mailto", "file", "ws", "wss"}


def _extract_kv_findings(text: str) -> list[dict]:
    """Last-resort generic "key: value" / "key=value" extractor over free-text prose — no
    field names, product names, or domain vocabulary hard-coded, just a punctuation shape.
    Used only when the model wrote neither a typed `structured_result` nor embeddable JSON
    (observed live: Qwen3-8B sometimes answers a subgoal with plain prose like `AeroClean 200
    price_usd: $89.99, rating: 3.9/5` despite being told twice to use the typed field). Each
    match's own full text becomes that finding's `evidence` — it's the model's literal output,
    the same trust level every other unverified model claim in this codebase already carries.
    Leading text before the first key=value pair that itself looks like a real title (the same
    capitalized-multi-word-phrase shape `_guess_label_from_text` looks for elsewhere, e.g.
    "AeroClean 200" above) is treated as a `name` finding, so entity_patch_from_findings's
    existing identity-field logic picks it up as the entity's name instead of falling back to
    the whole prose blob. Deliberately NOT "any leading text" (an earlier version of this code
    did that) — live forensic finding (Phase 3 continued-validation pass): Qwen3-8B's own prose
    commonly opens with an ordinary verb ("Recorded price_per_night_usd: $219.00, rating: ...")
    that isn't the candidate's name at all; accepting it verbatim fed a bogus single-word
    "name" into agent/controller.py's `infer_entity_name` (which prefers ANY identity-field
    value over guessing from the subgoal text, once one exists) and from there into the stale-
    evidence check, wrongly flagging a genuinely correct, on-the-right-page finish as stale.

    Requires at least 2 accepted pairs before returning anything: a single isolated match is
    weak, common-in-plain-prose evidence (e.g. one stray "key: value"-shaped phrase in an
    otherwise ordinary sentence) rather than genuinely structured data — real multi-attribute
    extractions (price + rating, stipend + duration, ...) always produce 2+."""
    matches = list(re.finditer(_KV_PATTERN, text))
    pairs = []
    for m in matches:
        field = m.group(1).strip()
        value = (m.group(2) if m.group(2) is not None else m.group(3) or "").strip()
        if len(field) < 2 or field.lower() in _KV_FIELD_BLOCKLIST or not value:
            continue
        pairs.append((m, field, value))
    if len(pairs) < 2:
        return []

    findings = []
    label_text = text[: pairs[0][0].start()].strip().rstrip(":-—").strip()
    label = _guess_label_from_text(label_text) if label_text else None
    if label:
        findings.append({"field": "name", "value": label, "evidence": label_text})
    for m, field, value in pairs:
        findings.append({"field": field, "value": value, "evidence": m.group(0).strip()})
    return findings


def coerce_structured_result(structured_result: Optional[dict], result_text: str) -> Optional[dict]:
    """Falls back to parsing `result_text` as embedded JSON when the typed `structured_result`
    field is empty. Qwen3-8B sometimes writes `{"findings": [...]}` as a string inside the
    plain-text `result` field despite being told (agent/context_builder.py::render_subgoal_
    block, Phase 3) to use the typed field directly — the exact "JSON inside a JSON string"
    fragility this project already fixed once for batch results (agent/schemas.py's
    FinishStructuredResult docstring; batch/orchestrator.py::_extract_structured_result
    "prefers the typed field when present and only falls back to legacy string-JSON parsing").
    Only ever accepted when it parses to a dict with a `findings` list of entries carrying a
    `value` — never trusted blindly, and the typed field always wins when it already has
    usable findings."""
    if structured_result and structured_result.get("findings"):
        return structured_result
    if not result_text:
        return structured_result
    try:
        parsed = json.loads(result_text)
    except (json.JSONDecodeError, TypeError):
        parsed = None
    if isinstance(parsed, dict) and isinstance(parsed.get("findings"), list):
        findings = [f for f in parsed["findings"] if isinstance(f, dict) and str(f.get("value") or "").strip()]
        if findings:
            return {"summary": parsed.get("summary"), "findings": findings}
    kv_findings = _extract_kv_findings(result_text)
    if kv_findings:
        return {"summary": result_text, "findings": kv_findings}
    return structured_result


def infer_entity_name(structured_result: Optional[dict], fallback_text: str) -> Optional[str]:
    """Generic candidate-identity extraction shared by `entity_patch_from_findings` below and
    agent/controller.py's desync-recovery/stale-evidence checks (Phase 3 corrective pass):
    prefers an explicit identity field (name/title/item_name) among the findings, then a
    capitalized-phrase guess from `fallback_text` (typically the subgoal text), then the
    structured result's own summary. No domain vocabulary anywhere in this priority order."""
    raw: Optional[str] = None
    if structured_result:
        findings = structured_result.get("findings") or []
        for f in findings:
            if not isinstance(f, dict) or not str(f.get("value") or "").strip():
                continue
            if f.get("field") in _IDENTITY_FIELDS or f.get("title"):
                raw = f.get("value") if f.get("field") in _IDENTITY_FIELDS else f.get("title")
                break
    if raw is None:
        guessed = _guess_label_from_text(fallback_text)
        if guessed:
            return guessed
        return structured_result.get("summary") if structured_result else None
    # An explicit identity field is usually already clean ("AeroClean 200"), but live evidence
    # (docs/BROWSERAGENT_MASTER_STATUS.md's Phase 3 corrective pass) showed Qwen3-8B sometimes
    # fills it with instruction-shaped prose instead ("Structured finding for Cedar Plaza") —
    # strip to the core capitalized phrase when one is found, since that is the actual name;
    # falls back to the raw value untouched when no such phrase exists (e.g. a genuinely
    # lowercase or single-word identity), so this never *loses* a clean name.
    return _guess_label_from_text(str(raw)) or raw


def names_plausibly_match(a: Optional[str], b: Optional[str]) -> bool:
    """Case-insensitive substring match either direction, with a minimum-length guard against
    trivial short/single-word false matches — the same imprecise-label reasoning already used
    to tell a real (if abbreviated) candidate name from a genuinely different one (e.g.
    Qwen3-8B writing "QuietSweep" for "QuietSweep Mini"). Generic string-similarity heuristic,
    no domain vocabulary."""
    if not a or not b:
        return False
    na, nb = a.strip().lower(), b.strip().lower()
    if len(na) < 4 or len(nb) < 4:
        return False
    return na in nb or nb in na


def entity_patch_from_findings(
    structured_result: Optional[dict],
    *,
    entity_id: str,
    subgoal: str,
    source_event_id: int,
    source_url: Optional[str],
    entity_type: str = DEFAULT_ENTITY_TYPE,
    preceding_subgoal: Optional[str] = None,
) -> Optional[WorkspacePatch]:
    """Turn one finished subgoal/child's `structured_result` (agent/schemas.py's
    FinishStructuredResult, already a plain dict via the event payload) into a single generic
    WorkspaceEntity with evidence — one entity per ingest call, since a batch/subgoal work item
    is already scoped to "one candidate page" by construction (section 9: workspace entities,
    not a domain-specific ranker). Returns None when there is nothing entity-shaped to ingest
    (no findings with a value) — the caller's existing subgoal_result fact/evidence ingestion is
    untouched either way, this is additive.

    `preceding_subgoal`, when given, is searched for a candidate name ONLY when `subgoal`'s own
    text names none — live evidence (docs/BROWSERAGENT_MASTER_STATUS.md's Phase 3 corrective
    pass) showed a live planner sometimes atomizes a single candidate into several subgoals
    ("Visit AeroClean 200's detail page" then, separately, "Extract price_usd from the page"),
    and the second one's own text never repeats the candidate's name at all — without this
    fallback, the entity ends up misnamed after the instruction text itself ("Extract price_usd
    from the page") instead of the real candidate. Deliberately tried in this order, not
    concatenated together and searched as one blob (an earlier version of this code did that,
    live forensic finding, Phase 3 continued-validation pass): two subgoals naming
    same-length candidates ("Visit the Alpha Widget..." then "Visit the Beta Widget...") made
    `_guess_label_from_text`'s longest-match tiebreak silently prefer whichever name happened to
    appear first in the concatenated text, misattributing an entirely different candidate's own
    findings onto the wrong entity — `subgoal`'s own text is always searched alone first, and
    only consults `preceding_subgoal` when that search finds nothing at all."""
    if not structured_result:
        return None
    findings = structured_result.get("findings") or []
    usable = [f for f in findings if isinstance(f, dict) and str(f.get("value") or "").strip()]
    if not usable:
        return None

    attributes: dict[str, Any] = {}
    for f in usable:
        field_key = f.get("field") or f.get("type") or f.get("title")
        if not field_key:
            continue
        attributes[field_key] = f.get("value")
    name = infer_entity_name(structured_result, subgoal)
    if not name and preceding_subgoal:
        name = infer_entity_name(structured_result, preceding_subgoal)
    name = name or subgoal

    entity = WorkspaceEntity(id=entity_id, entity_type=entity_type, name=str(name)[:200], attributes=attributes)
    evidence = [
        EvidenceRef(
            entity_id=entity_id,
            field_key=f.get("field") or f.get("type") or f.get("title"),
            source_event_id=source_event_id,
            source_url=f.get("source_url") or source_url,
            excerpt=(f.get("evidence") or str(f.get("value")))[:500],
        )
        for f in usable
        if f.get("field") or f.get("type") or f.get("title")
    ]
    return WorkspacePatch(add_entities=[entity], add_evidence=evidence)


# --------------------------------------------------------------------------- top-k phrase parsing

# Spelled-out cardinals up to twenty, matched purely as number words (never a domain term) —
# a live planner LLM rewording a goal's own digit-form count ("the 2 best...") into prose
# ("the two best...") is common enough (docs/BROWSERAGENT_MASTER_STATUS.md's Phase 3 corrective
# pass, live evidence) that a digits-only pattern silently missed the resulting synthesis
# subgoal, letting it fall through to entity ingestion as if it were a new candidate.
_NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
    "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20,
}
_NUMBER_PATTERN = r"(?:\d{1,2}|" + "|".join(_NUMBER_WORDS) + r")"

# A superlative immediately adjacent to a count — a language-structure pattern, never a
# domain/field word. Regular English superlatives share one morphological marker (the "-est"
# suffix: cheapest, highest, smallest, soonest, nearest, largest, ...), matched structurally so
# a live planner's own paraphrase of a subgoal ("highest-paying", "smallest due_in_days") is
# recognized without hardcoding every possible attribute name (live forensic finding, Phase 3
# continued-validation pass: the original fixed word list didn't cover "highest-paying" at all,
# so a synthesis subgoal using it never qualified for the top-k evidence bypass or the goal-
# level deterministic-selection path, and was held to an unsatisfiable extraction-evidence bar
# instead). The handful of irregular superlatives/comparative phrases English doesn't form with
# "-est" (best, worst, top, first, "most/least <word>") are listed explicitly alongside it. An
# optional hyphenated modifier ("-rated", "-paying", "-priced") is swallowed either way.
_SUPERLATIVE = r"(?:\w+est|best|worst|top|first|most\s+\w+|least\s+\w+)(?:-\w+)?"

_TOPK_RE = re.compile(
    rf"\b{_SUPERLATIVE}\s+({_NUMBER_PATTERN})\b"
    rf"|\b({_NUMBER_PATTERN})\s+{_SUPERLATIVE}\b"
    # A relative-clause shape ("the N items WITH the SUPERLATIVE ..."), also purely
    # structural: the count and the superlative are still directly connected, just via
    # "with"/"having" instead of sitting next to each other (live forensic finding, Phase 3
    # continued-validation pass: "Identify the two readings with the smallest due_in_days
    # values" — count and superlative separated by the noun phrase they both describe). The
    # `[^.]{{0,40}}?` gap is bounded and non-greedy so this never reaches across a sentence
    # boundary or matches an unrelated later superlative far down the same goal text.
    rf"|\b({_NUMBER_PATTERN})\b[^.]{{0,40}}?\b(?:with|having)\s+(?:the\s+)?{_SUPERLATIVE}\b",
    re.IGNORECASE,
)


def _parse_count_word(text: str) -> Optional[int]:
    if text.isdigit():
        return int(text)
    return _NUMBER_WORDS.get(text.lower())


def parse_requested_top_k(goal: str) -> Optional[int]:
    """A generic natural-language "top-k" detector ("the 3 best...", "top 5...", "cheapest 3
    ...", "the two best..."), owned by code so the controller can deterministically decide when
    to run a bounded top-k selection instead of trusting the model's own sense of how many to
    report. Not domain-specific — matches purely on number (digit or spelled-out cardinal) +
    comparative-adjective shape, never a product/college/hotel-specific keyword."""
    match = _TOPK_RE.search(goal)
    if not match:
        return None
    k = _parse_count_word(match.group(1) or match.group(2) or match.group(3))
    if k is None:
        return None
    return k if 1 <= k <= 20 else None


def render_entities_report(entities: list[WorkspaceEntity], rationale: Optional[dict[str, str]] = None) -> str:
    """Deterministic, code-owned rendering of a final selected-entity set — never invents an
    entity not already in `entities` (section 13: "no unsupported final entity")."""
    rationale = rationale or {}
    lines = []
    for i, e in enumerate(entities, start=1):
        attrs = ", ".join(f"{k}={v}" for k, v in e.attributes.items())
        line = f"{i}. {e.name or e.id} ({attrs})" if attrs else f"{i}. {e.name or e.id}"
        note = rationale.get(e.id)
        if note:
            line += f" — {note}"
        lines.append(line)
    return "\n".join(lines)
