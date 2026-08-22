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


def _actions(elements: list[dict], *, block_id: str) -> dict:
    return {"type": "actions", "block_id": block_id, "elements": elements}


def _button(text: str, action_id: str, value: str, *, style: str | None = None) -> dict:
    """A Block Kit button. `text` is server-authored copy; `value` is only ever an id or an opaque
    token this package minted — never anything a person or a model wrote."""
    button = {"type": "button", "text": {"type": "plain_text", "text": text},
              "action_id": action_id, "value": value}
    if style:
        button["style"] = style
    return button


def _show_it_here_button(path: str, asker_slack_user_id: str, mint_token) -> dict:
    """Slack has no per-viewer buttons, so "must not act for another channel member" is enforced
    SERVER-SIDE at click time (`show_it_here.handle_show_it_here`). The value is an OPAQUE token
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
                 anchor_reason: str = "", born: list[str] = ()) -> list[dict]:
    """`anchor_reason` is the AGENT's sentence about a judged anchor, derived from captured
    material, so it is escaped exactly like `anchor` — unescaped, a `<https://evil.example|text>`
    inside it renders as a REAL live link in the card. `born` names the entities the librarian
    created for this capture: names lifted from the material, escaped the same way."""
    return [_section(copy.filed(page_path=page_path, commit=commit,
                               anchor=escape_mrkdwn(anchor), source_page=source_page,
                               anchor_reason=escape_mrkdwn(anchor_reason),
                               born=[escape_mrkdwn(str(name)) for name in born or ()]))]


def render_generic_report(status: str, raw_summary: str) -> list[dict]:
    """`rejected`/`resolved`/`failed`: the status prefix bolded, the rest of
    `report['summary']` reused verbatim (it already starts with the literal `"{status} — "`
    prefix). **`escape_mrkdwn` alone, never `_render_markdown`** — the summary carries
    agent-classified text, and `to_mrkdwn` would turn attacker-chosen `[text](url)` in it into a
    REAL live link. Real summaries use only backticks, which Slack renders natively."""
    prefix = f"{status} — "
    body = raw_summary[len(prefix):] if raw_summary.startswith(prefix) else raw_summary
    return [_section(f"*{status}* — {escape_mrkdwn(body)}")]


def render_show_it_here_success(*, page_title: str, excerpt: str) -> list[dict]:
    # `_section` clamps every section, this one included.
    return [_section(copy.show_it_here_success(escape_mrkdwn(page_title), escape_mrkdwn(excerpt)))]


def render_show_it_here_refusal(path: str) -> list[dict]:
    return [_section(copy.show_it_here_refusal(path))]
