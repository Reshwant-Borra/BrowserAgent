"""Recovery state machine (ARCHITECTURE.md §12) and idempotency policy (§14, Cases A/B/C).

Two independent decisions live here:
1. `next_recovery_level` — how much more context/reasoning budget the *next* model call gets.
2. `idempotency_decision` — whether a failed action may be safely retried automatically at all.
They are deliberately separate: escalating recovery level changes what the model sees; the
idempotency policy changes whether we let the model act again before a human looks at it.
"""
from __future__ import annotations

from enum import Enum

from agent.schemas import RecoveryLevel, RiskLevel

_LADDER = [
    RecoveryLevel.NORMAL,
    RecoveryLevel.RETRY,
    RecoveryLevel.REFRESH_STATE,
    RecoveryLevel.DEEP_RECOVERY,
    RecoveryLevel.REPLAN_REQUIRED,
    RecoveryLevel.USER_REQUIRED,
]


def _step_up(current: RecoveryLevel) -> RecoveryLevel:
    idx = _LADDER.index(current)
    return _LADDER[min(idx + 1, len(_LADDER) - 1)]


def next_recovery_level(current: RecoveryLevel, verification_passed: bool, loop_detected: bool,
                         retry_count: int, max_retries: int) -> RecoveryLevel:
    if verification_passed and not loop_detected:
        return RecoveryLevel.NORMAL

    if loop_detected:
        # A detected loop means simple retry already isn't working — skip straight past it.
        if current in (RecoveryLevel.NORMAL, RecoveryLevel.RETRY):
            return RecoveryLevel.REFRESH_STATE
        return _step_up(current)

    # Verification failed, no loop detected yet.
    if current == RecoveryLevel.NORMAL:
        return RecoveryLevel.RETRY
    if current == RecoveryLevel.RETRY:
        return RecoveryLevel.RETRY if retry_count < max_retries else RecoveryLevel.REFRESH_STATE
    return _step_up(current)


class RetryDecision(str, Enum):
    SAFE_RETRY = "safe_retry"
    ESCALATE_NO_RETRY = "escalate_no_retry"
    NEVER_RETRY_CONSEQUENTIAL = "never_retry_consequential"


def idempotency_decision(risk: RiskLevel, state_changed: bool, verification_passed: bool) -> RetryDecision:
    """Called only when verification_passed is False (nothing to decide otherwise).

    Case A: risk is not consequential, and the page state did not change -> safe to retry,
            the action plausibly never took effect.
    Case B: the page state *did* change but verification still failed -> the action had
            *some* effect we didn't expect; do not blindly repeat it, escalate instead.
    Case C: risk is CONSEQUENTIAL -> never automatic, regardless of A/B, full stop.
    """
    if risk == RiskLevel.CONSEQUENTIAL:
        return RetryDecision.NEVER_RETRY_CONSEQUENTIAL
    if not state_changed:
        return RetryDecision.SAFE_RETRY
    return RetryDecision.ESCALATE_NO_RETRY


def requires_approval(risk: RiskLevel, interactive_approval_enabled: bool) -> bool:
    return risk == RiskLevel.CONSEQUENTIAL and interactive_approval_enabled
