import copy
import hashlib
import json
from dataclasses import replace
from pathlib import Path

from evals.filing import planner_eval
from evals.filing import worktree as eval_worktree
from evals.filing.constants import PRODUCTION_EQUIVALENT_MODE
from evals.filing.parity import current_release_inputs, evaluate
from stigmergy.knowledge.plan import EntityProposal, FilingPlan, PageMutation

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
    semantic = planner_eval.score(plan, case, source_text=source_text)
    with eval_worktree.prepared(
        case,
        source_text,
        template=str(EXPECTED.repo_root / "evals" / "filing" / "repo"),
    ) as worktree:
        gates = eval_worktree.apply_and_gate(worktree, plan)
    raw_gates = {
        **{gate: semantic[gate]["passed"] for gate in sorted(semantic) if gate != "passed"},
        "writer": gates["passed"],
    }
    payload = {
        "brain_prompt": copy.deepcopy(BRAIN_PROMPT) if implementation == "stigmergy" else None,
        "case_sha256": EXPECTED.source_cases[case_id],
        "fixture_sha256": EXPECTED.source_fixtures[case_id],
        "plan": plan.model_dump(mode="json"),
        "effective_plan": plan.model_dump(mode="json"),
        "score": semantic,
        "gates": gates,
        "raw_gates": raw_gates,
    }
    _PAYLOADS[key] = copy.deepcopy(payload)
    return payload


def _case_result(case_id: str, repeat: int, *, implementation: str, passed: bool, runtime: dict) -> dict:
    payload = _payload(case_id, implementation=implementation, passing=passed)
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
        "configured_max_turns": 2,
        "model_requests": 1,
        "planning_model_requests": 1,
        "repair_model_requests": 0,
        "schema_retry_count": 0,
        "semantic_repair_count": 0,
        "elapsed_ms": 42 + repeat,
        "usage": {"requests": 1},
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
        {"model": "openai/gpt-5.4", "reasoning_level": level, "provider": "azure"}
        if implementation == "stigmergy"
        else {"model": "fixture", "reasoning_level": level, "provider": "fixture"}
    )
    return {
        "implementation": implementation,
        "run_id": run_id,
        "runtime": runtime,
        "execution": {"mode": PRODUCTION_EQUIVALENT_MODE, "configured_max_turns": 2},
        "provenance": provenance,
        "case_results": [
            _case_result(case_id, repeat, implementation=implementation, passed=passed, runtime=runtime)
            for case_id in sorted(EXPECTED.source_cases)
        ],
    }


def _matrix_item(level: str, *, passed: bool, repeats: int = 1) -> dict:
    return {
        "reasoning_level": level,
        "runtime": {"model": "openai/gpt-5.4", "reasoning_level": level, "provider": "azure"},
        "runs": [
            _run("stigmergy", f"matrix-{level}-{repeat}", level, repeat, passed=passed)
            for repeat in range(1, repeats + 1)
        ],
        "passed": passed,
    }


def _artifact() -> dict:
    selected = [_run("stigmergy", f"matrix-high-{repeat}", "high", repeat, passed=True) for repeat in range(1, 4)]
    return {
        "schema_version": 3,
        "corpus_sha256": CORPUS,
        "initial_graph_ref": INITIAL,
        "stigmergy_commit": EXPECTED.commit,
        "librarian_skill_sha256": EXPECTED.librarian_skill_sha256,
        "brain_prompt": copy.deepcopy(BRAIN_PROMPT),
        "source_cases": [{"id": key, "sha256": value} for key, value in EXPECTED.source_cases.items()],
        "source_fixtures": [{"id": key, "sha256": value} for key, value in EXPECTED.source_fixtures.items()],
        "runs": [
            _run("hippocampus", "hippocampus-reference-1", "high", 1, passed=True),
            *selected,
        ],
        "reasoning_matrix": [
            _matrix_item("minimal", passed=False),
            _matrix_item("low", passed=False),
            _matrix_item("medium", passed=False),
            {
                "reasoning_level": "high",
                "runtime": {"model": "openai/gpt-5.4", "reasoning_level": "high", "provider": "azure"},
                "runs": copy.deepcopy(selected),
                "passed": True,
            },
        ],
        "blind_editorial_review": {
            "verdict": "no_material_stigmergy_regression",
            "provenance": {
                "reviewer": "fixture",
                "method": "blind-pairwise",
                "artifact_ref": "fixture-review",
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


def _rebind_review(artifact: dict) -> None:
    artifact["blind_editorial_review"]["provenance"]["runs"] = {
        implementation: sorted(run["run_id"] for run in artifact["runs"] if run["implementation"] == implementation)
        for implementation in ("hippocampus", "stigmergy")
    }


def _reasons(artifact: dict) -> set[str]:
    result = evaluate(artifact, expected=EXPECTED)
    assert result["passed"] is False
    return {item["reason"] for item in result["failures"]}


def test_parity_gate_accepts_complete_case_level_evidence_and_three_selected_repeats():
    assert evaluate(_artifact(), expected=EXPECTED)["passed"] is True


def test_parity_gate_rejects_aggregate_only_or_missing_case_evidence():
    artifact = _artifact()
    artifact["runs"][0].pop("case_results")
    assert "aggregate-only-evidence" in _reasons(artifact)

    artifact = _artifact()
    artifact["runs"][1]["case_results"].pop()
    assert "missing-case-result" in _reasons(artifact)


def test_parity_gate_rejects_duplicate_or_failed_case_repeats():
    artifact = _artifact()
    artifact["runs"][1]["case_results"].append(copy.deepcopy(artifact["runs"][1]["case_results"][0]))
    assert "duplicate-case-result" in _reasons(artifact)

    artifact = _artifact()
    artifact["runs"][1]["case_results"][0]["raw_gates"]["writer"] = False
    assert {"case-failed", "case-payload"} <= _reasons(artifact)


def test_parity_gate_requires_selected_production_equivalent_three_repeat_evidence():
    artifact = _artifact()
    artifact["runs"][1]["execution"]["mode"] = "planner-only"
    assert "production-equivalence" in _reasons(artifact)

    artifact = _artifact()
    artifact["runs"].pop()
    artifact["reasoning_matrix"][-1]["runs"].pop()
    _rebind_review(artifact)
    assert {"selected-reasoning-insufficient-repeats", "selected-run-mismatch"} <= _reasons(artifact)


def test_parity_gate_requires_a_complete_lowest_passing_reasoning_matrix():
    artifact = _artifact()
    artifact["reasoning_matrix"][1] = _matrix_item("low", passed=True)
    assert "reasoning-not-lowest-passing" in _reasons(artifact)

    artifact = _artifact()
    artifact["reasoning_matrix"].pop(1)
    assert "reasoning-matrix-incomplete" in _reasons(artifact)


def test_parity_gate_rejects_prompt_drift_and_nonimmutable_output_metadata():
    artifact = _artifact()
    artifact["brain_prompt"]["sha256"] = "abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789"
    assert "candidate-brain-prompt" in _reasons(artifact)

    artifact = _artifact()
    artifact["runs"][1]["case_results"][0]["output"]["artifact_ref"] = "mutable-path"
    assert "case-output" in _reasons(artifact)


def test_parity_gate_rejects_tampered_payload_hash_score_and_gates():
    artifact = _artifact()
    artifact["runs"][1]["case_results"][0]["payload"]["effective_plan"]["summary"] = "tampered"
    assert {"case-payload", "case-output-hash"} & _reasons(artifact)

    artifact = _artifact()
    artifact["runs"][1]["case_results"][0]["score"]["passed"] = False
    assert "case-payload" in _reasons(artifact)

    artifact = _artifact()
    artifact["runs"][1]["case_results"][0]["gates"]["passed"] = False
    assert "case-payload" in _reasons(artifact)
