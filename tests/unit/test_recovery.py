from __future__ import annotations

from agent.recovery import RetryDecision, idempotency_decision, next_recovery_level, requires_approval
from agent.schemas import RecoveryLevel, RiskLevel


def test_recovery_normal_on_success():
    assert next_recovery_level(RecoveryLevel.RETRY, verification_passed=True, loop_detected=False,
                                retry_count=1, max_retries=2) == RecoveryLevel.NORMAL


def test_recovery_ladder_on_repeated_failure():
    level = RecoveryLevel.NORMAL
    level = next_recovery_level(level, verification_passed=False, loop_detected=False, retry_count=0, max_retries=2)
    assert level == RecoveryLevel.RETRY
    level = next_recovery_level(level, verification_passed=False, loop_detected=False, retry_count=1, max_retries=2)
    assert level == RecoveryLevel.RETRY  # under max_retries still
    level = next_recovery_level(level, verification_passed=False, loop_detected=False, retry_count=2, max_retries=2)
    assert level == RecoveryLevel.REFRESH_STATE
    level = next_recovery_level(level, verification_passed=False, loop_detected=False, retry_count=0, max_retries=2)
    assert level == RecoveryLevel.DEEP_RECOVERY
    level = next_recovery_level(level, verification_passed=False, loop_detected=False, retry_count=0, max_retries=2)
    assert level == RecoveryLevel.REPLAN_REQUIRED
    level = next_recovery_level(level, verification_passed=False, loop_detected=False, retry_count=0, max_retries=2)
    assert level == RecoveryLevel.USER_REQUIRED


def test_loop_skips_straight_to_refresh_state():
    level = next_recovery_level(RecoveryLevel.NORMAL, verification_passed=False, loop_detected=True,
                                 retry_count=0, max_retries=2)
    assert level == RecoveryLevel.REFRESH_STATE


def test_user_required_is_terminal():
    level = next_recovery_level(RecoveryLevel.USER_REQUIRED, verification_passed=False, loop_detected=True,
                                 retry_count=0, max_retries=2)
    assert level == RecoveryLevel.USER_REQUIRED


def test_idempotency_case_a_safe_retry():
    decision = idempotency_decision(RiskLevel.LOW_RISK_WRITE, state_changed=False, verification_passed=False)
    assert decision == RetryDecision.SAFE_RETRY


def test_idempotency_case_b_escalate():
    decision = idempotency_decision(RiskLevel.LOW_RISK_WRITE, state_changed=True, verification_passed=False)
    assert decision == RetryDecision.ESCALATE_NO_RETRY


def test_idempotency_case_c_never_retry_consequential():
    decision = idempotency_decision(RiskLevel.CONSEQUENTIAL, state_changed=False, verification_passed=False)
    assert decision == RetryDecision.NEVER_RETRY_CONSEQUENTIAL
    decision2 = idempotency_decision(RiskLevel.CONSEQUENTIAL, state_changed=True, verification_passed=False)
    assert decision2 == RetryDecision.NEVER_RETRY_CONSEQUENTIAL


def test_requires_approval():
    assert requires_approval(RiskLevel.CONSEQUENTIAL, True) is True
    assert requires_approval(RiskLevel.CONSEQUENTIAL, False) is False
    assert requires_approval(RiskLevel.LOW_RISK_WRITE, True) is False
