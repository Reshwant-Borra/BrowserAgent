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

from pydantic import ValidationError

from agent.schemas import (
    ActionType,
    DecisionValidationError,
    ModelDecision,
    TARGETED_ACTIONS,
    UNTARGETED_ACTIONS,
    ValidationErrorKind,
)
from browser.page_model import PageObservation

_TYPE_TO_ROLE = {
    ActionType.TYPE: {"textbox"},
    ActionType.SELECT: {"select", "combobox"},
}


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
        return ModelDecision.model_validate(data)
    except ValidationError as e:
        raise DecisionValidationError(ValidationErrorKind.SCHEMA_INVALID, str(e)) from e


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
