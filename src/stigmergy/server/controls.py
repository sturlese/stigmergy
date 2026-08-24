"""Validation for the versioned control-file set."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from stigmergy.server import entity_aliases, identity

IDENTITIES_PATH = "ops/identities.json"
REGISTRY_PATH = "ops/entity-registry.json"
SLACK_CHANNELS_PATH = "ops/slack-channels.json"
PATHS = (IDENTITIES_PATH, REGISTRY_PATH, SLACK_CHANNELS_PATH)


class ControlError(ValueError):
    pass


@dataclass(frozen=True)
class ControlSet:
    principals: dict
    registry: dict
    slack_channels: dict
    groups: frozenset[str]


def validate_texts(
    texts: dict[str, str],
    *,
    origins: dict[str, str] | None = None,
) -> ControlSet:
    missing = [path for path in PATHS if path not in texts]
    if missing:
        raise ControlError(f"required control file is missing: {missing[0]}")
    labels = origins or {}
    try:
        principals = identity.principals_from_text(
            texts[IDENTITIES_PATH],
            origin=labels.get(IDENTITIES_PATH, IDENTITIES_PATH),
        )
        registry = entity_aliases.registry_payload(
            texts[REGISTRY_PATH],
            labels.get(REGISTRY_PATH, REGISTRY_PATH),
        )
        channels = identity.channel_map_from_text(
            texts[SLACK_CHANNELS_PATH],
            origin=labels.get(SLACK_CHANNELS_PATH, SLACK_CHANNELS_PATH),
        )
    except (identity.IdentityError, ValueError) as error:
        raise ControlError(str(error)) from error

    groups = frozenset(
        group for principal in principals.values() for group in principal.groups
    )
    used = {
        group
        for value in channels.values()
        for group in (value or ())
    }
    for record in registry["entities"].values():
        for claim in (*record["claims"], *record["external_ids"]):
            used.update(claim.get("acl") or ())
    unknown = sorted(used - groups)
    if unknown:
        raise ControlError(f"control files use unknown group: {unknown[0]}")
    return ControlSet(principals, registry, channels, groups)


def validate_root(root: str) -> ControlSet:
    texts = {}
    origins = {}
    for relative in PATHS:
        path = Path(root, *relative.split("/"))
        if not path.is_file() or path.is_symlink():
            raise ControlError(f"required control file is missing: {relative}")
        try:
            texts[relative] = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as error:
            raise ControlError(f"required control file is unreadable: {relative}") from error
        origins[relative] = str(path)
    return validate_texts(texts, origins=origins)
