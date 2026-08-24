"""Select repository control snapshots, falling back to configured local files."""
from stigmergy.index import store
from stigmergy.server import identity as identity_module

IDENTITIES_RELPATH = store.IDENTITIES_RELPATH
SLACK_CHANNELS_RELPATH = store.SLACK_CHANNELS_RELPATH

IDENTITIES_SNAPSHOT_ORIGIN = "the index's cached ops/identities.json"
CHANNELS_SNAPSHOT_ORIGIN = "the index's cached ops/slack-channels.json"


def text_or_none(conn, relpath: str) -> str | None:
    """Return a cached file, or ``None`` when the local file should be used."""
    if conn is None:
        return None
    return store.read_ops_file(conn, relpath)


def known_groups(conn, identities_path: str) -> set[str]:
    """Return groups from the same roster source used for identity resolution."""
    text = text_or_none(conn, IDENTITIES_RELPATH)
    if text is not None:
        return identity_module.known_groups_from_text(text, origin=IDENTITIES_SNAPSHOT_ORIGIN)
    with open(identities_path, encoding="utf-8") as f:
        return identity_module.known_groups_from_text(f.read(), origin=identities_path)


def resolve_identity_audiences(conn, identities_path: str, identity: str | None):
    """Resolve audiences from the snapshot-first roster."""
    text = text_or_none(conn, IDENTITIES_RELPATH)
    if text is not None:
        return identity_module.audiences_from_text(text, identity,
                                                   origin=IDENTITIES_SNAPSHOT_ORIGIN)
    return identity_module.resolve_audiences(identities_path, identity)


def resolve_identity_principal(conn, identities_path: str, subject: str | None):
    text = text_or_none(conn, IDENTITIES_RELPATH)
    if text is not None:
        return identity_module.principal_from_text(
            text,
            subject,
            origin=IDENTITIES_SNAPSHOT_ORIGIN,
        )
    return identity_module.resolve_principal(identities_path, subject)
