"""The answer renderer — a PURE function `(answer_dict, link_resolver) -> blocks`. Purity is what
makes it testable with no Slack: every test in `tests/slack/test_render.py` calls these functions
directly on a hand-built `answer` dict and asserts on the returned Block Kit list — no gateway, no
event, no network.

`link_resolver` is a `Callable[[str], str | None]`: given a page path, the URL a browsable read
surface serves it at, or `None` when there is no such URL. Injected as configuration, never
imported — `stigmergy.slack.settings.no_link_resolver` is what is wired today, and a browsable
surface replaces that VALUE, not this module.

**Two properties this module exists to guarantee, both security-class:**

1. **A `partial` verdict can never render as `verified`.** `copy.verdict_line` is a literal dict
   keyed on the verdict string — a verdict it does not recognize raises `KeyError` rather than
   silently falling through to nothing, which is what makes "no rendering path can present a
   non-verified verdict as verified" testable rather than merely "the happy path looks right".
2. **`answer['confidence']` is never rendered at all** — `render_answer` does not read that key
   under any circumstance.

`escape_mrkdwn` runs on every piece of MODEL- or PAGE-derived free text (the answer body, a
citation quote/title, a refusal reason) BEFORE `to_mrkdwn`'s structural conversion — order matters:
escaping first protects any literal `&`/`<`/`>` the source text itself contained, and running it
AFTER `to_mrkdwn` would corrupt the `<url|text>` link syntax that conversion deliberately
introduces.
"""
import re
import uuid

# These constants deliberately do NOT come from the server module: a "pure Block Kit renderer"
# (this module's own job) importing the world (`stigmergy.librarian.*`, `stigmergy.entities.*`,
# `stigmergy.index.*`, `subprocess`, PyYAML) for a few string literals is exactly the coupling this
# module exists without. `stigmergy.review_kinds` is a dependency-free module at the bottom of the
# stack beside `stigmergy.text`, which both this module and `server.review` import with no
# import-graph cost either way.
from stigmergy.review_kinds import ENTITY_TYPES, KIND_ENTITY_PROPOSAL, KIND_PARKED_CAPTURE
from stigmergy.slack import copy
from stigmergy.slack.mrkdwn import escape_mrkdwn, to_mrkdwn

# The `action_id` every "Show it here" button carries — `stigmergy.slack.app`'s own `@app.action`
# listener matches on this exact string to know which block_actions payload is this affordance and
# not some future one.
SHOW_IT_HERE_ACTION_ID = "slack_show_page"


def _unbound_token(path: str, asker_slack_user_id: str) -> str:
    """The default `mint_token` — production ALWAYS passes `SlackContext.mint_show_it_here_token`
    instead (`stigmergy.slack.mention`'s two call sites); this exists only so a caller (chiefly a
    test exercising some OTHER property of `render_answer`, not the button itself) does not have to
    wire a real token store just to render an answer with a citation. Deterministically unusable: a
    random token with no server-side entry behind it looks up to nothing, so a caller that actually
    needs the button to be clickable must pass a real `mint_token`."""
    return uuid.uuid4().hex


# Slack's own ceiling for one `section` block's text. Enforced on the FINAL string, after every
# escape and interpolation, because that is the only length Slack measures.
SECTION_TEXT_MAX = 3000

# A trailing `&`, `&l`, `&g`, `&am`… left by cutting mid-entity. Dropped rather than served: a
# half-written entity renders as literal `&am` in the middle of a page excerpt.
_PARTIAL_ENTITY_RE = re.compile(r"&[a-z]{0,3}$")


def clamp_section_text(text: str, limit: int = SECTION_TEXT_MAX) -> str:
    """Hold one section's text to Slack's ceiling.

    Callers that clamp their own input BEFORE `escape_mrkdwn` are not clamping what Slack sees:
    escaping expands (`&` -> `&amp;` is 5x), so an entity-heavy page excerpt cut to 2800 characters
    arrived here at over 14000 and Slack rejected the WHOLE `blocks` payload with `invalid_blocks`
    — which the caller logs and swallows, so the person who clicked got nothing at all: no page, no
    refusal, no sign anything had happened.
    """
    if len(text) <= limit:
        return text
    return _PARTIAL_ENTITY_RE.sub("", text[:limit])


def _section(text: str) -> dict:
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}


def _context(text: str) -> dict:
    """`escape_mrkdwn` only escapes `&`/`<`/`>` — asterisks, headers, newlines and `to_mrkdwn`'s own
    link syntax all survive in MODEL-derived text, so a prompt-injected page can steer the agent
    into emitting, inside the answer body, a forged `*Sources*` header and a literal verdict
    sentence — visually indistinguishable from the real ones if everything renders as `section`
    blocks. Rendering the REAL Sources block and verdict line as `context` blocks instead gives
    them Slack's own smaller, grey chrome: a channel the answer body (always a `section`) cannot
    reach into, no matter what it contains. Structural, not string-scrubbing — a scrubber here
    would be exactly the proxy defense this project rejects."""
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": text}]}


_DIVIDER = {"type": "divider"}


def _render_markdown(raw: str) -> str:
    return to_mrkdwn(escape_mrkdwn(raw or ""))


def _show_it_here_button(path: str, asker_slack_user_id: str, mint_token) -> dict:
    """The button is on a message everyone in the channel can see — Slack has no notion of a button
    only one viewer may press — so "this affordance must not act for another channel member" is
    enforced SERVER-SIDE, at click time (`stigmergy.slack.replies.handle_show_it_here`). `mint_token`
    is injected exactly like `link_resolver` — this module never touches `SlackContext` itself —
    and returns an OPAQUE token. The value carries neither the path nor an email: anything put in a
    button value is retrievable by any workspace member via `conversations.history`, so an asker's
    email in cleartext here would be a disclosure.

    `block_id` is opaque for a DIFFERENT reason than the value token: Slack rejects an entire
    `blocks` payload outright when two blocks share one explicit `block_id`
    (`gateway._raise_if_invalid_blocks` mirrors this), and the old path-derived id
    (`f"show_it_here:{path}"`) collided the moment one page was cited twice — a recorded
    production failure. A random suffix can never collide, no matter how many buttons one render
    builds, so `_citation_blocks` dedupes buttons by path only to avoid showing the same affordance
    twice — not to keep block_id unique. Only `action_id` and the `value` token are ever read back
    (`replies.handle_show_it_here`), so nothing downstream depends on this string's shape."""
    token = mint_token(path, asker_slack_user_id)
    return {
        "type": "actions",
        "block_id": f"show_it_here:{uuid.uuid4().hex}"[:255],
        "elements": [{
            "type": "button",
            "text": {"type": "plain_text", "text": copy.SHOW_IT_HERE_LABEL},
            "action_id": SHOW_IT_HERE_ACTION_ID,
            "value": token,
        }],
    }


def _citation_blocks(citations: list[dict], link_resolver, asker_slack_user_id: str,
                     mint_token) -> list[dict]:
    """One Sources LINE per citation — every quote stays visible, even two of the same page — but
    at most one "Show it here" BUTTON per DISTINCT page, in first-occurrence order.
    `stigmergy.answer.synthesize.Citation` carries no uniqueness constraint on `path`, so a model may
    legally cite one page twice with two different quotes (observed live on staging); a
    button per citation would mint the identical page's affordance twice in one message — both
    buttons open the same page — for no reason a user could act on."""
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
    """`answer` is the dict `AnswerService.ask()` (or the MCP `ask` tool) returns — the SAME shape
    on every transport, in-process, one seam. `asker_slack_user_id` is who this render is FOR;
    `mint_token` (injected the same way as `link_resolver`) turns `(path, asker_slack_user_id)`
    into the opaque token a "Show it here" button's value carries — only invoked when a button is
    actually built (an unlinked citation).

    The answer BODY is the only `section` block here: the Sources block and the verdict line are
    `context` blocks behind a `divider`, so the bot's own trust chrome renders in a channel a
    prompt-injected body cannot imitate, no matter what it contains."""
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
    """The ONE place a fuller answer is ever acknowledged: the DM. A header block, then
    `render_answer` reused unchanged — the DM is a wider scope, not a different dialect."""
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


def render_filed(*, page_path: str, commit: str, anchor: str, source_page: str = "") -> list[dict]:
    return [_section(copy.filed(page_path=page_path, commit=commit,
                               anchor=escape_mrkdwn(anchor), source_page=source_page))]


def render_needs_input(*, situation_prose: str, slack_user_id: str) -> list[dict]:
    """`situation_prose` is `report['summary']` with the trailing MCP invocation stripped —
    code-composed by `librarian.report.needs_input`, but embedding the AGENT's own reading of
    captured material (the entity name candidate it could not resolve). Unescaped, a raw
    Slack-native `<https://evil.example|text>` in the submitted material's judged name renders as a
    REAL live link. Escaped BEFORE composition, not after: `copy.needs_input_body` composes a real
    `<@slack_user_id>` mention around this text, and escaping the WHOLE result afterward would
    corrupt that code-composed mention syntax the same way escaping after `to_mrkdwn` would corrupt
    a real link (this module's own docstring's ordering rule, applied to a second composed-markup
    case)."""
    return [_section(copy.needs_input_body(escape_mrkdwn(situation_prose),
                                           slack_user_id=slack_user_id))]


def render_generic_report(status: str, raw_summary: str) -> list[dict]:
    """`triage`/`rejected`/`resolved`/`failed`: the enum-first prefix BOLDED, the rest of
    `report['summary']` reused verbatim. `raw_summary` is `report['summary']` as
    `librarian.report`/`capture.dispositions` composed it — already starting with the literal
    `"{status} — "` prefix (`librarian.report`'s own hard rule); this function replaces exactly
    that prefix with its bolded form rather than composing a new sentence.

    **`escape_mrkdwn` alone, never `_render_markdown`.** `raw_summary` carries agent-classified
    `judged_type`/entity-name text (`librarian.report.triage_type`/`triage_entity`), so a
    `to_mrkdwn`-converting call here would turn attacker-chosen `[text](url)` in that text into a
    REAL live link — the same exposure as the doorbell's parked-capture card, just delivered to the
    SUBMITTER's own thread instead of the steward's DM. None of this surface's real summaries
    (`triage`/`rejected`/`resolved`/`failed` — never `needs_input`, which has its own render
    function above) use bold or link markdown deliberately; only backticks for code spans, which
    Slack's OWN mrkdwn already renders natively with no conversion needed."""
    prefix = f"{status} — "
    body = raw_summary[len(prefix):] if raw_summary.startswith(prefix) else raw_summary
    return [_section(f"*{status}* — {escape_mrkdwn(body)}")]


def render_reply_delivered() -> list[dict]:
    return [_section(copy.REPLY_DELIVERED)]


def render_reply_already_answered() -> list[dict]:
    return [_section(copy.REPLY_ALREADY_ANSWERED)]


def render_show_it_here_success(*, page_title: str, excerpt: str) -> list[dict]:
    return [_section(clamp_section_text(
        copy.show_it_here_success(escape_mrkdwn(page_title), escape_mrkdwn(excerpt))))]


def render_show_it_here_refusal(path: str) -> list[dict]:
    return [_section(copy.show_it_here_refusal(path))]


# ── the steward doorbell, and the review surface's Block Kit cards ───────────────────────────────
# Action-id convention (`stigmergy.slack.review` matches on these exact prefixes): `review:<kind>:
# <verdict>` fires `review_decide` immediately with no note; `review-modal:<kind>:<verdict>` opens
# a short modal collecting the one piece of free text that verdict requires (a note, or a reason)
# BEFORE calling it. `value` is always the bare item id — our own generated identifier, never
# untrusted text: nothing a caller typed ever becomes part of an action_id or a button value.
DIRECT_ACTION_PREFIX = "review:"
MODAL_ACTION_PREFIX = "review-modal:"
REVIEW_NOTE_MODAL_CALLBACK_ID = "review_note_modal"
REVIEW_NOTE_MODAL_BLOCK_ID = "note"
REVIEW_NOTE_MODAL_ACTION_ID = "note_text"


def direct_action_id(kind: str, verdict_token: str) -> str:
    return f"{DIRECT_ACTION_PREFIX}{kind}:{verdict_token}"


def modal_action_id(kind: str, verdict_token: str) -> str:
    return f"{MODAL_ACTION_PREFIX}{kind}:{verdict_token}"


def _actions(elements: list[dict], *, block_id: str) -> dict:
    return {"type": "actions", "block_id": block_id[:255], "elements": elements}


def _button(text: str, action_id: str, value: str, *, style: str | None = None) -> dict:
    button = {"type": "button", "text": {"type": "plain_text", "text": text},
             "action_id": action_id, "value": value}
    if style:
        button["style"] = style
    return button


def _link_button(text: str, url: str) -> dict:
    return {"type": "button", "text": {"type": "plain_text", "text": text}, "url": url}


def render_doorbell_parked_capture(*, item_id: str, summary: str) -> tuple[list[dict], str]:
    """`escape_mrkdwn` alone, never `_render_markdown` (`to_mrkdwn(escape_mrkdwn(...))`) — matching
    its sibling below. `to_mrkdwn` turns `[text](url)` into a REAL Slack hyperlink, and `text` here
    is `report['summary']`, which `server.service._neutralize_report`'s own docstring calls
    "DERIVED from captured material... untrusted text". Reproduced: a `judged_type` of
    `[Approve now](https://attacker.example/steal)` renders two live attacker-controlled links in a
    DM whose whole copy doctrine is "the single next action". The doorbell renders code-composed
    copy with one untrusted slot and never renders CommonMark on purpose, so no card here may call
    `to_mrkdwn`."""
    text = copy.doorbell_triage(item_id=item_id, summary=summary)
    blocks = [_section(escape_mrkdwn(text)),
             _actions([
                 _button(copy.REQUEUE_LABEL, direct_action_id(KIND_PARKED_CAPTURE, "requeue"),
                        item_id),
                 _button(copy.RESOLVE_LABEL, modal_action_id(KIND_PARKED_CAPTURE, "resolve"),
                        item_id),
                 _button(copy.REJECT_LABEL, modal_action_id(KIND_PARKED_CAPTURE, "reject"),
                        item_id, style="danger"),
             ], block_id=f"review:{KIND_PARKED_CAPTURE}:{item_id}")]
    return blocks, f"parked capture #{item_id} needs you"


def render_doorbell_entity_proposal(*, item_id: str, submitter: str, name: str) -> tuple[list[dict], str]:
    """`name` is the proposed entity's short name — lifted by the agent from PRIVATE captured
    material and published nowhere, so it is escaped like any other untrusted slot.

    **Approve opens a modal (ADR 030 D5)** — it used to fire directly (`direct_action_id`) and the
    DM echoed the CLI's `stigmergy-entities approve` command (`mint_command`, deleted server-side);
    now it mints on submit, so it needs the SAME metadata a mint needs first. Reject is unchanged:
    there is nothing to mint, so its modal still collects only a reason
    (`render_entity_mint_modal` below is Approve's own, distinct modal)."""
    text = copy.doorbell_entity_proposal(item_id=item_id, submitter=submitter, name=name)
    blocks = [_section(escape_mrkdwn(text)),
             _actions([
                 _button(copy.APPROVE_LABEL, modal_action_id(KIND_ENTITY_PROPOSAL, "approve"),
                        item_id, style="primary"),
                 _button(copy.REJECT_LABEL, modal_action_id(KIND_ENTITY_PROPOSAL, "reject"),
                        item_id, style="danger"),
             ], block_id=f"review:{KIND_ENTITY_PROPOSAL}:{item_id}")]
    return blocks, f"entity proposal #{item_id} needs a decision"


def render_note_modal(*, trigger_id: str, private_metadata: str, title: str, label: str,
                      placeholder: str = "", initial_context: str = "") -> dict:
    """One modal shape for every free-text collection this surface needs (a note, or a reason) —
    what is being recorded is a judgment, not a state transition, so the control is a composed
    sentence, never a checkbox. `private_metadata` carries which (item_kind, item_id, verdict) this
    submission is for — Slack's own mechanism for round-tripping state through a modal with no
    server-side store needed."""
    blocks = []
    if initial_context:
        blocks.append(_section(escape_mrkdwn(initial_context)))
    blocks.append({
        "type": "input",
        "block_id": REVIEW_NOTE_MODAL_BLOCK_ID,
        "label": {"type": "plain_text", "text": label},
        "element": {
            "type": "plain_text_input", "action_id": REVIEW_NOTE_MODAL_ACTION_ID,
            "multiline": True,
            **({"placeholder": {"type": "plain_text", "text": placeholder}} if placeholder else {}),
        },
    })
    return {
        "type": "modal",
        "callback_id": REVIEW_NOTE_MODAL_CALLBACK_ID,
        "private_metadata": private_metadata,
        "title": {"type": "plain_text", "text": title[:24] or copy.NOTE_MODAL_TITLE},
        "submit": {"type": "plain_text", "text": "Submit"},
        "close": {"type": "plain_text", "text": copy.NOT_YET_LEAVE_AS_DEVELOPING[:24]},
        "blocks": blocks,
    }


# ── the entity-proposal mint modal (ADR 030 D5) ───────────────────────────────────────────────────
# A second, distinct modal shape from `render_note_modal` above: that one collects ONE piece of
# free text (a note, a reason); this one collects the identity metadata a mint needs — a name, a
# closed-vocabulary type, two optional fields and a checkbox — so it cannot reuse `_MODAL_FIELD`'s
# `(field, label, placeholder)` shape. `stigmergy.slack.review` opens it in place of the generic note
# modal for exactly one (kind, verdict) pair: `(entity-proposal, approve)`.
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
    """One option per `stigmergy.review_kinds.ENTITY_TYPES` entry, label and value both the bare
    string — the same spelling `entities.generator`/every error message on this lane already uses,
    so nothing here can disagree with the closed vocabulary `entities.mint` actually enforces
    (`tests/test_architecture.py::test_review_kinds_entity_types_matches_the_generators_closed_list`
    is what keeps the restatement honest)."""
    return [{"text": {"type": "plain_text", "text": t}, "value": t} for t in ENTITY_TYPES]


def _requeue_option() -> dict:
    return {"text": {"type": "plain_text", "text": copy.ENTITY_MINT_REQUEUE_OPTION_LABEL},
           "value": ENTITY_MINT_REQUEUE_OPTION_VALUE}


def render_entity_mint_modal(*, trigger_id: str, private_metadata: str,
                             proposed_name: str = "") -> dict:
    """The entity-proposal Approve modal: the metadata a mint needs, collected once, submitted
    once (ADR 030 D5). `proposed_name` prefills `name` from the proposal's own unresolved subject —
    the SAME text the doorbell card itself already showed (`doorbell._render_for_item` ->
    `item["subject"]`; `stigmergy.slack.review._proposed_name_for` is the re-fetch this modal's own
    caller does at click time) — never a default a steward cannot see or override: the `name` field
    stays a plain, editable text input, exactly like an UNPREFILLED one would.

    `entity_type` is a `static_select` over the closed six (`_entity_type_options`) — never free
    text, so a submission can never carry a type `entities.mint` would refuse anyway. `aliases` is
    ONE comma-separated text field, not a repeating field — `server.review._alias_list` is what
    splits it, the same shape an MCP caller's own one comma-separated string already takes; `role`
    is one short free-text field. Both are optional.
    `requeue` is a single checkbox, PRE-CHECKED (`_requeue_option` in both `options` and
    `initial_options`) because approve-then-requeue is the ordinary flow — a steward who wants the
    capture left parked unchecks it. Both optional blocks are marked `optional: True` so an empty
    submission (no aliases/role typed, the checkbox unchecked) is valid Slack input, not a modal
    validation error.

    `entity_id` is deliberately NOT a field here — ADR 030 D5's own "one less field to mistype":
    the server prefills it from `name`'s own slug (`review._decide_entity_proposal`), and a steward
    who needs a different one uses `stigmergy-entities`/MCP directly, where `birth.prepare` validates
    it against a real collision check this form cannot run itself."""
    name_element = {"type": "plain_text_input", "action_id": ENTITY_MINT_NAME_ACTION_ID,
                    **({"initial_value": proposed_name} if proposed_name else {})}
    return {
        "type": "modal",
        "callback_id": ENTITY_MINT_MODAL_CALLBACK_ID,
        "private_metadata": private_metadata,
        "title": {"type": "plain_text", "text": copy.ENTITY_MINT_MODAL_TITLE[:24]},
        "submit": {"type": "plain_text", "text": copy.APPROVE_LABEL[:24]},
        "close": {"type": "plain_text", "text": copy.NOT_YET_LEAVE_AS_DEVELOPING[:24]},
        "blocks": [
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
