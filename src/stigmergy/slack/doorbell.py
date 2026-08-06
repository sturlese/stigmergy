"""The steward's doorbell: the events that park work on a human — a capture parking in `triage`,
or a parked row that is an identity decision (an entity proposal) — each ring a bell in the Slack
channel the steward already reads.

**Rides the existing push channel, on the SAME `slack` process group** — there is no fourth
always-on process. `poll_once` below is a second `asyncio` background task
`stigmergy.slack.app._async_main` starts alongside `poller.run_poller`: a second coroutine inside the
process the poller already owns, not a second machine. It never claims, leases or mutates a queue
row (the same read-only posture `poller.py`'s own module docstring states): every read here goes
through `stigmergy.server.review.items_for_doorbell`, the management-shaped, unscoped sibling of
`review_queue` (see that function's own docstring for the shared base the two wrap).

**Three properties, each load-bearing:**

- **One notification per (item, steward), re-sent only on a state change** — `_state_signature`
  computes a small, stable fingerprint per item kind; `store.last_notified_state`/`mark_notified`
  compare and record it, mirroring the poller's own `store.due_for_report`/`store.mark_reported`
  "send, then mark" order exactly: a post that fails leaves nothing recorded, so the next pass
  retries it rather than treating a failed send as delivered.
- **An undeliverable notification is recorded, never swallowed** — no steward resolving for the
  scope, or a resolved steward with no Slack identity in this workspace, writes a `job_runs` row
  (`review.record_undeliverable`, riding the EXISTING writer `capture.ops` already provides)
  naming the event and the reason.
- **No material excerpt for a capture the librarian has not yet looked at** — this is a BREVITY
  rule, kept textually separate from `capture.schema.withheld_reason`'s SECURITY rule on purpose:
  the doorbell is terse by DESIGN, not terse because the steward lacks access. `_render_for_item`
  never reads a capture's raw material or an entity proposal's rationale — only `report['summary']`
  (which `librarian.report`'s own "state the fact, never the implication" discipline already keeps
  free of the raw excerpt) and a proposed entity's own short name, lifted by the agent from PRIVATE
  captured material and published nowhere.

**That brevity rule is enforced HERE too, fail-closed, not only by the upstream status filter.**
`_collect_open_items` (`stigmergy.server.review`, another module) happens to keep `FAILED`/`QUEUED`
rows out of the doorbell's parked-capture items — but that filter was written for `review_queue`'s
own purpose, carries no comment tying it to this rule, and is one `REPORTABLE_STATUSES`-shaped edit
away from silently handing this module a `report['summary']` the secrets/PII gate never ran over.
`_render_for_item` asks `capture_schema.withheld_reason` itself, from the item's OWN status, before
it will render a parked capture's summary at all — so this module's guarantee does not depend on
which rows a different module's query happens to select.

**Two silent failure modes, both closed here:**

1. *Permanent silence for an item that returns to a state it was already notified at*
   (`_state_signature`'s own docstring, below, for the mechanism) — a parked capture that gets
   requeued, reprocessed and parks again in the SAME status (the ordinary "try again" outcome)
   would otherwise produce the IDENTICAL signature as before, so `last_notified_state == state` and
   the bell never rings again for it, ever. `attempts` — `capture_queue`'s own monotonic
   per-delivery fencing counter, incremented only by a real reprocessing claim, never by a clock —
   is folded into the signature wherever it is present on the item, so a requeue-and-reprocess is a
   real state change even when the STATUS string comes back around to the same value.
   `stigmergy.server.review._collect_open_items` forwards `attempts` onto the
   `parked-capture`/`entity-proposal` item dicts it builds (it already reads it off every
   `capture_queue` row). Proved end to end, through the real queue primitives, not a mock:
   `tests/slack/test_doorbell.py::
   test_requeue_and_reprocess_back_into_the_same_status_rings_a_second_time`. `item.get("attempts")`
   staying absent (a caller that hand-builds an item dict for a test) degrades to the safe,
   status-only shape rather than raising.
2. *A transient Slack failure recorded as a false permanent fact* — `_resolve_slack_user_id`
   returns a TRI-STATE result (`"found"` / `"not_found"` / `"failed"`) instead of collapsing a
   timeout/5xx/429 and an honest `users_not_found` onto the same `None`, so a caller cannot record
   "has no Slack identity in this workspace" for a fact the API never actually established. The
   lookup is also cached on `ctx.cache` (already threaded through every handler, extended with the
   reverse email->id direction) — `users.lookupByEmail` is Tier-3 (~50/min), and running it once
   per (item, steward) on EVERY poll pass with no cache is exactly what turns a transient rate
   limit into a sustained one. Every undeliverable reason is recorded through the SAME
   state-comparison mechanism a successful delivery uses (`store.last_notified_state`/
   `mark_notified`, keyed on an `"undeliverable:<class>"` state string) rather than an
   unconditional `record_undeliverable` insert on every pass — dedup by (item, steward, reason
   class), without a second table.
"""
import logging

from stigmergy.server import review
from stigmergy.slack import copy, render, store
from stigmergy.slack.gateway import SlackApiError

log = logging.getLogger(__name__)

# `ops/stewards.json` changes on a monthly cadence (the same posture `identities.json` takes), but
# `review.load_stewards` is a real `git fetch origin main` — unbounded, that is one fetch per poll
# pass, 8,640/day at the default 10s interval. This TTL bounds it without touching
# `review.load_stewards` itself: `review_decide`'s authorization check (`review._is_steward`) calls
# that function directly and is INTENTIONALLY left alone — that path is always-fresh on purpose (a
# revoked steward's approval must never succeed off a stale cache), and this TTL must never be
# wired into it.
_STEWARDS_CACHE_TTL_S = 300


def _load_stewards_cached(ctx, repo: str, baked_path: str = "") -> dict:
    """`review.load_stewards(repo, baked_path)`, served from `ctx._stewards_cache` for `_STEWARDS_CACHE_TTL_S`
    — LOCAL to the doorbell's own steward resolution (see the note above this function). On a
    process WITH a checkout this bounds a real `git fetch`; on the deployed `slack` group, which
    has none, it bounds a plain file read of the baked snapshot — cheap either way, and kept
    uniform so the authorization path has ONE freshness story rather than two. A cache
    hit also means a git-fetch failure inside `review.load_stewards` (already logged loudly by
    `gitcmd.base_ref` on every occurrence) surfaces at most once per TTL window instead of once per
    10-second poll pass, which is most of the practical log spam — even though this function has no
    way to distinguish "fetched fresh" from "fell back to local": `review.load_stewards`'s own
    return contract carries only the resolved map, and widening it to say which would touch the
    same function `_is_steward`'s authorization freshness guarantee depends on."""
    cache = ctx._stewards_cache
    now = ctx._clock()
    if cache.get("repo") == repo and (now - cache.get("loaded_at", -1.0)) < _STEWARDS_CACHE_TTL_S:
        return cache["map"]
    stewards_map = review.load_stewards(repo, baked_path)
    ctx._stewards_cache = {"repo": repo, "loaded_at": now, "map": stewards_map}
    return stewards_map

# `_resolve_slack_user_id`'s tri-state result — a failure is never allowed to collapse onto the
# same value an honest "no such person" produces.
LOOKUP_FOUND = "found"
LOOKUP_NOT_FOUND = "not_found"
LOOKUP_FAILED = "failed"

# The state-signature prefix an UNDELIVERABLE outcome is recorded under — distinct from every real
# item-content state (`"triage"`, `"needs_input"`, an entity `situation` string, ...), none of which
# begin with this literal, so the two namespaces never collide inside the SAME
# `steward_notifications` row.
_UNDELIVERABLE_PREFIX = "undeliverable:"

# `review.KIND_*` -> the noun `job_runs` and the undeliverable copy name as "the {event}".
_EVENT_NAMES = {
    review.KIND_PARKED_CAPTURE: "capture-parked notification",
    review.KIND_ENTITY_PROPOSAL: "entity-proposal notification",
}


def _state_signature(item: dict) -> str:
    """A small, stable fingerprint of "what a steward would be told right now" — re-sent only when
    this changes. Deliberately NOT the whole item dict (which carries timestamps that tick on their
    own and would defeat the whole point).

    **`attempts` is folded in wherever the item carries it.** A parked capture's status string
    ALONE is a two-value alphabet (`triage`/`needs_input`), so the ordinary "steward clicks
    Requeue, the librarian reprocesses, the capture parks again in the SAME status" outcome
    (requeue exists for exactly this) would produce the IDENTICAL signature as before, and the bell
    would never ring again for that item. `attempts` (`capture_queue`'s own monotonic per-delivery
    fencing counter — incremented only by a real reprocessing claim, never by a clock ticking on
    its own) makes a requeue-and-reprocess a real state change even when the status comes back
    around. `stigmergy.server.review._collect_open_items` forwards it onto every
    `parked-capture`/`entity-proposal` item; if a caller ever hands this function an item without
    the key (a hand-built test fixture), it degrades to the safe, status-only shape — inert, not
    wrong."""
    kind = item["kind"]
    attempts = item.get("attempts")
    attempts_suffix = f"@{attempts}" if attempts is not None else ""
    if kind == review.KIND_PARKED_CAPTURE:
        return item.get("status", "") + attempts_suffix
    return (item.get("situation", "") or "parked") + attempts_suffix


def _summary_for_doorbell(item: dict) -> str:
    """The parked-capture summary, fail-closed at the point of rendering. This module's own
    guarantee — no material excerpt for a capture the librarian has not yet looked at — must not
    depend ENTIRELY on `stigmergy.server.review._collect_open_items` only ever selecting
    `triage`/`needs_input` rows: that is a status filter written for `review_queue`'s purpose, in a
    different module, with no comment tying it to this rule. Add `FAILED` or `QUEUED` to that
    filter and this module would DM `report['summary']` for a row the secrets/PII gate never ran
    on, with nothing here objecting.

    `store.withheld_reason` (re-exported from `stigmergy.capture.schema` — the ONE permitted edge
    into `stigmergy.capture`, `store.py`'s own; this module reuses it rather than importing
    `stigmergy.capture` itself a second way) is the SAME function the fast-lane report surfaces
    already call — asked here from the item's own `status` alone. No `report` dict travels onto a
    doorbell item, so there is nothing for the report-dependent branch to read; passing `None`
    reads as "not flagged" for `triage` and `needs_input` — the only statuses this item kind is
    expected to carry — and as "flagged", fail closed, for `queued`, `claimed`, `failed` and
    `rejected`, which the doorbell was never designed to see in the first place."""
    status = item.get("status", "")
    withheld = store.withheld_reason(status, None)
    return withheld or item.get("summary", "")


def _render_for_item(item: dict, *, is_resend: bool) -> tuple[list[dict], str]:
    """`(blocks, plain_text_fallback)` — never reads a capture's raw material or an entity
    proposal's rationale (see the module docstring). `is_resend` (this exact steward has already
    been told about this item, at a DIFFERENT state) is accepted and currently unused: neither item
    kind renders differently on a re-send, so a card is re-sent verbatim or not at all (see
    `_state_signature`)."""
    kind = item["kind"]
    if kind == review.KIND_PARKED_CAPTURE:
        return render.render_doorbell_parked_capture(item_id=item["id"],
                                                      summary=_summary_for_doorbell(item))
    return render.render_doorbell_entity_proposal(
        item_id=item["id"], submitter=item.get("submitted_by", ""),
        name=item.get("subject", ""))


async def _resolve_slack_user_id(ctx, email: str) -> tuple[str | None, str]:
    """`(slack_user_id, status)` — `status` is one of `LOOKUP_FOUND` / `LOOKUP_NOT_FOUND` /
    `LOOKUP_FAILED`. An API failure (a timeout, a 5xx, a 429) must never be recorded as the SAME
    fact an honest `users_not_found` miss is — the caller decides what each status means; this
    function only reports which one actually happened, and still logs a failure loudly so an
    operator debugging a silent doorbell can find it in the process log.

    **Cached on `ctx.cache`, positive results only** (same posture as the forward `users.info`
    cache this same object already provides) — `users.lookupByEmail` is Tier-3 (~50/min), and
    running it once per (item, steward) on EVERY poll pass with no cache is twenty open items on a
    10-second loop, 120 calls/min, well past the limit. A caller that cannot tell a 429 from "no
    such person" then reads the whole workspace as steward-less for as long as the limit stays hot
    — which, with no cache to relieve the call volume, is indefinitely."""
    team_id = ctx.settings.team_id
    cached = ctx.cache.get_id_by_email(team_id, email)
    if cached is not None:
        return cached, LOOKUP_FOUND
    try:
        result = await ctx.gateway.users_lookup_by_email(email)
    except SlackApiError:
        log.error("steward doorbell: users.lookupByEmail failed for %s", email, exc_info=True)
        return None, LOOKUP_FAILED
    if result is None:
        return None, LOOKUP_NOT_FOUND
    slack_user_id = ((result.get("user") or {}).get("id")) or ""
    if not slack_user_id:
        return None, LOOKUP_NOT_FOUND
    ctx.cache.put_id_by_email(team_id, email, slack_user_id)
    return slack_user_id, LOOKUP_FOUND


def _record_delivered(ctx, *, kind: str, item_id: str, steward_email: str, event: str) -> None:
    """The positive half of the delivery record: "the steward never had to discover a pending item
    by remembering to look" is unmeasurable without it. A row lands in `job_runs` when delivery
    FAILS (`_record_undeliverable_once`), and `store.mark_notified` upserts a single row in place —
    so without this, not even a send HISTORY survives a second notification on the same item. One
    `audit_log` row per delivered DM, through the SAME `AuditWriter` seam every MCP-tool call
    already writes through (`ctx.audit`, wired once at process start by `app.build_context`;
    `None` only for a caller that never wired one, such as a test double), attributed to the
    NOTIFIED steward — the identity any report measuring this signal groups by."""
    if ctx.audit is None:
        return
    ctx.audit.write(identity=steward_email, tool="steward-doorbell", duration_ms=0.0,
                    outcome="ok", args={"item_kind": kind, "item_id": item_id, "event": event})


def _record_undeliverable_once(ctx, *, kind: str, item_id: str, steward_email: str,
                               undeliverable_state: str, event: str, item_ref: str,
                               reason: str) -> None:
    """An undeliverable OUTCOME is recorded through the SAME state-comparison mechanism a
    successful delivery uses (`store.last_notified_state`/`mark_notified`), keyed on an
    `"undeliverable:<class>"` state string (`_UNDELIVERABLE_PREFIX`) rather than an unconditional
    insert every pass. One row per (item, steward, reason CLASS) — not one per pass for as long as
    the condition persists — which closes the "one unresolvable steward x 20 items x 8,640
    passes/day = 172,800 `job_runs` rows a day" failure mode without a second table: it reuses
    `steward_notifications`, already keyed on exactly (item_kind, item_id, steward_email)."""
    last_state = store.last_notified_state(ctx.conn, item_kind=kind, item_id=item_id,
                                           steward_email=steward_email)
    if last_state == undeliverable_state:
        return
    review.record_undeliverable(ctx.conn, event=event, item_ref=item_ref, reason=reason)
    store.mark_notified(ctx.conn, item_kind=kind, item_id=item_id, steward_email=steward_email,
                        state=undeliverable_state)


async def _notify_item(ctx, item: dict, stewards_map: dict) -> int:
    kind, item_id = item["kind"], item["id"]
    item_ref = f"{kind}:{item_id}"
    event = _EVENT_NAMES.get(kind, kind)
    # Neither item kind (entity-proposal · parked-capture) is anchored to a zone — no page path
    # exists yet — so `resolve_stewards_for_scope` is asked with the empty scope, which by its own
    # contract can only ever match the universal `"*"` key. That is the scope the copy names too.
    display_scope = "*"
    stewards = review.resolve_stewards_for_scope(stewards_map, "")
    if not stewards:
        # No steward EMAIL resolves at all for this item's scope — there is no per-steward key to
        # dedup on, so the state lives against the empty steward (the item itself, scope-level).
        _record_undeliverable_once(
            ctx, kind=kind, item_id=item_id, steward_email="",
            undeliverable_state=f"{_UNDELIVERABLE_PREFIX}no-steward", event=event,
            item_ref=item_ref,
            reason=copy.doorbell_undeliverable_no_steward(scope=display_scope, event=event,
                                                          item_ref=item_ref))
        return 0

    state = _state_signature(item)
    sent = 0
    for email in stewards:
        last_state = store.last_notified_state(ctx.conn, item_kind=kind, item_id=item_id,
                                               steward_email=email)
        if last_state == state:
            continue   # already told, at this exact state
        slack_user_id, lookup_status = await _resolve_slack_user_id(ctx, email)
        if slack_user_id is None:
            if lookup_status == LOOKUP_NOT_FOUND:
                # An honest fact worth recording: THIS Slack workspace has no member at this
                # email. Deduped the same way as the no-steward case above.
                _record_undeliverable_once(
                    ctx, kind=kind, item_id=item_id, steward_email=email,
                    undeliverable_state=f"{_UNDELIVERABLE_PREFIX}{LOOKUP_NOT_FOUND}", event=event,
                    item_ref=item_ref,
                    reason=copy.doorbell_undeliverable_no_slack_identity(
                        email=email, scope=display_scope, event=event, item_ref=item_ref))
            # LOOKUP_FAILED: a transient API problem, already logged loudly above — NEVER recorded
            # as "no Slack identity", which would be a false and potentially permanent fact about
            # the person. Not recorded as an undeliverable outcome at all: the next pass simply
            # retries the lookup, exactly like a failed `chat.postMessage` below is retried rather
            # than marked notified.
            continue
        blocks, text = _render_for_item(item, is_resend=last_state is not None)
        try:
            # Slack's `chat.postMessage` opens a DM implicitly when `channel` is a user id — no
            # `conversations.open` round trip needed (Slack's own documented convention).
            await ctx.gateway.chat_post_message(slack_user_id, blocks=blocks, text=text)
        except SlackApiError:
            log.error("steward doorbell: could not DM %s about %s", email, item_ref,
                     exc_info=True)
            continue   # not marked notified — the next pass retries, same as poller.poll_once
        store.mark_notified(ctx.conn, item_kind=kind, item_id=item_id, steward_email=email,
                            state=state)
        _record_delivered(ctx, kind=kind, item_id=item_id, steward_email=email, event=event)
        sent += 1
    return sent


def _remediation(repo: str, baked: str) -> str:
    """What to actually DO, named per source. The two roads have different fixes and the wrong one
    wastes an operator's afternoon: a process holding a checkout re-reads `origin/main` on its next
    pass, so a push is enough; the deployed `app`/`slack` groups read a snapshot baked into the
    image, so a push alone changes nothing there until the next deploy."""
    if repo:
        return (f"commit and push ops/stewards.json in {repo} — it is re-read at origin/main's "
                f"tip on the next pass, so no deploy is needed")
    return (f"commit and push ops/stewards.json, then re-bake and redeploy (`make deploy-staging`) "
            f"— this process holds no checkout and reads the snapshot at {baked or '<unset>'}, so "
            f"a push alone changes nothing here")


def _record_configuration_fault(ctx, *, reason: str) -> None:
    """The ONE way this module reports a deployment-wide "nothing can ever ring" fact.

    Three conditions reach it — no source at all, an empty map, and a map that cannot be loaded —
    and they used to be three hand-written branches with three different treatments: two recorded
    and deduped, the third only logged, once per pass, forever. The module's docstring promises an
    undeliverable notification is recorded and never swallowed, and a malformed map is exactly as
    undeliverable as an empty one. One helper, one record shape, one dedup rule.

    Once per PROCESS: all three are global facts that cannot change while this process runs
    (`Settings` is frozen at startup), so the alternative is N items x 8,640 passes a day of
    identical rows.
    """
    if ctx._stewards_empty_warned:
        return
    log.warning("steward doorbell: %s", reason)
    review.record_undeliverable(ctx.conn, event="doorbell-configuration", item_ref="*",
                                reason=reason)
    ctx._stewards_empty_warned = True


async def poll_once(ctx) -> int:
    """One pass over every open review item. Returns how many DMs were actually sent (test seam,
    mirroring `poller.poll_once`'s own return contract)."""
    repo = ctx.settings.server.knowledge_repo
    baked = getattr(ctx.settings.server, "stewards_path", "") or ""
    if not repo and not baked:
        # Nothing to resolve against: no checkout AND no baked snapshot. This branch used to
        # `return 0` in SILENCE, which is how the defect stayed invisible — an item sat parked for
        # twenty minutes with `steward_notifications` empty, no `job_runs` row and nothing in the
        # logs, while the empty-map branch three blocks below logged loudly and recorded an
        # undeliverable for a configuration that is barely worse. This module's own docstring
        # promises "an undeliverable notification is recorded, never swallowed"; a deployment-wide
        # reason to deliver nothing at all is the one an operator most needs to find, and it is
        # recorded once per process lifetime for the same cost reason the empty-map branch is.
        _record_configuration_fault(
            ctx, reason="no stewards map on this deployment: neither a knowledge-repo checkout "
                        "nor a baked snapshot, so nothing can ever resolve to a steward. Bake "
                        "ops/stewards.json (scripts/deploy_staging.sh) or configure a repo")
        return 0
    try:
        stewards_map = _load_stewards_cached(ctx, repo, baked)
    except Exception as ex:  # noqa: BLE001 — a bad pass must never kill the poller's process
        log.error("steward doorbell: the stewards map could not be loaded", exc_info=True)
        _record_configuration_fault(
            ctx, reason=f"the stewards map could not be loaded ({ex.__class__.__name__}) — "
                        f"{_remediation(repo, baked)}")
        return 0

    if not stewards_map:
        # A COMPLETELY empty map is a single, global misconfiguration fact ("nobody is on call for
        # anything"), not a per-item one — letting every open item independently discover it and
        # write its OWN `record_undeliverable` row every pass costs N items x 8,640 passes/day.
        # This is exactly the day-one state: `ops/stewards.json` is untracked in the knowledge repo
        # until it is committed and pushed, and `load_stewards` reads it at `origin/main`. Logged,
        # and recorded, ONCE per process lifetime (`ctx._stewards_empty_warned`) rather than once
        # per item per pass.
        _record_configuration_fault(
            ctx, reason=f"the stewards map resolves to an EMPTY map — no scope resolves to a "
                        f"steward for any item. {_remediation(repo, baked)}")
        return 0

    sent = 0
    for item in review.items_for_doorbell(ctx.conn):
        sent += await _notify_item(ctx, item, stewards_map)
    return sent


async def run_doorbell(ctx, *, interval_s: int = 10, stop_event=None) -> None:
    """The loop `stigmergy.slack.app` runs as its OWN background task, alongside `poller.run_poller`
    — same shape (`asyncio.Event`-gated sleep between passes, one bad pass logged and swallowed
    rather than killing the loop), reused rather than re-invented, on the SAME process."""
    import asyncio

    stop_event = stop_event or asyncio.Event()
    while not stop_event.is_set():
        try:
            await poll_once(ctx)
        except Exception:  # noqa: BLE001 — one bad pass must never kill the process
            log.error("steward doorbell: pass failed", exc_info=True)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_s)
        except TimeoutError:
            pass
