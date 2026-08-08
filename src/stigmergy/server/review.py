"""The review lane — the ONE inbox where work parks on a human, and the record of what they decided.

There is no PR ceremony here, deliberately: `status` is a maturity axis, not a court. What this
module holds is the narrower thing that genuinely needs a person — *humans decide what only humans
can*:

- **`review_queue`** — the unified, ACL-scoped inbox over two kinds: `entity-proposal` (a
  capture that wants an entity minted) and `parked-capture` (a capture waiting on a human answer).
- **`review_decide`** — a verdict, attributed to the caller's RESOLVED identity, into the
  append-only `review_decisions` record. `reject` and every `parked-capture` verdict are Postgres
  only, categorically — nothing on those paths ever touches git. Approving an `entity-proposal` is
  the one exception (ADR 030): it mints through the governed door
  (`entities.remote.mint_via_clone` -> the same `entities.mint.mint` the CLI's `approve`/`create`
  call), exactly ONE commit, authored as the librarian App with an `Approved-by: <caller>` trailer.
- **the doorbell's read side** — `items_for_doorbell` (unscoped, management-shaped),
  `load_stewards` / `resolve_stewards_for_scope`, and
  `record_undeliverable` (a notification nobody could receive is a `job_runs` row, never silence).

The authorization rule is why `_guard_governance_decision` exists: an `entity-proposal` needs a
STEWARD, a `parked-capture` needs the steward OR the submitter who was asked, and neither may be
satisfied by an unattributed caller. It runs FIRST, before any git work and before the metadata a
mint needs is even inspected — a governance refusal must never become more specific once a caller
has failed it (see `NOT_YOURS_TO_DECIDE`'s own docstring).
"""
import logging
import os
from datetime import date

from psycopg.types.json import Jsonb

from stigmergy.capture import dispositions
from stigmergy.capture import ops as capture_ops
from stigmergy.capture import queue as capture_queue
from stigmergy.capture import schema as capture_schema
from stigmergy.capture.errors import CaptureError
from stigmergy.capture.schema import startup_ddl_lock
from stigmergy.entities import remote as entities_remote
from stigmergy.entities import situations
from stigmergy.entities.errors import CapabilityUnavailableError as EntityCapabilityUnavailableError
from stigmergy.entities.errors import EntityError
from stigmergy.entities.generator import ENTITY_TYPES, canonical_id_for
from stigmergy.librarian import base_inputs, gates, gitcmd
from stigmergy.review_kinds import (
    ITEM_KINDS,
    KIND_ENTITY_PROPOSAL,
    KIND_PARKED_CAPTURE,
)
from stigmergy.server.errors import CapabilityUnavailableError

log = logging.getLogger(__name__)


def _neutralize(text: str) -> str:
    """`server.service.neutralize_fence`, imported lazily to avoid a module-load cycle:
    `service.py` imports THIS module (to mount `propose`/`review_queue`/`review_decide` on
    `BrainService`), so this module must never import `service` at module scope. By the time any
    function here actually RUNS, both modules are fully loaded, so a deferred import costs
    nothing — the same pattern `mcp_server.py`'s `ask` closure already uses for `stigmergy.answer`.
    """
    from stigmergy.server.service import neutralize_fence
    return neutralize_fence(text or "")


def _check_len(name: str, value: str) -> None:
    """`server.service.check_arg_length`, lazily imported for the same module-cycle reason
    `_neutralize` is. `review_decide` is an entry point on this seam that must bound its arguments
    before doing real work with them — without this, a ~1 MiB `notes` string reaches
    `dispositions.clean` or a gitleaks scan unbounded."""
    from stigmergy.server.service import check_arg_length
    check_arg_length(name, value or "")

# One append-only record for every verdict on every item kind. No code path here ever UPDATEs or
# DELETEs a row, so "a second decision on the same item does not overwrite the first" holds by
# construction, not by a rule anyone has to remember to keep.
_REVIEW_DECISIONS_DDL = """
CREATE TABLE IF NOT EXISTS review_decisions (
    id BIGSERIAL PRIMARY KEY,
    item_kind TEXT NOT NULL,
    item_id TEXT NOT NULL,
    verdict TEXT NOT NULL,
    actor TEXT NOT NULL,
    notes TEXT NOT NULL DEFAULT '',
    extra JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""
_REVIEW_DECISIONS_INDEX = (
    "CREATE INDEX IF NOT EXISTS review_decisions_item_idx ON review_decisions (item_kind, item_id)"
)

# `review_decisions` is the one table this module owns.
_ALL_DDL = (_REVIEW_DECISIONS_DDL, _REVIEW_DECISIONS_INDEX)


def ensure_review_schema(conn) -> None:
    """Idempotent DDL for the review lane's one table — safe on every startup, from two processes
    at once (same `startup_ddl_lock` `capture.schema.ensure_capture_schema` uses)."""
    with startup_ddl_lock(conn) as cur:
        for statement in _ALL_DDL:
            cur.execute(statement)


# ── item kinds ───────────────────────────────────────────────────────────────────────────────
# Re-exported from `stigmergy.review_kinds`, the dependency-free module `stigmergy.slack` also reads
# them from. ONE definition: a server that restated these literals beside it would need a
# drift-guard test to stay honest, and the shared module removes the need for one.

# `review_decide`'s verdict vocabulary. Uniform across kinds EXCEPT `parked-capture`, which keeps
# `capture.dispositions`' own three verbs instead (documented deviation — see `review_decide`'s
# docstring for why forcing the generic three onto that kind would be a false 1:1 mapping: there
# is no honest `approve` equivalent of a `resolve` that carries a REQUIRED note).
APPROVE, REJECT, REQUEST_CHANGES = "approve", "reject", "request_changes"
GENERIC_VERDICTS = (APPROVE, REJECT, REQUEST_CHANGES)

class ReviewError(CaptureError):
    """A clean, caller-facing refusal from the review lane — same posture as `CaptureError`
    (echoed verbatim over the wire; never a stack trace, never git/DB internals)."""


# ── one authorization predicate for the whole review_decide surface ──────────────────────────
# Checking only that AN identity resolved and that `item_kind` was known is not enough: it
# consults neither `ops/stewards.json` (the map that decides who may act, not merely who to ring a
# bell for) nor item ownership. A reader (`review_queue`) scoped by ownership while the MUTATOR is
# not is the shape of every IDOR ever written.
#
# **Every unauthorized refusal, and the nonexistent-id refusal, is ONE byte-identical sentence**
# — the SAME `service.NO_REPLY_WAITING` pattern `BrainService._reply` already uses for
# `brain_reply`: a nonexistent item, an item belonging to somebody else, and an item belonging to
# somebody else the caller is not the steward of are indistinguishable from the outside. Three
# distinct refusals from `situations.require_situation`/`dispositions` (does not exist / wrong
# state / wrong kind) are real and useful to an AUTHORIZED caller (a steward can already see a
# row's true state via `review_queue`), so they still surface once this predicate has cleared —
# they must simply never be the FIRST thing a caller not authorized to read the row learns.
NOT_YOURS_TO_DECIDE = "there is nothing for you to decide at that id"

# Governance requires a SECOND human: the proposer of an entity proposal may never be the one who
# approves it, even when they are also the resolved steward for the scope. The message says what
# to do rather than merely refusing — the case this correctly refuses is a steward approving their
# own proposal, and the fix is "ask another steward", never weakening the rule.
SELF_APPROVAL_REFUSED = (
    "you filed this — approving it needs a second, different steward. Ask another steward to "
    "review it; you may still record reject or request_changes on your own submission yourself")


def _is_steward(service, scope_path: str) -> bool:
    """Is the caller's resolved identity a steward for `scope_path`, per `ops/stewards.json` read
    fresh at the base commit (`load_stewards`) like every other governed input? `False` — never an
    exception — when this server has neither a checkout nor a baked snapshot to resolve against:
    an authority nothing can establish is not one to grant, whoever is asking."""
    repo = getattr(service.settings, "knowledge_repo", "") or ""
    baked = getattr(service.settings, "stewards_path", "") or ""
    if not repo and not baked:
        return False
    stewards = load_stewards(repo, baked)
    return bool(service.identity) and service.identity in resolve_stewards_for_scope(
        stewards, scope_path)


def _guard_governance_decision(service, *, found: bool, submitted_by: str, scope_path: str,
                               verdict: str) -> None:
    """The shared authorization gate for a governance decision (`entity-proposal`): steward
    required, self-approval refused. `found=False` (the row does not exist, or the id did not even
    parse) and "not a steward" collapse onto the SAME `NOT_YOURS_TO_DECIDE` sentence — an
    existence oracle is exactly what separately-worded refusals build."""
    if not found or not _is_steward(service, scope_path):
        raise ReviewError(NOT_YOURS_TO_DECIDE)
    if verdict == APPROVE and submitted_by and service.identity == submitted_by:
        raise ReviewError(SELF_APPROVAL_REFUSED)


def _guard_parked_capture_decision(service, *, found: bool, submitted_by: str) -> None:
    """`parked-capture`'s own, looser rule: the row's own submitter, OR a steward — no
    self-approval refusal, because there is no "approve" on this kind to begin with (its verdicts
    are `capture.dispositions`' own three verbs, and a submitter disposing of their own capture is
    the ordinary case, not a governance bypass)."""
    if not found:
        raise ReviewError(NOT_YOURS_TO_DECIDE)
    if service.identity and service.identity == submitted_by:
        return
    if _is_steward(service, ""):   # a parked capture has no page path yet: universal scope only
        return
    raise ReviewError(NOT_YOURS_TO_DECIDE)


def _parse_id(item_id: str) -> int | None:
    """`int(item_id)`, or `None` for anything that is not one — never a raw `ValueError` escaping
    to a caller: a malformed id is exactly as "not found" as a nonexistent one, and must read that
    way rather than as an unhandled exception."""
    try:
        return int(item_id)
    except (TypeError, ValueError):
        return None


# ── neutralize at the boundary, not field by field ───────────────────────────────────────────────
def _neutralize_leaves(value, depth: int = 0):
    """`_neutralize` (`service.neutralize_fence`) over every STRING LEAF of a structure, mirroring
    `service._neutralize_report`'s exact recursion (str/dict/list, depth-bounded) so the boundary
    this module has — each item dict leaving `_collect_open_items` — holds ONE rule instead of a
    scatter of separately-decided per-field calls. That scatter reliably misses a field: a `path`
    left raw while the `title` immediately beside it was neutralized is the exact shape of the
    defect this replaces, and every field added to the dict later inherits the protection for free.

    Depth-bounded with the same constant the audit shaper uses (`server.service.MAX_AUDIT_DEPTH`),
    lazily imported for the same module-cycle reason `_neutralize` already is.
    """
    from stigmergy.server.service import MAX_AUDIT_DEPTH
    if depth > MAX_AUDIT_DEPTH:
        return value
    if isinstance(value, str):
        return _neutralize(value)
    if isinstance(value, dict):
        return {str(k): _neutralize_leaves(v, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_neutralize_leaves(v, depth + 1) for v in value]
    return value


def _latest_decisions(conn) -> dict[tuple[str, str], dict]:
    """The most recent decision per `(item_kind, item_id)` — a rendering convenience over the
    append-only record: a reader must be able to tell an item already has a decision on it, even
    though a second one may still land beside the first."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT ON (item_kind, item_id) item_kind, item_id, verdict, actor, "
            "created_at FROM review_decisions ORDER BY item_kind, item_id, created_at DESC")
        return {(kind, item_id): {"verdict": verdict, "actor": actor, "created_at": created_at}
                for kind, item_id, verdict, actor, created_at in cur.fetchall()}


def _query_all_open_submissions(conn, *, submitted_by: str | None, limit: int) -> list[dict]:
    """Page through `capture_queue.query_submissions` in `capture_queue.MAX_LIST_LIMIT`-sized
    steps rather than asking it for `limit` in one call.

    That function silently clamps any request above its own page-size ceiling (200) — so
    `items_for_doorbell`'s own `limit=500` (`DOORBELL_ITEM_LIMIT`) returned only the NEWEST 200
    open `triage`/`needs_input` rows, and with 201+ such rows the rest never reached the doorbell
    at all. That is precisely backwards for a doorbell: the OLDEST parked items are the ones a
    doorbell exists for, and they were the ones silently dropped. Pages newest-first (the query's
    own unchanged order) via `offset` until `limit` rows are collected or a page comes back short
    (the table is exhausted) — `review_queue`'s own small default `limit` (50, well under one
    page) still resolves in a single call, unaffected."""
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
        # No silent caps: more open rows may exist beyond what even this many pages
        # collected. A caller sizing `limit` generously (the doorbell's own 500) should still know
        # when reality outgrew it, rather than quietly serving a truncated list forever.
        log.warning(
            "review: open-submission paging hit its own limit (%d) for submitted_by=%r — more "
            "open triage/needs_input rows may exist and were not included this pass", limit,
            submitted_by)
    return rows


def _collect_open_items(conn, *, submitted_by: str | None, limit: int) -> list[dict]:
    """The shared base every review-item listing filters afterward: everything that currently
    parks on a human, across both kinds (`entity-proposal`, `parked-capture`), with the latest
    decision (if any) attached. `submitted_by=None` is the MANAGEMENT read — every item,
    regardless of who filed it — and is never exposed to an MCP caller directly; `review_queue`
    below is the OPERATIONAL wrapper that narrows it to one identity's own items unless that
    identity is unrestricted. `items_for_doorbell` is the OTHER wrapper: management-shaped (every
    item, unscoped) but for a different consumer — the steward notifier, which is not "a caller
    reading their own inbox" and must never be scoped by ownership the way `review_queue` is.

    **Kinds are disjoint by construction**: a `triage` row is classified by `situations.classify`
    FIRST, and only a row that is NOT an entity situation reaches the generic `parked-capture`
    branch — the same row can never produce two items.

    **Every item is neutralized at the boundary on the way out**: one `_neutralize_leaves` pass
    over each item dict, rather than a per-field decision that reliably misses whichever field was
    added to this same dict most recently.
    """
    items: list[dict] = []

    rows = _query_all_open_submissions(conn, submitted_by=submitted_by, limit=limit)
    for row in rows:
        situation = situations.classify(row)
        if situation:
            items.append({
                "kind": KIND_ENTITY_PROPOSAL, "id": str(row["id"]),
                "submitted_by": row["submitted_by"], "situation": situation,
                "subject": situations.subject_of(row),
                "parked_age_ms": row.get("parked_age_ms"), "created_at": row.get("created_at"),
                # the doorbell's own monotonic change token (`slack/doorbell._state_signature`) —
                # `capture_queue`'s per-delivery fencing counter, incremented only by a real
                # reprocessing claim, never by a clock. Forwarded here so a requeue-and-reprocess
                # that parks a row back into the SAME status/situation is a real state change,
                # not silence.
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
    decisions = _latest_decisions(conn)
    for item in items:
        item["decision"] = decisions.get((item["kind"], item["id"]))
    return items


def review_queue(service, *, limit: int = 50) -> dict:
    """The unified inbox over everything parking on a human. ACL-scoped to the caller, no
    existence leak: an unrestricted (steward) identity sees every item; a scoped identity sees
    only items IT submitted — the same ownership scope `BrainService.submissions` already applies
    to the fast-lane queue, extended to the review kinds rather than invented a second way for
    them. The OPERATIONAL wrapper over `_collect_open_items` — see that function for the shared
    base and its MANAGEMENT sibling (`items_for_doorbell`).
    """
    identity = service.identity
    unrestricted = service.audiences is None
    items = _collect_open_items(service.conn, submitted_by=None if unrestricted else identity,
                                limit=limit)
    return {"identity": identity, "scope": "all" if unrestricted else "own",
           "count": len(items), "items": items}


# Every open item, unscoped by any caller's ownership — the doorbell's own
# read. Not an MCP tool and never exposed as one: a caller-facing surface goes through
# `review_queue`'s ACL scoping instead. Kept here, beside `review_queue`, because both are
# wrappers over the SAME shared base (`_collect_open_items`) and must never independently
# reimplement "which rows are open" or "which kind is this row" a second way.
DOORBELL_ITEM_LIMIT = 500


def items_for_doorbell(conn, *, limit: int = DOORBELL_ITEM_LIMIT) -> list[dict]:
    """Every open review item, system-wide — the steward doorbell notifies about
    everything that parks, not only what one identity happens to own. `stigmergy.slack.doorbell`
    resolves which steward(s) each item's scope belongs to and rings the bell for THEM; that
    resolution is orthogonal to `review_queue`'s per-caller visibility and must not be confused
    with it."""
    return _collect_open_items(conn, submitted_by=None, limit=limit)


# ── the doorbell's steward resolution ───────────────────────────────────────────────────────────
def load_stewards(repo: str, baked_path: str = "") -> dict:
    """`ops/stewards.json` — from the REPO at a fresh base commit wherever a checkout exists, and
    from the deploy-time snapshot at `baked_path` where none does.

    The repo read is first and stays the rule: at the base commit like every other governed input,
    never the working tree. Doorbell events are not all tied to a git operation (a parked capture
    or a new entity situation touches no repo at all), so there is no natural "this item's base" to
    reuse; resolving `origin/main`'s current tip fresh, each poll pass, is what "read like every
    other governed input" means for a caller with no worktree of its own already open.

    **The fallback exists because two deployed process groups hold no checkout at all.** `fly.toml`
    starts `app` and `slack` with baked identities and registry and NO `--repo`, so this read had
    nothing to read: the doorbell returned 0 in silence and every entity-proposal decision failed
    closed on a server whose steward was correctly configured. The snapshot is the same
    mechanism the deploy already uses for the three other `ops/` files, and it costs the same
    thing: a redeploy to change it, which is exactly the trade `identities.json` — a STRONGER
    authority — already accepted. Where a checkout exists nothing changes, so the worker and a
    local stdio server keep ADR 016's per-decision freshness.

    **Also the ONE input `review_decide`'s own authorization predicate (`_is_steward`) reads.** A
    steward map read only to decide who to ring a bell for, and never to decide who may actually
    approve or reject, is a map that is not doing the job its name implies. Same read, same
    freshness guarantee, used for both.

    An absent file on either road is an EMPTY map, never an error — nobody resolves for any scope,
    every decision fails closed, and the doorbell records the undeliverable rather than swallowing
    it. That posture is `base_inputs.load_stewards`' own and is unchanged here.
    """
    if repo:
        return base_inputs.load_stewards(repo, gitcmd.base_ref(repo, "main"))
    return base_inputs.load_stewards_file(baked_path) if baked_path else {}


def resolve_stewards_for_scope(stewards_map: dict, scope_path: str) -> list[str]:
    """Longest-matching zone-path-prefix key in `stewards_map` wins; `"*"` is the universal
    fallback, never itself treated as a prefix to compare lengths against. Scope keys are zone path
    prefixes or `*`, and nothing else. `scope_path=""` (an item with no page path yet — a parked
    capture, an entity proposal) can only ever match `"*"`, which is exactly right: those items are
    not anchored to any zone, so only the universal scope can claim them.
    """
    best_key, best_len = None, -1
    for key in (stewards_map or {}):
        if key == "*":
            continue
        if scope_path and scope_path.startswith(key) and len(key) > best_len:
            best_key, best_len = key, len(key)
    if best_key is not None:
        return _as_list(stewards_map[best_key])
    return _as_list((stewards_map or {}).get("*"))


def _as_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value]
    return [str(value)]


def record_undeliverable(conn, *, event: str, item_ref: str, reason: str) -> None:
    """An event whose scope resolves to no steward, or to a steward with no Slack
    identity, is recorded — never swallowed. Rides the EXISTING `job_runs` writer
    (`capture.ops.record_job_run`) rather than a second table: `job_runs` already exists precisely
    for "a run produced no useful outcome, and here is why", and a steward-doorbell miss is exactly
    that shape of fact, not a new kind of thing to store."""
    capture_ops.record_job_run(conn, "steward-doorbell", status="error",
                               stats={"event": event, "item_ref": item_ref}, error=reason)


# ── review_decide() ────────────────────────────────────────────────────────────────────────────
def record_decision(conn, *, item_kind: str, item_id: str, verdict: str, actor: str,
                    notes: str = "", extra: dict | None = None) -> None:
    """The ONE write to the append-only ledger — every `review_decide` path makes it, and so does
    the admin console's own `entity_approve` (`admin.service`, ADR 030): the console mints through
    the governed door DIRECTLY, never through `review_decide` itself (D2's attribution-not-
    authorization posture for the console would be contradicted by routing through
    `review_decide`'s steward-enforcing guard), but the decision it records belongs in the SAME
    `review_decisions` table, so it reuses this function rather than a second hand-written INSERT
    that could drift from this one's columns. Public (no longer `_record_decision`) for exactly
    that reason — `stigmergy.admin` is a declared, symbol-scoped exception to this package's
    normally-closed boundary (`tests/test_architecture.py`), not a license to import the rest of
    this module. Postgres only — never git: nothing downstream of this function touches a repo.

    `extra` is the seam for per-kind structured detail and no caller passes one today, so the
    column is uniformly NULL. Kept because the ledger is append-only: a kind that later needs to
    record more than free-text `notes` must be able to, without a migration on a table whose whole
    point is that old rows are never rewritten."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO review_decisions (item_kind, item_id, verdict, actor, notes, extra) "
            "VALUES (%s,%s,%s,%s,%s,%s)",
            (item_kind, item_id, verdict, actor, notes, Jsonb(extra) if extra else None))


def _refuse_secret_note(notes: str) -> None:
    """A secrets scan `capture.dispositions.clean` cannot add to itself: `stigmergy.capture` may
    never import `stigmergy.librarian` (the architecture boundary `tests/test_architecture.py`
    pins), so the scanner has to sit at the CALL SITE instead. `review_decide` sits on top of
    `dispositions.clean` and CAN reach `librarian.gates` (a declared exception), so the scan runs
    here, before `notes` reaches `dispositions.clean` or a submitter-visible report — never inside
    `capture.dispositions` itself."""
    if not (notes or "").strip():
        return
    gitleaks_bin = os.environ.get("STIGMERGY_GITLEAKS_BIN", "gitleaks")
    hits = gates.scan_secrets(notes, gitleaks_bin=gitleaks_bin, label="a review note")
    if hits:
        # The rule id from `values`, never re-parsed out of the finding's own display message.
        # `message.rsplit("rule: ", 1)[-1]` returns everything after that marker INCLUDING the
        # `)` the message ends with, and the sentence below adds its own — so a steward was told
        # `(rule: github-pat))`. `Finding.values` carries `(line, rule)` structurally for exactly
        # this; `librarian.processing._refuse` learned the same lesson on its own refusal path.
        _line, rule = hits[0].values
        raise ReviewError(
            "refusing to record this note — it matches a likely secret "
            f"(rule: {rule}). Nothing was recorded; remove "
            "the credential and try again")


def _decide_parked_capture(service, item_id: str, verdict: str, notes: str, actor: str) -> dict:
    """Deliberately NOT the generic `approve`/`reject`/`request_changes` vocabulary:
    `capture.dispositions` already gives a steward three verbs (`requeue`/`resolve`/`reject`) with
    no honest 1:1 mapping to the generic three — there is no `approve` equivalent of a `resolve`
    that carries a REQUIRED note. Forcing one onto this kind would let the button label and the
    recorded verdict disagree, which is the property that must never happen. `verdict` on THIS
    kind IS one of `dispositions.DISPOSITIONS` and is stored verbatim.

    **Authorized before anything else runs**: the row's own submitter, or a steward for
    the universal scope — fetched with `get_submission_trace` (unscoped), never with
    `dispositions.*` directly, so a nonexistent id is refused by the SAME sentence an unauthorized
    one is, before `dispositions`' own three distinct refusals (which name the row's real state)
    ever run.
    """
    submission_id = _parse_id(item_id)
    row = capture_queue.get_submission_trace(service.conn, submission_id) \
        if submission_id is not None else None
    _guard_parked_capture_decision(service, found=row is not None,
                                   submitted_by=(row or {}).get("submitted_by", ""))

    if verdict not in dispositions.DISPOSITIONS:
        raise ReviewError(
            f"a parked-capture decision must be one of {dispositions.DISPOSITIONS} "
            f"(review_decide does not reuse the generic approve/reject/request_changes "
            f"vocabulary for this item kind — see capture.dispositions)")
    _refuse_secret_note(notes)
    if verdict == dispositions.REQUEUE:
        result = dispositions.requeue(service.conn, submission_id, actor=actor, note=notes)
    elif verdict == dispositions.RESOLVE:
        if not notes:
            raise ReviewError("resolve requires a note — what did you do with the material?")
        result = dispositions.resolve(service.conn, submission_id, actor=actor, note=notes)
    else:
        if not notes:
            raise ReviewError("reject requires a reason")
        result = dispositions.reject(service.conn, submission_id, actor=actor, reason=notes)
    record_decision(service.conn, item_kind=KIND_PARKED_CAPTURE, item_id=item_id, verdict=verdict,
                    actor=actor, notes=notes)
    # Composed HERE, once, so a Slack response and any other reader relay the SAME sentence rather
    # than re-deriving their own: a decision's confirmation text is composed by code, not by
    # whichever surface happens to be showing it.
    if verdict == dispositions.RESOLVE:
        message = (f"recorded: resolve on {KIND_PARKED_CAPTURE} #{item_id} by {actor}. "
                  f"Note: \"{notes}\"\nNothing else happens automatically — this was not filed "
                  f"as a page.")
    elif verdict == dispositions.REJECT:
        message = f"recorded: reject on {KIND_PARKED_CAPTURE} #{item_id} by {actor}."
    else:
        message = f"recorded: requeue on {KIND_PARKED_CAPTURE} #{item_id} by {actor}."
    return {"recorded": verdict, "item_kind": KIND_PARKED_CAPTURE, "item_id": item_id,
           "actor": actor, "result": result, "message": message}


def _decide_entity_proposal(service, item_id: str, verdict: str, notes: str, actor: str, *,
                            name: str = "", entity_id: str = "", entity_type: str = "",
                            aliases=None, role: str = "", requeue: bool = False) -> dict:
    """**Authorized before anything else runs**: steward required (entity minting is a
    governance act — there is no "the submitter may act on their own capture" carve-out here the
    way `parked-capture` has), self-approval refused. The row is fetched once, unscoped, purely to
    answer "does it exist, and who submitted it" for that predicate — `situations.require_situation`
    still runs afterward and its own three distinct refusals (not found / not parked / not an
    identity question) are fine to surface to a caller who has already cleared authorization.

    **`approve` mints (ADR 030 D5).** `name`/`entity_type` are validated only AFTER authorization
    and after `situations.require_situation` — never before, so an unauthorized or self-approving
    caller learns nothing about what a mint would need (`NOT_YOURS_TO_DECIDE`'s own rule: a
    governance refusal must not get more specific before authorization has cleared). A steward
    still authors every identity field by hand: `entity_id` PREFILLS to `name`'s slug, never an
    agent's judgment (ADR 016 unchanged) — a caller may still override it, and `entities.mint.mint`
    (by way of `birth.prepare`) refuses one that is not actually that slug, same as the CLI's
    `--id`.
    """
    submission_id = _parse_id(item_id)
    row = capture_queue.get_submission_trace(service.conn, submission_id) \
        if submission_id is not None else None
    _guard_governance_decision(service, found=row is not None,
                               submitted_by=(row or {}).get("submitted_by", ""),
                               scope_path="", verdict=verdict)

    if verdict not in GENERIC_VERDICTS or verdict == REQUEST_CHANGES:
        raise ReviewError(
            f"an entity proposal takes {APPROVE!r} or {REJECT!r} only — there is nothing to "
            f"'request changes' to: either the name resolves to an identity worth minting or it "
            f"does not")
    _refuse_secret_note(notes)
    situations.require_situation(service.conn, submission_id, action=verdict)
    if verdict == REJECT:
        if not notes:
            raise ReviewError("reject requires a reason")
        dispositions.reject(service.conn, submission_id, actor=actor, reason=notes)
        message = f"recorded: reject on {KIND_ENTITY_PROPOSAL} #{item_id} by {actor}."
        record_decision(service.conn, item_kind=KIND_ENTITY_PROPOSAL, item_id=item_id,
                        verdict=verdict, actor=actor, notes=notes)
        return {"recorded": verdict, "item_kind": KIND_ENTITY_PROPOSAL, "item_id": item_id,
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

    mint_result = _mint_entity_proposal(
        service, submission_id=submission_id, entity_id=resolved_id, name=clean_name,
        entity_type=clean_type, aliases=alias_list, role=role or "", approved_by=actor)

    record_decision(service.conn, item_kind=KIND_ENTITY_PROPOSAL, item_id=item_id,
                    verdict=verdict, actor=actor, notes=notes,
                    extra={"entity_id": mint_result["entity_id"], "commit": mint_result["commit"]})

    requeued = None
    if requeue:
        # AFTER the push, never before — `entities.cli`'s own correctness property (its module
        # docstring): a requeue that ran first would hand the librarian a capture whose entity is
        # not yet on the remote it fetches from, and the capture would park a second time.
        requeued = dispositions.requeue(
            service.conn, submission_id, actor=actor,
            note=f"entity {mint_result['entity_id']} approved and pushed "
                 f"({mint_result['commit'][:12]})")

    return {"recorded": verdict, "item_kind": KIND_ENTITY_PROPOSAL, "item_id": item_id,
           "actor": actor, "minted": True, "entity_id": mint_result["entity_id"],
           "name": mint_result["name"], "commit": mint_result["commit"],
           "requeued": bool(requeued)}


def _alias_list(aliases) -> list[str]:
    """`aliases` from an MCP/HTTP caller: already a JSON list, or one comma-separated string typed
    by a human — the same two shapes `entities.cli`'s own `--aliases` accepts. Mirrored here
    (`entities.cli._aliases` is private, and reads an argparse `action="append"` list this caller
    never has) rather than imported."""
    if not aliases:
        return []
    values = aliases if isinstance(aliases, (list, tuple)) else [aliases]
    out = []
    for value in values:
        out += [part.strip() for part in str(value).split(",") if part.strip()]
    return out


# The knowledge repo's default branch — `entities.cli`'s own `--branch` default, and every mint
# through this door targets it. No per-deployment override exists yet (nothing in ADR 030 asks for
# one); a future need is one constant to change, not a new concept to invent.
_MINT_BRANCH = "main"


def _mint_entity_proposal(service, *, submission_id: int, entity_id: str, name: str,
                          entity_type: str, aliases: list[str], role: str,
                          approved_by: str) -> dict:
    """The one call into the governed door (ADR 030 D3): clone `settings.librarian_repo_url` with
    the librarian App's own credential, mint through `entities.mint.mint`, push, clean up.

    `entities.remote.mint_via_clone` is reached as a MODULE ATTRIBUTE (`entities_remote.
    mint_via_clone(...)`, never `from ... import mint_via_clone`), so it stays patchable the same
    way `entities.clone.write_page`/`commit_and_push` already are for the CLI's own tests — a unit
    test of this orchestration alone can monkeypatch `stigmergy.entities.remote.mint_via_clone`,
    though the pg suite itself calls the real thing, against a real local bare remote, never a
    double (this repo's own testing doctrine: a faked git proves nothing about the property being
    claimed).

    Every refusal the entities package raises is mapped into THIS package's own vocabulary here,
    the one place both are in scope: `entities.errors.CapabilityUnavailableError` (no App
    credential, no repo URL configured) becomes `server.errors.CapabilityUnavailableError`, the
    identical posture one layer up; every other `entities.errors.EntityError` (a collision, drift,
    a missing template, a secret in the role/aliases text, a lost push race) becomes a
    `ReviewError` — the same posture every other clean refusal on this lane already has, echoed
    verbatim over MCP.
    """
    try:
        return entities_remote.mint_via_clone(
            service.settings.librarian_repo_url, _MINT_BRANCH, os.environ,
            entity_id=entity_id, name=name, entity_type=entity_type, aliases=aliases, role=role,
            today=date.today().isoformat(), submission_id=submission_id, approved_by=approved_by)
    except EntityCapabilityUnavailableError as ex:
        raise CapabilityUnavailableError(str(ex)) from ex
    except EntityError as ex:
        raise ReviewError(str(ex)) from ex


def review_decide(service, *, item_kind: str, item_id: str, verdict: str, notes: str = "",
                  name: str = "", entity_id: str = "", entity_type: str = "", aliases=None,
                  role: str = "", requeue: bool = False) -> dict:
    """Record a verdict, attributed to the caller's RESOLVED identity (never an argument — the
    same rule `_reply` already enforces for the ask-back channel), into the append-only decisions
    record.

    **Git.** `reject` and every `parked-capture` verdict never touch git — Postgres only,
    categorically, with no exception to defend against. Approving an `entity-proposal` is the one
    path that does (ADR 030 D3-D5): it mints through the governed door
    (`entities.remote.mint_via_clone`, the same `entities.mint.mint` `stigmergy-entities approve`/
    `create` call), exactly ONE commit, authored as the librarian App with an
    `Approved-by: <this caller>` trailer. That verdict alone needs `name` (the page title) and
    `entity_type` (one of `entities.generator.ENTITY_TYPES`) — the old call shape (neither) is
    refused, loud and actionable, naming what is missing, and mints nothing. `entity_id` defaults
    to `name`'s slug; `aliases` (a list, or one comma-separated string) and `role` are optional;
    `requeue` sends the originating capture back to the librarian AFTER the push lands, so it
    re-files anchored to the entity just minted. The human still authors every identity field — a
    prefilled slug is a convenience, never an agent's judgment (ADR 016).

    **Authorization.** `entity-proposal` requires the caller to be a STEWARD (resolved from
    `ops/stewards.json` for the item's own scope) and refuses self-approval; `parked-capture`
    accepts the row's own submitter OR a steward. Every refusal for "not authorized" and "does not
    exist" is the SAME sentence (`NOT_YOURS_TO_DECIDE`) — an authorized caller still sees the
    specific, useful refusals `dispositions`/`situations` raise afterward, and — for an entity
    proposal's `approve` — the metadata/mint refusals above those.
    """
    identity = service.identity
    if not identity:
        raise ReviewError("no resolved identity — a decision cannot be attributed unattributed")
    if item_kind not in ITEM_KINDS:
        raise ReviewError(f"unknown item kind {item_kind!r} (one of {', '.join(ITEM_KINDS)})")
    _check_len("notes", notes or "")
    clean_notes = dispositions.clean(notes)

    if item_kind == KIND_PARKED_CAPTURE:
        return _decide_parked_capture(service, item_id, verdict, clean_notes, identity)
    return _decide_entity_proposal(service, item_id, verdict, clean_notes, identity, name=name,
                                   entity_id=entity_id, entity_type=entity_type, aliases=aliases,
                                   role=role, requeue=requeue)


def review_decide_safe(service, *, item_kind: str, item_id: str, verdict: str, notes: str = "",
                       name: str = "", entity_id: str = "", entity_type: str = "",
                       aliases=None, role: str = "", requeue: bool = False) -> dict:
    """`BrainService.review_decide` (the SAME audited `_call` path — see that method), except a
    CLEAN refusal (a missing required note, an unknown verdict, a verdict this kind does not
    take, ...) is returned as `{"error": str}` instead of raised.

    Exists for a caller that must not import `stigmergy.capture.errors`/`stigmergy.server.errors`
    itself to catch those exception types — `stigmergy.slack` (the Block Kit review surface) is
    architecturally barred from reaching into `stigmergy.capture` at all beyond `store.py`'s own one
    pinned edge (`tests/test_architecture.py`), so a Slack button handler cannot write
    `except CaptureError` the way `mcp_server.py`'s tool closures do. This
    gives it the SAME clean-refusal-vs-unanticipated-exception distinction through a return value
    instead of an exception type it is not allowed to name.

    An UNANTICIPATED exception still propagates — exactly like `mcp_server.py`'s own tool
    closures, which re-raise anything outside their known exception tuple into their generic
    `except Exception` fallback. The caller here is expected to do the same: catch broad
    `Exception` separately and show a GENERIC failure, never `str(ex)` of something this function
    did not anticipate (the same rule `stigmergy.slack.replies` already follows for `service.reply`).
    """
    try:
        return service.review_decide(item_kind, item_id, verdict, notes=notes, name=name,
                                     entity_id=entity_id, entity_type=entity_type,
                                     aliases=aliases, role=role, requeue=requeue)
    except (CaptureError, CapabilityUnavailableError) as ex:
        return {"error": str(ex)}
