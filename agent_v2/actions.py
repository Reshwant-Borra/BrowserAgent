"""V2 action contract: what the model is allowed to say, and what it means.

Three layers, in order, every step:

1. `DECISION_JSON_SCHEMA` — handed to Ollama as `format`, so constrained decoding can only
   emit this shape. Deliberately a *flat* object with an `action` enum rather than a
   discriminated union of ten object shapes: an 8B model under grammar constraint picks a
   single enum token far more reliably than it navigates a ten-way `anyOf`, and the flat
   shape is exactly what the V2 spec's own example schema looks like.
2. `RawDecision` — Pydantic parse of that JSON (defense against a backend that ignores
   `format`, e.g. llama.cpp without a grammar).
3. `validate_decision()` — semantic validation against the *current* observation: does the
   target exist, is its role compatible with the action, is the URL well-formed, is this a
   password field we must never type into. Only output of this layer ever reaches the browser.

No action here knows about any specific website.
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Optional
from urllib.parse import urlsplit

from pydantic import BaseModel, Field

from browser.page_model import PageObservation

REASON_MAX_CHARS = 160
TEXT_MAX_CHARS = 400
ANSWER_MAX_CHARS = 4000


class V2Action(str, Enum):
    OPEN_URL = "open_url"
    CLICK = "click"
    TYPE = "type"
    SELECT = "select"
    SCROLL = "scroll"
    BACK = "back"
    EXTRACT = "extract"
    OPEN_TAB = "open_tab"
    SWITCH_TAB = "switch_tab"
    CLOSE_AGENT_CREATED_TAB = "close_agent_created_tab"
    WAIT = "wait"
    FINISH = "finish"
    NEED_USER = "need_user"


#: Actions that address an element id in the current observation.
TARGETED = {V2Action.CLICK, V2Action.TYPE, V2Action.SELECT}
#: Actions that never change the page and therefore never need verification beyond re-observing.
READ_ONLY = {V2Action.EXTRACT, V2Action.SCROLL, V2Action.WAIT, V2Action.FINISH, V2Action.NEED_USER}

_TEXT_INPUT_ROLES = {"textbox", "searchbox", "combobox", "spinbutton"}
_SELECTABLE_ROLES = {"select", "combobox", "listbox"}


class StateUpdates(BaseModel):
    """The model's own edits to the compact task state (V2 spec §10/§19: the plan lives as
    data inside task state, not as a separate planner agent)."""

    add_facts: list[str] = Field(default_factory=list)
    completed: list[str] = Field(default_factory=list)
    pending: list[str] = Field(default_factory=list)


class RawDecision(BaseModel):
    """Exactly the JSON the model is asked for. Everything but `action` is optional so the
    grammar never forces the model to invent a value for a field its action doesn't use."""

    action: V2Action
    target: Optional[int] = None
    text: Optional[str] = None
    url: Optional[str] = None
    value: Optional[str] = None
    direction: Optional[str] = None
    submit: Optional[bool] = None
    tab_id: Optional[int] = None
    ms: Optional[int] = None
    answer: Optional[str] = None
    message: Optional[str] = None
    expect: Optional[str] = None
    reason: Optional[str] = None
    state_updates: Optional[StateUpdates] = None


class Decision(BaseModel):
    """A validated, executable action bound to the observation it was decided against."""

    action: V2Action
    target: Optional[int] = None
    text: Optional[str] = None
    url: Optional[str] = None
    value: Optional[str] = None
    direction: str = "down"
    submit: bool = False
    tab_id: Optional[int] = None
    ms: Optional[int] = None
    answer: Optional[str] = None
    message: Optional[str] = None
    expect: Optional[str] = None
    reason: str = ""
    state_updates: StateUpdates = Field(default_factory=StateUpdates)
    #: Accessible name of the targeted element, captured at validation time — used for risk
    #: classification, loop signatures and logs so none of those re-resolve a stale id later.
    target_name: Optional[str] = None
    target_role: Optional[str] = None

    def signature(self) -> str:
        """Stable "same meaning" key across re-observations (element ids are positional and
        get reassigned every observation, so they must not appear here)."""
        name = _normalize(self.target_name or "")
        detail = ""
        if self.action is V2Action.TYPE:
            detail = _normalize(self.text or "")
        elif self.action is V2Action.SELECT:
            detail = _normalize(self.value or "")
        elif self.action is V2Action.OPEN_URL:
            detail = _normalize_url(self.url or "")
        elif self.action is V2Action.SCROLL:
            detail = self.direction
        return f"{self.action.value}:{name}:{detail}"


class DecisionError(Exception):
    """A model decision that cannot be executed. `kind` is fed back to the model verbatim on
    the next turn so it can correct itself (V2 spec §18/§21) rather than silently retrying."""

    def __init__(self, kind: str, message: str):
        self.kind = kind
        self.message = message
        super().__init__(f"{kind}: {message}")


def decision_json_schema(exclude: Optional[set["V2Action"]] = None) -> dict[str, Any]:
    """JSON Schema handed to Ollama's `format`. Kept hand-written rather than generated from
    `RawDecision` so the property descriptions stay short — every character here is decoded
    into the grammar and paid for on every single step.

    `exclude` removes actions from the enum for a single call. Constrained decoding then
    makes the excluded action literally unemittable, which is the only reliable way to stop
    a small model that has decided to do something the loop has already refused: asking it
    in prose not to do that again is advice, removing the token is a guarantee.
    """
    actions = [a.value for a in V2Action if not exclude or a not in exclude]
    return {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": actions},
            "target": {"type": ["integer", "null"]},
            "text": {"type": ["string", "null"]},
            "url": {"type": ["string", "null"]},
            "value": {"type": ["string", "null"]},
            "direction": {"type": ["string", "null"], "enum": ["up", "down", None]},
            "submit": {"type": ["boolean", "null"]},
            "tab_id": {"type": ["integer", "null"]},
            "ms": {"type": ["integer", "null"]},
            "answer": {"type": ["string", "null"]},
            "message": {"type": ["string", "null"]},
            "expect": {"type": ["string", "null"]},
            "reason": {"type": ["string", "null"]},
            "state_updates": {
                "type": ["object", "null"],
                "properties": {
                    "add_facts": {"type": "array", "items": {"type": "string"}},
                    "completed": {"type": "array", "items": {"type": "string"}},
                    "pending": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
        "required": ["action", "reason"],
    }


def plan_json_schema() -> dict[str, Any]:
    """First-turn variant that *requires* a non-empty `pending` list.

    Plan-as-data (V2 spec §19) only works if a plan actually gets written, and asking an 8B
    model in prose to "list the parts of the goal first" reliably produces no list at all —
    it dives at the first page and then declares victory halfway through a two-part task.
    Requiring the field in the schema makes the grammar unable to emit a decision without
    one. It is applied on the first turn only: after that the model revises its own plan
    freely, which is the exploratory behaviour the spec asks for.
    """
    schema = decision_json_schema()
    schema["properties"]["state_updates"] = {
        "type": "object",
        "properties": {
            "add_facts": {"type": "array", "items": {"type": "string"}},
            "completed": {"type": "array", "items": {"type": "string"}},
            "pending": {"type": "array", "items": {"type": "string"}, "minItems": 1},
        },
        "required": ["pending"],
    }
    schema["required"] = ["action", "reason", "state_updates"]
    return schema


def validate_decision(raw: RawDecision, obs: PageObservation) -> Decision:
    """Bind a parsed decision to the current observation, or raise `DecisionError`.

    Every rule here is generic. There is deliberately no branch anywhere in this function
    that inspects the *identity* of a site — only element roles, URL schemes, and the
    sensitivity flag the observer already sets on password inputs.
    """
    action = raw.action
    decision = Decision(
        action=action,
        reason=(raw.reason or "")[:REASON_MAX_CHARS],
        expect=(raw.expect or None),
        state_updates=raw.state_updates or StateUpdates(),
    )

    if action in TARGETED:
        if raw.target is None:
            raise DecisionError("missing_target", f"{action.value} requires a target element id")
        element = obs.element_by_id(raw.target)
        if element is None:
            valid = ", ".join(str(e.id) for e in obs.elements[:40]) or "none"
            raise DecisionError(
                "stale_target",
                f"element id {raw.target} is not on the current page. Valid ids: {valid}",
            )
        if element.disabled:
            raise DecisionError("disabled_target", f'element [{raw.target}] "{element.name}" is disabled')
        decision.target = raw.target
        decision.target_name = element.name
        decision.target_role = element.role

        if action is V2Action.TYPE:
            if element.sensitive:
                # Hard safety rail (V2 spec §7/§29). The model is never permitted to author a
                # value for a credential field, whatever it thinks it knows — this raises
                # rather than typing, and the loop converts it into a human-takeover pause.
                raise DecisionError(
                    "sensitive_field",
                    f'element [{raw.target}] "{element.name}" is a password field. '
                    "Use need_user and let the human type it.",
                )
            if element.role not in _TEXT_INPUT_ROLES:
                raise DecisionError(
                    "target_type_mismatch",
                    f'cannot type into [{raw.target}] which is a {element.role}, not a text field',
                )
            if raw.text is None:
                raise DecisionError("missing_text", "type requires `text`")
            decision.text = raw.text[:TEXT_MAX_CHARS]
            decision.submit = bool(raw.submit)

        if action is V2Action.SELECT:
            if element.role not in _SELECTABLE_ROLES:
                raise DecisionError(
                    "target_type_mismatch",
                    f'cannot select on [{raw.target}] which is a {element.role}',
                )
            if not raw.value:
                raise DecisionError("missing_value", "select requires `value`")
            if element.options and raw.value not in element.options:
                match = _closest_option(raw.value, element.options)
                if match is None:
                    raise DecisionError(
                        "invalid_option",
                        f'"{raw.value}" is not an option of [{raw.target}]. Options: {element.options[:20]}',
                    )
                decision.value = match
            else:
                decision.value = raw.value
    elif raw.target is not None and action is V2Action.EXTRACT:
        element = obs.element_by_id(raw.target)
        if element is None:
            raise DecisionError("stale_target", f"element id {raw.target} is not on the current page")
        decision.target = raw.target
        decision.target_name = element.name
        decision.target_role = element.role

    if action in (V2Action.OPEN_URL, V2Action.OPEN_TAB):
        url = (raw.url or "").strip()
        if not url:
            raise DecisionError("missing_url", f"{action.value} requires `url`")
        decision.url = normalize_url(url, obs.url)

    if action is V2Action.SWITCH_TAB:
        if raw.tab_id is None:
            raise DecisionError("missing_tab", "switch_tab requires `tab_id` from the TABS list")
        decision.tab_id = raw.tab_id

    if action is V2Action.CLOSE_AGENT_CREATED_TAB:
        if raw.tab_id is None:
            raise DecisionError("missing_tab", "close_agent_created_tab requires `tab_id`")
        decision.tab_id = raw.tab_id

    if action is V2Action.SCROLL:
        direction = (raw.direction or "down").strip().lower()
        if direction not in ("up", "down"):
            raise DecisionError("invalid_direction", "scroll direction must be 'up' or 'down'")
        decision.direction = direction

    if action is V2Action.WAIT:
        decision.ms = max(200, min(int(raw.ms or 1000), 3000))
        decision.text = raw.text  # optional "wait until this text appears"

    if action is V2Action.FINISH:
        answer = (raw.answer or raw.text or "").strip()
        if not answer:
            raise DecisionError(
                "missing_answer",
                "finish requires `answer` containing the actual result for the user",
            )
        decision.answer = answer[:ANSWER_MAX_CHARS]

    if action is V2Action.NEED_USER:
        decision.message = (raw.message or raw.reason or "Your input is needed in the browser.")[:REASON_MAX_CHARS * 2]

    return decision


def normalize_url(url: str, base_url: str) -> str:
    """http(s) absolute, or a same-origin relative path resolved against the current page.

    Anything else — file:, javascript:, data:, chrome: — is refused. This is the V2 loop's
    only URL gate and it is scheme-based, never host-based.
    """
    lowered = url.lower()
    if lowered.startswith(("http://", "https://")):
        return url
    if lowered.startswith(("javascript:", "data:", "file:", "chrome:", "about:", "vbscript:")):
        raise DecisionError("invalid_url", f"refusing non-web URL scheme: {url[:60]}")
    if url.startswith("/"):
        parts = urlsplit(base_url)
        if parts.scheme in ("http", "https") and parts.netloc:
            return f"{parts.scheme}://{parts.netloc}{url}"
        raise DecisionError("invalid_url", f"cannot resolve relative URL {url!r} from {base_url!r}")
    if "." in url.split("/")[0] and " " not in url:
        return f"https://{url}"  # bare host like "example.com"
    raise DecisionError("invalid_url", f"not a usable URL: {url[:60]}")


def _closest_option(value: str, options: list[str]) -> Optional[str]:
    """Case/whitespace-insensitive option match. Real <select> labels routinely carry padding
    or differing case from what a model reproduces; failing those outright would be a
    grounding failure, not a genuine invalid choice."""
    wanted = _normalize(value)
    for option in options:
        if _normalize(option) == wanted:
            return option
    for option in options:
        if wanted and wanted in _normalize(option):
            return option
    return None


def _normalize(text: str) -> str:
    return " ".join("".join(c.lower() if c.isalnum() else " " for c in text).split())


def _normalize_url(url: str) -> str:
    parts = urlsplit(url.strip())
    return f"{parts.netloc.lower()}{parts.path.rstrip('/')}"
