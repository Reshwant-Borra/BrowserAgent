from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from agent.auth_detect import blob_indicates_auth_required
from agent.schemas import ActionType, ModelDecision, RiskLevel, classify_risk
from batch.models import FailureCategory, NavigationScope
from memory.event_store import Event, EventType


def normalize_target_url(url: str) -> str:
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    host = parts.netloc.lower()
    path = parts.path or "/"
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")
    return urlunsplit((scheme, host, path, parts.query, ""))


def target_payload(url: str, tab_id: int | None = None, title: str | None = None) -> dict[str, Any]:
    """The discriminated target-identity blob persisted per work item (batch_work_items.
    target_payload). `tab_id` set means this target is an existing open browser tab the
    resolver picked out, not a plain URL to navigate to — see BatchOrchestrator._runtime_policy
    and PlaywrightBackend's preferred_tab_url for how that identity is used to attach to the
    exact right tab instead of navigating whatever tab happens to be active."""
    if tab_id is not None:
        return {"type": "open_tab", "url": url, "tab_id": tab_id, "title": title}
    return {"type": "url", "url": url}


def in_navigation_scope(source_url: str, next_url: str, scope: NavigationScope) -> bool:
    if scope == NavigationScope.UNRESTRICTED:
        return True
    source = urlsplit(source_url)
    dest = urlsplit(next_url)
    if scope == NavigationScope.SAME_ORIGIN:
        return source.scheme.lower() == dest.scheme.lower() and source.netloc.lower() == dest.netloc.lower()
    if scope == NavigationScope.SAME_DOMAIN:
        return _registrable_domain(source.hostname or "") == _registrable_domain(dest.hostname or "")
    return False


def read_only_allows(decision: ModelDecision, element_name: str | None = None) -> bool:
    risk = classify_risk(decision.action, element_name)
    if risk == RiskLevel.CONSEQUENTIAL:
        return False
    return decision.action in {
        ActionType.OPEN_URL,
        ActionType.CLICK,
        ActionType.SCROLL,
        ActionType.BACK,
        ActionType.EXTRACT,
        ActionType.WAIT,
        ActionType.FINISH,
    }


def classify_child_failure(events: list[Event], status: str, last_error: str | None = None) -> FailureCategory:
    # Authoritative, structural signal first: agent/loop.py's _block_by_runtime_policy
    # already records the exact failure_category (SCOPE_BLOCKED/READ_ONLY_BLOCKED) on the
    # TASK_BLOCKED event the moment a runtime policy violation happens. That must win over
    # every heuristic below — in particular over the blob-wide auth-keyword scan, which
    # previously mis-fired whenever *any* event's payload (e.g. an OBSERVATION of a page
    # that merely has a "Sign In" nav link) happened to mention an auth keyword, even though
    # the real, already-known reason for the block was something else entirely.
    explicit_categories = [
        e.payload.get("failure_category") for e in events
        if e.type == EventType.TASK_BLOCKED and e.payload.get("failure_category")
    ]
    if explicit_categories:
        try:
            return FailureCategory(explicit_categories[-1])
        except ValueError:
            pass
    blob = "\n".join([json.dumps(e.payload).lower() for e in events] + [(last_error or "").lower()])
    if "captcha" in blob or "bot challenge" in blob:
        return FailureCategory.CAPTCHA_OR_BOT_CHALLENGE
    if blob_indicates_auth_required(blob):
        return FailureCategory.AUTH_REQUIRED
    if "unsupported" in blob or "malformed target" in blob or "malformed url" in blob:
        return FailureCategory.UNSUPPORTED_PAGE
    if "connect_timeout" in blob or "read_timeout" in blob or "total_request_timeout" in blob or "timeout" in blob:
        return FailureCategory.TIMEOUT
    if (
        "ollama_http_error" in blob
        or "connection_reset" in blob
        or "service_unavailable" in blob
        or "malformed_response" in blob
        or "unknown_inference_failure" in blob
        or "local ollama inference failed" in blob
    ):
        return FailureCategory.MODEL
    if "scope_blocked" in blob or "navigation_scope" in blob:
        return FailureCategory.SCOPE_BLOCKED
    if "read_only_blocked" in blob or "read_only policy" in blob:
        return FailureCategory.READ_ONLY_BLOCKED
    if "consequential" in blob or "approval" in blob:
        return FailureCategory.BLOCKED_CONSEQUENTIAL
    if any(e.type == EventType.TASK_BLOCKED for e in events):
        return FailureCategory.MODEL
    decision_errors = [
        e.payload.get("error") for e in events
        if e.type == EventType.MODEL_DECISION and e.payload.get("error")
    ]
    if decision_errors:
        if any(err in {"malformed_json", "schema_invalid"} for err in decision_errors):
            return FailureCategory.CONTRACT
        return FailureCategory.MODEL
    if any(
        e.type == EventType.VERIFICATION_RESULT
        and e.verification_result
        and not e.verification_result.get("passed", False)
        for e in events
    ):
        return FailureCategory.VERIFICATION
    if status == "running":
        return FailureCategory.MAX_STEPS
    return FailureCategory.UNKNOWN


def finding_dedupe_key(finding: dict[str, Any], source_url: str | None = None) -> str:
    course = _norm(str(finding.get("course") or ""))
    title = _norm(str(
        finding.get("title")
        or finding.get("assignment")
        or finding.get("fact")
        or finding.get("field")
        or finding.get("type")
        or ""
    ))
    value = _norm_due(str(finding.get("value") or finding.get("due_date") or finding.get("deadline") or ""))
    source = normalize_target_url(source_url or finding.get("source_url") or "") if (source_url or finding.get("source_url")) else ""
    if title or value:
        return f"{course}|{title}|{value}" if course else f"{title}|{value}"
    return f"{source}|{_norm(json.dumps(finding, sort_keys=True))}"


def _norm(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().lower())


def _norm_due(value: str) -> str:
    normalized = _norm(value)
    if not normalized or "to be announced" in normalized:
        return ""
    normalized = normalized.removeprefix("due ").removeprefix("deadline: ").removeprefix("submit by ").strip()
    normalized = normalized.split(" at ", 1)[0]
    normalized = normalized.replace("sept ", "sep ")
    month_match = re.fullmatch(r"(?:september|sep)\s+(\d{1,2})(?:,\s*(\d{4}))?", normalized)
    if month_match:
        return f"sep {int(month_match.group(1))}"
    return normalized


def _registrable_domain(host: str) -> str:
    parts = host.lower().split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host.lower()
