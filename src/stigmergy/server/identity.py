"""Identity → audiences resolution, and the ONE grammar both `ops/` group files are written in.

Two halves, as ADR 013 drew them: the file seam (`ops/identities.json`) and the per-request half
(bearer token → sha256 → token store → email → the SAME `resolve_audiences`). Fail-closed on
EVERY path: any failure raises `IdentityError`; the server never starts open.

**One value shape** (ADR 045 D7): a principal maps to a LIST OF GROUPS and to nothing else —
`{"marc": ["brain-admins"], "ana": ["finance"], "bob": []}`. The `"*"` and bare-label spellings
this file once accepted are refused by name, with the line to write instead, because a roster is
read on every request and three spellings for one fact is three things to get right. Unrestricted
is membership of `brain-admins`, a group the identity provider will also have, so the later
resolver swap is a swap and not a relabel.

**Open is the ABSENCE of a label, never a group called `all`.** `acl.visible()` has always read a
page with no `acl:` as visible to everyone, so "every authenticated identity sees every open page"
costs no injection anywhere: an identity with `[]` reads open pages and no others. A group
literally named `all` would be the opposite of what its author meant — a page labelled `[all]` is
restricted to whoever holds that label — so the name is reserved and refused in both files.

`ops/slack-channels.json` is the same grammar with a different principal (`slack.channels`
delegates its parse here), and one difference stated in `group_map_from_text`: an identity's
groups may resolve to unrestricted, a channel's never do.

The identities file is CONFIGURATION, not authentication — anyone who can edit it or set
`--identity` impersonates anyone, acceptable only for one local operator over stdio. The bearer
token, which an impersonator cannot fabricate, is what the public HTTP transport verifies.
"""
import hashlib
import json
import os

from stigmergy.server.errors import IdentityError

# Membership of this group IS the unrestricted scope: `resolve_audiences` returns `None` for a
# principal that holds it, which is the value `acl.visible()` has always read as "sees everything".
# A NAME rather than a sigil, because the identity provider that replaces this file has groups and
# has no sigils.
UNRESTRICTED_GROUP = "brain-admins"

# Refused as a group name in either file, and as an `audience` value at the door. See the module
# docstring: open is the absence of a label.
RESERVED_GROUP_NAMES = frozenset({"all"})

DEFAULT_RELATIVE = os.path.join("ops", "identities.json")


def check_group_names(names, *, origin: str, subject: str) -> tuple[str, ...]:
    """Validate a list of group names and return it normalized, or raise `IdentityError`.

    The one place the vocabulary's rules live, so the roster, the channel map and the door's own
    `audience` argument cannot come to disagree about what a group may be called. A comma is
    refused because a label list is CSV-serialized on at least one road (`acl.visible()` still
    normalizes a bare CSV string), and one comma inside a name would silently become two groups at
    enforcement time; `all` is refused for the reason in the module docstring.
    """
    if not isinstance(names, list):
        raise IdentityError(
            f"{subject} has a malformed group list in {origin}: expected a list of group names, "
            f"got {type(names).__name__}"
            + (' — the "*" spelling was retired: write ["brain-admins"] instead (ADR 045)'
               if names == "*" else
               f' — a bare label was retired: write ["{names}"] instead (ADR 045)'
               if isinstance(names, str) and names.strip() else ""))
    out = []
    for raw in names:
        if not isinstance(raw, str):
            raise IdentityError(
                f"{subject} names a group that is not a string in {origin}: "
                f"got {type(raw).__name__}")
        name = raw.strip()
        if not name or "," in name:
            raise IdentityError(
                f"{subject} names an invalid group {raw!r} in {origin} (a group name must be "
                f"non-empty and must not contain ',')")
        if name in RESERVED_GROUP_NAMES:
            raise IdentityError(
                f"{subject} names the reserved group {name!r} in {origin} — open is the ABSENCE "
                f"of a label, so a page labelled {name!r} would be restricted to a group by that "
                f"name rather than open to everyone. Remove it: a principal with no groups "
                f"already reads every open page")
        if name not in out:
            out.append(name)
    return tuple(out)


def group_map_from_text(text: str, *, origin: str, subject: str) -> dict[str, tuple[str, ...]]:
    """`{principal: (group, …)}` from one control file's TEXT, or raise `IdentityError`.

    The WHOLE file is validated, not only the entry being looked up: a malformed neighbour in an
    access-scoping file is a file the server cannot make sense of, and "the entry I wanted happened
    to parse" is not a property to grant access on. `subject` names what a key is ("identity",
    "channel") so a refusal reads in the operator's own vocabulary.

    An EMPTY text is malformed JSON and raises — on this road "nobody is listed" is spelled `{}`,
    a committed statement, never bytes that failed to arrive.

    **A key beginning with `_` is a comment and is dropped**, so an operator can say in the file
    itself which channel `C0BL6QH7AQN` is. Dropped rather than exempted from validation: the key
    never reaches the map, so looking that name up is an `unknown` refusal like any other — the
    fail-closed direction. No email and no Slack channel id begins with an underscore.
    """
    try:
        data = json.loads(text)
    except json.JSONDecodeError as ex:
        raise IdentityError(f"{subject} map malformed: {origin}: {ex}") from ex
    if not isinstance(data, dict):
        raise IdentityError(
            f"{subject} map malformed: {origin} "
            f"(expected an object mapping {subject} -> [group, ...])")
    return {str(key): check_group_names(value, origin=origin, subject=f"{subject} {key!r}")
            for key, value in data.items() if not str(key).startswith("_")}


def default_path(repo_dir: str | None) -> str:
    """The conventional identities file inside a knowledge-repo checkout, or '' when no repo is
    given (the resolver then fails closed with an actionable message)."""
    return os.path.join(repo_dir, DEFAULT_RELATIVE) if repo_dir else ""


def resolve_audiences(identities_path: str, identity: str | None) -> tuple[str, ...] | None:
    """Resolve `identity` to its audience tuple through the identities FILE, or None for
    unrestricted. Raises IdentityError on any failure — the caller must not proceed without a
    resolved scope. A caller holding the file's TEXT already (the index's snapshot) uses
    `audiences_from_text`, the same parse under this one."""
    if not identity:
        raise IdentityError(
            "no identity given: pass --identity <name> or set STIGMERGY_IDENTITY "
            "(the server never starts without a resolved identity)")
    if not identities_path:
        raise IdentityError(
            "no identities file configured: pass --identities <path> or --repo <knowledge repo> "
            f"(so the resolver can read <repo>/{DEFAULT_RELATIVE})")
    if not os.path.exists(identities_path):
        raise IdentityError(
            f"identities file not found: {identities_path} "
            '(create it, e.g. {"marc": ["brain-admins"], "ana": ["finance"]})')
    try:
        with open(identities_path, encoding="utf-8") as f:
            text = f.read()
    except OSError as ex:
        raise IdentityError(f"identities file unreadable: {identities_path}: {ex}") from ex
    return audiences_from_text(text, identity, origin=identities_path)


def audiences_from_text(text: str, identity: str | None, *, origin: str) -> tuple[str, ...] | None:
    """`identity` -> its group tuple, or `None` for an unrestricted one, from the roster's TEXT.

    The one parse under both roads, so the index's snapshot and a `--identities` file cannot mean
    different things. Fail-closed on EVERY path: malformed JSON, a non-object top level, an unknown
    identity and any malformed value all raise `IdentityError` — and an EMPTY text is malformed
    JSON, so an empty snapshot resolves NOBODY, never everybody. `origin` names the source in the
    operator-facing message only; the HTTP middleware never echoes these sentences to a caller.

    An identity with `[]` is authenticated and holds no group: it reads every open page and
    nothing else. That is a fact about the PRINCIPAL and is not the `acl: []` of a page, which
    means nobody — see `server.acl`'s truth table and ADR 045 D9.
    """
    if not identity:
        raise IdentityError("no identity given to resolve")
    groups = group_map_from_text(text, origin=origin, subject="identity")
    if identity not in groups:
        known = ", ".join(sorted(groups)) or "(none)"
        raise IdentityError(f"unknown identity {identity!r} (known: {known})")
    labels = groups[identity]
    return None if UNRESTRICTED_GROUP in labels else labels


# ── per-request token resolution (HTTP transport) ──────────────────────────────────────────────
# The token store is a deploy secret shaped {"<sha256hex>": "<email>"}: plaintext bearer tokens
# are NEVER stored — only their hash. Issuance is `stigmergy-issue-token`.
TOKEN_STORE_ENV = "STIGMERGY_TOKEN_STORE"            # inline JSON (a Fly secret, typically)
TOKEN_STORE_FILE_ENV = "STIGMERGY_TOKEN_STORE_FILE"  # a path to the same JSON shape


# 256 bits of entropy; `secrets.token_urlsafe` base64-encodes it (~43 chars). Every minter — the
# per-user token and the console's one credential — draws from this figure, never its own.
TOKEN_BYTES = 32


def hash_token(token: str) -> str:
    """SHA-256 hex digest of a plaintext bearer token — the ONLY form ever written to disk, a
    log, or a repo."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def load_token_store(raw_json: str | None, path: str | None) -> dict[str, str]:
    """Load the `{sha256hex: email}` token store: inline JSON (`$STIGMERGY_TOKEN_STORE`, the usual
    deploy-secret shape) takes priority over a file path (`$STIGMERGY_TOKEN_STORE_FILE`). Fail
    closed — both absent, unreadable, or malformed all raise `IdentityError`; the HTTP transport
    must never start auth open."""
    if raw_json:
        text, where = raw_json, f"${TOKEN_STORE_ENV}"
    elif path:
        if not os.path.exists(path):
            raise IdentityError(f"token store file not found: {path}")
        try:
            with open(path, encoding="utf-8") as f:
                text = f.read()
        except OSError as ex:
            raise IdentityError(f"token store file unreadable: {path}: {ex}") from ex
        where = path
    else:
        raise IdentityError(
            f"no token store configured: set ${TOKEN_STORE_ENV} (inline JSON) or "
            f"${TOKEN_STORE_FILE_ENV} (a path) — the HTTP transport never starts auth open")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as ex:
        raise IdentityError(f"token store malformed: {where}: {ex}") from ex
    if not isinstance(data, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in data.items()):
        raise IdentityError(f'token store malformed: {where} (expected {{"sha256hex": "email"}})')
    return data


def resolve_email_for_token(token_store: dict[str, str], token: str | None) -> str:
    """Bearer token → email, fail-closed. NEVER enumerates known tokens or emails: the HTTP auth
    middleware catches `IdentityError` here and returns one fixed, generic 401 body — the
    detailed reason is for the server's own log only."""
    if not token:
        raise IdentityError("no bearer token presented")
    # No constant-time compare needed: a dict lookup on the SHA-256 hash leaks nothing timing-wise
    # to anyone who cannot already invert SHA-256.
    email = token_store.get(hash_token(token))
    if not email:
        raise IdentityError("token not recognized")
    return email
