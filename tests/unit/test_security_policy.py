from __future__ import annotations

from agent.config import SecurityConfig
from agent.schemas import ActionType, ModelDecision
from agent.security_policy import domain_permission_violation, is_sensitive_fact_key
from browser.page_model import ElementRef, SelectorHint


def _element(name: str) -> ElementRef:
    return ElementRef(id=1, role="button", name=name, selector_hint=SelectorHint(css="button", nth=0))


def test_is_sensitive_fact_key_matches_common_credential_vocabulary():
    for key in ("password", "current_password", "API-Key", "apiKey", "credit_card_number",
                "SSN", "auth_token", "cvv", "pin", "Secret"):
        assert is_sensitive_fact_key(key), key


def test_is_sensitive_fact_key_rejects_ordinary_facts():
    for key in ("price_usd", "course", "due_date", "rating", "project_code", "status"):
        assert not is_sensitive_fact_key(key), key


def test_domain_permission_browser_control_is_a_no_op():
    config = SecurityConfig(default_domain_permission="browser_control")
    decision = ModelDecision(action=ActionType.CLICK, target=1, params={})
    assert domain_permission_violation(config, decision, _element("Submit Order"), "http://x.test") is None
    assert domain_permission_violation(config, ModelDecision(action=ActionType.EXTRACT, target=None, params={}),
                                        None, "http://x.test") is None


def test_domain_permission_no_access_blocks_every_action():
    config = SecurityConfig(default_domain_permission="no_access")
    read_decision = ModelDecision(action=ActionType.EXTRACT, target=None, params={})
    violation = domain_permission_violation(config, read_decision, None, "http://x.test")
    assert violation is not None
    assert violation.category == "DOMAIN_NO_ACCESS_BLOCKED"


def test_domain_permission_read_only_blocks_only_consequential_actions():
    config = SecurityConfig(default_domain_permission="read_only")
    consequential = ModelDecision(action=ActionType.CLICK, target=1, params={})
    violation = domain_permission_violation(config, consequential, _element("Submit Order"), "http://x.test")
    assert violation is not None
    assert violation.category == "DOMAIN_READ_ONLY_BLOCKED"

    low_risk = ModelDecision(action=ActionType.CLICK, target=1, params={})
    assert domain_permission_violation(config, low_risk, _element("Next page"), "http://x.test") is None

    read_only_action = ModelDecision(action=ActionType.EXTRACT, target=None, params={})
    assert domain_permission_violation(config, read_only_action, None, "http://x.test") is None
