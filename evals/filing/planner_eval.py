"""Pure scoring for planner-only filing evaluations."""

from __future__ import annotations

import json
from pathlib import Path

from stigmergy.entities.service import _tokens as entity_tokens
from stigmergy.kernel.normalize import resolution_key
from stigmergy.knowledge.plan import FilingPlan, PageMutation


def load_case(path: str | Path) -> dict:
    """Load one versioned filing case without interpreting source material."""
    case = json.loads(Path(path).read_text(encoding="utf-8"))
    if case.get("version") != 1:
        raise ValueError("filing case must declare version 1")
    for key in (
        "name",
        "source_title",
        "source_path",
        "mutations",
        "identities",
        "external_ids",
        "links",
        "link_coverage",
    ):
        if key not in case:
            raise ValueError(f"filing case is missing {key}")
    return case


def score(plan: FilingPlan, case: dict, *, source_text: str = "") -> dict:
    """Score closed graph expectations without writing a worktree."""
    mutations = tuple(plan.mutations)
    mutation_score = _mutations(case["mutations"], mutations)
    candidates = _candidate_sets(plan)
    resolutions = _resolve_references(mutations, candidates)
    identity_score = _membership(case["identities"], _proposal_names(plan))
    external_id_score = _membership(case["external_ids"], _external_ids(plan))
    link_score = _membership(
        case["links"],
        {
            resolved
            for index in mutation_score["expected_indexes"]
            for resolved in resolutions["resolved"].get(index, ())
        },
    )
    alias_score = _alias_evidence(plan, source_text)
    coverage_score = _link_coverage(
        _proposal_names(plan),
        {
            resolved
            for values in resolutions["resolved"].values()
            for resolved in values
        },
        case["link_coverage"],
    )
    resolution_score = {
        "unresolved": resolutions["unresolved"],
        "ambiguous": resolutions["ambiguous"],
        "passed": not resolutions["unresolved"] and not resolutions["ambiguous"],
    }
    passed = all(
        result["passed"]
        for result in (
            mutation_score,
            identity_score,
            external_id_score,
            link_score,
            alias_score,
            coverage_score,
            resolution_score,
        )
    )
    return {
        "passed": passed,
        "mutations": _public_mutation_score(mutation_score),
        "identity_proposals": identity_score,
        "external_ids": external_id_score,
        "entity_links": link_score,
        "link_coverage": coverage_score,
        "reference_resolution": resolution_score,
        "alias_evidence": alias_score,
    }


def _mutations(expectation: dict, mutations: tuple[PageMutation, ...]) -> dict:
    allowed = {_case_signature(item) for item in expectation["allowed"]}
    actual = tuple(_mutation_signature(mutation) for mutation in mutations)
    expected_indexes = tuple(index for index, signature in enumerate(actual) if signature in allowed)
    found = set(actual)
    missing = allowed - found
    unexpected = set(actual) - allowed
    expected_count = int(expectation["count"])
    return {
        "expected_count": expected_count,
        "actual_count": len(mutations),
        "allowed": allowed,
        "missing": missing,
        "unexpected": unexpected,
        "expected_indexes": expected_indexes,
        "passed": len(mutations) == expected_count and not missing and not unexpected,
    }


def _public_mutation_score(score: dict) -> dict:
    return {
        key: (
            [_render_signature(item) for item in sorted(score[key])]
            if key in {"allowed", "missing", "unexpected"}
            else score[key]
        )
        for key in ("expected_count", "actual_count", "allowed", "missing", "unexpected", "passed")
    }


def _case_signature(item: dict) -> tuple[str, str, str]:
    return (
        str(item["action"]),
        str(item.get("role") or ""),
        resolution_key(item.get("title") or item.get("path") or ""),
    )


def _mutation_signature(mutation: PageMutation) -> tuple[str, str, str]:
    return (
        mutation.action,
        mutation.role or "",
        resolution_key(mutation.title or mutation.path or ""),
    )


def _render_signature(value: tuple[str, str, str]) -> dict:
    action, role, title = value
    return {"action": action, "role": role or None, "title": title}


def _proposal_names(plan: FilingPlan) -> set[str]:
    return {resolution_key(proposal.name) for proposal in plan.entities}


def _external_ids(plan: FilingPlan) -> set[str]:
    return {
        f"{resolution_key(proposal.external_namespace)}:{resolution_key(proposal.external_id)}"
        for proposal in plan.entities
        if proposal.external_namespace and proposal.external_id
    }


def _link_coverage(proposals: set[str], linked: set[str], expectation: dict) -> dict:
    allowed_unlinked = _keys(expectation.get("allow_unlinked", ()))
    unlinked = proposals - linked - allowed_unlinked
    return {
        "proposals": sorted(proposals),
        "linked": sorted(linked),
        "allowed_unlinked": sorted(allowed_unlinked),
        "unlinked_proposals": sorted(unlinked),
        "passed": not unlinked,
    }


def _candidate_sets(plan: FilingPlan) -> dict[str, tuple[tuple[int, str], ...]]:
    candidates: dict[str, list[tuple[int, str]]] = {}
    for index, proposal in enumerate(plan.entities):
        canonical = resolution_key(proposal.name)
        for value in (proposal.name, *proposal.aliases):
            candidates.setdefault(resolution_key(value), []).append((index, canonical))
    return {key: tuple(values) for key, values in candidates.items()}


def _resolve_references(
    mutations: tuple[PageMutation, ...], candidates: dict[str, tuple[tuple[int, str], ...]]
) -> dict:
    resolved: dict[int, tuple[str, ...]] = {}
    unresolved: set[str] = set()
    ambiguous: dict[str, list[str]] = {}
    for index, mutation in enumerate(mutations):
        selected = []
        for reference in mutation.entities or ():
            key = resolution_key(reference)
            matches = candidates.get(key, ())
            if not matches:
                unresolved.add(key)
            elif len(matches) != 1:
                ambiguous[key] = [canonical for _proposal, canonical in matches]
            else:
                selected.append(matches[0][1])
        resolved[index] = tuple(dict.fromkeys(selected))
    return {
        "resolved": resolved,
        "unresolved": sorted(unresolved),
        "ambiguous": [
            {"reference": reference, "candidates": candidates}
            for reference, candidates in sorted(ambiguous.items())
        ],
    }


def _alias_evidence(plan: FilingPlan, source_text: str) -> dict:
    source_tokens = entity_tokens(source_text)
    missing = []
    for proposal in plan.entities:
        for alias in proposal.aliases:
            alias_tokens = entity_tokens(alias)
            if not any(
                source_tokens[offset : offset + len(alias_tokens)] == alias_tokens
                for offset in range(len(source_tokens) - len(alias_tokens) + 1)
            ):
                missing.append({"proposal": proposal.name, "alias": alias})
    return {"missing": missing, "passed": not missing}


def _membership(expectation: dict, found: set[str]) -> dict:
    required = _keys(expectation.get("required", ()))
    allowed = _keys(expectation.get("allowed", ()))
    forbidden = _keys(expectation.get("forbidden", ()))
    closed = bool(expectation.get("closed", True))
    expected = required | allowed
    missing = required - found
    present_forbidden = forbidden & found
    unexpected = found - expected if closed else set()
    return {
        "required": sorted(required),
        "allowed": sorted(allowed),
        "found": sorted(found),
        "missing": sorted(missing),
        "allowed_present": sorted(allowed & found),
        "unexpected": sorted(unexpected),
        "forbidden_present": sorted(present_forbidden),
        "closed": closed,
        "passed": not missing and not unexpected and not present_forbidden,
    }


def _keys(values: tuple[str, ...] | list[str]) -> set[str]:
    return {resolution_key(value) for value in values}
