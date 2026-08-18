"""The answer renderer — a PURE function `(answer_dict, link_resolver) -> blocks`, testable with
no gateway, no event, no network. `link_resolver` maps a page path to a URL or `None`; injected as
configuration (`settings.no_link_resolver` today), never imported.

Two guarantees, both security-class:

1. **A `partial` verdict can never render as `verified`** — `copy.verdict_line` is a literal dict
   lookup that raises on an unrecognized verdict rather than silently falling through.
2. **`answer['confidence']` is never rendered** — `render_answer` does not read that key under
   any circumstance.

`escape_mrkdwn` runs on every piece of MODEL- or PAGE-derived free text BEFORE `to_mrkdwn` —
running it after would corrupt the `<url|text>` link syntax the conversion introduces.
"""
import re
import uuid

# From `stigmergy.review_kinds`, deliberately not the server module: a pure Block Kit renderer
# must not drag `stigmergy.server.review`'s whole import graph in for a few string literals.
from stigmergy.review_kinds import ENTITY_TYPES, KIND_ENTITY_PROPOSAL, KIND_PARKED_CAPTURE
from stigmergy.slack import copy
from stigmergy.slack.mrkdwn import escape_mrkdwn, to_mrkdwn

# The `action_id` every "Show it here" button carries — `app`'s `@app.action` listener matches on
# this exact string.
SHOW_IT_HERE_ACTION_ID = "slack_show_page"


def _unbound_token(path: str, asker_slack_user_id: str) -> str:
    """The default `mint_token` — production always passes `SlackContext.mint_show_it_here_token`.
    Exists so a test exercising some other property of `render_answer` need not wire a token
    store; deterministically unusable (no server-side entry behind it), so a caller that needs a
    clickable button must pass a real `mint_token`."""
    return uuid.uuid4().hex


# Slack's own ceiling for one `section` block's text. Enforced on the FINAL string, after every
# escape and interpolation, because that is the only length Slack measures.
SECTION_TEXT_MAX = 3000

# A trailing `&`, `&l`, `&g`, `&am`… left by cutting mid-entity. Dropped rather than served: a
# half-written entity renders as literal `&am` in the middle of a page excerpt.
_PARTIAL_ENTITY_RE = re.compile(r"&[a-z]{0,3}$")

# What a reader sees where the text was cut. Italic, so it reads as the bot's own note rather than
# as part of the material, and unambiguous: silence here is indistinguishable from a short answer.
TRUNCATION_MARKER = "\n\n_[…truncated to fit Slack's block limit]_"


def clamp_section_text(text: str, limit: int = SECTION_TEXT_MAX) -> str:
    """Hold one section's text to Slack's ceiling, measured AFTER escaping — escaping expands
    (`&` -> `&amp;` is 5x), and an over-limit section makes Slack reject the WHOLE payload with
    `invalid_blocks`, which callers swallow: the person who clicked gets nothing at all. **A cut
    says so**: the dropped tail is exactly where a model puts its caveats, and a silently clamped
    answer is indistinguishable from a short one. The marker pays its own length out of the
    budget.
    """
    if len(text) <= limit:
        return text
    return _PARTIAL_ENTITY_RE.sub("", text[:limit - len(TRUNCATION_MARKER)]) + TRUNCATION_MARKER


def escape_and_clamp(text: str) -> str:
    """The same two steps every block on this surface takes, for the one message that is posted as
    a bare `text=` instead of as blocks: the steward's decision confirmation.

    Its sentence is composed from stored governance data — `review_decisions.actor`, naming whoever
    decided the item first, plus a steward's own typed note — and Slack renders a `text` field as
    mrkdwn exactly as it renders a section, so an unescaped `<url|label>` in there is a live link in
    a steward's DM. Clamped by the section ceiling too: a confirmation is nowhere near it, and one
    ceiling for the whole surface is what keeps this one habit rather than two.
    """
    return clamp_section_text(escape_mrkdwn(text))


def _section(text: str) -> dict:
    """Every section, clamped HERE — the ONE builder every section goes through, so the next
    caller cannot be the one that forgot. An unclamped section (the answer body is unbounded
    model output) fails the whole payload, and the text-only degrade that follows loses the
    citations, the buttons and the `context`-block trust chrome — a collapse a page author who
    can make an answer entity-dense can force.
    """
    return {"type": "section", "text": {"type": "mrkdwn", "text": clamp_section_text(text)}}


def _context(text: str) -> dict:
    """The trust chrome. `escape_mrkdwn` leaves asterisks, headers and newlines intact, so a
    prompt-injected answer body can emit a forged `*Sources*` header and verdict sentence —
    indistinguishable from the real ones if everything rendered as `section`s. The REAL Sources
    and verdict render as `context` blocks: Slack's smaller grey chrome, a channel the body
    (always a `section`) cannot reach into no matter what it contains. Structural, not
    string-scrubbing. Clamped like `_section`, because citation QUOTES are verbatim page text —
    a page AUTHOR can blow the ceiling even when the model does not.
    """
    return {"type": "context",
            "elements": [{"type": "mrkdwn", "text": clamp_section_text(text)}]}


_DIVIDER = {"type": "divider"}


def _render_markdown(raw: str) -> str:
    return to_mrkdwn(escape_mrkdwn(raw or ""))


def _show_it_here_button(path: str, asker_slack_user_id: str, mint_token) -> dict:
    """Slack has no per-viewer buttons, so "must not act for another channel member" is enforced
    SERVER-SIDE at click time (`replies.handle_show_it_here`). The value is an OPAQUE token
    (`mint_token`, injected like `link_resolver`): anything in a button value is retrievable by
    any workspace member via `conversations.history`, so an email in cleartext would be a
    disclosure. `block_id` carries a random suffix because Slack rejects a whole payload when two
    blocks share an explicit `block_id`, and a path-derived id collides the moment one page is
    cited twice; only `action_id` and the value token are ever read back."""
    token = mint_token(path, asker_slack_user_id)
    return _actions([_button(copy.SHOW_IT_HERE_LABEL, SHOW_IT_HERE_ACTION_ID, token)],
                    block_id=f"show_it_here:{uuid.uuid4().hex}")


def _citation_blocks(citations: list[dict], link_resolver, asker_slack_user_id: str,
                     mint_token) -> list[dict]:
    """One Sources LINE per citation — every quote stays visible — but at most one "Show it here"
    BUTTON per DISTINCT page, in first-occurrence order: a model may legally cite one page twice
    with two different quotes, and both buttons would open the same page."""
    if not citations:
        return []
    lines = []
    buttons = []
    buttoned_paths: set[str] = set()
    for c in citations:
        path, quote = c["path"], c["quote"]
        title = escape_mrkdwn(c.get("title") or path)
        clean_quote = escape_mrkdwn(quote)
        url = link_resolver(path)
        if url:
            lines.append(copy.citation_linked(url, title, clean_quote))
        else:
            lines.append(copy.citation_unlinked(title, clean_quote))
            if path not in buttoned_paths:
                buttoned_paths.add(path)
                buttons.append(_show_it_here_button(path, asker_slack_user_id, mint_token))
    blocks = [_context("*Sources*\n" + "\n".join(lines))]
    blocks.extend(buttons)
    return blocks


def render_answer(answer: dict, link_resolver, *, asker_slack_user_id: str = "",
                  mint_token=_unbound_token) -> list[dict]:
    """`answer` is the dict `AnswerService.ask()` returns — the SAME shape on every transport.
    `mint_token` (injected like `link_resolver`) is only invoked when a button is actually built.
    The answer BODY is the only `section` block here: the Sources block and the verdict line are
    `context` blocks behind a `divider` — trust chrome a prompt-injected body cannot imitate, no
    matter what it contains."""
    if answer.get("refused"):
        reason = escape_mrkdwn(answer.get("reason") or "")
        return [_section(copy.refusal(reason))]

    blocks = [_section(_render_markdown(answer.get("answer_markdown") or "")), _DIVIDER]
    blocks.extend(_citation_blocks(answer.get("citations") or [], link_resolver,
                                   asker_slack_user_id, mint_token))
    verdict = (answer.get("verdict") or {}).get("verdict", "")
    blocks.append(_context(copy.verdict_line(verdict)))
    return blocks


def render_dm_fuller_answer(*, channel_name: str, question: str, answer: dict, link_resolver,
                            asker_slack_user_id: str = "",
                            mint_token=_unbound_token) -> list[dict]:
    """The DM's fuller answer: a header block, then `render_answer` reused unchanged — the DM is
    a wider scope, not a different dialect."""
    header = _section(escape_mrkdwn(copy.dm_fuller_answer_header(channel_name, question)))
    return [header, *render_answer(answer, link_resolver, asker_slack_user_id=asker_slack_user_id,
                                   mint_token=mint_token)]


# ── the small, single-purpose renders (each one message) ─────────────────────────────────────────
def render_no_access(*, is_dm: bool) -> list[dict]:
    return [_section(copy.no_access(is_dm=is_dm))]


def render_transient_identity_failure() -> list[dict]:
    return [_section(copy.TRANSIENT_IDENTITY_FAILURE)]


def render_placeholder() -> list[dict]:
    return [_section(copy.PLACEHOLDER)]


def render_timeout() -> list[dict]:
    return [_section(copy.TIMEOUT)]


def render_server_error(short_id: str = "") -> list[dict]:
    return [_section(copy.server_error(short_id))]


def render_rate_limit() -> list[dict]:
    return [_section(copy.RATE_LIMIT)]


def render_capture_ack(display_name: str) -> list[dict]:
    return [_section(copy.capture_ack(escape_mrkdwn(display_name)))]


def render_capture_failed() -> list[dict]:
    return [_section(copy.CAPTURE_FAILED)]


def render_private_channel_refusal() -> list[dict]:
    return [_section(copy.PRIVATE_CHANNEL_REFUSAL)]


def render_filed(*, page_path: str, commit: str, anchor: str, source_page: str = "",
                 anchor_reason: str = "") -> list[dict]:
    """`anchor_reason` is the AGENT's sentence about a judged anchor, derived from captured
    material, so it is escaped exactly like `anchor` — unescaped, a `<https://evil.example|text>`
    inside it renders as a REAL live link in the card."""
    return [_section(copy.filed(page_path=page_path, commit=commit,
                               anchor=escape_mrkdwn(anchor), source_page=source_page,
                               anchor_reason=escape_mrkdwn(anchor_reason)))]


def render_needs_input(*, situation_prose: str, slack_user_id: str) -> list[dict]:
    """`situation_prose` embeds the AGENT's own reading of captured material: unescaped, a raw
    `<https://evil.example|text>` in the judged name renders as a REAL live link. Escaped BEFORE
    composition — `copy.needs_input_body` composes a real `<@slack_user_id>` mention around this
    text, which escaping the whole result afterward would corrupt."""
    return [_section(copy.needs_input_body(escape_mrkdwn(situation_prose),
                                           slack_user_id=slack_user_id))]


def render_generic_report(status: str, raw_summary: str) -> list[dict]:
    """`triage`/`rejected`/`resolved`/`failed`: the status prefix bolded, the rest of
    `report['summary']` reused verbatim (it already starts with the literal `"{status} — "`
    prefix). **`escape_mrkdwn` alone, never `_render_markdown`** — the summary carries
    agent-classified text, and `to_mrkdwn` would turn attacker-chosen `[text](url)` in it into a
    REAL live link. Real summaries use only backticks, which Slack renders natively."""
    prefix = f"{status} — "
    body = raw_summary[len(prefix):] if raw_summary.startswith(prefix) else raw_summary
    return [_section(f"*{status}* — {escape_mrkdwn(body)}")]


def render_reply_delivered() -> list[dict]:
    return [_section(copy.REPLY_DELIVERED)]


def render_reply_already_answered() -> list[dict]:
    return [_section(copy.REPLY_ALREADY_ANSWERED)]


def render_show_it_here_success(*, page_title: str, excerpt: str) -> list[dict]:
    # `_section` clamps every section, this one included.
    return [_section(copy.show_it_here_success(escape_mrkdwn(page_title), escape_mrkdwn(excerpt)))]


def render_show_it_here_refusal(path: str) -> list[dict]:
    return [_section(copy.show_it_here_refusal(path))]


# ── the steward doorbell, and the review surface's Block Kit cards ───────────────────────────────
# Action-id convention (`stigmergy.slack.review` matches these exact prefixes):
# `review:<kind>:<verdict>` fires `review_decide` immediately; `review-modal:<kind>:<verdict>`
# opens a modal collecting the one piece of free text that verdict requires first. `value` is
# always the bare item id — our own generated identifier, never text a caller typed.
DIRECT_ACTION_PREFIX = "review:"
MODAL_ACTION_PREFIX = "review-modal:"
REVIEW_NOTE_MODAL_CALLBACK_ID = "review_note_modal"
REVIEW_NOTE_MODAL_BLOCK_ID = "note"
REVIEW_NOTE_MODAL_ACTION_ID = "note_text"


def _direct_action_id(kind: str, verdict_token: str) -> str:
    return f"{DIRECT_ACTION_PREFIX}{kind}:{verdict_token}"


def _modal_action_id(kind: str, verdict_token: str) -> str:
    return f"{MODAL_ACTION_PREFIX}{kind}:{verdict_token}"


def _actions(elements: list[dict], *, block_id: str) -> dict:
    return {"type": "actions", "block_id": block_id[:255], "elements": elements}


def _button(text: str, action_id: str, value: str, *, style: str | None = None) -> dict:
    button = {"type": "button", "text": {"type": "plain_text", "text": text},
             "action_id": action_id, "value": value}
    if style:
        button["style"] = style
    return button


def render_doorbell_parked_capture(*, item_id: str, summary: str) -> tuple[list[dict], str]:
    """`escape_mrkdwn` alone, never `_render_markdown`: `summary` derives from captured material,
    and `to_mrkdwn` would turn a `judged_type` of `[Approve now](https://attacker.example/steal)`
    into live attacker-controlled links in a steward's DM. No card here may call `to_mrkdwn`."""
    text = copy.doorbell_triage(item_id=item_id, summary=summary)
    blocks = [_section(escape_mrkdwn(text)),
             _actions([
                 _button(copy.REQUEUE_LABEL, _direct_action_id(KIND_PARKED_CAPTURE, "requeue"),
                        item_id),
                 _button(copy.RESOLVE_LABEL, _modal_action_id(KIND_PARKED_CAPTURE, "resolve"),
                        item_id),
                 _button(copy.REJECT_LABEL, _modal_action_id(KIND_PARKED_CAPTURE, "reject"),
                        item_id, style="danger"),
             ], block_id=f"review:{KIND_PARKED_CAPTURE}:{item_id}")]
    return blocks, copy.doorbell_parked_capture_fallback(item_id=item_id)


def render_doorbell_entity_proposal(*, item_id: str, submitter: str, name: str) -> tuple[list[dict], str]:
    """`name` is the proposed entity's short name — lifted by the agent from PRIVATE captured
    material and published nowhere, so it is escaped like any other untrusted slot. Approve mints
    on submit, so it opens the mint-metadata modal (`render_entity_mint_modal`); Reject has
    nothing to mint, so its modal collects only a reason."""
    text = copy.doorbell_entity_proposal(item_id=item_id, submitter=submitter, name=name)
    blocks = [_section(escape_mrkdwn(text)),
             _actions([
                 _button(copy.APPROVE_LABEL, _modal_action_id(KIND_ENTITY_PROPOSAL, "approve"),
                        item_id, style="primary"),
                 _button(copy.REJECT_LABEL, _modal_action_id(KIND_ENTITY_PROPOSAL, "reject"),
                        item_id, style="danger"),
             ], block_id=f"review:{KIND_ENTITY_PROPOSAL}:{item_id}")]
    return blocks, copy.doorbell_entity_proposal_fallback(item_id=item_id)


def _doorbell_card(headline: str, item_line: str) -> tuple[list[dict], str]:
    """The buttonless frame both TERMINAL doorbell cards are edited into — decided, and superseded.
    No `actions` block at all, which is the whole point of either edit: a button left on a card
    that can no longer act is a control that only ever answers with a staleness refusal.

    `escape_mrkdwn` on the same terms as the two live cards above: `actor` is a resolved identity
    and `verdict`/`source` come from closed vocabularies, but this is a doorbell card and no card
    here interpolates anything unescaped — the rule is cheaper to keep than to re-audit per field.
    """
    return [_section(escape_mrkdwn(headline)), _context(escape_mrkdwn(item_line))], item_line


def render_doorbell_closed(*, kind: str, item_id: str, verdict: str, actor: str,
                           source: str) -> tuple[list[dict], str]:
    """The card a DECIDED item's DM is edited into: what was decided, by whom, through which door.
    `actor` comes straight out of the ledger, which no writer sanitizes."""
    return _doorbell_card(*copy.doorbell_closed(kind=kind, item_id=item_id, verdict=verdict,
                                                actor=actor, source=source))


def render_doorbell_superseded(*, kind: str, item_id: str) -> tuple[list[dict], str]:
    """The card a REPLACED item's DM is edited into — same frame, no verdict. It is reached when a
    real state change earns the item a second card: the first one is spent before the second is
    posted, because one `steward_notifications` row holds one pair of coordinates and the newer
    card's overwrite is what used to orphan the older message with its buttons still live."""
    return _doorbell_card(*copy.doorbell_superseded(kind=kind, item_id=item_id))


def render_note_modal(*, private_metadata: str, title: str, label: str,
                      placeholder: str = "") -> dict:
    """One modal shape for every free-text collection this surface needs (a note, or a reason) —
    what is being recorded is a judgment, so the control is a composed sentence, never a checkbox.
    `private_metadata` carries which (item_kind, item_id, verdict) the submission is for — Slack's
    own round-trip mechanism, no server-side store needed."""
    blocks = [{
        "type": "input",
        "block_id": REVIEW_NOTE_MODAL_BLOCK_ID,
        "label": {"type": "plain_text", "text": label},
        "element": {
            "type": "plain_text_input", "action_id": REVIEW_NOTE_MODAL_ACTION_ID,
            "multiline": True,
            **({"placeholder": {"type": "plain_text", "text": placeholder}} if placeholder else {}),
        },
    }]
    return {
        "type": "modal",
        "callback_id": REVIEW_NOTE_MODAL_CALLBACK_ID,
        "private_metadata": private_metadata,
        "title": {"type": "plain_text", "text": title[:24] or copy.NOTE_MODAL_TITLE},
        "submit": {"type": "plain_text", "text": "Submit"},
        "close": {"type": "plain_text", "text": copy.NOT_YET_LEAVE_AS_DEVELOPING[:24]},
        "blocks": blocks,
    }


# ── the entity-proposal mint modal ───────────────────────────────────────────────────────────────
# A second, distinct modal shape: the identity metadata a mint needs — a name, a closed-vocabulary
# type, two optional fields and a checkbox — so it cannot reuse `_MODAL_FIELD`'s
# `(field, label, placeholder)` shape. `stigmergy.slack.review` opens it for exactly one
# (kind, verdict) pair: `(entity-proposal, approve)`.
ENTITY_MINT_MODAL_CALLBACK_ID = "entity_mint_modal"
ENTITY_MINT_NAME_BLOCK_ID = "entity_name"
ENTITY_MINT_NAME_ACTION_ID = "entity_name_text"
ENTITY_MINT_TYPE_BLOCK_ID = "entity_type"
ENTITY_MINT_TYPE_ACTION_ID = "entity_type_select"
ENTITY_MINT_ALIASES_BLOCK_ID = "entity_aliases"
ENTITY_MINT_ALIASES_ACTION_ID = "entity_aliases_text"
ENTITY_MINT_ROLE_BLOCK_ID = "entity_role"
ENTITY_MINT_ROLE_ACTION_ID = "entity_role_text"
ENTITY_MINT_REQUEUE_BLOCK_ID = "entity_requeue"
ENTITY_MINT_REQUEUE_ACTION_ID = "entity_requeue_checkboxes"
# The checkboxes element's own single option value — never rendered, only round-tripped through
# `state_values["selected_options"]`, the same way a button's `value` never surfaces to a human.
ENTITY_MINT_REQUEUE_OPTION_VALUE = "requeue"


def _entity_type_options() -> list[dict]:
    """One option per `review_kinds.ENTITY_TYPES` entry, label and value both the bare string —
    the same spelling `entities.mint` enforces; the architecture drift test keeps the restatement
    honest."""
    return [{"text": {"type": "plain_text", "text": t}, "value": t} for t in ENTITY_TYPES]


def _requeue_option() -> dict:
    return {"text": {"type": "plain_text", "text": copy.ENTITY_MINT_REQUEUE_OPTION_LABEL},
           "value": ENTITY_MINT_REQUEUE_OPTION_VALUE}


def render_entity_mint_modal(*, private_metadata: str, unresolved_names: list[str] = (),
                             name_prefill: str = "") -> dict:
    """The entity-proposal Approve modal. Obeys `name_prefill` rather than counting: an empty
    prefill with `unresolved_names` non-empty IS the several-names case, so the names are listed
    and the field left empty. The decision lives in `entities.situations.mint_name_prefill`,
    which this module may not import — the value travels in the item dict. `entity_id` is
    deliberately not a field: only `birth.prepare` can run a real collision check."""
    names = [str(n) for n in (unresolved_names or []) if str(n).strip()]
    proposed_name = str(name_prefill or "")
    name_element = {"type": "plain_text_input", "action_id": ENTITY_MINT_NAME_ACTION_ID,
                    **({"initial_value": proposed_name} if proposed_name else {})}
    # No second count: an empty prefill with names still to place IS the several-names case —
    # that is what the one decision means by `""`.
    heading = ([_section(escape_mrkdwn(copy.entity_mint_several_unresolved(names=names)))]
               if not proposed_name and names else [])
    return {
        "type": "modal",
        "callback_id": ENTITY_MINT_MODAL_CALLBACK_ID,
        "private_metadata": private_metadata,
        "title": {"type": "plain_text", "text": copy.ENTITY_MINT_MODAL_TITLE[:24]},
        "submit": {"type": "plain_text", "text": copy.APPROVE_LABEL[:24]},
        "close": {"type": "plain_text", "text": copy.NOT_YET_LEAVE_AS_DEVELOPING[:24]},
        "blocks": [
            *heading,
            {
                "type": "input",
                "block_id": ENTITY_MINT_NAME_BLOCK_ID,
                "label": {"type": "plain_text", "text": copy.ENTITY_MINT_NAME_LABEL},
                "element": name_element,
            },
            {
                "type": "input",
                "block_id": ENTITY_MINT_TYPE_BLOCK_ID,
                "label": {"type": "plain_text", "text": copy.ENTITY_MINT_TYPE_LABEL},
                "element": {
                    "type": "static_select",
                    "action_id": ENTITY_MINT_TYPE_ACTION_ID,
                    "placeholder": {"type": "plain_text", "text": copy.ENTITY_MINT_TYPE_PLACEHOLDER},
                    "options": _entity_type_options(),
                },
            },
            {
                "type": "input",
                "block_id": ENTITY_MINT_ALIASES_BLOCK_ID,
                "optional": True,
                "label": {"type": "plain_text", "text": copy.ENTITY_MINT_ALIASES_LABEL},
                "element": {
                    "type": "plain_text_input",
                    "action_id": ENTITY_MINT_ALIASES_ACTION_ID,
                    "placeholder": {"type": "plain_text",
                                    "text": copy.ENTITY_MINT_ALIASES_PLACEHOLDER},
                },
            },
            {
                "type": "input",
                "block_id": ENTITY_MINT_ROLE_BLOCK_ID,
                "optional": True,
                "label": {"type": "plain_text", "text": copy.ENTITY_MINT_ROLE_LABEL},
                "element": {
                    "type": "plain_text_input",
                    "action_id": ENTITY_MINT_ROLE_ACTION_ID,
                    "placeholder": {"type": "plain_text", "text": copy.ENTITY_MINT_ROLE_PLACEHOLDER},
                },
            },
            {
                "type": "input",
                "block_id": ENTITY_MINT_REQUEUE_BLOCK_ID,
                "optional": True,
                "label": {"type": "plain_text", "text": copy.ENTITY_MINT_REQUEUE_LABEL},
                "element": {
                    "type": "checkboxes",
                    "action_id": ENTITY_MINT_REQUEUE_ACTION_ID,
                    "options": [_requeue_option()],
                    "initial_options": [_requeue_option()],
                },
            },
        ],
    }
