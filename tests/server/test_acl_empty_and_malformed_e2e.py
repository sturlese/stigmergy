"""End-to-end: pages whose stored acl is an EMPTY list, through the real build pipeline and the
real service (search_brain + read_page), for both the ways an empty acl can arise:

- `EMPTY_ACL_PAGE`     — the author deliberately wrote `acl: []` ("nobody but the unrestricted").
- `MALFORMED_ACL_PAGE` — the author wrote a YAML shape `corpus._acl_labels` cannot recognize (a
  mapping); the index layer fails closed and normalizes it to `[]` too (see
  `tests/index/test_corpus.py::test_acl_malformed_shapes_fail_closed`).

Both must reach the SAME observable state end-to-end: visible to the unrestricted client
(`steward@example.com`), hidden from every scoped client (`ana@example.com`, `eng@example.com`) —
never silently open just because the author's YAML was wrong (ADR 012 §4)."""
import pytest

from tests.server.conftest import Fixture, make_service


@pytest.mark.parametrize("page_attr", ["EMPTY_ACL_PAGE", "MALFORMED_ACL_PAGE"])
class TestEmptyAclEndToEnd:
    def test_visible_to_unrestricted_identity(self, indexed, page_attr):
        conn, fx = indexed
        path = getattr(fx, page_attr)
        out = make_service(fx, conn, fx.STEWARD).search("widget compliance audit")
        assert any(h["path"] == path for h in out["hits"])
        page = make_service(fx, conn, fx.STEWARD).read_page(path)
        assert "body" in page and "UNTRUSTED-DATA" in page["body"]

    @pytest.mark.parametrize("scoped_identity", [Fixture.ANA, Fixture.ENG])
    def test_absent_for_a_scoped_identity(self, indexed, page_attr, scoped_identity):
        conn, fx = indexed
        path = getattr(fx, page_attr)
        out = make_service(fx, conn, scoped_identity).search("widget compliance audit")
        assert not any(h["path"] == path for h in out["hits"])
        page = make_service(fx, conn, scoped_identity).read_page(path)
        # existence-leak guarantee: identical shape to a genuinely nonexistent path
        ghost = make_service(fx, conn, scoped_identity).read_page("wiki/notes/does-not-exist.md")
        assert set(page) == set(ghost) == {"error"}


def test_deliberate_and_malformed_empty_acl_are_indistinguishable_to_a_scoped_client(indexed):
    """The two authoring mistakes/choices collapse to the identical stored acl (`{}`/empty) and
    therefore the identical enforcement outcome — proving corpus.py's fail-closed normalization
    (malformed -> []) never leaves a page MORE visible than a deliberately empty one would be."""
    conn, fx = indexed
    eng = make_service(fx, conn, fx.ENG)
    deliberate = eng.read_page(fx.EMPTY_ACL_PAGE)
    malformed = eng.read_page(fx.MALFORMED_ACL_PAGE)
    # same shape (existence-leak guarantee) and the same "unknown page" wording, just naming
    # each one's own path — the enforcement OUTCOME is identical, not literally the same string.
    assert set(deliberate) == set(malformed) == {"error"}
    assert deliberate["error"] == f"unknown page: {fx.EMPTY_ACL_PAGE}"
    assert malformed["error"] == f"unknown page: {fx.MALFORMED_ACL_PAGE}"
