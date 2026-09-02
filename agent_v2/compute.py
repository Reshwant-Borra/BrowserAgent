"""Arithmetic the model is not asked to do.

Qwen3 8B is good at deciding that two prices need subtracting and unreliable at subtracting
them. So the split is: the model chooses the operation and names the operands, and this
module produces the answer (V2 hardening §9/§10).

A fixed set of operations over parsed literals. There is no expression language, no `eval`,
no `exec`, no shell, no generated JavaScript and no generated Python — the model cannot
express a computation this module does not already implement, which is the point. Operand
counts and lengths are bounded, division by zero is an error rather than an exception, and
anything that cannot be parsed unambiguously is refused with a message the model can act on
rather than guessed at.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from typing import Any, Optional

MAX_OPERANDS = 20
MAX_OPERAND_CHARS = 64


class ComputeOp(str, Enum):
    COMPARE = "compare"
    MIN = "min"
    MAX = "max"
    SORT_ASC = "sort_asc"
    SORT_DESC = "sort_desc"
    ADD = "add"
    SUBTRACT = "subtract"
    MULTIPLY = "multiply"
    DIVIDE = "divide"
    PERCENT_OF = "percent_of"
    PERCENT_CHANGE = "percent_change"
    COUNT = "count"
    DATE_COMPARE = "date_compare"
    VERSION_COMPARE = "version_compare"


#: How many operands each operation needs: (minimum, maximum or None for "any").
_ARITY: dict[ComputeOp, tuple[int, Optional[int]]] = {
    ComputeOp.COMPARE: (2, 2),
    ComputeOp.MIN: (1, None),
    ComputeOp.MAX: (1, None),
    ComputeOp.SORT_ASC: (1, None),
    ComputeOp.SORT_DESC: (1, None),
    ComputeOp.ADD: (2, None),
    ComputeOp.SUBTRACT: (2, 2),
    ComputeOp.MULTIPLY: (2, None),
    ComputeOp.DIVIDE: (2, 2),
    ComputeOp.PERCENT_OF: (2, 2),
    ComputeOp.PERCENT_CHANGE: (2, 2),
    ComputeOp.COUNT: (0, None),
    ComputeOp.DATE_COMPARE: (2, 2),
    ComputeOp.VERSION_COMPARE: (2, 2),
}


@dataclass
class ComputeResult:
    ok: bool
    operation: str = ""
    #: Human-readable sentence for the task state and for an evidence record.
    text: str = ""
    #: The machine value, when there is a single one: a float, an ordering, a comparison word.
    value: Any = None
    error: str = ""
    operands: list[str] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------------------
# parsing
# --------------------------------------------------------------------------------------

_NUMBER_RE = re.compile(r"^[^\d\-+]*([-+]?\d[\d,]*(?:\.\d+)?)\s*(%)?[^\d]*$")
_VERSION_RE = re.compile(r"^v?(\d+(?:\.\d+)*)(?:[-+]([0-9A-Za-z.\-]+))?$")
_ISO_DATE_RE = re.compile(r"^(\d{4})[-/](\d{1,2})[-/](\d{1,2})$")
_SLASH_DATE_RE = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})$")
_YEAR_RE = re.compile(r"^(\d{4})$")
_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
_TEXT_DATE_RE = re.compile(
    r"^(?:(\d{1,2})\s+)?([A-Za-z]{3,9})\.?\s+(?:(\d{1,2}),?\s+)?(\d{4})$")


def parse_number(text: str) -> Optional[float]:
    """A number written the way a page writes it: `$1,299.99`, `12%`, `8 848 m` is not
    accepted (spaces inside numbers are ambiguous), `-4.5` is."""
    raw = str(text or "").strip()
    if not raw:
        return None
    match = _NUMBER_RE.match(raw)
    if match is None:
        return None
    body = match.group(1).replace(",", "")
    try:
        return float(body)
    except ValueError:
        return None


def parse_version(text: str) -> Optional[tuple]:
    """Dotted numeric versions, optionally `v`-prefixed, optionally with a prerelease suffix.

    Sorts the way versions actually sort: 3.10 above 3.9, and a prerelease below the release
    it precedes. Anything with non-numeric components in the core (`3.x`, `2024a`) is refused
    rather than guessed — an ambiguous version comparison is exactly the kind of thing this
    module exists to avoid getting confidently wrong.
    """
    raw = str(text or "").strip()
    match = _VERSION_RE.match(raw)
    if match is None:
        return None
    parts = tuple(int(p) for p in match.group(1).split("."))
    prerelease = match.group(2)
    # A release outranks any prerelease of the same core version.
    return (parts, 1, ()) if not prerelease else (parts, 0, (prerelease.lower(),))


def parse_date(text: str) -> Optional[date]:
    """ISO first, then unambiguous slash dates and month names.

    `03/04/2020` is refused: it is 3 April in most of the world and 4 March in the United
    States, and a date comparison that silently picks one is worse than one that asks."""
    raw = " ".join(str(text or "").split())
    if not raw:
        return None
    match = _ISO_DATE_RE.match(raw)
    if match:
        return _safe_date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    match = _SLASH_DATE_RE.match(raw)
    if match:
        first, second, year = int(match.group(1)), int(match.group(2)), int(match.group(3))
        if first > 12 and second <= 12:
            return _safe_date(year, second, first)
        if second > 12 and first <= 12:
            return _safe_date(year, first, second)
        return None  # ambiguous
    match = _TEXT_DATE_RE.match(raw)
    if match:
        leading_day, month_name, trailing_day, year = match.groups()
        month = _MONTHS.get(month_name[:3].lower())
        if month is None:
            return None
        day = int(leading_day or trailing_day or 1)
        return _safe_date(int(year), month, day)
    match = _YEAR_RE.match(raw)
    if match:
        return _safe_date(int(match.group(1)), 1, 1)
    return None


def _safe_date(year: int, month: int, day: int) -> Optional[date]:
    try:
        return date(year, month, day)
    except ValueError:
        return None


# --------------------------------------------------------------------------------------
# execution
# --------------------------------------------------------------------------------------

def run_compute(operation: str, operands: list[str],
                labels: Optional[list[str]] = None) -> ComputeResult:
    """Validate and execute one operation. Never raises for bad model input — a refusal is a
    result the loop can hand back so the model can correct itself."""
    raw_operands = [str(o) for o in (operands or [])]
    labels = [str(l) for l in (labels or [])]

    try:
        op = ComputeOp(str(operation or "").strip().lower())
    except ValueError:
        allowed = ", ".join(o.value for o in ComputeOp)
        return ComputeResult(False, operation=str(operation), operands=raw_operands,
                             error=f"'{operation}' is not a supported operation. Use one of: {allowed}")

    if len(raw_operands) > MAX_OPERANDS:
        return ComputeResult(False, operation=op.value, operands=raw_operands[:MAX_OPERANDS],
                             error=f"too many operands ({len(raw_operands)}); the limit is {MAX_OPERANDS}")
    if any(len(o) > MAX_OPERAND_CHARS for o in raw_operands):
        return ComputeResult(False, operation=op.value, operands=raw_operands,
                             error=f"an operand is longer than {MAX_OPERAND_CHARS} characters")

    low, high = _ARITY[op]
    if len(raw_operands) < low or (high is not None and len(raw_operands) > high):
        wanted = f"exactly {low}" if low == high else (f"at least {low}" if high is None else f"{low}-{high}")
        return ComputeResult(False, operation=op.value, operands=raw_operands,
                             error=f"{op.value} needs {wanted} operands, got {len(raw_operands)}")

    result = _dispatch(op, raw_operands, labels)
    result.operands = raw_operands
    result.labels = labels
    result.operation = op.value
    return result


def _dispatch(op: ComputeOp, operands: list[str], labels: list[str]) -> ComputeResult:
    if op is ComputeOp.COUNT:
        return ComputeResult(True, text=f"count = {len(operands)}", value=float(len(operands)))

    if op is ComputeOp.DATE_COMPARE:
        dates = [parse_date(o) for o in operands]
        bad = [o for o, d in zip(operands, dates) if d is None]
        if bad:
            return ComputeResult(False, error=(
                f"could not read {', '.join(repr(b) for b in bad)} as an unambiguous date. "
                "Use YYYY-MM-DD."))
        left, right = dates
        word = "earlier than" if left < right else ("later than" if left > right else "the same date as")
        return ComputeResult(True, value=word,
                             text=f"{_name(labels, 0, operands[0])} ({left.isoformat()}) is {word} "
                                  f"{_name(labels, 1, operands[1])} ({right.isoformat()})")

    if op is ComputeOp.VERSION_COMPARE:
        versions = [parse_version(o) for o in operands]
        bad = [o for o, v in zip(operands, versions) if v is None]
        if bad:
            return ComputeResult(False, error=(
                f"could not read {', '.join(repr(b) for b in bad)} as a numeric version "
                "(for example 3.12.1)."))
        left, right = versions
        word = "older than" if left < right else ("newer than" if left > right else "the same version as")
        return ComputeResult(True, value=word,
                             text=f"{_name(labels, 0, operands[0])} ({operands[0]}) is {word} "
                                  f"{_name(labels, 1, operands[1])} ({operands[1]})")

    numbers = [parse_number(o) for o in operands]
    bad = [o for o, n in zip(operands, numbers) if n is None]
    if bad:
        return ComputeResult(False, error=(
            f"could not read {', '.join(repr(b) for b in bad)} as a number. Give the bare "
            "figure, for example 159.00 or $159.00."))

    if op is ComputeOp.COMPARE:
        left, right = numbers
        word = "less than" if left < right else ("greater than" if left > right else "equal to")
        return ComputeResult(True, value=word,
                             text=f"{_name(labels, 0, operands[0])} ({_fmt(left)}) is {word} "
                                  f"{_name(labels, 1, operands[1])} ({_fmt(right)})")

    if op in (ComputeOp.MIN, ComputeOp.MAX):
        pick = min(numbers) if op is ComputeOp.MIN else max(numbers)
        index = numbers.index(pick)
        which = "lowest" if op is ComputeOp.MIN else "highest"
        return ComputeResult(True, value=pick,
                             text=f"{which} of {len(numbers)} values is {_fmt(pick)} "
                                  f"({_name(labels, index, operands[index])})")

    if op in (ComputeOp.SORT_ASC, ComputeOp.SORT_DESC):
        order = sorted(range(len(numbers)), key=lambda i: numbers[i],
                       reverse=op is ComputeOp.SORT_DESC)
        rendered = ", ".join(f"{_name(labels, i, operands[i])} {_fmt(numbers[i])}" for i in order)
        direction = "ascending" if op is ComputeOp.SORT_ASC else "descending"
        return ComputeResult(True, value=[operands[i] for i in order],
                             text=f"sorted {direction}: {rendered}")

    if op is ComputeOp.ADD:
        total = sum(numbers)
        return ComputeResult(True, value=total,
                             text=f"{' + '.join(_fmt(n) for n in numbers)} = {_fmt(total)}")

    if op is ComputeOp.SUBTRACT:
        left, right = numbers
        return ComputeResult(True, value=left - right,
                             text=f"{_fmt(left)} - {_fmt(right)} = {_fmt(left - right)}")

    if op is ComputeOp.MULTIPLY:
        product = 1.0
        for n in numbers:
            product *= n
        return ComputeResult(True, value=product,
                             text=f"{' x '.join(_fmt(n) for n in numbers)} = {_fmt(product)}")

    if op is ComputeOp.DIVIDE:
        left, right = numbers
        if right == 0:
            return ComputeResult(False, error="cannot divide by zero")
        return ComputeResult(True, value=left / right,
                             text=f"{_fmt(left)} / {_fmt(right)} = {_fmt(left / right)}")

    if op is ComputeOp.PERCENT_OF:
        part, whole = numbers
        if whole == 0:
            return ComputeResult(False, error="cannot express a percentage of zero")
        share = part / whole * 100
        return ComputeResult(True, value=share,
                             text=f"{_fmt(part)} is {_fmt(share)}% of {_fmt(whole)}")

    if op is ComputeOp.PERCENT_CHANGE:
        old, new = numbers
        if old == 0:
            return ComputeResult(False, error="cannot express a percentage change from zero")
        change = (new - old) / abs(old) * 100
        direction = "increase" if change >= 0 else "decrease"
        return ComputeResult(True, value=change,
                             text=f"{_fmt(old)} to {_fmt(new)} is a {_fmt(abs(change))}% {direction}")

    return ComputeResult(False, error=f"{op.value} is not implemented")


def _name(labels: list[str], index: int, fallback: str) -> str:
    label = labels[index].strip() if index < len(labels) and labels[index] else ""
    return label or fallback


def _fmt(value: float) -> str:
    """Numbers as a person writes them: no trailing `.0`, and money-scale values to two
    decimals rather than to seventeen."""
    if value == int(value) and abs(value) < 1e15:
        return str(int(value))
    rounded = round(value, 2)
    return f"{rounded:.2f}".rstrip("0").rstrip(".") if abs(rounded - value) < 1e-9 else f"{value:.4f}"
