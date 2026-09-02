"""Phase 5 zero-trust security policy checks (BrowserAgent_General_Autonomous_Agent_
Architecture_REVISED.pdf, section 12: "Security Architecture: Zero Trust for Page Content").

Deterministic, config-driven controls that constrain what a compromised or misled planner can
do — independent of, and never a replacement for, `agent/schemas.py::classify_risk`'s own
approval gate. Both mechanisms apply together; neither is "the" defense (section 12's own
warning: "a safer agent that completes 0% of tasks is not useful; a capable agent that
violates policy is not shippable" — this module adds bounded, structural checks rather than
relying on the model's own compliance with prompt wording).
"""
from __future__ import annotations

import re
from typing import Optional

from agent.config import SecurityConfig
from agent.runtime_policy import RuntimePolicyViolation
from agent.schemas import ModelDecision, RiskLevel, classify_risk
from browser.page_model import ElementRef

# Common credential/secret vocabulary — generic, not site-specific (same keyword-heuristic
# style already used by agent/schemas.py's own _CONSEQUENTIAL_KEYWORDS).
_SENSITIVE_KEY_KEYWORDS = (
    "password", "passwd", "pwd", "secret", "token", "api_key", "apikey", "credential",
    "credit_card", "card_number", "cvv", "cvc", "ssn", "social_security", "pin",
    "private_key", "auth", "otp",
)


def is_sensitive_fact_key(key: str) -> bool:
    """Generic keyword heuristic (never a per-site special case) for the cross-origin
    sensitive-transfer gate (workflow/orchestrator.py). Matches on normalized substrings, so
    "current_password", "API-Key", and "creditCardNumber" all match without a combinatorial
    keyword list."""
    normalized = re.sub(r"[\s\-]+", "_", (key or "").strip().lower())
    return any(kw in normalized for kw in _SENSITIVE_KEY_KEYWORDS)


def domain_permission_violation(
    security_config: SecurityConfig,
    decision: ModelDecision,
    element: Optional[ElementRef],
    current_url: Optional[str],
) -> Optional[RuntimePolicyViolation]:
    """`security.default_domain_permission` (section 17's illustrative config) — currently a
    single global default, since no per-domain override table exists yet (section 15: "Add
    per-task/domain Browser Control / Read Only / No Access policy" — the table itself is
    future work; this is the enforcement point it will plug into). "browser_control" (the
    default, unchanged from every existing caller) is a deliberate no-op — identical to
    today's behavior for every pre-Phase-5 test. "read_only" blocks CONSEQUENTIAL actions only,
    matching `agent/runtime_policy.py::BatchRuntimePolicy.read_only`'s own existing precedent
    (one consistent meaning of "read only" across the codebase, rather than a second, stricter
    definition living here). "no_access" blocks every action outright."""
    permission = security_config.default_domain_permission
    if permission == "no_access":
        return RuntimePolicyViolation(
            category="DOMAIN_NO_ACCESS_BLOCKED",
            reason=f"domain permission is no_access; blocked {decision.action.value} at {current_url or '(unknown)'}",
        )
    if permission == "read_only":
        risk = classify_risk(decision.action, element.name if element else None)
        if risk == RiskLevel.CONSEQUENTIAL:
            return RuntimePolicyViolation(
                category="DOMAIN_READ_ONLY_BLOCKED",
                reason=f"domain permission is read_only; blocked consequential action "
                       f"{decision.action.value} on {element.name if element else None!r}",
            )
        return None
    return None  # "browser_control" (default) or any unrecognized value: no extra restriction
