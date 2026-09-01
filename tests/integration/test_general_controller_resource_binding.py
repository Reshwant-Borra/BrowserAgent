"""Phase 3 continued-validation pass — deterministic resource-bound subgoal reorientation
(docs/BROWSERAGENT_MASTER_STATUS.md's Phase 3 continued-validation section). The live
`benchmarks/general_agent/run_phase3_resumable.py` matrix diagnosed three distinct,
deterministic, entity-generic gaps behind the `NAVIGATION`/`CONTROLLER`/`EVIDENCE_GROUNDING`
failure categories — none of them a model-capacity limit:

1. Advancing from one candidate subgoal to the next never repositioned the browser at all
   within a single continuous session (only a full session restart did); the model was left to
   notice and self-correct via a prompt hint alone, and did not reliably comply.
2. A controller-level replan could persist a `SUBGOAL_CHANGED` whose `active_subgoal` its own
   `plan` list omitted, permanently desyncing the finish intercept the next time the model
   (correctly) finished for that exact subgoal text.
3. A per-candidate subgoal's stale-evidence guard only caught a URL *already* claimed by a
   different-named entity, missing a forward overshoot onto a not-yet-visited sibling's page —
   which then poisoned every later genuine visit to the real owner of that URL.
4. A synthesis-only subgoal (naming no single candidate, just "report the top-k") was held to
   the same fresh-extraction-evidence bar as a per-candidate subgoal, rejecting a verbatim-
   correct finish forever since there was nothing new to extract.

This file exercises all four fixes: one real-Playwright, real-WorkspaceStore, deterministic-
scripted-model end-to-end run (proving reorientation + the synthesis bypass together, with a
minimal model script that contains no explicit "navigate back to the hub" step at all — if
reorientation were missing, the scripted click targets would not exist on whatever page the
browser was actually left on, and the run would fail), plus focused regression tests for the
plan-invariant self-heal and the stale-evidence forward-overshoot gap, mirroring this
directory's own existing test style (test_general_controller_continuous.py,
test_general_controller_entities.py).
"""
from __future__ import annotations

import json
import re

import pytest

from agent.config import AppConfig
from agent.controller import GeneralAgentController, _HUB_URL_FACT_KEY
from agent.controller_models import ControllerDecision
from agent.schemas import ActionType, ModelDecision
from agent.workspace_models import EvidenceRef, WorkspaceEntity, WorkspaceFact, WorkspacePatch
from inference.llama_client import CompletionResult
from memory.event_store import EventType
from tests.integration.fake_llama import ScriptedLlamaClient, decision


class _SequencedSchemaClient:
    """Same pattern as test_general_controller_entities.py's own fake: one queue per schema
    title, plus a RankResult responder that reads candidate ids straight out of the rendered
    prompt (agent/ranking.py always renders each candidate as `[ent_xxxxx]`)."""

    def __init__(self, responses_by_title: dict[str, list], rank_k: int = 0):
        self._queues = {k: list(v) for k, v in responses_by_title.items()}
        self.endpoint = "fake://controller"
        self.calls: list[str] = []
        self._rank_k = rank_k

    async def complete(self, prompt, grammar=None, max_tokens=256, json_schema=None) -> CompletionResult:
        title = (json_schema or {}).get("title", "")
        self.calls.append(title)
        if title == "RankResult":
            ids = re.findall(r"\[(ent_[0-9a-f]+)\]", prompt)
            selected = ids[: self._rank_k]
            return CompletionResult(text=json.dumps({
                "ranked_entity_ids": selected,
                "rationale_by_entity": {i: "cheapest" for i in selected},
                "missing_information": [],
            }), total_latency_ms=1.0)
        queue = self._queues.get(title)
        if not queue:
            raise AssertionError(f"schema {title!r} ran out of scripted responses (prompt: {prompt[:150]!r})")
        raw = queue.pop(0)
        return CompletionResult(text=json.dumps(raw), total_latency_ms=1.0)

    async def health_check(self) -> bool:
        return True


def _config(tmp_config: AppConfig, **agent_overrides) -> AppConfig:
    for key, value in agent_overrides.items():
        setattr(tmp_config.agent, key, value)
    return tmp_config


def _finding(field: str, value: str) -> dict:
    return {"field": field, "value": value, "evidence": f"{field}: {value}"}


async def test_continuous_reorients_deterministically_between_candidates_no_wasted_navigation(
    tmp_config, fixture_site_url,
):
    """End-to-end regression fixture (Section 8 of the continued-validation task): a hub page
    linking to 3 candidates, one continuous AgentLoop session, a controller-issued plan naming
    each candidate as its own subgoal plus a final top-2 synthesis subgoal. The scripted model
    is given ONLY the click needed to reach each candidate FROM THE HUB (link ids 1/2/3 on
    `candidates_hub.html`) — no script step ever tells it to navigate back to the hub between
    candidates. If the controller did not deterministically reposition the browser there before
    each new subgoal, the second and third candidates' scripted `click` targets would not exist
    on whatever page a *previous* subgoal actually left the browser on (each detail page has
    only one link, id 1, "Back to Widget Directory") — the run would fail contract validation
    and exhaust the scripted queue instead of completing.
    """
    config = _config(tmp_config, completion_check_after_subgoal=False, max_steps_per_subgoal=6,
                      planner_max_subgoals=5)
    hub_url = f"{fixture_site_url}/candidates_hub.html"
    alpha_url = f"{fixture_site_url}/candidate_alpha.html"
    beta_url = f"{fixture_site_url}/candidate_beta.html"
    gamma_url = f"{fixture_site_url}/candidate_gamma.html"

    plan = [
        "Visit the Alpha Widget detail page and record its price_usd and rating",
        "Visit the Beta Widget detail page and record its price_usd and rating",
        "Visit the Gamma Widget detail page and record its price_usd and rating",
        "Find the 2 cheapest widgets and report them with evidence",
    ]
    planner_client = _SequencedSchemaClient({
        "ControllerDecision": [
            {"decision": "start_subgoal", "reason_code": "initial_plan",
             "active_subgoal": plan[0], "plan": plan,
             "resource_refs": [], "clarification_question": None, "completion_claim": None},
        ],
        "CompletionEvaluation": [
            {"satisfied": True, "missing_requirements": [], "unsupported_claims": [],
             "next_recommendation": "finish"},
        ],
    }, rank_k=2)

    # ONE shared child client for the whole continuous session (unlike delegated mode's
    # per-subgoal factory) — this is what proves reorientation happens WITHOUT any extra model
    # turn: each candidate's script is exactly [click the hub's own link, finish], nothing else.
    child_client = ScriptedLlamaClient([
        decision("click", target=1, expected_result={"url_contains": "candidate_alpha"}),
        decision("finish", params={
            "result": "Alpha Widget recorded",
            "structured_result": {"summary": "Alpha Widget page", "findings": [
                _finding("price_usd", "30.00"), _finding("rating", "3.5"),
            ]},
        }),
        decision("click", target=2, expected_result={"url_contains": "candidate_beta"}),
        decision("finish", params={
            "result": "Beta Widget recorded",
            "structured_result": {"summary": "Beta Widget page", "findings": [
                _finding("price_usd", "10.00"), _finding("rating", "4.8"),
            ]},
        }),
        decision("click", target=3, expected_result={"url_contains": "candidate_gamma"}),
        decision("finish", params={
            "result": "Gamma Widget recorded",
            "structured_result": {"summary": "Gamma Widget page", "findings": [
                _finding("price_usd", "20.00"), _finding("rating", "4.2"),
            ]},
        }),
        # The synthesis subgoal: plain prose only, no structured_result and no further browser
        # action — only reachable because _continuous_subgoal_has_evidence's top-k bypass
        # (Bug C's fix) lets it advance from already-collected workspace entities alone.
        decision("finish", params={
            "result": "The 2 cheapest widgets are Beta Widget ($10.00) and Gamma Widget ($20.00).",
        }),
    ])

    controller = GeneralAgentController.create_new(
        config, "Find the 2 cheapest widgets and report them with evidence.", [],
        llama_client=planner_client, child_llama_client_factory=lambda: child_client,
    )
    try:
        state = await controller.run(explicit_target_url=hub_url, strategy="continuous")

        assert state.status == "completed", state.blocked_reason
        assert controller._replans_used == 0  # every subgoal advanced on its first attempt

        workspace = controller.workspace_store.load(controller.control_task_id)
        assert len(workspace.entities) == 3  # all three real candidates, nothing invented
        selected = [e for e in workspace.entities if e.status == "selected"]
        rejected = [e for e in workspace.entities if e.status == "rejected"]
        # WorkspaceStore orders entities by id (random per-run, not insertion order — same
        # caveat test_general_controller_entities.py's own top-k test documents), and the fake
        # RankResult responder just takes the first k ids as they appear in the rendered prompt
        # rather than doing real semantic ranking — which 2 of the 3 real widgets it "selects"
        # varies run to run. The invariant under test is deterministic reorientation + exact-k
        # selection + full evidence provenance, never which specific 2 a non-semantic fake
        # ranker happens to pick.
        all_names = {"Alpha Widget", "Beta Widget", "Gamma Widget"}
        assert len(selected) == 2
        assert len(rejected) == 1
        assert {e.name for e in selected} | {e.name for e in rejected} == all_names
        for e in workspace.entities:
            assert any(ev.entity_id == e.id for ev in workspace.evidence)  # every entity evidenced

        # Positive proof of deterministic reorientation: right before each candidate-click
        # decision, the immediately preceding OBSERVATION was the hub page again — never the
        # previous candidate's own detail page — even though nothing in the script asked for it.
        events = controller.event_store.all_events(controller.control_task_id)
        observations = [e for e in events if e.type == EventType.OBSERVATION and e.payload.get("phase") == "pre_decision"]
        obs_urls = [e.payload["url"] for e in observations]
        hub_hits = sum(1 for u in obs_urls if u == hub_url)
        assert hub_hits >= 3  # once before each of the 3 candidate-click decisions
        assert obs_urls[0] == hub_url  # session start already grounded on the hub
    finally:
        controller.close()


async def test_resolve_subgoal_resource_survives_paraphrase_after_replan(tmp_config):
    """Section 5's replan invariant: a controller-level replan's paraphrased subgoal text must
    still resolve to the same already-known candidate page, not fall back to the hub, as long as
    it still names (or, via the preceding plan item, implies) the same real-world candidate —
    recomputed fresh from persisted evidence every call, no stable-ID bookkeeping needed."""
    config = _config(tmp_config)
    controller = GeneralAgentController.create_new(config, "goal", [])
    try:
        cid = controller.control_task_id
        hub_url = "http://127.0.0.1:1/widgets/index.html"
        detail_url = "http://127.0.0.1:1/widgets/beta_widget.html"
        controller.workspace_store.apply_patch(cid, WorkspacePatch(
            add_facts=[WorkspaceFact(key=_HUB_URL_FACT_KEY, value=hub_url)],
            add_entities=[WorkspaceEntity(id="ent_b", entity_type="candidate", name="Beta Widget",
                                           attributes={"price_usd": "10.00"})],
            add_evidence=[EvidenceRef(entity_id="ent_b", field_key="price_usd", source_event_id=1,
                                       source_url=detail_url, excerpt="Price: $10.00")],
        ))
        # Original collection subgoal text is gone; only a differently-worded replan survives —
        # this is the exact shape a live replan call produces (never verbatim-identical text).
        paraphrased = "Check out the Beta Widget's own page again and confirm its price and rating"
        resolved = controller._resolve_subgoal_resource(paraphrased, [paraphrased])
        assert resolved == detail_url

        # A candidate never visited before still correctly falls back to the hub.
        unvisited = "Visit the Gamma Widget detail page and record its price and rating"
        assert controller._resolve_subgoal_resource(unvisited, [unvisited]) == hub_url

        # A nameless follow-up subgoal resolves via the immediately preceding plan item, exactly
        # like entity_patch_from_findings's own preceding_subgoal reasoning.
        two_step_plan = ["Visit the Beta Widget detail page", "record structured finding"]
        assert controller._resolve_subgoal_resource(two_step_plan[1], two_step_plan) == detail_url
    finally:
        controller.close()


async def test_evidence_from_unvisited_sibling_page_rejected_even_when_url_never_claimed_before(tmp_config):
    """Regression for the exact live poisoning chain (docs/BROWSERAGENT_MASTER_STATUS.md's
    Phase 3 continued-validation forensic trace, `hotels` trial 1): the pre-existing stale-
    evidence heuristic only caught a URL *already* claimed as evidence for a different-named
    entity. It had no way to catch a finish whose evidence came from a page that simply is not
    the subgoal's own already-known candidate page, when that page had never been used as
    evidence before — exactly a forward overshoot onto an unvisited sibling. Left uncaught, that
    single bad ingestion poisons every later genuine visit to the real owner of that URL (the
    OLD heuristic then rejects THOSE too, permanently, since the URL now "belongs" to the wrong
    entity) — this is what exhausted an entire domain's replan budget live."""
    config = _config(tmp_config)
    controller = GeneralAgentController.create_new(config, "goal", [])
    try:
        cid = controller.control_task_id
        hub_url = "http://127.0.0.1:1/hotels/index.html"
        cedar_url = "http://127.0.0.1:1/hotels/cedar_plaza_hotel.html"
        budget_url = "http://127.0.0.1:1/hotels/budget_stay_downtown.html"  # never seen before
        controller.workspace_store.apply_patch(cid, WorkspacePatch(
            add_facts=[WorkspaceFact(key=_HUB_URL_FACT_KEY, value=hub_url)],
            add_entities=[WorkspaceEntity(id="ent_c", entity_type="candidate", name="Cedar Plaza Hotel",
                                           attributes={"price_per_night_usd": "219.00"})],
            add_evidence=[EvidenceRef(entity_id="ent_c", field_key="price_per_night_usd", source_event_id=1,
                                       source_url=cedar_url, excerpt="Price: $219.00")],
        ))
        subgoal = "extract rating from Cedar Plaza Hotel detail page"
        overshoot = ModelDecision(action=ActionType.FINISH, params={
            "result": "Cedar Plaza Hotel rating recorded",
            "structured_result": {"findings": [
                {"field": "rating", "value": "3.6/5", "evidence": "Rating: 3.6/5"},
            ]},
        })
        assert controller._continuous_subgoal_has_evidence(
            subgoal, overshoot, budget_url, [subgoal],
        ) is False

        # The real owner of that page must still be accepted (never over-corrected into
        # rejecting every finish from a fresh URL).
        real_visit_subgoal = "extract rating from Budget Stay Downtown detail page"
        real_visit = ModelDecision(action=ActionType.FINISH, params={
            "result": "Budget Stay Downtown rating recorded",
            "structured_result": {"findings": [
                {"field": "rating", "value": "3.6/5", "evidence": "Rating: 3.6/5"},
            ]},
        })
        assert controller._continuous_subgoal_has_evidence(
            real_visit_subgoal, real_visit, budget_url, [real_visit_subgoal],
        ) is True
    finally:
        controller.close()


async def test_apply_controller_decision_self_heals_active_subgoal_missing_from_plan(tmp_config):
    """Regression for the exact live desync (docs/BROWSERAGENT_MASTER_STATUS.md's Phase 3
    continued-validation forensic trace, `vacuums` trial 1): a replan call returned an
    `active_subgoal` its own `plan` list omitted. Persisting that mismatch as-is permanently
    desyncs the finish intercept's `subgoal not in plan` check the next time the model
    (correctly) finishes for that exact subgoal text — self-heal by appending, deterministic
    state repair rather than more prompt wording."""
    config = _config(tmp_config)
    controller = GeneralAgentController.create_new(config, "goal", [])
    try:
        malformed = ControllerDecision(
            decision="revise_plan", reason_code="repeated_failure",
            active_subgoal="Find the 2 cheapest and report with evidence",
            plan=["Visit the detail page of DustHunter Pro", "Record price_usd and rating for DustHunter Pro"],
        )
        state = await controller._apply_controller_decision(malformed)
        assert state.current_subgoal == "Find the 2 cheapest and report with evidence"
        assert state.current_subgoal in state.plan
        assert state.plan[-1] == "Find the 2 cheapest and report with evidence"
    finally:
        controller.close()


async def test_synthesis_subgoal_advances_without_fresh_extraction_evidence(tmp_config):
    """Regression for the exact live loss (docs/BROWSERAGENT_MASTER_STATUS.md's Phase 3
    continued-validation forensic trace, `laptops` trial 1): a subgoal that only asks the model
    to report already-collected candidates has no page of its own to extract fresh evidence
    from. Holding it to the same extraction-evidence bar as a per-candidate subgoal rejected a
    verbatim-correct finish forever (the model correctly named the real top-k candidates from
    already-recorded facts, with nothing new on the current page to act on), exhausting the
    entire replan budget on a subgoal that was already answerable from persisted state."""
    config = _config(tmp_config)
    controller = GeneralAgentController.create_new(config, "goal", [])
    try:
        cid = controller.control_task_id
        controller.workspace_store.apply_patch(cid, WorkspacePatch(
            add_entities=[
                WorkspaceEntity(id="ent_a", entity_type="candidate", name="Widget A",
                                 attributes={"price_usd": "30"}),
                WorkspaceEntity(id="ent_b", entity_type="candidate", name="Widget B",
                                 attributes={"price_usd": "10"}),
            ],
        ))
        subgoal = "Identify the 2 cheapest widgets from the recorded findings and report them with evidence"
        finish = ModelDecision(action=ActionType.FINISH, params={
            "result": "The 2 cheapest widgets are Widget B ($10) and Widget A ($30).",
        })
        assert controller._continuous_subgoal_has_evidence(subgoal, finish, None, [subgoal]) is True

        # Not enough candidates collected yet -> must NOT bypass (nothing deterministic to
        # advance on, still needs either real evidence or to stay blocked/replanned).
        few_entities_subgoal = "Identify the 3 cheapest widgets from the recorded findings"
        assert controller._continuous_subgoal_has_evidence(
            few_entities_subgoal, finish, None, [few_entities_subgoal],
        ) is False
    finally:
        controller.close()

