"""Structured-action validation: nothing reaches the browser that hasn't been bound to the
observation it claims to act on."""
from __future__ import annotations

import pytest

from agent_v2.actions import (
    Decision,
    DecisionError,
    RawDecision,
    V2Action,
    decision_json_schema,
    normalize_url,
    validate_decision,
)
from browser.page_model import ElementRef, PageObservation, SelectorHint


def _element(id: int, role: str, name: str, **kwargs) -> ElementRef:
    return ElementRef(id=id, role=role, name=name,
                      selector_hint=SelectorHint(css="a", nth=id), **kwargs)


@pytest.fixture
def obs() -> PageObservation:
    return PageObservation(
        url="https://site.example/page",
        title="Page",
        elements=[
            _element(1, "link", "Home", href="/"),
            _element(2, "textbox", "Search"),
            _element(3, "button", "Go"),
            _element(4, "select", "Size", options=["Small", "Large"]),
            _element(5, "textbox", "Password", sensitive=True),
            _element(6, "button", "Unavailable", disabled=True),
        ],
        visible_text=["hello world"],
    )


def _raw(**kwargs) -> RawDecision:
    kwargs.setdefault("reason", "because")
    return RawDecision.model_validate(kwargs)


def test_click_binds_element_name_and_role(obs):
    decision = validate_decision(_raw(action="click", target=1), obs)
    assert decision.target_name == "Home" and decision.target_role == "link"


def test_target_not_on_page_is_rejected_with_the_valid_ids(obs):
    with pytest.raises(DecisionError) as exc:
        validate_decision(_raw(action="click", target=99), obs)
    assert exc.value.kind == "stale_target"
    assert "1" in exc.value.message  # the model is told what it may use instead


def test_disabled_element_is_rejected(obs):
    with pytest.raises(DecisionError) as exc:
        validate_decision(_raw(action="click", target=6), obs)
    assert exc.value.kind == "disabled_target"


def test_typing_into_a_password_field_is_refused(obs):
    with pytest.raises(DecisionError) as exc:
        validate_decision(_raw(action="type", target=5, text="hunter2"), obs)
    assert exc.value.kind == "sensitive_field"


def test_typing_into_a_button_is_refused(obs):
    with pytest.raises(DecisionError) as exc:
        validate_decision(_raw(action="type", target=3, text="x"), obs)
    assert exc.value.kind == "target_type_mismatch"


def test_type_carries_submit_flag(obs):
    decision = validate_decision(_raw(action="type", target=2, text="widgets", submit=True), obs)
    assert decision.text == "widgets" and decision.submit is True


def test_select_matches_option_case_insensitively(obs):
    decision = validate_decision(_raw(action="select", target=4, value="large"), obs)
    assert decision.value == "Large"  # the real label, not what the model typed


def test_select_rejects_an_option_that_does_not_exist(obs):
    with pytest.raises(DecisionError) as exc:
        validate_decision(_raw(action="select", target=4, value="Enormous"), obs)
    assert exc.value.kind == "invalid_option"


@pytest.mark.parametrize("url", ["javascript:alert(1)", "file:///etc/passwd", "data:text/html,x",
                                  "chrome://settings"])
def test_non_web_schemes_are_refused(obs, url):
    with pytest.raises(DecisionError):
        validate_decision(_raw(action="open_url", url=url), obs)


def test_relative_url_resolves_against_the_current_page(obs):
    decision = validate_decision(_raw(action="open_url", url="/other"), obs)
    assert decision.url == "https://site.example/other"


def test_bare_host_gets_https():
    assert normalize_url("example.com/x", "https://a.example") == "https://example.com/x"


def test_finish_requires_a_real_answer(obs):
    with pytest.raises(DecisionError) as exc:
        validate_decision(_raw(action="finish"), obs)
    assert exc.value.kind == "missing_answer"


def test_need_user_serializes_a_message(obs):
    decision = validate_decision(
        _raw(action="need_user", message="Please log in"), obs)
    assert decision.action is V2Action.NEED_USER and decision.message == "Please log in"


def test_need_user_falls_back_to_reason_when_no_message(obs):
    decision = validate_decision(_raw(action="need_user", reason="2FA code needed"), obs)
    assert "2FA" in decision.message


def test_switch_tab_requires_a_tab_id(obs):
    with pytest.raises(DecisionError) as exc:
        validate_decision(_raw(action="switch_tab"), obs)
    assert exc.value.kind == "missing_tab"


def test_wait_is_clamped(obs):
    assert validate_decision(_raw(action="wait", ms=99999), obs).ms == 3000
    assert validate_decision(_raw(action="wait", ms=1), obs).ms == 200


def test_state_updates_default_to_empty(obs):
    decision = validate_decision(_raw(action="scroll", direction="down"), obs)
    assert decision.state_updates.add_facts == []


def test_signature_is_stable_across_changing_element_ids():
    a = Decision(action=V2Action.CLICK, target=3, target_name="Next page")
    b = Decision(action=V2Action.CLICK, target=17, target_name="Next  page")
    assert a.signature() == b.signature()


def test_schema_lists_every_action():
    enum = decision_json_schema()["properties"]["action"]["enum"]
    assert set(enum) == {a.value for a in V2Action}
