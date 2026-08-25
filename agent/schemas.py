"""Core typed contracts: model decision schema, expected-result assertions, verification
results, and the risk classifier. Nothing in this module talks to the browser or the model
server — it is the shape that both sides are validated against (defense-in-depth layer 3/4:
GBNF -> JSON parse -> this Pydantic schema -> semantic validation in agent/decision.py).
"""
from __future__ import annotations

from enum import Enum
from typing import Annotated, Any, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator

REASON_MAX_CHARS = 120


class ActionType(str, Enum):
    OPEN_URL = "open_url"
    CLICK = "click"
    TYPE = "type"
    SELECT = "select"
    SCROLL = "scroll"
    BACK = "back"
    EXTRACT = "extract"
    DOWNLOAD = "download"
    WAIT = "wait"
    FINISH = "finish"


# Actions that operate on a specific element id in the current observation.
TARGETED_ACTIONS = {ActionType.CLICK, ActionType.TYPE, ActionType.SELECT, ActionType.DOWNLOAD}
# Actions that never take a target.
UNTARGETED_ACTIONS = {ActionType.OPEN_URL, ActionType.BACK, ActionType.SCROLL, ActionType.WAIT, ActionType.FINISH}


class ExpectedResult(BaseModel):
    """At least one assertion should normally be present for any state-changing action.
    All fields are optional so read-only actions (scroll/extract/wait) can omit them."""

    url_contains: Optional[str] = None
    page_contains: Optional[str] = None
    element_present: Optional[str] = None
    element_absent: Optional[str] = None
    title_contains: Optional[str] = None

    def is_empty(self) -> bool:
        return not any([self.url_contains, self.page_contains, self.element_present,
                        self.element_absent, self.title_contains])


class ModelDecision(BaseModel):
    """Executor-facing normalized action.

    The model-facing contract is the discriminated union below. This legacy-normalized
    shape remains the internal transport used by the existing executor, verifier, and
    replay code.
    """

    action: ActionType
    target: Optional[int] = None
    params: dict[str, Any] = Field(default_factory=dict)
    expected_result: ExpectedResult = Field(default_factory=ExpectedResult)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    reason: Optional[str] = None
    verification_mode: Literal["legacy", "action_default"] = "legacy"

    @field_validator("reason", mode="before")
    @classmethod
    def _cap_reason(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        return v[:REASON_MAX_CHARS]


class _StrictAction(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OpenUrlAction(_StrictAction):
    action: Literal["open_url"]
    url: str


class ClickAction(_StrictAction):
    action: Literal["click"]
    target: int


class TypeAction(_StrictAction):
    action: Literal["type"]
    target: int
    text: str


class SelectAction(_StrictAction):
    action: Literal["select"]
    target: int
    value: str


class ScrollAction(_StrictAction):
    action: Literal["scroll"]
    direction: Literal["up", "down"] = "down"


class BackAction(_StrictAction):
    action: Literal["back"]


class ExtractAction(_StrictAction):
    action: Literal["extract"]
    target: Optional[int] = None


class DownloadAction(_StrictAction):
    action: Literal["download"]
    target: int


class WaitAction(_StrictAction):
    action: Literal["wait"]
    for_text: Optional[str] = None
    url_contains: Optional[str] = None
    ms: Optional[int] = Field(default=None, ge=0, le=3000)


class FinishAction(_StrictAction):
    action: Literal["finish"]
    result: str


ModelAction = Annotated[
    Union[
        OpenUrlAction,
        ClickAction,
        TypeAction,
        SelectAction,
        ScrollAction,
        BackAction,
        ExtractAction,
        DownloadAction,
        WaitAction,
        FinishAction,
    ],
    Field(discriminator="action"),
]


class ValidationErrorKind(str, Enum):
    MALFORMED_JSON = "malformed_json"
    SCHEMA_INVALID = "schema_invalid"
    MODEL_TARGET_BINDING_ERROR = "model_target_binding_error"
    MODEL_INTENT_ERROR = "model_intent_error"
    MODEL_PARAMETER_ERROR = "model_parameter_error"
    MODEL_COMPLETION_ERROR = "model_completion_error"
    STALE_TARGET = "stale_target"
    TARGET_TYPE_MISMATCH = "target_type_mismatch"
    MISSING_TARGET = "missing_target"
    UNEXPECTED_TARGET = "unexpected_target"
    INVALID_URL = "invalid_url"
    INVALID_OPTION = "invalid_option"


class DecisionValidationError(Exception):
    def __init__(self, kind: ValidationErrorKind, message: str):
        self.kind = kind
        self.message = message
        super().__init__(f"{kind.value}: {message}")


class CheckResult(BaseModel):
    type: str
    expected: str
    actual: str
    passed: bool


class VerificationResult(BaseModel):
    passed: bool
    checks: list[CheckResult] = Field(default_factory=list)


class RiskLevel(str, Enum):
    READ_ONLY = "read_only"
    LOW_RISK_WRITE = "low_risk_write"
    CONSEQUENTIAL = "consequential"


_CONSEQUENTIAL_KEYWORDS = (
    "submit", "buy", "purchase", "checkout", "pay", "confirm", "delete", "remove",
    "send", "publish", "unsubscribe", "cancel subscription", "change password",
    "transfer", "donate", "place order", "sign", "agree", "accept terms",
)

_READ_ONLY_ACTIONS = {ActionType.EXTRACT, ActionType.SCROLL, ActionType.BACK,
                       ActionType.WAIT, ActionType.FINISH, ActionType.OPEN_URL}


def classify_risk(action: ActionType, element_name: Optional[str] = None) -> RiskLevel:
    """Conservative, generic classifier — no per-site special-casing.

    Any targeted action whose element name matches a consequential-action keyword is
    treated as CONSEQUENTIAL regardless of action type, since a "click" on a button
    literally labeled "Submit Application" is the case this exists to catch.
    """
    name = (element_name or "").lower()
    if any(kw in name for kw in _CONSEQUENTIAL_KEYWORDS):
        return RiskLevel.CONSEQUENTIAL
    if action in _READ_ONLY_ACTIONS:
        return RiskLevel.READ_ONLY
    return RiskLevel.LOW_RISK_WRITE


class RecoveryLevel(str, Enum):
    NORMAL = "normal"
    RETRY = "retry"
    REFRESH_STATE = "refresh_state"
    DEEP_RECOVERY = "deep_recovery"
    REPLAN_REQUIRED = "replan_required"
    USER_REQUIRED = "user_required"
