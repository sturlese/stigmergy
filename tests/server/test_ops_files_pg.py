"""`server.ops_files` — the ONE preference order for which copy of an `ops/` control file a
running process answers from: the index's snapshot wherever the database carries one, the
process's own file where it does not (issues #74 and #79).

Real Postgres, because the order is only real over a real snapshot row — and the trap this file
exists to pin (`""` versus `None` out of `store.read_ops_file`) cannot be seen through a double
that never returns the empty string.
"""
import json

import pytest

from stigmergy.index import store
from stigmergy.server import ops_files
from stigmergy.server.errors import IdentityError
from tests import testdb


def identity(name, groups, default):
    return {"display_name": name, "groups": groups, "default_audience": default}


@pytest.fixture()
def conn():
    c = testdb.connect_or_skip("server")
    store.clear_ops_file(c, store.IDENTITIES_RELPATH)
    yield c
    store.clear_ops_file(c, store.IDENTITIES_RELPATH)
    c.close()


@pytest.fixture()
def identities_file(tmp_path):
    path = tmp_path / "identities.json"
    path.write_text(json.dumps({
        "baked@example.com": identity("Baked", ["finance"], ["finance"]),
    }), encoding="utf-8")
    return str(path)


def test_with_no_snapshot_the_file_road_answers_unchanged(conn, identities_file):
    assert ops_files.resolve_identity_audiences(
        conn, identities_file, "baked@example.com") == ("finance",)


def test_a_snapshot_wins_over_the_file_and_a_revocation_lands_without_a_deploy(
        conn, identities_file):
    """Issue #79's finding, as behaviour: the file (the deploy-baked copy) still says
    `baked@example.com` may read finance; the snapshot (the pushed edit) no longer does — and the
    snapshot answers, so the revocation is live seconds after the push."""
    store.write_ops_file(conn, store.IDENTITIES_RELPATH,
                         json.dumps({
                             "other@example.com": identity(
                                 "Other", ["brain-admins"], None
                             ),
                         }), "pushed-sha")

    with pytest.raises(IdentityError, match="not configured"):
        ops_files.resolve_identity_audiences(conn, identities_file, "baked@example.com")
    assert ops_files.resolve_identity_audiences(conn, identities_file,
                                                "other@example.com") is None


def test_an_EMPTY_snapshot_resolves_nobody_and_never_falls_through_to_the_file(
        conn, identities_file):
    """The `is not None` order, proven where it bites: `read_ops_file` answers `""` for a row
    holding empty text, and a truthiness fallback would hand resolution back to the baked file —
    the stale roster the snapshot exists to replace. Empty is MALFORMED and fails closed."""
    store.write_ops_file(conn, store.IDENTITIES_RELPATH, "", "pushed-sha")

    with pytest.raises(IdentityError, match="malformed"):
        ops_files.resolve_identity_audiences(conn, identities_file, "baked@example.com")


def test_a_caller_with_no_database_at_all_rides_the_file_road(identities_file):
    assert ops_files.resolve_identity_audiences(
        None, identities_file, "baked@example.com") == ("finance",)
