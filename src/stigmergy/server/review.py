"""The write lane's two governed doors: a repair a person approves, and a page removal a person
performs.

Nothing is PROPOSED to a person here any more (ADR 044): an identity a capture introduces is born
confirmed by whoever captured it, and this module keeps only the two sequences that touch the
knowledge repo from the serving process — `apply_repair_and_record`/`reject_repair_and_record`
(the repair loop's verdict, ADR 039) and `delete_pages`/`delete_and_record` (a person's own
deletion, applied in the act, ADR 043).

Both sequences take NO authorization argument: authorization is per-surface, so each door decides
who may before it calls in — the MCP tool by requiring an unrestricted identity, the console by
sitting behind its operator token.
"""
import logging
import os
from datetime import datetime

from stigmergy.capture import queue
from stigmergy.capture import schema as capture_schema
from stigmergy.capture.errors import CaptureError
from stigmergy.entities.generator import ENTITY_TYPES, canonical_id_for
from stigmergy.librarian import gates
from stigmergy.repair import remote as repair_remote
from stigmergy.repair import schema as repair_schema
from stigmergy.repair import store as repair_store
from stigmergy.repair.errors import RepairError
from stigmergy.text import fence, one_line

log = logging.getLogger(__name__)

# The repair ledger's DDL, re-exported for the startup pass in `service.build_service`: this module
# is where the table is written, so this is where a caller asks for it.
ensure_repair_schema = repair_schema.ensure_repair_schema


def _check_len(name: str, value: str) -> None:
    """Lazy: `service.py` imports THIS module at module scope, so the reverse edge must not be
    taken at import time. Without the bound an unbounded `notes` string reaches
    the ledger or a gitleaks scan."""
    from stigmergy.server.service import check_arg_length
    check_arg_length(name, value or "")

# The two sentences every refusal on this lane shares. Anonymous on purpose: a caller who may not
# act on an id learns nothing about whether it exists.
NOT_YOURS_TO_DECIDE = "there is nothing for you to decide at that id"


class ReviewError(CaptureError):
    """A refusal a caller may read — the vocabulary both doors below raise in."""


def _parse_id(item_id: str) -> int | None:
    """`int(item_id)`, or `None` — a malformed id is as "not found" as a nonexistent one."""
    try:
        return int(item_id)
    except (TypeError, ValueError):
        return None


def _neutralize_leaves(value, depth: int = 0):
    """`neutralize_fence` over every STRING LEAF — one rule at the boundary instead of per-field
    calls, which reliably miss a field. Past the depth bound the subtree is DROPPED: a recursion
    limit that hands back what it declined to check is a fail-open bound.

    The walker itself is `service._neutralize_report`, the ONE implementation — a second copy of a
    boundary rule is a second place for it to drift, and this one had. Lazy for `_check_len`'s
    cycle reason."""
    from stigmergy.server.service import _neutralize_report
    return _neutralize_report(value, depth)


def _iso(value) -> str | None:
    """`capture.queue._iso`'s rule, spelled here rather than imported: it is private to that module
    and this is a two-line predicate, not a shared decision."""
    return value.isoformat() if isinstance(value, datetime) else None


NOTE_SCAN_TIMEOUT_S = 30


def _refuse_secret_note(notes: str) -> None:
    """The secrets scan over a caller-typed note, at the CALL SITE because `stigmergy.capture` may
    never import `stigmergy.librarian`. Runs before `notes` reaches `clean` or a report."""
    if not (notes or "").strip():
        return
    gitleaks_bin = os.environ.get("STIGMERGY_GITLEAKS_BIN", "gitleaks")
    hits = gates.scan_secrets(notes, gitleaks_bin=gitleaks_bin, label="a review note",
                              timeout_s=NOTE_SCAN_TIMEOUT_S)
    if hits:
        # the rule id comes structurally from `Finding.values`, never re-parsed out of prose
        _line, rule = hits[0].values
        raise ReviewError(
            "refusing to record this note — it matches a likely secret "
            f"(rule: {rule}). Nothing was recorded; remove "
            "the credential and try again")


_KNOWLEDGE_BRANCH = "main"

# What a door is told when this deployment cannot write to the knowledge repo at all. Asked BEFORE
# the proposal leaves `pending`, which is the whole point: `remote.apply_approved` records a
# refusal as `failed`, and a deployment that was never configured would burn a proposal per
# approval for a reason that has nothing to do with the proposal. The entity door refuses the same
# condition by name for the same reason (`entities.remote`'s own capability refusal).
REPAIR_REPO_UNCONFIGURED = (
    "no knowledge-repo URL is configured for a server-driven repair — set "
    "$STIGMERGY_LIBRARIAN_REPO_URL to the same repo the librarian worker writes to. The proposal "
    "is untouched and still pending")


# The sentence the loser of two simultaneous decisions gets. Composed once: both verdicts can lose
# the same race, and a reader given two different explanations of one condition would reasonably
# conclude they are two conditions.
_LOST_THE_RACE = ("this proposal is no longer pending — somebody decided it between your reading "
                  "of the queue and this decision. Re-read the queue; nothing was changed by your "
                  "call")


def _repair_proposal_or_refuse(conn, item_id) -> dict:
    """The pending proposal at `item_id`, or the anonymous refusal. A malformed id, a nonexistent
    one and an already-decided one are ONE sentence, exactly as they are for the other two kinds:
    "there is nothing for you to decide at that id" is true of all three."""
    proposal_id = _parse_id(str(item_id))
    row = repair_store.proposal(conn, proposal_id) if proposal_id is not None else None
    if row is None or row["status"] != repair_schema.STATUS_PENDING:
        raise ReviewError(NOT_YOURS_TO_DECIDE)
    return row


def apply_repair_and_record(conn, *, repo_url: str, proposal: dict, actor: str, source: str,
                            notes: str = "") -> dict:
    """Approve ONE repair proposal and apply it, in the one order that is correct.

    The console is the door that runs this today; ADR 044 D2 retires the approval itself, and the
    worker will derive-validate-apply in one pass (phase 3 of #134). Until then the ordering rule
    lives here, once, so no door can reorder it.

    The order, and why each step is where it is:

    1. `mark_decided(approved)` FIRST, and it is a conditional UPDATE (`WHERE status = 'pending'`).
       That is what makes a second Approve lose rather than clone: the loser sees zero rows and is
       told so, and the winner holds a row nothing else can act on for the whole of the clone. No
       lease is needed anywhere below because of this one line.
    2. `remote.apply_approved` — clone, re-validate, gate, cross-check, commit, push, and record
       the outcome (`applied` with the sha, or `failed` with the sentence). A `RepairError` out of
       it has ALREADY been recorded as failed, and the approved status is deliberately NOT
       restored: a silent revert to pending would hide that a gate refused.
    It takes NO authorization argument, on purpose: authorization is per-surface (the operator
    token, at the console), so the CALLER SET is closed and pinned in `tests/test_architecture.py`.

    `apply_approved` is reached as a MODULE ATTRIBUTE so it stays monkeypatchable.

    `notes` is the only record of why a repair was worth applying, stored on the proposal row
    beside the verdict.
    """
    if not repo_url:
        raise ReviewError(REPAIR_REPO_UNCONFIGURED)
    if not repair_store.mark_decided(conn, proposal["id"], status=repair_schema.STATUS_APPROVED,
                                     decided_by=actor, notes=notes):
        raise ReviewError(_LOST_THE_RACE)
    del source          # the door is recorded on the proposal row and in `admin_actions`
    result = repair_remote.apply_approved(
        conn, repo_url, _KNOWLEDGE_BRANCH, os.environ, proposal=proposal, approved_by=actor)
    return {"applied": True, "commit": result["commit"], "paths": result["paths"]}


# What a deletion may be, at the door. `MAX_DELETED_PAGES` is not a technical bound — the plan's
# byte ceiling is — it is what one person's `brain_delete` call may mean: a page they judged
# stale, or a handful, never a corpus sweep typed in one line.
MAX_DELETED_PAGES = 10
DELETE_REASON_CHARS = 400

DELETE_NEEDS_A_REASON = (
    "a deletion needs a reason: what makes these pages stale. It is what `git log` carries "
    "afterwards and the only thing a later reader will have — nothing was deleted")

DELETE_NEEDS_A_PAGE = "a deletion names at least one page — nothing was deleted"

DELETE_TOO_MANY = (
    "a deletion at this door names at most {ceiling} page(s), and this one names {n}. One call is "
    "one judgment a person made about pages they read; a larger sweep is a series of them — "
    "nothing was deleted")


def delete_pages(service, *, paths, why: str, source: str) -> dict:
    """A person's own deletion, decided and applied in ONE call (ADR 043 D2).

    A human decides, at the command when they gave it. The judgment is in this call — these
    pages, this reason, this identity — so there is nobody left to ask, and what the second click
    used to supply was an AUTHENTICATION the CLI could not perform. It is performed here instead,
    in the act.

    The order, and why each step is where it is:

    1. **Authorization FIRST, before any clone** — an unrestricted identity, or the lane's own
       anonymous refusal. An unauthorized caller must not be able to spend a network leg, and must
       learn nothing about which pages exist.
    2. **One clone, held open for the whole pass.** The plan, the written sweep and the apply all
       run against THIS tree, which is why there is no propose-to-apply gap to prove anything
       across (D3): `apply_declared`'s base hashes and its walk for a latecomer are a formality
       that costs one walk.
    3. **The row is born `approved` in the caller's name**, then applied through the same
       `remote.apply_approved` every other door runs — so `applied`/`failed`, the ledger row and
       the console's history are the ones they already are. It is never listed as pending.
    Returns the commit, the pages, and the per-page DIFF — which is the whole of D5: nobody read
    the written prose before it landed, so the diff is the reading, and it goes back to the person
    who asked in the same breath.
    """
    if not service.identity:
        raise ReviewError("no resolved identity — a deletion cannot be attributed unattributed")
    # UNRESTRICTED identities only (ADR 044 D3). A removal touches the pages it names AND every
    # page that refers to them — a set the server cannot know before it clones — so the one
    # question it can answer at the door is "may this caller see the whole corpus": an identity
    # with no audience restriction can, and a scoped one cannot, whatever the paths turn out to be.
    # The refusal is the lane's anonymous sentence, so it is no existence oracle either.
    if not service.unrestricted:
        raise ReviewError(NOT_YOURS_TO_DECIDE)
    return delete_and_record(
        service.conn, repo_url=service.settings.librarian_repo_url, paths=paths, why=why,
        actor=service.identity, source=source, can_read=service.may_read_page)


def delete_and_record(conn, *, repo_url: str, paths, why: str, actor: str, source: str,
                      can_read=None) -> dict:
    """The sequence itself, shared by the two doors a person deletes from — the MCP lane through
    `delete_pages` and the console through `admin.service.pages_delete`.

    It takes NO authorization of its own: authorization is per-surface, so each door decides who
    may before it calls in — the MCP tool by requiring an unrestricted identity, the console by
    sitting behind its operator token. The CALLER SET is closed and pinned in
    `tests/test_architecture.py`.

    `can_read` is the seam for the OTHER question: the diffs this returns are page bytes, so the
    MCP door hands in `acl.visible()` through the caller's own audiences, and the console hands in
    nothing, because it is not a read surface over pages and its token already stands for the whole
    deployment.
    """
    if not repo_url:
        raise ReviewError(REPAIR_REPO_UNCONFIGURED)
    targets = sorted({str(p).strip() for p in (paths or ()) if str(p).strip()})
    if not targets:
        raise ReviewError(DELETE_NEEDS_A_PAGE)
    if len(targets) > MAX_DELETED_PAGES:
        raise ReviewError(DELETE_TOO_MANY.format(ceiling=MAX_DELETED_PAGES, n=len(targets)))
    _check_len("why", why or "")
    _refuse_secret_note(why or "")
    reason = one_line(capture_schema.clean_note(why), DELETE_REASON_CHARS)
    if not reason:
        raise ReviewError(DELETE_NEEDS_A_REASON)

    # Imported HERE, not at module scope, and for the reason `ask` imports the answer layer
    # inside its tool: `repair.sweep` loads a model stack, and this module is imported by every
    # process that serves an MCP call. A deletion is the one call that wants one — so the weight
    # is paid by the caller who asked for it, and `review.py`'s import graph stays what
    # `test_review_transitive_kernel_reach_is_a_named_declared_exception` pins. The edge itself is
    # still declared and pruned in `tests/test_architecture.py`, which reads function-level
    # imports too (ADR 043 D4).
    from stigmergy.repair import brief as repair_brief
    from stigmergy.repair import deletion as repair_deletion
    from stigmergy.repair import sweep as repair_sweep
    from stigmergy.repair.settings import RepairSettings

    settings = RepairSettings.from_env()
    try:
        with repair_remote.cloned(repo_url, _KNOWLEDGE_BRANCH, os.environ) as ready:
            ops = repair_deletion.plan(ready.path, targets)
            oversize = repair_deletion.oversize_reason(ops, settings.max_plan_bytes)
            if oversize:
                raise ReviewError(oversize)
            spend: list = []
            ops = repair_sweep.write_sync(ready.path, ops,
                                          skill_text=repair_brief.read_skill(ready.path),
                                          model_name=settings.model, spend=spend)
            diffs = repair_deletion.unified_diffs(ready.path, ops)
            proposal_id = repair_store.insert_proposal(
                conn=conn, run_id=0, finding_ids=[], kind=repair_schema.KIND_DELETE,
                target_paths=repair_schema.target_paths(ops), ops=ops, rationale=reason,
                content_key=repair_schema.content_key(ops, kind=repair_schema.KIND_DELETE),
                # A model wrote the pages that stay, never which pages go — and this column is
                # where that stays true after the session is gone.
                model_id=settings.model if repair_deletion.scrubbed_paths(ops) else "",
                finding_subjects=[list(targets)],
                status=repair_schema.STATUS_APPROVED, decided_by=actor)
            proposal = repair_store.proposal(conn, proposal_id)
            result = repair_remote.apply_approved(
                conn, repo_url, _KNOWLEDGE_BRANCH, os.environ, proposal=proposal,
                approved_by=actor, prepared=ready)
    except RepairError as ex:
        # Every sentence `repair.remote`, `repair.deletion` and `repair.sweep` raise is written to
        # be published (their own module docstrings), so `str(ex)` crosses verbatim — and a row
        # that reached the apply is already recorded as `failed`.
        raise ReviewError(str(ex)) from ex
    # The reading (D5), and it is page CONTENT going back over the wire, so it obeys the two rules
    # every other surface that echoes a page obeys: `visible()` decides who may read one — the ONE
    # place read access is decided, and being a STEWARD of a folder is a different question from
    # being in a page's audience — and every diff is FENCED, because it carries both the page's own
    # bytes and fresh model output, and neither is an instruction to whoever reads this response.
    readable = {path: fence(text) for path, text in diffs.items()
                if can_read is None or can_read(path)}
    # A page whose diff is withheld is NAMED rather than dropped: it changed, the commit says so,
    # and a reader who cannot see why must not be left thinking nothing happened to it. Fails
    # closed on a page this server's index does not carry, which is the same reading `read_page`
    # gives — existence itself is scoped.
    withheld = sorted(set(diffs) - set(readable))
    return {
        "deleted": result.get("deleted", []), "rewritten": readable,
        "withheld": withheld, "commit": result["commit"], "proposal_id": proposal["id"],
        "model_calls": len(spend),
        "message": (
            f"deleted {len(result.get('deleted', []))} page(s) and rewrote {len(diffs)} that "
            f"referred to them, as commit {result['commit'][:12]}. Nobody read the rewritten prose "
            f"before it landed — the diffs above are that reading, and `git revert` in the "
            f"knowledge repo is the undo."
            + (f" {len(withheld)} diff(s) are withheld: those pages are outside what you may read "
               f"here, or this server's index does not carry them." if withheld else ""))}


def reject_repair_and_record(conn, *, proposal: dict, actor: str, source: str,
                             reason: str) -> dict:
    """Decline ONE repair proposal.

    A REJECTED row is the dismissal memory (`repair.schema`): the proposer skips a content key that
    has any prior row, so "reviewed and declined" is a durable fact and nobody is asked the same
    question tomorrow. The reason is stored on the proposal, which is what the proposer reads.
    """
    del source          # the door is recorded in `admin_actions`
    if not repair_store.mark_decided(conn, proposal["id"], status=repair_schema.STATUS_REJECTED,
                                     decided_by=actor, notes=reason):
        raise ReviewError(_LOST_THE_RACE)
    return {"rejected": True}


def commission_registration(conn, evidence, *, name: str, entity_type: str, aliases: list[str],
                            about: str, actor: str, source: str) -> dict:
    """A person introducing an entity nobody has captured about yet — the console's Register door.

    There is no deterministic birth: what they know about the entity (`about`) is queued as a
    capture carrying the registration, and the librarian writes the entity's page from it and from
    what the brain already holds, anchors the note to it, and births the identity CONFIRMED by
    `actor` (ADR 042, ADR 044 D1). Nothing here touches git.

    It carries no authorization of its own: the console decides under its operator token before
    calling it, and `brain_submit` accepts the same `register_*` hints from any door — a
    registration pins what the librarian would otherwise infer, and pins nothing else.
    """
    clean_name = " ".join(str(name or "").split())
    clean_about = str(about or "").strip()
    if not clean_name or not clean_about:
        raise ReviewError(
            "registering an entity needs its name and what it is: the librarian writes the page "
            "from what you say and from what the brain already holds, and a page with nothing said "
            "about the entity is not written at all")
    if entity_type not in ENTITY_TYPES:
        raise ReviewError(f"entity_type {entity_type!r} is not one of {', '.join(ENTITY_TYPES)}")
    if evidence is None:
        raise ReviewError("the capture queue is not available on this server, so nothing can be "
                           "registered — it needs an evidence store configured")
    hints = capture_schema.registration_hints(name=clean_name, entity_type=entity_type,
                                              aliases=aliases, source=source)
    ack = queue.submit(conn, evidence, kind=capture_schema.RAW, material=clean_about, hints=hints,
                       submitted_by=actor)
    return {**ack, "entity_id": canonical_id_for(clean_name), "name": clean_name,
            "message": (f"commissioned as capture #{ack['id']}: the librarian writes the page of "
                        f"{clean_name} from what you said and what the brain already holds, anchors "
                        f"the note to it, and the entity is born confirmed by {actor}. It appears "
                        f"in Entities when the capture files.")}
