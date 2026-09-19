import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import sys

import pytest

from evals.filing import parity, planner_eval
from evals.filing import worktree as eval_worktree
from stigmergy.knowledge.plan import (
    EntityProposal,
    ExistingPageRelation,
    FilingPlan,
    GraphEntity,
    GraphShape,
    GraphSubject,
    GraphTopology,
    PageMutation,
)
from stigmergy.knowledge.planner import (
    PlanRun,
    graph_shape_violations,
    graph_topology_violations,
)

ROOT = pathlib.Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy"
DEPLOY_SCRIPT = ROOT / "scripts" / "deploy_staging.sh"
EMPTY_DEFAULTS = {
    "identities.json": {},
    "entity-registry.json": {"version": 1, "entities": {}, "redirects": {}},
    "slack-channels.json": {},
}
ROSTER = {
    "someone@example.com": {
        "display_name": "Someone",
        "groups": ["brain-admins", "finance"],
        "default_audience": None,
    }
}
REGISTRY = {"version": 1, "entities": {}, "redirects": {}}
CHANNELS = {"C0123456789": ["finance"]}


class _RecordedRevisionPlanner:
    def __init__(self, plan: FilingPlan):
        self.plan = plan

    def revise(self, **_kwargs) -> PlanRun:
        return PlanRun(self.plan, model_requests=1)


@pytest.mark.parametrize("name, expected", sorted(EMPTY_DEFAULTS.items()))
def test_committed_deploy_controls_are_fail_closed_defaults(name, expected):
    assert json.loads((DEPLOY / name).read_text(encoding="utf-8")) == expected


def test_deploy_directory_contains_only_known_artifacts():
    assert {path.name for path in DEPLOY.iterdir()} == {
        *EMPTY_DEFAULTS,
        "slack-app-manifest.json",
    }


def _git(cwd: pathlib.Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _commit(repo: pathlib.Path, message: str) -> str:
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _artifact_path(repo: pathlib.Path) -> pathlib.Path:
    return repo.parent / f"{repo.name}-parity-result.json"


def _sidecar_path(repo: pathlib.Path, name: str) -> pathlib.Path:
    return repo.parent / f"{repo.name}-{name}"


def _run_deploy(
    tmp_path: pathlib.Path,
    *,
    roster=ROSTER,
    python: str | None = sys.executable,
    parity_artifact: str = "valid",
    dirty_platform: bool = False,
    brain_drift: bool = False,
) -> tuple[subprocess.CompletedProcess[str], pathlib.Path, pathlib.Path]:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    shutil.copy2(DEPLOY_SCRIPT, scripts / DEPLOY_SCRIPT.name)
    refresh_script = scripts / "refresh_staging_checkout.sh"
    refresh_script.write_text(
        '#!/usr/bin/env bash\nset -euo pipefail\nprintf "staging-refresh: root=%s head=%s\\n" "$1" "$STAGING_SHA"\n',
        encoding="utf-8",
    )
    refresh_script.chmod(0o755)

    candidate_filing = tmp_path / "evals" / "filing"
    candidate_filing.parent.mkdir(parents=True)
    shutil.copytree(ROOT / "evals" / "filing", candidate_filing)
    candidate_skill = tmp_path / "src" / "stigmergy" / "knowledge"
    candidate_skill.mkdir(parents=True)
    shutil.copy2(
        ROOT / "src" / "stigmergy" / "knowledge" / "librarian_skill.md",
        candidate_skill / "librarian_skill.md",
    )

    deploy = tmp_path / "deploy"
    deploy.mkdir()
    for name, value in EMPTY_DEFAULTS.items():
        (deploy / name).write_text(json.dumps(value) + "\n", encoding="utf-8")
    probe = deploy / "unmanaged" / "tracked.txt"
    probe.parent.mkdir(parents=True)
    probe.write_text("keep\n", encoding="utf-8")

    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test User")
    candidate_commit = _commit(tmp_path, "candidate parity inputs")

    knowledge = _sidecar_path(tmp_path, "knowledge")
    subprocess.run(["git", "init", "-q", "-b", "main", str(knowledge)], check=True)
    _git(knowledge, "config", "user.email", "test@example.com")
    _git(knowledge, "config", "user.name", "Test User")
    ops = knowledge / "ops"
    ops.mkdir(parents=True)
    (ops / "identities.json").write_text(json.dumps(roster), encoding="utf-8")
    (ops / "entity-registry.json").write_text(json.dumps(REGISTRY), encoding="utf-8")
    (ops / "slack-channels.json").write_text(json.dumps(CHANNELS), encoding="utf-8")
    brain_skill = knowledge / ".claude" / "skills" / "librarian" / "SKILL.md"
    brain_skill.parent.mkdir(parents=True)
    brain_skill.write_bytes((candidate_skill / "librarian_skill.md").read_bytes())
    staging_sha = _commit(knowledge, "test controls")

    seen = _sidecar_path(tmp_path, "seen")
    bin_dir = _sidecar_path(tmp_path, "bin")
    bin_dir.mkdir()
    fly = bin_dir / "fly"
    fly.write_text(
        '#!/usr/bin/env bash\nif [ "$1" = "deploy" ]; then mkdir -p "$SEEN"; cp deploy/*.json "$SEEN"/; fi\nexit 0\n',
        encoding="utf-8",
    )
    fly.chmod(0o755)

    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "STIGMERGY_REPO": str(ops.parent),
        "STAGING_SHA": staging_sha,
        "SEEN": str(seen),
    }
    artifact = _artifact_path(tmp_path)
    if parity_artifact != "absent":
        payload = _parity_artifact(tmp_path, candidate_commit, knowledge, staging_sha)
        if parity_artifact == "stale":
            payload["stigmergy_commit"] = "a" * 40
        elif parity_artifact == "mismatched":
            payload["librarian_skill_sha256"] = "b" * 64
        artifact.write_text(json.dumps(payload), encoding="utf-8")
        env["STIGMERGY_PARITY_ARTIFACT"] = str(artifact)
    if dirty_platform:
        with (candidate_skill / "librarian_skill.md").open("a", encoding="utf-8") as output:
            output.write("\nDirty after parity recording.\n")
    if brain_drift:
        with brain_skill.open("ab") as output:
            output.write(b"\n")
    if python is not None:
        env["STIGMERGY_PYTHON"] = python
    else:
        env["STIGMERGY_PYTHON"] = str(tmp_path / "missing-python")
    result = subprocess.run(
        ["bash", str(scripts / DEPLOY_SCRIPT.name)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return result, deploy, seen


def _raw_gates(*, passed=True):
    return {gate: "passed" if passed else "failed" for gate in parity.REQUIRED_SEMANTIC_GATES}


def _canonical(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _write_review_bundle(root: pathlib.Path, artifact: dict) -> None:
    root.mkdir(parents=True, exist_ok=True)
    baseline = next(run for run in artifact["runs"] if run["implementation"] == "hippocampus")
    selected = [run for run in artifact["runs"] if run["implementation"] == "stigmergy"]
    baseline_cases = {case["case_id"]: case for case in baseline["case_results"]}
    comparisons, mappings, pairs, regressions = [], [], [], []
    for run in selected:
        for case in run["case_results"]:
            comparison_id = f"comparison-{case['case_id']}-{run['run_id']}"
            labels = []
            for implementation, candidate_run, candidate_case in (
                ("hippocampus", baseline, baseline_cases[case["case_id"]]),
                ("stigmergy", run, case),
            ):
                label = "candidate-" + hashlib.sha256(f"{comparison_id}:{implementation}".encode()).hexdigest()[:16]
                labels.append(
                    {
                        "label": label,
                        "case_output_sha256": candidate_case["output"]["sha256"],
                        "effective_pages_sha256": hashlib.sha256(
                            _canonical(candidate_case["payload"]["effective_plan"])
                        ).hexdigest(),
                        "draft_plan_sha256": hashlib.sha256(_canonical(candidate_case["payload"]["plan"])).hexdigest(),
                        "reviewed_plan_sha256": (
                            hashlib.sha256(_canonical(candidate_case["payload"]["reviewed_plan"])).hexdigest()
                            if candidate_case["payload"]["reviewed_plan"] is not None
                            else None
                        ),
                        "repair_plan_sha256": candidate_case["payload"]["repair_plan_sha256"],
                        "semantic_revision": candidate_case["payload"]["semantic_revision"],
                        "implementation": implementation,
                        "run_id": candidate_run["run_id"],
                    }
                )
            comparisons.append(
                {
                    "comparison_id": comparison_id,
                    "candidates": [
                        {key: value for key, value in label.items() if key not in {"implementation", "run_id"}}
                        for label in labels
                    ],
                }
            )
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
            pairs.append(
                {
                    "pair_id": comparison_id,
                    "judgments": {"fixture": {"winner": "tie", "reason": "Fixture."}},
                    "overall": {"winner": "tie", "reason": "Fixture."},
                    "material_regression": {"side": labels[0]["label"], "reason": regression["reason"]},
                }
            )
    packet = {"schema_version": 1, "comparisons": comparisons}
    packet_bytes = _canonical(packet)
    packet_digest = hashlib.sha256(packet_bytes).hexdigest()
    packet_name = f"blind-review-packet-{packet_digest}.json"
    (root / packet_name).write_bytes(packet_bytes)
    mapping = {"schema_version": 1, "packet_sha256": packet_digest, "mapping": mappings}
    mapping_bytes = _canonical(mapping)
    mapping_digest = hashlib.sha256(mapping_bytes).hexdigest()
    mapping_name = f"blind-review-mapping-{mapping_digest}.secret.json"
    (root / mapping_name).write_bytes(mapping_bytes)
    response = {
        "reviewer_identity": "fixture-reviewer",
        "method": "fixture-blind-pairwise",
        "packet_sha256": hashlib.sha256(packet_bytes).hexdigest(),
        "pairs": pairs,
    }
    response_bytes = _canonical(response)
    response_digest = hashlib.sha256(response_bytes).hexdigest()
    response_name = f"blind-reviewer-response-{response_digest}.json"
    (root / response_name).write_bytes(response_bytes)
    review = {
        "schema_version": "blind-editorial-review-unblind-v2",
        "verdict": "no_material_stigmergy_regression",
        "reviewer_response": {
            "path": response_name,
            "raw_sha256": hashlib.sha256(response_bytes).hexdigest(),
            "canonical_sha256": response_digest,
            "artifact_ref": f"sha256:{response_digest}",
        },
        "packet": {
            "path": packet_name,
            "raw_sha256": hashlib.sha256(packet_bytes).hexdigest(),
            "canonical_sha256": packet_digest,
        },
        "mapping": {
            "path": mapping_name,
            "raw_sha256": hashlib.sha256(mapping_bytes).hexdigest(),
            "canonical_sha256": mapping_digest,
        },
        "unblinding": {
            "material_regressions": regressions,
            "stigmergy_material_regressions": [],
        },
    }
    review_bytes = _canonical(review)
    review_digest = hashlib.sha256(review_bytes).hexdigest()
    (root / f"blind-review-unblind-{review_digest}.json").write_bytes(review_bytes)
    artifact["admission_status"] = "passed"
    artifact["blind_review_packet"] = {
        "status": "completed",
        "raw_sha256": hashlib.sha256(packet_bytes).hexdigest(),
        "canonical_sha256": packet_digest,
    }
    artifact["blind_editorial_review"]["provenance"]["artifact_ref"] = f"sha256:{review_digest}"


def _legacy_parity_artifact(repo: pathlib.Path, commit: str) -> dict:
    expected = parity.current_release_inputs(repo)
    assert expected.commit == commit
    corpus = "0123456789abcdef" * 4
    initial = "0123456789abcdef0123456789abcdef01234567"
    provenance = {
        "corpus_sha256": corpus,
        "initial_graph_ref": initial,
        "source_cases": expected.source_cases,
        "commit": expected.commit,
        "librarian_skill_sha256": expected.librarian_skill_sha256,
    }

    def case_result(case_id: str, repeat: int, passed: bool, runtime: dict) -> dict:
        digest = f"{repeat + len(case_id):064x}"
        return {
            "case_id": case_id,
            "runtime": runtime,
            "execution_mode": "production-equivalent",
            "configured_max_turns": 3,
            "model_requests": 1,
            "planning_model_requests": 1,
            "semantic_revision_required": False,
            "semantic_revision_attempted": False,
            "semantic_revision_applied": False,
            "semantic_revision_model_requests": 0,
            "repair_model_requests": 0,
            "schema_retry_count": 0,
            "semantic_repair_count": 0,
            "elapsed_ms": 1,
            "usage": {"requests": 1},
            "score": {"passed": passed},
            "gates": {"passed": passed},
            "raw_gates": _raw_gates(passed=passed),
            "output": {"sha256": digest, "artifact_ref": f"sha256:{digest}"},
        }

    def run(implementation: str, run_id: str, level: str, repeat: int, passed: bool) -> dict:
        runtime = (
            {
                "model": "openai/gpt-oss-120b",
                "reasoning_level": level,
                "provider": "cerebras",
                "max_tokens": 40960,
                "temperature": 0,
            }
            if implementation == "stigmergy"
            else {"model": "fixture", "reasoning_level": level, "provider": "fixture"}
        )
        run_provenance = (
            provenance
            if implementation == "stigmergy"
            else {key: value for key, value in provenance.items() if key not in {"commit", "librarian_skill_sha256"}}
        )
        return {
            "implementation": implementation,
            "run_id": run_id,
            "runtime": runtime,
            "execution": {"mode": "production-equivalent", "configured_max_turns": 3},
            "provenance": run_provenance,
            "case_results": [
                case_result(case_id, repeat, passed, runtime) for case_id in sorted(expected.source_cases)
            ],
        }

    def matrix(level: str, passed: bool, repeats: int = 1) -> dict:
        return {
            "reasoning_level": level,
            "runtime": {
                "model": "openai/gpt-oss-120b",
                "reasoning_level": level,
                "provider": "cerebras",
                "max_tokens": 40960,
                "temperature": 0,
            },
            "runs": [
                run("stigmergy", f"matrix-{level}-{repeat}", level, repeat, passed) for repeat in range(1, repeats + 1)
            ],
            "passed": passed,
        }

    selected = [run("stigmergy", f"matrix-high-{repeat}", "high", repeat, True) for repeat in range(1, 4)]

    return {
        "schema_version": 2,
        "corpus_sha256": corpus,
        "initial_graph_ref": initial,
        "stigmergy_commit": expected.commit,
        "librarian_skill_sha256": expected.librarian_skill_sha256,
        "source_cases": [{"id": key, "sha256": value} for key, value in expected.source_cases.items()],
        "runs": [
            run("hippocampus", "hippocampus-recorded-run", "medium", 1, True),
            *selected,
        ],
        "reasoning_matrix": [
            matrix("minimal", False),
            matrix("low", False),
            matrix("medium", False),
            {
                "reasoning_level": "high",
                "runtime": {
                    "model": "openai/gpt-oss-120b",
                    "reasoning_level": "high",
                    "provider": "cerebras",
                    "max_tokens": 40960,
                    "temperature": 0,
                },
                "runs": selected,
                "passed": True,
            },
        ],
        "blind_editorial_review": {
            "verdict": "no_material_stigmergy_regression",
            "provenance": {
                "reviewer": "recorded-reviewer",
                "method": "blind-pairwise",
                "artifact_ref": "recorded-review",
                "corpus_sha256": corpus,
                "initial_graph_ref": initial,
                "source_cases": expected.source_cases,
                "runs": {
                    "hippocampus": ["hippocampus-recorded-run"],
                    "stigmergy": ["matrix-high-1", "matrix-high-2", "matrix-high-3"],
                },
            },
        },
    }


def _plan_for_case(case_id: str) -> FilingPlan:
    source = "sources/2026/09/00000000-0000-4000-8000-000000000001.md"
    harness_body = (
        "# Harness Engineering\n\n"
        "A model alone is not an agentic system: the model supplies reasoning while the agent harness "
        "turns its text into work. The six capabilities are tools let the model request actions; a loop "
        "repeats decide, act, observe; memory/state preserves work; context selection chooses what the "
        "model sees; a working environment provides an isolated workspace; and a clear objective and "
        f"verification establish external acceptance criteria. (Source: `{source}`)\n\n"
        "The three trust capabilities are permissions and limits, observability through traces of context "
        "and outcomes, and evals using stable evaluation tasks. Skills, MCP, subagents, and long-term "
        "memory extend the same design. Santi (@santtiagom_) authored this explanation. OpenAI built one "
        "million lines and 1,500 pull requests; LangChain rose from rank 30 to the top 5 on Terminal Bench; "
        "Anthropic showed a polished but broken application versus a working app under different harness "
        f"configurations. (Source: `{source}`)"
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
                        "# Decision Trace Quality\n\nDecision trace quality records the evidence behind a "
                        "decision with a concise rationale so later readers can inspect the result. "
                        "Mira Chen introduced the method and Northstar Signal Lab measured Recall at Five. (Source: "
                        "`sources/2026/09/00000000-0000-4000-8000-000000000002.md`)\n\n"
                        "Within an [[Agent Harness]], the method makes automated choices reviewable by "
                        "preserving why an action was selected alongside its evidence. The source does not "
                        "report an acceptance threshold or independent verification for Recall at Five. (Source: "
                        "`sources/2026/09/00000000-0000-4000-8000-000000000002.md`)"
                    ),
                    entities=("Mira Chen", "Northstar Signal Lab"),
                    reason="The source explains the concept.",
                ),
            ),
        )
    entities = (
        EntityProposal(
            name="Santi",
            entity_type="person",
            aliases=("@santtiagom_",),
        ),
        *tuple(EntityProposal(name=name, entity_type="organization") for name in ("OpenAI", "Anthropic", "LangChain")),
    )
    mutation = PageMutation(
        action="create",
        role="concept",
        title="Harness Engineering",
        body=(
            harness_body + "\n\nClaude Code and Codex are coding-agent environments named by the source. "
            f"(Source: `{source}`)"
        ),
        entities=("Santi", "OpenAI", "Anthropic", "LangChain"),
        reason="The source explains the concept.",
    )
    if case_id == "harness_engineering":
        return FilingPlan(
            summary="Filed Harness Engineering and Agent Harness separately.",
            entities=entities,
            mutations=(
                mutation.model_copy(update={"body": mutation.body.replace("agent harness", "[[Agent Harness]]", 1)}),
                PageMutation(
                    action="create",
                    role="concept",
                    title="Agent Harness",
                    body=(
                        "# Agent Harness\n\nAn agent harness is the operational system around a model. "
                        "It is designed and improved through [[Harness Engineering]]. "
                        f"(Source: `{source}`)\n\n"
                        "It supplies the execution loop, state, environment, objectives, and verification that "
                        "turn model output into observable work rather than treating the model as the whole "
                        f"system. (Source: `{source}`)"
                    ),
                    entities=(),
                    reason="The source independently defines the target system.",
                ),
            ),
        )
    return FilingPlan(
        summary="Enriched the seeded harness graph.",
        entities=entities,
        mutations=(
            mutation.model_copy(update={"body": harness_body.replace("agent harness", "[[Agent Harness]]")}),
            PageMutation(
                action="update",
                path="wiki/concepts/Agent Harness.md",
                body=(
                    "# Agent Harness\n\nAn [[Agent Harness]] is the operating layer enriched by "
                    "[[Harness Engineering]]. Its task loop coordinates tools and feedback while memory and "
                    f"context assembly preserve the state needed for continued work. (Source: `{source}`)\n\n"
                    "The harness handles errors and exposes telemetry so operators can inspect outcomes rather "
                    "than assuming that the prompt is the policy. Santi, OpenAI, Anthropic, and LangChain "
                    f"illustrate its leverage. (Source: `{source}`)"
                ),
                entities=(),
                reason="Existing concept gains evidence.",
            ),
        ),
    )


def _graph_shape_for_case(case_id: str, *, stigmergy: bool):
    if not stigmergy:
        return None, None, None
    if case_id == "decision_trace_quality":
        shape = GraphShape(
            summary="File the method and enrich its credited identities.",
            subjects=(
                GraphSubject(
                    title="Decision Trace Quality",
                    title_evidence="Decision Trace Quality",
                    name_variants=("Decision Trace Quality",),
                    role="concept",
                    abstraction="method",
                    abstraction_evidence="a method for reviewing",
                    significance="Makes automated decisions reproducible.",
                    required_terms=("concise rationale", "Recall at Five"),
                    entities=(
                        GraphEntity(
                            name="Mira Chen",
                            entity_type="person",
                            aliases=(),
                            relationship_kind="responsible",
                            relationship="Mira Chen introduced Decision Trace Quality",
                            evidence_terms=(),
                        ),
                        GraphEntity(
                            name="Northstar Signal Lab",
                            entity_type="organization",
                            aliases=(),
                            relationship_kind="produced_evidence",
                            relationship="Northstar Signal Lab measured Recall at Five",
                            evidence_terms=("Recall at Five",),
                        ),
                    ),
                ),
            ),
            existing_relations=(),
        )
    else:
        relations = (
            (
                ExistingPageRelation(
                    source_subject="Agent Harness",
                    path="wiki/concepts/Agent Harness.md",
                    relation="same_subject",
                    reason="The seeded page has the exact canonical subject title.",
                ),
            )
            if case_id == "harness_engineering_seeded"
            else ()
        )
        shape = GraphShape(
            summary="Keep the engineering practice separate from its target system.",
            subjects=(
                GraphSubject(
                    title="Harness Engineering",
                    title_evidence="Harness Engineering",
                    name_variants=("Harness Engineering",),
                    role="concept",
                    abstraction="practice",
                    abstraction_evidence="practice of designing and improving",
                    significance="Improves agent reliability through harness design.",
                    required_terms=(
                        "Skills",
                        "MCP",
                        "subagents",
                        "long-term memory",
                        "one million lines",
                        "1,500 pull requests",
                        "rank 30",
                        "top 5",
                        "polished but broken",
                        "working app",
                    ),
                    entities=(
                        GraphEntity(
                            name="Santi",
                            entity_type="person",
                            aliases=("@santtiagom_",),
                            relationship_kind="authored",
                            relationship="Santi authored this explanation",
                            evidence_terms=(),
                        ),
                        GraphEntity(
                            name="OpenAI",
                            entity_type="organization",
                            aliases=(),
                            relationship_kind="produced_evidence",
                            relationship="OpenAI built one million lines and 1,500 pull requests",
                            evidence_terms=("one million lines", "1,500 pull requests"),
                        ),
                        GraphEntity(
                            name="LangChain",
                            entity_type="organization",
                            aliases=(),
                            relationship_kind="produced_evidence",
                            relationship="LangChain rose from rank 30 to the top 5",
                            evidence_terms=("rank 30", "top 5"),
                        ),
                        GraphEntity(
                            name="Anthropic",
                            entity_type="organization",
                            aliases=(),
                            relationship_kind="produced_evidence",
                            relationship="Anthropic showed a polished but broken application versus a working app",
                            evidence_terms=("polished but broken", "working app"),
                        ),
                    ),
                ),
                GraphSubject(
                    title="Agent Harness",
                    title_evidence="Agent Harness",
                    name_variants=("Agent Harness",),
                    role="concept",
                    abstraction="system",
                    abstraction_evidence="operational system",
                    significance="Turns model choices into reliable work.",
                    required_terms=("agent harness",),
                    entities=(),
                ),
            ),
            existing_relations=relations,
        )
    topology = GraphTopology.model_validate(
        {
            "summary": shape.summary,
            "subjects": [subject.model_dump(exclude={"required_terms", "entities"}) for subject in shape.subjects],
            "existing_relations": [relation.model_dump(mode="json") for relation in shape.existing_relations],
        }
    )
    return shape, topology, topology


def _parity_artifact(
    repo: pathlib.Path,
    commit: str,
    brain_root: pathlib.Path,
    brain_commit: str,
) -> dict:
    expected = parity.current_release_inputs(
        repo,
        brain_root=brain_root,
        brain_commit=brain_commit,
    )
    corpus = "0123456789abcdef" * 4
    initial = "0123456789abcdef0123456789abcdef01234567"

    def case_result(case_id: str, runtime: dict, brain_prompt: dict | None, passing: bool) -> dict:
        case = planner_eval.load_case(expected.case_paths[case_id])
        source_text = expected.fixture_paths[case_id].read_text(encoding="utf-8")
        plan = _plan_for_case(case_id) if passing else FilingPlan(summary="Deliberately failing result.")
        graph_shape, graph_shape_draft, graph_shape_review = _graph_shape_for_case(
            case_id,
            stigmergy=brain_prompt is not None,
        )
        graph_shape_failures = (
            list(
                graph_topology_violations(graph_shape_review, graph_shape)
                + graph_shape_violations(
                    graph_shape,
                    plan,
                    source_path=case["source_path"],
                )
            )
            if graph_shape is not None and graph_shape_review is not None
            else []
        )
        graph_semantic_reviewed = graph_shape is not None and not graph_shape_failures
        planning_model_requests = 5 if brain_prompt is not None else 1
        active_plan = plan
        revision = {"required": False, "attempted": False, "applied": False, "model_requests": 0}
        with eval_worktree.prepared(
            case,
            source_text,
            template=str(expected.repo_root / "evals" / "filing" / "repo"),
        ) as worktree:
            gates, active_plan = eval_worktree.apply_with_production_repair(
                worktree,
                plan,
                _RecordedRevisionPlanner(plan),
                planning_model_requests=planning_model_requests,
                max_turns=parity.PRODUCTION_MAX_TURNS,
                graph_shape=graph_shape,
                semantic_reviewed=graph_semantic_reviewed,
                return_plan=True,
            )
            revision = {
                "required": gates["semantic_revision_required"],
                "attempted": gates["semantic_revision_attempted"],
                "applied": gates["semantic_revision_applied"],
                "model_requests": gates["semantic_revision_model_requests"],
            }
            effective = eval_worktree.effective_plan(worktree, active_plan) if gates["passed"] else active_plan
        score = planner_eval.score(effective, case, source_text=source_text)
        graph_shape_score = planner_eval.score_graph_shape(graph_shape, case)
        score["graph_shape"] = graph_shape_score
        score["passed"] = bool(score["passed"] and graph_shape_score["passed"])
        raw_gates = {
            **{gate: score[gate]["passed"] for gate in sorted(score) if gate != "passed"},
            "writer": gates["passed"],
        }
        payload = {
            "brain_prompt": brain_prompt,
            "case_sha256": expected.source_cases[case_id],
            "fixture_sha256": expected.source_fixtures[case_id],
            "plan": plan.model_dump(mode="json"),
            "graph_shape": (graph_shape.model_dump(mode="json") if graph_shape is not None else None),
            "graph_shape_draft": (graph_shape_draft.model_dump(mode="json") if graph_shape_draft is not None else None),
            "graph_shape_review": (
                graph_shape_review.model_dump(mode="json") if graph_shape_review is not None else None
            ),
            "graph_shape_violations": graph_shape_failures,
            "reviewed_plan": active_plan.model_dump(mode="json") if revision["applied"] else None,
            "semantic_revision": revision,
            "repair_plan": None,
            "repair_plan_sha256": None,
            "effective_plan": effective.model_dump(mode="json"),
            "score": score,
            "gates": gates,
            "raw_gates": raw_gates,
        }
        digest = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        model_requests = planning_model_requests + revision["model_requests"]
        return {
            "case_id": case_id,
            "case_sha256": expected.source_cases[case_id],
            "fixture_sha256": expected.source_fixtures[case_id],
            "brain_prompt": brain_prompt,
            "runtime": runtime,
            "execution_mode": "production-equivalent",
            "configured_max_turns": parity.PRODUCTION_MAX_TURNS,
            "model_requests": model_requests,
            "planning_model_requests": planning_model_requests,
            "graph_shape_model_requests": 1 if brain_prompt is not None else 0,
            "graph_shape_review_model_requests": 1 if brain_prompt is not None else 0,
            "graph_shape_enrichment_model_requests": 1 if brain_prompt is not None else 0,
            "compilation_model_requests": 1 if brain_prompt is not None else 0,
            "graph_semantic_review_model_requests": 1 if brain_prompt is not None else 0,
            "graph_semantic_reviewed": bool(graph_semantic_reviewed),
            "semantic_revision_required": revision["required"],
            "semantic_revision_attempted": revision["attempted"],
            "semantic_revision_applied": revision["applied"],
            "semantic_revision_model_requests": revision["model_requests"],
            "repair_model_requests": 0,
            "repair_plan_sha256": None,
            "schema_retry_count": 0,
            "semantic_repair_count": 0,
            "elapsed_ms": 1,
            "usage": {"requests": model_requests},
            "score": score,
            "gates": gates,
            "raw_gates": raw_gates,
            "output": {"sha256": digest, "artifact_ref": f"sha256:{digest}"},
            "payload": payload,
        }

    def run(implementation: str, run_id: str, level: str, *, passing: bool = True) -> dict:
        runtime = (
            {
                "model": "openai/gpt-oss-120b",
                "reasoning_level": level,
                "provider": "cerebras",
                "max_tokens": 40960,
                "temperature": 0,
            }
            if implementation == "stigmergy"
            else {"model": "fixture", "reasoning_level": level, "provider": "fixture"}
        )
        provenance = {
            "corpus_sha256": corpus,
            "initial_graph_ref": initial,
            "source_cases": expected.source_cases,
            "source_fixtures": expected.source_fixtures,
        }
        if implementation == "stigmergy":
            provenance.update(
                commit=expected.commit,
                librarian_skill_sha256=expected.librarian_skill_sha256,
                brain_prompt=expected.brain_prompt,
            )
        return {
            "implementation": implementation,
            "run_id": run_id,
            "runtime": runtime,
            "execution": {
                "mode": "production-equivalent",
                "configured_max_turns": parity.PRODUCTION_MAX_TURNS,
            },
            "provenance": provenance,
            "case_results": [
                case_result(
                    case_id,
                    runtime,
                    expected.brain_prompt if implementation == "stigmergy" else None,
                    passing,
                )
                for case_id in sorted(expected.source_cases)
            ],
        }

    selected = [run("stigmergy", f"matrix-medium-{repeat}", "medium") for repeat in range(1, 4)]
    hippocampus = run("hippocampus", "hippocampus-recorded-run", "medium")
    artifact = {
        "schema_version": 5,
        "corpus_sha256": corpus,
        "initial_graph_ref": initial,
        "stigmergy_commit": expected.commit,
        "librarian_skill_sha256": expected.librarian_skill_sha256,
        "brain_prompt": expected.brain_prompt,
        "source_cases": [{"id": key, "sha256": value} for key, value in expected.source_cases.items()],
        "source_fixtures": [{"id": key, "sha256": value} for key, value in expected.source_fixtures.items()],
        "runs": [hippocampus, *selected],
        "reasoning_matrix": [
            {
                "reasoning_level": level,
                "runtime": {
                    "model": "openai/gpt-oss-120b",
                    "reasoning_level": level,
                    "provider": "cerebras",
                    "max_tokens": 40960,
                    "temperature": 0,
                },
                "runs": [run("stigmergy", f"matrix-{level}-1", level, passing=False)],
                "passed": False,
            }
            for level in ("minimal", "low")
        ]
        + [
            {
                "reasoning_level": "medium",
                "runtime": {
                    "model": "openai/gpt-oss-120b",
                    "reasoning_level": "medium",
                    "provider": "cerebras",
                    "max_tokens": 40960,
                    "temperature": 0,
                },
                "runs": selected,
                "passed": True,
            }
        ],
        "blind_editorial_review": {
            "verdict": "no_material_stigmergy_regression",
            "provenance": {
                "reviewer": "recorded-reviewer",
                "method": "blind-pairwise",
                "artifact_ref": "recorded-review",
                "corpus_sha256": corpus,
                "initial_graph_ref": initial,
                "source_cases": expected.source_cases,
                "runs": {
                    "hippocampus": [hippocampus["run_id"]],
                    "stigmergy": [item["run_id"] for item in selected],
                },
            },
        },
    }
    _write_review_bundle(repo.parent, artifact)
    return artifact


def test_deploy_bakes_all_controls_then_restores_defaults(tmp_path):
    result, deploy, seen = _run_deploy(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads((seen / "identities.json").read_text()) == ROSTER
    assert json.loads((seen / "entity-registry.json").read_text()) == REGISTRY
    assert json.loads((seen / "slack-channels.json").read_text()) == CHANNELS
    assert {name: json.loads((deploy / name).read_text()) for name in EMPTY_DEFAULTS} == EMPTY_DEFAULTS
    assert (deploy / "unmanaged" / "tracked.txt").read_text() == "keep\n"


def test_invalid_control_file_stops_deploy(tmp_path):
    result, _, seen = _run_deploy(tmp_path, roster={"someone@example.com": "*"})
    assert result.returncode == 2
    assert "refusing to bake" in result.stderr
    assert not seen.exists()


def test_missing_preflight_runtime_stops_deploy(tmp_path):
    result, _, seen = _run_deploy(tmp_path, python=None)
    assert result.returncode == 2
    assert "cannot import stigmergy" in result.stderr
    assert not seen.exists()


def test_dirty_platform_checkout_stops_deploy_before_parity_or_fly(tmp_path):
    result, _deploy, seen = _run_deploy(tmp_path, parity_artifact="stale", dirty_platform=True)

    assert result.returncode == 2
    assert "platform checkout has tracked or untracked changes" in result.stderr
    assert "recorded parity gate rejected" not in result.stderr
    assert not seen.exists()


@pytest.mark.parametrize("artifact_state", ["absent", "stale", "mismatched"])
def test_release_deploy_refuses_missing_or_candidate_mismatched_parity_evidence(tmp_path, artifact_state):
    result, _deploy, seen = _run_deploy(tmp_path, parity_artifact=artifact_state)

    assert result.returncode == 2
    assert "parity" in result.stderr
    assert not seen.exists()


def test_deploy_refuses_one_byte_drift_in_the_refreshed_brain_prompt(tmp_path):
    result, _deploy, seen = _run_deploy(tmp_path, brain_drift=True)

    assert result.returncode == 2
    assert "parity" in result.stderr
    assert not seen.exists()


@pytest.mark.parametrize("name", ["identities", "entity-registry", "slack-channels"])
def test_missing_control_file_stops_deploy(tmp_path, name):
    result, _, seen = _run_deploy(tmp_path)
    assert result.returncode == 0
    knowledge = _sidecar_path(tmp_path, "knowledge")
    knowledge_file = knowledge / "ops" / f"{name}.json"
    knowledge_file.unlink()
    staging_sha = _commit(knowledge, "remove deployed control")
    _artifact_path(tmp_path).write_text(
        json.dumps(_parity_artifact(tmp_path, _git(tmp_path, "rev-parse", "HEAD"), knowledge, staging_sha)),
        encoding="utf-8",
    )

    env = {
        **os.environ,
        "PATH": f"{_sidecar_path(tmp_path, 'bin')}{os.pathsep}{os.environ['PATH']}",
        "STIGMERGY_REPO": str(knowledge),
        "STAGING_SHA": staging_sha,
        "SEEN": str(seen),
        "STIGMERGY_PYTHON": sys.executable,
        "STIGMERGY_PARITY_ARTIFACT": str(_artifact_path(tmp_path)),
    }
    second = subprocess.run(
        ["bash", str(tmp_path / "scripts" / DEPLOY_SCRIPT.name)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert second.returncode == 2
    assert "required control file" in second.stderr
