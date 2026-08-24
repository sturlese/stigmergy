from __future__ import annotations

import datetime as dt
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType

from stigmergy.capture.schema import EntityMergeEvidence
from stigmergy.entities.model import (
    EntityRecord,
    ExternalIdClaim,
    NameClaim,
    entity_path,
    load_entities,
    mint_entity_id,
    new_name_claim,
    registry_bytes,
    render_entity,
    with_preferred_claim,
)
from stigmergy.index.corpus import split_frontmatter_checked
from stigmergy.kernel.normalize import resolution_key
from stigmergy.knowledge.plan import EntityProposal


class EntityOperationError(RuntimeError):
    pass


@dataclass(frozen=True)
class ProposalResolution:
    proposal_ids: tuple[str, ...]
    name_candidates: Mapping[str, frozenset[str]]

    @classmethod
    def empty(cls) -> ProposalResolution:
        return cls((), MappingProxyType({}))

    def candidates(self, value: str) -> frozenset[str]:
        return self.name_candidates.get(resolution_key(value), frozenset())

    def resolve(self, value: str) -> str | None:
        candidates = self.candidates(value)
        return next(iter(candidates)) if len(candidates) == 1 else None


def apply_proposals(
    root: str,
    proposals: tuple[EntityProposal, ...],
    *,
    acl: tuple[str, ...] | None,
    source: str,
    readable_artifacts: tuple[str, ...],
    actor: str,
    at: dt.datetime,
    allowed_same_as: frozenset[str] = frozenset(),
) -> ProposalResolution:
    _validate_alias_evidence(root, source, readable_artifacts, proposals)
    records = load_entities(root)
    proposal_ids = []
    name_candidates: dict[str, set[str]] = {}
    assigned: set[str] = set()
    for proposal in proposals:
        entity_id = _strong_match(
            records,
            proposal,
            allowed_same_as=allowed_same_as,
        )
        if entity_id is None:
            entity_id = mint_entity_id()
        if entity_id in assigned:
            raise EntityOperationError("one entity proposal per identity is required")
        assigned.add(entity_id)
        claims = (
            new_name_claim(
                proposal.name,
                kind="preferred",
                acl=acl,
                source=source,
                actor=actor,
                introduced_at=at,
            ),
            *(
                new_name_claim(
                    alias,
                    kind="alias",
                    acl=acl,
                    source=source,
                    actor=actor,
                    introduced_at=at,
                )
                for alias in proposal.aliases
            ),
        )
        if entity_id not in records:
            external = _external_claim(proposal, acl=acl, source=source, actor=actor, at=at)
            records[entity_id] = EntityRecord(
                entity_id=entity_id,
                entity_type=proposal.entity_type,
                created_at=at,
                updated_at=at,
                claims=claims,
                external_ids=(external,) if external else (),
            )
        else:
            record = records[entity_id]
            if record.entity_type != proposal.entity_type:
                raise EntityOperationError("strong entity match conflicts with entity type")
            for claim in claims:
                if _same_claim_exists(record, claim):
                    continue
                if claim.kind == "preferred":
                    record = with_preferred_claim(record, claim)
                else:
                    record = replace(
                        record,
                        claims=(*record.claims, claim),
                        updated_at=max(record.updated_at, claim.introduced_at),
                    )
            external = _external_claim(proposal, acl=acl, source=source, actor=actor, at=at)
            if external and not any(
                (
                    item.namespace,
                    item.value,
                    item.acl,
                    item.source,
                )
                == (
                    external.namespace,
                    external.value,
                    external.acl,
                    external.source,
                )
                for item in record.external_ids
            ):
                record = replace(
                    record,
                    external_ids=(*record.external_ids, external),
                    updated_at=max(record.updated_at, at),
                )
            records[entity_id] = record
        proposal_ids.append(entity_id)
        for name in (proposal.name, *proposal.aliases):
            name_candidates.setdefault(resolution_key(name), set()).add(entity_id)
    write_records(root, records)
    return ProposalResolution(
        tuple(proposal_ids),
        MappingProxyType(
            {
                name: frozenset(entity_ids)
                for name, entity_ids in name_candidates.items()
            }
        ),
    )


def resolve_reference(records: dict[str, EntityRecord], value: str) -> str | None:
    if value in records:
        return value
    key = resolution_key(value)
    matches = {entity_id for entity_id, record in records.items() for claim in record.claims if claim.normalized == key}
    return next(iter(matches)) if len(matches) == 1 else None


def merge_entities(
    root: str,
    entity_ids: tuple[str, ...],
    *,
    at: dt.datetime,
    evidence: EntityMergeEvidence,
) -> str:
    records = load_entities(root)
    selected = []
    for entity_id in entity_ids:
        canonical = follow_redirect(records, entity_id)
        if canonical not in selected:
            selected.append(canonical)
    if len(selected) < 2 or any(entity_id not in records for entity_id in selected):
        raise EntityOperationError("merge requires distinct live entities")
    selected_records = tuple(records[entity_id] for entity_id in selected)
    if len({record.entity_type for record in selected_records}) != 1:
        raise EntityOperationError("entities with different types cannot be merged")
    _verify_merge_evidence(root, selected_records, evidence)
    ordered = sorted(selected, key=lambda value: (records[value].created_at, value))
    canonical_id = ordered[0]
    canonical = records[canonical_id]
    for absorbed_id in ordered[1:]:
        absorbed = records.pop(absorbed_id)
        canonical = replace(
            canonical,
            claims=_dedupe_claims((*canonical.claims, *absorbed.claims)),
            external_ids=_dedupe_external((*canonical.external_ids, *absorbed.external_ids)),
            absorbed_ids=tuple(dict.fromkeys((*canonical.absorbed_ids, absorbed_id, *absorbed.absorbed_ids))),
            updated_at=max(canonical.updated_at, absorbed.updated_at, at),
        )
        _unlink(root, absorbed.path)
        rewrite_entity_anchors(root, absorbed_id, canonical_id)
        for redirected in absorbed.absorbed_ids:
            rewrite_entity_anchors(root, redirected, canonical_id)
    records[canonical_id] = canonical
    write_records(root, records)
    return canonical_id


_IDENTITY_KIND = r"(?:entity|identity|person|organization|organisation|company|account)"
_IDENTITY_KIND_PLURAL = r"(?:entities|identities|people|persons|organizations|organisations|companies|accounts)"
_IDENTITY_LIST_RE = re.compile(
    rf"^(?P<subject>.+?) (?:"
    rf"are (?:the same {_IDENTITY_KIND}"
    rf"|aliases? for the same {_IDENTITY_KIND}"
    rf"|duplicate(?:d)? (?:[a-z0-9&+_-]+ )?{_IDENTITY_KIND_PLURAL})"
    rf"|refer to the same {_IDENTITY_KIND})$"
)
_IDENTITY_PAIR_RE = re.compile(
    rf"^(?P<left>.+?) (?:"
    rf"is (?:an? )?alias of"
    rf"|is another name for"
    rf"|is the same {_IDENTITY_KIND} as"
    rf"|refers to the same {_IDENTITY_KIND} as) (?P<right>.+)$"
)
_WORD_RE = re.compile(r"[a-z0-9]+(?:[&+_-][a-z0-9]+)*")


def _verify_merge_evidence(
    root: str,
    records: tuple[EntityRecord, ...],
    evidence: EntityMergeEvidence,
) -> None:
    if evidence.shared_external_id is not None:
        shared = evidence.shared_external_id
        if any(
            not any(
                claim.namespace == shared.namespace and claim.value == shared.value for claim in record.external_ids
            )
            for record in records
        ):
            raise EntityOperationError("shared external-id evidence is not present on every entity")
        return

    for item in evidence.source_assertions:
        body = _read_immutable_source(root, item.path)
        assertion = " ".join(item.assertion.split())
        readable = " ".join(body.split())
        if assertion not in readable:
            raise EntityOperationError("merge assertion is not present in the cited source")
        if not _assertion_binds_records(assertion, records):
            raise EntityOperationError("merge assertion does not unambiguously equate every selected entity")


def _read_immutable_source(root: str, relative: str) -> str:
    root_path = Path(root).resolve()
    path = root_path.joinpath(*relative.split("/"))
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise EntityOperationError("source does not exist") from error
    if not resolved.is_relative_to(root_path) or not resolved.is_file() or path.is_symlink():
        raise EntityOperationError("source must be a regular repository file")
    try:
        text = resolved.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise EntityOperationError("source could not be read") from error
    metadata, body, malformed = split_frontmatter_checked(text)
    if (
        malformed
        or metadata.get("type") != "source"
        or str(metadata.get("id") or "") != resolved.stem
    ):
        raise EntityOperationError("path is not an immutable source")
    return body


def _validate_alias_evidence(
    root: str,
    source: str,
    readable_artifacts: tuple[str, ...],
    proposals: tuple[EntityProposal, ...],
) -> None:
    aliases = tuple(alias for proposal in proposals for alias in proposal.aliases)
    if not aliases:
        return
    _read_immutable_source(root, source)
    artifact_tokens = tuple(_tokens(text) for text in readable_artifacts)
    for alias in aliases:
        alias_tokens = _tokens(alias)
        if not any(
            tokens[offset : offset + len(alias_tokens)] == alias_tokens
            for tokens in artifact_tokens
            for offset in range(len(tokens) - len(alias_tokens) + 1)
        ):
            raise EntityOperationError(
                "entity alias is not present in the immutable source"
            )


def _tokens(value: str) -> tuple[str, ...]:
    return tuple(_WORD_RE.findall(resolution_key(value)))


def _claim_sequences(record: EntityRecord) -> tuple[tuple[str, ...], ...]:
    values = tuple(dict.fromkeys(_tokens(claim.value) for claim in record.claims))
    return tuple(value for value in values if value)


def _sequence_inside(left: tuple[str, ...], right: tuple[str, ...]) -> bool:
    if len(left) > len(right):
        left, right = right, left
    return any(right[index : index + len(left)] == left for index in range(len(right) - len(left) + 1))


def _list_subject_binds(
    subject: str,
    records: tuple[EntityRecord, ...],
) -> bool:
    words = _tokens(subject)
    candidates = []
    for record in records:
        matches = []
        for name in _claim_sequences(record):
            for start in range(len(words) - len(name) + 1):
                if words[start : start + len(name)] == name:
                    matches.append((start, start + len(name), name))
        if not matches:
            return False
        candidates.append(tuple(dict.fromkeys(matches)))
    order = sorted(range(len(records)), key=lambda index: len(candidates[index]))
    states = 0

    def assign(
        offset: int,
        used: frozenset[int],
        names: tuple[tuple[str, ...], ...],
    ) -> bool:
        nonlocal states
        states += 1
        if states > 10_000:
            return False
        if offset == len(order):
            remaining = [word for index, word in enumerate(words) if index not in used]
            return bool(remaining) and all(word == "and" for word in remaining)
        for start, end, name in candidates[order[offset]]:
            positions = frozenset(range(start, end))
            if positions & used or any(_sequence_inside(name, other) for other in names):
                continue
            if assign(offset + 1, used | positions, (*names, name)):
                return True
        return False

    return assign(0, frozenset(), ())


def _pair_subject_binds(
    left: str,
    right: str,
    records: tuple[EntityRecord, ...],
) -> bool:
    if len(records) != 2:
        return False
    sides = (_tokens(left), _tokens(right))
    if not all(sides):
        return False
    for first, second in ((records[0], records[1]), (records[1], records[0])):
        if (
            sides[0] in _claim_sequences(first)
            and sides[1] in _claim_sequences(second)
            and not _sequence_inside(sides[0], sides[1])
        ):
            return True
    return False


def _assertion_binds_records(
    assertion: str,
    records: tuple[EntityRecord, ...],
) -> bool:
    normalized = " ".join(_tokens(assertion))
    listed = _IDENTITY_LIST_RE.fullmatch(normalized)
    if listed is not None:
        return _list_subject_binds(listed.group("subject"), records)
    paired = _IDENTITY_PAIR_RE.fullmatch(normalized)
    return bool(
        paired
        and _pair_subject_binds(
            paired.group("left"),
            paired.group("right"),
            records,
        )
    )


def delete_entity(root: str, entity_id: str) -> None:
    records = load_entities(root)
    live = follow_redirect(records, entity_id)
    record = records.pop(live, None)
    if record is None:
        raise EntityOperationError("entity does not exist")
    _unlink(root, record.path)
    sweep_entity_anchors(root, {live, *record.absorbed_ids})
    write_records(root, records)


def remove_source_claims(
    root: str,
    sources: set[str],
    *,
    at: dt.datetime,
) -> tuple[str, ...]:
    if not sources:
        return ()
    records = load_entities(root)
    changed: set[str] = set()
    removed_ids: set[str] = set()
    for entity_id, record in tuple(records.items()):
        claims = tuple(claim for claim in record.claims if claim.source not in sources)
        external_ids = tuple(claim for claim in record.external_ids if claim.source not in sources)
        if claims == record.claims and external_ids == record.external_ids:
            continue
        changed.add(record.path)
        if not claims:
            records.pop(entity_id)
            removed_ids.update((entity_id, *record.absorbed_ids))
            _unlink(root, record.path)
            continue
        records[entity_id] = replace(
            record,
            claims=_promote_missing_preferred(claims),
            external_ids=external_ids,
            updated_at=max(record.updated_at, at),
        )
    if not changed:
        return ()
    if removed_ids:
        changed.update(sweep_entity_anchors(root, removed_ids))
    write_records(root, records)
    changed.add("ops/entity-registry.json")
    return tuple(sorted(changed))


def follow_redirect(records: dict[str, EntityRecord], entity_id: str) -> str:
    for canonical, record in records.items():
        if entity_id in record.absorbed_ids:
            return canonical
    return entity_id


def write_records(root: str, records: dict[str, EntityRecord]) -> None:
    for entity_id, record in records.items():
        path = os.path.join(root, *entity_path(entity_id).split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(render_entity(record))
    registry = os.path.join(root, "ops", "entity-registry.json")
    os.makedirs(os.path.dirname(registry), exist_ok=True)
    with open(registry, "wb") as handle:
        handle.write(registry_bytes(records))


def rewrite_entity_anchors(root: str, old: str, new: str) -> tuple[str, ...]:
    return _rewrite_anchor_values(root, {old}, new)


def sweep_entity_anchors(root: str, removed: set[str]) -> tuple[str, ...]:
    return _rewrite_anchor_values(root, removed, None)


def _rewrite_anchor_values(
    root: str,
    targets: set[str],
    replacement: str | None,
) -> tuple[str, ...]:
    import yaml

    from stigmergy.index.corpus import split_frontmatter_checked

    changed = []
    for folder in ("wiki/notes", "wiki/concepts"):
        base = os.path.join(root, *folder.split("/"))
        if not os.path.isdir(base):
            continue
        for name in sorted(os.listdir(base)):
            if not name.endswith(".md"):
                continue
            path = os.path.join(base, name)
            with open(path, encoding="utf-8") as handle:
                text = handle.read()
            metadata, body, malformed = split_frontmatter_checked(text)
            if malformed or not isinstance(metadata.get("entity"), list):
                continue
            before = list(metadata["entity"])
            after = [replacement if value in targets and replacement else value for value in before]
            after = [value for value in after if value not in targets]
            after = list(dict.fromkeys(after))
            rewritten_body = _rewrite_entity_links(body, targets, replacement)
            if after == before and rewritten_body == body:
                continue
            metadata["entity"] = after
            front = yaml.safe_dump(metadata, allow_unicode=True, sort_keys=False, width=1000).rstrip()
            with open(path, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(f"---\n{front}\n---\n\n{rewritten_body.strip()}\n")
            changed.append(os.path.relpath(path, root).replace(os.sep, "/"))
    return tuple(changed)


_WIKILINK_RE = re.compile(r"\[\[([^\[\]]+?)\]\]")


def _rewrite_entity_links(body: str, targets: set[str], replacement: str | None) -> str:
    def rewrite(match):
        target, separator, label = match.group(1).partition("|")
        cleaned = target.strip().removesuffix(".md")
        entity_id = cleaned.rsplit("/", 1)[-1]
        if entity_id not in targets:
            return match.group(0)
        if replacement:
            prefix = cleaned[: -len(entity_id)]
            rewritten = f"{prefix}{replacement}"
            return f"[[{rewritten}|{label}]]" if separator else f"[[{rewritten}]]"
        return label.strip() if separator else target.strip()

    return _WIKILINK_RE.sub(rewrite, body)


def _strong_match(
    records: dict[str, EntityRecord],
    proposal: EntityProposal,
    *,
    allowed_same_as: frozenset[str],
) -> str | None:
    if proposal.same_as:
        visible_records = {entity_id: record for entity_id, record in records.items() if entity_id in allowed_same_as}
        found = resolve_reference(visible_records, proposal.same_as)
        if found is None:
            raise EntityOperationError("same_as does not identify one entity")
        return found
    if proposal.external_namespace and proposal.external_id:
        matches = [
            entity_id
            for entity_id, record in records.items()
            if any(
                claim.namespace == proposal.external_namespace and claim.value == proposal.external_id
                for claim in record.external_ids
            )
        ]
        if len(matches) > 1:
            raise EntityOperationError("external identifier matches multiple entities")
        if matches:
            return matches[0]
    return None


def _external_claim(
    proposal: EntityProposal,
    *,
    acl: tuple[str, ...] | None,
    source: str,
    actor: str,
    at: dt.datetime,
) -> ExternalIdClaim | None:
    if not proposal.external_namespace or not proposal.external_id:
        return None
    return ExternalIdClaim(
        namespace=proposal.external_namespace,
        value=proposal.external_id,
        acl=acl,
        source=source,
        actor=actor,
        introduced_at=at,
    )


def _same_claim_exists(record: EntityRecord, claim: NameClaim) -> bool:
    return any(
        current.normalized == claim.normalized
        and current.kind == claim.kind
        and current.acl == claim.acl
        and current.source == claim.source
        for current in record.claims
    )


def _dedupe_claims(claims: tuple[NameClaim, ...]) -> tuple[NameClaim, ...]:
    seen: set[tuple] = set()
    result = []
    for claim in claims:
        key = (claim.normalized, claim.kind, claim.acl, claim.source, claim.actor)
        if key not in seen:
            seen.add(key)
            result.append(claim)
    preferred: set[tuple[str, ...] | None] = set()
    normalized = []
    for claim in sorted(result, key=lambda item: item.introduced_at, reverse=True):
        if claim.kind == "preferred" and claim.acl in preferred:
            claim = replace(claim, kind="alias")
        if claim.kind == "preferred":
            preferred.add(claim.acl)
        normalized.append(claim)
    return tuple(sorted(normalized, key=lambda item: item.introduced_at))


def _dedupe_external(claims: tuple[ExternalIdClaim, ...]) -> tuple[ExternalIdClaim, ...]:
    seen = set()
    result = []
    for claim in claims:
        key = (claim.namespace, claim.value, claim.acl, claim.source, claim.actor)
        if key not in seen:
            seen.add(key)
            result.append(claim)
    return tuple(result)


def _promote_missing_preferred(claims: tuple[NameClaim, ...]) -> tuple[NameClaim, ...]:
    preferred = {claim.acl for claim in claims if claim.kind == "preferred"}
    promote = {
        scope: max(
            (claim for claim in claims if claim.acl == scope),
            key=lambda claim: (claim.introduced_at, claim.claim_id),
        ).claim_id
        for scope in {claim.acl for claim in claims} - preferred
    }
    return tuple(
        replace(claim, kind="preferred") if promote.get(claim.acl) == claim.claim_id else claim for claim in claims
    )


def _unlink(root: str, relative: str) -> None:
    try:
        os.unlink(os.path.join(root, *relative.split("/")))
    except FileNotFoundError:
        pass
