from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class BatchStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    COMPLETED_WITH_FAILURES = "completed_with_failures"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"


class WorkItemStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED_RETRYABLE = "failed_retryable"
    BLOCKED = "blocked"
    FAILED_FINAL = "failed_final"
    SKIPPED_DUPLICATE = "skipped_duplicate"


class FailureCategory(str, Enum):
    NAVIGATION = "NAVIGATION"
    MODEL = "MODEL"
    CONTRACT = "CONTRACT"
    VERIFICATION = "VERIFICATION"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    CAPTCHA_OR_BOT_CHALLENGE = "CAPTCHA_OR_BOT_CHALLENGE"
    TIMEOUT = "TIMEOUT"
    UNSUPPORTED_PAGE = "UNSUPPORTED_PAGE"
    BLOCKED_CONSEQUENTIAL = "BLOCKED_CONSEQUENTIAL"
    MAX_STEPS = "MAX_STEPS"
    UNKNOWN = "UNKNOWN"


class BatchEventType(str, Enum):
    BATCH_CREATED = "BATCH_CREATED"
    WORK_ITEM_ADDED = "WORK_ITEM_ADDED"
    WORK_ITEM_DEDUPED = "WORK_ITEM_DEDUPED"
    WORK_ITEM_STARTED = "WORK_ITEM_STARTED"
    WORK_ITEM_COMPLETED = "WORK_ITEM_COMPLETED"
    WORK_ITEM_RETRY_SCHEDULED = "WORK_ITEM_RETRY_SCHEDULED"
    WORK_ITEM_FAILED = "WORK_ITEM_FAILED"
    WORK_ITEM_BLOCKED = "WORK_ITEM_BLOCKED"
    WORK_ITEM_RECONCILED = "WORK_ITEM_RECONCILED"
    BATCH_PAUSED = "BATCH_PAUSED"
    BATCH_RESUMED = "BATCH_RESUMED"
    SYNTHESIS_STARTED = "SYNTHESIS_STARTED"
    SYNTHESIS_COMPLETED = "SYNTHESIS_COMPLETED"
    BATCH_COMPLETED = "BATCH_COMPLETED"


class SessionMode(str, Enum):
    ISOLATED = "isolated_session"
    SHARED = "shared_session"


class NavigationScope(str, Enum):
    SAME_ORIGIN = "same_origin"
    SAME_DOMAIN = "same_domain"
    UNRESTRICTED = "unrestricted"


@dataclass(frozen=True)
class BatchPolicy:
    continue_on_failure: bool = True
    work_item_max_attempts: int = 2
    max_steps_per_item: int = 20
    max_pages_per_item: int = 5
    max_seconds_per_item: float = 120.0
    max_total_items: Optional[int] = None
    max_total_model_calls: Optional[int] = None
    max_total_seconds: Optional[float] = None
    lease_seconds: int = 900
    worker_id: str = "local"
    read_only: bool = True
    navigation_scope: NavigationScope = NavigationScope.SAME_ORIGIN
    session_mode: SessionMode = SessionMode.ISOLATED


@dataclass(frozen=True)
class ResultContract:
    name: str = "generic"
    description: str = "Return relevant findings with concise evidence and source URLs."
    required_fields: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class WorkTarget:
    raw: str
    normalized: str
    payload: dict[str, Any]

