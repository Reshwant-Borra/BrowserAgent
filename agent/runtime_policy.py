from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from urllib.parse import urlsplit

from agent.schemas import ActionType, ModelDecision, RiskLevel, classify_risk
from browser.page_model import ElementRef


class NavigationScopePolicy(str, Enum):
    SAME_ORIGIN = "same_origin"
    SAME_DOMAIN = "same_domain"
    UNRESTRICTED = "unrestricted"


@dataclass(frozen=True)
class BatchRuntimePolicy:
    target_url: str
    read_only: bool = True
    navigation_scope: NavigationScopePolicy = NavigationScopePolicy.SAME_ORIGIN
    batch_id: str | None = None
    work_item_id: int | None = None
    # True only for a work item whose target is an already-open browser tab the semantic
    # resolver picked out (not a plain URL to navigate to). AgentLoop uses this to tell
    # PlaywrightBackend to attach to that exact tab (by matching target_url against the
    # attached browser's open pages) instead of the default "most recently active tab"
    # heuristic, which is what let one batch child's navigation leak onto a sibling item's
    # tab in cdp_attach mode.
    is_open_tab: bool = False


@dataclass(frozen=True)
class RuntimePolicyViolation:
    category: str
    reason: str


def pre_action_violation(
    policy: BatchRuntimePolicy | None,
    decision: ModelDecision,
    element: ElementRef | None,
) -> RuntimePolicyViolation | None:
    if policy is None:
        return None
    element_name = element.name if element else None
    risk = classify_risk(decision.action, element_name)
    if policy.read_only and risk == RiskLevel.CONSEQUENTIAL:
        return RuntimePolicyViolation(
            category="READ_ONLY_BLOCKED",
            reason=f"batch read_only policy blocked consequential action {decision.action.value} on {element_name!r}",
        )
    if decision.action == ActionType.OPEN_URL:
        url = decision.params.get("url")
        if isinstance(url, str) and not url_in_scope(policy.target_url, url, policy.navigation_scope):
            return RuntimePolicyViolation(
                category="SCOPE_BLOCKED",
                reason=f"batch navigation_scope={policy.navigation_scope.value} blocked open_url to {url}",
            )
    return None


def post_navigation_violation(policy: BatchRuntimePolicy | None, current_url: str) -> RuntimePolicyViolation | None:
    if policy is None:
        return None
    if current_url and not url_in_scope(policy.target_url, current_url, policy.navigation_scope):
        return RuntimePolicyViolation(
            category="SCOPE_BLOCKED",
            reason=f"batch navigation_scope={policy.navigation_scope.value} blocked navigation to {current_url}",
        )
    return None


def url_in_scope(source_url: str, candidate_url: str, scope: NavigationScopePolicy) -> bool:
    if scope == NavigationScopePolicy.UNRESTRICTED:
        return True
    source = urlsplit(source_url)
    candidate = urlsplit(candidate_url)
    if not source.scheme or not source.netloc or not candidate.scheme or not candidate.netloc:
        return True
    if scope == NavigationScopePolicy.SAME_ORIGIN:
        return source.scheme.lower() == candidate.scheme.lower() and source.netloc.lower() == candidate.netloc.lower()
    if scope == NavigationScopePolicy.SAME_DOMAIN:
        return _registrable_domain(source.hostname or "") == _registrable_domain(candidate.hostname or "")
    return False


def _registrable_domain(host: str) -> str:
    parts = host.lower().split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host.lower()
