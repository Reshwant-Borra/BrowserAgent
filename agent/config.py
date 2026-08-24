"""Configuration loading: YAML file + environment-variable overrides.

Every path/threshold used elsewhere in the codebase flows through this module so there
is exactly one place that resolves "what config is in effect" — no hard-coded machine
paths anywhere else.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

_ENV_PREFIX = "BROWSER_AGENT_"


@dataclass
class ModelConfig:
    endpoint: str = "http://127.0.0.1:8080"
    model_name: str = "qwen3-8b"
    temperature: float = 0.1
    max_output_tokens: int = 256
    max_output_tokens_deep_recovery: int = 512
    request_timeout_s: float = 30.0


@dataclass
class BrowserConfig:
    headless: bool = False
    user_data_dir: str = "./runtime/tasks"
    action_timeout_ms: int = 10000
    interactive_approval: bool = True


@dataclass
class ContextConfig:
    max_page_chars: int = 3000
    max_page_chars_deep_recovery: int = 6000
    recent_actions: int = 5
    max_visible_text_items: int = 12


@dataclass
class RecoveryConfig:
    max_action_retries: int = 2
    identical_action_limit: int = 3
    navigation_cycle_limit: int = 2
    verification_retry_limit: int = 2


@dataclass
class StorageConfig:
    tasks_dir: str = "./runtime/tasks"


@dataclass
class LoggingConfig:
    level: str = "INFO"
    dir: str = "./runtime/logs"
    redact_secrets: bool = True


@dataclass
class AppConfig:
    model: ModelConfig = field(default_factory=ModelConfig)
    browser: BrowserConfig = field(default_factory=BrowserConfig)
    context: ContextConfig = field(default_factory=ContextConfig)
    recovery: RecoveryConfig = field(default_factory=RecoveryConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)


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
    field_types = {f.name: f.type for f in dataclass_type.__dataclass_fields__.values()}
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
    return AppConfig(
        model=_coerce(ModelConfig, raw.get("model", {})),
        browser=_coerce(BrowserConfig, raw.get("browser", {})),
        context=_coerce(ContextConfig, raw.get("context", {})),
        recovery=_coerce(RecoveryConfig, raw.get("recovery", {})),
        storage=_coerce(StorageConfig, raw.get("storage", {})),
        logging=_coerce(LoggingConfig, raw.get("logging", {})),
    )
