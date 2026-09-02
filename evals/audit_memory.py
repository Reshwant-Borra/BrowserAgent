"""Forensic audit of what the agent actually made durable (V2 hardening §13).

Reads a live memory database and classifies every active row against the write policy in
`agent_v2.memory`. The point is not to grep the database for secrets afterwards — that is a
weak test, because a policy can only be judged against rows a real run produced. So this
prints the rows themselves, grouped by verdict, and that distribution is what the thresholds
in `classify_write` answer to.

    python -m evals.audit_memory
    python -m evals.audit_memory --db runtime/v2/memory.sqlite3 --verbose

Exit status is 1 if any *currently stored* row would be refused today, which is what makes
this usable as a check after a policy change: the database is allowed to contain rows written
under an older, laxer policy, but you should know about them.
"""
from __future__ import annotations

import argparse
import sqlite3
from collections import Counter
from pathlib import Path

from agent_v2.memory import WritePolicy, classify_write

#: What a row is worth, judged from its content alone. The first four are the categories the
#: spec asks about; the rest are the ways a row fails to be durable knowledge.
USEFUL_LONG_TERM = "USEFUL_LONG_TERM"
USEFUL_PROCEDURAL = "USEFUL_PROCEDURAL"

_VERDICT_LABEL = {
    WritePolicy.SECRET: "SENSITIVE",
    WritePolicy.TRANSIENT: "TASK_SPECIFIC",
    WritePolicy.EPISODIC: "TASK_SPECIFIC",
    WritePolicy.GOAL_ECHO: "TASK_SPECIFIC",
    WritePolicy.LOW_INFORMATION: "LOW_INFORMATION",
}


def classify_row(text: str, type: str) -> tuple[str, str]:
    verdict, reason = classify_write(text, type=type)
    if verdict != WritePolicy.ACCEPT:
        return _VERDICT_LABEL.get(verdict, verdict), reason
    if type in ("strategy", "procedure"):
        return USEFUL_PROCEDURAL, ""
    return USEFUL_LONG_TERM, ""


def audit(db_path: Path, verbose: bool = False) -> int:
    if not db_path.exists():
        print(f"no memory database at {db_path}")
        return 0
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, type, text, domain, source_task, created_at FROM memories "
        "WHERE active = 1 ORDER BY id"
    ).fetchall()
    procedures = conn.execute(
        "SELECT COUNT(*) AS n FROM procedures WHERE active = 1").fetchone()["n"]

    counts: Counter[str] = Counter()
    duplicates: Counter[str] = Counter()
    offenders: list[tuple[int, str, str, str]] = []
    for row in rows:
        label, reason = classify_row(row["text"], row["type"])
        counts[label] += 1
        duplicates[" ".join(row["text"].lower().split())] += 1
        if label not in (USEFUL_LONG_TERM, USEFUL_PROCEDURAL):
            offenders.append((row["id"], row["type"], label, f"{row['text']}  <- {reason}"))

    repeated = sum(n - 1 for n in duplicates.values() if n > 1)
    total = len(rows)
    print(f"{db_path}: {total} active memories, {procedures} procedures")
    if total:
        for label, n in counts.most_common():
            print(f"  {label:<18} {n:>4}  ({n / total:.0%})")
        print(f"  {'DUPLICATE':<18} {repeated:>4}")
    if offenders:
        print("\nrows that today's write policy would refuse:")
        for memory_id, type, label, text in offenders:
            print(f"  [{memory_id}] {label} ({type}) {text[:160]}")
    if verbose:
        print("\nall active rows:")
        for row in rows:
            label, _ = classify_row(row["text"], row["type"])
            print(f"  [{row['id']}] {label} {row['type']}/{row['domain'] or '-'}: {row['text'][:150]}")
    conn.close()
    return 1 if offenders else 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m evals.audit_memory")
    parser.add_argument("--db", default="runtime/v2/memory.sqlite3")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    return audit(Path(args.db), args.verbose)


if __name__ == "__main__":
    raise SystemExit(main())
