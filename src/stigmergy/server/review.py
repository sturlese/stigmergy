"""The one thing a person still writes to the knowledge repo from the serving process: a page
removal, decided and applied in the same call (ADR 043 D2).

Everything else that used to live here is gone with the waiting (ADR 044): an identity a capture
introduces is born confirmed by whoever captured it, and a repair is derived and applied by the
worker without anybody being asked. What is left is the deletion — a judgment only a person can
make, made at the command they gave it — plus `commission_registration`, which queues a capture
and touches no git at all.

The deletion goes through the SAME `repair.apply` door the worker's repairs go through, and lands
in the same ledger under the same three outcomes. What differs is one field: `actor` names the
person, which puts their name in the commit's `Approved-by:` trailer where a worker-derived repair
carries a `Repair:` line instead.

`delete_and_record` takes NO authorization argument: authorization is per-surface, so each door
decides who may before it calls in — the MCP tool by requiring an unrestricted identity, the
console by sitting behind its operator token.
"""
import logging
import os

from stigmergy.capture import queue
from stigmergy.capture import schema as capture_schema
from stigmergy.capture.errors import CaptureError
from stigmergy.entities.generator import ENTITY_TYPES, canonical_id_for
from stigmergy.librarian import gates
from stigmergy.repair import apply as repair_apply
from stigmergy.repair import schema as repair_schema
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
# anything is cloned or planned: a deployment that was never configured would otherwise spend a
# network leg and a model call to arrive at the same refusal.
REPAIR_REPO_UNCONFIGURED = (
    "no knowledge-repo URL is configured for this deployment — set "
    "$STIGMERGY_LIBRARIAN_REPO_URL to the same repo the librarian worker writes to. Nothing was "
    "changed")


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
        with repair_apply.cloned(repo_url, _KNOWLEDGE_BRANCH, os.environ) as ready:
            ops = repair_deletion.plan(ready.path, targets)
            oversize = repair_deletion.oversize_reason(ops, settings.max_plan_bytes)
            if oversize:
                raise ReviewError(oversize)
            spend: list = []
            ops = repair_sweep.write_sync(ready.path, ops,
                                          skill_text=repair_brief.read_skill(ready.path),
                                          model_name=settings.model, spend=spend)
            diffs = repair_deletion.unified_diffs(ready.path, ops)
            repair = {
                "kind": repair_schema.KIND_DELETE, "ops": ops,
                "target_paths": repair_schema.target_paths(ops), "rationale": reason,
                "content_key": repair_schema.content_key(ops, kind=repair_schema.KIND_DELETE),
                "run_id": 0, "finding_ids": [], "finding_subjects": [list(targets)],
                # A model wrote the pages that stay, never which pages go — and this column is
                # where that stays true after the session is gone.
                "model_id": settings.model if repair_deletion.scrubbed_paths(ops) else "",
            }
            # The SAME door the worker's repairs go through, so a deletion is in the ledger under
            # the same three outcomes as everything else — with `actor` filled in, which is what
            # puts a person's name in the commit's `Approved-by:` trailer instead of a `Repair:`
            # line. It is the one repair a human decides (ADR 043 D2).
            result = repair_apply.apply_and_record(
                conn, ready.path, _KNOWLEDGE_BRANCH, os.environ, repair=repair,
                author=ready.author, actor=actor)
    except RepairError as ex:
        # Every sentence `repair.apply`, `repair.deletion` and `repair.sweep` raise is written to
        # be published (their own module docstrings), so `str(ex)` crosses verbatim — and an
        # attempt that reached the apply is already recorded as `failed`.
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
        "withheld": withheld, "commit": result["commit"], "repair_id": result["id"],
        "model_calls": len(spend),
        "message": (
            f"deleted {len(result.get('deleted', []))} page(s) and rewrote {len(diffs)} that "
            f"referred to them, as commit {result['commit'][:12]}. Nobody read the rewritten prose "
            f"before it landed — the diffs above are that reading, and `git revert` in the "
            f"knowledge repo is the undo."
            + (f" {len(withheld)} diff(s) are withheld: those pages are outside what you may read "
               f"here, or this server's index does not carry them." if withheld else ""))}


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
