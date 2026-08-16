"""`review_decisions`' provenance column — which DOOR recorded a verdict (issue #41 part 2).

Real Postgres: `extra` is a JSONB column and `latest_decisions` reads the source back out of it
with `->>`, so a stubbed connection would prove nothing about either half.

The ledger is APPEND-ONLY and cannot be migrated afterwards, which is why the source is validated
against a closed tuple at the writer rather than trusted from the caller: a row spelling the admin
console `"console"` is a row no reader can ever join back to the door it came from.
"""
import pytest

from stigmergy.capture import decisions
from stigmergy.review_kinds import KIND_ENTITY_PROPOSAL, KIND_PARKED_CAPTURE
from tests.capture.conftest import connect_or_skip


@pytest.fixture(scope="module")
def ledger_conn():
    conn = connect_or_skip()
    decisions.ensure_decisions_schema(conn)
    yield conn
    conn.close()


@pytest.fixture()
def clean_ledger(ledger_conn):
    with ledger_conn.cursor() as cur:
        cur.execute("DELETE FROM review_decisions")
    return ledger_conn


def _extra_of(conn, item_id: str) -> dict | None:
    with conn.cursor() as cur:
        cur.execute("SELECT extra FROM review_decisions WHERE item_id = %s", (item_id,))
        return cur.fetchone()[0]


# ── the closed set ─────────────────────────────────────────────────────────────────────────────
def test_record_decision_refuses_a_source_outside_the_closed_set(clean_ledger):
    """OLD BEHAVIOUR: `record_decision` had no `source` parameter at all — a ledger row could not
    say which door decided it, and every door that wanted to say so invented its own spelling in
    `extra` (`entities/cli.py` wrote `{"door": "cli"}`; nobody else wrote anything).

    A wrong source is a BUG in a door, not data a reader should have to interpret: this table is
    append-only, so a misspelled row is permanent. `ValueError`, not a refusal type — nothing a
    caller typed reaches here, only a literal a door's own code passed.
    """
    with pytest.raises(ValueError, match="rest-api"):
        decisions.record_decision(clean_ledger, item_kind=KIND_ENTITY_PROPOSAL, item_id="1",
                                  verdict=decisions.APPROVE, actor="steward@example.com",
                                  source="rest-api")

    with clean_ledger.cursor() as cur:
        cur.execute("SELECT count(*) FROM review_decisions")
        assert cur.fetchone()[0] == 0, "a refused source must write no row at all"


@pytest.mark.parametrize("source", decisions.DECISION_SOURCES)
def test_every_declared_source_is_accepted_and_lands_in_extra(clean_ledger, source):
    """The benign twin of the refusal above, over the WHOLE closed set rather than one member —
    a validator measured only by what it rejects has an unmeasured specificity, and this one can
    bounce a real steward's decision on any of the four doors."""
    decisions.record_decision(clean_ledger, item_kind=KIND_PARKED_CAPTURE, item_id="7",
                              verdict=decisions.REJECT, actor="steward@example.com",
                              source=source, notes="not worth filing")

    assert _extra_of(clean_ledger, "7") == {"source": source}


def test_the_source_rides_beside_the_per_kind_detail_extra_already_carried(clean_ledger):
    """`extra` stays the per-kind seam it was: a mint's `entity_id`/`commit` and the CLI's own
    `door` marker survive the merge untouched, with `source` added beside them."""
    decisions.record_decision(clean_ledger, item_kind=KIND_ENTITY_PROPOSAL, item_id="42",
                              verdict=decisions.APPROVE, actor="steward@example.com",
                              source=decisions.SOURCE_CLI,
                              extra={"entity_id": "globex-robotics", "commit": "a" * 40,
                                     "door": "cli"})

    assert _extra_of(clean_ledger, "42") == {
        "source": "cli", "entity_id": "globex-robotics", "commit": "a" * 40, "door": "cli"}


# ── the read side ──────────────────────────────────────────────────────────────────────────────
def test_latest_decisions_returns_the_recorded_source(clean_ledger):
    decisions.record_decision(clean_ledger, item_kind=KIND_ENTITY_PROPOSAL, item_id="42",
                              verdict=decisions.APPROVE, actor="steward@example.com",
                              source=decisions.SOURCE_SLACK)

    latest = decisions.latest_decisions(clean_ledger)[(KIND_ENTITY_PROPOSAL, "42")]

    assert latest["source"] == "slack"
    assert latest["verdict"] == decisions.APPROVE
    assert latest["actor"] == "steward@example.com"


def test_a_row_written_before_this_column_existed_reads_back_as_an_empty_source(clean_ledger):
    """Coexistence: the ledger cannot be migrated, so rows written by every earlier build carry
    `extra IS NULL` forever. They must still READ — as `""`, never as a KeyError in a renderer or
    a `None` that formats as the word "None" in a steward's refusal."""
    with clean_ledger.cursor() as cur:
        cur.execute(
            "INSERT INTO review_decisions (item_kind, item_id, verdict, actor, notes, extra) "
            "VALUES (%s, %s, %s, %s, '', NULL)",
            (KIND_ENTITY_PROPOSAL, "9", decisions.APPROVE, "steward@example.com"))

    latest = decisions.latest_decisions(clean_ledger)[(KIND_ENTITY_PROPOSAL, "9")]

    assert latest["source"] == ""
