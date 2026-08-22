"""The door decides a capture's audience, and its authorization is `acl.visible()` itself.

ADR 045 D2. `audience` is the one access decision a CALLER makes: the groups this material is
for, omitted to file open. The door resolves it, checks it against the caller's own groups, and
stores the answer on `capture_queue.acl` — a server-owned column no client input reaches.

**The rule is one sentence: you may file only what you could read afterwards.** It is not a new
predicate; it is the one read predicate asked of the WRITER, which is what keeps the model small.
A page nobody who wrote it can see is a page nobody can fix.

Real Postgres, the real queue primitive, the in-memory evidence double.
"""
import pytest

from stigmergy.capture.errors import CaptureError
from stigmergy.capture.evidence import MemoryEvidenceStore
from tests.server.conftest import Fixture, make_service

STEWARD, ANA, ENG = Fixture.STEWARD, Fixture.ANA, Fixture.ENG


def _service(indexed, identity):
    conn, fx = indexed
    return make_service(fx, conn, identity, evidence=MemoryEvidenceStore())


def _row_acl(indexed, submission_id: int):
    conn, _fx = indexed
    with conn.cursor() as cur:
        cur.execute("SELECT acl FROM capture_queue WHERE id = %s", (submission_id,))
        return cur.fetchone()[0]


# ── the caller's own decision, checked against the caller's own groups ────────────────────────
def test_a_scoped_caller_may_file_at_a_group_they_hold(indexed):
    ack = _service(indexed, ANA).submit("raw", "Payroll rates for Q3.",
                                              audience=["finance"])
    assert ack["acl"] == ["finance"]
    assert _row_acl(indexed, ack["id"]) == ["finance"]


def test_a_scoped_caller_may_not_file_at_a_group_they_do_NOT_hold(indexed):
    """The whole authorization. `ana` holds `finance`; filing at `eng` would produce a page she
    could not read back, which is the shape that makes a mislabelled page unfixable."""
    with pytest.raises(CaptureError, match="could not read afterwards"):
        _service(indexed, ANA).submit("raw", "Something for engineering.",
                                            audience=["eng"])


def test_the_refusal_names_the_callers_own_groups_and_not_the_ones_asked_for(indexed):
    """It echoes what the caller already holds, never the requested set: which groups EXIST is not
    this message's to confirm."""
    with pytest.raises(CaptureError) as caught:
        _service(indexed, ANA).submit("raw", "Probing.", audience=["leadership"])
    assert "finance" in str(caught.value)
    assert "leadership" not in str(caught.value)


def test_a_refused_submission_queues_nothing(indexed):
    """A refusal is a refusal: the row must not exist, or a scoped caller could fill the queue
    with captures they cannot read."""
    conn, _fx = indexed
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM capture_queue")
        before = cur.fetchone()[0]
    with pytest.raises(CaptureError):
        _service(indexed, ANA).submit("raw", "Nothing should land.", audience=["eng"])
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM capture_queue")
        assert cur.fetchone()[0] == before


def test_an_unrestricted_caller_may_file_at_any_group(indexed):
    """Passes by construction — `visible()` shows an unrestricted client every page — so the door
    needs no special case for the identity that already reads everything."""
    ack = _service(indexed, STEWARD).submit("raw", "Board pack summary.",
                                                  audience=["leadership"])
    assert ack["acl"] == ["leadership"]


def test_a_caller_may_file_at_several_groups_when_they_hold_one_of_them(indexed):
    """`visible()` is "shares at least one label", and the door asks exactly that of the writer:
    ana can read a `[finance, eng]` page, so she may file one. Widening it to `eng` readers is a
    human choice, attributed to her on every page it writes."""
    ack = _service(indexed, ANA).submit("raw", "Shared finance and engineering note.",
                                              audience=["finance", "eng"])
    assert ack["acl"] == ["finance", "eng"]


# ── the benign twin: the ordinary capture nobody labels ───────────────────────────────────────
def test_omitting_audience_files_open_and_stores_NULL(indexed):
    """The specificity half, and the case that runs on almost every capture anybody makes.

    NULL rather than `{}`: "this caller named no audience" is open, and `{}` means nobody. The
    two spellings meeting in one column is the defect ADR 045 D9 ends."""
    ack = _service(indexed, ANA).submit("raw", "An ordinary note about Initech.")
    assert ack["acl"] is None
    assert _row_acl(indexed, ack["id"]) is None


def test_an_empty_audience_list_is_REFUSED_rather_than_read_as_open(indexed):
    """`[]` is the corpus's spelling for NOBODY, so a caller sending it may mean the exact
    opposite of "open" — and a request whose two readings are "everyone" and "no one" is not one
    to guess at. Omitting the argument is the unambiguous way to say open, and the refusal says
    so."""
    with pytest.raises(CaptureError, match="not a request"):
        _service(indexed, ENG).submit("raw", "Another ordinary note.", audience=[])


# ── the vocabulary is the roster's, and the remedy is the door's ──────────────────────────────
def test_a_reserved_group_is_refused_at_the_door_too(indexed):
    """One vocabulary: a name that is spellable at the door and refused in the roster would be a
    capture filed at a label the server can never resolve for anyone."""
    with pytest.raises(CaptureError, match="reserved group"):
        _service(indexed, STEWARD).submit("raw", "For everyone.", audience=["all"])


def test_the_door_refusal_names_a_remedy_the_CALLER_can_act_on(indexed):
    """The shared validator's default remedy talks about editing a file. An MCP caller cannot see
    that file, so the door passes its own: omit `audience`."""
    with pytest.raises(CaptureError) as caught:
        _service(indexed, STEWARD).submit("raw", "For everyone.", audience=["all"])
    assert "omit `audience`" in str(caught.value)


def test_a_group_name_carrying_a_newline_is_refused_at_the_door(indexed):
    """These names are stamped into page frontmatter, and since D2 one of them can come from a
    model. The narrow grammar is what keeps that from being a page-contract injection."""
    with pytest.raises(CaptureError, match="invalid group"):
        _service(indexed, STEWARD).submit("raw", "Hi.", audience=["fin\nance"])


def test_a_bare_string_audience_is_refused_with_the_list_to_write(indexed):
    """The obvious client mistake, answered with the exact replacement rather than a type error."""
    with pytest.raises(CaptureError, match=r'\["finance"\]'):
        _service(indexed, ANA).submit("raw", "Hi.", audience="finance")


# ── `acl` stays the server's; `audience` is the request ───────────────────────────────────────
def test_the_resolved_label_is_still_refused_as_an_argument(indexed):
    """Both exist, and they are not the same thing. A caller may REQUEST an audience; asserting
    the resolved label would be asserting the server's own answer."""
    with pytest.raises(CaptureError, match="acl is set by the server"):
        _service(indexed, STEWARD).submit("raw", "Hi.", acl=["finance"])
