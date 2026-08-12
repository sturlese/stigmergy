"""The steward's doorbell: a capture parking in `triage`, or an entity proposal, rings the
steward's Slack DM. A second `asyncio` background task in the SAME `slack` process as the poller —
never a fourth process group. It never claims, leases or mutates a queue row: every read goes
through `stigmergy.server.review.items_for_doorbell`.

Three properties, each load-bearing:

- **One notification per (item, steward), re-sent only on a state change** — `_state_signature`
  compared and recorded via `store.last_notified_state`/`mark_notified`, in send-then-mark order:
  a post that fails leaves nothing recorded, so the next pass retries it.
- **An undeliverable notification is recorded, never swallowed** — no steward resolving for the
  scope, or a resolved steward with no Slack identity here, writes a `job_runs` row
  (`review.record_undeliverable`) naming the event and the reason.
- **No material excerpt for a capture the librarian has not yet looked at** — a BREVITY rule,
  distinct from `capture.schema.withheld_reason`'s SECURITY rule (the doorbell is terse by
  design, not because the steward lacks access), and enforced HERE fail-closed
  (`_summary_for_doorbell`) rather than trusted to the upstream status filter
  `_collect_open_items` happens to apply: that filter serves `review_queue`'s own purpose, and
  one edit to it must not hand this module a `report['summary']` the secrets/PII gate never ran
  over.
"""
import logging

from stigmergy.server import review
from stigmergy.slack import copy, render, store
from stigmergy.slack.gateway import SlackApiError

log = logging.getLogger(__name__)

# `review.load_stewards` is a real `git fetch origin main` per call — unbounded, that is one
# fetch per poll pass. This TTL is for the doorbell only: `review._is_steward` (the authorization
# check behind `review_decide`) calls `load_stewards` directly and stays always-fresh on purpose —
# a revoked steward's approval must never succeed off a stale cache — so this TTL must never be
# wired into it.
_STEWARDS_CACHE_TTL_S = 300


def _load_stewards_cached(ctx, repo: str, baked_path: str = "") -> dict:
    """`review.load_stewards(repo, baked_path)` served from `ctx._stewards_cache` for
    `_STEWARDS_CACHE_TTL_S` — LOCAL to the doorbell's own steward resolution (see above). Bounds a
    real `git fetch` where a checkout exists, a plain file read of the baked snapshot on the
    deployed groups, and caps a fetch failure's log spam at once per TTL window."""
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

# The state-signature prefix an UNDELIVERABLE outcome is recorded under — no real item state
# begins with this literal, so the two namespaces share one `steward_notifications` column safely.
_UNDELIVERABLE_PREFIX = "undeliverable:"

# `review.KIND_*` -> the noun `job_runs` and the undeliverable copy name as "the {event}".
_EVENT_NAMES = {
    review.KIND_PARKED_CAPTURE: "capture-parked notification",
    review.KIND_ENTITY_PROPOSAL: "entity-proposal notification",
}


def _state_signature(item: dict) -> str:
    """A small, stable fingerprint of "what a steward would be told right now" — re-sent only
    when it changes. Deliberately NOT the whole item dict (timestamps tick on their own).

    **`attempts` is folded in wherever the item carries it.** A parked capture's status alone is
    a two-value alphabet, so requeue-then-reprocess back into the SAME status — the ordinary
    outcome requeue exists for — would fingerprint identically and the bell would never ring
    again. `attempts` (`capture_queue`'s monotonic per-delivery fence, incremented only by a real
    reprocessing claim) makes that a real state change; an item without the key degrades to the
    status-only shape — inert, not wrong."""
    kind = item["kind"]
    attempts = item.get("attempts")
    attempts_suffix = f"@{attempts}" if attempts is not None else ""
    if kind == review.KIND_PARKED_CAPTURE:
        return item.get("status", "") + attempts_suffix
    return (item.get("situation", "") or "parked") + attempts_suffix


def _summary_for_doorbell(item: dict) -> str:
    """The parked-capture summary, fail-closed at the point of rendering: `store.withheld_reason`
    asked from the item's OWN status, never trusted to `_collect_open_items`' status filter (see
    the module docstring). Passing `None` for the report reads as "not flagged" for
    `triage`/`needs_input` — the only statuses this item kind is expected to carry — and as
    withheld, fail closed, for every status the doorbell was never designed to see."""
    status = item.get("status", "")
    withheld = store.withheld_reason(status, None)
    return withheld or item.get("summary", "")


def _render_for_item(item: dict, *, is_resend: bool) -> tuple[list[dict], str]:
    """`(blocks, plain_text_fallback)` — never reads a capture's raw material or an entity
    proposal's rationale (module docstring). `is_resend` is accepted and currently unused: a card
    is re-sent verbatim or not at all."""
    kind = item["kind"]
    if kind == review.KIND_PARKED_CAPTURE:
        return render.render_doorbell_parked_capture(item_id=item["id"],
                                                      summary=_summary_for_doorbell(item))
    return render.render_doorbell_entity_proposal(
        item_id=item["id"], submitter=item.get("submitted_by", ""),
        name=item.get("subject", ""))


async def _resolve_slack_user_id(ctx, email: str) -> tuple[str | None, str]:
    """`(slack_user_id, status)`, `status` one of `LOOKUP_FOUND`/`LOOKUP_NOT_FOUND`/
    `LOOKUP_FAILED`. An API failure must never be recorded as the SAME fact an honest
    `users_not_found` miss is; failures are still logged loudly for an operator debugging a
    silent doorbell. Cached on `ctx.cache`, positive results only — `users.lookupByEmail` is
    Tier-3 (~50/min), and uncached per-(item, steward)-per-pass calls turn a transient rate limit
    into a sustained one that reads the whole workspace as steward-less indefinitely."""
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
    """The positive half of the delivery record: one `audit_log` row per delivered DM, through
    the SAME `AuditWriter` seam every MCP-tool call writes through, attributed to the NOTIFIED
    steward. `mark_notified` upserts in place, so without this no send history survives a second
    notification — and "the steward never had to discover a pending item by remembering to look"
    is unmeasurable."""
    if ctx.audit is None:
        return
    ctx.audit.write(identity=steward_email, tool="steward-doorbell", duration_ms=0.0,
                    outcome="ok", args={"item_kind": kind, "item_id": item_id, "event": event})


def _record_undeliverable_once(ctx, *, kind: str, item_id: str, steward_email: str,
                               undeliverable_state: str, event: str, item_ref: str,
                               reason: str) -> None:
    """An undeliverable OUTCOME recorded through the SAME state-comparison mechanism a delivery
    uses, keyed on an `"undeliverable:<class>"` state — one `job_runs` row per (item, steward,
    reason CLASS), never one per pass for as long as the condition persists, and no second
    table."""
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
    # Neither item kind is anchored to a zone — no page path exists yet — so the empty scope can
    # only ever match the universal `"*"` key, which is the scope the copy names too.
    display_scope = "*"
    stewards = review.resolve_stewards_for_scope(stewards_map, "")
    if not stewards:
        # No steward EMAIL resolves at all — no per-steward key to dedup on, so the state lives
        # against the empty steward.
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
                # An honest fact worth recording: THIS workspace has no member at this email.
                _record_undeliverable_once(
                    ctx, kind=kind, item_id=item_id, steward_email=email,
                    undeliverable_state=f"{_UNDELIVERABLE_PREFIX}{LOOKUP_NOT_FOUND}", event=event,
                    item_ref=item_ref,
                    reason=copy.doorbell_undeliverable_no_slack_identity(
                        email=email, scope=display_scope, event=event, item_ref=item_ref))
            # LOOKUP_FAILED: transient, already logged — never recorded as "no Slack identity"
            # (a false, potentially permanent fact about the person), and not recorded at all:
            # the next pass retries the lookup, like a failed post below.
            continue
        blocks, text = _render_for_item(item, is_resend=last_state is not None)
        try:
            # `chat.postMessage` opens the DM implicitly when `channel` is a user id — Slack's
            # own documented convention, no `conversations.open` round trip.
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
    """What to actually DO, named per source — the two roads have different fixes: a process
    holding a checkout re-reads `origin/main` on its next pass, so a push is enough; the deployed
    groups read a snapshot baked into the image, so a push alone changes nothing until a
    redeploy."""
    if repo:
        return (f"commit and push ops/stewards.json in {repo} — it is re-read at origin/main's "
                f"tip on the next pass, so no deploy is needed")
    return (f"commit and push ops/stewards.json, then re-bake and redeploy (`make deploy-staging`) "
            f"— this process holds no checkout and reads the snapshot at {baked or '<unset>'}, so "
            f"a push alone changes nothing here")


def _record_configuration_fault(ctx, *, reason: str) -> None:
    """The ONE way this module reports a deployment-wide "nothing can ever ring" fact — no
    stewards source, an empty map, or a map that cannot be loaded: one record shape, one dedup
    rule. Once per PROCESS: all three are global facts frozen at startup, and the alternative is
    N items x thousands of passes a day of identical rows.
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
        # No checkout AND no baked snapshot: nothing can ever resolve to a steward. Recorded, not
        # silent — a deployment-wide reason to deliver nothing is the fault an operator most
        # needs to find.
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
        # A COMPLETELY empty map is ONE global misconfiguration fact, not a per-item one — and it
        # is the day-one state, until `ops/stewards.json` is committed and pushed. Recorded once
        # per process lifetime, never once per item per pass.
        _record_configuration_fault(
            ctx, reason=f"the stewards map resolves to an EMPTY map — no scope resolves to a "
                        f"steward for any item. {_remediation(repo, baked)}")
        return 0

    sent = 0
    for item in review.items_for_doorbell(ctx.conn):
        sent += await _notify_item(ctx, item, stewards_map)
    return sent


async def run_doorbell(ctx, *, interval_s: int = 10, stop_event=None) -> None:
    """The loop `app` runs as its own background task beside `poller.run_poller` — same shape:
    `asyncio.Event`-gated sleep between passes, one bad pass logged and swallowed."""
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
