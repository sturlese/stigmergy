"""Issue #32's road, TRAVELLED — an ordinary capture naming two unresolved entities, end to end.

`test_ordinary_multi_entity_park_unit.py` pins `processing._triage`'s routing with a duck-typed
outcome and no database. That is the routing contract; it is not the road. Until `double.py`'s
`DOUBLE:triage-entity=` learned to carry more than one name, the ordinary PLURAL inbound road was
crossed ZERO times by the entire keyless suite: every `_pg` test that parks an ordinary capture
declares exactly one name, so the flow at the heart of issue #32 — two names, through a real
parse, a real queue row, a real registry and a real worker cycle — was proven only by unit tests
of the pieces.

Everything here is real except the model: a real Postgres queue row claimed and finished through
`worker.process_next`, a real git repo + bare remote, the real `agent.parse_outcome` boundary the
double parks through, and the real one-ask budget in the database. The same posture
`test_processing_pg.py` states for itself.

The second half is the SINGULAR inbound spelling, which the same directive still emits for one
name. `agent.parse_outcome` accepts `triage.name` and folds it into a one-element list; that fold
is what every pre-collapse producer (and the repair brief's own PARK option) depends on, and the
one-name directive is what exercises it on every keyless run rather than in one unit test.
"""
import json
import pathlib

from stigmergy.capture import dispositions, queue, schema
from stigmergy.librarian import agent as agent_module
from stigmergy.librarian import worker
from tests.librarian import support

MATERIAL = "A memo about the Jack and Acme Capital co-investment, drafted for the record.\n"

# Two names in ONE directive, comma-separated — the shape `meeting-triage` already had and the
# ordinary lane did not. Neither is in the rig's registry (`Acme Corp`, aliased `Acme`), and
# "Acme Capital" is deliberately close to the one entry that IS: a park that quietly resolved it
# would be a false pass here.
TWO_NAMES = ["Jack", "Acme Capital"]
TWO_NAME_MATERIAL = f"DOUBLE:triage-entity={','.join(TWO_NAMES)}\n{MATERIAL}"

STEWARD = "steward@stigmergy.test"


def _row(conn, submission_id) -> dict:
    return queue.get_submission_trace(conn, submission_id)


def test_an_ordinary_two_name_capture_asks_about_both_then_parks_carrying_both(rig, clean_queue):
    """The whole plural road in one delivery pair, asserted on the PERSISTED ROW both times.

    Phase 1 — a fresh capture spends its one question on BOTH names at once: one ask, both
    numbered, `unresolved_names` carrying two entries on the row a steward and an MCP client both
    read. Phase 2 — the row goes back to the queue with its question already spent (`asked_at` is
    stamped on the first transition into `needs_input` and cleared by nothing, which is what
    `processing._ask_or_park` promises), so the next delivery parks it on the steward with
    `schema.SITUATION_NAMES_KEY` still carrying both names, independently approvable.

    The requeue is the real steward seam (`capture.dispositions.requeue`, what `stigmergy-queue
    requeue` calls), not a hand-written UPDATE: a test that reset the row itself would prove
    nothing about a row a human can actually produce, and the surviving `asked_at` is half of what
    this asserts.

    OLD BEHAVIOUR: `DOUBLE:triage-entity=` carried one name, so no keyless run could produce this
    row at all — the second name was unreachable from the double, and issue #32's flow was pinned
    only where `processing._triage` was called directly.
    """
    env, deps = rig
    before = support.branch_sha(env.bare)

    ack = support.submit(clean_queue, deps, TWO_NAME_MATERIAL)
    item, asked = worker.process_next(clean_queue, deps)

    # ── phase 1: ONE question, naming both ────────────────────────────────────────────────────
    assert asked.status == schema.NEEDS_INPUT, asked.report.get("summary")
    asked_row = _row(clean_queue, ack["id"])
    assert asked_row["status"] == schema.NEEDS_INPUT
    assert asked_row["report"]["unresolved_names"] == TWO_NAMES
    assert "unresolved_name" not in asked_row["report"]
    assert '1. "Jack"' in asked_row["report"]["summary"]
    assert '2. "Acme Capital"' in asked_row["report"]["summary"]
    assert "all 2 at once" in asked_row["report"]["summary"]
    # an ordinary capture is not a transcript, on the row a real submitter reads
    assert "meeting" not in asked_row["report"]["summary"]
    assert asked_row["asked_at"] is not None
    assert support.branch_sha(env.bare) == before          # nothing is filed ownerless

    # ── phase 2: the question is spent; the next delivery parks it on the steward ─────────────
    dispositions.requeue(clean_queue, ack["id"], actor=STEWARD, note="have a second look")
    _, parked = worker.process_next(clean_queue, deps)

    assert parked.status == schema.TRIAGE, parked.report.get("summary")
    parked_row = _row(clean_queue, ack["id"])
    assert parked_row["status"] == schema.TRIAGE
    assert parked_row["report"][schema.SITUATION_NAMES_KEY] == TWO_NAMES
    assert schema.SITUATION_NAME_KEY not in parked_row["report"], (
        "the singular key is retired as an OUTPUT — a row still carrying both keys is the "
        "duplication the collapse removed, and it passes a plural-key-only assertion")
    assert parked_row["report"][schema.SITUATION_KEY] == schema.SITUATION_UNRESOLVED_ENTITY
    assert '"Jack" and "Acme Capital"' in parked_row["report"]["summary"]
    assert "named 2 things" in parked_row["report"]["summary"]
    assert parked_row["result_ref"] == ""
    assert support.branch_sha(env.bare) == before          # still nothing committed


def test_the_two_name_park_is_two_independently_approvable_subjects_for_a_steward(rig, clean_queue):
    """What the plural key is FOR, read back through the steward's own reader rather than asserted
    as a JSON shape. `entities.situations` is the one module that reads these keys, and its answer
    for this row is what the mint doors and the admin console are handed: two subjects, and no
    prefill — because no single string is the right default for a two-name park (the C-3 contract,
    pinned pure in `tests/entities/test_situations.py`).

    A row produced by the REAL pipeline rather than hand-built: the pure tests decide what the
    reader does with a shape, and this decides that the writer really produces that shape.
    """
    from stigmergy.entities import situations

    _, deps = rig
    ack = support.submit(clean_queue, deps, TWO_NAME_MATERIAL)
    worker.process_next(clean_queue, deps)
    dispositions.requeue(clean_queue, ack["id"], actor=STEWARD)
    worker.process_next(clean_queue, deps)

    row = _row(clean_queue, ack["id"])
    assert row["status"] == schema.TRIAGE
    assert situations.classify(row) == schema.SITUATION_UNRESOLVED_ENTITY
    assert situations.subjects_of(row) == TWO_NAMES
    assert situations.mint_name_prefill(row) == ""


# ── the SINGULAR inbound spelling, still emitted, still folded ─────────────────────────────────
# `agent.parse_outcome` accepts `triage.name` and folds it into a one-element `names` list. That
# tolerance is permanent (the knowledge repo's `librarian` skill offers both spellings, and
# `gates.anchoring_brief`'s PARK option spells the singular), so it needs to be TRAVELLED, not
# merely unit-tested: the one-name directive is what travels it, on every keyless run in the suite.
#
# Asserted at the WIRE — the `.librarian-outcome.json` the double actually writes — because that is
# the only place the two spellings are still distinguishable. One layer later they are the same
# object, so a double that quietly started emitting the plural for one name would leave every other
# test in the suite green and the fold exercised nowhere.
def _emitted_outcome(env, deps, material: str) -> dict:
    """Run the double against the rig's real checkout and read back the account it wrote. The
    double parks through `agent.parse_outcome` itself, so a shape it could not have produced never
    reaches this file."""
    run = deps.agent.run(worktree=env.repo, material=material, hints={},
                         submitted_by=support.DEFAULT_SUBMITTER)
    raw = json.loads(pathlib.Path(env.repo, agent_module.OUTCOME_FILENAME).read_text())
    return {"wire": raw, "parsed": run.outcome}


def test_a_one_name_directive_still_writes_the_singular_triage_name_and_it_folds(rig):
    env, deps = rig

    emitted = _emitted_outcome(env, deps, f"DOUBLE:triage-entity=Halcyon Grid\n{MATERIAL}")

    assert emitted["wire"]["triage"]["name"] == "Halcyon Grid"
    assert "names" not in emitted["wire"]["triage"], (
        "one name must keep the SINGULAR spelling on the wire — emitting the plural here too "
        "leaves `parse_outcome`'s fold pinned in one unit test and travelled by nothing")
    # ...and the boundary folds it into the one shape everything downstream reads.
    assert emitted["parsed"].triage["names"] == ["Halcyon Grid"]
    assert "name" not in emitted["parsed"].triage


def test_a_two_name_directive_writes_the_plural_field_and_no_singular_one(rig):
    """The twin: the comma split really does reach the wire, and it does not ALSO write a
    singular field. Without this, the test above passes for a double that emits both every time."""
    env, deps = rig

    emitted = _emitted_outcome(env, deps, TWO_NAME_MATERIAL)

    assert emitted["wire"]["triage"]["names"] == TWO_NAMES
    assert "name" not in emitted["wire"]["triage"]
    assert emitted["parsed"].triage["names"] == TWO_NAMES
