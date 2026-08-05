"""Identity → audiences resolution, in two halves.

The FILE seam below maps a name to its audience scope. Beside it sits the PER-REQUEST half: a
bearer token → sha256 → a server-side token store → email, which then feeds the SAME
`resolve_audiences`. An email is just a string key, so keying `ops/identities.json` by email
rather than by name needs no change to the resolver at all. An OAuth flow, if one is ever added,
would replace only the token-store lookup below.

One resolver reads a versioned JSON file (`ops/identities.json` in the knowledge repo by
default, path injectable) mapping a name to its audience scope:

    {"steward": "*", "ana": ["finance"], "bob": ["sales", "leadership"]}

`"*"` means unrestricted (returns None — sees everything). A list becomes the client's audience
tuple. Fail-closed on EVERY path — missing identity, unknown identity, unreadable or malformed
file all raise `IdentityError`; the server never starts open.

**Security note: the identities file is CONFIGURATION, not authentication.** Anyone who can edit
it or set the `--identity` flag impersonates anyone, which is acceptable only for a single local
operator over stdio. Real verification — a bearer token no impersonator can fabricate — is the
per-request resolution below, and it is what the public HTTP transport uses.
"""
import hashlib
import json
import os

from stigmergy.server.errors import IdentityError

UNRESTRICTED = "*"
DEFAULT_RELATIVE = os.path.join("ops", "identities.json")


def default_path(repo_dir: str | None) -> str:
    """The conventional identities file inside a knowledge-repo checkout, or '' when no repo is
    given (the resolver then fails closed with an actionable message)."""
    return os.path.join(repo_dir, DEFAULT_RELATIVE) if repo_dir else ""


def resolve_audiences(identities_path: str, identity: str | None) -> tuple[str, ...] | None:
    """Resolve `identity` to its audience tuple, or None for unrestricted. Raises IdentityError
    on any failure — the caller must not proceed without a resolved scope."""
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
            '(create it, e.g. {"steward": "*", "ana": ["finance"]})')
    try:
        with open(identities_path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as ex:
        raise IdentityError(f"identities file unreadable or malformed: {identities_path}: {ex}") from ex
    if not isinstance(data, dict):
        raise IdentityError(
            f"identities file malformed: {identities_path} "
            '(expected an object mapping name -> "*" | [audiences])')
    if identity not in data:
        known = ", ".join(sorted(data)) or "(none)"
        raise IdentityError(f"unknown identity {identity!r} (known: {known})")

    value = data[identity]
    if value == UNRESTRICTED:
        return None
    if isinstance(value, str) and value.strip():          # a bare label = a one-audience scope
        return (value.strip(),)
    if isinstance(value, list):
        return tuple(s for s in (str(a).strip() for a in value) if s)
    raise IdentityError(
        f"identity {identity!r} has a malformed audience value in {identities_path} "
        f'(expected "*" or a list of audience labels, got {type(value).__name__})')


# ── per-request token resolution (HTTP transport) ──────────────────────────────────────────────
# The token store is a deploy secret shaped {"<sha256hex>": "<email>"}: plaintext bearer tokens
# are NEVER stored — only their hash. Issuance is `stigmergy-issue-token`
# (stigmergy/server/issue_token.py), which prints the plaintext once and the hash for this store.
TOKEN_STORE_ENV = "STIGMERGY_TOKEN_STORE"            # inline JSON (a Fly secret, typically)
TOKEN_STORE_FILE_ENV = "STIGMERGY_TOKEN_STORE_FILE"  # a path to the same JSON shape


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
    """Bearer token → email, fail-closed. Unlike `resolve_audiences`'s unknown-identity message
    (a local CLI/stderr affordance), this NEVER enumerates known tokens or emails: the caller (the
    HTTP auth middleware) catches `IdentityError` here and returns one fixed, generic 401 body —
    the detailed reason is for the server's own log only."""
    if not token:
        raise IdentityError("no bearer token presented")
    # No constant-time compare needed here: this is a dict-key lookup on the SHA-256 hash, not a
    # byte-by-byte comparison of the token itself — an attacker who can't already invert SHA-256
    # (a preimage attack) learns nothing timing-wise from a hash-table miss vs. hit.
    email = token_store.get(hash_token(token))
    if not email:
        raise IdentityError("token not recognized")
    return email
