"""The review lane — the ONE inbox where work parks on a human, and the append-only record of what
they decided.

`reject` and every `parked-capture` verdict are Postgres only, categorically. Approving an
`entity-proposal` is the one path that touches git: exactly ONE commit through the governed door,
authored as the librarian App with an `Approved-by: <caller>` trailer.

Authorization runs FIRST — an `entity-proposal` needs a STEWARD, a `parked-capture` the steward OR
the asked submitter — and a refusal never becomes more specific once a caller has failed it.
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
    """Lazy: `service.py` imports THIS module at module scope, so the reverse edge must not be
    taken at import time."""
    from stigmergy.server.service import neutralize_fence
    return neutralize_fence(text or "")


def _check_len(name: str, value: str) -> None:
    """Lazy for `_neutralize`'s cycle reason; without it an unbounded `notes` string reaches
    `dispositions.clean` or a gitleaks scan."""
    from stigmergy.server.service import check_arg_length
    check_arg_length(name, value or "")

# Append-only: no code path here UPDATEs or DELETEs, so a second decision cannot overwrite the
# first.
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
    """Idempotent DDL for the review lane's one table, safe from two processes at once."""
    with startup_ddl_lock(conn) as cur:
        for statement in _ALL_DDL:
            cur.execute(statement)


# `review_decide`'s verdict vocabulary — uniform EXCEPT `parked-capture`, which keeps
# `capture.dispositions`' own three verbs.
APPROVE, REJECT, REQUEST_CHANGES = "approve", "reject", "request_changes"
GENERIC_VERDICTS = (APPROVE, REJECT, REQUEST_CHANGES)

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


def _is_steward(service, scope_path: str) -> bool:
    """Is the caller's resolved identity a steward for `scope_path`? Fails closed with `False`,
    never an exception, when this server has neither a checkout nor a baked snapshot."""
    repo = getattr(service.settings, "knowledge_repo", "") or ""
    baked = getattr(service.settings, "stewards_path", "") or ""
    if not repo and not baked:
        return False
    stewards = load_stewards(repo, baked)
    return bool(service.identity) and service.identity in resolve_stewards_for_scope(
        stewards, scope_path)


def _guard_governance_decision(service, *, found: bool, submitted_by: str, scope_path: str,
                               verdict: str) -> None:
    """The `entity-proposal` gate: steward required, self-approval refused. `found=False` and "not
    a steward" collapse onto the SAME `NOT_YOURS_TO_DECIDE` sentence."""
    if not found or not _is_steward(service, scope_path):
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
    if _is_steward(service, ""):   # a parked capture has no page path yet: universal scope only
        return
    raise ReviewError(NOT_YOURS_TO_DECIDE)


def _parse_id(item_id: str) -> int | None:
    """`int(item_id)`, or `None` — a malformed id is as "not found" as a nonexistent one."""
    try:
        return int(item_id)
    except (TypeError, ValueError):
        return None


def _neutralize_leaves(value, depth: int = 0):
    """`_neutralize` over every STRING LEAF — one rule at the boundary instead of per-field calls,
    which reliably miss a field. Past the depth bound the subtree is DROPPED: a recursion limit
    that hands back what it declined to check is a fail-open bound."""
    from stigmergy.server.service import MAX_AUDIT_DEPTH
    if depth > MAX_AUDIT_DEPTH:
        return None
    if isinstance(value, str):
        return _neutralize(value)
    if isinstance(value, dict):
        return {str(k): _neutralize_leaves(v, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_neutralize_leaves(v, depth + 1) for v in value]
    return value


def _latest_decisions(conn) -> dict[tuple[str, str], dict]:
    """The most recent decision per item — a rendering convenience, not a state machine."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT ON (item_kind, item_id) item_kind, item_id, verdict, actor, "
            "created_at FROM review_decisions ORDER BY item_kind, item_id, created_at DESC")
        return {(kind, item_id): {"verdict": verdict, "actor": actor, "created_at": created_at}
                for kind, item_id, verdict, actor, created_at in cur.fetchall()}


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


def _collect_open_items(conn, *, submitted_by: str | None, limit: int) -> list[dict]:
    """The shared base under both wrappers: everything parking on a human, both kinds, latest
    decision attached. `submitted_by=None` is the MANAGEMENT read, never exposed to an MCP caller
    directly. Kinds are disjoint by construction — `situations.classify` runs FIRST, and only a
    non-entity-situation row reaches the `parked-capture` branch."""
    items: list[dict] = []

    rows = _query_all_open_submissions(conn, submitted_by=submitted_by, limit=limit)
    for row in rows:
        situation = situations.classify(row)
        if situation:
            items.append({
                "kind": KIND_ENTITY_PROPOSAL, "id": str(row["id"]),
                "submitted_by": row["submitted_by"], "situation": situation,
                "subject": situations.subject_of(row),
                # BOTH, exactly as `situations._situation_view` emits both, and for the same
                # reason: `subject` is ONE display string that joins several names with ", ",
                # so a consumer acting on a name — prefilling a mint form, running one command
                # per name — must read `subjects` or it will act on a joined compound that is
                # not any of them.
                "subjects": situations.subjects_of(row),
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
    decisions = _latest_decisions(conn)
    for item in items:
        item["decision"] = decisions.get((item["kind"], item["id"]))
    return items


def review_queue(service, *, limit: int = 50) -> dict:
    """The unified inbox over everything parking on a human. An unrestricted identity sees every
    item; a scoped one sees only items IT submitted, the ownership scope
    `BrainService.submissions` applies to the fast lane."""
    identity = service.identity
    unrestricted = service.audiences is None
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
def load_stewards(repo: str, baked_path: str = "") -> dict:
    """`ops/stewards.json` — from the REPO at `origin/main`'s fresh tip wherever a checkout exists
    (never the working tree: a revoked steward must not resolve off a stale read), from the
    deploy-time snapshot at `baked_path` where none does. The ONE input `_is_steward` reads too,
    so one map decides both who to ring and who may approve. An absent file on either road is an
    EMPTY map, never an error, and every decision downstream fails closed.
    """
    if repo:
        return base_inputs.load_stewards(repo, gitcmd.base_ref(repo, "main"))
    return base_inputs.load_stewards_file(baked_path) if baked_path else {}


def resolve_stewards_for_scope(stewards_map: dict, scope_path: str) -> list[str]:
    """Longest-matching zone-path-prefix key wins; `"*"` is the fallback, never compared as a
    prefix, so `scope_path=""` can only match it."""
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
    """An undeliverable event is recorded, not swallowed; rides the `job_runs` writer."""
    capture_ops.record_job_run(conn, "steward-doorbell", status="error",
                               stats={"event": event, "item_ref": item_ref}, error=reason)


# ── review_decide() ────────────────────────────────────────────────────────────────────────────
def record_decision(conn, *, item_kind: str, item_id: str, verdict: str, actor: str,
                    notes: str = "", extra: dict | None = None) -> None:
    """The ONE write to the append-only ledger, Postgres only. Public because the admin console's
    `entity_approve` records here too, bypassing `review_decide`'s steward guard by design.
    `extra` is the seam for per-kind detail: an append-only table cannot be migrated later."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO review_decisions (item_kind, item_id, verdict, actor, notes, extra) "
            "VALUES (%s,%s,%s,%s,%s,%s)",
            (item_kind, item_id, verdict, actor, notes, Jsonb(extra) if extra else None))


def _refuse_secret_note(notes: str) -> None:
    """The secrets scan over a steward note, here at the CALL SITE because `stigmergy.capture` may
    never import `stigmergy.librarian`. Runs before `notes` reaches `clean` or a report."""
    if not (notes or "").strip():
        return
    gitleaks_bin = os.environ.get("STIGMERGY_GITLEAKS_BIN", "gitleaks")
    hits = gates.scan_secrets(notes, gitleaks_bin=gitleaks_bin, label="a review note")
    if hits:
        # the rule id comes structurally from `Finding.values`, never re-parsed out of prose
        _line, rule = hits[0].values
        raise ReviewError(
            "refusing to record this note — it matches a likely secret "
            f"(rule: {rule}). Nothing was recorded; remove "
            "the credential and try again")


def _decide_parked_capture(service, item_id: str, verdict: str, notes: str, actor: str) -> dict:
    """Deliberately NOT the generic verdict vocabulary: `verdict` here IS one of
    `capture.dispositions.DISPOSITIONS`, stored verbatim, so a button label can never disagree
    with the recorded verdict. Authorization runs before anything else, so a nonexistent id is
    refused by the SAME sentence an unauthorized one is."""
    submission_id = _parse_id(item_id)
    row = capture_queue.get_submission_trace(service.conn, submission_id) \
        if submission_id is not None else None
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
    # `str(submission_id)`, never the raw `item_id`: `_parse_id` accepts " 204 "/"007", and a
    # raw-spelling ledger row could never join back to the item it decided.
    record_decision(service.conn, item_kind=KIND_PARKED_CAPTURE, item_id=str(submission_id),
                    verdict=verdict, actor=actor, notes=notes)
    # Composed HERE, once, so every surface relays the SAME confirmation sentence.
    if verdict == dispositions.RESOLVE:
        message = (f"recorded: resolve on {KIND_PARKED_CAPTURE} #{submission_id} by {actor}. "
                  f"Note: \"{notes}\"\nNothing else happens automatically — this was not filed "
                  f"as a page.")
    elif verdict == dispositions.REJECT:
        message = f"recorded: reject on {KIND_PARKED_CAPTURE} #{submission_id} by {actor}."
    else:
        message = f"recorded: requeue on {KIND_PARKED_CAPTURE} #{submission_id} by {actor}."
    return {"recorded": verdict, "item_kind": KIND_PARKED_CAPTURE, "item_id": str(submission_id),
           "actor": actor, "result": result, "message": message}


def _decide_entity_proposal(service, item_id: str, verdict: str, notes: str, actor: str, *,
                            name: str = "", entity_id: str = "", entity_type: str = "",
                            aliases=None, role: str = "", requeue: bool = False) -> dict:
    """Authorized before anything else runs: steward required, self-approval refused. `approve`
    mints. `name`/`entity_type` are validated only AFTER authorization, so a refused caller learns
    nothing about what a mint would need. `entity_id` prefills to `name`'s slug, and
    `entities.mint.mint` refuses one that is not actually that slug."""
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
    # Mapped HERE, like every other `EntityError` this module lets through: an exception type
    # from `stigmergy.entities` must never reach a caller, because `stigmergy.slack` is barred
    # from importing it and could only catch it generically — as an unanticipated fault whose
    # text may not be shown. `ReviewError` is a `CaptureError`, so both surfaces already echo it.
    try:
        situations.require_situation(service.conn, submission_id, action=verdict)
    except EntityError as ex:
        raise ReviewError(str(ex)) from ex
    if verdict == REJECT:
        if not notes:
            raise ReviewError("reject requires a reason")
        dispositions.reject(service.conn, submission_id, actor=actor, reason=notes)
        message = f"recorded: reject on {KIND_ENTITY_PROPOSAL} #{submission_id} by {actor}."
        record_decision(service.conn, item_kind=KIND_ENTITY_PROPOSAL, item_id=str(submission_id),
                        verdict=verdict, actor=actor, notes=notes)
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

    mint_result = _mint_entity_proposal(
        service, submission_id=submission_id, entity_id=resolved_id, name=clean_name,
        entity_type=clean_type, aliases=alias_list, role=role or "", approved_by=actor)

    record_decision(service.conn, item_kind=KIND_ENTITY_PROPOSAL, item_id=str(submission_id),
                    verdict=verdict, actor=actor, notes=notes,
                    extra={"entity_id": mint_result["entity_id"], "commit": mint_result["commit"]})

    requeued = None
    if requeue:
        # AFTER the push, never before: a requeue that ran first would hand the librarian a
        # capture whose entity is not yet on the remote it fetches from.
        requeued = dispositions.requeue(
            service.conn, submission_id, actor=actor,
            note=f"entity {mint_result['entity_id']} approved and pushed "
                 f"({mint_result['commit'][:12]})")

    return {"recorded": verdict, "item_kind": KIND_ENTITY_PROPOSAL,
               "item_id": str(submission_id),
           "actor": actor, "minted": True, "entity_id": mint_result["entity_id"],
           "name": mint_result["name"], "commit": mint_result["commit"],
           "requeued": bool(requeued)}


def _alias_list(aliases) -> list[str]:
    """`aliases` as a JSON list or one comma-separated string, mirroring `entities.cli`."""
    if not aliases:
        return []
    values = aliases if isinstance(aliases, (list, tuple)) else [aliases]
    out = []
    for value in values:
        out += [part.strip() for part in str(value).split(",") if part.strip()]
    return out


# The knowledge repo's default branch; every mint through this door targets it.
_MINT_BRANCH = "main"


def _mint_entity_proposal(service, *, submission_id: int, entity_id: str, name: str,
                          entity_type: str, aliases: list[str], role: str,
                          approved_by: str) -> dict:
    """The one call into the governed door: clone with the librarian App's credential, mint, push,
    clean up. `mint_via_clone` is reached as a MODULE ATTRIBUTE so it stays monkeypatchable. The
    mint's own `EntityError` is mapped into this package's vocabulary here, under the rule this
    whole module holds: an `entities` exception type is translated where it is raised, never
    allowed to leave — `_decide_entity_proposal`'s pre-mint guard does the same for its own."""
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
    """Record a verdict, attributed to the caller's RESOLVED identity — never an argument.

    `reject` and every `parked-capture` verdict are Postgres only. Approving an `entity-proposal`
    is the one path that touches git: one App-authored commit through the governed door; that
    verdict alone needs `name` and `entity_type`, and `requeue` re-files the originating capture
    AFTER the push lands.

    `entity-proposal` requires a STEWARD and refuses self-approval; `parked-capture` accepts the
    row's own submitter OR a steward. "Not authorized" and "does not exist" are the SAME sentence.
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
    """`BrainService.review_decide` with a CLEAN refusal returned as `{"error": str}` rather than
    raised — for `stigmergy.slack`, barred from importing the exception types. An UNANTICIPATED
    exception still propagates, and its `str(ex)` must never be shown."""
    try:
        return service.review_decide(item_kind, item_id, verdict, notes=notes, name=name,
                                     entity_id=entity_id, entity_type=entity_type,
                                     aliases=aliases, role=role, requeue=requeue)
    except (CaptureError, CapabilityUnavailableError) as ex:
        return {"error": str(ex)}
