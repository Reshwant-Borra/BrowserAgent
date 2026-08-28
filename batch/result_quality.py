from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field, ValidationError, field_validator

from batch.models import ResultContract


ACTIONABLE_ASSIGNMENT_STATUSES = {"upcoming", "current_incomplete"}
NON_ACTIONABLE_ASSIGNMENT_STATUSES = {"completed", "closed", "past_archived", "none"}
ALL_ASSIGNMENT_STATUSES = ACTIONABLE_ASSIGNMENT_STATUSES | NON_ACTIONABLE_ASSIGNMENT_STATUSES | {"unknown"}

NON_ACTIONABLE_STATUS_CUES = {
    "submitted": "completed",
    "completed": "completed",
    "graded": "completed",
    "closed": "closed",
    "no longer accepting submissions": "closed",
    "archived": "past_archived",
    "last month": "past_archived",
    "old assignment": "past_archived",
    "old review": "past_archived",
}


class GenericFinding(BaseModel):
    evidence: str = ""
    source_url: str | None = None

    model_config = {"extra": "allow"}


class GenericBatchResult(BaseModel):
    relevant: bool = False
    summary: str = ""
    findings: list[dict[str, Any]] = Field(default_factory=list)

    model_config = {"extra": "allow"}

    @field_validator("findings", mode="before")
    @classmethod
    def _findings_list(cls, value: Any) -> list[Any]:
        return value if isinstance(value, list) else []


def normalize_structured_result(
    structured: dict[str, Any],
    contract: ResultContract,
    final_url: str | None,
    page_excerpt: str,
) -> tuple[dict[str, Any], dict[str, int | bool]]:
    quality: dict[str, int | bool] = {
        "schema_valid": True,
        "structured_outputs_attempted": 1,
        "schema_valid_results": 1,
        "evidence_backed_findings": 0,
        "unsupported_findings_rejected": 0,
        "status_conflicts": 0,
    }
    try:
        base = GenericBatchResult.model_validate(structured).model_dump()
    except ValidationError:
        quality["schema_valid"] = False
        quality["schema_valid_results"] = 0
        base = {"relevant": False, "summary": "Invalid result schema.", "findings": []}

    if contract.name == "assignment":
        normalized = _normalize_assignment_result(base, final_url, page_excerpt, quality)
    elif contract.name == "research":
        normalized = _normalize_research_result(base, contract, final_url, quality)
    else:
        normalized = _normalize_generic_result(base, final_url, quality)

    normalized["_quality"] = quality
    return normalized, quality


def _normalize_generic_result(
    structured: dict[str, Any],
    final_url: str | None,
    quality: dict[str, int | bool],
) -> dict[str, Any]:
    findings = []
    for raw in structured.get("findings", []):
        if not isinstance(raw, dict):
            quality["unsupported_findings_rejected"] += 1
            continue
        evidence = str(raw.get("evidence") or "").strip()
        if not evidence:
            quality["unsupported_findings_rejected"] += 1
            continue
        finding = dict(raw)
        finding.setdefault("source_url", final_url)
        findings.append(finding)
        quality["evidence_backed_findings"] += 1
    return {**structured, "findings": findings, "relevant": bool(findings) or bool(structured.get("relevant"))}


def _normalize_assignment_result(
    structured: dict[str, Any],
    final_url: str | None,
    page_excerpt: str,
    quality: dict[str, int | bool],
) -> dict[str, Any]:
    findings = []
    page_status = explicit_non_actionable_status(page_excerpt)
    for raw in structured.get("findings", []):
        if not isinstance(raw, dict):
            quality["unsupported_findings_rejected"] += 1
            continue
        evidence = str(raw.get("evidence") or "").strip()
        if not evidence:
            quality["unsupported_findings_rejected"] += 1
            continue
        finding = dict(raw)
        finding.setdefault("source_url", final_url)
        finding["title"] = str(finding.get("title") or finding.get("assignment") or "").strip()
        finding["course"] = str(finding.get("course") or "").strip()
        finding["due_date"] = str(finding.get("due_date") or finding.get("value") or "").strip()
        status = normalize_assignment_status(finding.get("status"), evidence, page_excerpt)
        actionable = _coerce_bool(finding.get("actionable"))
        if actionable is None:
            actionable = status in ACTIONABLE_ASSIGNMENT_STATUSES
        if page_status and actionable:
            quality["status_conflicts"] += 1
            actionable = False
            status = page_status
            finding["status_conflict"] = True
        elif status in NON_ACTIONABLE_ASSIGNMENT_STATUSES and actionable:
            quality["status_conflicts"] += 1
            actionable = False
            finding["status_conflict"] = True
        if status == "unknown":
            actionable = False
        finding["status"] = status
        finding["actionable"] = bool(actionable)
        finding["evidence"] = evidence
        findings.append(finding)
        quality["evidence_backed_findings"] += 1
    return {**structured, "findings": findings, "relevant": any(f.get("actionable") for f in findings)}


def _normalize_research_result(
    structured: dict[str, Any],
    contract: ResultContract,
    final_url: str | None,
    quality: dict[str, int | bool],
) -> dict[str, Any]:
    requested = [field for field in contract.required_fields if field not in {"relevant", "source", "source_url", "evidence"}]
    fields = {
        field: {"status": "not_found", "value": "", "evidence": "", "source_url": final_url}
        for field in requested
    }
    findings = []
    raw_fields = structured.get("fields") if isinstance(structured.get("fields"), dict) else {}
    for field in requested:
        raw_field = raw_fields.get(field)
        if isinstance(raw_field, str):
            status = raw_field.strip().lower()
            if status not in {"found", "not_found", "unresolved"}:
                status = "unresolved"
            fields[field] = {"status": status, "value": "", "evidence": "", "source_url": final_url}
            continue
        if not isinstance(raw_field, dict):
            continue
        status = str(raw_field.get("status") or "unresolved").strip().lower()
        if status not in {"found", "not_found", "unresolved"}:
            status = "unresolved"
        value = str(raw_field.get("value") or "").strip()
        evidence = str(raw_field.get("evidence") or "").strip()
        source_url = str(raw_field.get("source_url") or final_url or "")
        if status == "found":
            if not value or not evidence:
                fields[field] = {"status": "unresolved", "value": value, "evidence": evidence, "source_url": source_url}
                quality["unsupported_findings_rejected"] += 1
                continue
            fact = {"field": field, "value": value, "evidence": evidence, "source_url": source_url}
            fields[field] = {"status": "found", **fact}
            findings.append(fact)
            quality["evidence_backed_findings"] += 1
        else:
            fields[field] = {"status": status, "value": "", "evidence": "", "source_url": source_url}
    for raw in structured.get("findings", []):
        if not isinstance(raw, dict):
            quality["unsupported_findings_rejected"] += 1
            continue
        field = _normalize_research_field(raw, requested)
        evidence = str(raw.get("evidence") or "").strip()
        value = str(raw.get("value") or raw.get("fact") or raw.get("title") or "").strip()
        if not field or not evidence or not value:
            quality["unsupported_findings_rejected"] += 1
            continue
        source_url = str(raw.get("source_url") or raw.get("source") or final_url or "")
        fact = {
            "field": field,
            "value": value,
            "evidence": evidence,
            "source_url": source_url,
        }
        fields.setdefault(field, {"status": "not_found", "value": "", "evidence": "", "source_url": final_url})
        if (
            fields[field].get("status") == "found"
            and fields[field].get("value")
            and fields[field].get("evidence")
        ):
            continue
        fields[field] = {"status": "found", **fact}
        findings.append(fact)
        quality["evidence_backed_findings"] += 1
    relevant = any(item.get("status") == "found" for item in fields.values()) or bool(findings)
    return {**structured, "fields": fields, "findings": findings, "relevant": relevant}


def normalize_assignment_status(status: Any, evidence: str = "", page_excerpt: str = "") -> str:
    text = " ".join([str(status or ""), evidence, page_excerpt]).lower()
    explicit = explicit_non_actionable_status(text)
    if explicit:
        return explicit
    if any(word in text for word in ("upcoming", "due ", "deadline", "submit by", "assigned")):
        return "upcoming"
    if any(word in text for word in ("incomplete", "pending", "not submitted", "to do")):
        return "current_incomplete"
    normalized = str(status or "").strip().lower().replace("-", "_").replace(" ", "_")
    if normalized == "missing_due_date":
        return "upcoming"
    if normalized in ALL_ASSIGNMENT_STATUSES:
        return normalized
    return "unknown"


def explicit_non_actionable_status(text: str) -> str | None:
    lowered = text.lower()
    for cue, status in NON_ACTIONABLE_STATUS_CUES.items():
        if cue in lowered:
            return status
    if re.search(r"\bpast due\b|\boverdue\b", lowered):
        return "past_archived"
    return None


def _normalize_research_field(raw: dict[str, Any], requested: list[str]) -> str | None:
    explicit = str(raw.get("field") or raw.get("type") or "").strip().lower().replace(" ", "_")
    if explicit in requested:
        return explicit
    blob = " ".join(str(raw.get(key) or "") for key in ("field", "type", "title", "fact", "value", "evidence")).lower()
    for field in requested:
        words = field.replace("_", " ").split()
        if all(word in blob for word in words):
            return field
    if "pricing" in requested and any(word in blob for word in ("price", "pricing", "$", "seat")):
        return "pricing"
    if "education_discount" in requested and "education" in blob and "discount" in blob:
        return "education_discount"
    if "public_api_docs" in requested and "api" in blob and any(word in blob for word in ("docs", "documentation", "rest")):
        return "public_api_docs"
    if "item_name" in requested and any(word in blob for word in ("item", "product", "name", "title")):
        return "item_name"
    return explicit if explicit and explicit in requested else None


def _coerce_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "yes", "1"}:
            return True
        if lowered in {"false", "no", "0"}:
            return False
    return None
