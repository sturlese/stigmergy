import copy
import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from evals.filing import planner_eval
from evals.filing import worktree as eval_worktree
from evals.filing.constants import PRODUCTION_EQUIVALENT_MODE
from evals.filing.parity import current_release_inputs, evaluate
from stigmergy.knowledge.plan import EntityProposal, FilingPlan, PageMutation, RepairMutation, RepairPlan
from stigmergy.knowledge.planner import PlanRun

ROOT = Path(__file__).resolve().parents[2]
BASE_EXPECTED = current_release_inputs(ROOT)
BRAIN_PROMPT = {
    "commit": "0123456789abcdef0123456789abcdef01234567",
    "sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
}
EXPECTED = replace(BASE_EXPECTED, brain_prompt=BRAIN_PROMPT)
CORPUS = "0123456789abcdef" * 4
INITIAL = "0123456789abcdef0123456789abcdef01234567"
SOURCE = "sources/2026/09/00000000-0000-4000-8000-000000000001.md"
HARNESS_BODY = (
    "# Harness Engineering\n\n"
    "A model alone is not an agentic system: the model supplies reasoning while the "
    "agent harness turns its text into work. The six capabilities are tools let the model "
    "request actions; a loop repeats decide, act, observe; memory/state preserves work; context "
    "selection chooses what the model sees; a working environment provides an isolated workspace; "
    "and a clear objective and verification establish external acceptance criteria.\n\n"
    "The three trust capabilities are permissions and limits, observability through traces of "
    "context and outcomes, and evals using stable evaluation tasks. Skills, MCP, subagents, and "
    "long-term memory extend the same design. Santi (@santtiagom_) authored this explanation. "
    "OpenAI built one million lines and 1,500 pull requests; LangChain rose from rank 30 to the "
    "top 5 on Terminal Bench; Anthropic showed a polished but broken application versus a working "
    f"app under different harness configurations. (Source: `{SOURCE}`)"
)
_PAYLOADS: dict[tuple[str, bool], dict] = {}


class _RecordedRevisionPlanner:
    def __init__(self, plan: FilingPlan):
        self.plan = plan

    def revise(self, **_kwargs) -> PlanRun:
        return PlanRun(self.plan, model_requests=1)


class _RecordedRepairPlanner:
    def repair(self, *, files, **_kwargs) -> PlanRun:
        path, body = next(iter(files.items()))
        repaired_body = body.replace(
            "app under different harness configurations.",
            f"app under different harness configurations. (Source: `{SOURCE}`)",
        )
        return PlanRun(
            RepairPlan(
                summary="Restored the required local source citation.",
                mutations=(
                    RepairMutation(
                        path=path,
                        body=repaired_body,
                        reason="Restored the local source citation.",
                    ),
                ),
            ),
            model_requests=1,
        )


def _plan(case_id: str, *, passing: bool) -> FilingPlan:
    if not passing:
        return FilingPlan(summary="Deliberately failing evaluation result.")
    entities = tuple(
        EntityProposal(name=name, entity_type="organization") for name in ("Santi", "OpenAI", "Anthropic", "LangChain")
    )
    if case_id == "decision_trace_quality":
        return FilingPlan(
            summary="Filed decision trace quality.",
            entities=(
                EntityProposal(name="Mira Chen", entity_type="person"),
                EntityProposal(name="Northstar Signal Lab", entity_type="organization"),
            ),
            mutations=(
                PageMutation(
                    action="create",
                    role="concept",
                    title="Decision Trace Quality",
                    body=(
                        "# Decision Trace Quality\n\nDecision trace quality records the evidence behind "
                        "a decision so later readers can inspect the result. Mira Chen authored the method "
                        "and Northstar Signal Lab measured its use. (Source: "
                        "`sources/2026/09/00000000-0000-4000-8000-000000000002.md`)"
                    ),
                    entities=("Mira Chen", "Northstar Signal Lab"),
                    reason="The source explains the concept.",
                ),
            ),
        )
    mutation = PageMutation(
        action="create",
        role="concept",
        title="Harness Engineering",
        body=(
            HARNESS_BODY + "\n\nClaude Code and Codex are coding-agent environments named by the source. "
            f"(Source: `{SOURCE}`)"
        ),
        entities=("Santi", "OpenAI", "Anthropic", "LangChain"),
        reason="The source explains the concept.",
    )
    if case_id == "harness_engineering":
        return FilingPlan(summary="Filed Harness Engineering.", entities=entities, mutations=(mutation,))
    return FilingPlan(
        summary="Enriched the seeded Harness Engineering graph.",
        entities=entities,
        mutations=(
            mutation.model_copy(update={"body": HARNESS_BODY.replace("agent harness", "[[Agent Harness]]")}),
            PageMutation(
                action="update",
                path="wiki/concepts/Agent Harness.md",
                body=(
                    "# Agent Harness\n\nAn [[Agent Harness]] is the operating layer enriched by "
                    "[[Harness Engineering]]. Santi, OpenAI, Anthropic, and LangChain illustrate its "
                    f"leverage. (Source: `{SOURCE}`)"
                ),
                entities=("Santi", "OpenAI", "Anthropic", "LangChain"),
                reason="Existing concept gains evidence.",
            ),
        ),
    )


def _payload(case_id: str, *, implementation: str, passing: bool) -> dict:
    key = (f"{implementation}:{case_id}", passing)
    cached = _PAYLOADS.get(key)
    if cached is not None:
        return copy.deepcopy(cached)
    case = planner_eval.load_case(EXPECTED.case_paths[case_id])
    source_text = EXPECTED.fixture_paths[case_id].read_text(encoding="utf-8")
    plan = _plan(case_id, passing=passing)
    active_plan = plan
    recorded_repair = None
    revision = {"required": False, "attempted": False, "applied": False, "model_requests": 0}
    with eval_worktree.prepared(
        case,
        source_text,
        template=str(EXPECTED.repo_root / "evals" / "filing" / "repo"),
    ) as worktree:
        if case_id == "harness_engineering" and passing:
            broken = plan.model_copy(
                update={
                    "mutations": (
                        plan.mutations[0].model_copy(
                            update={
                                "body": plan.mutations[0].body.replace(
                                    f" (Source: `{SOURCE}`)", ""
                                )
                            }
                        ),
                    )
                }
            )
            plan = broken
            gates, active_plan, recorded_repair = eval_worktree.apply_with_production_repair(
                worktree,
                plan,
                _RecordedRepairPlanner(),
                planning_model_requests=1,
                max_turns=2,
                return_plan=True,
                return_repair_plan=True,
            )
        elif case_id == "harness_engineering_seeded" and passing:
            gates, active_plan = eval_worktree.apply_with_production_repair(
                worktree,
                plan,
                _RecordedRevisionPlanner(plan),
                planning_model_requests=1,
                max_turns=2,
                return_plan=True,
            )
            revision = {
                "required": gates["semantic_revision_required"],
                "attempted": gates["semantic_revision_attempted"],
                "applied": gates["semantic_revision_applied"],
                "model_requests": gates["semantic_revision_model_requests"],
            }
        else:
            gates = eval_worktree.apply_and_gate(worktree, plan)
        effective = (
            eval_worktree.effective_plan(worktree, active_plan)
            if gates["passed"]
            else active_plan
        )
    semantic = planner_eval.score(effective, case, source_text=source_text)
    raw_gates = {
        **{gate: semantic[gate]["passed"] for gate in sorted(semantic) if gate != "passed"},
        "writer": gates["passed"],
    }
    payload = {
        "brain_prompt": copy.deepcopy(BRAIN_PROMPT) if implementation == "stigmergy" else None,
        "case_sha256": EXPECTED.source_cases[case_id],
        "fixture_sha256": EXPECTED.source_fixtures[case_id],
        "plan": plan.model_dump(mode="json"),
        "reviewed_plan": active_plan.model_dump(mode="json") if revision["applied"] else None,
        "semantic_revision": revision,
        "repair_plan": recorded_repair.model_dump(mode="json") if recorded_repair is not None else None,
        "repair_plan_sha256": (
            hashlib.sha256(_canonical(recorded_repair.model_dump(mode="json"))).hexdigest()
            if recorded_repair is not None
            else None
        ),
        "effective_plan": effective.model_dump(mode="json"),
        "score": semantic,
        "gates": gates,
        "raw_gates": raw_gates,
    }
    _PAYLOADS[key] = copy.deepcopy(payload)
    return payload


def _case_result(case_id: str, repeat: int, *, implementation: str, passed: bool, runtime: dict) -> dict:
    payload = _payload(case_id, implementation=implementation, passing=passed)
    model_requests = (
        1
        + payload["semantic_revision"]["model_requests"]
        + payload["gates"].get("repair_model_requests", 0)
    )
    digest = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {
        "case_id": case_id,
        "case_sha256": EXPECTED.source_cases[case_id],
        "fixture_sha256": EXPECTED.source_fixtures[case_id],
        "brain_prompt": copy.deepcopy(payload["brain_prompt"]),
        "runtime": runtime,
        "execution_mode": PRODUCTION_EQUIVALENT_MODE,
        "configured_max_turns": 3,
        "model_requests": model_requests,
        "planning_model_requests": 1,
        "semantic_revision_required": payload["semantic_revision"]["required"],
        "semantic_revision_attempted": payload["semantic_revision"]["attempted"],
        "semantic_revision_applied": payload["semantic_revision"]["applied"],
        "semantic_revision_model_requests": payload["semantic_revision"]["model_requests"],
        "repair_model_requests": payload["gates"].get("repair_model_requests", 0),
        "repair_plan_sha256": payload["repair_plan_sha256"],
        "schema_retry_count": 0,
        "semantic_repair_count": payload["gates"].get("semantic_repair_count", 0),
        "elapsed_ms": 42 + repeat,
        "usage": {"requests": model_requests},
        "score": copy.deepcopy(payload["score"]),
        "gates": copy.deepcopy(payload["gates"]),
        "raw_gates": copy.deepcopy(payload["raw_gates"]),
        "output": {"sha256": digest, "artifact_ref": f"sha256:{digest}"},
        "payload": payload,
    }


def _run(implementation: str, run_id: str, level: str, repeat: int, *, passed: bool) -> dict:
    provenance = {
        "corpus_sha256": CORPUS,
        "initial_graph_ref": INITIAL,
        "source_cases": EXPECTED.source_cases,
        "source_fixtures": EXPECTED.source_fixtures,
    }
    if implementation == "stigmergy":
        provenance.update(
            commit=EXPECTED.commit,
            librarian_skill_sha256=EXPECTED.librarian_skill_sha256,
            brain_prompt=copy.deepcopy(BRAIN_PROMPT),
        )
    runtime = (
        {"model": "openai/gpt-oss-120b", "reasoning_level": level, "provider": "cerebras", "max_tokens": 40960}
        if implementation == "stigmergy"
        else {"model": "fixture", "reasoning_level": level, "provider": "fixture"}
    )
    return {
        "implementation": implementation,
        "run_id": run_id,
        "runtime": runtime,
        "execution": {"mode": PRODUCTION_EQUIVALENT_MODE, "configured_max_turns": 3},
        "provenance": provenance,
        "case_results": [
            _case_result(case_id, repeat, implementation=implementation, passed=passed, runtime=runtime)
            for case_id in sorted(EXPECTED.source_cases)
        ],
    }


def _matrix_item(level: str, *, passed: bool, repeats: int = 1) -> dict:
    return {
        "reasoning_level": level,
        "runtime": {"model": "openai/gpt-oss-120b", "reasoning_level": level, "provider": "cerebras", "max_tokens": 40960},
        "runs": [
            _run("stigmergy", f"matrix-{level}-{repeat}", level, repeat, passed=passed)
            for repeat in range(1, repeats + 1)
        ],
        "passed": passed,
    }


def _unstable_matrix_item(level: str) -> dict:
    runs = [
        _run("stigmergy", f"matrix-{level}-1", level, 1, passed=True),
        _run("stigmergy", f"matrix-{level}-2", level, 2, passed=False),
    ]
    return {
        "reasoning_level": level,
        "runtime": {"model": "openai/gpt-oss-120b", "reasoning_level": level, "provider": "cerebras", "max_tokens": 40960},
        "runs": runs,
        "passed": False,
    }


def _canonical(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _write_review_bundle(root: Path, artifact: dict) -> None:
    """Build external, content-addressed review evidence for the validator contract."""
    root.mkdir(parents=True, exist_ok=True)
    baseline = next(run for run in artifact["runs"] if run["implementation"] == "hippocampus")
    selected = [run for run in artifact["runs"] if run["implementation"] == "stigmergy"]
    baseline_cases = {case["case_id"]: case for case in baseline["case_results"]}
    comparisons = []
    mappings = []
    reviewer_pairs = []
    regressions = []
    for run in selected:
        for case in run["case_results"]:
            comparison_id = f"comparison-{case['case_id']}-{run['run_id']}"
            entries = []
            for implementation, _candidate_run, candidate_case in (
                ("hippocampus", baseline, baseline_cases[case["case_id"]]),
                ("stigmergy", run, case),
            ):
                label = "candidate-" + hashlib.sha256(
                    f"{comparison_id}:{implementation}".encode()
                ).hexdigest()[:16]
                entry = {
                    "label": label,
                    "case_output_sha256": candidate_case["output"]["sha256"],
                    "effective_pages_sha256": hashlib.sha256(
                        _canonical(candidate_case["payload"]["effective_plan"])
                    ).hexdigest(),
                    "draft_plan_sha256": hashlib.sha256(
                        _canonical(candidate_case["payload"]["plan"])
                    ).hexdigest(),
                    "reviewed_plan_sha256": (
                        hashlib.sha256(_canonical(candidate_case["payload"]["reviewed_plan"])).hexdigest()
                        if candidate_case["payload"]["reviewed_plan"] is not None
                        else None
                    ),
                    "repair_plan_sha256": candidate_case["payload"]["repair_plan_sha256"],
                    "semantic_revision": candidate_case["payload"]["semantic_revision"],
                }
                entries.append(entry)
            comparisons.append({"comparison_id": comparison_id, "candidates": entries})
            labels = [
                {
                    **entry,
                    "implementation": implementation,
                    "run_id": candidate_run["run_id"],
                }
                for entry, (implementation, candidate_run) in zip(
                    entries, (("hippocampus", baseline), ("stigmergy", run)), strict=True
                )
            ]
            mappings.append({"comparison_id": comparison_id, "labels": labels})
            regression = {
                "comparison_id": comparison_id,
                "candidate_label": labels[0]["label"],
                "implementation": "hippocampus",
                "run_id": baseline["run_id"],
                "case_output_sha256": labels[0]["case_output_sha256"],
                "effective_pages_sha256": labels[0]["effective_pages_sha256"],
                "reason": "Fixture baseline regression.",
            }
            regressions.append(regression)
            reviewer_pairs.append(
                {
                    "pair_id": comparison_id,
                    "judgments": {"fixture": {"winner": "tie", "reason": "Fixture."}},
                    "overall": {"winner": "tie", "reason": "Fixture."},
                    "material_regression": {
                        "side": labels[0]["label"],
                        "reason": regression["reason"],
                    },
                }
            )
    packet = {"schema_version": 1, "comparisons": comparisons}
    packet_bytes = _canonical(packet)
    packet_canonical = hashlib.sha256(packet_bytes).hexdigest()
    packet_name = f"blind-review-packet-{packet_canonical}.json"
    (root / packet_name).write_bytes(packet_bytes)
    mapping = {"schema_version": 1, "packet_sha256": packet_canonical, "mapping": mappings}
    mapping_bytes = _canonical(mapping)
    mapping_canonical = hashlib.sha256(mapping_bytes).hexdigest()
    mapping_name = f"blind-review-mapping-{mapping_canonical}.secret.json"
    (root / mapping_name).write_bytes(mapping_bytes)
    response = {
        "reviewer_identity": "fixture-reviewer",
        "method": "fixture-blind-pairwise",
        "packet_sha256": hashlib.sha256(packet_bytes).hexdigest(),
        "pairs": reviewer_pairs,
    }
    response_bytes = _canonical(response)
    response_canonical = hashlib.sha256(response_bytes).hexdigest()
    response_name = f"blind-reviewer-response-{response_canonical}.json"
    (root / response_name).write_bytes(response_bytes)
    review = {
        "schema_version": "blind-editorial-review-unblind-v2",
        "verdict": "no_material_stigmergy_regression",
        "reviewer_response": {
            "path": response_name,
            "raw_sha256": hashlib.sha256(response_bytes).hexdigest(),
            "canonical_sha256": response_canonical,
            "artifact_ref": f"sha256:{response_canonical}",
        },
        "packet": {
            "path": packet_name,
            "raw_sha256": hashlib.sha256(packet_bytes).hexdigest(),
            "canonical_sha256": packet_canonical,
        },
        "mapping": {
            "path": mapping_name,
            "raw_sha256": hashlib.sha256(mapping_bytes).hexdigest(),
            "canonical_sha256": mapping_canonical,
        },
        "unblinding": {
            "material_regressions": regressions,
            "stigmergy_material_regressions": [],
        },
    }
    review_bytes = _canonical(review)
    review_canonical = hashlib.sha256(review_bytes).hexdigest()
    (root / f"blind-review-unblind-{review_canonical}.json").write_bytes(review_bytes)
    artifact["admission_status"] = "passed"
    artifact["blind_review_packet"] = {
        "status": "completed",
        "raw_sha256": hashlib.sha256(packet_bytes).hexdigest(),
        "canonical_sha256": packet_canonical,
    }
    artifact["blind_editorial_review"]["provenance"]["artifact_ref"] = f"sha256:{review_canonical}"


def _artifact(review_root: Path) -> dict:
    selected = [_run("stigmergy", f"matrix-high-{repeat}", "high", repeat, passed=True) for repeat in range(1, 4)]
    artifact = {
        "schema_version": 4,
        "corpus_sha256": CORPUS,
        "initial_graph_ref": INITIAL,
        "stigmergy_commit": EXPECTED.commit,
        "librarian_skill_sha256": EXPECTED.librarian_skill_sha256,
        "brain_prompt": copy.deepcopy(BRAIN_PROMPT),
        "source_cases": [{"id": key, "sha256": value} for key, value in EXPECTED.source_cases.items()],
        "source_fixtures": [{"id": key, "sha256": value} for key, value in EXPECTED.source_fixtures.items()],
        "runs": [
            _run("hippocampus", "hippocampus-reference-1", "medium", 1, passed=True),
            *selected,
        ],
        "reasoning_matrix": [
            _matrix_item("minimal", passed=False),
            _unstable_matrix_item("low"),
            _unstable_matrix_item("medium"),
            {
                "reasoning_level": "high",
                "runtime": {
                    "model": "openai/gpt-oss-120b",
                    "reasoning_level": "high",
                    "provider": "cerebras",
                    "max_tokens": 40960,
                },
                "runs": copy.deepcopy(selected),
                "passed": True,
            },
        ],
        "blind_editorial_review": {
            "verdict": "no_material_stigmergy_regression",
            "provenance": {
                "reviewer": "fixture",
                "method": "blind-pairwise",
                "corpus_sha256": CORPUS,
                "initial_graph_ref": INITIAL,
                "source_cases": EXPECTED.source_cases,
                "runs": {
                    "hippocampus": ["hippocampus-reference-1"],
                    "stigmergy": ["matrix-high-1", "matrix-high-2", "matrix-high-3"],
                },
            },
        },
    }
    _write_review_bundle(review_root, artifact)
    return artifact


def _rebind_review(artifact: dict) -> None:
    artifact["blind_editorial_review"]["provenance"]["runs"] = {
        implementation: sorted(run["run_id"] for run in artifact["runs"] if run["implementation"] == implementation)
        for implementation in ("hippocampus", "stigmergy")
    }


def _reasons(artifact: dict, review_root: Path) -> set[str]:
    result = evaluate(artifact, expected=EXPECTED, review_root=review_root)
    assert result["passed"] is False
    return {item["reason"] for item in result["failures"]}


def test_parity_gate_accepts_complete_case_level_evidence_and_three_selected_repeats(tmp_path):
    assert evaluate(_artifact(tmp_path), expected=EXPECTED, review_root=tmp_path)["passed"] is True


def test_parity_admits_one_pure_create_structural_repair_within_two_requests(tmp_path):
    artifact = _artifact(tmp_path)
    repair_case = next(
        case
        for run in artifact["runs"]
        if run["implementation"] == "stigmergy"
        for case in run["case_results"]
        if case["case_id"] == "harness_engineering"
    )

    assert repair_case["repair_model_requests"] == 1
    assert repair_case["model_requests"] == 2
    assert repair_case["payload"]["repair_plan"] is not None
    assert repair_case["repair_plan_sha256"] == repair_case["payload"]["repair_plan_sha256"]
    assert evaluate(artifact, expected=EXPECTED, review_root=tmp_path)["passed"] is True


def test_honest_hippocampus_baseline_may_fail_hard_gates_without_relaxing_replay_validation(tmp_path):
    artifact = _artifact(tmp_path)
    artifact["runs"][0] = _run("hippocampus", "hippocampus-reference-1", "medium", 1, passed=False)
    _write_review_bundle(tmp_path, artifact)

    assert evaluate(artifact, expected=EXPECTED, review_root=tmp_path)["passed"] is True

    artifact["runs"][0]["case_results"][0]["payload"]["effective_plan"]["summary"] = "tampered"
    assert {"case-payload", "case-output-hash"} & _reasons(artifact, tmp_path)


def test_blind_review_must_reference_each_baseline_and_selected_run(tmp_path):
    artifact = _artifact(tmp_path)
    artifact["blind_editorial_review"]["provenance"]["runs"]["hippocampus"] = []
    assert "stale-or-mismatched-review" in _reasons(artifact, tmp_path)

    artifact = _artifact(tmp_path)
    artifact["blind_editorial_review"]["provenance"]["runs"]["stigmergy"].pop()
    assert "stale-or-mismatched-review" in _reasons(artifact, tmp_path)


def test_parity_gate_rejects_aggregate_only_or_missing_case_evidence(tmp_path):
    artifact = _artifact(tmp_path)
    artifact["runs"][0].pop("case_results")
    assert "aggregate-only-evidence" in _reasons(artifact, tmp_path)

    artifact = _artifact(tmp_path)
    artifact["runs"][1]["case_results"].pop()
    assert "missing-case-result" in _reasons(artifact, tmp_path)


def test_parity_gate_rejects_duplicate_or_failed_case_repeats(tmp_path):
    artifact = _artifact(tmp_path)
    artifact["runs"][1]["case_results"].append(copy.deepcopy(artifact["runs"][1]["case_results"][0]))
    assert "duplicate-case-result" in _reasons(artifact, tmp_path)

    artifact = _artifact(tmp_path)
    artifact["runs"][1]["case_results"][0]["raw_gates"]["writer"] = False
    assert {"case-failed", "case-payload"} <= _reasons(artifact, tmp_path)


def test_parity_gate_requires_selected_production_equivalent_three_repeat_evidence(tmp_path):
    artifact = _artifact(tmp_path)
    artifact["runs"][1]["execution"]["mode"] = "planner-only"
    assert "production-equivalence" in _reasons(artifact, tmp_path)

    artifact = _artifact(tmp_path)
    artifact["runs"].pop()
    artifact["reasoning_matrix"][-1]["runs"].pop()
    _rebind_review(artifact)
    assert {"selected-reasoning-insufficient-repeats", "selected-run-mismatch"} <= _reasons(artifact, tmp_path)


def test_parity_gate_requires_a_complete_lowest_passing_reasoning_matrix(tmp_path):
    artifact = _artifact(tmp_path)
    artifact["reasoning_matrix"][1] = _matrix_item("low", passed=True)
    assert "reasoning-not-lowest-passing" in _reasons(artifact, tmp_path)

    artifact = _artifact(tmp_path)
    artifact["reasoning_matrix"].pop(1)
    assert "reasoning-matrix-incomplete" in _reasons(artifact, tmp_path)


def test_parity_gate_rejects_prompt_drift_and_nonimmutable_output_metadata(tmp_path):
    artifact = _artifact(tmp_path)
    artifact["brain_prompt"]["sha256"] = "abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789"
    assert "candidate-brain-prompt" in _reasons(artifact, tmp_path)

    artifact = _artifact(tmp_path)
    artifact["runs"][1]["case_results"][0]["output"]["artifact_ref"] = "mutable-path"
    assert "case-output" in _reasons(artifact, tmp_path)


def test_parity_gate_rejects_tampered_payload_hash_score_and_gates(tmp_path):
    artifact = _artifact(tmp_path)
    artifact["runs"][1]["case_results"][0]["payload"]["effective_plan"]["summary"] = "tampered"
    assert {"case-payload", "case-output-hash"} & _reasons(artifact, tmp_path)

    artifact = _artifact(tmp_path)
    artifact["runs"][1]["case_results"][0]["score"]["passed"] = False
    assert "case-payload" in _reasons(artifact, tmp_path)


def test_parity_rejects_missing_extra_or_tampered_structural_repair_evidence(tmp_path):
    artifact = _artifact(tmp_path)
    repair_case = next(
        case
        for run in artifact["runs"]
        if run["implementation"] == "stigmergy"
        for case in run["case_results"]
        if case["case_id"] == "harness_engineering"
    )
    repair_case["payload"]["repair_plan"] = None
    repair_case["payload"]["repair_plan_sha256"] = None
    repair_case["repair_plan_sha256"] = None
    repair_case["output"]["sha256"] = hashlib.sha256(_canonical(repair_case["payload"])).hexdigest()
    repair_case["output"]["artifact_ref"] = f"sha256:{repair_case['output']['sha256']}"
    _write_review_bundle(tmp_path, artifact)
    assert "case-repair-evidence" in _reasons(artifact, tmp_path)

    artifact = _artifact(tmp_path)
    pure_case = next(
        case
        for run in artifact["runs"]
        if run["implementation"] == "stigmergy"
        for case in run["case_results"]
        if case["case_id"] == "decision_trace_quality"
    )
    extra_repair = RepairPlan(summary="Unexpected repair evidence.").model_dump(mode="json")
    extra_hash = hashlib.sha256(_canonical(extra_repair)).hexdigest()
    pure_case["payload"]["repair_plan"] = extra_repair
    pure_case["payload"]["repair_plan_sha256"] = extra_hash
    pure_case["repair_plan_sha256"] = extra_hash
    pure_case["output"]["sha256"] = hashlib.sha256(_canonical(pure_case["payload"])).hexdigest()
    pure_case["output"]["artifact_ref"] = f"sha256:{pure_case['output']['sha256']}"
    _write_review_bundle(tmp_path, artifact)
    assert "case-repair-evidence" in _reasons(artifact, tmp_path)

    artifact = _artifact(tmp_path)
    repair_case = next(
        case
        for run in artifact["runs"]
        if run["implementation"] == "stigmergy"
        for case in run["case_results"]
        if case["case_id"] == "harness_engineering"
    )
    repair_case["payload"]["repair_plan"]["mutations"][0]["body"] += " tampered"
    _write_review_bundle(tmp_path, artifact)
    assert "case-output-hash" in _reasons(artifact, tmp_path)


def test_parity_rejects_a_valid_but_different_recorded_effective_plan(tmp_path):
    artifact = _artifact(tmp_path)
    case = artifact["runs"][1]["case_results"][0]
    case["payload"]["effective_plan"]["summary"] = "A valid but non-replayed effective plan."
    case["output"]["sha256"] = hashlib.sha256(_canonical(case["payload"])).hexdigest()
    case["output"]["artifact_ref"] = f"sha256:{case['output']['sha256']}"
    _write_review_bundle(tmp_path, artifact)

    assert "replay-effective-plan-mismatch" in _reasons(artifact, tmp_path)


@pytest.mark.parametrize("action", ("update", "delete"))
def test_parity_worktree_revises_an_authorized_page_omitted_from_model_context(action):
    case = {
        "source_title": "Asymmetric ACL filing case",
        "source_path": SOURCE,
        "audience": ["engineering", "finance"],
        "seed_pages": [
            {
                "id": "engineering_target",
                "role": "concept",
                "title": "Engineering Target",
                "body": "# Engineering Target\n\nEngineering-only body. (Source: `{source_path}`)",
                "audience": ["engineering"],
            }
        ],
    }
    source_text = "A capture shared by engineering and finance revises an engineering concept."
    mutation = PageMutation(
        action=action,
        path="wiki/concepts/Engineering Target.md",
        reason="The capture changes the existing engineering target.",
        **(
            {
                "body": "# Engineering Target\n\nRevised engineering-only body. (Source: `"
                + SOURCE
                + "`)"
            }
            if action == "update"
            else {}
        ),
    )
    draft = FilingPlan(summary="Revised an asymmetric-ACL target.", mutations=(mutation,))

    class RecordingPlanner:
        def __init__(self):
            self.calls = []

        def revise(self, **kwargs):
            self.calls.append(kwargs)
            return PlanRun(draft, model_requests=1)

    planner = RecordingPlanner()
    with eval_worktree.prepared(
        case,
        source_text,
        template=str(ROOT / "evals" / "filing" / "repo"),
    ) as worktree:
        gates, _active = eval_worktree.apply_with_production_repair(
            worktree,
            draft,
            planner,
            planning_model_requests=1,
            max_turns=2,
            return_plan=True,
        )

    assert gates["semantic_revision_required"] is True
    assert gates["semantic_revision_attempted"] is True
    assert gates["semantic_revision_applied"] is True
    assert len(planner.calls) == 1
    assert "Engineering Target" not in planner.calls[0]["context"]
    assert "Engineering-only body" not in planner.calls[0]["context"]


def test_parity_gate_rejects_unaccounted_semantic_revision_requests(tmp_path):
    artifact = _artifact(tmp_path)
    artifact["runs"][1]["case_results"][0]["semantic_revision_model_requests"] = 1
    artifact["runs"][1]["case_results"][0]["semantic_revision_attempted"] = True

    assert "case-observability" in _reasons(artifact, tmp_path)

    artifact = _artifact(tmp_path)
    artifact["runs"][1]["case_results"][0]["gates"]["passed"] = False
    assert "case-payload" in _reasons(artifact, tmp_path)


def test_parity_replays_the_original_draft_trigger_and_three_request_budget(tmp_path):
    artifact = _artifact(tmp_path)
    seeded = next(
        item for item in artifact["runs"][1]["case_results"]
        if item["case_id"] == "harness_engineering_seeded"
    )
    seeded["semantic_revision_required"] = False
    seeded["semantic_revision_attempted"] = False
    seeded["semantic_revision_applied"] = False
    seeded["semantic_revision_model_requests"] = 0
    seeded["model_requests"] = 1
    seeded["usage"] = {"requests": 1}
    seeded["payload"]["semantic_revision"] = {
        "required": False,
        "attempted": False,
        "applied": False,
        "model_requests": 0,
    }
    seeded["output"]["sha256"] = hashlib.sha256(_canonical(seeded["payload"])).hexdigest()
    seeded["output"]["artifact_ref"] = f"sha256:{seeded['output']['sha256']}"
    _write_review_bundle(tmp_path, artifact)

    assert "case-semantic-revision-trigger" in _reasons(artifact, tmp_path)

    artifact = _artifact(tmp_path)
    artifact["runs"][1]["case_results"][0]["model_requests"] = 4
    artifact["runs"][1]["case_results"][0]["planning_model_requests"] = 4
    assert "case-observability" in _reasons(artifact, tmp_path)

    artifact = _artifact(tmp_path)
    seeded = next(
        item
        for item in artifact["runs"][1]["case_results"]
        if item["case_id"] == "harness_engineering_seeded"
    )
    forbidden_repair = RepairPlan(summary="A repair after revision must not be admitted.").model_dump(mode="json")
    forbidden_hash = hashlib.sha256(_canonical(forbidden_repair)).hexdigest()
    seeded["repair_model_requests"] = 1
    seeded["repair_plan_sha256"] = forbidden_hash
    seeded["semantic_repair_count"] = 1
    seeded["model_requests"] = 3
    seeded["usage"] = {"requests": 3}
    seeded["payload"]["repair_plan"] = forbidden_repair
    seeded["payload"]["repair_plan_sha256"] = forbidden_hash
    seeded["output"]["sha256"] = hashlib.sha256(_canonical(seeded["payload"])).hexdigest()
    seeded["output"]["artifact_ref"] = f"sha256:{seeded['output']['sha256']}"
    _write_review_bundle(tmp_path, artifact)

    assert "case-repair-evidence" in _reasons(artifact, tmp_path)


def test_parity_rejects_schema_v3_as_missing_replayable_revision_evidence(tmp_path):
    artifact = _artifact(tmp_path)
    artifact["schema_version"] = 3

    assert "schema-v3-obsolete" in _reasons(artifact, tmp_path)


def test_parity_gate_rejects_pending_or_nonterminal_admission_packet(tmp_path):
    artifact = _artifact(tmp_path)
    artifact["admission_status"] = "awaiting-blind-editorial-review"
    artifact["pending_full_artifact_fields"] = ["blind_editorial_review"]
    artifact["blind_review_packet"]["status"] = "awaiting-reviewer-response"

    assert {
        "admission-status",
        "pending-admission-field",
        "blind-review-packet",
    } <= _reasons(artifact, tmp_path)


def test_parity_gate_rejects_missing_or_tampered_content_addressed_review_evidence(tmp_path):
    artifact = _artifact(tmp_path)
    digest = artifact["blind_editorial_review"]["provenance"]["artifact_ref"].removeprefix("sha256:")
    review = tmp_path / f"blind-review-unblind-{digest}.json"
    review.write_text("{}", encoding="utf-8")
    assert "blind-review-evidence" in _reasons(artifact, tmp_path)

    artifact = _artifact(tmp_path)
    digest = artifact["blind_editorial_review"]["provenance"]["artifact_ref"].removeprefix("sha256:")
    (tmp_path / f"blind-review-unblind-{digest}.json").unlink()
    assert "blind-review-evidence" in _reasons(artifact, tmp_path)


def test_parity_gate_rejects_selected_reasoning_that_differs_from_runtime_librarian_setting(tmp_path):
    artifact = _artifact(tmp_path)
    for run in artifact["runs"][1:]:
        run["runtime"]["reasoning_level"] = "low"
        for case in run["case_results"]:
            case["runtime"]["reasoning_level"] = "low"

    assert {"runtime-production-reasoning", "selected-production-reasoning"} <= _reasons(artifact, tmp_path)


def test_parity_gate_rejects_a_stigmergy_run_with_a_different_output_ceiling(tmp_path):
    artifact = _artifact(tmp_path)
    for run in artifact["runs"][1:]:
        run["runtime"]["max_tokens"] = 65536
        for case in run["case_results"]:
            case["runtime"]["max_tokens"] = 65536

    assert "runtime-output-ceiling" in _reasons(artifact, tmp_path)
