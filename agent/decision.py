"""Turns raw model output into a validated ModelDecision, or refuses to.

Defense in depth: GBNF (inference layer) -> json.loads -> Pydantic (agent/schemas.py) ->
semantic validation (this module, against the *current* PageObservation). Nothing past this
module ever sees a decision that hasn't cleared all four layers. A stale target id, a
type/role mismatch, or a malformed URL is caught here and turned into a DecisionValidationError
that the recovery state machine treats as a normal (if disappointing) model mistake — never
as something that reaches Playwright.
"""
from __future__ import annotations

import json
from typing import Optional

from pydantic import TypeAdapter, ValidationError

from agent.schemas import (
    ActionType,
    BackAction,
    ClickAction,
    DecisionValidationError,
    DownloadAction,
    ExtractAction,
    FinishAction,
    ModelDecision,
    ModelAction,
    OpenUrlAction,
    ScrollAction,
    SelectAction,
    TARGETED_ACTIONS,
    TypeAction,
    UNTARGETED_ACTIONS,
    ValidationErrorKind,
    WaitAction,
)
from browser.page_model import PageObservation

_TYPE_TO_ROLE = {
    ActionType.TYPE: {"textbox"},
    ActionType.SELECT: {"select", "combobox"},
}

_ACTION_ADAPTER = TypeAdapter(ModelAction)


def parse_model_output(raw_text: str) -> ModelDecision:
    """Layers 2-3: JSON parse, then Pydantic schema. Raises DecisionValidationError for
    both — the caller does not need to distinguish "bad JSON" from "bad schema" to decide
    what to do next (both mean: don't execute, tell the recovery state machine)."""
    raw_text = raw_text.strip()
    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as e:
        raise DecisionValidationError(ValidationErrorKind.MALFORMED_JSON, str(e)) from e

    try:
        if isinstance(data, dict) and "params" in data:
            return ModelDecision.model_validate(data)
        action = _ACTION_ADAPTER.validate_python(data)
        return normalize_model_action(action)
    except ValidationError as e:
        raise DecisionValidationError(ValidationErrorKind.SCHEMA_INVALID, str(e)) from e


def normalize_model_action(action: ModelAction) -> ModelDecision:
    """Convert the action-specific model I/O contract into the executor contract."""
    if isinstance(action, OpenUrlAction):
        return ModelDecision(action=ActionType.OPEN_URL, params={"url": action.url}, verification_mode="action_default")
    if isinstance(action, ClickAction):
        return ModelDecision(action=ActionType.CLICK, target=action.target, verification_mode="action_default")
    if isinstance(action, TypeAction):
        return ModelDecision(action=ActionType.TYPE, target=action.target, params={"text": action.text}, verification_mode="action_default")
    if isinstance(action, SelectAction):
        return ModelDecision(action=ActionType.SELECT, target=action.target, params={"value": action.value}, verification_mode="action_default")
    if isinstance(action, ScrollAction):
        return ModelDecision(action=ActionType.SCROLL, params={"direction": action.direction}, verification_mode="action_default")
    if isinstance(action, BackAction):
        return ModelDecision(action=ActionType.BACK, verification_mode="action_default")
    if isinstance(action, ExtractAction):
        return ModelDecision(action=ActionType.EXTRACT, target=action.target, verification_mode="action_default")
    if isinstance(action, DownloadAction):
        return ModelDecision(action=ActionType.DOWNLOAD, target=action.target, verification_mode="action_default")
    if isinstance(action, WaitAction):
        params = {
            k: v for k, v in {
                "for_text": action.for_text,
                "url_contains": action.url_contains,
                "ms": action.ms,
            }.items() if v is not None
        }
        return ModelDecision(action=ActionType.WAIT, params=params, verification_mode="action_default")
    if isinstance(action, FinishAction):
        return ModelDecision(action=ActionType.FINISH, params={"result": action.result}, verification_mode="action_default")
    raise DecisionValidationError(ValidationErrorKind.SCHEMA_INVALID, f"unsupported action: {action!r}")


def validate_against_observation(decision: ModelDecision, observation: PageObservation) -> None:
    """Layer 4: semantic validation against what the browser actually shows right now."""
    action = decision.action

    if action in UNTARGETED_ACTIONS:
        if decision.target is not None:
            raise DecisionValidationError(
                ValidationErrorKind.UNEXPECTED_TARGET,
                f"action '{action.value}' does not take a target, got {decision.target}",
            )
    elif action in TARGETED_ACTIONS:
        if decision.target is None:
            raise DecisionValidationError(
                ValidationErrorKind.MISSING_TARGET, f"action '{action.value}' requires a target",
            )
        element = observation.element_by_id(decision.target)
        if element is None:
            raise DecisionValidationError(
                ValidationErrorKind.STALE_TARGET,
                f"target id {decision.target} is not present in the current observation",
            )
        allowed_roles = _TYPE_TO_ROLE.get(action)
        if allowed_roles is not None and element.role not in allowed_roles:
            raise DecisionValidationError(
                ValidationErrorKind.TARGET_TYPE_MISMATCH,
                f"action '{action.value}' requires role in {allowed_roles}, target {decision.target} has role '{element.role}'",
            )
        if element.disabled:
            raise DecisionValidationError(
                ValidationErrorKind.TARGET_TYPE_MISMATCH,
                f"target {decision.target} ('{element.name}') is disabled",
            )
        if action == ActionType.CLICK and _looks_like_download(element):
            raise DecisionValidationError(
                ValidationErrorKind.MODEL_INTENT_ERROR,
                f"target {decision.target} ('{element.name}') appears to be a download; use action 'download'",
            )
        if action == ActionType.TYPE and element.value == decision.params.get("text"):
            raise DecisionValidationError(
                ValidationErrorKind.MODEL_INTENT_ERROR,
                f"target {decision.target} ('{element.name}') already contains the requested text; choose the next action",
            )
        if action == ActionType.SELECT:
            value = decision.params.get("value")
            if not value or (element.options is not None and value not in element.options):
                raise DecisionValidationError(
                    ValidationErrorKind.INVALID_OPTION,
                    f"'{value}' is not one of the available options for target {decision.target}: {element.options}",
                )

    if action == ActionType.OPEN_URL:
        url = decision.params.get("url", "")
        if not (isinstance(url, str) and (url.startswith("http://") or url.startswith("https://") or url.startswith("/"))):
            raise DecisionValidationError(ValidationErrorKind.INVALID_URL, f"invalid url: {url!r}")

    if action == ActionType.TYPE:
        if not isinstance(decision.params.get("text"), str):
            raise DecisionValidationError(
                ValidationErrorKind.SCHEMA_INVALID, "action 'type' requires a string params.text",
            )


def _looks_like_download(element) -> bool:
    name = (element.name or "").lower()
    href = (element.href or "").lower()
    if "download" in name:
        return True
    return href.endswith((
        ".csv", ".json", ".pdf", ".txt", ".zip", ".gz", ".tar", ".xlsx", ".docx", ".pptx",
        ".png", ".jpg", ".jpeg",
    ))
