"""Pure scoring for planner-only filing evaluations."""

from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path

from stigmergy.entities.service import _tokens as entity_tokens
from stigmergy.kernel.normalize import resolution_key
from stigmergy.knowledge.plan import FilingPlan, PageMutation

_DASH_VARIANTS = str.maketrans({character: "-" for character in "‐‑‒–—―−"})


def load_case(path: str | Path) -> dict:
    """Load one versioned filing case without interpreting source material."""
    case = json.loads(Path(path).read_text(encoding="utf-8"))
    if case.get("version") not in {1, 2}:
        raise ValueError("filing case must declare version 1 or 2")
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
    body_score = _bodies(case, mutations)
    connection_score = _connections(case, mutations)
    entity_relationship_score = _entity_relationships(mutations, resolutions, candidates)
    entity_wikilink_score = _entity_wikilinks(case, mutations, candidates)
    anti_fragmentation_score = _anti_fragmentation(case, mutations)
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
            body_score,
            connection_score,
            entity_relationship_score,
            entity_wikilink_score,
            anti_fragmentation_score,
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
        "bodies": body_score,
        "connections": connection_score,
        "entity_relationships": entity_relationship_score,
        "entity_wikilinks": entity_wikilink_score,
        "anti_fragmentation": anti_fragmentation_score,
    }


def _mutations(expectation: dict, mutations: tuple[PageMutation, ...]) -> dict:
    if "min_count" in expectation:
        required = {_case_signature(item) for item in expectation.get("required", ())}
        allowed = required | {_case_signature(item) for item in expectation.get("allowed", ())}
        forbidden = {_case_signature(item) for item in expectation.get("forbidden", ())}
        actual = tuple(_mutation_signature(mutation) for mutation in mutations)
        found = set(actual)
        duplicates = {item for item in found if actual.count(item) > 1}
        missing = required - found
        unexpected = found - allowed if expectation.get("closed", True) else set()
        present_forbidden = forbidden & found
        minimum = int(expectation["min_count"])
        maximum = int(expectation["max_count"])
        expected_indexes = tuple(index for index, signature in enumerate(actual) if signature in allowed)
        return {
            "minimum_count": minimum,
            "maximum_count": maximum,
            "actual_count": len(mutations),
            "required": required,
            "allowed": allowed,
            "missing": missing,
            "unexpected": unexpected,
            "forbidden_present": present_forbidden,
            "duplicates": duplicates,
            "expected_indexes": expected_indexes,
            "passed": minimum <= len(mutations) <= maximum and not missing and not unexpected
            and not present_forbidden and not duplicates,
        }
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
    if "minimum_count" in score:
        return {
            key: (
                [_render_signature(item) for item in sorted(score[key])]
                if key in {"required", "allowed", "missing", "unexpected", "forbidden_present", "duplicates"}
                else score[key]
            )
            for key in ("minimum_count", "maximum_count", "actual_count", "required", "allowed",
                        "missing", "unexpected", "forbidden_present", "duplicates", "passed")
        }
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


def _bodies(case: dict, mutations: tuple[PageMutation, ...]) -> dict:
    expectation = case.get("bodies", {})
    relevant = tuple(mutation for mutation in mutations if mutation.action != "delete")
    combined = _normalized_text("\n".join(mutation.body or "" for mutation in relevant))
    required = [str(value) for value in expectation.get("required", ())]
    required_any = tuple(expectation.get("required_any", ()))
    forbidden = [str(value) for value in expectation.get("forbidden", ())]
    missing = [value for value in required if _normalized_text(value) not in combined]
    missing_any = [
        str(group.get("label") or "required semantic coverage")
        for group in required_any
        if not _body_requirement_met(combined, group)
    ]
    present_forbidden = [value for value in forbidden if _normalized_text(value) in combined]
    heading_mismatch = [
        mutation.title or mutation.path or ""
        for mutation in relevant
        if mutation.title and not (mutation.body or "").lstrip().startswith(f"# {mutation.title}")
    ]
    placeholders = [
        mutation.title or mutation.path or ""
        for mutation in relevant
        if _placeholder_body(mutation.body or "")
    ]
    source_path = str(case.get("source_path") or "")
    missing_citations = [
        mutation.title or mutation.path or ""
        for mutation in relevant
        if expectation.get("require_local_source") and source_path not in (mutation.body or "")
    ]
    return {
        "missing_required": missing,
        "missing_required_any": missing_any,
        "forbidden_present": present_forbidden,
        "heading_mismatch": heading_mismatch,
        "placeholders": placeholders,
        "missing_local_source_attribution": missing_citations,
        "passed": not missing and not missing_any and not present_forbidden and not heading_mismatch
        and not placeholders and not missing_citations,
    }


def _anti_fragmentation(case: dict, mutations: tuple[PageMutation, ...]) -> dict:
    """Reject only declared redundant conceptual splits; it is not a page-count quota."""
    expectation = case.get("anti_fragmentation", {})
    groups = tuple(expectation.get("redundant_title_groups", ()))
    created = {
        resolution_key(mutation.title or mutation.path or "")
        for mutation in mutations
        if mutation.action == "create" and mutation.role in {"note", "concept"}
    }
    fragmented = []
    for group in groups:
        members = {resolution_key(title) for title in group}
        overlap = sorted(created & members)
        if len(overlap) > 1:
            fragmented.append(overlap)
    return {"fragmented_groups": fragmented, "passed": not fragmented}


def _placeholder_body(body: str) -> bool:
    lines = [line.strip() for line in body.splitlines() if line.strip()]
    prose = " ".join(lines[1:]) if lines and lines[0].startswith("# ") else ""
    alphanumeric = re.sub(r"[^A-Za-z0-9]", "", prose)
    return not prose or len(alphanumeric) < 32 or bool(
        re.search(r"\b(?:todo|tbd|placeholder|to be written|coming soon)\b", prose, re.I)
    )


def _connections(case: dict, mutations: tuple[PageMutation, ...]) -> dict:
    by_title = {resolution_key(mutation.title or mutation.path or ""): mutation for mutation in mutations}
    missing = []
    for item in case.get("connections", ()):
        source = resolution_key(item["from"])
        target = str(item["to"])
        source_mutation = by_title.get(source)
        if source_mutation is None or f"[[{target}".casefold() not in (source_mutation.body or "").casefold():
            missing.append({"from": item["from"], "to": target})
            continue
        if item.get("reciprocal"):
            target_mutation = by_title.get(resolution_key(target))
            if target_mutation is None or f"[[{item['from']}".casefold() not in (target_mutation.body or "").casefold():
                missing.append({"from": target, "to": item["from"]})
    return {"missing": missing, "passed": not missing}


def _entity_relationships(mutations, resolutions, candidates) -> dict:
    missing = []
    for index, mutation in enumerate(mutations):
        body = mutation.body or ""
        for canonical in resolutions["resolved"].get(index, ()):
            names = [
                value for value, entries in candidates.items()
                if any(candidate == canonical for _proposal, candidate in entries)
            ]
            if not any(_relationship_mention(body, name) for name in names):
                missing.append({"mutation": mutation.title or mutation.path, "entity": canonical})
    return {"missing": missing, "passed": not missing}


def _entity_wikilinks(case, mutations, candidates) -> dict:
    """Reject links that pretend an internal entity anchor is a normal wiki page."""
    normal_targets = {
        resolution_key(value)
        for value in (
            *case.get("normal_page_targets", ()),
            *(mutation.title for mutation in mutations if mutation.role in {"note", "concept"}),
        )
        if value
    }
    violations = []
    for mutation in mutations:
        for target in re.findall(r"\[\[([^\]|#]+)(?:#[^\]|]+)?(?:\|[^\]]+)?\]\]", mutation.body or ""):
            key = resolution_key(target)
            if key in candidates and key not in normal_targets:
                violations.append({"mutation": mutation.title or mutation.path, "target": target})
    return {"violations": violations, "passed": not violations}


def _body_requirement_met(body: str, requirement: dict) -> bool:
    if any(_normalized_text(phrase) in body for phrase in requirement.get("phrases", ())):
        return True
    return any(
        all(_normalized_text(term) in body for term in terms)
        for terms in requirement.get("co_occurrence", ())
    )


def _normalized_text(value: object) -> str:
    return unicodedata.normalize("NFKC", str(value)).translate(_DASH_VARIANTS).casefold()


def _relationship_mention(body: str, name: str) -> bool:
    for paragraph in re.split(r"\n\s*\n", body):
        if "(source:" not in paragraph.casefold():
            continue
        for line in paragraph.splitlines():
            if name.casefold() not in line.casefold():
                continue
            remainder = re.sub(re.escape(name), "", line, flags=re.I)
            if len(re.findall(r"[A-Za-z0-9]+", remainder)) >= 3:
                return True
    return False


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
