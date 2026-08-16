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

# `review_decisions` is no longer this module's table. It moved to `capture.decisions`, BELOW both
# this package and `stigmergy.entities`, so `stigmergy-entities approve` can write the same row
# without crossing the `entities` -> `server` edge the architecture tests enforce (ADR 030 D2, and
# the amendment recording why). Re-exported by the two names callers already use, because the
# owner of a table moving is not a reason for eight entry points to learn a new import.
ensure_review_schema = decisions.ensure_decisions_schema
record_decision = decisions.record_decision


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


_latest_decisions = decisions.latest_decisions


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

    minted = _mint_entity_proposal(
        service, submission_id=submission_id, entity_id=resolved_id, name=clean_name,
        entity_type=clean_type, aliases=alias_list, role=role or "", notes=notes,
        approved_by=actor, requeue=requeue)

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


# The knowledge repo's default branch; every mint through this door targets it.
_MINT_BRANCH = "main"


def mint_and_record_approval(conn, *, repo_url: str, submission_id: int, entity_id: str,
                             name: str, entity_type: str, aliases: list[str], role: str,
                             actor: str, notes: str = "", requeue: bool = False) -> dict:
    """The mint sequence itself, in the ONE order that is correct — mint through the governed
    door, write the governance ledger row, then (only if asked) requeue the capture.

    Both SERVER-SIDE doors that mint run THIS: MCP/Slack through `_mint_entity_proposal` below,
    the admin console through `admin.service.entity_approve`. Two copies of an ordering rule are
    two places for it to be reordered, and the reordering is invisible from either door's end
    state. There IS a third door, and it is the copy: `stigmergy-entities approve` mints from the
    steward's own clone and runs its own spelling of the same order, because `stigmergy.entities`
    cannot import `stigmergy.server`.

    What it no longer does is skip the ledger. The writer moved DOWN to `capture.decisions`, below
    both packages, so all three doors record the same row and `review_decisions` answers "who
    approved this identity" completely (issue #51, ADR 030's amendment). A CLI approval is
    attributable twice over now — from its commit's author and from its ledger row, which carry the
    same steward identity.

    It deliberately covers three steps and no more. The pending-situation guard
    (`situations.require_situation`) stays at each call site because the two doors run it at
    different points in their own argument validation, and one shared answer would silently change
    the other door's refusal for a caller who is wrong in both ways at once.

    `stigmergy.entities` exceptions leave here UNTRANSLATED, on purpose: each door maps them
    itself and maps them differently. `_mint_entity_proposal` turns them into this package's
    vocabulary; the console lets the library's own class reach `admin.service._mutate`, which
    records that class name in `admin_actions` before turning it into an `AdminRefused`. A
    translation here would rename what the console's bookkeeping captures precisely.

    `notes` is the doors' other asymmetry, and a parameter for that reason: the review lane
    carries the steward's cleaned note into the ledger row, the console has no note field and
    writes `''`. It goes VERBATIM into an append-only table that cannot be migrated afterwards,
    and nothing here scans it — a caller passing a NON-EMPTY note must already have run
    `_refuse_secret_note` (or an equivalent secrets scan) upstream. `review_decide` does, far
    before the mint; the console passes `''` and so has nothing to scan.

    `mint_via_clone` is reached as a MODULE ATTRIBUTE so it stays monkeypatchable.
    """
    mint_result = entities_remote.mint_via_clone(
        repo_url, _MINT_BRANCH, os.environ,
        entity_id=entity_id, name=name, entity_type=entity_type, aliases=aliases, role=role,
        today=date.today().isoformat(), submission_id=submission_id, approved_by=actor)
    record_decision(conn, item_kind=KIND_ENTITY_PROPOSAL, item_id=str(submission_id),
                    verdict=APPROVE, actor=actor, notes=notes,
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
                          approved_by: str, requeue: bool) -> dict:
    """This door's half of `mint_and_record_approval`: the shared sequence with THIS package's
    translation wrapped around it, under the rule this whole module holds — an `entities`
    exception type is translated where this package meets it, never allowed to leave
    (`_decide_entity_proposal`'s pre-mint guard does the same for its own).

    The `try` covers the WHOLE shared sequence, not just the mint: the ledger row and the requeue
    are inside it too. That is inert today — `CaptureError` and `EntityError` are disjoint
    hierarchies, so neither of those two steps can raise what these handlers catch — but the
    clause no longer means "the mint's own errors", it means "anything the sequence raises". Add
    an `entities` call to `mint_and_record_approval` and this door starts translating it into a
    `ReviewError` silently, where before it would have surfaced as an unanticipated fault.
    """
    try:
        return mint_and_record_approval(
            service.conn, repo_url=service.settings.librarian_repo_url,
            submission_id=submission_id, entity_id=entity_id, name=name, entity_type=entity_type,
            aliases=aliases, role=role, actor=approved_by, notes=notes, requeue=requeue)
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
