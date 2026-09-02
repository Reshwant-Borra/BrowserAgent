"""BrowserAgent V2 — one small loop over the user's real browser.

    from agent_v2 import BrowserAgentV2, BrowserSession, MemoryStore, build_session

Six modules, no framework: `actions` (what the model may say), `state` (compact working
memory), `memory` (durable cross-task memory), `prompts` (bounded context), `browser_ops`
(execute + verify), `agent` (the loop).
"""
from agent_v2.actions import Decision, DecisionError, V2Action, validate_decision
from agent_v2.agent import BrowserAgentV2, LoopLimits, build_session
from agent_v2.browser_ops import BrowserSession
from agent_v2.memory import MemoryStore
from agent_v2.prompts import ContextBudget, build_context
from agent_v2.state import TaskState, TaskStatus

__all__ = [
    "BrowserAgentV2",
    "BrowserSession",
    "ContextBudget",
    "Decision",
    "DecisionError",
    "LoopLimits",
    "MemoryStore",
    "TaskState",
    "TaskStatus",
    "V2Action",
    "build_context",
    "build_session",
    "validate_decision",
]
