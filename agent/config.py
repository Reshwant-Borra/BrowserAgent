"""Configuration loading: YAML file + environment-variable overrides.

Every path/threshold used elsewhere in the codebase flows through this module so there
is exactly one place that resolves "what config is in effect" — no hard-coded machine
paths anywhere else.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, get_type_hints

import yaml

_ENV_PREFIX = "BROWSER_AGENT_"


@dataclass
class ModelConfig:
    backend: str = "ollama"
    endpoint: str = ""
    ollama_endpoint: str = "http://127.0.0.1:11434"
    llamacpp_endpoint: str = "http://127.0.0.1:8080"
    model_name: str = "qwen3:8b"
    temperature: float = 0.1
    context_window: int = 8192
    max_output_tokens: int = 256
    max_output_tokens_deep_recovery: int = 512
    request_timeout_s: float = 30.0
    request_connect_timeout_s: float = 10.0
    request_write_timeout_s: float = 10.0
    request_pool_timeout_s: float = 10.0
    max_inference_attempts: int = 2
    inference_retry_backoff_s: float = 0.5
    ollama_keep_alive: str = "5m"


@dataclass
class BrowserConfig:
    headless: bool = False
    user_data_dir: str = "./runtime/tasks"
    action_timeout_ms: int = 10000
    interactive_approval: bool = True
    mode: str = "launch"  # "launch" (default, tests/fixtures) or "cdp_attach" (everyday use)
    cdp_endpoint: str = "http://127.0.0.1:9222"  # only used when mode == "cdp_attach"


@dataclass
class ContextConfig:
    max_page_chars: int = 3000
    max_page_chars_deep_recovery: int = 6000
    recent_actions: int = 5
    max_visible_text_items: int = 12
    max_total_tokens: int = 4096
    recent_window_tokens: int = 800
    summary_tokens: int = 500
    retrieved_memory_tokens: int = 500
    page_tokens: int = 1400
    retrieved_memory_top_k: int = 5
    enable_running_summary: bool = True
    enable_memory_retrieval: bool = True
    enable_active_facts: bool = True
    active_fact_tokens: int = 300
    enforce_active_fact_constraints: bool = False
    summary_rebuild_interval: int = 4


@dataclass
class RecoveryConfig:
    max_action_retries: int = 2
    identical_action_limit: int = 3
    navigation_cycle_limit: int = 2
    verification_retry_limit: int = 2


@dataclass
class StorageConfig:
    runtime_dir: str = "./runtime"
    tasks_dir: str = "./runtime/tasks"


@dataclass
class LoggingConfig:
    level: str = "INFO"
    dir: str = "./runtime/logs"
    redact_secrets: bool = True


@dataclass
class SecurityConfig:
    """Phase 5 (BrowserAgent_General_Autonomous_Agent_Architecture_REVISED.pdf, section 17):
    "zero trust for page content" boundaries that sit alongside, never instead of,
    agent/schemas.py::classify_risk's existing approval gate.

    `default_domain_permission` is the baseline every domain gets absent a future per-domain
    override table (not built yet — no caller sets one, so this is currently a global default
    only): "browser_control" is today's existing behavior (read+write gated by classify_risk/
    approval, unchanged); "read_only" additionally forces every action's risk to never exceed
    READ_ONLY regardless of classify_risk's own verdict; "no_access" blocks navigating to the
    domain at all. `block_local_schemes` documents an invariant `agent/decision.py::
    validate_semantics` already enforces unconditionally (open_url only ever accepts http(s)
    or a same-origin-relative path) — kept here as a read-only documented flag, not a toggle,
    since disabling it would have no legitimate use and Section 29's own policy is "explicit
    invariant over silent behavior."
    """

    default_domain_permission: str = "browser_control"  # "read_only" | "browser_control" | "no_access"
    block_local_schemes: bool = True
    cross_origin_sensitive_transfer_requires_approval: bool = True


@dataclass
class RoutingConfig:
    """See router/policy.py's module docstring for what each mode does. Default is "hybrid":
    deterministic fast paths stay an optimization, everything else goes through the semantic
    planner, with the old keyword/regex-adjacent Qwen router kept only as a last-resort
    fallback if planning itself fails (Section 32 of the semantic planner task)."""

    mode: str = "hybrid"  # "legacy" | "semantic" | "hybrid"


@dataclass
class AgentControlConfig:
    """General-controller migration (BrowserAgent_General_Autonomous_Agent_Architecture_
    REVISED.pdf, section 17). `control_mode` defaults to "legacy" so nothing here changes
    default behavior — the general controller (agent/controller.py) is only reachable by
    explicitly constructing/running it (Phase 2 "shadow/fixture mode"), not via router/ or
    the live UI yet.

    `max_subgoal_attempts` and `max_steps_per_subgoal` are not in the architecture doc's
    illustrative YAML snippet (section 17 says "conceptually" extend, not exhaustively) but
    are required to make the controller's own bounds concrete: the doc's "repeated failure"
    replan trigger needs a defined retry count per subgoal, and delegating to AgentLoop as
    "one bounded interactive subgoal" (section 8) needs an explicit step budget per delegate.
    """

    control_mode: str = "legacy"  # "legacy" | "general" | "hybrid"
    # Which agent implementation a generic entry point uses: "v1" is everything under
    # agent/ (controller/loop/planner/router), "v2" is agent_v2/'s single loop. Default
    # stays "v1" so no existing caller changes behavior (V2 spec §33).
    version: str = "v1"  # "v1" | "v2"
    planner_max_subgoals: int = 5
    max_replans: int = 6
    max_subgoal_attempts: int = 2
    max_steps_per_subgoal: int = 30
    completion_check_after_subgoal: bool = True
    max_workspace_entities_in_context: int = 12
    max_workspace_evidence_in_context: int = 8
    # Phase 4 (delegation to existing Batch/Workflow/Research capabilities, architecture doc
    # section 18/8.1): "deterministic substrate selection before using the LLM" — when the
    # goal text itself already names at least this many literal target URLs, the controller
    # skips the initial planning call entirely and goes straight to a delegate_batch decision
    # (section 8.1: "If a subgoal contains 20 resolved independent URLs... BatchOrchestrator
    # is the obvious substrate... Only ambiguous structural choices require a controller model
    # call"). Below this threshold, whether to delegate remains the planner's own explicit
    # decision (delegate_batch/delegate_workflow/discover_sources are always available to it
    # regardless of this threshold).
    batch_delegation_min_targets: int = 3
    research_discovery_max_sources: int = 8


@dataclass
class V2Config:
    """BrowserAgent V2 (agent_v2/). Entirely additive: nothing here is read by the legacy
    controller/loop, and `agent.version` (below) still defaults to "v1", so installing V2
    changes no existing behavior until it is explicitly selected.

    `keep_alive` is longer than the legacy default because V2 issues one model call per
    browser action — a cold reload of an 8B model between steps costs more than the step
    itself (V2 spec §23 Optimization D).
    """

    max_steps: int = 40
    tasks_dir: str = "./runtime/v2/tasks"
    memory_db: str = "./runtime/v2/memory.sqlite3"
    memory_enabled: bool = True
    memory_top_k: int = 6
    max_total_tokens: int = 3600
    page_tokens: int = 1500
    memory_tokens: int = 380
    state_tokens: int = 700
    max_output_tokens: int = 400
    keep_alive: str = "30m"
    request_timeout_s: float = 120.0


@dataclass
class AppConfig:
    model: ModelConfig = field(default_factory=ModelConfig)
    browser: BrowserConfig = field(default_factory=BrowserConfig)
    context: ContextConfig = field(default_factory=ContextConfig)
    recovery: RecoveryConfig = field(default_factory=RecoveryConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    routing: RoutingConfig = field(default_factory=RoutingConfig)
    agent: AgentControlConfig = field(default_factory=AgentControlConfig)
    security: SecurityConfig = field(default_factory=SecurityConfig)
    v2: V2Config = field(default_factory=V2Config)


def _apply_env_overrides(raw: dict[str, Any]) -> dict[str, Any]:
    """Env vars of the form BROWSER_AGENT_SECTION__KEY override the loaded YAML.

    Example: BROWSER_AGENT_MODEL__ENDPOINT=http://127.0.0.1:9000
    """
    for env_key, value in os.environ.items():
        if not env_key.startswith(_ENV_PREFIX):
            continue
        path = env_key[len(_ENV_PREFIX):].lower().split("__")
        if len(path) != 2:
            continue
        section, key = path
        raw.setdefault(section, {})
        if section in raw and isinstance(raw[section], dict):
            raw[section][key] = value
    return raw


def _coerce(dataclass_type: type, values: dict[str, Any]):
    field_types = get_type_hints(dataclass_type)
    coerced = {}
    for k, v in values.items():
        if k not in field_types:
            continue
        target_type = field_types[k]
        if target_type is bool and isinstance(v, str):
            coerced[k] = v.strip().lower() in ("1", "true", "yes", "on")
        elif target_type in (int,) and isinstance(v, str):
            coerced[k] = int(v)
        elif target_type in (float,) and isinstance(v, str):
            coerced[k] = float(v)
        else:
            coerced[k] = v
    return dataclass_type(**coerced)


def load_config(path: str | Path | None = None) -> AppConfig:
    """Load config/default.yaml (or a given path), then apply BROWSER_AGENT_* env overrides.

    Never assumes a hard-coded model file/endpoint is correct for the running machine —
    this function is the only supported way to obtain configuration.
    """
    if path is None:
        path = Path(__file__).resolve().parent.parent / "config" / "default.yaml"
    path = Path(path)
    raw: dict[str, Any] = {}
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
    raw = _apply_env_overrides(raw)
    config = AppConfig(
        model=_coerce(ModelConfig, raw.get("model", {})),
        browser=_coerce(BrowserConfig, raw.get("browser", {})),
        context=_coerce(ContextConfig, raw.get("context", {})),
        recovery=_coerce(RecoveryConfig, raw.get("recovery", {})),
        storage=_coerce(StorageConfig, raw.get("storage", {})),
        logging=_coerce(LoggingConfig, raw.get("logging", {})),
        routing=_coerce(RoutingConfig, raw.get("routing", {})),
        agent=_coerce(AgentControlConfig, raw.get("agent", {})),
        security=_coerce(SecurityConfig, raw.get("security", {})),
        v2=_coerce(V2Config, raw.get("v2", {})),
    )
    _resolve_runtime_paths(config)
    return config


def _resolve_runtime_paths(config: AppConfig) -> None:
    runtime_dir = Path(config.storage.runtime_dir)
    default_tasks = Path("./runtime/tasks")
    default_logs = Path("./runtime/logs")
    if Path(config.storage.tasks_dir) == default_tasks:
        config.storage.tasks_dir = str(runtime_dir / "tasks")
    if Path(config.browser.user_data_dir) == default_tasks:
        config.browser.user_data_dir = str(runtime_dir / "tasks")
    if Path(config.logging.dir) == default_logs:
        config.logging.dir = str(runtime_dir / "logs")
