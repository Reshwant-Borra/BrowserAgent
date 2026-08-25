from __future__ import annotations

import json

import pytest

from agent.config import load_config
from agent.context_builder import build_prompt
from agent.decision import parse_model_output, validate_against_observation
from agent.schemas import ActionType, DecisionValidationError, ModelDecision, ValidationErrorKind
from agent.verifier import check_action_default
from browser.page_model import ElementRef, PageObservation, SelectorHint
from inference.llama_client import active_model_endpoint
from memory.models import TaskRecord, TaskState


def obs(elements=None, texts=None) -> PageObservation:
    return PageObservation(
        url="http://example.test/page",
        title="Page",
        elements=elements or [],
        visible_text=texts or [],
        state_hash="h1",
    )


def el(id_: int, role: str, name: str = "Item", value: str | None = None, options=None) -> ElementRef:
    return ElementRef(
        id=id_,
        role=role,
        name=name,
        value=value,
        options=options,
        selector_hint=SelectorHint(css="x", nth=id_),
    )


def test_action_specific_open_url_forbids_target():
    with pytest.raises(DecisionValidationError) as exc:
        parse_model_output('{"action":"open_url","url":"http://example.test","target":1}')
    assert exc.value.kind == ValidationErrorKind.SCHEMA_INVALID


def test_action_specific_click_requires_integer_target():
    with pytest.raises(DecisionValidationError) as exc:
        parse_model_output('{"action":"click"}')
    assert exc.value.kind == ValidationErrorKind.SCHEMA_INVALID


def test_action_specific_type_normalizes_params_and_verifier_mode():
    decision = parse_model_output('{"action":"type","target":4,"text":"hello"}')
    assert decision.action == ActionType.TYPE
    assert decision.target == 4
    assert decision.params == {"text": "hello"}
    assert decision.verification_mode == "action_default"


def test_numeric_target_binding_rejects_stale_current_page_id():
    decision = parse_model_output('{"action":"click","target":22}')
    with pytest.raises(DecisionValidationError) as exc:
        validate_against_observation(decision, obs([el(1, "button")]))
    assert exc.value.kind == ValidationErrorKind.STALE_TARGET


def test_clicking_download_target_is_semantic_intent_error():
    decision = parse_model_output('{"action":"click","target":31}')
    with pytest.raises(DecisionValidationError) as exc:
        validate_against_observation(decision, obs([el(31, "link", "Download report.csv")]))
    assert exc.value.kind == ValidationErrorKind.MODEL_INTENT_ERROR


def test_retyping_existing_value_is_semantic_intent_error():
    decision = parse_model_output('{"action":"type","target":4,"text":"alpha"}')
    with pytest.raises(DecisionValidationError) as exc:
        validate_against_observation(decision, obs([el(4, "textbox", "Search", value="alpha")]))
    assert exc.value.kind == ValidationErrorKind.MODEL_INTENT_ERROR


def test_compact_observation_renders_current_non_sensitive_value():
    rendered = obs([el(4, "textbox", "Search", value="alpha")]).render_compact(3000, 12)
    assert '[4] textbox "Search" value="alpha"' in rendered


def test_prompt_contains_explicit_target_rule_and_finish_rule():
    prompt = build_prompt(
        TaskRecord(id="t", created_at="now", status="running", goal="Do task", success_criteria=["Done"]),
        TaskState(task_id="t"),
        obs([el(22, "select", "Mode", options=["Basic", "Advanced"])]),
        max_page_chars=3000,
        max_visible_text_items=12,
    )
    assert "Element IDs are the numbers in square brackets" in prompt
    assert "your target must be 22" in prompt
    assert "Return finish" in prompt


def test_action_default_type_verifier_checks_element_value():
    decision = ModelDecision(
        action=ActionType.TYPE,
        target=1,
        params={"text": "alpha"},
        verification_mode="action_default",
    )
    after = obs([el(1, "textbox", "Search", value="alpha")])
    assert check_action_default(decision, obs(), after).passed is True


def test_action_default_select_verifier_checks_selected_label():
    decision = ModelDecision(
        action=ActionType.SELECT,
        target=1,
        params={"value": "Advanced"},
        verification_mode="action_default",
    )
    after = obs([el(1, "select", "Mode", value="Advanced", options=["Basic", "Advanced"])])
    assert check_action_default(decision, obs(), after).passed is True


def test_backend_specific_endpoint_selection(monkeypatch):
    monkeypatch.setenv("BROWSER_AGENT_MODEL__BACKEND", "llama_cpp")
    monkeypatch.setenv("BROWSER_AGENT_MODEL__LLAMACPP_ENDPOINT", "http://127.0.0.1:9000")
    config = load_config()
    assert active_model_endpoint(config) == "http://127.0.0.1:9000"


def test_runtime_dir_rewrites_default_relative_paths(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(json.dumps({"storage": {"runtime_dir": str(tmp_path / "runtime")}}), encoding="utf-8")
    monkeypatch.delenv("BROWSER_AGENT_MODEL__BACKEND", raising=False)
    config = load_config(config_path)
    assert config.storage.tasks_dir == str(tmp_path / "runtime" / "tasks")
    assert config.browser.user_data_dir == str(tmp_path / "runtime" / "tasks")
    assert config.logging.dir == str(tmp_path / "runtime" / "logs")
