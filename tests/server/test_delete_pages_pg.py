"""`brain_delete` — the door a person removes pages at, which since the capture-is-the-approval change QUEUES and writes
nothing. There is ONE writer for the corpus and it is the worker; this process holds neither the
checkout nor the credential, so what lands here is a durable `delete` row with the person's name on
it and what they get back is a queue acknowledgement.

Real Postgres and the real queue primitive — a faked `capture_queue` would prove nothing about the
row a worker will claim minutes later. No git and no gates: there is nothing here to gate.

The four properties this door still owns, each with its subject moved to the queueing shape:

  · **authorization runs at the door, and before anything is queued.** An UNRESTRICTED identity may
    remove (the only kind that can see every page a removal touches, including the ones the sweep
    rewrites); a scoped one meets the lane's ONE anonymous sentence, the same for a page that
    exists and a page that does not, so the refusal is no existence oracle;
  · **the row carries the caller's identity, the kind, and the paths** — everything the worker will
    ever know about who asked for this and for what;
  · **a malformed removal is refused with nothing queued at all** — every question answerable
    without a checkout is answered in the person's own session, not minutes later where they are
    not looking;
  · **`brain_submit` cannot queue one.** That refusal is load-bearing: the worker performs whatever
    `delete` row it claims, so a submittable `delete` kind would let a scoped identity queue a
    removal without ever meeting the unrestricted check this door exists to run.

**The secrets scan over `why` is NOT here.** A reason becomes a commit message, which is permanent
and which no gate reads — and the scan that catches one runs in the worker's `_pre_agent`, over the
material of the row it claimed, exactly as it does for every other kind. It is proven where it
runs: `tests/librarian/test_delete_processing_pg.py::test_a_reason_carrying_a_secret_is_rejected_
before_any_tree_is_read`. Asserting it here would be asserting a guard this door does not have.
"""
import json

import pytest

from stigmergy.capture import queue
from stigmergy.capture import schema as capture_schema
from stigmergy.capture.errors import CaptureError, SubmissionRejected
from stigmergy.server import review, service
from tests.repair import support as repair_support
from tests.server.conftest import ALICE, STEWARD
from tests.server.conftest import make_review_service as make_service

WHY = "the memo was superseded and nothing needs it any more"
DOOMED = "wiki/notes/Superseded Renewal Memo.md"


def _delete(env, conn, paths, *, identity=STEWARD, why=WHY, audiences=None):
    """`brain_delete`, through the whole service seam — `_call`'s audit/rate-limit wrapper and the
    length checks included, because the door's refusals are specified as happening inside it."""
    return make_service(env, conn, identity_name=identity, audiences=audiences).delete_pages(
        paths, why, source="mcp")


def _rows(conn) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute("SELECT id, kind, status, submitted_by, hints, payload FROM capture_queue"
                    " ORDER BY id")
        return [{"id": r[0], "kind": r[1], "status": r[2], "submitted_by": r[3], "hints": r[4],
                 "payload": r[5]} for r in cur.fetchall()]


# ── the act: one queued row, in the caller's name ─────────────────────────────────────────────
def test_an_unrestricted_callers_removal_is_queued_with_their_name_and_their_paths(env, conn):
    """The benign twin every refusal below is measured against, and the whole of what this door
    does. The row is the worker's ONLY input — the kind that routes it, the identity that will
    become the commit's `Approved-by:` trailer, and the paths — so all three are asserted on the
    row in Postgres rather than on the acknowledgement, which is a copy."""
    ack = _delete(env, conn, [DOOMED, "wiki/notes/Another Memo.md"])

    (row,) = _rows(conn)
    assert row["id"] == ack["id"]
    assert (row["kind"], row["status"]) == (capture_schema.DELETE, capture_schema.QUEUED)
    assert row["submitted_by"] == STEWARD
    assert capture_schema.delete_paths(row["hints"]) == [DOOMED, "wiki/notes/Another Memo.md"]
    assert row["payload"]["text"] == WHY, (
        "the reason IS the material — it becomes the commit message body, and the worker scans it "
        "for secrets as it scans any capture's text")
    assert row["hints"]["client"]["delete_source"] == "mcp", "the row names the door it came from"
    assert f"Approved-by: {STEWARD}" in ack["message"], (
        "the acknowledgement promises exactly what will land — a person who asked for a removal "
        "must not have to guess whether it happened yet")


def test_the_acknowledgement_promises_a_queue_and_not_a_commit(env, conn):
    """OLD BEHAVIOUR: this door cloned, swept, gated, committed and pushed inside the MCP call, and
    handed back a commit sha and the diffs. the capture-is-the-approval change moved the act to the one writer, so the
    response can no longer claim any of that — what it may promise is a queued row, and where the
    reading of the written prose will appear when the worker has done it."""
    ack = _delete(env, conn, [DOOMED])

    assert ack["status"] == capture_schema.QUEUED
    assert "commit" not in ack and "rewritten" not in ack
    assert f"queued #{ack['id']}" in ack["message"]
    assert "diffs are on the capture" in ack["message"]


# ── authorization, at the door and before anything is queued ──────────────────────────────────
@pytest.mark.parametrize("path", [DOOMED, "wiki/notes/No Such Page At All.md"],
                         ids=["a-page-that-exists", "a-page-that-does-not"])
def test_a_scoped_caller_meets_one_anonymous_sentence_and_queues_nothing(env, conn, path):
    """The capture-is-the-approval change asks the one question this process can answer without a tree: is this identity
    unrestricted? A removal touches the pages it names AND every page that refers to them, a set
    nothing knows until the tree is read — so "may this caller see the whole corpus" is the only
    honest question at the door.

    ONE sentence for both "you may not" and "there is no such page", which is why this is
    parametrized over a page the corpus has and a page it has never had: a scoped identity probing
    for a page it cannot read must learn nothing from the difference."""
    with pytest.raises(CaptureError) as caught:
        _delete(env, conn, [path], identity=ALICE, audiences={"sales"})

    assert str(caught.value) == service.NOT_YOURS_TO_REMOVE
    assert _rows(conn) == [], "the refusal costs no row and no blob"


def test_an_unattributed_call_is_refused(env, conn):
    """A removal attributed to nobody would be a page removed with no answer to "who said so" —
    and the worker would have no name to put in the commit's trailer. Fail-closed: unreachable
    through either transport, characterized directly."""
    svc = make_service(env, conn, identity_name=STEWARD)
    svc.identity = None

    with pytest.raises(CaptureError, match="unattributed"):
        svc.delete_pages([DOOMED], WHY, source="mcp")

    assert _rows(conn) == []


# ── what the door refuses, with nothing queued ────────────────────────────────────────────────
@pytest.mark.parametrize("paths, why, phrase", [
    ([], WHY, "at least one page"),
    ([DOOMED], "   ", "needs a reason"),
    ([f"wiki/notes/{n}.md" for n in range(capture_schema.MAX_DELETED_PAGES + 1)], WHY, "at most"),
    (["wiki/entities/Acme Corp.md"], WHY, "identity"),
    (["ops/acl.json"], WHY, "not a corpus page"),
    (["wiki/notes/../../etc/passwd.md"], WHY, "not a corpus page"),
], ids=["no-page", "no-reason", "too-many", "an-entity-page", "outside-the-corpus", "traversal"])
def test_a_malformed_removal_is_refused_and_nothing_is_queued(env, conn, paths, why, phrase):
    """Every question answerable WITHOUT a checkout is answered here, in the person's own session.
    The alternative is a queued row that fails minutes later where nobody is looking — which is the
    whole reason this seam exists on the door as well as in the tree that decides.

    The entity page is the refusal a person is most likely to meet and the one that has to explain
    itself: an identity is retired by removing what made it one, never by deleting the page out
    from under the pages anchored to it."""
    with pytest.raises(CaptureError, match=phrase):
        _delete(env, conn, paths, why=why)

    assert _rows(conn) == []


def test_the_refusals_this_door_publishes_are_written_for_a_person(env, conn):
    """Every sentence here crosses to a person over MCP, so none may name a path on this host or
    hand out a command to run. Collected from the real refusals rather than re-typed, so a reword
    is checked by the same rule that first admitted it."""
    said = []
    for paths, identity, audiences in ((["wiki/entities/Acme Corp.md"], STEWARD, None),
                                       (["ops/acl.json"], STEWARD, None),
                                       ([], STEWARD, None),
                                       ([DOOMED], ALICE, {"sales"})):
        with pytest.raises(CaptureError) as caught:
            _delete(env, conn, paths, identity=identity, audiences=audiences)
        said.append(str(caught.value))

    assert len(said) == 4
    for message in said:
        repair_support.assert_person_facing(message)


# ── the authorization cannot be side-stepped: `delete` is not submittable ─────────────────────
def test_brain_submit_cannot_queue_a_removal(env, conn):
    """**The load-bearing refusal of this phase.** The queue's kind vocabulary is wider than what a
    submitter may ask for by exactly one, and the difference is this: the worker performs whatever
    `delete` row it claims, and the row is the whole of what it knows. If `brain_submit` accepted
    the kind, a SCOPED identity could queue a removal without ever meeting the unrestricted check
    `brain_delete` runs — the authorization would still be written down and simply never asked.

    Driven from a scoped identity for exactly that reason: this is the caller the hole would have
    served."""
    svc = make_service(env, conn, identity_name=ALICE, audiences={"sales"})

    with pytest.raises(SubmissionRejected, match="not something to submit"):
        svc.submit(capture_schema.DELETE, WHY,
                   hints={"delete_paths": DOOMED})

    assert _rows(conn) == []


def test_the_same_caller_may_still_submit_an_ordinary_capture(env, conn):
    """The benign twin, and it is not decoration: a kind refusal that also bounced a scoped
    identity's ordinary capture would be a rate limit on everybody's work wearing a security
    argument. One kind is refused at this door; every other one is not."""
    svc = make_service(env, conn, identity_name=ALICE, audiences={"sales"})

    ack = svc.submit(capture_schema.RAW, "a note worth keeping about the renewal")

    (row,) = _rows(conn)
    assert (row["id"], row["kind"]) == (ack["id"], capture_schema.RAW)


def test_the_queue_accepts_the_kind_the_submit_door_refuses(env, conn):
    """The other side of the same asymmetry, so "refused" can never quietly become "impossible":
    `queue.submit` — the primitive BOTH doors reach — takes `delete` happily. The kind is real, the
    worker dispatches on it, and what stands between a scoped caller and a removal is the door's
    check and nothing structural underneath it."""
    from stigmergy.capture.evidence import MemoryEvidenceStore

    ack = queue.submit(conn, MemoryEvidenceStore(), kind=capture_schema.DELETE, material=WHY,
                       hints={"delete_paths": DOOMED, "delete_source": "mcp"},
                       submitted_by=STEWARD)

    (row,) = _rows(conn)
    assert (row["id"], row["kind"]) == (ack["id"], capture_schema.DELETE)


# ── the shared seam, and the audit row ────────────────────────────────────────────────────────
def test_the_console_and_this_door_queue_through_the_same_seam(env, conn):
    """`review.queue_deletion` is what both doors call, and the ONLY thing either of them does —
    so which door a person removed from changes the row's `delete_source` and nothing else. Pinned
    here because the two doors authorize differently (an unrestricted identity, an operator token)
    and a second copy of the queueing would be a second place for that to drift."""
    ack = review.queue_deletion(conn, make_service(env, conn).evidence, paths=[DOOMED],
                                why=WHY, actor="ops@example.com", source="admin")

    (row,) = _rows(conn)
    assert row["id"] == ack["id"]
    assert row["submitted_by"] == "ops@example.com"
    assert row["hints"]["client"]["delete_source"] == "admin"
    assert row["kind"] == capture_schema.DELETE


def test_a_removal_queued_unattributed_through_the_shared_seam_is_refused(env, conn):
    """`queue_deletion` carries no authorization — each door decides who may before calling in —
    but it does refuse an unattributed one, because the trailer it promises has nobody to name."""
    with pytest.raises(review.ReviewError, match="unattributed"):
        review.queue_deletion(conn, make_service(env, conn).evidence, paths=[DOOMED], why=WHY,
                              actor="   ", source="admin")

    assert _rows(conn) == []


def test_the_audit_row_keeps_the_shape_and_never_the_reason(env, conn):
    """`why` is free text a person wrote about pages they read — a length and the paths are what an
    operator needs from `audit_log`, and the sentence itself lives on the row and in the commit
    message, where it was written to be read."""
    class _Audit:
        def __init__(self):
            self.rows = []

        def write(self, **kwargs):
            self.rows.append(kwargs)

    audit = _Audit()
    svc = make_service(env, conn, identity_name=STEWARD)
    svc.audit = audit

    svc.delete_pages([DOOMED], WHY, source="mcp")

    (row,) = [r for r in audit.rows if r["tool"] == "brain_delete"]
    assert row["identity"] == STEWARD
    assert row["outcome"] == "ok"
    assert row["args"]["paths"] == [DOOMED]
    assert row["args"]["why_chars"] == len(WHY)
    assert row["args"]["source"] == "mcp"
    assert WHY not in json.dumps(row["args"])
