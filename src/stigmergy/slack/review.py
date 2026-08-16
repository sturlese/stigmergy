"""The Slack review surface: buttons on a doorbell DM that call `review_decide`, and the modals
some verdicts require first — a short one for the one piece of free text a note/reason needs, and
a longer one for the metadata an entity-proposal Approve mints from. **The bot enforces
nothing**: it resolves who is asking and calls the SAME `review_decide_safe` every MCP caller
calls, so `review_decide`'s own validation stays the one place a decision is made. Two item kinds
reach a human here: a parked capture and an entity proposal. `items_for_doorbell` is the one read
this module reuses (never re-queried a second way), so the entity-mint modal can prefill the
proposal's own name at click time.
"""
import json
import logging

from stigmergy.server import review
from stigmergy.slack import copy, render
from stigmergy.slack.gateway import SlackApiError
from stigmergy.slack.identity import Resolved

log = logging.getLogger(__name__)

# Which (kind, verdict_token) pairs need a modal before `review_decide` can be called at all, and
# what that modal collects: `(field_kwarg, label, placeholder)`. Every OTHER button on this
# surface fires directly with no note — friction only where a human sentence is actually required.
_MODAL_FIELD = {
    ("parked-capture", "resolve"): ("notes", copy.NOTE_LABEL,
                                    "what did you do with the material?"),
    ("parked-capture", "reject"): ("notes", copy.REASON_LABEL, ""),
    ("entity-proposal", "reject"): ("notes", copy.REASON_LABEL, ""),
}


def _parse_action_id(action_id: str) -> tuple[str, str, str] | None:
    """`(mode, kind, verdict_token)` from `review:<kind>:<verdict>` (fire directly) or
    `review-modal:<kind>:<verdict>` (open a modal first) — `None` for any action_id this module
    does not own, so a listener handling other actions can fall through cleanly."""
    for prefix, mode in ((render.MODAL_ACTION_PREFIX, "modal"),
                        (render.DIRECT_ACTION_PREFIX, "direct")):
        if action_id.startswith(prefix):
            rest = action_id[len(prefix):]
            kind, sep, verdict_token = rest.partition(":")
            if sep:
                return mode, kind, verdict_token
    return None


def _confirmation_text(result: dict, *, kind: str, item_id: str, verdict: str) -> str:
    """`result["minted"]` gets its OWN confirmation, naming the entity and the short commit —
    `_decide_entity_proposal` composes no `message` key for that outcome, so the generic
    `copy.decision_recorded` fallback would name neither."""
    if "error" in result:
        return result["error"]
    if result.get("minted"):
        return copy.entity_minted(entity_id=result.get("entity_id", ""),
                                  name=result.get("name", ""), commit=result.get("commit", ""),
                                  requeued=bool(result.get("requeued")))
    return (result.get("message")
           or copy.decision_recorded(verdict=verdict, kind=kind, item_id=item_id,
                                     actor=result.get("actor", "")))


async def _post_decision_confirmation(ctx, *, channel_id: str, result: dict, kind: str,
                                      item_id: str, verdict: str, what: str) -> None:
    """Post `review_decide`'s own confirmation (or clean refusal) back to the steward — decided
    in ONE place; every confirmation is the same plain message, whatever the verdict."""
    text = _confirmation_text(result, kind=kind, item_id=item_id, verdict=verdict)
    await ctx.post_or_log(ctx.gateway.chat_post_message(channel_id, text=text), what=what)


async def _decide_and_confirm(ctx, service, *, channel_id: str, kind: str, item_id: str,
                              verdict: str, what: str, **decide_kwargs) -> None:
    """Make the decision and say what happened — including when it goes wrong.
    `review_decide_safe` converts CLEAN refusals into a result dict; an UNANTICIPATED exception
    still propagates and is caught here as a GENERIC failure message. Silence instead is
    expensive: `_decide_entity_proposal` mints and PUSHES before it records the decision, so an
    untold steward retries Approve and hits a collision refusal for an entity they were never
    told they had created.
    """
    try:
        result = review.review_decide_safe(service, item_kind=kind, item_id=item_id,
                                           verdict=verdict, **decide_kwargs)
    except Exception:
        log.error("slack review: review_decide failed for %s:%s", kind, item_id, exc_info=True)
        await ctx.post_or_log(
            ctx.gateway.chat_post_message(channel_id, blocks=render.render_server_error(),
                                          text=copy.server_error()),
            what=f"review-decide server-error for {kind}:{item_id}")
        return
    await _post_decision_confirmation(ctx, channel_id=channel_id, result=result, kind=kind,
                                      item_id=item_id, verdict=verdict, what=what)


async def _open_modal(ctx, *, trigger_id: str, view: dict, what: str) -> None:
    """`views.open`, failure logged and swallowed — never raised into the listener (Slack's
    `trigger_id` is single-use and expires quickly, so a failed open cannot be retried with the
    same one anyway). The ONE place every modal-opening branch below calls, so a Slack outage while
    opening either modal degrades the same way."""
    try:
        await ctx.gateway.views_open(trigger_id=trigger_id, view=view)
    except SlackApiError:
        log.error("slack review: could not open %s", what, exc_info=True)


def _mint_modal_inputs(conn, item_id: str) -> tuple[list[str], str]:
    """The entity proposal's unresolved names AND the prefill decided for them, for the mint modal.

    Both come off the same item: `subjects`, the per-name list, never the `subject` display string
    the doorbell card shows (`situations.subject_of` joins several names with ", ", and this value
    decides what a steward MINTS); and `mint_name_prefill`, which `entities.situations` already
    decided — this surface reads it, and `render` renders it, so neither counts the names again.

    `items_for_doorbell` is the management-shaped, unscoped read this needs: a steward approving
    someone else's proposal must see ITS names, not their own ACL-scoped `review_queue` (which
    would hide it — a steward may be scoped, and self-approval is refused, so the row the steward
    is about to act on was never theirs to begin with).

    `([], "")` when the item can no longer be found among the OPEN items (already decided, or
    disposed of between the doorbell DM and this click) — the modal still opens, with an empty
    field a steward can fill by hand; `review_decide`'s own validation is what actually enforces
    the field is non-empty on submit, exactly as it would for a steward who never saw a doorbell
    card at all."""
    for item in review.items_for_doorbell(conn):
        if item["kind"] == "entity-proposal" and item["id"] == item_id:
            return list(item.get("subjects") or []), str(item.get("mint_name_prefill") or "")
    return [], ""


def _text_value(state_values: dict, block_id: str, action_id: str) -> str:
    """A `plain_text_input`'s typed value, `""` for an omitted OPTIONAL field — the one shape every
    free-text field on this surface's modals reads state through, whether the modal has one field
    (`render_note_modal`) or several (`render_entity_mint_modal`)."""
    block = (state_values or {}).get(block_id) or {}
    return ((block.get(action_id) or {}).get("value") or "").strip()


def _selected_option_value(state_values: dict, block_id: str, action_id: str) -> str:
    """A `static_select`'s chosen option value, `""` if somehow nothing was selected (Slack's own
    modal validation already refuses submission with a REQUIRED `static_select` unset, so this is a
    defensive default, not a path this surface's own UI can actually reach)."""
    block = (state_values or {}).get(block_id) or {}
    selected = (block.get(action_id) or {}).get("selected_option") or {}
    return str(selected.get("value") or "")


def _checkbox_checked(state_values: dict, block_id: str, action_id: str) -> bool:
    """Whether a single-option `checkboxes` element is checked — `selected_options` is Slack's own
    list of chosen options, empty when none are (an `optional: True` block is what lets a steward
    submit it unchecked at all)."""
    block = (state_values or {}).get(block_id) or {}
    return bool((block.get(action_id) or {}).get("selected_options"))


async def handle_block_action(ctx, *, action_id: str, value: str, trigger_id: str,
                              channel_id: str, slack_user_id: str, event_team_id: str) -> None:
    """One button click on a doorbell card. `value` is always the bare item id this surface's OWN
    render functions put there — never text a caller supplied: nothing untrusted ever becomes a
    button value on this surface.

    `private_metadata` carries only WHAT the eventual decision is about (item kind, id, verdict,
    field, where to post the confirmation) — never WHO is making it. The click that opens this
    modal is itself resolved from this call's own `slack_user_id`/`event_team_id` (Slack's
    authoritative identity for THIS interaction); the modal's own submission re-resolves identity
    independently, from ITS OWN authoritative body, rather than trusting a value this code stamped
    and round-tripped through the client.
    """
    parsed = _parse_action_id(action_id)
    if parsed is None:
        return   # not one of this module's actions
    mode, kind, verdict_token = parsed
    item_id = value

    identity_result = await ctx.resolve_slack_identity(event_team_id=event_team_id,
                                                       slack_user_id=slack_user_id)
    if not isinstance(identity_result, Resolved):
        return   # silently declined — same posture `handle_show_it_here` takes for an identity
                 # failure; a steward whose own identity fails to resolve gets no feedback that
                 # leaks whether the button itself was valid.

    if mode == "modal":
        # An entity-proposal Approve needs the mint-metadata modal, not the generic note one — it
        # is checked FIRST, ahead of `_MODAL_FIELD`, because it has no `(field, label, placeholder)`
        # entry to look up there at all.
        if kind == "entity-proposal" and verdict_token == "approve":
            metadata = json.dumps({"item_kind": kind, "item_id": item_id,
                                   "channel_id": channel_id})
            names, name_prefill = _mint_modal_inputs(ctx.conn, item_id)
            view = render.render_entity_mint_modal(
                private_metadata=metadata, unresolved_names=names, name_prefill=name_prefill)
            await _open_modal(ctx, trigger_id=trigger_id, view=view,
                              what=f"the entity-mint modal for {item_id}")
            return

        # A button from an OLDER deploy can carry a (kind, verdict) this build no longer maps to a
        # modal field. Subscripting `_MODAL_FIELD[...]` would raise `KeyError` straight out of the
        # handler for that case (caught only by `app.py`'s generic last-resort backstop, which
        # logs an incident but tells the steward nothing). `.get(...)` plus an explicit, worded
        # decline keeps this a normal, informative refusal instead of an opaque failure.
        modal_field = _MODAL_FIELD.get((kind, verdict_token))
        if modal_field is None:
            log.error("slack review: no modal field configured for stale action %s:%s", kind,
                     verdict_token)
            await ctx.post_or_log(
                ctx.gateway.chat_post_message(channel_id, text=copy.STALE_REVIEW_ACTION),
                what=f"stale review action {kind}:{verdict_token}")
            return
        field, label, placeholder = modal_field
        metadata = json.dumps({
            "item_kind": kind, "item_id": item_id, "verdict": verdict_token,
            "field": field, "channel_id": channel_id,
        })
        view = render.render_note_modal(private_metadata=metadata, title=copy.NOTE_MODAL_TITLE,
                                        label=label, placeholder=placeholder)
        await _open_modal(ctx, trigger_id=trigger_id, view=view,
                          what=f"the note modal for {kind}:{item_id}")
        return

    service = ctx.build_service(identity_result.email, identity_result.audiences)
    await _decide_and_confirm(
        ctx, service, channel_id=channel_id, kind=kind, item_id=item_id, verdict=verdict_token,
        what=f"review-decide confirmation for {kind}:{item_id}")


async def handle_note_modal_submission(ctx, *, private_metadata: str, state_values: dict,
                                       slack_user_id: str, event_team_id: str) -> None:
    """The modal's `view_submission` — `private_metadata` is Slack's own round-trip mechanism for
    which (item_kind, item_id, verdict, field) this text is FOR; no server-side store needed
    (unlike the "Show it here" token, which protects a value visible to every channel member — a
    modal's private_metadata is never shown to anyone but the submitter).

    **`slack_user_id`/`event_team_id` name WHO is submitting, and come from the listener's own
    authoritative `body` at submission time — never from `private_metadata`.** Stamping them into
    `private_metadata` when the modal is OPENED and round-tripping them back here would make the
    submitter a value this code itself wrote, not a fact Slack is asserting about who just clicked
    Submit — inverting this codebase's own load-bearing rule (private_metadata carries WHAT a
    decision is about, never WHO is making it) on the one surface that writes a governance
    verdict."""
    try:
        metadata = json.loads(private_metadata or "{}")
    except json.JSONDecodeError:
        log.error("slack review: modal private_metadata was not valid JSON")
        return
    kind = metadata.get("item_kind", "")
    item_id = metadata.get("item_id", "")
    verdict = metadata.get("verdict", "")
    channel_id = metadata.get("channel_id", "") or slack_user_id

    text_value = _text_value(state_values, render.REVIEW_NOTE_MODAL_BLOCK_ID,
                             render.REVIEW_NOTE_MODAL_ACTION_ID)

    identity_result = await ctx.resolve_slack_identity(event_team_id=event_team_id,
                                                       slack_user_id=slack_user_id)
    if not isinstance(identity_result, Resolved):
        return

    service = ctx.build_service(identity_result.email, identity_result.audiences)
    await _decide_and_confirm(
        ctx, service, channel_id=channel_id, kind=kind, item_id=item_id, verdict=verdict,
        what=f"review-decide (modal) confirmation for {kind}:{item_id}", notes=text_value)


async def handle_entity_mint_modal_submission(ctx, *, private_metadata: str, state_values: dict,
                                              slack_user_id: str, event_team_id: str) -> None:
    """The entity-mint modal's `view_submission` — `handle_note_modal_submission`'s sibling above,
    to the letter: the SAME `private_metadata` discipline (WHAT the decision is about — item kind
    and id, and where to post the confirmation — never WHO), and the SAME re-resolution of the
    acting identity from THIS call's own authoritative `slack_user_id`/`event_team_id`, never from
    the metadata or from anything the modal round-tripped. The verdict is always `"approve"` — this
    modal exists for no other reason — so, unlike the generic note modal (one handler shared by
    several (kind, verdict) pairs), it is never carried in `private_metadata` at all.

    `entity_id` is never collected or forwarded here: `review._decide_entity_proposal` prefills it
    from `name`'s own slug, and a steward who needs a different one uses `stigmergy-entities`/MCP
    directly, where `birth.prepare` validates it against a real collision check."""
    try:
        metadata = json.loads(private_metadata or "{}")
    except json.JSONDecodeError:
        log.error("slack review: entity-mint modal private_metadata was not valid JSON")
        return
    kind = metadata.get("item_kind", "")
    item_id = metadata.get("item_id", "")
    channel_id = metadata.get("channel_id", "") or slack_user_id

    name = _text_value(state_values, render.ENTITY_MINT_NAME_BLOCK_ID,
                       render.ENTITY_MINT_NAME_ACTION_ID)
    entity_type = _selected_option_value(state_values, render.ENTITY_MINT_TYPE_BLOCK_ID,
                                         render.ENTITY_MINT_TYPE_ACTION_ID)
    aliases = _text_value(state_values, render.ENTITY_MINT_ALIASES_BLOCK_ID,
                          render.ENTITY_MINT_ALIASES_ACTION_ID)
    role = _text_value(state_values, render.ENTITY_MINT_ROLE_BLOCK_ID,
                       render.ENTITY_MINT_ROLE_ACTION_ID)
    requeue = _checkbox_checked(state_values, render.ENTITY_MINT_REQUEUE_BLOCK_ID,
                                render.ENTITY_MINT_REQUEUE_ACTION_ID)

    identity_result = await ctx.resolve_slack_identity(event_team_id=event_team_id,
                                                       slack_user_id=slack_user_id)
    if not isinstance(identity_result, Resolved):
        return

    service = ctx.build_service(identity_result.email, identity_result.audiences)
    await _decide_and_confirm(
        ctx, service, channel_id=channel_id, kind=kind, item_id=item_id, verdict="approve",
        what=f"entity-mint confirmation for {kind}:{item_id}", name=name,
        entity_type=entity_type, aliases=aliases, role=role, requeue=requeue)
