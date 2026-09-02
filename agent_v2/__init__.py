"""BrowserAgent V2 — one small loop over the user's real browser.

    from agent_v2 import BrowserAgentV2, BrowserSession, EvidenceLedger, MemoryStore

Eight modules, no framework. The loop itself: `actions` (what the model may say), `state`
(compact working memory), `prompts` (bounded context), `browser_ops` (execute + verify),
`agent` (the loop). The deterministic boundaries around it: `ledger` (what was really
observed and what evidence really exists), `grounding` (what may be presented as supported),
`compute` (arithmetic the model is not asked to do), and `memory`'s write policy (what may
outlive a task).
"""
from agent_v2.actions import Decision, DecisionError, V2Action, validate_decision
from agent_v2.agent import BrowserAgentV2, LoopLimits, build_session
from agent_v2.browser_ops import BrowserSession
from agent_v2.compute import ComputeOp, ComputeResult, run_compute
from agent_v2.grounding import Claim, ClaimKind, GroundingReport, check_answer, classify_outcome
from agent_v2.ledger import EvidenceLedger, EvidenceRecord, VisitedResource
from agent_v2.memory import MemoryStore, WritePolicy, classify_write
from agent_v2.prompts import ContextBudget, build_context
from agent_v2.state import TaskState, TaskStatus

__all__ = [
    "BrowserAgentV2",
    "BrowserSession",
    "Claim",
    "ClaimKind",
    "ComputeOp",
    "ComputeResult",
    "ContextBudget",
    "Decision",
    "DecisionError",
    "EvidenceLedger",
    "EvidenceRecord",
    "GroundingReport",
    "LoopLimits",
    "MemoryStore",
    "TaskState",
    "TaskStatus",
    "V2Action",
    "VisitedResource",
    "WritePolicy",
    "build_context",
    "build_session",
    "check_answer",
    "classify_outcome",
    "classify_write",
    "run_compute",
    "validate_decision",
]
