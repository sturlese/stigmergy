"""The review lane — the ONE inbox where work parks on a human, and the append-only record of what
they decided.

`reject` and every `parked-capture` verdict are Postgres only, categorically. Approving an
`entity-proposal` or a `repair-proposal` are the two paths that touch git: exactly ONE commit
through a governed door, authored as the librarian App with an `Approved-by: <caller>` trailer.

Authorization runs FIRST — an `entity-proposal` needs a STEWARD, a `parked-capture` the steward OR
the asked submitter, a `repair-proposal` a steward for EVERY page it would edit — and a refusal
never becomes more specific once a caller has failed it.

The two git-touching verdicts share a shape and not an implementation: each has ONE ordering
function that both of its doors run (`mint_and_record_approval`, `apply_repair_and_record`), so
"the ledger row is written, and written after the push" is a property of the code rather than of
each surface remembering.
"""
import logging
import os
from datetime import UTC, date, datetime

from stigmergy.capture import decisions, dispositions
from stigmergy.capture import ops as capture_ops
from stigmergy.capture import queue as capture_queue
from stigmergy.capture import schema as capture_schema
from stigmergy.capture.errors import CaptureError
from stigmergy.entities import remote as entities_remote
from stigmergy.entities import situations
from stigmergy.entities.errors import CapabilityUnavailableError as EntityCapabilityUnavailableError
from stigmergy.entities.errors import EntityError
from stigmergy.entities.generator import ENTITY_TYPES, canonical_id_for
from stigmergy.librarian import base_inputs, gates, gitcmd
from stigmergy.librarian.errors import LibrarianError
from stigmergy.repair import remote as repair_remote
from stigmergy.repair import schema as repair_schema
from stigmergy.repair import store as repair_store
from stigmergy.repair.errors import RepairError
from stigmergy.review_kinds import (
    ITEM_KINDS,
    KIND_ENTITY_PROPOSAL,
    KIND_PARKED_CAPTURE,
    KIND_REPAIR_PROPOSAL,
)
from stigmergy.server.errors import CapabilityUnavailableError

log = logging.getLogger(__name__)


def _check_len(name: str, value: str) -> None:
    """Lazy: `service.py` imports THIS module at module scope, so the reverse edge must not be
    taken at import time. Without the bound an unbounded `notes` string reaches
    `dispositions.clean` or a gitleaks scan."""
    from stigmergy.server.service import check_arg_length
    check_arg_length(name, value or "")

# The ledger lives in `capture.decisions`, BELOW both this package and `stigmergy.entities`, so
# the entities CLI writes the same row without crossing the `entities` -> `server` edge.
# Re-exported here under the names callers already use — `latest_decisions` among them, because
# `stigmergy.slack` may reach `stigmergy.capture` only through `slack.store`, and only for
# `.schema` (tests/test_architecture.py): the doorbell's closing pass reads the ledger through
# THIS module or not at all.
ensure_review_schema = decisions.ensure_decisions_schema
record_decision = decisions.record_decision
latest_decisions = decisions.latest_decisions

# Which door is recording — the closed set lives with the table (`capture.decisions`) and is
# re-exported here so the two surfaces that reach the ledger through this module (`stigmergy.slack`,
# `stigmergy.admin`) name their door with a constant rather than a literal.
DECISION_SOURCES = decisions.DECISION_SOURCES
SOURCE_MCP, SOURCE_SLACK = decisions.SOURCE_MCP, decisions.SOURCE_SLACK
SOURCE_ADMIN, SOURCE_CLI = decisions.SOURCE_ADMIN, decisions.SOURCE_CLI


# `review_decide`'s verdict vocabulary — uniform EXCEPT `parked-capture`, which keeps
# `capture.dispositions`' own three verbs.
# Re-exported from the ledger that records them (`capture.decisions`), not defined here: this
# module is one of three writers, and the vocabulary belongs with the table.
APPROVE, REJECT, REQUEST_CHANGES = decisions.APPROVE, decisions.REJECT, decisions.REQUEST_CHANGES
GENERIC_VERDICTS = decisions.GENERIC_VERDICTS

class ReviewError(CaptureError):
    """A clean, caller-facing refusal, echoed verbatim over the wire — never git/DB internals."""


# ONE byte-identical sentence for every unauthorized refusal AND for a nonexistent id: "does not
# exist", "somebody else's item" and "not a steward" must be indistinguishable from the outside.
# The specific refusals still surface, but only AFTER this predicate has cleared.
NOT_YOURS_TO_DECIDE = "there is nothing for you to decide at that id"

# Governance requires a SECOND human: the proposer may never approve, even when they are also the
# resolved steward.
SELF_APPROVAL_REFUSED = (
    "you filed this — approving it needs a second, different steward. Ask another steward to "
    "review it; you may still record reject or request_changes on your own submission yourself")


def _stewards_snapshot(service) -> dict | None:
    """The stewards map this server resolves against, or `None` when it cannot be read at all.

    ONE loader, because a decision is made against ONE map. `is_steward` answers a single scope and
    calls this once; `_guard_repair_decision` asks about several PATHS and calls it once for all of
    them. Splitting the load out is what makes that possible without a second copy of the
    fail-closed reasoning below.

    `None` is "no answer", distinct from `{}` ("an answer, and it names nobody") — the caller turns
    either into a refusal, but only this shape lets it.
    """
    repo = service.settings.knowledge_repo or ""
    baked = service.settings.stewards_path or ""
    if not repo and not baked:
        return None
    try:
        return load_stewards(repo, baked)
    except (LibrarianError, OSError):
        # The promise `is_steward`'s docstring makes, kept here rather than at each call site. A
        # malformed `ops/stewards.json` raises `LibrarianConfigError` and a broken checkout raises
        # out of `gitcmd`; the DECIDE leg's own `except Exception` would absorb either, but the
        # READ leg in `slack.review` has nothing to absorb them and a steward's click would vanish
        # with no feedback at all. Fail closed — and log the fault, because the caller only ever
        # sees an ordinary refusal and this is the operator's only copy of the diagnosis.
        log.error("steward resolution failed — treating the caller as not a steward",
                  exc_info=True)
        return None


def is_steward(service, scope_path: str) -> bool:
    """Is the caller's resolved identity a steward for `scope_path`? Fails closed with `False`,
    never an exception, when this server has neither a checkout nor a baked snapshot.

    PUBLIC because it is also the READ-side gate: a surface that shows review material BEFORE a
    decision (`slack.review`'s entity-mint modal renders a proposal's unresolved names) has to ask
    the same question the decide leg asks, at the same scope, or the decide leg's own guard arrives
    after the material has already been served.

    ONE scope, ONE load. A caller asking about several paths must call `_stewards_snapshot` itself
    rather than this in a loop — see `_guard_repair_decision`."""
    stewards = _stewards_snapshot(service)
    if stewards is None:
        return False
    return bool(service.identity) and service.identity in resolve_stewards_for_scope(
        stewards, scope_path)


def _guard_governance_decision(service, *, found: bool, submitted_by: str, scope_path: str,
                               verdict: str) -> None:
    """The `entity-proposal` gate: steward required, self-approval refused. `found=False` and "not
    a steward" collapse onto the SAME `NOT_YOURS_TO_DECIDE` sentence."""
    if not found or not is_steward(service, scope_path):
        raise ReviewError(NOT_YOURS_TO_DECIDE)
    if verdict == APPROVE and submitted_by and service.identity == submitted_by:
        raise ReviewError(SELF_APPROVAL_REFUSED)


def _guard_parked_capture_decision(service, *, found: bool, submitted_by: str) -> None:
    """`parked-capture`'s looser rule: the row's own submitter, OR a steward. No self-approval
    refusal — this kind has no `approve`."""
    if not found:
        raise ReviewError(NOT_YOURS_TO_DECIDE)
    if service.identity and service.identity == submitted_by:
        return
    if is_steward(service, ""):   # a parked capture has no page path yet: universal scope only
        return
    raise ReviewError(NOT_YOURS_TO_DECIDE)


def _guard_repair_decision(service, *, found: bool, target_paths) -> None:
    """`repair-proposal`'s rule: a steward at the scope of EVERY page the proposal would edit.

    **This is the first verdict in this lane that can be asked a per-PATH question, and it must
    be.** The other two kinds are anchored to no page — an entity proposal has no page yet, a
    parked capture never got one — so `is_steward(service, "")` is the only scope they could
    resolve, and it can only match the universal `"*"` key. A repair names the exact pages it would
    edit, and `ops/stewards.json` exists to DELEGATE zones: the universal question would let the
    general steward apply an edit inside a folder whose own steward never saw it, which is the
    delegation being silently undone by the one verdict that writes to those folders.

    `all(...)`, not `any(...)`: a contradiction repair edits both sides, so a proposal spanning two
    zones needs somebody who stewards both. That is not a deadlock — either steward may still
    REJECT it, and the pair can be proposed as two one-sided repairs.

    **There is no self-approval refusal here, and its absence is a decision** (ADR 039 D5). The
    `entity-proposal` rule exists because a human submitted that row and a second human has to
    agree; a repair proposal has no submitter at all — a nightly job derived it from the gardener's
    findings, and the model that wrote it approves nothing. The one steward IS the second party.
    Asking "did you file this?" of a machine-authored row would refuse nobody and imply a submitter
    that does not exist.

    An empty `target_paths` collapses to the universal scope. It cannot occur from the proposer
    (`store.insert_proposal` derives the column from the ops, and a proposal with no ops is refused
    before it is stored), so this is the fail-closed reading of a row that should not exist rather
    than a supported shape.

    **The map is read ONCE for the whole decision**, not once per path. Both halves of that matter:
    an authorization decision is made against one map, and N reads mean N maps, so a
    `ops/stewards.json` landing mid-decision could have one proposal approved against two different
    answers to the same question. The other half is that each read is a `git fetch` plus a file
    read, and an unauthorized caller could trigger one per op just by asking.
    """
    paths = [str(p) for p in (target_paths or ()) if str(p)]
    stewards = _stewards_snapshot(service)
    if not found or stewards is None or not service.identity:
        raise ReviewError(NOT_YOURS_TO_DECIDE)
    if not all(service.identity in resolve_stewards_for_scope(stewards, p)
               for p in (paths or [""])):
        raise ReviewError(NOT_YOURS_TO_DECIDE)


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


def _query_all_open_submissions(conn, *, submitted_by: str | None, limit: int) -> list[dict]:
    """Page through `query_submissions` in `MAX_LIST_LIMIT` steps: it silently clamps a larger
    request, and a doorbell seeing only the newest page drops the OLDEST parked items."""
    page_size = capture_queue.MAX_LIST_LIMIT
    rows: list[dict] = []
    offset = 0
    while len(rows) < limit:
        want = min(page_size, limit - len(rows))
        page = capture_queue.query_submissions(
            conn, submitter=submitted_by,
            statuses=[capture_schema.TRIAGE, capture_schema.NEEDS_INPUT], limit=want,
            offset=offset)
        rows.extend(page)
        if len(page) < want:
            break   # exhausted — fewer open rows exist than `limit` allows for
        offset += len(page)
    if len(rows) >= limit:
        # no silent caps: a caller sizing `limit` generously still learns when reality outgrew it
        log.warning(
            "review: open-submission paging hit its own limit (%d) for submitted_by=%r — more "
            "open triage/needs_input rows may exist and were not included this pass", limit,
            submitted_by)
    return rows


def _iso(value) -> str | None:
    """`capture.queue._iso`'s rule, spelled here rather than imported: it is private to that module
    and this is a two-line predicate, not a shared decision."""
    return value.isoformat() if isinstance(value, datetime) else None


def _repair_proposal_items(conn, *, limit: int) -> list[dict]:
    """The first `limit` pending `repair_proposals` rows, as review-inbox items.

    `limit` is not optional and has no default: this list is the one item kind a NIGHTLY JOB
    produces in bulk, and a caller that forgot to bound it would read the whole pending table into
    an MCP response.

    `ops_preview` is a COUNT and the set of op kinds, never the ops themselves: this list is a
    scan, and the ops carry page paths and a free-text `note`. What a steward has to read before
    approving is the `rationale` and the pages it would touch; the whole thing is one `review_queue`
    entry away in the console, and one `stigmergy-repair show <id>` away in a terminal.
    """
    return [{
        "kind": KIND_REPAIR_PROPOSAL, "id": str(row["id"]),
        # ISO, like every other item's: `repair.store` hands back the driver's `datetime` and
        # `mcp_server` serializes this whole structure with a plain `json.dumps`. `capture.queue`
        # converts on the way out for the same reason, which is why the other two kinds already
        # arrive as strings and nothing here noticed until a third writer joined them.
        "created_at": _iso(row.get("created_at")), "rationale": row.get("rationale", ""),
        "target_paths": list(row.get("target_paths") or ()),
        "ops_preview": {"count": len(row.get("ops") or ()),
                        "kinds": sorted({str(o.get(repair_schema.OP_KIND_KEY, ""))
                                         for o in (row.get("ops") or ())})},
        "model_id": row.get("model_id", ""),
    } for row in repair_store.pending_proposals(conn, limit=limit)]


def _collect_open_items(conn, *, submitted_by: str | None, limit: int) -> list[dict]:
    """The shared base under both wrappers: everything parking on a human, latest decision
    attached. `submitted_by=None` is the MANAGEMENT read, never exposed to an MCP caller directly.
    The first two kinds are disjoint by construction — `situations.classify` runs FIRST, and only a
    non-entity-situation row reaches the `parked-capture` branch.

    **`repair-proposal` is in the MANAGEMENT read only**, and the asymmetry is the honest reading of
    the ownership scope rather than an omission: a repair proposal has no submitter — a nightly job
    derived it from the gardener's findings, nobody asked for it — so there is no "own" for an
    ownership-scoped caller to be shown. Including it anyway would show every scoped caller every
    proposal, and a proposal names the PAGE PATHS it would edit: `acl.visible()` decides who may
    see that a page exists, and this list does not ask it. Fail closed; the steward guard on the
    decide leg is a separate question and answers it separately.
    """
    items: list[dict] = []
    if submitted_by is None:
        # `limit` bounds THIS read as well as the submissions one below. It was outside the bound
        # once, which made the caller's ceiling advisory over exactly the kind a cron can produce
        # a thousand of overnight.
        items += _repair_proposal_items(conn, limit=limit)

    rows = _query_all_open_submissions(conn, submitted_by=submitted_by, limit=limit)
    for row in rows:
        situation = situations.classify(row)
        if situation:
            items.append({
                "kind": KIND_ENTITY_PROPOSAL, "id": str(row["id"]),
                "submitted_by": row["submitted_by"], "situation": situation,
                "subject": situations.subject_of(row),
                # BOTH, for the reason `situations.subjects_of` states: `subject` is ONE display
                # string that joins several names with ", ", so a consumer acting on a name —
                # prefilling a mint form, running one command per name — must read `subjects` or
                # it will act on a joined compound that is not any of them.
                "subjects": situations.subjects_of(row),
                # The name a mint form may default to, decided ONCE (`situations`) rather than
                # re-derived from `subjects` by each door: both doors write the same irreversible
                # commit, so the two must never disagree about when a default is safe. `""` means
                # "no single string can be right here" — the surface lists `subjects` instead.
                "mint_name_prefill": situations.mint_name_prefill(row),
                "parked_age_ms": row.get("parked_age_ms"), "created_at": row.get("created_at"),
                # the doorbell's change token: a requeue re-parking into the SAME situation is
                # still a state change, not silence
                "attempts": row.get("attempts"),
            })
        else:
            report = row.get("report") or {}
            items.append({
                "kind": KIND_PARKED_CAPTURE, "id": str(row["id"]),
                "submitted_by": row["submitted_by"], "status": row["status"],
                "summary": report.get("summary", ""),
                "parked_age_ms": row.get("parked_age_ms"), "created_at": row.get("created_at"),
                "attempts": row.get("attempts"),
            })

    items = [_neutralize_leaves(item) for item in items]
    latest_by_item = decisions.latest_decisions(conn)
    for item in items:
        item["decision"] = latest_by_item.get((item["kind"], item["id"]))
    return items


def review_queue(service, *, limit: int = 50) -> dict:
    """The unified inbox over everything parking on a human. An unrestricted identity sees every
    item; a scoped one sees only items IT submitted, the ownership scope
    `BrainService.submissions` applies to the fast lane."""
    identity = service.identity
    unrestricted = service.unrestricted
    # A SCOPED caller with no identity would pass `submitted_by=None` — the MANAGEMENT scope —
    # while being labelled `scope: "own"`. Fail closed where the widening decision is made.
    if not unrestricted and not identity:
        raise ValueError("a scoped review queue needs a resolved identity — refusing to widen to "
                         "every identity's items")
    items = _collect_open_items(service.conn, submitted_by=None if unrestricted else identity,
                                limit=limit)
    return {"identity": identity, "scope": "all" if unrestricted else "own",
           "count": len(items), "items": items}


# The doorbell's own read — never an MCP tool: a caller-facing surface goes through
# `review_queue`'s ACL scoping instead.
DOORBELL_ITEM_LIMIT = 500


def items_for_doorbell(conn, *, limit: int = DOORBELL_ITEM_LIMIT) -> list[dict]:
    """Every open review item, system-wide and unscoped; `slack.doorbell` resolves the steward."""
    return _collect_open_items(conn, submitted_by=None, limit=limit)


# ── the doorbell's steward resolution ───────────────────────────────────────────────────────────
# `base_ref` FETCHES, and this runs inside an authorization check on a request — an unreachable
# remote must make the decision FAIL (closed, through `is_steward`'s own `except`) rather than
# stall it. The worker's own callers pass none; this one cannot.
STEWARDS_FETCH_TIMEOUT_S = 30


def load_stewards(repo: str, baked_path: str = "") -> dict:
    """`ops/stewards.json` — from the REPO at `origin/main`'s fresh tip wherever a checkout exists
    (never the working tree: a revoked steward must not resolve off a stale read), from the
    deploy-time snapshot at `baked_path` where none does. The ONE input `is_steward` reads too,
    so one map decides both who to ring and who may approve. An absent file on either road is an
    EMPTY map, never an error, and every decision downstream fails closed.
    """
    if repo:
        return base_inputs.load_stewards(
            repo, gitcmd.base_ref(repo, "main", timeout_s=STEWARDS_FETCH_TIMEOUT_S))
    return base_inputs.load_stewards_file(baked_path) if baked_path else {}


def resolve_stewards_for_scope(stewards_map: dict, scope_path: str) -> list[str]:
    """Longest-matching zone-path key wins; `"*"` is the fallback, never compared as a prefix, so
    `scope_path=""` can only match it.

    A key names a PATH, and the match is on a path BOUNDARY: the key itself, or the key followed by
    `/`. A bare `startswith` made the key `wiki/note` govern `wiki/notes/x.md` — a delegation for
    one folder silently deciding a different folder whose name it is a prefix of, and, being the
    longer key, beating the general steward to it. Keys are accepted with or without a trailing
    slash because `ops/stewards.json` is hand-written both ways.
    """
    best_key, best_len = None, -1
    for key in (stewards_map or {}):
        if key == "*":
            continue
        if scope_path and _covers(key, scope_path) and len(key) > best_len:
            best_key, best_len = key, len(key)
    if best_key is not None:
        return _as_list(stewards_map[best_key])
    return _as_list((stewards_map or {}).get("*"))


def _covers(key: str, scope_path: str) -> bool:
    """Does a stewards-map key govern this page path? The key exactly, or the folder it names."""
    return scope_path == key or scope_path.startswith(key.rstrip("/") + "/")


def _as_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value]
    return [str(value)]


def record_undeliverable(conn, *, event: str, item_ref: str, reason: str) -> None:
    """An undeliverable event is recorded, not swallowed; rides the `job_runs` writer."""
    capture_ops.record_job_run(conn, "steward-doorbell", status="error",
                               stats={"event": event, "item_ref": item_ref}, error=reason)


# ── review_decide() ────────────────────────────────────────────────────────────────────────────
def _row_for_item(service, item_id: str) -> tuple[int | None, dict | None]:
    """`(submission_id, row)` for a review-queue item id — `(None, None)` for a malformed one, the
    same "not found" a nonexistent id gets. Shared by both decide paths so neither can start
    reading the row on its own terms; what each does with a found row is its own rule."""
    submission_id = _parse_id(item_id)
    row = capture_queue.get_submission_trace(service.conn, submission_id) \
        if submission_id is not None else None
    return submission_id, row


def _recorded_message(kind: str, verdict: str, submission_id, actor: str, note: str = "") -> str:
    """The confirmation sentence, composed ONCE so every surface and every item kind relays the
    same one. `note` is passed only where the verdict's own rule requires one."""
    line = f"recorded: {verdict} on {kind} #{submission_id} by {actor}."
    if note:
        line += (f" Note: \"{note}\"\nNothing else happens automatically — this was not filed "
                f"as a page.")
    return line


def _already_decided_suffix(conn, item_kind: str, submission_id) -> str:
    """`" — already decided: …"` for an item the ledger already holds a verdict on, `""` otherwise.

    **Only ever composed AFTER an authorization guard has passed.** `review_decisions` is not a
    caller-visible surface: it names WHO decided an identity and through which door. Appended to
    `NOT_YOURS_TO_DECIDE` it would turn one anonymous sentence into an oracle answering four
    questions a refused caller may not ask — that the id exists, that it was decided, by whom, and
    where. That is why this is called at the two TRANSLATION sites inside the decide paths, each
    of which runs strictly after its own `_guard_*`, and never from a wrapper around them.

    The timestamp is converted to UTC before it is stamped `Z`: `created_at` comes back in the
    session's timezone, and a local time labelled `Z` is worse than no time at all when the point
    is to work out which of two decisions came first. `source` is `""` on every row written before
    the column existed (the ledger is never migrated), and those rows still have to render.
    """
    decision = decisions.latest_decision_for(conn, item_kind=item_kind, item_id=str(submission_id))
    if not decision:
        return ""
    when = decision["created_at"]
    if when.tzinfo is not None:
        when = when.astimezone(UTC)
    return (f" — already decided: {decision['verdict']} by {decision['actor']} via "
            f"{decision['source'] or 'an unrecorded door'} at {when:%Y-%m-%d %H:%M}Z")


# The note scan is a subprocess on the decide path, like the stewards fetch above — a scanner
# that never returns must fail the decide (as a config fault), not pin the request. The note
# itself is length-clamped upstream, so the budget is generous for the input it can ever see.
NOTE_SCAN_TIMEOUT_S = 30


def _refuse_secret_note(notes: str) -> None:
    """The secrets scan over a steward note, here at the CALL SITE because `stigmergy.capture` may
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


def _decide_parked_capture(service, item_id: str, verdict: str, notes: str, actor: str, *,
                           source: str) -> dict:
    """Deliberately NOT the generic verdict vocabulary: `verdict` here IS one of
    `capture.dispositions.DISPOSITIONS`, stored verbatim, so a button label can never disagree
    with the recorded verdict. Authorization runs before anything else, so a nonexistent id is
    refused by the SAME sentence an unauthorized one is.

    A `CaptureError` out of the disposition is this kind's staleness refusal — translated into
    `ReviewError` and enriched with the decision that beat it, both of which may only happen after
    `_guard_parked_capture_decision` has cleared the caller."""
    submission_id, row = _row_for_item(service, item_id)
    # An entity situation is NOT a parked capture, enforced HERE: without this the caller's
    # `item_kind` picks the authorization rule, and an entity proposal's own submitter could route
    # it into this kind's looser guard. NOT FOUND, not a distinct refusal, which would leak.
    if row is not None and situations.classify(row):
        row = None
    _guard_parked_capture_decision(service, found=row is not None,
                                   submitted_by=(row or {}).get("submitted_by", ""))

    if verdict not in dispositions.DISPOSITIONS:
        raise ReviewError(
            f"a parked-capture decision must be one of {dispositions.DISPOSITIONS} "
            f"(review_decide does not reuse the generic approve/reject/request_changes "
            f"vocabulary for this item kind — see capture.dispositions)")
    _refuse_secret_note(notes)
    if verdict in (dispositions.RESOLVE, dispositions.REJECT) and not notes:
        raise ReviewError("resolve requires a note — what did you do with the material?"
                          if verdict == dispositions.RESOLVE else "reject requires a reason")
    try:
        # The disposition's own SQL state guard, which is the ONLY place a lost race is caught for
        # this kind — there is no pre-flight read here, unlike the entity path's
        # `require_situation`. Its `QueueStateError` used to leave this function untranslated,
        # which broke this module's own rule that an exception type from below never escapes as
        # itself, and told a steward nothing about WHO had already decided the row.
        if verdict == dispositions.REQUEUE:
            result = dispositions.requeue(service.conn, submission_id, actor=actor, note=notes)
        elif verdict == dispositions.RESOLVE:
            result = dispositions.resolve(service.conn, submission_id, actor=actor, note=notes)
        else:
            result = dispositions.reject(service.conn, submission_id, actor=actor, reason=notes)
    except CaptureError as ex:
        raise ReviewError(
            f"{ex}{_already_decided_suffix(service.conn, KIND_PARKED_CAPTURE, submission_id)}"
        ) from ex
    # `str(submission_id)`, never the raw `item_id`: `_parse_id` accepts " 204 "/"007", and a
    # raw-spelling ledger row could never join back to the item it decided.
    record_decision(service.conn, item_kind=KIND_PARKED_CAPTURE, item_id=str(submission_id),
                    verdict=verdict, actor=actor, source=source, notes=notes)
    message = _recorded_message(KIND_PARKED_CAPTURE, verdict, submission_id, actor,
                                note=notes if verdict == dispositions.RESOLVE else "")
    return {"recorded": verdict, "item_kind": KIND_PARKED_CAPTURE, "item_id": str(submission_id),
           "actor": actor, "result": result, "message": message}


def _decide_entity_proposal(service, item_id: str, verdict: str, notes: str, actor: str, *,
                            source: str, name: str = "", entity_id: str = "",
                            entity_type: str = "", aliases=None, role: str = "",
                            requeue: bool = False) -> dict:
    """Authorized before anything else runs: steward required, self-approval refused. `approve`
    mints. `name`/`entity_type` are validated only AFTER authorization, so a refused caller learns
    nothing about what a mint would need. `entity_id` prefills to `name`'s slug, and
    `entities.mint.mint` refuses one that is not actually that slug."""
    submission_id, row = _row_for_item(service, item_id)
    _guard_governance_decision(service, found=row is not None,
                               submitted_by=(row or {}).get("submitted_by", ""),
                               scope_path="", verdict=verdict)

    if verdict not in GENERIC_VERDICTS or verdict == REQUEST_CHANGES:
        raise ReviewError(
            f"an entity proposal takes {APPROVE!r} or {REJECT!r} only — there is nothing to "
            f"'request changes' to: either the name resolves to an identity worth minting or it "
            f"does not")
    _refuse_secret_note(notes)
    # Mapped HERE, like every other `EntityError` this module lets through: an exception type
    # from `stigmergy.entities` must never reach a caller, because `stigmergy.slack` is barred
    # from importing it and could only catch it generically — as an unanticipated fault whose
    # text may not be shown. `ReviewError` is a `CaptureError`, so both surfaces already echo it.
    # The translation is also where the "a second door got here first" clause is appended, because
    # this is the first point in this path at which the caller is known to be authorized.
    try:
        situations.require_situation(service.conn, submission_id, action=verdict)
    except EntityError as ex:
        raise ReviewError(
            f"{ex}{_already_decided_suffix(service.conn, KIND_ENTITY_PROPOSAL, submission_id)}"
        ) from ex
    if verdict == REJECT:
        if not notes:
            raise ReviewError("reject requires a reason")
        dispositions.reject(service.conn, submission_id, actor=actor, reason=notes)
        message = _recorded_message(KIND_ENTITY_PROPOSAL, verdict, submission_id, actor)
        record_decision(service.conn, item_kind=KIND_ENTITY_PROPOSAL, item_id=str(submission_id),
                        verdict=verdict, actor=actor, source=source, notes=notes)
        return {"recorded": verdict, "item_kind": KIND_ENTITY_PROPOSAL,
               "item_id": str(submission_id),
               "actor": actor, "message": message}

    clean_name = " ".join(str(name or "").split())
    clean_type = str(entity_type or "").strip().lower()
    missing = [field for field, value in (("name", clean_name), ("entity_type", clean_type))
              if not value]
    if missing:
        raise ReviewError(
            f"approving an entity proposal mints it (ADR 030) — missing {' and '.join(missing)}: "
            f"pass name (the entity's page title) and entity_type (one of "
            f"{', '.join(ENTITY_TYPES)}) with the decision. entity_id defaults to name's slug; "
            f"aliases and role are optional; requeue re-files the originating capture after the "
            f"push lands")
    if clean_type not in ENTITY_TYPES:
        raise ReviewError(f"entity_type {clean_type!r} is not one of {', '.join(ENTITY_TYPES)}")
    _check_len("name", clean_name)
    _check_len("role", role or "")
    alias_list = _alias_list(aliases)
    for alias in alias_list:
        _check_len("alias", alias)
    resolved_id = str(entity_id or "").strip() or canonical_id_for(clean_name)

    minted = _mint_entity_proposal(
        service, submission_id=submission_id, entity_id=resolved_id, name=clean_name,
        entity_type=clean_type, aliases=alias_list, role=role or "", notes=notes,
        approved_by=actor, source=source, requeue=requeue)

    return {"recorded": verdict, "item_kind": KIND_ENTITY_PROPOSAL,
               "item_id": str(submission_id),
           "actor": actor, "minted": True, "entity_id": minted["entity_id"],
           "name": minted["name"], "commit": minted["commit"],
           "requeued": minted["requeued"]}


def _alias_list(aliases) -> list[str]:
    """`aliases` as a JSON list or one comma-separated string, mirroring `entities.cli`."""
    if not aliases:
        return []
    values = aliases if isinstance(aliases, (list, tuple)) else [aliases]
    out = []
    for value in values:
        out += [part.strip() for part in str(value).split(",") if part.strip()]
    return out


# The knowledge repo's default branch; every server-driven write through this module — a mint and
# an approved repair alike — targets it.
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
# the same race, and a steward reading two different explanations of one condition would reasonably
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
    """Approve ONE repair proposal and apply it, in the one order that is correct — record the
    steward's verdict, apply through the governed door, then write the governance ledger row.

    Both doors that approve a repair run THIS: the review lane through `_decide_repair`, the admin
    console through `admin.service.repair_approve`. It is `mint_and_record_approval`'s lesson
    applied to the second irreversible verdict this module owns — two copies of an ordering rule
    are two places for it to be reordered, and the reordering is invisible from either door's end
    state.

    The order, and why each step is where it is:

    1. `mark_decided(approved)` FIRST, and it is a conditional UPDATE (`WHERE status = 'pending'`).
       That is what makes a second Approve lose rather than clone: the loser sees zero rows and is
       told so, and the winner holds a row nothing else can act on for the whole of the clone. No
       lease is needed anywhere below because of this one line.
    2. `remote.apply_approved` — clone, re-validate, gate, cross-check, commit, push, and record
       the outcome (`applied` with the sha, or `failed` with the sentence). A `RepairError` out of
       it has ALREADY been recorded as failed, and the approved status is deliberately NOT
       restored: a silent revert to pending would hide that a gate refused.
    3. The ledger row LAST, after the push, exactly as the mint sequence writes its own — a row
       claiming a decision whose commit never landed is worse than a missing row, and the commit is
       the irreversible half.

    It takes NO authorization argument, on purpose and by the same rule ADR 030 D2 states for the
    mint: authorization is per-surface (a resolved steward here, the operator token there), so the
    CALLER SET is closed and pinned in `tests/test_architecture.py`.

    `apply_approved` is reached as a MODULE ATTRIBUTE so it stays monkeypatchable, the same seam
    `entities_remote.mint_via_clone` keeps one door over.

    `notes` reaches BOTH writes, exactly as `reject_repair_and_record` already does with its
    reason — it is the only record of why a repair was worth applying, and it used to be dropped on
    approve while being kept on reject. It is `mint_and_record_approval`'s asymmetry too: the review
    lane carries the steward's cleaned note, the console has no note field and passes nothing. It
    goes VERBATIM into an append-only table, so a caller supplying a non-empty one must already
    have run `_refuse_secret_note` — `review_decide` does, before either branch.
    """
    if not repo_url:
        raise ReviewError(REPAIR_REPO_UNCONFIGURED)
    if not repair_store.mark_decided(conn, proposal["id"], status=repair_schema.STATUS_APPROVED,
                                     decided_by=actor, notes=notes):
        raise ReviewError(_LOST_THE_RACE)
    result = repair_remote.apply_approved(
        conn, repo_url, _KNOWLEDGE_BRANCH, os.environ, proposal=proposal, approved_by=actor)
    # The apply's own account of what it did, projected through a NAMED key list rather than
    # copied whole: the result is a dict a lower layer composes, and a ledger row is append-only
    # governance history. Keys the apply did not produce are simply absent — a `delete` records
    # what it removed and how much it rewrote, and an additive repair gains no empty columns
    # teaching a reader that this loop deletes things.
    record_decision(conn, item_kind=KIND_REPAIR_PROPOSAL, item_id=str(proposal["id"]),
                    verdict=APPROVE, actor=actor, source=source, notes=notes,
                    extra={key: result[key] for key in repair_remote.LEDGER_RESULT_KEYS
                           if key in result})
    return {"applied": True, "commit": result["commit"], "paths": result["paths"]}


def reject_repair_and_record(conn, *, proposal: dict, actor: str, source: str,
                             reason: str) -> dict:
    """Decline ONE repair proposal — the same two writes, in the same order, for both doors.

    A REJECTED row is the dismissal memory (`repair.schema`): the proposer skips a content key that
    has any prior row, so "reviewed and declined" is a durable fact and a steward who says no once
    is not asked again tomorrow. That is why the reason is stored on the proposal AND in the
    ledger, and why this is shared rather than spelled twice: a door that wrote only the ledger row
    would leave the proposer re-asking every night.
    """
    if not repair_store.mark_decided(conn, proposal["id"], status=repair_schema.STATUS_REJECTED,
                                     decided_by=actor, notes=reason):
        raise ReviewError(_LOST_THE_RACE)
    record_decision(conn, item_kind=KIND_REPAIR_PROPOSAL, item_id=str(proposal["id"]),
                    verdict=REJECT, actor=actor, source=source, notes=reason)
    return {"rejected": True}


def _decide_repair(service, item_id: str, verdict: str, notes: str, actor: str, *,
                   source: str) -> dict:
    """`approve` applies the repair; `reject` records the dismissal. Authorization runs before
    anything else, so a nonexistent id is refused by the same sentence an unauthorized one is.

    There is no `request_changes`: a proposal is a concrete set of edits, and the thing to change
    about one is which edits it contains — which is a new proposal, not an amendment to this one.
    The proposer will make it, because a rejected key is skipped and a re-derived DIFFERENT repair
    is a different key.
    """
    proposal = _repair_proposal_or_refuse(service.conn, item_id)
    _guard_repair_decision(service, found=True, target_paths=proposal["target_paths"])

    if verdict not in (APPROVE, REJECT):
        raise ReviewError(
            f"a repair proposal takes {APPROVE!r} or {REJECT!r} only — there is nothing to "
            f"'request changes' to: a proposal IS its edits, so a different set of edits is a "
            f"different proposal")
    _refuse_secret_note(notes)
    if verdict == REJECT:
        if not notes:
            raise ReviewError("reject requires a reason")
        result = reject_repair_and_record(service.conn, proposal=proposal, actor=actor,
                                          source=source, reason=notes)
        return {"recorded": verdict, "item_kind": KIND_REPAIR_PROPOSAL,
                "item_id": str(proposal["id"]), "actor": actor,
                "message": _recorded_message(KIND_REPAIR_PROPOSAL, verdict, proposal["id"], actor),
                **result}
    try:
        result = apply_repair_and_record(
            service.conn, repo_url=service.settings.librarian_repo_url, proposal=proposal,
            actor=actor, source=source, notes=notes)
    except RepairError as ex:
        # Mapped HERE, under the rule this whole module holds: an exception type from below never
        # leaves as itself, because `stigmergy.slack` is barred from importing it and could only
        # catch it as an unanticipated fault whose text may not be shown. Every sentence
        # `repair.remote` raises is written for a steward (its own module docstring), so `str(ex)`
        # crosses verbatim — and the row it names is already recorded as `failed`.
        raise ReviewError(str(ex)) from ex
    return {"recorded": verdict, "item_kind": KIND_REPAIR_PROPOSAL,
            "item_id": str(proposal["id"]), "actor": actor, **result}


def mint_and_record_approval(conn, *, repo_url: str, submission_id: int, entity_id: str,
                             name: str, entity_type: str, aliases: list[str], role: str,
                             actor: str, source: str, notes: str = "",
                             requeue: bool = False) -> dict:
    """The mint sequence itself, in the ONE order that is correct — mint through the governed
    door, write the governance ledger row, then (only if asked) requeue the capture.

    Both SERVER-SIDE doors that mint run THIS: MCP/Slack through `_mint_entity_proposal` below,
    the admin console through `admin.service.entity_approve`. Two copies of an ordering rule are
    two places for it to be reordered, and the reordering is invisible from either door's end
    state. There IS a third door, and it is the copy: `stigmergy-entities approve` mints from the
    steward's own clone and runs its own spelling of the same order, because `stigmergy.entities`
    cannot import `stigmergy.server`.

    All three doors write the ledger row (the writer is `capture.decisions`), so `review_decisions`
    answers "who approved this identity" for every door.

    It deliberately covers three steps and no more. The pending-situation guard
    (`situations.require_situation`) stays at each call site because the two doors run it at
    different points in their own argument validation, and one shared answer would silently change
    the other door's refusal for a caller who is wrong in both ways at once.

    `stigmergy.entities` exceptions leave here UNTRANSLATED, on purpose: each door maps them
    itself and maps them differently. `_mint_entity_proposal` turns them into this package's
    vocabulary; the console lets the library's own class reach `admin.service._mutate`, which
    records that class name in `admin_actions` before turning it into an `AdminRefused`. A
    translation here would rename what the console's bookkeeping captures precisely.

    `source` is a parameter for the same reason authorization is not one: this sequence is shared
    by doors that are not each other, and each has to name itself. It is passed straight to
    `record_decision`, which refuses any spelling outside `DECISION_SOURCES`.

    `notes` is the doors' other asymmetry, and a parameter for that reason: the review lane
    carries the steward's cleaned note into the ledger row, the console has no note field and
    writes `''`. It goes VERBATIM into an append-only table that cannot be migrated afterwards,
    and nothing here scans it — a caller passing a NON-EMPTY note must already have run
    `_refuse_secret_note` (or an equivalent secrets scan) upstream. `review_decide` does, far
    before the mint; the console passes `''` and so has nothing to scan.

    `mint_via_clone` is reached as a MODULE ATTRIBUTE so it stays monkeypatchable.
    """
    mint_result = entities_remote.mint_via_clone(
        repo_url, _KNOWLEDGE_BRANCH, os.environ,
        entity_id=entity_id, name=name, entity_type=entity_type, aliases=aliases, role=role,
        today=date.today().isoformat(), submission_id=submission_id, approved_by=actor)
    record_decision(conn, item_kind=KIND_ENTITY_PROPOSAL, item_id=str(submission_id),
                    verdict=APPROVE, actor=actor, source=source, notes=notes,
                    extra={"entity_id": mint_result["entity_id"], "commit": mint_result["commit"]})
    requeued = None
    if requeue:
        # AFTER the push, never before — the CLI's own correctness property (`entities.cli`'s
        # module docstring), restated by every door that mints: a requeue that ran first would
        # hand the librarian a capture whose entity is not yet on the remote it fetches from, and
        # the capture would park a second time. The note text is the operator-facing trace of why
        # a capture came back; every door emits the same sentence.
        requeued = dispositions.requeue(
            conn, submission_id, actor=actor,
            note=f"entity {mint_result['entity_id']} approved and pushed "
                 f"({mint_result['commit'][:12]})")
    return {**mint_result, "requeued": bool(requeued)}


def _mint_entity_proposal(service, *, submission_id: int, entity_id: str, name: str,
                          entity_type: str, aliases: list[str], role: str, notes: str,
                          approved_by: str, source: str, requeue: bool) -> dict:
    """This door's half of `mint_and_record_approval`: the shared sequence with THIS package's
    translation wrapped around it, under the rule this whole module holds — an `entities`
    exception type is translated where this package meets it, never allowed to leave
    (`_decide_entity_proposal`'s pre-mint guard does the same for its own).

    The `try` covers the WHOLE shared sequence, not just the mint. Inert today (`CaptureError` and
    `EntityError` are disjoint), but any `entities` call added to `mint_and_record_approval` starts
    being translated into `ReviewError` here.
    """
    try:
        return mint_and_record_approval(
            service.conn, repo_url=service.settings.librarian_repo_url,
            submission_id=submission_id, entity_id=entity_id, name=name, entity_type=entity_type,
            aliases=aliases, role=role, actor=approved_by, source=source, notes=notes,
            requeue=requeue)
    except EntityCapabilityUnavailableError as ex:
        raise CapabilityUnavailableError(str(ex)) from ex
    except EntityError as ex:
        raise ReviewError(str(ex)) from ex


def review_decide(service, *, item_kind: str, item_id: str, verdict: str, source: str,
                  notes: str = "", name: str = "", entity_id: str = "", entity_type: str = "",
                  aliases=None, role: str = "", requeue: bool = False) -> dict:
    """Record a verdict, attributed to the caller's RESOLVED identity — never an argument.

    `reject` and every `parked-capture` verdict are Postgres only. Approving an `entity-proposal`
    or a `repair-proposal` are the two paths that touch git, one App-authored commit each through a
    governed door: the entity verdict alone needs `name` and `entity_type`, and `requeue` re-files
    the originating capture AFTER the push lands; the repair verdict needs nothing but the id,
    because the proposal already IS the change.

    `entity-proposal` requires a STEWARD and refuses self-approval; `parked-capture` accepts the
    row's own submitter OR a steward; `repair-proposal` requires a steward for EVERY page it would
    edit. "Not authorized" and "does not exist" are the SAME sentence.

    `source` names the DOOR, not the caller — the caller is `service.identity`. It is required and
    undefaulted all the way down: this function serves two transports (an MCP client and a Slack
    card), and a default would silently attribute one of them to the other on the day a third
    arrives.
    """
    identity = service.identity
    if not identity:
        raise ReviewError("no resolved identity — a decision cannot be attributed unattributed")
    if item_kind not in ITEM_KINDS:
        raise ReviewError(f"unknown item kind {item_kind!r} (one of {', '.join(ITEM_KINDS)})")
    _check_len("notes", notes or "")
    clean_notes = dispositions.clean(notes)

    if item_kind == KIND_PARKED_CAPTURE:
        return _decide_parked_capture(service, item_id, verdict, clean_notes, identity,
                                      source=source)
    if item_kind == KIND_REPAIR_PROPOSAL:
        return _decide_repair(service, item_id, verdict, clean_notes, identity, source=source)
    return _decide_entity_proposal(service, item_id, verdict, clean_notes, identity, source=source,
                                   name=name, entity_id=entity_id, entity_type=entity_type,
                                   aliases=aliases, role=role, requeue=requeue)


def review_decide_safe(service, *, item_kind: str, item_id: str, verdict: str, source: str,
                       notes: str = "", name: str = "", entity_id: str = "", entity_type: str = "",
                       aliases=None, role: str = "", requeue: bool = False) -> dict:
    """`BrainService.review_decide` with a CLEAN refusal returned as `{"error": str}` rather than
    raised — for `stigmergy.slack`, barred from importing the exception types. An UNANTICIPATED
    exception still propagates, and its `str(ex)` must never be shown."""
    try:
        return service.review_decide(item_kind, item_id, verdict, source=source, notes=notes,
                                     name=name, entity_id=entity_id, entity_type=entity_type,
                                     aliases=aliases, role=role, requeue=requeue)
    except (CaptureError, CapabilityUnavailableError) as ex:
        return {"error": str(ex)}
