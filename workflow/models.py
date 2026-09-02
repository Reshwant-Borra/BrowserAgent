from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from batch.models import NavigationScope


class WorkflowStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"


class WorkflowStepStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    VERIFICATION_FAILED = "verification_failed"
    FAILED = "failed"
    BLOCKED = "blocked"
    SKIPPED = "skipped"


class WorkflowEventType(str, Enum):
    WORKFLOW_CREATED = "WORKFLOW_CREATED"
    STEP_STARTED = "STEP_STARTED"
    STEP_VERIFIED = "STEP_VERIFIED"
    STEP_VERIFICATION_FAILED = "STEP_VERIFICATION_FAILED"
    STEP_RETRY_SCHEDULED = "STEP_RETRY_SCHEDULED"
    STEP_BLOCKED = "STEP_BLOCKED"
    STEP_COMPLETED = "STEP_COMPLETED"
    WORKFLOW_BLOCKED = "WORKFLOW_BLOCKED"
    WORKFLOW_COMPLETED = "WORKFLOW_COMPLETED"


@dataclass(frozen=True)
class WorkflowPolicy:
    """Ordered workflows exist specifically to perform reversible actions across sites
    (Section 15/18), so unlike BatchPolicy's read_only=True default, this defaults to
    allowing reversible (non-consequential) writes; consequential actions are still gated
    unconditionally by agent.schemas.classify_risk + the approval flow regardless of this."""

    max_attempts_per_step: int = 2
    max_steps_per_step: int = 20
    max_seconds_per_step: float = 120.0
    read_only: bool = False
    navigation_scope: NavigationScope = NavigationScope.SAME_ORIGIN
    worker_id: str = "local"
    # Phase 5 (architecture doc section 12: "Data-flow policy... never allow arbitrary 'read
    # from A, type into B' when data is marked sensitive"). Gates a verified fact whose key
    # looks like a credential (agent/security_policy.py::is_sensitive_fact_key) being seeded
    # into a later step on a DIFFERENT origin than the one it was discovered on — via the same
    # approval callback consequential actions already use, never a silent pass-through. True
    # by default; a caller with no approval_callback and this left True fails the step closed
    # (blocks) rather than transferring silently.
    cross_origin_sensitive_transfer_requires_approval: bool = True
