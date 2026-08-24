"""Schema parsing + semantic validation: the defense-in-depth layers that must never let
malformed or nonsensical model output reach Playwright."""
from __future__ import annotations

import pytest

from agent.decision import parse_model_output, validate_against_observation
from agent.schemas import DecisionValidationError, ValidationErrorKind
from browser.page_model import ElementRef, PageObservation, SelectorHint


def make_observation(elements=None) -> PageObservation:
    return PageObservation(
        url="http://example.test/page",
        title="Test Page",
        elements=elements or [],
        visible_text=["hello world"],
        state_hash="deadbeef",
    )


def el(id_, role, name="Item", disabled=False, options=None) -> ElementRef:
    return ElementRef(id=id_, role=role, name=name, disabled=disabled, options=options,
                       selector_hint=SelectorHint(css="a", nth=id_))


# ---- parse_model_output ---------------------------------------------------

def test_valid_action_parses():
    raw = '{"action": "click", "target": 1, "params": {}, "expected_result": {}, "confidence": 0.9}'
    decision = parse_model_output(raw)
    assert decision.action.value == "click"
    assert decision.target == 1


def test_malformed_json_raises():
    with pytest.raises(DecisionValidationError) as exc:
        parse_model_output("{not json")
    assert exc.value.kind == ValidationErrorKind.MALFORMED_JSON


def test_unsupported_action_raises():
    raw = '{"action": "delete_everything", "target": 1, "params": {}, "expected_result": {}, "confidence": 0.5}'
    with pytest.raises(DecisionValidationError) as exc:
        parse_model_output(raw)
    assert exc.value.kind == ValidationErrorKind.SCHEMA_INVALID


def test_bad_confidence_raises():
    raw = '{"action": "click", "target": 1, "params": {}, "expected_result": {}, "confidence": 1.7}'
    with pytest.raises(DecisionValidationError) as exc:
        parse_model_output(raw)
    assert exc.value.kind == ValidationErrorKind.SCHEMA_INVALID


def test_reason_is_capped():
    raw = ('{"action": "click", "target": 1, "params": {}, "expected_result": {}, '
           '"confidence": 0.9, "reason": "' + ("x" * 500) + '"}')
    decision = parse_model_output(raw)
    assert len(decision.reason) <= 120


# ---- validate_against_observation -----------------------------------------

def test_missing_target_for_click_raises():
    decision = parse_model_output('{"action": "click", "target": null, "params": {}, '
                                   '"expected_result": {}, "confidence": 0.5}')
    obs = make_observation([el(1, "button")])
    with pytest.raises(DecisionValidationError) as exc:
        validate_against_observation(decision, obs)
    assert exc.value.kind == ValidationErrorKind.MISSING_TARGET


def test_stale_target_raises():
    decision = parse_model_output('{"action": "click", "target": 99, "params": {}, '
                                   '"expected_result": {}, "confidence": 0.5}')
    obs = make_observation([el(1, "button")])
    with pytest.raises(DecisionValidationError) as exc:
        validate_against_observation(decision, obs)
    assert exc.value.kind == ValidationErrorKind.STALE_TARGET


def test_wrong_target_type_raises():
    decision = parse_model_output('{"action": "type", "target": 1, "params": {"text": "hi"}, '
                                   '"expected_result": {}, "confidence": 0.5}')
    obs = make_observation([el(1, "button")])  # type requires role "textbox"
    with pytest.raises(DecisionValidationError) as exc:
        validate_against_observation(decision, obs)
    assert exc.value.kind == ValidationErrorKind.TARGET_TYPE_MISMATCH


def test_unexpected_target_for_untargeted_action_raises():
    decision = parse_model_output('{"action": "back", "target": 1, "params": {}, '
                                   '"expected_result": {}, "confidence": 0.5}')
    obs = make_observation([el(1, "button")])
    with pytest.raises(DecisionValidationError) as exc:
        validate_against_observation(decision, obs)
    assert exc.value.kind == ValidationErrorKind.UNEXPECTED_TARGET


def test_disabled_target_raises():
    decision = parse_model_output('{"action": "click", "target": 1, "params": {}, '
                                   '"expected_result": {}, "confidence": 0.5}')
    obs = make_observation([el(1, "button", disabled=True)])
    with pytest.raises(DecisionValidationError):
        validate_against_observation(decision, obs)


def test_valid_click_passes():
    decision = parse_model_output('{"action": "click", "target": 1, "params": {}, '
                                   '"expected_result": {"page_contains": "ok"}, "confidence": 0.8}')
    obs = make_observation([el(1, "button")])
    validate_against_observation(decision, obs)  # should not raise


def test_invalid_select_option_raises():
    decision = parse_model_output('{"action": "select", "target": 1, "params": {"value": "Nope"}, '
                                   '"expected_result": {}, "confidence": 0.5}')
    obs = make_observation([el(1, "select", options=["A", "B"])])
    with pytest.raises(DecisionValidationError) as exc:
        validate_against_observation(decision, obs)
    assert exc.value.kind == ValidationErrorKind.INVALID_OPTION


def test_invalid_url_raises():
    decision = parse_model_output('{"action": "open_url", "target": null, "params": {"url": "not-a-url"}, '
                                   '"expected_result": {}, "confidence": 0.5}')
    obs = make_observation()
    with pytest.raises(DecisionValidationError) as exc:
        validate_against_observation(decision, obs)
    assert exc.value.kind == ValidationErrorKind.INVALID_URL
