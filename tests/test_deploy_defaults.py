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
from stigmergy.knowledge.plan import FilingPlan, PageMutation
from stigmergy.knowledge.planner import PlanRun

ROOT = pathlib.Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy"
DEPLOY_SCRIPT = ROOT / "scripts" / "deploy_staging.sh"


class _RecordedRevisionPlanner:
    def __init__(self, plan: FilingPlan):
        self.plan = plan

    def revise(self, **_kwargs) -> PlanRun:
        return PlanRun(self.plan, model_requests=1)


def test_deploy_directory_contains_only_known_artifacts():
    """The image bakes no roster: people and channels live in Postgres."""
    assert {path.name for path in DEPLOY.iterdir()} == {"slack-app-manifest.json"}


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
    python: str | None = sys.executable,
    parity_artifact: str = "valid",
    dirty_platform: bool = False,
    legal_source: pathlib.Path | None = None,
    parity_gate: str | None = None,
) -> tuple[subprocess.CompletedProcess[str], pathlib.Path, pathlib.Path]:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    shutil.copy2(DEPLOY_SCRIPT, scripts / DEPLOY_SCRIPT.name)

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
    probe = deploy / "unmanaged" / "tracked.txt"
    probe.parent.mkdir(parents=True)
    probe.write_text("keep\n", encoding="utf-8")

    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test User")
    candidate_commit = _commit(tmp_path, "candidate parity inputs")

    seen = _sidecar_path(tmp_path, "seen")
    bin_dir = _sidecar_path(tmp_path, "bin")
    bin_dir.mkdir()
    fly = bin_dir / "fly"
    fly.write_text(
        '#!/usr/bin/env bash\nif [ "$1" = "deploy" ]; then mkdir -p "$SEEN"; touch "$SEEN/deployed"; '
        'printf "%s\\n" "$@" > "$SEEN/deploy-args"; fi\nexit 0\n',
        encoding="utf-8",
    )
    fly.chmod(0o755)

    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "SEEN": str(seen),
    }
    if legal_source is not None:
        env["STIGMERGY_LEGAL_SOURCE"] = str(legal_source)
    if parity_gate is not None:
        env["STIGMERGY_PARITY_GATE"] = parity_gate
    artifact = _artifact_path(tmp_path)
    if parity_artifact != "absent":
        payload = _parity_artifact(tmp_path, candidate_commit)
        if parity_artifact == "stale":
            payload["release"]["candidate"]["commit"] = "a" * 40
        elif parity_artifact == "mismatched":
            payload["release"]["candidate"]["librarian_skill_sha256"] = "b" * 64
        elif parity_artifact == "minimal-reasoning":
            payload["runs"] = json.loads(json.dumps(payload["runs"]))
            for run in payload["runs"]:
                if run["implementation"] == "stigmergy":
                    run["runtime"]["reasoning_level"] = "minimal"
        elif parity_artifact == "tampered-blind-packet":
            packet_digest = payload["blind_review_packet"]["canonical_sha256"]
            packet_path = tmp_path.parent / f"blind-review-packet-{packet_digest}.json"
            packet = json.loads(packet_path.read_text(encoding="utf-8"))
            packet["comparisons"] = packet["comparisons"][:-1]
            packet_path.write_text(json.dumps(packet), encoding="utf-8")
        artifact.write_text(json.dumps(payload), encoding="utf-8")
        env["STIGMERGY_PARITY_ARTIFACT"] = str(artifact)
    if dirty_platform:
        with (candidate_skill / "librarian_skill.md").open("a", encoding="utf-8") as output:
            output.write("\nDirty after parity recording.\n")
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


def _entity_page(title: str, entity_type: str, who: str, fact: str, source: str, links: str, aliases=None):
    return PageMutation(
        action="create",
        role="entity",
        title=title,
        entity_type=entity_type,
        aliases=aliases,
        body=(
            f"# {title}\n\n## What / Who\n\n{who} (Source: `{source}`)\n\n"
            f"## Facts\n\n- {fact} (Source: `{source}`)\n\n## Connections\n\n{links}"
        ),
        reason="The source establishes a durable identity.",
    )


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
        "memory extend the same design. [[Robin Hale]] (@robinhale_example) authored this explanation. "
        "[[OpenAI]] built "
        "one million lines and 1,500 pull requests; [[LangChain]] rose from rank 30 to the top 5 on Terminal "
        "Bench; [[Anthropic]] showed a polished but broken application versus a working app under different "
        f"harness configurations. (Source: `{source}`)"
    )
    if case_id == "decision_trace_quality":
        source = "sources/2026/09/00000000-0000-4000-8000-000000000002.md"
        return FilingPlan(
            summary="Filed decision trace quality.",
            mutations=(
                PageMutation(
                    action="create",
                    role="concept",
                    title="Decision Trace Quality",
                    body=(
                        "# Decision Trace Quality\n\nDecision trace quality records the evidence behind a "
                        "decision with a concise rationale so later readers can inspect the result. "
                        "[[Mira Chen]] introduced the method and [[Northstar Signal Lab]] measured Recall at "
                        f"Five. (Source: `{source}`)\n\n"
                        "Within an [[Agent Harness]], the method makes automated choices reviewable by "
                        "preserving why an action was selected alongside its evidence. The source does not "
                        "report an acceptance threshold or independent verification for Recall at Five. "
                        f"(Source: `{source}`)"
                    ),
                    reason="The source explains the concept.",
                ),
                _entity_page(
                    "Mira Chen", "person", "Researcher who introduced decision trace quality.",
                    "Introduced the method in 2026.", source,
                    "- [[Decision Trace Quality]] - introduced the method",
                ),
                _entity_page(
                    "Northstar Signal Lab", "organization", "Research lab that measured the method.",
                    "Measured Recall at Five in 2026.", source,
                    "- [[Decision Trace Quality]] - measured the method",
                ),
            ),
        )
    if case_id == "meeting_entity_quality":
        source = "sources/2026/09/00000000-0000-4000-8000-000000000003.md"
        note_link = "- [[Helio Stack product review]] - reviewed the evaluation workflow"
        people = (
            ("Maya Ortiz", "A founder of Helio Stack who owns the pilot decision.",
             "On 2026-09-20 she committed to decide whether the workflow is ready for a first customer pilot."),
            ("Leon Park", "A founder of Helio Stack who owns the pilot decision.",
             "On 2026-09-20 he committed to decide on the first customer pilot with Maya Ortiz."),
            ("Noor Balan", "The engineering lead working with Helio Stack.",
             "On 2026-09-20 Noor committed to run the next evaluation cycle by 2026-10-01."),
            ("Priya Sen", "The customer research lead working with Helio Stack.",
             "On 2026-09-20 Priya committed to provide five interview summaries before the next review."),
        )
        return FilingPlan(
            summary="Recorded the Helio Stack product review and participant commitments.",
            mutations=(
                PageMutation(
                    action="create",
                    role="note",
                    title="Helio Stack product review",
                    body=(
                        "# Helio Stack product review\n\nOn 2026-09-20, [[Maya Ortiz]] and [[Leon Park]] "
                        "reviewed [[Helio Stack]]'s evaluation workflow with [[Noor Balan]] and [[Priya Sen]]. "
                        "Noor will run the next evaluation cycle by 2026-10-01, Priya will provide five interview "
                        "summaries, and the founders will then decide whether the workflow is ready for a "
                        f"first customer pilot. (Source: `{source}`)\n\n"
                        "The meeting links current operating work to AI-assisted Founder Evaluation while "
                        "retaining the prior source for the founders' established roles. SignalBoard only "
                        f"organized notes and is not part of the product or operating work. (Source: `{source}`)"
                    ),
                    reason="The meeting records a dated operating decision and commitments.",
                ),
                _entity_page(
                    "Helio Stack", "organization",
                    "An early-stage product tool that helps teams compare candidate product directions.",
                    "On 2026-09-20 its founders reviewed the evaluation workflow.", source, note_link,
                ),
                *(
                    _entity_page(name, "person", who, fact, source, f"{note_link}\n- [[Helio Stack]] - works on it")
                    for name, who, fact in people
                ),
            ),
        )
    entity_pages = (
        _entity_page(
            "Robin Hale", "person", "Author who explained harness engineering.",
            "Published the explanation in 2026.", source,
            "- [[Harness Engineering]] - authored the explanation", aliases=("@robinhale_example",),
        ),
        *(
            _entity_page(
                name, "organization", f"{name} is an AI organization cited for harness results.",
                f"{name} reported a harness result in 2026.", source,
                "- [[Harness Engineering]] - cited result",
            )
            for name in ("OpenAI", "Anthropic", "LangChain")
        ),
    )
    mutation = PageMutation(
        action="create",
        role="concept",
        title="Harness Engineering",
        body=(
            harness_body + "\n\nClaude Code and Codex are coding-agent environments named by the source. "
            f"(Source: `{source}`)"
        ),
        reason="The source explains the concept.",
    )
    if case_id == "harness_engineering":
        return FilingPlan(
            summary="Filed Harness Engineering and Agent Harness separately.",
            mutations=(
                mutation.model_copy(update={"body": mutation.body.replace("agent harness", "[[Agent Harness]]", 1)}),
                PageMutation(
                    action="create",
                    role="concept",
                    title="Agent Harness",
                    body=(
                        "# Agent Harness\n\nAn agent harness is the operational system around a model. "
                        "It is designed and improved through [[Harness Engineering]], as [[Robin Hale]] explains. "
                        f"(Source: `{source}`)\n\n"
                        "It supplies the execution loop, state, environment, objectives, and verification that "
                        "turn model output into observable work rather than treating the model as the whole "
                        f"system. (Source: `{source}`)"
                    ),
                    reason="The source independently defines the target system.",
                ),
                *entity_pages,
            ),
        )
    seed = eval_worktree.SEED_SOURCE_PATH
    return FilingPlan(
        summary="Enriched the seeded harness graph.",
        mutations=(
            mutation.model_copy(update={"body": harness_body.replace("agent harness", "[[Agent Harness]]")}),
            PageMutation(
                action="update",
                path="wiki/concepts/Agent Harness.md",
                body=(
                    "# Agent Harness\n\n## Definition\n\n"
                    "An agent harness is the control layer around a model. It supplies the task loop, tools, "
                    "feedback, memory, context assembly, and execution environment needed to turn model output "
                    f"into reliable work. (Source: `{seed}`)\n\n## How It Works\n\n"
                    "The harness routes tool calls, repeats act-observe cycles, manages context, applies "
                    "permissions, handles errors and retries, and records telemetry. The prompt is the policy; "
                    f"the harness is the environment that enforces it. (Source: `{seed}`)\n\n"
                    "## Why It Matters\n\nMost failures that look like weak model reasoning can instead come from "
                    "poor context, vague tools, weak feedback, or missing verification. Those are harness "
                    f"problems that can be changed without replacing the model. (Source: `{seed}`)\n\n"
                    "## Connections\n\n[[Harness Engineering]] is the practice that designs and improves this "
                    f"operational system. (Source: `{source}`)"
                ),
                reason="Existing concept gains evidence.",
            ),
            *entity_pages,
        ),
    )


def _parity_artifact(
    repo: pathlib.Path,
    commit: str,
) -> dict:
    expected = parity.current_release_inputs(repo)
    corpus = "0123456789abcdef" * 4
    initial = "0123456789abcdef0123456789abcdef01234567"

    def case_result(case_id: str, runtime: dict, passing: bool) -> dict:
        case = planner_eval.load_case(expected.case_paths[case_id])
        source_text = expected.fixture_paths[case_id].read_text(encoding="utf-8")
        plan = _plan_for_case(case_id) if passing else FilingPlan(summary="Deliberately failing result.")
        with eval_worktree.prepared(
            case, source_text, template=str(expected.repo_root / "evals" / "filing" / "repo")
        ) as worktree:
            initial_worktree_manifest = worktree.initial_worktree_manifest_sha256
            gates, active_plan = eval_worktree.apply_with_production_repair(
                worktree,
                plan,
                _RecordedRevisionPlanner(plan),
                planning_model_requests=1,
                max_turns=parity.PRODUCTION_MAX_TURNS,
                return_plan=True,
            )
            effective = eval_worktree.effective_plan(worktree, active_plan) if gates["passed"] else active_plan
        semantic = planner_eval.score(effective, case, source_text=source_text)
        raw_gates = {
            **{gate: semantic[gate]["passed"] for gate in sorted(semantic) if gate != "passed"},
            "writer": gates["passed"],
        }
        correction_plan = active_plan.model_dump(mode="json") if gates["semantic_revision_applied"] else None
        correction = {
            "required": gates["semantic_revision_required"],
            "attempted": gates["semantic_revision_attempted"],
            "applied": gates["semantic_revision_applied"],
            "plan": correction_plan,
        }
        evidence = {
            "input": {
                "case_sha256": expected.source_cases[case_id],
                "fixture_sha256": expected.source_fixtures[case_id],
                "source_sha256": expected.source_fixtures[case_id],
                "initial_worktree_manifest_sha256": initial_worktree_manifest,
            },
            "plans": {
                "initial": plan.model_dump(mode="json"),
                "correction": correction_plan,
                "effective": effective.model_dump(mode="json"),
            },
            "result": {
                "score": semantic,
                "writer_gates": {
                    "passed": gates["passed"],
                    "violations": gates["violations"],
                    "changed_paths": gates["changed_paths"],
                    "plan_rejection": gates["plan_rejection"],
                },
                "raw_gates": raw_gates,
            },
        }
        digest = hashlib.sha256(_canonical(evidence)).hexdigest()
        correction_requests = gates["semantic_revision_model_requests"]
        return {
            "case_id": case_id,
            "runtime": runtime,
            "execution": {
                "mode": parity.PRODUCTION_EQUIVALENT_MODE,
                "configured_max_turns": parity.PRODUCTION_MAX_TURNS,
            },
            "input": evidence["input"],
            "requests": {
                "initial": 1,
                "correction": correction_requests,
                "total": 1 + correction_requests,
                "schema_retries": 0,
                "elapsed_ms": 1,
                "usage": {"requests": 1 + correction_requests},
            },
            "correction": correction,
            "evidence": evidence,
            "output": {"sha256": digest, "artifact_ref": f"sha256:{digest}"},
        }

    def run(implementation: str, run_id: str, level: str, *, passing: bool = True) -> dict:
        runtime = (
            {**parity.STIGMERGY_RUNTIME, "reasoning_level": level}
            if implementation in ("stigmergy", "production")
            else {"model": "fixture", "reasoning_level": level, "provider": "fixture"}
        )
        provenance = {
            "corpus_sha256": corpus,
            "initial_graph_ref": initial,
            "initial_graph_manifest_sha256": expected.initial_graph_manifest_sha256,
            "source_cases": expected.source_cases,
            "source_fixtures": expected.source_fixtures,
        }
        if implementation == "stigmergy":
            provenance["candidate"] = {
                "commit": expected.commit,
                "librarian_skill_sha256": expected.librarian_skill_sha256,
            }
        if implementation == "production":
            provenance["production"] = {
                "commit": expected.commit,
                "librarian_skill_sha256": expected.librarian_skill_sha256,
            }
        return {
            "implementation": implementation,
            "run_id": run_id,
            "runtime": runtime,
            "execution": {
                "mode": parity.PRODUCTION_EQUIVALENT_MODE,
                "configured_max_turns": parity.PRODUCTION_MAX_TURNS,
            },
            "provenance": provenance,
            "case_results": [
                case_result(
                    case_id,
                    runtime,
                    passing,
                )
                for case_id in sorted(expected.source_cases)
            ],
        }

    selected = [run("stigmergy", f"matrix-medium-{repeat}", "medium") for repeat in range(1, 4)]
    production = [run("production", f"production-medium-{repeat}", "medium") for repeat in range(1, 4)]
    hippocampus = run("hippocampus", "hippocampus-recorded-run", "medium")
    artifact = {
        "schema_version": parity.ARTIFACT_SCHEMA_VERSION,
        "admission_status": "passed",
        "blind_review_packet": {"status": "completed", "raw_sha256": "", "canonical_sha256": ""},
        "blind_editorial_review": {
            "verdict": parity.VERDICT,
            "provenance": {
                "reviewer": "fixture-reviewer",
                "method": "fixture-blind-pairwise",
                "artifact_ref": "",
                "corpus_sha256": corpus,
                "initial_graph_ref": initial,
                "initial_graph_manifest_sha256": expected.initial_graph_manifest_sha256,
                "source_cases": expected.source_cases,
                "source_fixtures": expected.source_fixtures,
                "runs": {
                    "hippocampus": [hippocampus["run_id"]],
                    "stigmergy": [item["run_id"] for item in selected],
                    "production": [item["run_id"] for item in production],
                },
            },
        },
        "release": {
            "corpus_sha256": corpus,
            "initial_graph_ref": initial,
            "initial_graph_manifest_sha256": expected.initial_graph_manifest_sha256,
            "source_cases": [{"id": key, "sha256": value} for key, value in expected.source_cases.items()],
            "source_fixtures": [{"id": key, "sha256": value} for key, value in expected.source_fixtures.items()],
            "candidate": {
                "commit": expected.commit,
                "librarian_skill_sha256": expected.librarian_skill_sha256,
            },
            "production": {
                "commit": expected.commit,
                "librarian_skill_sha256": expected.librarian_skill_sha256,
            },
        },
        "runs": [hippocampus, *selected, *production],
        "reasoning_matrix": [
            {
                "reasoning_level": "medium",
                "runtime": {**parity.STIGMERGY_RUNTIME, "reasoning_level": "medium"},
                "run_ids": [item["run_id"] for item in selected],
                "passed": True,
            },
        ],
    }
    _write_blind_review_bundle(
        repo.parent,
        artifact,
        {case_id: path.read_text(encoding="utf-8") for case_id, path in expected.fixture_paths.items()},
    )
    return artifact


def _review_binding(case: dict) -> dict:
    evidence = case["evidence"]
    plans = evidence["plans"]
    return {
        "case_id": case["case_id"],
        "source_sha256": case["input"]["source_sha256"],
        "effective_payload": plans["effective"],
        "effective_payload_sha256": hashlib.sha256(_canonical(plans["effective"])).hexdigest(),
        "case_output_sha256": case["output"]["sha256"],
        "initial_plan_sha256": hashlib.sha256(_canonical(plans["initial"])).hexdigest(),
        "correction_plan_sha256": (
            hashlib.sha256(_canonical(plans["correction"])).hexdigest() if plans["correction"] is not None else None
        ),
        "correction": case["correction"],
    }


def _write_blind_review_bundle(
    root: pathlib.Path,
    artifact: dict,
    sources: dict[str, str],
) -> None:
    root.mkdir(parents=True, exist_ok=True)
    runs = {run["run_id"]: run for run in artifact["runs"]}
    baseline = next(run for run in runs.values() if run["implementation"] == "hippocampus")
    selected_ids = next(
        row["run_ids"]
        for row in artifact["reasoning_matrix"]
        if row["reasoning_level"] == parity.LIBRARIAN_REASONING_LEVEL
    )
    baseline_cases = {case["case_id"]: case for case in baseline["case_results"]}
    production_ids = [run["run_id"] for run in runs.values() if run["implementation"] == "production"]
    comparisons, mappings, pairs = [], [], []
    for run_id in [*selected_ids, *production_ids]:
        candidate_run = runs[run_id]
        for candidate_case in candidate_run["case_results"]:
            comparison_id = f"comparison-{candidate_case['case_id']}-{run_id}"
            labels = []
            for implementation, run, case in (
                ("hippocampus", baseline, baseline_cases[candidate_case["case_id"]]),
                (candidate_run["implementation"], candidate_run, candidate_case),
            ):
                label = "candidate-" + hashlib.sha256(f"{comparison_id}:{implementation}".encode()).hexdigest()[:16]
                labels.append(
                    {
                        "label": label,
                        **_review_binding(case),
                        "implementation": implementation,
                        "run_id": run["run_id"],
                    }
                )
            case_id = candidate_case["case_id"]
            source = sources[case_id]
            comparisons.append(
                {
                    "comparison_id": comparison_id,
                    "case_id": case_id,
                    "source": {
                        "sha256": candidate_case["input"]["source_sha256"],
                        "text": source,
                    },
                    "candidates": [{key: label[key] for key in parity.PACKET_CANDIDATE_FIELDS} for label in labels],
                }
            )
            mappings.append(
                {
                    "comparison_id": comparison_id,
                    "case_id": case_id,
                    "source_sha256": candidate_case["input"]["source_sha256"],
                    "labels": [{key: label[key] for key in parity.MAPPING_LABEL_FIELDS} for label in labels],
                }
            )
            pairs.append(
                {
                    "pair_id": comparison_id,
                    "judgments": {
                        dimension: {"winner": "tie", "reason": "Fixture comparison."}
                        for dimension in parity.BLIND_REVIEW_DIMENSIONS
                    },
                    "overall": {"winner": "tie", "reason": "Fixture."},
                    "material_regression": {"side": "none", "reason": "Fixture."},
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
        "verdict": parity.VERDICT,
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
            "material_regressions": [],
            "stigmergy_material_regressions": [],
            "production_material_regressions": [],
        },
    }
    review_bytes = _canonical(review)
    review_digest = hashlib.sha256(review_bytes).hexdigest()
    (root / f"blind-review-unblind-{review_digest}.json").write_bytes(review_bytes)
    artifact["blind_review_packet"] = {
        "status": "completed",
        "raw_sha256": hashlib.sha256(packet_bytes).hexdigest(),
        "canonical_sha256": packet_digest,
    }
    artifact["blind_editorial_review"]["provenance"]["artifact_ref"] = f"sha256:{review_digest}"


def _evaluate_recorded_artifact(repo: pathlib.Path, payload: dict | None = None) -> dict:
    artifact = payload or json.loads(_artifact_path(repo).read_text(encoding="utf-8"))
    return parity.evaluate(
        artifact,
        expected=parity.current_release_inputs(repo),
        review_root=repo.parent,
    )


def _review_document(repo: pathlib.Path, payload: dict, field: str) -> tuple[dict, pathlib.Path, dict]:
    reference = payload["blind_editorial_review"]["provenance"]["artifact_ref"].removeprefix("sha256:")
    review_path = repo.parent / f"blind-review-unblind-{reference}.json"
    review = json.loads(review_path.read_text(encoding="utf-8"))
    metadata = review[field]
    return review, repo.parent / metadata["path"], metadata


def test_v7_release_evidence_accepts_exactly_three_complete_production_repeats(tmp_path):
    result, _deploy, _seen = _run_deploy(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(_artifact_path(tmp_path).read_text(encoding="utf-8"))
    production_runs = [
        run
        for run in payload["runs"]
        if run["implementation"] == "stigmergy"
        and run["runtime"]["reasoning_level"] == parity.LIBRARIAN_REASONING_LEVEL
    ]
    assert len(production_runs) == 3
    assert all(len(run["case_results"]) == len(payload["release"]["source_cases"]) for run in production_runs)
    report = _evaluate_recorded_artifact(tmp_path, payload)

    assert report["passed"] is True


def test_v7_release_evidence_rejects_nonproduction_reasoning_runs(tmp_path):
    result, _deploy, _seen = _run_deploy(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(_artifact_path(tmp_path).read_text(encoding="utf-8"))
    nonproduction = json.loads(json.dumps(next(run for run in payload["runs"] if run["implementation"] == "stigmergy")))
    nonproduction["run_id"] = "matrix-minimal-1"
    nonproduction["runtime"]["reasoning_level"] = "minimal"
    for case in nonproduction["case_results"]:
        case["runtime"]["reasoning_level"] = "minimal"
    payload["runs"].append(nonproduction)

    report = _evaluate_recorded_artifact(tmp_path, payload)

    assert report["passed"] is False
    assert "nonproduction-reasoning" in {failure["reason"] for failure in report["failures"]}


def test_v7_evaluate_rejects_altered_writer_plan_rejection(tmp_path):
    result, _deploy, _seen = _run_deploy(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(_artifact_path(tmp_path).read_text(encoding="utf-8"))
    case = payload["runs"][0]["case_results"][0]
    case["evidence"]["result"]["writer_gates"]["plan_rejection"] = "existing-source-block-not-preserved"
    digest = hashlib.sha256(_canonical(case["evidence"])).hexdigest()
    case["output"] = {"sha256": digest, "artifact_ref": f"sha256:{digest}"}

    report = _evaluate_recorded_artifact(tmp_path, payload)

    assert report["passed"] is False
    assert "case-writer-gate" in {failure["reason"] for failure in report["failures"]}


@pytest.mark.parametrize("change", ["remove", "add"])
def test_v7_release_evidence_requires_exactly_three_production_repeats(tmp_path, change):
    result, _deploy, _seen = _run_deploy(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(_artifact_path(tmp_path).read_text(encoding="utf-8"))
    matrix = next(
        row for row in payload["reasoning_matrix"] if row["reasoning_level"] == parity.LIBRARIAN_REASONING_LEVEL
    )
    if change == "remove":
        run_id = matrix["run_ids"].pop()
        payload["runs"] = [run for run in payload["runs"] if run["run_id"] != run_id]
    else:
        extra = json.loads(json.dumps(next(run for run in payload["runs"] if run["run_id"] == matrix["run_ids"][0])))
        extra["run_id"] = "matrix-medium-4"
        payload["runs"].append(extra)
        matrix["run_ids"].append(extra["run_id"])

    report = _evaluate_recorded_artifact(tmp_path, payload)

    assert report["passed"] is False
    assert {"reasoning-matrix", "reasoning-matrix-coverage"} & {
        failure["reason"] for failure in report["failures"]
    }


@pytest.mark.parametrize(
    ("container", "retired_field", "reason"),
    [
        ("root", "graph_shape", "artifact-shape"),
        ("run", "reviewed_plan", "run-shape"),
        ("case", "expected_graph_mutations", "case-result-shape"),
        ("runtime", "graph_shape_draft", "runtime-metadata"),
        ("provenance", "graph_shape_review", "run-provenance"),
        ("input", "graph_shape_violations", "case-input"),
        ("requests", "repair_model_requests", "case-request-telemetry"),
        ("correction", "compilation_model_requests", "case-correction"),
        ("evidence", "reviewed_plan", "case-evidence"),
        ("output", "repair_model_requests", "case-output"),
        ("matrix", "graph_shape", "reasoning-matrix"),
        ("review", "expected_graph_mutations", "stale-or-mismatched-review"),
    ],
)
def test_v7_evaluate_rejects_retired_staged_fields(tmp_path, container, retired_field, reason):
    result, _deploy, _seen = _run_deploy(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(_artifact_path(tmp_path).read_text(encoding="utf-8"))
    run = payload["runs"][0]
    case = run["case_results"][0]
    target = {
        "root": payload,
        "run": run,
        "case": case,
        "runtime": run["runtime"],
        "provenance": run["provenance"],
        "input": case["input"],
        "requests": case["requests"],
        "correction": case["correction"],
        "evidence": case["evidence"],
        "output": case["output"],
        "matrix": payload["reasoning_matrix"][0],
        "review": payload["blind_editorial_review"]["provenance"],
    }[container]
    target[retired_field] = "retired"

    report = _evaluate_recorded_artifact(tmp_path, payload)

    assert report["passed"] is False
    assert reason in {failure["reason"] for failure in report["failures"]}


@pytest.mark.parametrize(
    "tamper",
    [
        "source",
        "candidate",
        "response",
        "mapping",
        "label",
        "unblinding",
    ],
)
def test_v7_evaluate_rejects_tampered_blind_evidence(tmp_path, tamper):
    result, _deploy, _seen = _run_deploy(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(_artifact_path(tmp_path).read_text(encoding="utf-8"))
    if tamper == "unblinding":
        review, path, _metadata = _review_document(tmp_path, payload, "packet")
        review["unblinding"]["material_regressions"].append({"unexpected": "record"})
        reference = payload["blind_editorial_review"]["provenance"]["artifact_ref"].removeprefix("sha256:")
        (tmp_path.parent / f"blind-review-unblind-{reference}.json").write_text(json.dumps(review), encoding="utf-8")
    else:
        field = {
            "source": "packet",
            "candidate": "packet",
            "response": "reviewer_response",
            "mapping": "mapping",
            "label": "mapping",
        }[tamper]
        _review, path, _metadata = _review_document(tmp_path, payload, field)
        document = json.loads(path.read_text(encoding="utf-8"))
        if tamper == "source":
            document["comparisons"][0]["source"]["text"] += " tampered"
        elif tamper == "candidate":
            document["comparisons"][0]["candidates"][0]["effective_payload"]["summary"] = "tampered"
        elif tamper == "response":
            document["pairs"][0]["overall"]["reason"] = "tampered"
        elif tamper == "mapping":
            document["mapping"][0]["source_sha256"] = "a" * 64
        else:
            document["mapping"][0]["labels"][0]["label"] = "tampered-label"
        path.write_text(json.dumps(document), encoding="utf-8")

    report = _evaluate_recorded_artifact(tmp_path, payload)

    assert report["passed"] is False
    assert "blind-review-evidence" in {failure["reason"] for failure in report["failures"]}


def test_v7_evaluate_rejects_a_non_derived_material_regression(tmp_path):
    result, _deploy, _seen = _run_deploy(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(_artifact_path(tmp_path).read_text(encoding="utf-8"))
    review, response_path, _response_metadata = _review_document(tmp_path, payload, "reviewer_response")
    response = json.loads(response_path.read_text(encoding="utf-8"))
    _mapping_review, mapping_path, _mapping_metadata = _review_document(tmp_path, payload, "mapping")
    mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
    response["pairs"][0]["material_regression"] = {
        "side": mapping["mapping"][0]["labels"][0]["label"],
        "reason": "Invalid fixture derivation.",
    }
    response["pairs"][0]["overall"]["winner"] = mapping["mapping"][0]["labels"][1]["label"]
    response_bytes = _canonical(response)
    response_digest = hashlib.sha256(response_bytes).hexdigest()
    response_name = f"blind-reviewer-response-{response_digest}.json"
    (tmp_path.parent / response_name).write_bytes(response_bytes)
    review["reviewer_response"] = {
        "path": response_name,
        "raw_sha256": response_digest,
        "canonical_sha256": response_digest,
        "artifact_ref": f"sha256:{response_digest}",
    }
    review_bytes = _canonical(review)
    review_digest = hashlib.sha256(review_bytes).hexdigest()
    (tmp_path.parent / f"blind-review-unblind-{review_digest}.json").write_bytes(review_bytes)
    payload["blind_editorial_review"]["provenance"]["artifact_ref"] = f"sha256:{review_digest}"

    report = _evaluate_recorded_artifact(tmp_path, payload)

    assert report["passed"] is False
    assert "blind-review-evidence" in {failure["reason"] for failure in report["failures"]}



@pytest.mark.parametrize(("candidate", "production", "passes"), [(1, 1, True), (0, 2, True), (1, 0, False)])
def test_the_candidate_may_regress_no_more_than_production_in_the_same_review(
    tmp_path, candidate, production, passes
):
    result, _deploy, _seen = _run_deploy(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(_artifact_path(tmp_path).read_text(encoding="utf-8"))
    review, response_path, _metadata = _review_document(tmp_path, payload, "reviewer_response")
    response = json.loads(response_path.read_text(encoding="utf-8"))
    _mapping_review, mapping_path, _mapping_metadata = _review_document(tmp_path, payload, "mapping")
    mapping = {row["comparison_id"]: row for row in json.loads(mapping_path.read_text(encoding="utf-8"))["mapping"]}
    packet = {row["comparison_id"]: row for row in json.loads(
        (tmp_path.parent / review["packet"]["path"]).read_text(encoding="utf-8"))["comparisons"]}
    wanted = {"stigmergy": candidate, "production": production}
    regressions = []
    for pair in response["pairs"]:
        labels = {label["implementation"]: label for label in mapping[pair["pair_id"]]["labels"]}
        side = next(name for name in labels if name != "hippocampus")
        if wanted[side] == 0:
            continue
        wanted[side] -= 1
        selected, other = labels[side], labels["hippocampus"]
        pair["material_regression"] = {"side": selected["label"], "reason": "Fixture regression."}
        pair["overall"]["winner"] = other["label"]
        regressions.append({
            "comparison_id": pair["pair_id"], "candidate_label": selected["label"],
            "implementation": side, "run_id": selected["run_id"], "case_id": selected["case_id"],
            "source_sha256": packet[pair["pair_id"]]["source"]["sha256"],
            "case_output_sha256": selected["case_output_sha256"],
            "effective_payload_sha256": selected["effective_payload_sha256"], "reason": "Fixture regression."})
    response_bytes = _canonical(response)
    response_digest = hashlib.sha256(response_bytes).hexdigest()
    response_name = f"blind-reviewer-response-{response_digest}.json"
    (tmp_path.parent / response_name).write_bytes(response_bytes)
    review["reviewer_response"] = {"path": response_name, "raw_sha256": response_digest,
                                   "canonical_sha256": response_digest, "artifact_ref": f"sha256:{response_digest}"}
    review["unblinding"] = {
        "material_regressions": regressions,
        "stigmergy_material_regressions": [item for item in regressions if item["implementation"] == "stigmergy"],
        "production_material_regressions": [item for item in regressions if item["implementation"] == "production"],
    }
    review_bytes = _canonical(review)
    review_digest = hashlib.sha256(review_bytes).hexdigest()
    (tmp_path.parent / f"blind-review-unblind-{review_digest}.json").write_bytes(review_bytes)
    payload["blind_editorial_review"]["provenance"]["artifact_ref"] = f"sha256:{review_digest}"

    report = _evaluate_recorded_artifact(tmp_path, payload)

    assert report["passed"] is passes, report["failures"]
    if not passes:
        assert {failure["reason"] for failure in report["failures"]} == {"regression-beyond-production"}

def test_deploy_ships_the_clean_candidate_and_releases_its_lock(tmp_path):
    result, deploy, seen = _run_deploy(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (seen / "deployed").exists()
    assert not (deploy / ".staging-deploy.lock").exists()
    assert (deploy / "unmanaged" / "tracked.txt").read_text() == "keep\n"


def test_an_operator_may_deploy_a_released_version_without_parity_evidence(tmp_path):
    result, _deploy, seen = _run_deploy(tmp_path, parity_artifact="absent", parity_gate="off")

    assert result.returncode == 0, result.stdout + result.stderr
    assert (seen / "deployed").exists()
    assert "parity gate off" in result.stderr


def test_without_evidence_or_an_explicit_opt_out_the_deploy_is_refused(tmp_path):
    result, _deploy, seen = _run_deploy(tmp_path, parity_artifact="absent")

    assert result.returncode == 2
    assert not (seen / "deployed").exists()
    assert "STIGMERGY_PARITY_GATE=off" in result.stderr


def test_deploy_copies_the_operators_legal_pages_onto_the_machines(tmp_path):
    legal = _sidecar_path(tmp_path, "legal")
    legal.mkdir()
    (legal / "sub-processors.md").write_text("# Sub-processors\n", encoding="utf-8")

    result, _deploy, seen = _run_deploy(tmp_path, legal_source=legal)

    assert result.returncode == 0, result.stdout + result.stderr
    args = (seen / "deploy-args").read_text(encoding="utf-8").splitlines()
    assert args[args.index("--env") + 1] == "STIGMERGY_LEGAL_DIR=/srv/legal"
    assert args[args.index("--file-local") + 1] == f"/srv/legal/sub-processors.md={legal / 'sub-processors.md'}"


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


def test_release_deploy_refuses_a_tampered_blind_review_packet(tmp_path):
    result, _deploy, seen = _run_deploy(tmp_path, parity_artifact="tampered-blind-packet")

    assert result.returncode == 2
    report = json.loads(result.stdout)
    assert "blind-review-evidence" in {failure["reason"] for failure in report["failures"]}
    assert not seen.exists()


def test_release_deploy_refuses_nonproduction_reasoning_evidence(tmp_path):
    result, _deploy, seen = _run_deploy(tmp_path, parity_artifact="minimal-reasoning")

    assert result.returncode == 2
    report = json.loads(result.stdout)
    assert "nonproduction-reasoning" in {failure["reason"] for failure in report["failures"]}
    assert not seen.exists()
