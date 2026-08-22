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
import re

from stigmergy.server.errors import IdentityError

# Membership of this group IS the unrestricted scope: `resolve_audiences` returns `None` for a
# principal that holds it, which is the value `acl.visible()` has always read as "sees everything".
# A NAME rather than a sigil, because the identity provider that replaces this file has groups and
# has no sigils.
UNRESTRICTED_GROUP = "brain-admins"

# Refused as a group name in either file, and as an `audience` value at the door — COMPARED
# CASEFOLDED, because the reservation exists to stop a human writing a label they believe means
# "everyone", and `All` is that same intention with a different shift key. See the module
# docstring: open is the absence of a label.
RESERVED_GROUP_NAMES = frozenset({"all"})

# The retired unrestricted sigil, refused as a group NAME too. `{"m": "*"}` is refused by the
# value rule below with the line to write instead; `{"m": ["*"]}` is exactly what an operator
# reaches for while following that advice halfway, and it resolves to a group nobody can hold —
# so the admin silently loses unrestricted access and, at the door, files pages nobody can read.
RETIRED_UNRESTRICTED_SIGIL = "*"

# A group name's own grammar. Deliberately narrow: these names are stamped into page frontmatter
# (`acl: ["finance"]`) and into a Postgres `text[]`, and since ADR 045 D2 one of them can arrive
# from a MODEL through `brain_submit(audience=…)`. A newline or a control character crossing into
# YAML is a page-contract injection, and there is no legitimate group name that needs one.
_GROUP_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
MAX_GROUP_NAME_CHARS = 64
# One principal in a hundred groups is a mistake, not a policy; the cap bounds what a caller can
# make the server stamp on a page and store on a row.
MAX_GROUPS = 32

DEFAULT_RELATIVE = os.path.join("ops", "identities.json")

# The remedy a refusal ends with, per caller. A shared validator that hands a FILE remedy to an
# MCP caller tells them to edit something they cannot see, so each caller says what its own reader
# can actually do.
FILE_REMEDY = ("a principal with no groups already reads every open page, so an empty list is the "
               "right spelling for a plain reader")
DOOR_REMEDY = "omit `audience` to file the capture open"


def _suggest_list(value: str) -> str:
    """The `write [...] instead` half of a retired-spelling refusal — but only when the wrapped
    value would actually be accepted.

    A refusal that names a replacement is an executable promise, and the naive version broke it
    three ways: `"finance,sales"` was answered with `write ["finance,sales"]`, which the comma
    rule then refuses; `"all"` with `write ["all"]`, which the reservation refuses; and any value
    carrying a quote produced text that is not JSON at all. So the suggestion is CHECKED before it
    is offered, with `json.dumps` doing the escaping.
    """
    try:
        check_group_names([value], origin="<suggestion>", subject="a group name")
    except IdentityError:
        return ""
    return f' — a bare label was retired: write {json.dumps([value])} instead (ADR 045)'


def check_group_names(names, *, origin: str, subject: str,
                      remedy: str = FILE_REMEDY) -> tuple[str, ...]:
    """Validate a list of group names and return it normalized, or raise `IdentityError`.

    The one place the vocabulary's rules live, so the roster, the channel map and the door's own
    `audience` argument cannot come to disagree about what a group may be called — a name that is
    spellable at the door and refused in the file would be a capture that files at a label the
    server can never resolve.

    A comma is refused for its own reason and the message says so: a label list is CSV-serialized
    on at least one road (`acl.visible()` still normalizes a bare CSV string), so one comma inside
    a name would silently become two groups at enforcement time.
    """
    if not isinstance(names, list):
        detail = ""
        if names == RETIRED_UNRESTRICTED_SIGIL:
            detail = ' — the "*" spelling was retired: write ["brain-admins"] instead (ADR 045)'
        elif isinstance(names, str) and names.strip():
            detail = _suggest_list(names.strip())
        raise IdentityError(
            f"{subject} has a malformed group list in {origin}: expected a list of group names, "
            f"got {type(names).__name__}{detail}")
    if len(names) > MAX_GROUPS:
        raise IdentityError(
            f"{subject} names {len(names)} groups in {origin}, over the ceiling of {MAX_GROUPS}")
    out = []
    for raw in names:
        if not isinstance(raw, str):
            raise IdentityError(
                f"{subject} names a group that is not a string in {origin}: "
                f"got {type(raw).__name__}")
        name = raw.strip()
        if name == RETIRED_UNRESTRICTED_SIGIL:
            raise IdentityError(
                f"{subject} names a group called {name!r} in {origin} — that is the retired "
                f"unrestricted sigil, not a group. Unrestricted is membership of "
                f'{UNRESTRICTED_GROUP!r}: write ["{UNRESTRICTED_GROUP}"] (ADR 045)')
        if "," in name:
            raise IdentityError(
                f"{subject} names an invalid group {raw!r} in {origin}: a group name must not "
                f"contain ',' — a label list is CSV-serialized on at least one road, so the comma "
                f"would silently become two groups where access is decided")
        if len(name) > MAX_GROUP_NAME_CHARS or not _GROUP_NAME_RE.match(name):
            raise IdentityError(
                f"{subject} names an invalid group {raw!r} in {origin}: a group name is 1-"
                f"{MAX_GROUP_NAME_CHARS} characters of letters, digits, '.', '_' or '-', starting "
                f"with a letter or a digit. These names are written into page frontmatter and "
                f"stored as an access label, so the vocabulary is narrow on purpose")
        folded = name.casefold()
        if folded in RESERVED_GROUP_NAMES:
            raise IdentityError(
                f"{subject} names the reserved group {name!r} in {origin} — open is the ABSENCE "
                f"of a label, so a page labelled {name!r} would be restricted to a group by that "
                f"name rather than open to everyone. Remove it: {remedy}")
        if folded == UNRESTRICTED_GROUP.casefold() and name != UNRESTRICTED_GROUP:
            # Refused rather than folded: accepting `Brain-Admins` as unrestricted would WIDEN on
            # a typo, and this is the one label whose typo grants the whole corpus.
            raise IdentityError(
                f"{subject} names {name!r} in {origin}, which differs only in case from the "
                f"unrestricted group {UNRESTRICTED_GROUP!r}. Group names are compared exactly, so "
                f"this grants nothing — write {UNRESTRICTED_GROUP!r} if that is what you meant")
        if name not in out:
            out.append(name)
    return tuple(out)


def _pairs_without_duplicates(pairs, *, origin: str, subject: str) -> dict:
    """`json.loads`' `object_pairs_hook`: a repeated principal is a REFUSAL, not a last-win.

    The headline property of this parser is that the WHOLE file is validated, and a repeated key
    is exactly the file-level defect that claim implies. `json.loads` keeps the last occurrence
    silently, so a diff appending `"marc@x.com": ["brain-admins"]` far from an existing narrow
    line reads, in review, as one added line beside an unchanged one — and the push webhook makes
    it live within seconds.
    """
    seen = {}
    for key, value in pairs:
        if key in seen:
            raise IdentityError(
                f"{subject} {key!r} appears more than once in {origin} — the last one would "
                f"silently win, which is not something an access-control file gets to do. Say it "
                f"once")
        folded = str(key).strip().casefold()
        collision = next((k for k in seen if str(k).strip().casefold() == folded), None)
        if collision is not None:
            raise IdentityError(
                f"{subject} {key!r} and {collision!r} in {origin} differ only in case or "
                f"whitespace, and lookups are exact — two entries that look identical in review "
                f"and grant differently. Say it once")
        seen[key] = value
    return seen


def group_map_from_text(text: str, *, origin: str, subject: str) -> dict[str, tuple[str, ...]]:
    """`{principal: (group, …)}` from one control file's TEXT, or raise `IdentityError`.

    The WHOLE file is validated, not only the entry being looked up: a malformed neighbour in an
    access-scoping file is a file the server cannot make sense of, and "the entry I wanted happened
    to parse" is not a property to grant access on. `subject` names what a key is ("identity",
    "channel") so a refusal reads in the operator's own vocabulary.

    An EMPTY text is malformed JSON and raises — on this road "nobody is listed" is spelled `{}`,
    a committed statement, never bytes that failed to arrive.

    **A key beginning with `_` is a comment and is dropped**, so an operator can say in the file
    itself which channel `C0BL6QH7AQN` is. A `_`-prefixed key whose value is a LIST is REFUSED
    instead: a comment's value is prose, an entry's value is a group list, and the ambiguous case
    is somebody whose entry silently does nothing. (`_marc@example.com` is a valid email address,
    and `stigmergy-issue-token` will issue for one, so "no principal begins with `_`" is a rule
    this file states rather than a fact about the world.)

    **What this function does NOT do is collapse anything to unrestricted.** An identity's groups
    may resolve to `None` — `audiences_from_text`, one layer up, is where `UNRESTRICTED_GROUP`
    means "sees everything". A CHANNEL's never do: `slack.channels` reads this map directly and
    always returns a set, because Slack capture and Slack answering are public-channel only. If
    that asymmetry is ever unified here, a channel listed as `brain-admins` starts meaning "this
    channel sees the whole corpus", and the digest broadcasts it.
    """
    try:
        data = json.loads(text, object_pairs_hook=lambda pairs: _pairs_without_duplicates(
            pairs, origin=origin, subject=subject))
    except json.JSONDecodeError as ex:
        raise IdentityError(f"{subject} map malformed: {origin}: {ex}") from ex
    if not isinstance(data, dict):
        raise IdentityError(
            f"{subject} map malformed: {origin} "
            f"(expected an object mapping {subject} -> [group, ...])")
    out = {}
    for key, value in data.items():
        if key.startswith("_"):
            if isinstance(value, list):
                raise IdentityError(
                    f"{origin} carries a key {key!r} whose value is a group list. A `_` prefix "
                    f"marks a COMMENT and is dropped, so this entry would grant nothing while "
                    f"looking active — rename it without the underscore, or make its value prose")
            continue
        out[key] = check_group_names(value, origin=origin, subject=f"{subject} {key!r}")
    return out


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


def known_groups_from_text(text: str, *, origin: str) -> set[str]:
    """Every group the roster grants to anybody — the vocabulary that EXISTS.

    The door needs it because shape validation is not enough: `audience=["finanace"]` is a legal
    group name, and for an UNRESTRICTED caller `visible()` returns True unconditionally, so the
    typo files a page nobody can ever read — permanently, since a filed page's audience cannot be
    changed. A scoped caller is protected from this by accident (they must share a label with what
    they name); the unrestricted caller, whose typo is the silent one, is not.
    """
    return {group for groups in group_map_from_text(
        text, origin=origin, subject="identity").values() for group in groups}


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
