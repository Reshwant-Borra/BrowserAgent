"""Structured JSONL logging + basic secret redaction.

Not a complete secrets-detection system — a best-effort filter so ordinary operation
doesn't casually write passwords/tokens to disk. Anything genuinely sensitive should not
be in an automated browser task's field values in the first place.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

_SECRET_KEY_PATTERN = re.compile(r"(password|passwd|token|secret|api[_-]?key|auth|cookie)", re.IGNORECASE)
# Deliberately key-based only, not value-shape-based: this codebase logs plenty of
# legitimate long opaque strings (SHA-256 state hashes, task ids) that a generic
# "long alphanumeric string" heuristic would also catch, destroying exactly the
# diagnostic data ARCHITECTURE.md's instrumentation section asks for.
REDACTED = "***REDACTED***"


def redact_value(key: str, value: Any) -> Any:
    if isinstance(value, str) and _SECRET_KEY_PATTERN.search(key):
        return REDACTED
    return value


def redact_dict(d: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in d.items():
        if isinstance(v, dict):
            out[k] = redact_dict(v)
        elif isinstance(v, list):
            out[k] = [redact_dict(i) if isinstance(i, dict) else i for i in v]
        else:
            out[k] = redact_value(k, v)
    return out


class JsonlLogger:
    """Appends one JSON object per line — used for both the human-readable structured
    log and the model/action metrics streams. Deliberately never logs raw HTML/DOM."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path

    def log(self, **fields: Any) -> None:
        record = redact_dict(fields)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str) + "\n")


def get_logger(name: str, level: str = "INFO") -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        logger.addHandler(handler)
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    return logger
