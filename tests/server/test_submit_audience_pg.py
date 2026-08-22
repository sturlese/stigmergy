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
        _service(indexed, ANA).submit("raw", "Probing.", audience=["eng"])
    assert "finance" in str(caught.value)
    assert "eng" not in str(caught.value)


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
    ack = _service(indexed, STEWARD).submit("raw", "Board pack summary.", audience=["eng"])
    assert ack["acl"] == ["eng"]


def test_a_caller_may_file_at_several_groups_when_they_hold_one_of_them(indexed):
    """`visible()` is "shares at least one label", and the door asks exactly that of the writer:
    ana can read a `[finance, eng]` page, so she may file one. Widening it to `eng` readers is a
    human choice, attributed to her on every page it writes.

    Stored SORTED: `capture_queue.acl` is a `text[]` and dedup compares it element-wise, so one
    audience must have one spelling however a caller ordered it."""
    ack = _service(indexed, ANA).submit("raw", "Shared finance and engineering note.",
                                              audience=["finance", "eng"])
    assert ack["acl"] == ["eng", "finance"]


def test_the_stored_label_is_canonical_whatever_order_the_caller_used(indexed):
    """Two callers naming the same groups in different orders must produce the SAME row value, or
    dedup's `IS NOT DISTINCT FROM` sees two audiences and files the material twice."""
    first = _service(indexed, STEWARD).submit("raw", "One ordering.",
                                              audience=["eng", "finance"])
    second = _service(indexed, STEWARD).submit("raw", "The other ordering.",
                                               audience=["finance", "eng"])
    assert first["acl"] == second["acl"] == ["eng", "finance"]


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


# ── `brain_submissions` names pages, and is safe by construction ──────────────────────────────
def test_your_own_submissions_name_only_pages_you_could_read(indexed):
    """**Why the queue read needs no per-page filter, asserted rather than assumed.**

    `brain_submissions` returns `result_ref` and the librarian's report, which name page paths —
    with no `visible()` over them. It is scoped by IDENTITY: you see your own rows, and an
    unrestricted identity sees the whole queue.

    That is safe by CONSTRUCTION, and the construction is the door's rule: a capture is filed only
    at an audience its submitter could read afterwards, so every page their own rows name is one
    they may open. Break the door check and this read becomes a disclosure — which is why the
    property is pinned here, beside the door, and not merely argued in a comment."""
    conn, _fx = indexed
    ana = _service(indexed, ANA)
    ack = ana.submit("raw", "A payroll note for the finance folder.", audience=["finance"])

    # Through the READ this test is about, not through the predicate the door just applied to the
    # same values — that would only re-run `visible()` on its own answer.
    rows = ana.submissions(limit=50)["submissions"]
    mine = next(r for r in rows if r["id"] == ack["id"])
    assert mine["submitted_by"] == ANA

    with conn.cursor() as cur:
        cur.execute("SELECT acl FROM capture_queue WHERE id = %s", (ack["id"],))
        row_acl = cur.fetchone()[0]
    from stigmergy.server.acl import visible
    assert visible(row_acl, ana.audiences), (
        "a submitter's own row carries an audience they cannot read — `brain_submissions` would "
        "then hand them a page path they may not open")


def test_a_scoped_identity_sees_only_its_own_rows(indexed):
    """The other half of the construction: the identity scope itself. Ana must not see Eng's row,
    whatever either of them filed at."""
    _service(indexed, ENG).submit("raw", "An engineering note nobody else asked for.")
    mine = _service(indexed, ANA).submissions(limit=50)
    assert mine["scope"] == "own"
    assert all(row["submitted_by"] == ANA for row in mine["submissions"]), mine


def test_a_principal_holding_NO_group_may_still_file_open(indexed):
    """**The sharpest missing twin.** The ADR says "a caller with no groups may file open and
    nothing else", and every other test of that sentence used an identity that holds one. The
    half that matters for a newcomer — who holds nothing on their first day — is that they can
    still capture. A door that refused them would make the brain unusable for exactly the people
    it is trying to onboard, and would read as a passing security test."""
    conn, fx = indexed
    from stigmergy.capture.evidence import MemoryEvidenceStore
    from tests.server.conftest import make_service
    newcomer = make_service(fx, conn, ANA, evidence=MemoryEvidenceStore())
    newcomer.audiences = set()          # authenticated, holds nothing

    ack = newcomer.submit("raw", "My first note, about Initech.")

    assert ack["acl"] is None
    assert _row_acl(indexed, ack["id"]) is None


def test_and_that_same_principal_may_file_at_NOTHING_else(indexed):
    """Its sensitivity half, on the same identity: holding no group means open is the only
    audience available, so the two tests together say the whole sentence."""
    conn, fx = indexed
    from stigmergy.capture.evidence import MemoryEvidenceStore
    from tests.server.conftest import make_service
    newcomer = make_service(fx, conn, ANA, evidence=MemoryEvidenceStore())
    newcomer.audiences = set()

    with pytest.raises(CaptureError, match="could not read afterwards"):
        newcomer.submit("raw", "Something for finance.", audience=["finance"])


# ── a group nobody holds is a page nobody can ever read ───────────────────────────────────────
def test_an_audience_naming_a_group_nobody_holds_is_refused(indexed):
    """The typo the shape rules cannot see and `visible()` will not catch: `["finanace"]` is a
    legal group NAME, and for an unrestricted caller `visible()` returns True unconditionally — so
    the page files at a label no identity holds and is readable by nobody, permanently, since a
    filed page's audience cannot be changed. The unrestricted caller is precisely the one whose
    typo is silent: a scoped caller is protected by accident, because they must share a label with
    what they name."""
    with pytest.raises(CaptureError, match="readable by nobody"):
        _service(indexed, STEWARD).submit("raw", "For a group that does not exist.",
                                          audience=["finanace"])


def test_that_refusal_echoes_the_callers_own_word_and_no_others(indexed):
    """It names what the caller typed — already theirs — and never enumerates the groups that DO
    exist, which would make a refused submit a roster oracle."""
    with pytest.raises(CaptureError) as caught:
        _service(indexed, STEWARD).submit("raw", "Hi.", audience=["nosuchgroup"])
    message = str(caught.value)
    assert "nosuchgroup" in message
    assert "finance" not in message and "eng" not in message, message


def test_the_benign_twin_a_real_group_the_caller_is_not_in_still_files(indexed):
    """The specificity half, and it is the intended WIDENING: an unrestricted caller may file at
    any group that exists, including ones they are not in. A rule that refused that would make the
    check indistinguishable from "you may only file at your own groups", which is not the rule."""
    ack = _service(indexed, STEWARD).submit("raw", "For the engineers.", audience=["eng"])
    assert ack["acl"] == ["eng"]
