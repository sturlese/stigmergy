"""Every user-facing string this transport ships. `render` and the handler modules call these
functions and never format their own copy — a wording change is a one-file change, and the
strings are pinnable by a test. Distinct states keep distinct strings: a transient identity failure is not an unmapped
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
            "you'll hear back right here when it's filed.")


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
def filed(*, page_path: str, commit: str, anchor: str, source_page: str = "",
          anchor_reason: str = "", proposed: list[str] = ()) -> str:
    # `source_page` names the thread's own verbatim archive filed beside the synthesis; empty for
    # captures without one, and the card is unchanged.
    source_line = (f"Your thread is also archived word-for-word at `{source_page}`.\n\n"
                   if source_page else "")
    # `proposed` names the entities the librarian CREATED for this capture because the registry
    # did not know them — already in the brain, unconfirmed until a steward looks. Said here so the
    # submitter knows nothing is waiting on them, and that a steward may still rename or merge it.
    proposed_line = (f"It proposed {', '.join(f'*{name}*' for name in proposed)} as "
                     f"{'a new entity' if len(proposed) == 1 else 'new entities'} — created now, "
                     f"and a steward confirms, merges or declines "
                     f"{'it' if len(proposed) == 1 else 'them'} later; nothing waits on you.\n\n"
                     if proposed else "")
    # Which entity a capture is about is a JUDGMENT (issue #77), so where one was explained the
    # explanation belongs beside the invitation to correct it: "tell a steward if that's wrong" is
    # only actionable for a reader who can see what the librarian thought. Empty for a capture that
    # named its entity plainly, which is most of them — a card that always carried an explanation
    # would train its reader past the one that matters.
    reason_clause = f" ({anchor_reason})" if anchor_reason else ""
    return (f"*filed* — this became a page: `{page_path}` @ `{commit}`\n\n"
            f"{source_line}{proposed_line}"
            f"The librarian read this as being about *{anchor}*{reason_clause} — if that's wrong, "
            f"tell {STEWARD_NAME} so the page can be pointed at the right thing.\n\n"
            "Heads up: this won't show up yet if you `@brain` a question about it — that catches "
            "up automatically tonight, not right away.")


def filed_fallback(*, page_path: str) -> str:
    """The `text=` companion of `filed`'s card: the notification line for a filed capture."""
    return f"filed: {page_path}"


def report_fallback(status: str) -> str:
    """The `text=` companion of every other reported status — the status word, then a noun for
    what happened, since the sentence itself is in the card."""
    return f"{status}: capture update"



# ── error states ────────────────────────────────────────────────────────────────────────────────
def server_error(short_id: str = "") -> str:
    ref = f" (ref: {short_id})" if short_id else ""
    return ("Something went wrong on my end, not with your question. Try again in a minute — if "
           f"it keeps happening, tell {STEWARD_NAME}{ref}.")


# Questions spend the `ask` bucket, stricter than the shared per-tool one. Each transport
# constructs its own limiter, so the budget is per surface — the copy must not promise otherwise.
RATE_LIMIT = ("You've hit the question limit — 10 questions a minute. Try again in a moment.")


# ── the steward doorbell ────────────────────────────────────────────────────────────────────────
# One shape (headline, what the librarian did, the next action as buttons), two fillings. Nothing
# here asks a steward to go and look something up first: the card carries what the decision needs.
MAX_DOORBELL_ANCHORED = 3


def doorbell_identity_proposal(*, name: str, entity_type: str, summary: str, aliases: list[str],
                               anchored_pages: list[str], anchored_total: int) -> str:
    lines = [f"🔔 The librarian proposed a new {entity_type or 'entity'}: *{name}*"]
    if aliases:
        lines.append(f"also spelled {', '.join(aliases)}")
    if summary:
        lines.append(summary)
    shown = list(anchored_pages)[:MAX_DOORBELL_ANCHORED]
    if shown:
        more = anchored_total - len(shown)
        lines.append("filed against it: " + ", ".join(f"`{p}`" for p in shown)
                     + (f" and {more} more" if more > 0 else ""))
    lines.append("It is already in the brain. Confirm it, say which registered entity it really "
                 "is, or decline it.")
    return "\n".join(lines)


def doorbell_alias_proposal(*, entity_name: str, alias: str) -> str:
    return (f"🔔 A new spelling for *{entity_name}*: \"{alias}\"\n"
            f"The librarian met it in a capture and anchored the page to {entity_name}. Confirm it "
            f"as one of its names, or decline it.")


# The `text=` companion of each doorbell card: the DM's notification line, which must name the
# item without carrying any of the material the card itself is deliberately terse about.
def doorbell_identity_proposal_fallback(*, item_id) -> str:
    return f"proposed entity {item_id} needs a decision"


def doorbell_alias_proposal_fallback(*, item_id) -> str:
    return f"proposed spelling {item_id} needs a decision"


def doorbell_closed(*, kind: str, item_id, verdict: str, actor: str, source: str) -> tuple[str, str]:
    """`(headline, item_line)` for a card the doorbell is closing because the item was decided.

    Returns BOTH lines rather than one string: they render as two different Block Kit blocks (a
    section and the smaller grey context chrome), and a renderer splitting a joined string on a
    newline would put the wording back in `render.py`, which is the one thing this module exists
    to prevent.

    It says WHO and WHERE, not just "decided": a steward looking at a card that changed under them
    needs to know whether they were beaten to it by a colleague or by their own other window.
    `source` is empty on decisions recorded before the ledger carried one, and the sentence still
    has to read.
    """
    door = f" via {source}" if source else ""
    return (f"✅ {verdict} — by {actor}{door}",
            f"{kind} {item_id} — decided elsewhere, so this card's buttons are gone. The full "
            f"record is in the review ledger.")


def doorbell_superseded(*, kind: str, item_id) -> tuple[str, str]:
    """`(headline, item_line)` for a card being REPLACED by a newer one about the same item — the
    shape `doorbell_closed` returns, and rendered by the same buttonless frame.

    It must not read as a verdict, because nothing was decided: the item moved on (reprocessed,
    re-parked) and this card's buttons would act on a stale reading of it. A steward who is not
    told where the live card went reads a card that lost its buttons as the item being dropped.
    """
    return ("🔄 Superseded — a newer card for this item is further down this DM",
            f"{kind} {item_id} — it changed since this card was sent, so the buttons are gone. "
            f"Act on the newer card.")


# Read cold, later, by an operator debugging why a doorbell never rang: name WHY, and what could
# not happen — never a bare "delivery failed".
def doorbell_undeliverable_no_steward(*, scope: str, event: str, item_ref: str) -> str:
    return (f'steward-doorbell: no steward resolves for scope "{scope}" in ops/stewards.json — '
            f"the {event} for {item_ref} rang for nobody")


def doorbell_undeliverable_no_slack_identity(*, email: str, scope: str, event: str,
                                             item_ref: str) -> str:
    return (f'steward-doorbell: {email} (resolved for scope "{scope}") has no Slack identity in '
            f"this workspace — the {event} for {item_ref} could not be delivered")


# The CLOSE button of the merge modal.
NOT_YET = "Not yet"

# A button on a doorbell card rendered by an OLDER deploy, whose (kind, verdict) this build no
# longer recognizes.
STALE_REVIEW_ACTION = (
    "This button is from an older version of this card and I don't recognize it anymore — open "
    "the inbox fresh (the console, or `stigmergy-entities pending`) and act from there.")


# ── the review surface's own labels ─────────────────────────────────────────────────────────────
# Exactly what `render.py` renders. An identity proposal takes approve, merge or decline; a
# spelling takes approve or decline (`server.review.VERDICTS_BY_KIND` refuses anything else).
APPROVE_LABEL = "Approve"
MERGE_LABEL = "Merge into…"
DECLINE_LABEL = "Decline"


# ── the merge modal — which registered entity the proposal really is ───────────────────────────
MERGE_MODAL_TITLE = "Merge into"
MERGE_SELECT_LABEL = "It is really this entity"
MERGE_SELECT_PLACEHOLDER = "Choose a registered entity"
MERGE_TYPED_LABEL = "Or type its registry id"
MERGE_TYPED_PLACEHOLDER = "e.g. acme-corp — optional if you chose one above"
MERGE_NEEDS_TARGET = ("Nothing was merged — pick a registered entity in the list, or type its "
                      "registry id, and submit again.")


def merge_modal_heading(*, name: str) -> str:
    return (f"*{name}* was proposed by the librarian. Merging folds it into an entity that already "
            f"exists: its name and spellings become that entity's aliases, its page is removed, "
            f"and every page filed against it moves over.")


def decision_recorded(*, verdict: str, kind: str, item_id: str, actor: str) -> str:
    """The confirmation for a decision `review_decide` composed no `message` of its own for."""
    return f"recorded: {verdict} on {kind} {item_id} — {actor}"
