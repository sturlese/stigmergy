from __future__ import annotations

import datetime as dt
import json
import os
import re

from stigmergy.kernel.normalize import resolution_key
from stigmergy.server.acl import visible

ENTITY_REGISTRY_RELPATH = "ops/entity-registry.json"
ENTITY_ID_RE = re.compile(
    r"^ent_[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


def default_path(repo_dir: str | None) -> str:
    return os.path.join(repo_dir, *ENTITY_REGISTRY_RELPATH.split("/")) if repo_dir else ""


def read_file(path: str | None) -> str | None:
    if not path or not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def registry_payload(text: str | None, origin: str) -> dict:
    if text is None:
        return {"version": 1, "entities": {}, "redirects": {}}
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as error:
        raise ValueError(f"entity registry {origin}: malformed JSON") from error
    if not isinstance(payload, dict) or payload.get("version") != 1:
        raise ValueError(f"entity registry {origin}: version 1 object is required")
    entities = payload.get("entities")
    redirects = payload.get("redirects")
    if not isinstance(entities, dict) or not isinstance(redirects, dict):
        raise ValueError(f"entity registry {origin}: entities and redirects objects are required")
    for entity_id, record in entities.items():
        _validate_record(entity_id, record, origin)
    expected_redirects = {}
    for entity_id, record in entities.items():
        for absorbed in record["absorbed_ids"]:
            if absorbed in entities or absorbed in expected_redirects:
                raise ValueError(f"entity registry {origin}: absorbed entity id is ambiguous")
            expected_redirects[absorbed] = entity_id
    for absorbed, canonical in redirects.items():
        if not isinstance(absorbed, str) or not isinstance(canonical, str):
            raise ValueError(f"entity registry {origin}: redirects must map strings to strings")
        if absorbed in entities or canonical not in entities:
            raise ValueError(f"entity registry {origin}: redirect target is invalid")
    if redirects != expected_redirects:
        raise ValueError(f"entity registry {origin}: redirects do not match absorbed_ids")
    return payload


def registry_from_text(text: str | None, origin: str) -> dict[str, dict]:
    payload = registry_payload(text, origin)
    return {
        entity_id: {"id": entity_id, **record}
        for entity_id, record in payload["entities"].items()
    }


def redirects_from_text(text: str | None, origin: str) -> dict[str, str]:
    return dict(registry_payload(text, origin)["redirects"])


def aliases_from_text(
    text: str | None,
    origin: str,
    *,
    audiences: set[str] | None = None,
) -> dict[str, str]:
    records = registry_from_text(text, origin)
    candidates: dict[str, set[str]] = {}
    for entity_id, record in records.items():
        claims = visible_claims(record, audiences)
        if not claims:
            continue
        for value in (entity_id, *(claim["value"] for claim in claims)):
            key = resolution_key(value)
            if key:
                candidates.setdefault(key, set()).add(entity_id)
    return {
        key: next(iter(entity_ids))
        for key, entity_ids in candidates.items()
        if len(entity_ids) == 1
    }


def load_aliases(path: str | None, *, audiences: set[str] | None = None) -> dict[str, str]:
    return aliases_from_text(read_file(path), path or "", audiences=audiences)


def load_registry(path: str | None) -> dict[str, dict]:
    return registry_from_text(read_file(path), path or "")


def visible_claims(record: dict, audiences: set[str] | None) -> list[dict]:
    return [claim for claim in record.get("claims", ()) if visible(claim.get("acl"), audiences)]


def display_claim(record: dict, audiences: set[str] | None) -> dict | None:
    claims = visible_claims(record, audiences)
    preferred = [claim for claim in claims if claim.get("kind") == "preferred"]
    pool = preferred or [claim for claim in claims if claim.get("kind") == "alias"]
    if not pool:
        return None
    return max(
        pool,
        key=lambda claim: (claim.get("introduced_at", ""), claim.get("claim_id", "")),
    )


def project_record(record: dict, audiences: set[str] | None) -> dict | None:
    display = display_claim(record, audiences)
    if display is None:
        return None
    claims = sorted(
        visible_claims(record, audiences),
        key=lambda claim: (claim.get("introduced_at", ""), claim.get("claim_id", "")),
        reverse=True,
    )
    aliases = []
    for claim in claims:
        value = claim["value"]
        if value != display["value"] and value not in aliases:
            aliases.append(value)
    return {
        "id": record["id"],
        "name": display["value"],
        "type": record["entity_type"],
        "aliases": aliases,
        "claims": claims,
    }


def resolve_entity(aliases: dict[str, str], question: str) -> str | None:
    q_norm = resolution_key(question)
    if not q_norm:
        return None
    best = (0, None)
    for key, entity_id in aliases.items():
        if len(key) <= best[0]:
            continue
        if re.search(rf"(?<![a-z0-9]){re.escape(key)}(?![a-z0-9])", q_norm):
            best = (len(key), entity_id)
    return best[1]


def resolve_exact(aliases: dict[str, str], value: str) -> str | None:
    return aliases.get(resolution_key(value)) if value else None


def _validate_record(entity_id: str, record, origin: str) -> None:
    if not isinstance(entity_id, str) or not ENTITY_ID_RE.fullmatch(entity_id):
        raise ValueError(f"entity registry {origin}: entity id is invalid")
    if not isinstance(record, dict):
        raise ValueError(f"entity registry {origin}: entity records must be objects")
    required = {
        "entity_type",
        "created_at",
        "updated_at",
        "claims",
        "external_ids",
        "absorbed_ids",
    }
    if (
        set(record) != required
        or not isinstance(record["entity_type"], str)
        or not record["entity_type"].strip()
        or not isinstance(record["claims"], list)
        or not record["claims"]
    ):
        raise ValueError(f"entity registry {origin}: entity record is invalid")
    if not isinstance(record["external_ids"], list) or not isinstance(record["absorbed_ids"], list):
        raise ValueError(f"entity registry {origin}: entity collections must be lists")
    created_at = _timestamp(record["created_at"], origin)
    updated_at = _timestamp(record["updated_at"], origin)
    if updated_at < created_at:
        raise ValueError(f"entity registry {origin}: entity timestamps are out of order")
    claim_ids = set()
    preferred_scopes = set()
    for claim in record["claims"]:
        if not isinstance(claim, dict) or set(claim) != {
            "claim_id",
            "value",
            "normalized",
            "kind",
            "acl",
            "source",
            "actor",
            "introduced_at",
        }:
            raise ValueError(f"entity registry {origin}: name claim is invalid")
        if (
            claim["kind"] not in {"preferred", "alias"}
            or not isinstance(claim["claim_id"], str)
            or not claim["claim_id"].strip()
            or claim["claim_id"] in claim_ids
            or not isinstance(claim["value"], str)
            or not claim["value"].strip()
            or claim["normalized"] != resolution_key(claim["value"])
        ):
            raise ValueError(f"entity registry {origin}: name claim is invalid")
        claim_ids.add(claim["claim_id"])
        scope = _acl(claim["acl"], origin)
        if claim["kind"] == "preferred":
            if scope in preferred_scopes:
                raise ValueError(f"entity registry {origin}: duplicate preferred name scope")
            preferred_scopes.add(scope)
        _provenance(claim, origin)
    if not preferred_scopes:
        raise ValueError(f"entity registry {origin}: entity requires a preferred name claim")
    external_keys = set()
    for external in record["external_ids"]:
        if not isinstance(external, dict) or set(external) != {
            "namespace",
            "value",
            "acl",
            "source",
            "actor",
            "introduced_at",
        }:
            raise ValueError(f"entity registry {origin}: external id claim is invalid")
        key = (
            external["namespace"],
            external["value"],
            _acl(external["acl"], origin),
            external["source"],
            external["actor"],
        )
        if not all(isinstance(value, str) and value.strip() for value in key[:2]) or key in external_keys:
            raise ValueError(f"entity registry {origin}: external id claim is invalid")
        external_keys.add(key)
        _provenance(external, origin)
    absorbed = record["absorbed_ids"]
    if (
        any(not isinstance(value, str) or not ENTITY_ID_RE.fullmatch(value) for value in absorbed)
        or len(set(absorbed)) != len(absorbed)
        or entity_id in absorbed
    ):
        raise ValueError(f"entity registry {origin}: absorbed_ids is invalid")


def _timestamp(value, origin: str) -> dt.datetime:
    if not isinstance(value, str):
        raise ValueError(f"entity registry {origin}: timestamp is invalid")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"entity registry {origin}: timestamp is invalid") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"entity registry {origin}: timestamp is invalid")
    return parsed.astimezone(dt.UTC)


def _acl(value, origin: str) -> tuple[str, ...] | None:
    if value is None:
        return None
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(group, str) and group.strip() for group in value)
        or len(set(value)) != len(value)
    ):
        raise ValueError(f"entity registry {origin}: claim ACL is invalid")
    return tuple(value)


def _provenance(value: dict, origin: str) -> None:
    source = value["source"]
    actor = value["actor"]
    if (
        not isinstance(source, str)
        or not source.startswith("sources/")
        or not source.endswith(".md")
        or not isinstance(actor, str)
        or not actor.strip()
    ):
        raise ValueError(f"entity registry {origin}: claim provenance is invalid")
    _timestamp(value["introduced_at"], origin)
