from __future__ import annotations

import datetime as dt
import json
import re
import uuid
from dataclasses import dataclass, replace
from pathlib import Path

import yaml

from stigmergy.index.corpus import split_frontmatter_checked
from stigmergy.kernel.normalize import resolution_key

ENTITY_ID_RE = re.compile(r"^ent_[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
ENTITY_FOLDER = "wiki/entities"
REGISTRY_PATH = "ops/entity-registry.json"


class EntityContractError(ValueError):
    pass


@dataclass(frozen=True)
class NameClaim:
    claim_id: str
    value: str
    normalized: str
    kind: str
    acl: tuple[str, ...] | None
    source: str
    actor: str
    introduced_at: dt.datetime


@dataclass(frozen=True)
class ExternalIdClaim:
    namespace: str
    value: str
    acl: tuple[str, ...] | None
    source: str
    actor: str
    introduced_at: dt.datetime


@dataclass(frozen=True)
class EntityRecord:
    entity_id: str
    entity_type: str
    created_at: dt.datetime
    updated_at: dt.datetime
    claims: tuple[NameClaim, ...]
    external_ids: tuple[ExternalIdClaim, ...] = ()
    absorbed_ids: tuple[str, ...] = ()

    @property
    def path(self) -> str:
        return entity_path(self.entity_id)


def mint_entity_id() -> str:
    return f"ent_{uuid.uuid4()}"


def entity_path(entity_id: str) -> str:
    if not ENTITY_ID_RE.fullmatch(entity_id):
        raise EntityContractError("invalid entity id")
    return f"{ENTITY_FOLDER}/{entity_id}.md"


def new_name_claim(
    value: str,
    *,
    kind: str,
    acl: tuple[str, ...] | None,
    source: str,
    actor: str,
    introduced_at: dt.datetime,
) -> NameClaim:
    cleaned = " ".join(value.split())
    normalized = resolution_key(cleaned)
    if not cleaned or not normalized:
        raise EntityContractError("entity name is empty")
    if kind not in {"preferred", "alias"}:
        raise EntityContractError("name claim kind is invalid")
    return NameClaim(
        claim_id=f"claim_{uuid.uuid4()}",
        value=cleaned,
        normalized=normalized,
        kind=kind,
        acl=acl,
        source=source,
        actor=actor,
        introduced_at=_utc(introduced_at),
    )


def render_entity(record: EntityRecord) -> str:
    _validate_record(record)
    metadata = {
        "id": record.entity_id,
        "type": "entity",
        "entity_type": record.entity_type,
        "created_at": _iso(record.created_at),
        "updated_at": _iso(record.updated_at),
        "claims": [_claim_dict(claim) for claim in record.claims],
        "external_ids": [_external_dict(claim) for claim in record.external_ids],
        "absorbed_ids": list(record.absorbed_ids),
    }
    frontmatter = yaml.safe_dump(
        metadata,
        allow_unicode=True,
        sort_keys=False,
        width=1000,
    ).rstrip()
    return f"---\n{frontmatter}\n---\n\n# {record.entity_id}\n"


def parse_entity(path: str, text: str) -> EntityRecord:
    metadata, body, malformed = split_frontmatter_checked(text)
    if malformed or not metadata:
        raise EntityContractError("entity frontmatter is invalid")
    entity_id = str(metadata.get("id") or "")
    if path != entity_path(entity_id) or metadata.get("type") != "entity":
        raise EntityContractError("entity id, type, or path is invalid")
    if body.strip() != f"# {entity_id}":
        raise EntityContractError("entity body must contain only its stable id heading")
    claims_raw = metadata.get("claims")
    external_raw = metadata.get("external_ids")
    absorbed_raw = metadata.get("absorbed_ids")
    if not isinstance(claims_raw, list) or not isinstance(external_raw, list):
        raise EntityContractError("entity claim collections must be lists")
    if not isinstance(absorbed_raw, list):
        raise EntityContractError("absorbed_ids must be a list")
    try:
        record = EntityRecord(
            entity_id=entity_id,
            entity_type=_required_text(metadata.get("entity_type"), "entity_type"),
            created_at=_timestamp(metadata.get("created_at")),
            updated_at=_timestamp(metadata.get("updated_at")),
            claims=tuple(_parse_claim(value) for value in claims_raw),
            external_ids=tuple(_parse_external(value) for value in external_raw),
            absorbed_ids=tuple(_entity_ids(absorbed_raw)),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise EntityContractError("entity fields are invalid") from error
    _validate_record(record)
    return record


def load_entities(root: str) -> dict[str, EntityRecord]:
    folder = Path(root, *ENTITY_FOLDER.split("/"))
    records: dict[str, EntityRecord] = {}
    if not folder.is_dir():
        return records
    for path in sorted(folder.glob("*.md")):
        relative = path.relative_to(root).as_posix()
        record = parse_entity(relative, path.read_text(encoding="utf-8"))
        if record.entity_id in records:
            raise EntityContractError("duplicate entity id")
        records[record.entity_id] = record
    return records


def registry_bytes(records: dict[str, EntityRecord]) -> bytes:
    redirects: dict[str, str] = {}
    for entity_id, record in sorted(records.items()):
        for absorbed in record.absorbed_ids:
            if absorbed in records or absorbed in redirects:
                raise EntityContractError("absorbed entity id is ambiguous")
            redirects[absorbed] = entity_id
    payload = {
        "version": 1,
        "entities": {
            entity_id: {
                "entity_type": record.entity_type,
                "created_at": _iso(record.created_at),
                "updated_at": _iso(record.updated_at),
                "claims": [_claim_dict(claim) for claim in record.claims],
                "external_ids": [_external_dict(claim) for claim in record.external_ids],
                "absorbed_ids": list(record.absorbed_ids),
            }
            for entity_id, record in sorted(records.items())
        },
        "redirects": dict(sorted(redirects.items())),
    }
    return (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()


def with_preferred_claim(record: EntityRecord, claim: NameClaim) -> EntityRecord:
    claims = []
    for current in record.claims:
        if current.kind == "preferred" and current.acl == claim.acl:
            claims.append(replace(current, kind="alias"))
        else:
            claims.append(current)
    claims.append(claim)
    return replace(
        record,
        claims=tuple(claims),
        updated_at=max(record.updated_at, claim.introduced_at),
    )


def _claim_dict(claim: NameClaim) -> dict:
    return {
        "claim_id": claim.claim_id,
        "value": claim.value,
        "normalized": claim.normalized,
        "kind": claim.kind,
        "acl": None if claim.acl is None else list(claim.acl),
        "source": claim.source,
        "actor": claim.actor,
        "introduced_at": _iso(claim.introduced_at),
    }


def _external_dict(claim: ExternalIdClaim) -> dict:
    return {
        "namespace": claim.namespace,
        "value": claim.value,
        "acl": None if claim.acl is None else list(claim.acl),
        "source": claim.source,
        "actor": claim.actor,
        "introduced_at": _iso(claim.introduced_at),
    }


def _parse_claim(value) -> NameClaim:
    if not isinstance(value, dict):
        raise EntityContractError("name claim must be an object")
    return NameClaim(
        claim_id=_required_text(value.get("claim_id"), "claim_id"),
        value=_required_text(value.get("value"), "value"),
        normalized=_required_text(value.get("normalized"), "normalized"),
        kind=_required_text(value.get("kind"), "kind"),
        acl=_acl(value.get("acl")),
        source=_required_text(value.get("source"), "source"),
        actor=_required_text(value.get("actor"), "actor"),
        introduced_at=_timestamp(value.get("introduced_at")),
    )


def _parse_external(value) -> ExternalIdClaim:
    if not isinstance(value, dict):
        raise EntityContractError("external id claim must be an object")
    return ExternalIdClaim(
        namespace=_required_text(value.get("namespace"), "namespace"),
        value=_required_text(value.get("value"), "value"),
        acl=_acl(value.get("acl")),
        source=_required_text(value.get("source"), "source"),
        actor=_required_text(value.get("actor"), "actor"),
        introduced_at=_timestamp(value.get("introduced_at")),
    )


def _validate_record(record: EntityRecord) -> None:
    entity_path(record.entity_id)
    if not record.entity_type.strip() or not record.claims:
        raise EntityContractError("entity requires a type and at least one name claim")
    if record.updated_at < record.created_at:
        raise EntityContractError("entity timestamps are out of order")
    claim_ids = set()
    preferred_scopes = set()
    for claim in record.claims:
        if claim.claim_id in claim_ids:
            raise EntityContractError("duplicate claim id")
        claim_ids.add(claim.claim_id)
        if claim.normalized != resolution_key(claim.value):
            raise EntityContractError("name claim normalization is not reproducible")
        if claim.kind not in {"preferred", "alias"}:
            raise EntityContractError("name claim kind is invalid")
        if claim.kind == "preferred" and claim.acl in preferred_scopes:
            raise EntityContractError("entity has multiple preferred names for one audience")
        if claim.kind == "preferred":
            preferred_scopes.add(claim.acl)
        _validate_provenance(claim.source, claim.actor, claim.introduced_at)
    if not preferred_scopes:
        raise EntityContractError("entity requires a preferred name")
    for external in record.external_ids:
        _validate_provenance(external.source, external.actor, external.introduced_at)
    _entity_ids(record.absorbed_ids)
    if record.entity_id in record.absorbed_ids:
        raise EntityContractError("entity cannot absorb itself")


def _validate_provenance(source: str, actor: str, introduced_at: dt.datetime) -> None:
    if not source.startswith("sources/") or not source.endswith(".md") or not actor.strip():
        raise EntityContractError("entity claim provenance is invalid")
    _utc(introduced_at)


def _entity_ids(values) -> tuple[str, ...]:
    if not all(isinstance(value, str) and ENTITY_ID_RE.fullmatch(value) for value in values):
        raise EntityContractError("absorbed_ids contains an invalid id")
    if len(set(values)) != len(values):
        raise EntityContractError("absorbed_ids contains duplicates")
    return tuple(values)


def _acl(value) -> tuple[str, ...] | None:
    if value is None:
        return None
    if not isinstance(value, list) or not value or not all(isinstance(v, str) and v for v in value):
        raise EntityContractError("claim acl is invalid")
    if len(set(value)) != len(value):
        raise EntityContractError("claim acl contains duplicates")
    return tuple(value)


def _required_text(value, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EntityContractError(f"{field} is required")
    return value.strip()


def _timestamp(value) -> dt.datetime:
    if not isinstance(value, str):
        raise EntityContractError("timestamp is required")
    return _utc(dt.datetime.fromisoformat(value.replace("Z", "+00:00")))


def _utc(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise EntityContractError("timestamp must include a timezone")
    return value.astimezone(dt.UTC)


def _iso(value: dt.datetime) -> str:
    return _utc(value).isoformat()
