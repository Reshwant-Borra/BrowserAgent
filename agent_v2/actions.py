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
from agent_v2.compute import MAX_OPERAND_CHARS, MAX_OPERANDS, ComputeOp
from agent_v2.grounding import Claim, ClaimKind

REASON_MAX_CHARS = 160
TEXT_MAX_CHARS = 400
ANSWER_MAX_CHARS = 4000
CLAIM_MAX_CHARS = 300
MAX_CLAIMS = 20
MAX_EVIDENCE_IDS = 12


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
    COMPUTE = "compute"
    FINISH = "finish"
    NEED_USER = "need_user"


#: Actions that address an element id in the current observation.
TARGETED = {V2Action.CLICK, V2Action.TYPE, V2Action.SELECT}
#: Actions that never change the page and therefore never need verification beyond re-observing.
READ_ONLY = {V2Action.EXTRACT, V2Action.SCROLL, V2Action.WAIT, V2Action.FINISH,
             V2Action.NEED_USER, V2Action.COMPUTE}
#: Actions that never touch the browser at all.
OFFLINE = {V2Action.COMPUTE}

_TEXT_INPUT_ROLES = {"textbox", "searchbox", "combobox", "spinbutton"}
_SELECTABLE_ROLES = {"select", "combobox", "listbox"}


class StateUpdates(BaseModel):
    """The model's own edits to the compact task state (V2 spec §10/§19: the plan lives as
    data inside task state, not as a separate planner agent)."""

    add_facts: list[str] = Field(default_factory=list)
    completed: list[str] = Field(default_factory=list)
    pending: list[str] = Field(default_factory=list)


class RawClaim(BaseModel):
    """One statement in the final answer, with the evidence the model says it rests on.

    `evidence_ids` are *selected*, never authored: the only ids that resolve are ones
    BrowserAgent minted and showed in this task's context (V2 hardening §4)."""

    text: str = ""
    evidence_ids: list[str] = Field(default_factory=list)
    kind: Optional[str] = None


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
    operation: Optional[str] = None
    operands: Optional[list[str]] = None
    labels: Optional[list[str]] = None
    evidence_ids: Optional[list[str]] = None
    claims: Optional[list[RawClaim]] = None


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
    #: `compute` only: a fixed operation from `agent_v2.compute` over literal operands. The
    #: model names the operation; BrowserAgent performs it (V2 hardening §9/§11).
    operation: Optional[str] = None
    operands: list[str] = Field(default_factory=list)
    labels: list[str] = Field(default_factory=list)
    #: `compute` only: the evidence the operands were read from, so the result keeps lineage.
    evidence_ids: list[str] = Field(default_factory=list)
    #: `finish` only: the claim -> evidence contract.
    claims: list[Claim] = Field(default_factory=list)
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
        elif self.action is V2Action.COMPUTE:
            detail = f"{self.operation}({','.join(self.operands)})"
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
            "operation": {"type": ["string", "null"],
                          "enum": [o.value for o in ComputeOp] + [None]},
            "operands": {"type": ["array", "null"], "items": {"type": "string"}},
            "labels": {"type": ["array", "null"], "items": {"type": "string"}},
            "evidence_ids": {"type": ["array", "null"], "items": {"type": "string"}},
            "claims": {
                "type": ["array", "null"],
                "items": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string"},
                        "evidence_ids": {"type": "array", "items": {"type": "string"}},
                        "kind": {"type": "string", "enum": list(ClaimKind.ALL)},
                    },
                    "required": ["text", "kind"],
                },
            },
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


def finish_json_schema() -> dict[str, Any]:
    """Forces a well-formed `finish`: the action is fixed and `answer` is required.

    `answer` cannot be conditionally required in a flat schema, so a model that has decided
    to stop can emit `{"action":"finish"}` with nothing in it — and then does so again on
    every retry, burning the budget arguing about a field. Re-asking once under a schema
    that cannot express the malformed version resolves it in a single call.
    """
    schema = decision_json_schema({a for a in V2Action if a is not V2Action.FINISH})
    schema["required"] = ["action", "answer", "reason"]
    return schema


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
            # Reading a whole page is a reasonable reading of "extract element 11" when there
            # is no element 11, and it is what the model wanted anyway. Rejecting it instead
            # produced a genuine stall on a plain-text document — a page with no interactive
            # elements at all, where the model asked for the same absent id on every one of
            # the remaining steps and the task ran out of budget arguing about it.
            decision.target = None
        else:
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

    if action is V2Action.COMPUTE:
        # Everything about a compute action is checked here, before anything is executed:
        # the operation must be one of a fixed set, the operands must be literals, and there
        # must not be too many of them. There is no path from model output to code
        # execution — `agent_v2.compute` only ever dispatches on this enum (V2 hardening §11).
        operation = (raw.operation or "").strip().lower()
        if not operation:
            raise DecisionError("missing_operation",
                                "compute requires `operation`, one of: "
                                + ", ".join(o.value for o in ComputeOp))
        if operation not in {o.value for o in ComputeOp}:
            raise DecisionError("invalid_operation",
                                f"'{operation}' is not a supported operation. Use one of: "
                                + ", ".join(o.value for o in ComputeOp))
        operands = [" ".join(str(o).split())[:MAX_OPERAND_CHARS] for o in (raw.operands or [])]
        if len(operands) > MAX_OPERANDS:
            raise DecisionError("too_many_operands",
                                f"compute takes at most {MAX_OPERANDS} operands, got {len(operands)}")
        decision.operation = operation
        decision.operands = operands
        decision.labels = [" ".join(str(l).split())[:80] for l in (raw.labels or [])][:MAX_OPERANDS]
        decision.evidence_ids = [str(e).strip()[:48] for e in (raw.evidence_ids or [])][:MAX_EVIDENCE_IDS]

    if action is V2Action.FINISH:
        answer = (raw.answer or raw.text or "").strip()
        if not answer:
            raise DecisionError(
                "missing_answer",
                "finish requires `answer` containing the actual result for the user",
            )
        decision.answer = answer[:ANSWER_MAX_CHARS]
        decision.claims = _claims(raw.claims)

    if action is V2Action.NEED_USER:
        decision.message = (raw.message or raw.reason or "Your input is needed in the browser.")[:REASON_MAX_CHARS * 2]

    return decision


def _claims(raw_claims: Optional[list[RawClaim]]) -> list[Claim]:
    """Bound and normalize the claim list. Nothing is rejected here — an unknown evidence id
    is not a malformed action, it is an ungrounded answer, and that is decided against the
    ledger in `agent_v2.grounding` where the answer as a whole is checked."""
    out: list[Claim] = []
    for raw in (raw_claims or [])[:MAX_CLAIMS]:
        text = " ".join(str(raw.text or "").split())[:CLAIM_MAX_CHARS]
        if not text:
            continue
        kind = (raw.kind or ClaimKind.SOURCE).strip().lower()
        out.append(Claim(
            text=text,
            evidence_ids=[str(e).strip()[:48] for e in (raw.evidence_ids or [])][:MAX_EVIDENCE_IDS],
            kind=kind if kind in ClaimKind.ALL else ClaimKind.SOURCE,
        ))
    return out


def finish_with_claims_schema() -> dict[str, Any]:
    """A `finish` that must carry its claim list.

    Used only when the loop has already rejected an ungrounded answer: at that point asking
    in prose for citations has demonstrably not worked, so the grammar stops being able to
    express a finish without them (the same device `plan_json_schema` uses for the plan)."""
    schema = finish_json_schema()
    schema["required"] = ["action", "answer", "reason", "claims"]
    claims = schema["properties"]["claims"]
    claims["type"] = "array"
    claims["minItems"] = 1
    return schema


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
