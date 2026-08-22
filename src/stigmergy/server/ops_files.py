"""Which copy of an `ops/` control file a running process answers from — the ONE preference
order, stated once: the index's snapshot wherever the database has one, the process's own file
where it does not.

That order is issue #74's for the entity registry and issue #79's for the two access-scoping
files: the deployed `app` and `slack` groups hold no checkout, so their files are copies baked
into the image at deploy time — an identity revoked after the rollout kept resolving, and a
channel scoped after it stayed unscoped, until the next deploy. The snapshot is refreshed by the
push webhook (fetched at the BRANCH REF, so no replayed delivery can install a historical copy)
and reconciled per file by the nightly rebuild; the file road stays for a database with no
snapshot — a local server against its own checkout, or an index built before the table existed.

`BrainService._registry_source` holds the registry's own copy of this order and keeps it: its
memo lifetime is welded to the `_call` reset and this module has no business inheriting that.
The parse stays with each file's own reader (`server.identity`, `slack.channels`) — this module
chooses BYTES and never interprets them.

This is also the road `stigmergy.slack` reaches the snapshots BY: that package may import
`stigmergy.server` and not `stigmergy.index` (`tests/test_architecture.py`), so the store's
reader and relpath spellings cross here, once, under the server's own name.
"""
from stigmergy.index import store
from stigmergy.server import identity as identity_module

IDENTITIES_RELPATH = store.IDENTITIES_RELPATH
SLACK_CHANNELS_RELPATH = store.SLACK_CHANNELS_RELPATH

# What an operator-facing IdentityError names as the source when the snapshot road answered —
# never a path, because there is none: the copy is a database row.
IDENTITIES_SNAPSHOT_ORIGIN = "the index's cached ops/identities.json"
CHANNELS_SNAPSHOT_ORIGIN = "the index's cached ops/slack-channels.json"


def text_or_none(conn, relpath: str) -> str | None:
    """One cached ops file's TEXT, or `None` when this database has no snapshot of it (including
    `conn=None`, a caller with no database at all — the file road answers there)."""
    if conn is None:
        return None
    return store.read_ops_file(conn, relpath)


def known_groups(conn, identities_path: str) -> set[str]:
    """The groups the roster grants to anybody, through the SAME preference order as the resolver
    beside it — so the door cannot check a requested group against one copy of the roster while
    the reader resolves the caller's own from another."""
    text = text_or_none(conn, IDENTITIES_RELPATH)
    if text is not None:
        return identity_module.known_groups_from_text(text, origin=IDENTITIES_SNAPSHOT_ORIGIN)
    with open(identities_path, encoding="utf-8") as f:
        return identity_module.known_groups_from_text(f.read(), origin=identities_path)


def resolve_identity_audiences(conn, identities_path: str, identity: str | None):
    """`identity` -> audience tuple (None = unrestricted), preferring the snapshot — the one
    resolution both deployed transports (HTTP per request, Slack per event) run, so they cannot
    come to prefer different copies. Fail-closed exactly as `server.identity` is on every road:
    an EMPTY snapshot is malformed JSON and resolves nobody, a missing one falls back to the
    file, and the file road's own refusals are untouched."""
    text = text_or_none(conn, IDENTITIES_RELPATH)
    if text is not None:
        return identity_module.audiences_from_text(text, identity,
                                                   origin=IDENTITIES_SNAPSHOT_ORIGIN)
    return identity_module.resolve_audiences(identities_path, identity)
