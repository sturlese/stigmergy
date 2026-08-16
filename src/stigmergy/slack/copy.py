"""Every user-facing string this transport ships. `render` calls these functions and never
formats its own copy — a wording change is a one-file change, and the strings are pinnable by a
test. Distinct states keep distinct strings: a transient identity failure is not an unmapped
user, and a capture that failed to queue is not one the librarian declined.
"""
import os

# Who a user is told to go to when they need a human; the default keeps the copy true for a
# deployment with no named steward.
STEWARD_NAME = os.environ.get("STIGMERGY_STEWARD_NAME", "your steward")


# ── identity — the no-access reply ──────────────────────────────────────────────────────────────
NO_ACCESS_CHANNEL = (f"I don't have access set up for you yet — ask {STEWARD_NAME} to add you, "
                     "then try again.")
NO_ACCESS_DM = (f"I don't have access set up for you yet — ask {STEWARD_NAME} to add you, then "
                "try again here.")

# A transient Slack API failure is NOT an unmapped user, and must not be reported as one.
TRANSIENT_IDENTITY_FAILURE = "I couldn't check your access just now — try again in a moment."


def no_access(*, is_dm: bool) -> str:
    return NO_ACCESS_DM if is_dm else NO_ACCESS_CHANNEL


# ── the placeholder and its endings ─────────────────────────────────────────────────────────────
PLACEHOLDER = "_thinking…_"

TIMEOUT = ("That took longer than it should have, so I've stopped waiting rather than leave this "
           f"hanging. Try asking again — if it keeps happening, tell {STEWARD_NAME}.")


# ── the verdict, rendered honestly ──────────────────────────────────────────────────────────────
VERDICT_LINES = {
    "verified": "_I could confirm everything above against the pages cited below._",
    "partial": ("_Heads up: one of the citations below couldn't be confirmed word-for-word "
               "against its source page — treat this one with a little more caution. Everything "
               "else checked out._"),
}


def verdict_line(verdict: str) -> str:
    """A literal lookup keyed on the verdict string: a verdict this dict does not carry is a
    rendering bug and raises, never silently flattens to a default."""
    return VERDICT_LINES[verdict]


def refusal(reason: str) -> str:
    return f"*I don't have that.* {reason}"


# ── citations and the "show it here" affordance ─────────────────────────────────────────────────
SHOW_IT_HERE_LABEL = "Show it here"


def citation_linked(url: str, title: str, quote: str) -> str:
    return f"• <{url}|{title}> — \"{quote}\""


def citation_unlinked(title: str, quote: str) -> str:
    return f"• {title} — \"{quote}\" _(not on the read site yet — Show it here)_"


def show_it_here_success(page_title: str, excerpt: str) -> str:
    return f"📄 *{page_title}*\n\n{excerpt}"


def show_it_here_fallback(page_title: str) -> str:
    """The plain-text `text=` companion of `show_it_here_success` — the notification line, and all
    a client that renders no blocks shows."""
    return f"📄 {page_title}"


def show_it_here_refusal(path: str) -> str:
    """The exact string `read_page` returns for both a nonexistent and an out-of-scope path —
    reused, not rephrased: two different sentences would make this affordance an oracle for which
    restricted pages exist."""
    return f"unknown page: {path}"


def dm_fuller_answer_header(channel_name: str, question: str) -> str:
    return (f"You asked in #{channel_name}: \"{question}\"\n\n"
            "Here's a fuller answer, based on what you can see beyond that channel:")


# ── the degrade leg — every blocks-carrying send refused, text-only is all that's left ──────────
def degraded_sources_line(titles: list[str]) -> str:
    """The text-only lane's compact citation line: titles only, no link, no quote, no button —
    this lane exists because Slack refused every `blocks` payload. `titles` arrive already
    escaped by the caller."""
    return "Sources: " + ", ".join(titles)


# ── the 🧠 gesture — the capture ack, and its failure ────────────────────────────────────────────
def capture_ack(display_name: str) -> str:
    return (f"🧠 queued and attributed to {display_name}. The librarian will look at this thread — "
            "you'll hear back right here when it's filed (or if it has a question for you first).")


# The capture attempt itself failed before it could be queued — distinct from a capture the
# librarian looked at and declined.
CAPTURE_FAILED = ("That didn't go through — something went wrong on my side, not with the "
                  "reaction itself. Try reacting again in a moment; if it keeps failing, tell "
                  f"{STEWARD_NAME}.")


# ── the 🧠 gesture — private channel / group DM / DM refusal ─────────────────────────────────────
PRIVATE_CHANNEL_REFUSAL = ("🧠 doesn't work here — a private channel's material would carry "
                          "permissions the brain can't yet translate into its own rules. Public "
                          "channels only, for now.")


# ── the push channel ────────────────────────────────────────────────────────────────────────────
def filed(*, page_path: str, commit: str, anchor: str, source_page: str = "") -> str:
    # `source_page` names the thread's own verbatim archive filed beside the synthesis; empty for
    # captures without one, and the card is unchanged.
    source_line = (f"Your thread is also archived word-for-word at `{source_page}`.\n\n"
                   if source_page else "")
    return (f"*filed* — this became a page: `{page_path}` @ `{commit}`\n\n"
            f"{source_line}"
            f"The librarian read this as being about *{anchor}* — if that's wrong, tell "
            f"{STEWARD_NAME} so the page can be pointed at the right thing.\n\n"
            "Heads up: this won't show up yet if you `@brain` a question about it — that catches "
            "up automatically tonight, not right away.")


def filed_fallback(*, page_path: str) -> str:
    """The `text=` companion of `filed`'s card: the notification line for a filed capture."""
    return f"filed: {page_path}"


def report_fallback(status: str) -> str:
    """The `text=` companion of every other reported status — the status word, then a noun for
    what happened, since the sentence itself is in the card."""
    return f"{status}: capture update"


NEEDS_INPUT_INSTRUCTION = "Just reply in this thread with your answer."


def needs_input_body(situation_prose: str, *, slack_user_id: str) -> str:
    """`situation_prose` is `report['summary']` with its trailing MCP invocation clause already
    stripped — the situation prose verbatim, only the closing instruction swapped. @-mentions the
    submitter since the thread may have other participants."""
    return f"<@{slack_user_id}> — {situation_prose}\n\n{NEEDS_INPUT_INSTRUCTION}"


# ── ask-back — the delivery confirmation, and a second reply ────────────────────────────────────
REPLY_DELIVERED = ("Got it — that's recorded as your answer, thanks. If it doesn't match anything "
                   "on file, a steward will take it from here, and you won't be asked again about "
                   "this one.")

REPLY_ALREADY_ANSWERED = ("This one's already been answered — I only ask once per capture, so I'm "
                          f"not using this reply. If something's changed, tell {STEWARD_NAME} "
                          "directly.")


# ── error states ────────────────────────────────────────────────────────────────────────────────
def server_error(short_id: str = "") -> str:
    ref = f" (ref: {short_id})" if short_id else ""
    return ("Something went wrong on my end, not with your question. Try again in a minute — if "
           f"it keeps happening, tell {STEWARD_NAME}{ref}.")


# Questions spend the `ask` bucket, stricter than the shared per-tool one. Each transport
# constructs its own limiter, so the budget is per surface — the copy must not promise otherwise.
RATE_LIMIT = ("You've hit the question limit — 10 questions a minute. Try again in a moment.")


# ── the steward doorbell ────────────────────────────────────────────────────────────────────────
# One shape (headline, one-line reason, one concrete next action), two fillings. Every filling
# ends with a link or a copy-pasteable command — never "check the inbox", which would relocate the
# question the doorbell exists to answer.
def doorbell_triage(*, item_id, summary: str) -> str:
    return (f"🔔 A capture is parked and needs you — #{item_id}\n"
            f"{summary}\n\n"
            f"`stigmergy-queue show {item_id}` for the details, then requeue, resolve or reject "
            f"it.")


def doorbell_entity_proposal(*, item_id, submitter: str, name: str) -> str:
    return (f"🔔 An entity needs a decision — #{item_id}\n"
            f'Material submitted by {submitter} seems to be about "{name}", and nothing '
            f"registered resolves to it.\n\n"
            f"`stigmergy-entities show {item_id}` for the details and the exact command to "
            f"approve or reject it.")


# The `text=` companion of each doorbell card: the DM's notification line, which must name the
# item without carrying any of the material the card itself is deliberately terse about.
def doorbell_parked_capture_fallback(*, item_id) -> str:
    return f"parked capture #{item_id} needs you"


def doorbell_entity_proposal_fallback(*, item_id) -> str:
    return f"entity proposal #{item_id} needs a decision"


# Read cold, later, by an operator debugging why a doorbell never rang: name WHY, and what could
# not happen — never a bare "delivery failed".
def doorbell_undeliverable_no_steward(*, scope: str, event: str, item_ref: str) -> str:
    return (f'steward-doorbell: no steward resolves for scope "{scope}" in ops/stewards.json — '
            f"the {event} for {item_ref} rang for nobody")


def doorbell_undeliverable_no_slack_identity(*, email: str, scope: str, event: str,
                                             item_ref: str) -> str:
    return (f'steward-doorbell: {email} (resolved for scope "{scope}") has no Slack identity in '
            f"this workspace — the {event} for {item_ref} could not be delivered")


# The CLOSE button of `render.render_note_modal`; `status: developing` is the maturity axis a
# steward declines to move by pressing it.
NOT_YET_LEAVE_AS_DEVELOPING = "Not yet — leave it as developing"

# A button on a doorbell card rendered by an OLDER deploy, whose (kind, verdict) this build no
# longer recognizes as needing a modal at all.
STALE_REVIEW_ACTION = (
    "This button is from an older version of this card and I don't recognize it anymore — open "
    "the item fresh (`stigmergy-queue show` or `stigmergy-entities show`) and act from there.")


# ── the review surface's own labels ─────────────────────────────────────────────────────────────
# Exactly what `render.py` renders; an entity proposal takes approve or reject only
# (`review._decide_entity_proposal` raises on anything else).
APPROVE_LABEL = "Approve"
REJECT_LABEL = "Reject"
REQUEUE_LABEL = "Requeue"
RESOLVE_LABEL = "Resolve"

NOTE_MODAL_TITLE = "Your own words"
REASON_LABEL = "Reason"
NOTE_LABEL = "Note"


# ── the entity-mint modal — Approve's own metadata form, and its confirmation ───────────────────
ENTITY_MINT_MODAL_TITLE = "Mint this entity"
ENTITY_MINT_NAME_LABEL = "Name"
ENTITY_MINT_TYPE_LABEL = "Entity type"
ENTITY_MINT_TYPE_PLACEHOLDER = "Choose a type"
ENTITY_MINT_ALIASES_LABEL = "Aliases"
ENTITY_MINT_ALIASES_PLACEHOLDER = "comma-separated, optional"
ENTITY_MINT_ROLE_LABEL = "Role"
ENTITY_MINT_ROLE_PLACEHOLDER = "a short description, optional"
ENTITY_MINT_REQUEUE_LABEL = "After minting"
ENTITY_MINT_REQUEUE_OPTION_LABEL = "Requeue the originating capture so it re-files against this entity"


def entity_mint_several_unresolved(*, names: list[str]) -> str:
    """The Approve modal's header when the proposal carries MORE THAN ONE unresolved name.

    The `Name` field is left EMPTY in that case and this says why. A prefill cannot be right here:
    one submission mints ONE entity, and the only single string covering several names is the
    joined display form (`entities.situations.subject_of`), which is not any of their real names —
    accepting it would push a garbled entity into the knowledge repo as a real, signed commit.
    """
    listed = "\n".join(f"• {name}" for name in names)
    return (f"This capture names {len(names)} entities the registry doesn't recognize:\n{listed}\n"
            f"They are minted one at a time. Type the single name you are approving now — the "
            f"others stay unresolved on this capture until each gets its own decision.")


def decision_recorded(*, verdict: str, kind: str, item_id: str, actor: str) -> str:
    """The confirmation for a decision `review_decide` composed no `message` of its own for."""
    return f"recorded: {verdict} on {kind} #{item_id} — {actor}"


def entity_minted(*, entity_id: str, name: str, commit: str, requeued: bool) -> str:
    """Callers pass the full sha; truncated to the short form here."""
    requeue_line = ("The originating capture was requeued — the librarian will file it against "
                    "this entity next." if requeued else
                    "The originating capture stays parked, as asked — requeue it by hand when "
                    "it's ready.")
    return (f'*minted* — "{name}" (`{entity_id}`) is now a page, pushed at `{commit[:12]}`.\n\n'
           f"{requeue_line}")
