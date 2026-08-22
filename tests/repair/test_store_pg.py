"""`repair.store`: the three writes, the two memories, and the one race that is still real.

The store decides nothing and authorizes nothing, and since ADR 044 it holds no state machine
either — a row is written once, when the attempt is already over, and never transitions. So what is
worth pinning is exactly what a caller cannot see for itself: that a row comes back the way it went
in, that the memory remembers the two outcomes it must and forgets the one it must, and that two
passes deriving the same repair meet the unique index rather than both pushing it.
"""
import pytest

from stigmergy.repair import schema, store

OPS = [{"op": "contradiction", "path": "wiki/notes/a.md", "link": "B", "note": "they disagree"},
       {"op": "contradiction", "path": "wiki/notes/b.md", "link": "A", "note": "they disagree"}]

DIFF = ("--- a/wiki/notes/a.md\n+++ b/wiki/notes/a.md\n@@ -3,6 +3,7 @@\n"
        "+related: [\"[[B]]\"]\n")


def _applied(conn, *, key="k", findings=(7, 9), subjects=(("wiki/notes/a.md",),)) -> int:
    return store.record_applied(
        conn, run_id=42, finding_ids=list(findings), target_paths=schema.target_paths(OPS),
        ops=OPS, rationale="because the pages disagree", content_key=key, commit="cafebabe",
        diff=DIFF, model_id="m", finding_subjects=[list(g) for g in subjects])


def _failed(conn, *, key="f", error="the gates refused this repair (zone/outside-lane)") -> int:
    return store.record_failed(
        conn, run_id=42, finding_ids=[11], target_paths=schema.target_paths(OPS), ops=OPS,
        rationale="because the pages disagree", content_key=key, error=error, model_id="m",
        finding_subjects=[["wiki/notes/a.md"]])


def _skipped(conn, *, reason="no kind can express this finding") -> int:
    return store.record_skipped(conn, run_id=42, finding_ids=[13], reason=reason,
                                finding_subjects=[["wiki/notes/c.md"]])


# ── the round trips ────────────────────────────────────────────────────────────────────────────
def test_an_applied_repair_round_trips_every_field_it_was_given(conn):
    """Including the DIFF, which is the one field this table exists to carry: nobody read the
    change before it was pushed, so a row that lost it would be a change nobody will ever read."""
    row = store.repair(conn, _applied(conn))

    assert row["run_id"] == 42
    assert row["finding_ids"] == [7, 9]
    assert row["kind"] == schema.KIND_EDITS
    assert row["target_paths"] == ["wiki/notes/a.md", "wiki/notes/b.md"]
    assert row["ops"] == OPS
    assert row["rationale"] == "because the pages disagree"
    assert row["content_key"] == "k"
    assert row["model_id"] == "m"
    assert row["status"] == schema.STATUS_APPLIED
    assert row["applied_commit"] == "cafebabe"
    assert row["diff"] == DIFF
    assert row["finding_subjects"] == [["wiki/notes/a.md"]]
    assert (row["reason"], row["error"]) == ("", "")


def test_a_failed_repair_carries_its_sentence_and_no_commit(conn):
    """`error` is the whole of what anyone will ever know about why this finding stopped being
    answered — the row is not retried, so there is no second chance to explain."""
    row = store.repair(conn, _failed(conn))

    assert row["status"] == schema.STATUS_FAILED
    assert "the gates refused" in row["error"]
    assert (row["applied_commit"], row["diff"]) == ("", "")


def test_a_skipped_row_carries_its_reason_and_nothing_that_happened(conn):
    """Nothing was derived, so there are no ops, no paths and no key — a `skipped` row is a record
    that the loop LOOKED at a finding, which is what stops "why was this never answered" being
    unanswerable."""
    row = store.repair(conn, _skipped(conn))

    assert row["status"] == schema.STATUS_SKIPPED
    assert row["reason"] == "no kind can express this finding"
    assert (row["ops"], row["target_paths"], row["content_key"]) == ([], [], "")
    assert row["finding_ids"] == [13]


def test_an_unknown_id_is_none_rather_than_an_exception(conn):
    assert store.repair(conn, 999_999) is None


# ── the memory: what is remembered, and the one thing that is not ─────────────────────────────
def test_an_applied_repair_is_remembered_so_it_is_never_derived_twice(conn):
    """The obvious half, and the one that makes a repair somebody REVERTED in git stay reverted:
    the loop derives from the corpus, so without this the next pass would re-derive the very edit a
    person had just undone."""
    _applied(conn, key="applied-key")
    assert store.known_content_keys(conn) == {"applied-key"}


def test_a_failed_repair_is_remembered_too(conn):
    """**OLD BEHAVIOUR: a failed apply was deliberately NOT remembered**, because a failure was a
    human's YES that hit a fault and they could approve again. Nobody approves anything now, so the
    same rule would mean deriving a repair the gates refuse, spending a model call on it and
    refusing it again — every night, forever. The trade is stated in `store.known_content_keys`:
    the loop forgets nothing, and the `failed` row is where an operator reads why a finding stopped
    being answered."""
    _failed(conn, key="failed-key")
    assert store.known_content_keys(conn) == {"failed-key"}


def test_a_failure_the_corpus_moved_under_is_recorded_but_not_remembered(conn):
    """The exception the rule above has to have, and the one a race makes necessary.

    `record_failed` with no content key is what `apply` writes for a `CorpusMovedError` — a refusal
    that is about the TREE rather than about the repair: a page deleted since the derivation, a
    plan whose bytes another repair from this very pass had already changed. Remembering it would
    retire a finding because two repairs collided, which is the defect two merges in one pass
    reproduced. The row still exists, because an operator should be able to see it happened.
    """
    store.record_failed(conn, run_id=42, finding_ids=[11],
                        target_paths=schema.target_paths(OPS), ops=OPS, rationale="r",
                        content_key="", error="this repair no longer applies to the knowledge repo")

    assert store.known_content_keys(conn) == set()
    assert store.answered_findings(conn) == (set(), set())
    assert store.counts_by_status(conn)[schema.STATUS_FAILED] == 1


def test_a_skipped_row_is_remembered_by_nothing(conn):
    """The benign twin of the two above: nothing was derived, so there is nothing to recognise, and
    the next pass must be free to try once the corpus has moved. A `skipped` row that suppressed a
    finding would be the loop deciding never to look again at exactly the findings it could not
    answer yet."""
    _skipped(conn)
    assert store.known_content_keys(conn) == set()
    assert store.answered_findings(conn) == (set(), set())


def test_two_passes_deriving_the_same_repair_meet_the_unique_index(conn):
    """The one race that survives the state machine's removal: two workers on the same database,
    or one pass whose model derived the same repair twice. The loser is TOLD — a second row would
    be a second commit for an edit already in the corpus."""
    _applied(conn, key="same")
    with pytest.raises(store.ContentKeyTaken):
        _applied(conn, key="same")


def test_the_unique_index_does_not_collide_on_keyless_rows(conn):
    """The partial index's whole reason: several findings a pass could not express are several
    `skipped` rows, and they all carry the empty key. A total index would let the first one block
    every other skip in the pass."""
    _skipped(conn, reason="one")
    _skipped(conn, reason="two")
    assert store.counts_by_status(conn)[schema.STATUS_SKIPPED] == 2


# ── the pre-model memory: a finding this ledger already answered ───────────────────────────────
def test_answered_findings_recognises_a_finding_by_id_and_by_the_pages_it_named(conn):
    """TWO exact rules, deliberately not one fuzzy one. The id catches a second pass over the same
    gardener run; the page set catches the same problem rediscovered under a new id in a later run,
    which is the case that made this necessary. Both halves come off rows with a key — the answer
    is "this was answered", and a skip answers nothing."""
    _applied(conn, key="a", findings=(7,), subjects=(("wiki/notes/x.md", "wiki/notes/y.md"),))

    ids, page_sets = store.answered_findings(conn)

    assert ids == {7}
    assert schema.page_set_key(["wiki/notes/y.md", "wiki/notes/x.md"]) in page_sets
    # what the ANSWER edited, as well as what the finding named: the two are routinely different,
    # and matching only one is why a declined shape came back every night before #69.
    assert schema.page_set_key(["wiki/notes/a.md", "wiki/notes/b.md"]) in page_sets
    assert schema.page_set_key(["wiki/notes/x.md"]) not in page_sets


def test_a_page_set_key_is_order_and_duplicate_independent(conn):
    """Both ends of the memory spell the key through the same function precisely so a repair
    recorded in one order is recognised in another — a key built two ways is two keys, and the
    failure is silent."""
    assert (schema.page_set_key(["b.md", "a.md", "a.md"])
            == schema.page_set_key(["a.md", "b.md"]))
    assert schema.page_set_key([]) == schema.page_set_key(["", None])


# ── the reads a surface draws from ─────────────────────────────────────────────────────────────
def test_counts_by_status_covers_every_status_over_the_whole_table(conn):
    """Every declared status present with zero included, and counted over ALL rows — not a page: a
    surface drawing a part-to-whole from a bounded page would understate history the moment the
    page fills, which is the defect this aggregate exists to replace."""
    assert store.counts_by_status(conn) == {s: 0 for s in schema.STATUSES}

    _applied(conn, key="a")
    _applied(conn, key="b")
    _failed(conn, key="c")
    _skipped(conn)

    assert store.counts_by_status(conn) == {schema.STATUS_APPLIED: 2, schema.STATUS_FAILED: 1,
                                            schema.STATUS_SKIPPED: 1}


def test_recent_is_newest_first_and_bounded(conn):
    """Newest first and never oldest: this table only grows, and what a reader wants from it is
    what the last pass did."""
    first = _applied(conn, key="1")
    second = _applied(conn, key="2")
    third = _applied(conn, key="3")

    assert [r["id"] for r in store.recent(conn)] == [third, second, first]
    assert [r["id"] for r in store.recent(conn, limit=2)] == [third, second]


def test_recent_can_be_asked_for_one_outcome(conn):
    """The console lists everything and the digest counts only what landed; one query, one filter,
    rather than a second SELECT that could come to disagree about the ordering."""
    applied = _applied(conn, key="a")
    _failed(conn, key="b")

    assert [r["id"] for r in store.recent(conn, status=schema.STATUS_APPLIED)] == [applied]
    assert [r["status"] for r in store.recent(conn, status=schema.STATUS_FAILED)] == [
        schema.STATUS_FAILED]
