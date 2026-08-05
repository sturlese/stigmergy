"""The split-chain demotion lives at BUILD time — `corpus.load_pages` propagates a primary's
`superseded_by` onto every `#p<n>` sibling — never at rank time. `rank.py` carries no
cross-candidate reconstruction (`superseded_bases`) any more, and this is the Postgres half of
the proof: against a real rebuilt index, a continuation part is demoted even when its PRIMARY is
outside the candidate set `rank.rank()` is handed — the exact case the old reconstruction could
not see, since it only ever looked at candidates already in front of it.

Its own small corpus, isolated from `tests/index/test_pg_integration.py`'s module-scoped fixture
— the same posture `test_incremental_pg.py` takes.
"""
from datetime import date

import pytest

from stigmergy.index import build, rank, search
from stigmergy.index.backends.embedder import build_embedder
from tests import testdb

TODAY = date(2026, 7, 19)

PRIMARY = "sources/entities/acme/report-primary.md"
PART2 = "sources/entities/acme/report-part2.md"
PART3 = "sources/entities/acme/report-part3.md"
CURRENT = "sources/entities/acme/report-current.md"


def _connect_or_skip():
    return testdb.connect_or_skip("index")


@pytest.fixture(scope="module")
def conn(tmp_path_factory):
    root = tmp_path_factory.mktemp("r1-split-chain")
    idir = root / "sources" / "entities" / "acme"
    idir.mkdir(parents=True)
    (idir / "report-primary.md").write_text(
        "---\nid: drive:R1\nsuperseded_by: drive:R2\ntitle: Acme Report Primary\n"
        "verification: verified\n---\nacme quarterly figures part one narrative")
    (idir / "report-part2.md").write_text(
        "---\nid: drive:R1#p2\ntitle: Acme Report Part 2\nverification: verified\n---\n"
        "acme quarterly figures part two narrative continued")
    (idir / "report-part3.md").write_text(
        "---\nid: drive:R1#p3\ntitle: Acme Report Part 3\nverification: verified\n---\n"
        "acme quarterly figures part three narrative continued")
    (idir / "report-current.md").write_text(
        "---\nid: drive:R2\nsupersedes: drive:R1\ntitle: Acme Report Current\n"
        "verification: verified\n---\nacme quarterly figures corrected current narrative")
    conn = _connect_or_skip()
    stats = build.rebuild(conn, str(root), build_embedder("fake"))
    assert stats["pages"] == 4
    yield conn
    conn.close()


def test_build_time_propagation_reached_postgres(conn):
    """Sanity precondition the rest of this file relies on: `corpus.load_pages`'s propagation
    survived the round trip through `store.upsert_pages`/`insert_pages` into real rows."""
    rows = search.fetch_pages(conn, [PRIMARY, PART2, PART3, CURRENT])
    assert rows[PRIMARY]["superseded_by"] == "drive:R2"
    assert rows[PART2]["superseded_by"] == "drive:R2"
    assert rows[PART3]["superseded_by"] == "drive:R2"
    assert rows[CURRENT]["superseded_by"] == ""


def test_continuation_part_is_demoted_when_its_primary_is_outside_the_candidate_set(conn):
    """The case the old mechanism could not reach: `rank.rank()` is handed a candidates dict that
    does NOT include the primary's own path at all — as if the primary had ranked beyond the
    top-40 pool of a real search and never reached `fetch_pages`. Under the OLD rank-time
    reconstruction this could not demote PART2/PART3 (their own `superseded_by` was empty, and
    `chain_base` had nothing else in the candidate set to cross-reference against). With
    build-time propagation, the row's own column already carries the truth, so this needs no
    reconstruction at all."""
    candidates = search.fetch_pages(conn, [PART2, PART3, CURRENT])   # PRIMARY excluded on purpose
    assert PRIMARY not in candidates
    order = [PART2, PART3, CURRENT]
    hits = rank.rank(candidates, order, order, "acme quarterly figures narrative",
                     today=TODAY)
    ranked = [h["path"] for h in hits]
    assert ranked[0] == CURRENT
    # the chain collapse: the stale document holds ONE slot — its best-scoring part — and that
    # representative is demoted via its own propagated column, primary nowhere in sight.
    stale = [h for h in hits if h["path"] in (PART2, PART3)]
    assert len(stale) == 1
    assert "superseded" in stale[0]["factors"]
    assert next(h for h in hits if h["path"] == CURRENT)["factors"] == []


def test_current_only_drops_the_continuation_parts_too_with_the_primary_absent(conn):
    candidates = search.fetch_pages(conn, [PART2, PART3, CURRENT])
    order = [PART2, PART3, CURRENT]
    hits = rank.rank(candidates, order, order, "acme quarterly figures narrative",
                     today=TODAY, include_superseded=False)
    assert [h["path"] for h in hits] == [CURRENT]
