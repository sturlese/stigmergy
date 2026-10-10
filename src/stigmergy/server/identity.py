"""Principals, group-name rules, and the legacy file roster parsers used by the one-time import
and the evaluation corpora."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

from stigmergy.kernel import layers
from stigmergy.server.errors import IdentityError

UNRESTRICTED_GROUP = "brain-admins"
RESERVED_GROUP_NAMES = frozenset({"all"})
MAX_GROUP_NAME_CHARS = 64
MAX_GROUPS = 32
_GROUP_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

FILE_REMEDY = "use an explicit list of groups"
DOOR_REMEDY = "choose an audience configured for your identity"

TOKEN_STORE_ENV = "STIGMERGY_TOKEN_STORE"


@dataclass(frozen=True)
class Principal:
    subject: str
    display_name: str
    groups: tuple[str, ...]
    default_audience: tuple[str, ...] | None
    kind: str = "person"
    is_admin: bool = False
    tenant: str = ""
    operator: bool = False

    @property
    def audiences(self) -> tuple[str, ...] | None:
        return None if UNRESTRICTED_GROUP in self.groups else self.groups

    @property
    def administers(self) -> bool:
        return self.is_admin and self.kind == "person" and UNRESTRICTED_GROUP in self.groups

    @property
    def unrestricted(self) -> bool:
        return UNRESTRICTED_GROUP in self.groups

    @property
    def default_layer(self) -> str | None:
        """The layer a capture that names none files in; None when the principal has no default."""
        return None if not self.default_audience else self.default_audience[0]

    @property
    def publishable_layers(self) -> frozenset[str]:
        """Membership is the right to publish; `public` is open to every identity. Reading
        everything never grants publishing where the principal is not a member."""
        return frozenset({layers.PUBLIC, *(group for group in self.groups if group != UNRESTRICTED_GROUP)})


class LayerRefused(ValueError):
    pass


NOT_YOUR_LAYER = ("you cannot file a capture in a layer you are not a member of. You may file in {layers} "
                  "— name one of them in `audience`, or omit it to use your default layer")
NO_DEFAULT_LAYER = ("your identity has no default layer, so a capture must name its layer: send `audience` "
                    "as one of {layers}")


def resolve_capture_layer(principal: Principal, requested) -> tuple[str, ...] | None:
    """The one layer a capture files in, as a stored audience (`None` for `public`).

    A named layer wins; otherwise the principal's default. Nothing falls back to `public` silently.
    """
    allowed = ", ".join(sorted(principal.publishable_layers))
    if requested is None:
        layer = principal.default_layer
        if layer is None:
            raise LayerRefused(NO_DEFAULT_LAYER.format(layers=allowed))
    else:
        if isinstance(requested, str):
            raise LayerRefused(f'audience must be a list naming one layer, not a string — send ["{requested}"]')
        names = check_group_names(list(requested), origin="this submission", subject="audience",
                                  remedy=DOOR_REMEDY)
        if len(names) != 1:
            raise LayerRefused("a capture files in exactly one layer: send `audience` with one layer name")
        layer = names[0]
    if layer not in principal.publishable_layers:
        raise LayerRefused(NOT_YOUR_LAYER.format(layers=allowed))
    return layers.acl_of_layer(layer)


def check_group_names(
    names,
    *,
    origin: str,
    subject: str,
    remedy: str = FILE_REMEDY,
) -> tuple[str, ...]:
    if not isinstance(names, list):
        raise IdentityError(f"{subject} must be a list of group names in {origin}")
    if len(names) > MAX_GROUPS:
        raise IdentityError(f"{subject} exceeds the {MAX_GROUPS}-group limit in {origin}")
    result = []
    for value in names:
        if not isinstance(value, str):
            raise IdentityError(f"{subject} contains a non-string group in {origin}")
        name = value.strip()
        if (
            len(name) > MAX_GROUP_NAME_CHARS
            or not _GROUP_NAME_RE.fullmatch(name)
            or name.casefold() in RESERVED_GROUP_NAMES
            or name == "*"
        ):
            raise IdentityError(f"{subject} contains an invalid group name in {origin}: {remedy}")
        if name.casefold() == UNRESTRICTED_GROUP.casefold() and name != UNRESTRICTED_GROUP:
            raise IdentityError(f"{subject} contains a mis-cased unrestricted group in {origin}")
        if name in result:
            raise IdentityError(f"{subject} contains a duplicate group in {origin}")
        result.append(name)
    return tuple(result)


def _object_without_duplicate_keys(pairs):
    result = {}
    folded = set()
    for key, value in pairs:
        normalized = str(key).strip().casefold()
        if key in result or normalized in folded:
            raise IdentityError("identity configuration contains duplicate keys")
        result[key] = value
        folded.add(normalized)
    return result


def _load_json(text: str, *, origin: str) -> dict:
    try:
        value = json.loads(text, object_pairs_hook=_object_without_duplicate_keys)
    except json.JSONDecodeError as error:
        raise IdentityError(f"identity configuration is malformed in {origin}") from error
    if not isinstance(value, dict):
        raise IdentityError(f"identity configuration must be an object in {origin}")
    return value


def principals_from_text(text: str, *, origin: str) -> dict[str, Principal]:
    raw = _load_json(text, origin=origin)
    principals = {}
    for subject, value in raw.items():
        if str(subject).startswith("_"):
            if not isinstance(value, str):
                raise IdentityError(f"comment {subject!r} must contain prose in {origin}")
            continue
        if not isinstance(subject, str) or not subject.strip():
            raise IdentityError(f"identity name is invalid in {origin}")
        if not isinstance(value, dict) or set(value) != {
            "display_name",
            "groups",
            "default_audience",
        }:
            raise IdentityError(
                f"identity {subject!r} must define display_name, groups, and default_audience"
            )
        display_name = " ".join(str(value["display_name"] or "").split())
        if not display_name:
            raise IdentityError(f"identity {subject!r} has no display_name")
        groups = check_group_names(
            value["groups"],
            origin=origin,
            subject=f"identity {subject!r}",
        )
        default_value = value["default_audience"]
        default_audience = (
            None
            if default_value is None
            else check_group_names(
                default_value,
                origin=origin,
                subject=f"identity {subject!r} default_audience",
            )
        )
        if default_audience == ():
            raise IdentityError(f"identity {subject!r} has an empty default_audience")
        if (
            UNRESTRICTED_GROUP not in groups
            and default_audience is not None
            and not set(default_audience).issubset(groups)
        ):
            raise IdentityError(
                f"identity {subject!r} defaults to a group it does not hold"
            )
        principals[subject] = Principal(
            subject=subject,
            display_name=display_name,
            groups=groups,
            default_audience=default_audience,
        )
    if not principals:
        raise IdentityError(f"identity configuration contains no principals in {origin}")
    return principals


def principal_from_text(text: str, subject: str | None, *, origin: str) -> Principal:
    if not subject:
        raise IdentityError("no identity was provided")
    principals = principals_from_text(text, origin=origin)
    try:
        return principals[subject]
    except KeyError as error:
        raise IdentityError("identity is not configured") from error


def resolve_principal(identities_path: str, subject: str | None) -> Principal:
    if not identities_path:
        raise IdentityError("no identities file is configured")
    try:
        with open(identities_path, encoding="utf-8") as file:
            text = file.read()
    except OSError as error:
        raise IdentityError("identities file is unavailable") from error
    return principal_from_text(text, subject, origin=identities_path)


def resolve_audiences(
    identities_path: str, subject: str | None
) -> tuple[str, ...] | None:
    return resolve_principal(identities_path, subject).audiences


def known_groups_from_text(text: str, *, origin: str) -> set[str]:
    return {
        group
        for principal in principals_from_text(text, origin=origin).values()
        for group in principal.groups
    }


def channel_map_from_text(
    text: str, *, origin: str
) -> dict[str, tuple[str, ...] | None]:
    raw = _load_json(text, origin=origin)
    result = {}
    for channel_id, value in raw.items():
        if str(channel_id).startswith("_"):
            if not isinstance(value, str):
                raise IdentityError(f"comment {channel_id!r} must contain prose in {origin}")
            continue
        if not isinstance(channel_id, str) or not channel_id.strip():
            raise IdentityError(f"channel id is invalid in {origin}")
        if value is None:
            result[channel_id] = None
            continue
        groups = check_group_names(
            value,
            origin=origin,
            subject=f"channel {channel_id!r}",
        )
        if not groups:
            raise IdentityError(
                f"channel {channel_id!r} must be null or contain at least one group"
            )
        if UNRESTRICTED_GROUP in groups:
            raise IdentityError(f"channel {channel_id!r} cannot use the master group")
        result[channel_id] = groups
    return result


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def load_token_store(raw_json: str | None, path: str | None) -> dict[str, str]:
    if raw_json:
        text = raw_json
    elif path:
        try:
            with open(path, encoding="utf-8") as file:
                text = file.read()
        except OSError as error:
            raise IdentityError("token store is unavailable") from error
    else:
        raise IdentityError("no token store is configured")
    try:
        value = json.loads(text)
    except json.JSONDecodeError as error:
        raise IdentityError("token store is malformed") from error
    if not isinstance(value, dict) or not all(
        re.fullmatch(r"[0-9a-f]{64}", key)
        and isinstance(subject, str)
        and subject
        for key, subject in value.items()
    ):
        raise IdentityError("token store is malformed")
    return value
