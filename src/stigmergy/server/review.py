"""The review lane — the ONE inbox of what the librarian proposed and a steward has to decide,
and the append-only record of what they decided.

Three item kinds. An `identity-proposal` is an entity page the librarian created with
`approved_by` empty (Approve · Merge into… · Decline); an `alias-proposal` is a spelling it
appended to a registered entity's `proposed_aliases:` (Approve · Decline); a `repair-proposal` is
the nightly repair proposer's concrete edit (Approve · Reject). Nothing here waits on a submitter
— the capture that proposed an identity is already filed — and the inbox is DERIVED: the two
proposal kinds are read off the entity registry this server already serves, the page index says
what anchors to them, and the ledger says what has been decided. No table of its own.

Authorization runs FIRST — every kind needs a STEWARD, a repair a steward for EVERY page it would
edit — and a refusal never becomes more specific once a caller has failed it.

Every git-touching verdict has ONE ordering function that every door runs (`decide_and_record`,
`commission_registration`, `apply_repair_and_record`), so "the ledger row is written, and written after
the push" is a property of the code rather than of each surface remembering. The librarian reads
that ledger: an identity whose latest decision is a decline is never proposed again.
"""
import logging
import os
from datetime import UTC, date, datetime

from stigmergy.capture import decisions, queue
from stigmergy.capture import ops as capture_ops
from stigmergy.capture import schema as capture_schema
from stigmergy.capture.errors import CaptureError
from stigmergy.entities import decide as entities_decide
from stigmergy.entities import remote as entities_remote
from stigmergy.entities.errors import CapabilityUnavailableError as EntityCapabilityUnavailableError
from stigmergy.entities.errors import EntityError
from stigmergy.entities.generator import canonical_id_for
from stigmergy.librarian import base_inputs, gates, gitcmd
from stigmergy.librarian.errors import LibrarianError
from stigmergy.repair import remote as repair_remote
from stigmergy.repair import schema as repair_schema
from stigmergy.repair import store as repair_store
from stigmergy.repair.errors import RepairError
from stigmergy.review_kinds import (
    ENTITY_TYPES,
    ITEM_KINDS,
    KIND_ALIAS_PROPOSAL,
    KIND_IDENTITY_PROPOSAL,
    KIND_REPAIR_PROPOSAL,
    alias_item_id,
    split_alias_item_id,
)
from stigmergy.server import entity_aliases, ops_files
from stigmergy.server.acl import visible
from stigmergy.server.errors import CapabilityUnavailableError

log = logging.getLogger(__name__)


def _check_len(name: str, value: str) -> None:
    """Lazy: `service.py` imports THIS module at module scope, so the reverse edge must not be
    taken at import time. Without the bound an unbounded `notes` string reaches
    the ledger or a gitleaks scan."""
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


# `review_decide`'s verdict vocabulary, re-exported from the ledger that records it
# (`capture.decisions`), not defined here: this module is one of three writers, and the
# vocabulary belongs with the table. `decline` is the word every surface shows for `REJECT`.
APPROVE, REJECT, REQUEST_CHANGES = decisions.APPROVE, decisions.REJECT, decisions.REQUEST_CHANGES
MERGE = decisions.MERGE
GENERIC_VERDICTS = decisions.GENERIC_VERDICTS
DECLINE = "decline"
# What each kind accepts, the stored spelling on the right: a surface's button label and the
# ledger row can never disagree because the map is the only translation there is.
VERDICTS_BY_KIND = {
    KIND_IDENTITY_PROPOSAL: {APPROVE: APPROVE, MERGE: MERGE, DECLINE: REJECT, REJECT: REJECT},
    KIND_ALIAS_PROPOSAL: {APPROVE: APPROVE, DECLINE: REJECT, REJECT: REJECT},
    KIND_REPAIR_PROPOSAL: {APPROVE: APPROVE, REJECT: REJECT, DECLINE: REJECT},
}

class ReviewError(CaptureError):
    """A clean, caller-facing refusal, echoed verbatim over the wire — never git/DB internals."""


# ONE byte-identical sentence for every unauthorized refusal AND for a nonexistent id: "does not
# exist", "somebody else's item" and "not a steward" must be indistinguishable from the outside.
# The specific refusals still surface, but only AFTER this predicate has cleared.
NOT_YOURS_TO_DECIDE = "there is nothing for you to decide at that id"


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


def _guard_steward_decision(service, *, found: bool, scope_path: str) -> None:
    """The proposal gate: a steward for the entity page's own zone, or the universal one.
    `found=False` and "not a steward" collapse onto the SAME `NOT_YOURS_TO_DECIDE` sentence.

    There is no self-approval rule here, and its absence is a decision: the proposal's author is
    the LIBRARIAN, not the person whose capture prompted it, so the steward confirming it is
    already the second party — and a one-steward deployment (the common shape) could otherwise
    never confirm anything its steward had submitted about."""
    if not found or not is_steward(service, scope_path):
        raise ReviewError(NOT_YOURS_TO_DECIDE)


def _guard_repair_decision(service, *, found: bool, target_paths) -> None:
    """`repair-proposal`'s rule: a steward at the scope of EVERY page the proposal would edit.

    **This is the first verdict in this lane that can be asked a per-PATH question, and it must
    be.** The two proposal kinds resolve to the proposed entity's OWN page once the index has
    seen it (`_scope_path_of`), and until then to `""`, which can only match the universal `"*"`
    key. A repair names the exact pages it would
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

    `merge` is the ONE exception to that rule, and only for `entity-alias`. Every other kind's
    paths say what happens to them; a merge names two entity pages and the decision is WHICH ONE
    SURVIVES, which a sorted `target_paths` cannot express. Without it a steward approves on the
    strength of the `rationale` alone — model-authored text derived from two page bodies somebody
    else wrote — while the half code computed stays off the screen. Nothing new is disclosed: both
    paths are already in `target_paths`, so this adds no ACL question, and `_neutralize_leaves`
    covers it like every other string here.
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
        **({"merge": direction} if (direction := repair_schema.merge_direction(row.get("ops")))
           else {}),
        "model_id": row.get("model_id", ""),
    } for row in repair_store.pending_proposals(conn, limit=limit)]


# ── the inbox: derived from the registry, the index and the ledger ─────────────────────────────
# How many registered entities a proposal's Merge picker is offered by name-token overlap. A
# bound, not a search: the full registry is one `list_entities` away.
MAX_MERGE_CANDIDATES = 8
# How many anchored pages an item lists before the count stands in for the rest.
MAX_ANCHORED_PAGES = 20
_SUMMARY_HEADING = "## What / Who"
MAX_SUMMARY_CHARS = 600


def _registry_for(conn) -> dict[str, dict]:
    """The registry a CONNECTION-only caller (the doorbell) reads: the index's snapshot, or
    nothing — a process with no snapshot and no service has no registry to build an inbox from."""
    return registry_records(ops_files.text_or_none(conn, entity_aliases.ENTITY_REGISTRY_RELPATH),
                            "the index's snapshot")


def registry_records(text: str | None, origin: str) -> dict[str, dict]:
    """The registry TEXT a caller serves, as the records the inbox is derived from — `{}` when
    there is none or it cannot be read (logged: an unreadable registry must not look like an
    empty inbox without a trace). The console passes the copy its Entities desk answers from
    (`index.check.served_registry`: the snapshot, else the `--entity-registry` file) so the
    inbox and the desk can never list different proposals on a stack that has no snapshot."""
    if text is None:
        return {}
    try:
        return entity_aliases.registry_from_text(text, origin)
    except ValueError:
        log.error("the registry (%s) could not be read — the inbox shows no proposals", origin,
                  exc_info=True)
        return {}


def _entity_page_row(conn, entity_id: str):
    """`(path, title, acl, body, as_of, updated)` of the entity's OWN page, or `None` when the
    index has not seen it yet (a proposal is indexed on the next rebuild or webhook)."""
    with conn.cursor() as cur:
        cur.execute("SELECT path, title, acl, body, as_of, updated FROM pages_index"
                    " WHERE type = 'entity' AND %s = ANY(entity) ORDER BY path LIMIT 1",
                    (entity_id,))
        return cur.fetchone()


def _anchored_pages(conn, entity_id: str, *, exclude: str, audiences) -> tuple[list[str], int]:
    """The pages anchored to `entity_id` this caller may see — the capture(s) that proposed it,
    and everything filed against it since. `visible()` is asked of every row: the inbox is a
    steward's surface, but a page's existence is still `acl.visible()`'s to decide."""
    with conn.cursor() as cur:
        cur.execute("SELECT path, acl FROM pages_index WHERE %s = ANY(entity) AND path <> %s"
                    " ORDER BY path", (entity_id, exclude or ""))
        paths = [path for path, acl in cur.fetchall() if visible(acl, audiences)]
    return paths[:MAX_ANCHORED_PAGES], len(paths)


def _summary_of(body: str) -> str:
    """The page's What / Who paragraph — what the librarian said the thing IS — or its first
    paragraph when the page has no such section."""
    text = str(body or "")
    start = text.find(_SUMMARY_HEADING)
    section = text[start + len(_SUMMARY_HEADING):] if start >= 0 else text
    for paragraph in section.split("\n\n"):
        cleaned = " ".join(paragraph.split())
        if cleaned and not cleaned.startswith("#"):
            return cleaned[:MAX_SUMMARY_CHARS]
    return ""


def _merge_candidates(record: dict, registry: dict[str, dict]) -> list[dict]:
    """Registered (confirmed) entities sharing a whole word with the proposal's name or
    aliases — the likeliest `into` targets, first in the Merge picker."""
    words = {w for spelling in (record.get("name", ""), *record.get("aliases", ()))
             for w in _words(spelling)}
    out = []
    for cid, other in sorted(registry.items()):
        if cid == record["id"] or other.get("proposed"):
            continue
        theirs = {w for spelling in (other.get("name", ""), *other.get("aliases", ()))
                  for w in _words(spelling)}
        if words & theirs:
            out.append({"id": cid, "name": other.get("name", "")})
        if len(out) >= MAX_MERGE_CANDIDATES:
            break
    return out


def _words(spelling: str) -> set[str]:
    return {w for w in "".join(ch.lower() if ch.isalnum() else " " for ch in str(spelling)).split()
            if len(w) >= 3}


def _identity_item(conn, record: dict, registry: dict, *, audiences, scoped: bool) -> dict | None:
    page = _entity_page_row(conn, record["id"])
    if page is not None and not visible(page[2], audiences):
        return None
    if page is None and scoped:
        # Not indexed yet: nothing says who may see it, so a scoped caller does not.
        return None
    path = page[0] if page else ""
    anchored, total = _anchored_pages(conn, record["id"], exclude=path, audiences=audiences)
    return {
        "kind": KIND_IDENTITY_PROPOSAL, "id": record["id"],
        "name": record.get("name", ""), "entity_type": record.get("type", ""),
        "aliases": list(record.get("aliases") or []),
        "proposed_aliases": list(record.get("proposed_aliases") or []),
        "summary": _summary_of(page[3]) if page else "",
        "page": path or None,
        "anchored_pages": anchored, "anchored_total": total,
        "created": (page[4] or page[5]) if page else "",
        "merge_candidates": _merge_candidates(record, registry),
    }


def _alias_item(conn, record: dict, alias: str, *, audiences, scoped: bool) -> dict | None:
    page = _entity_page_row(conn, record["id"])
    if page is not None and not visible(page[2], audiences):
        return None
    if page is None and scoped:
        return None
    return {
        "kind": KIND_ALIAS_PROPOSAL, "id": alias_item_id(record["id"], alias),
        "entity_id": record["id"], "entity_name": record.get("name", ""), "alias": alias,
        "page": page[0] if page else None,
    }


def _collect_open_items(conn, registry: dict[str, dict], *, audiences, scoped: bool,
                        limit: int) -> list[dict]:
    """The shared base under both wrappers: every proposal waiting on a steward, latest decision
    attached. `scoped=False` is the MANAGEMENT read — an unrestricted identity, or the doorbell.

    **`repair-proposal` is in the MANAGEMENT read only**, and the asymmetry is the honest reading
    of the ownership scope rather than an omission: a repair proposal names the PAGE PATHS it
    would edit, `acl.visible()` decides who may see that a page exists, and this list does not
    ask it for repairs. The two proposal kinds DO ask it, per entity page, which is why a scoped
    caller can be shown them.
    """
    items: list[dict] = []
    if not scoped:
        # `limit` bounds THIS read as well as the proposals below: a cron can produce a thousand
        # repairs overnight, and the caller's ceiling must not be advisory over exactly that kind.
        items += _repair_proposal_items(conn, limit=limit)
    for cid in sorted(registry):
        record = registry[cid]
        if record.get("proposed"):
            item = _identity_item(conn, record, registry, audiences=audiences, scoped=scoped)
            if item is not None:
                items.append(item)
        for alias in record.get("proposed_aliases") or ():
            item = _alias_item(conn, record, alias, audiences=audiences, scoped=scoped)
            if item is not None:
                items.append(item)
    if len(items) > limit:
        # no silent caps: a caller sizing `limit` generously still learns when reality outgrew it
        log.warning("review: %d open items, %d shown — raise the limit to see the rest",
                    len(items), limit)
        items = items[:limit]

    items = [_neutralize_leaves(item) for item in items]
    latest_by_item = decisions.latest_decisions(conn)
    for item in items:
        item["decision"] = _serializable(latest_by_item.get((item["kind"], item["id"])))
    return items


def _serializable(decision: dict | None) -> dict | None:
    """A ledger row as JSON can carry it: `created_at` is a `datetime` off the driver."""
    if decision is None:
        return None
    return {**decision, "created_at": _iso(decision.get("created_at"))}


def review_queue(service, *, limit: int = 50) -> dict:
    """The unified inbox over everything waiting on a steward. An unrestricted identity sees every
    item; a scoped one sees the proposals whose entity page it may see, and no repairs."""
    identity = service.identity
    unrestricted = service.unrestricted
    # A SCOPED caller with no identity would be shown the MANAGEMENT read while being labelled
    # `scope: "own"`. Fail closed where the widening decision is made.
    if not unrestricted and not identity:
        raise ValueError("a scoped review queue needs a resolved identity — refusing to widen to "
                         "every identity's items")
    items = _collect_open_items(service.conn, service._registry_records(),
                                audiences=service.audiences, scoped=not unrestricted, limit=limit)
    return {"identity": identity, "scope": "all" if unrestricted else "own",
            "count": len(items), "items": items}


# The doorbell's own read — never an MCP tool: a caller-facing surface goes through
# `review_queue`'s ACL scoping instead.
DOORBELL_ITEM_LIMIT = 500


def items_for_doorbell(conn, *, limit: int = DOORBELL_ITEM_LIMIT,
                       registry: dict[str, dict] | None = None) -> list[dict]:
    """Every open review item, system-wide and unscoped; `slack.doorbell` resolves the steward.
    `registry` is the records a caller serves from (`registry_records`); the doorbell, which has
    only a connection, leaves it out and reads the index's snapshot."""
    records = _registry_for(conn) if registry is None else registry
    return _collect_open_items(conn, records, audiences=None, scoped=False, limit=limit)


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
def _recorded_message(kind: str, verdict: str, item_id: str, actor: str, *, summary: str = "") -> str:
    """The confirmation sentence, composed ONCE so every surface and every item kind relays the
    same one. `summary` is what the decision DID, when it did something to the knowledge repo."""
    line = f"recorded: {verdict} on {kind} {item_id} by {actor}."
    if summary:
        line += f" {summary}"
    return line


def _already_decided_suffix(conn, item_kind: str, item_id: str) -> str:
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
    decision = decisions.latest_decision_for(conn, item_kind=item_kind, item_id=str(item_id))
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


def _proposal_or_none(service, entity_id: str) -> dict | None:
    record = service._registry_records().get(str(entity_id or "").strip())
    return record if record and record.get("proposed") else None


def _scope_path_of(service, entity_id: str) -> str:
    page = _entity_page_row(service.conn, entity_id)
    return page[0] if page else ""


def _decide_identity(service, item_id: str, verdict: str, notes: str, actor: str, *,
                     source: str, into: str) -> dict:
    """Authorized before anything else runs: steward required. `verdict` is read through
    `VERDICTS_BY_KIND`, so `decline` and `reject` are one stored word. `merge` needs `into`, a
    registered and confirmed entity, refused BEFORE the clone for the two things the registry
    this server already holds can answer; `entities.decide` re-asks both against the knowledge
    repo as it actually is."""
    record = _proposal_or_none(service, item_id)
    _guard_steward_decision(service, found=record is not None,
                            scope_path=_scope_path_of(service, item_id))
    stored = VERDICTS_BY_KIND[KIND_IDENTITY_PROPOSAL].get(verdict)
    if stored is None:
        raise ReviewError(
            f"an identity proposal takes {APPROVE!r}, {MERGE!r} (with `into`) or {DECLINE!r} — "
            f"the librarian proposed an identity; you confirm it, say which registered entity it "
            f"really is, or decline it")
    _refuse_secret_note(notes)
    entity_id = record["id"]
    today = date.today().isoformat()
    if stored == MERGE:
        target = str(into or "").strip()
        if not target:
            raise ReviewError("merge needs `into`: the registered entity this proposal really is")
        survivor = service._registry_records().get(target)
        if survivor is None or survivor.get("proposed"):
            raise ReviewError(
                f"`into` must be a registered, confirmed entity — {target!r} is "
                f"{'itself a proposal' if survivor else 'not in the registry'} (list_entities "
                f"shows the ids)")
        action = lambda repo: entities_decide.merge_entity(  # noqa: E731
            repo, entity_id=entity_id, into=target, approved_by=actor, today=today)
        extra = {"into": target}
    elif stored == APPROVE:
        action = lambda repo: entities_decide.approve_entity(  # noqa: E731
            repo, entity_id=entity_id, approved_by=actor, today=today)
        extra = {}
    else:
        action = lambda repo: entities_decide.decline_entity(  # noqa: E731
            repo, entity_id=entity_id, today=today)
        extra = {}
    result = _translate(service, KIND_IDENTITY_PROPOSAL, entity_id,
                        lambda: decide_and_record(
                            service.conn, repo_url=service.settings.librarian_repo_url,
                            item_kind=KIND_IDENTITY_PROPOSAL, item_id=entity_id, verdict=stored,
                            actor=actor, source=source, notes=notes, action=action, extra=extra))
    return {"recorded": stored, "item_kind": KIND_IDENTITY_PROPOSAL, "item_id": entity_id,
            "actor": actor, "commit": result["commit"], "into": target if stored == MERGE else "",
            "reanchored": result["reanchored"],
            "message": _recorded_message(KIND_IDENTITY_PROPOSAL, stored, entity_id, actor,
                                         summary=result["summary"])}


def _decide_alias(service, item_id: str, verdict: str, notes: str, actor: str, *,
                  source: str) -> dict:
    entity_id, alias = split_alias_item_id(item_id)
    record = service._registry_records().get(entity_id) if entity_id else None
    found = bool(record) and alias in (record.get("proposed_aliases") or ())
    _guard_steward_decision(service, found=found,
                            scope_path=_scope_path_of(service, entity_id) if entity_id else "")
    stored = VERDICTS_BY_KIND[KIND_ALIAS_PROPOSAL].get(verdict)
    if stored is None:
        raise ReviewError(f"an alias proposal takes {APPROVE!r} or {DECLINE!r} — the spelling is "
                          f"one of that entity's names or it is not")
    _refuse_secret_note(notes)
    today = date.today().isoformat()
    if stored == APPROVE:
        action = lambda repo: entities_decide.approve_alias(  # noqa: E731
            repo, entity_id=entity_id, alias=alias, approved_by=actor, today=today)
    else:
        action = lambda repo: entities_decide.decline_alias(  # noqa: E731
            repo, entity_id=entity_id, alias=alias, today=today)
    canonical = alias_item_id(entity_id, alias)
    result = _translate(service, KIND_ALIAS_PROPOSAL, canonical,
                        lambda: decide_and_record(
                            service.conn, repo_url=service.settings.librarian_repo_url,
                            item_kind=KIND_ALIAS_PROPOSAL, item_id=canonical, verdict=stored,
                            actor=actor, source=source, notes=notes, action=action, extra={}))
    return {"recorded": stored, "item_kind": KIND_ALIAS_PROPOSAL, "item_id": canonical,
            "actor": actor, "commit": result["commit"],
            "message": _recorded_message(KIND_ALIAS_PROPOSAL, stored, canonical, actor,
                                         summary=result["summary"])}


def _translate(service, item_kind: str, item_id: str, run):
    """This package's vocabulary, applied where an `entities` exception is met: a type from below
    never reaches a caller, because `stigmergy.slack` is barred from importing it and could only
    catch it generically — as an unanticipated fault whose text may not be shown. A refusal that
    names a proposal nothing can find any more is where the "a second door got here first" clause
    is appended, and only here, strictly after the caller has been authorized."""
    try:
        return run()
    except EntityCapabilityUnavailableError as ex:
        raise CapabilityUnavailableError(str(ex)) from ex
    except EntityError as ex:
        raise ReviewError(
            f"{ex}{_already_decided_suffix(service.conn, item_kind, item_id)}") from ex


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
    console through `admin.service.repair_approve`. It is `decide_and_record`'s lesson
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
    `entities_remote.decide_via_clone` keeps one door over.

    `notes` reaches BOTH writes, exactly as `reject_repair_and_record` already does with its
    reason — it is the only record of why a repair was worth applying, and it used to be dropped on
    approve while being kept on reject. It is `decide_and_record`'s asymmetry too: the review
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


def decide_and_record(conn, *, repo_url: str, item_kind: str, item_id: str, verdict: str,
                      actor: str, source: str, notes: str = "", action, extra: dict) -> dict:
    """ONE steward decision on a proposal, in the one order that is correct — land it through the
    governed door, then write the governance ledger row.

    Both server-side doors that decide run THIS: MCP/Slack through `review_decide`, the admin
    console through `admin.service`. There IS a third door, and it is the copy: `stigmergy-entities
    approve|merge|decline` decides from the steward's own clone and runs its own spelling of the
    same order, because `stigmergy.entities` cannot import `stigmergy.server`.

    The ledger row is written LAST, after the push: a row claiming a decline whose commit never
    landed would make the librarian refuse an identity whose proposed page still stands. It
    takes NO authorization argument, on purpose: authorization is per-surface (a resolved steward
    here, the operator token in the console), so the CALLER SET is closed and pinned in
    `tests/test_architecture.py`. `decide_via_clone` is reached as a MODULE ATTRIBUTE so it stays
    monkeypatchable. `notes` goes VERBATIM into an append-only table, so a caller supplying a
    non-empty one must already have run `_refuse_secret_note`.
    """
    result = entities_remote.decide_via_clone(repo_url, _KNOWLEDGE_BRANCH, os.environ,
                                              action=action, decided_by=actor)
    record_decision(conn, item_kind=item_kind, item_id=item_id, verdict=verdict, actor=actor,
                    source=source, notes=notes, extra={"commit": result["commit"], **extra})
    return result


def commission_registration(conn, evidence, *, name: str, entity_type: str, aliases: list[str],
                            about: str, actor: str, source: str) -> dict:
    """A steward introducing an entity nobody proposed — the console's `create` door, ADR 042.

    There is no deterministic birth: what the steward knows about the entity (`about`) is queued
    as a capture carrying the registration, the librarian writes the entity's page from it and
    from what the brain already holds, anchors the note to it, and the worker births the entity
    CONFIRMED by `actor` and writes the ledger row after the push — so "entities born" counts this
    door exactly like an approval, and nothing here touches git or the ledger. `source` names the
    door for that row. Like `decide_and_record`, this carries no authorization of its own: the
    console decides under its operator token before calling it.
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
                        f"the note to it, and the entity is born confirmed by {actor}. It appears in "
                        f"Entities when the capture files.")}


def review_decide(service, *, item_kind: str, item_id: str, verdict: str, source: str,
                  notes: str = "", into: str = "") -> dict:
    """Record a verdict, attributed to the caller's RESOLVED identity — never an argument.

    Every verdict but a repair's `reject` touches git: one App-authored commit through a governed
    door, carrying a `Decided-by: <caller>` trailer. An `identity-proposal` takes `approve`,
    `merge` (with `into`, the registered entity it really is) or `decline`; an `alias-proposal`
    `approve` or `decline`; a `repair-proposal` `approve` or `reject`. Every kind requires a
    STEWARD — for a repair, at the scope of EVERY page it would edit. "Not authorized" and "does
    not exist" are the SAME sentence.

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
    _check_len("into", into or "")
    clean_notes = capture_schema.clean_note(notes)

    if item_kind == KIND_REPAIR_PROPOSAL:
        return _decide_repair(service, item_id, VERDICTS_BY_KIND[item_kind].get(verdict, verdict),
                              clean_notes, identity, source=source)
    if item_kind == KIND_ALIAS_PROPOSAL:
        return _decide_alias(service, item_id, verdict, clean_notes, identity, source=source)
    return _decide_identity(service, item_id, verdict, clean_notes, identity, source=source,
                            into=into)


def review_decide_safe(service, *, item_kind: str, item_id: str, verdict: str, source: str,
                       notes: str = "", into: str = "") -> dict:
    """`BrainService.review_decide` with a CLEAN refusal returned as `{"error": str}` rather than
    raised — for `stigmergy.slack`, barred from importing the exception types. An UNANTICIPATED
    exception still propagates, and its `str(ex)` must never be shown."""
    try:
        return service.review_decide(item_kind, item_id, verdict, source=source, notes=notes,
                                     into=into)
    except (CaptureError, CapabilityUnavailableError) as ex:
        return {"error": str(ex)}
