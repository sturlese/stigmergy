"""Pure scoring for planner-only filing evaluations."""

from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path

from stigmergy.entities.service import _tokens as entity_tokens
from stigmergy.kernel.normalize import resolution_key
from stigmergy.knowledge.plan import FilingPlan, PageMutation
from stigmergy.knowledge.relationships import has_entity_relationship_evidence

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
    entity_relationship_score = _entity_relationships(
        mutations, resolutions, candidates, source_path=case["source_path"]
    )
    entity_wikilink_score = _entity_wikilinks(case, mutations, candidates)
    anti_fragmentation_score = _anti_fragmentation(case, mutations)
    editorial_quality_score = _editorial_quality(case, plan)
    entity_editorial_quality_score = _entity_editorial_quality(case, plan)
    seeded_update_score = _seeded_update(case, mutations)
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
            editorial_quality_score,
            entity_editorial_quality_score,
            seeded_update_score,
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
        "editorial_quality": editorial_quality_score,
        "entity_editorial_quality": entity_editorial_quality_score,
        "seeded_update": seeded_update_score,
    }


def _mutations(expectation: dict, mutations: tuple[PageMutation, ...]) -> dict:
    if "min_count" in expectation:
        required_items = tuple(expectation.get("required", ()))
        allowed_items = tuple(expectation.get("allowed", ()))
        forbidden_items = tuple(expectation.get("forbidden", ()))
        required = {_case_signature(item) for item in required_items}
        allowed = required | {_case_signature(item) for item in allowed_items}
        actual = tuple(_mutation_signature(mutation) for mutation in mutations)
        found = set(actual)
        duplicates = {item for item in found if actual.count(item) > 1}
        missing = {
            _case_signature(item)
            for item in required_items
            if not any(_matches_case_mutation(actual_item, item) for actual_item in actual)
        }
        expected_indexes = tuple(
            index
            for index, actual_item in enumerate(actual)
            if any(
                _matches_case_mutation(actual_item, item)
                for item in (*required_items, *allowed_items)
            )
        )
        unexpected = (
            {item for index, item in enumerate(actual) if index not in expected_indexes}
            if expectation.get("closed", True)
            else set()
        )
        present_forbidden = {
            actual_item
            for actual_item in actual
            if any(_matches_case_mutation(actual_item, item) for item in forbidden_items)
        }
        minimum = int(expectation["min_count"])
        maximum = int(expectation["max_count"])
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
    allowed_items = tuple(expectation["allowed"])
    allowed = {_case_signature(item) for item in allowed_items}
    actual = tuple(_mutation_signature(mutation) for mutation in mutations)
    expected_indexes = tuple(
        index
        for index, actual_item in enumerate(actual)
        if any(_matches_case_mutation(actual_item, item) for item in allowed_items)
    )
    missing = {
        _case_signature(item)
        for item in allowed_items
        if not any(_matches_case_mutation(actual_item, item) for actual_item in actual)
    }
    unexpected = {item for index, item in enumerate(actual) if index not in expected_indexes}
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


def _matches_case_mutation(actual: tuple[str, str, str], expectation: dict) -> bool:
    action, role, title = _case_signature(expectation)
    return actual[:2] == (action, role) and (
        expectation.get("title") is None or actual[2] == title
    )


def _mutation_signature(mutation: PageMutation) -> tuple[str, str, str]:
    return (
        mutation.action,
        "" if mutation.action == "update" else mutation.role or "",
        resolution_key(_mutation_label(mutation)),
    )


def _mutation_label(mutation: PageMutation) -> str:
    if mutation.action == "update" and mutation.path:
        return Path(mutation.path).stem
    if mutation.title:
        return mutation.title
    return mutation.path or ""


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
        if mutation.action == "create" and mutation.title
        and not (mutation.body or "").lstrip().startswith(f"# {mutation.title}")
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


def _editorial_quality(case: dict, plan: FilingPlan) -> dict:
    expectation = case.get("editorial_quality", {})
    mutations = tuple(mutation for mutation in plan.mutations if mutation.action != "delete")
    minimum = int(expectation.get("min_explanatory_paragraphs", 0))
    paragraph_counts = {
        _mutation_label(mutation): _explanatory_paragraph_count(mutation.body or "")
        for mutation in mutations
    }
    thin_pages = sorted(title for title, count in paragraph_counts.items() if count < minimum)
    combined = "\n".join(mutation.body or "" for mutation in mutations)
    missing_caveat = bool(expectation.get("require_epistemic_caveat")) and not re.search(
        r"\b(?:does\s+not\s+(?:report|provide|specify)|no\s+(?:score|scores|result|results|"
        r"threshold|thresholds|procedure|methodology)\s+(?:is|are|was|were)?\s*(?:reported|provided|"
        r"specified)|not\s+independently\s+verified|source[- ]reported|source\s+(?:omits|gives\s+no|"
        r"provides\s+no))\b",
        combined,
        flags=re.IGNORECASE,
    )
    inventory_pages = []
    if expectation.get("reject_evidence_inventory_mirroring"):
        inventory_pages = sorted(
            _mutation_label(mutation)
            for mutation in mutations
            if _looks_like_evidence_inventory(mutation.body or "")
        )
    duplicate_prefixes = []
    if expectation.get("reject_duplicate_entity_prefixes"):
        for mutation in mutations:
            for proposal in plan.entities:
                if _duplicated_name(mutation.body or "", proposal.name):
                    duplicate_prefixes.append(
                        {"mutation": _mutation_label(mutation), "entity": proposal.name}
                    )
    return {
        "explanatory_paragraphs": paragraph_counts,
        "thin_pages": thin_pages,
        "missing_epistemic_caveat": missing_caveat,
        "inventory_pages": inventory_pages,
        "duplicate_entity_prefixes": duplicate_prefixes,
        "passed": not thin_pages
        and not missing_caveat
        and not inventory_pages
        and not duplicate_prefixes,
    }


def _entity_editorial_quality(case: dict, plan: FilingPlan) -> dict:
    expectation = case.get("entity_editorial_quality", {})
    required = tuple(expectation.get("required", ()))
    proposals = {resolution_key(proposal.name): proposal for proposal in plan.entities}
    missing_entities = []
    missing_descriptions = []
    missing_description_coverage = []
    missing_fact_coverage = []
    description_fact_duplicates = []

    for required_entity in required:
        name = str(required_entity["name"])
        proposal = proposals.get(resolution_key(name))
        if proposal is None:
            missing_entities.append(name)
            continue
        description = (proposal.description or "").strip()
        if not description:
            missing_descriptions.append(name)
        elif not _term_expectation_met(
            (description,),
            required_entity.get("description_terms", ()),
            required_entity.get("description_term_groups", ()),
        ):
            missing_description_coverage.append(name)
        facts = tuple(fact for fact in proposal.facts if fact.strip())
        if not _term_expectation_met(
            facts,
            required_entity.get("fact_terms", ()),
            required_entity.get("fact_term_groups", ()),
        ):
            missing_fact_coverage.append(name)
        description_key = _entity_editorial_key(description)
        for fact in facts:
            if description_key and description_key == _entity_editorial_key(fact):
                description_fact_duplicates.append({"entity": name, "fact": fact})

    return {
        "required": [str(item["name"]) for item in required],
        "missing_entities": missing_entities,
        "missing_descriptions": missing_descriptions,
        "missing_description_coverage": missing_description_coverage,
        "missing_fact_coverage": missing_fact_coverage,
        "description_fact_duplicates": description_fact_duplicates,
        "passed": not missing_entities
        and not missing_descriptions
        and not missing_description_coverage
        and not missing_fact_coverage
        and not description_fact_duplicates,
    }


def _term_expectation_met(values, terms, term_groups) -> bool:
    normalized = tuple(_normalized_text(value) for value in values)
    return all(any(_normalized_text(term) in value for value in normalized) for term in terms) and (
        not term_groups
        or any(
            all(_normalized_text(term) in value for term in group)
            for group in term_groups
            for value in normalized
        )
    )


def _entity_editorial_key(value: str) -> str:
    return re.sub(r"[^\w]+", " ", _normalized_text(value)).strip()


def _seeded_update(case: dict, mutations: tuple[PageMutation, ...]) -> dict:
    expectation = case.get("seeded_update")
    if not expectation:
        return {"required": False, "missing_terms": [], "passed": True}
    path = str(expectation["path"])
    mutation = next(
        (item for item in mutations if item.action == "update" and item.path == path),
        None,
    )
    body = _normalized_text("" if mutation is None else mutation.body or "")
    missing = [
        str(term)
        for term in expectation.get("preserve_terms", ())
        if _normalized_text(term) not in body
    ]
    return {
        "required": True,
        "path": path,
        "missing_terms": missing,
        "passed": mutation is not None and not missing,
    }


def _explanatory_paragraph_count(body: str) -> int:
    count = 0
    for block in re.split(r"\n\s*\n", body):
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if not lines or all(line.startswith("#") for line in lines):
            continue
        if all(re.match(r"^(?:[-*+]|\d+[.)])\s+", line) for line in lines):
            continue
        prose = re.sub(r"\(Source:\s*`[^`]+`\)", "", " ".join(lines), flags=re.I)
        prose = re.sub(r"[#*_`]", "", prose)
        if len(re.findall(r"\b\w+\b", prose, flags=re.UNICODE)) >= 12:
            count += 1
    return count


def _looks_like_evidence_inventory(body: str) -> bool:
    listed = sum(
        1
        for line in body.splitlines()
        if re.match(r"^\s*(?:[-*+]|\d+[.)])\s+", line)
    )
    return listed >= 4 and _explanatory_paragraph_count(body) < 2


def _duplicated_name(body: str, name: str) -> bool:
    escaped = re.escape(name)
    return bool(
        re.search(
            rf"(?<!\w){escaped}(?!\w)(?:\s*\([^\n)]*\))?\s+{escaped}(?!\w)",
            body,
            flags=re.IGNORECASE,
        )
    )


def _connections(case: dict, mutations: tuple[PageMutation, ...]) -> dict:
    by_title = {resolution_key(_mutation_label(mutation)): mutation for mutation in mutations}
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


def _entity_relationships(mutations, resolutions, candidates, *, source_path: str) -> dict:
    missing = []
    for index, mutation in enumerate(mutations):
        body = mutation.body or ""
        for canonical in resolutions["resolved"].get(index, ()):
            names = [
                value for value, entries in candidates.items()
                if any(candidate == canonical for _proposal, candidate in entries)
            ]
            if not any(
                has_entity_relationship_evidence(body, name, (source_path,))
                for name in names
            ):
                missing.append({"mutation": mutation.title or mutation.path, "entity": canonical})
    return {"missing": missing, "passed": not missing}


def _entity_wikilinks(case, mutations, candidates) -> dict:
    """Reject links that pretend an internal entity anchor is a normal wiki page."""
    normal_targets = {
        resolution_key(value)
        for value in (
            *case.get("normal_page_targets", ()),
            *(
                _mutation_label(mutation)
                for mutation in mutations
                if mutation.role in {"note", "concept"} or mutation.action == "update"
            ),
        )
        if value
    }
    violations = []
    for mutation in mutations:
        for target in re.findall(r"\[\[([^\]|#]+)(?:#[^\]|]+)?(?:\|[^\]]+)?\]\]", mutation.body or ""):
            key = resolution_key(target)
            if key in candidates and key not in normal_targets:
                violations.append({"mutation": _mutation_label(mutation), "target": target})
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
