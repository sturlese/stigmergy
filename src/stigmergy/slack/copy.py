"""Every user-facing string this transport ships. `render` and the handler modules call these
functions and never format their own copy — a wording change is a one-file change, and the
strings are pinnable by a test. Distinct states keep distinct strings: a transient identity failure is not an unmapped
user, and a capture that failed to queue is not one the librarian declined.
"""
# Who a user is pointed at when they need a human. A ROLE, never a name: this transport has no
# registry of people to look one up in, and copy naming somebody who has left the company is worse
# than copy naming nobody.
WHO_TO_ASK = "whoever runs this brain"


# ── identity — the no-access reply ──────────────────────────────────────────────────────────────
NO_ACCESS_CHANNEL = (f"I don't have access set up for you yet — ask {WHO_TO_ASK} to add you, "
                     "then try again.")
NO_ACCESS_DM = (f"I don't have access set up for you yet — ask {WHO_TO_ASK} to add you, then "
                "try again here.")

# A transient Slack API failure is NOT an unmapped user, and must not be reported as one.
TRANSIENT_IDENTITY_FAILURE = "I couldn't check your access just now — try again in a moment."


def no_access(*, is_dm: bool) -> str:
    return NO_ACCESS_DM if is_dm else NO_ACCESS_CHANNEL


# ── the placeholder and its endings ─────────────────────────────────────────────────────────────
PLACEHOLDER = "_thinking…_"

TIMEOUT = ("That took longer than it should have, so I've stopped waiting rather than leave this "
           f"hanging. Try asking again — if it keeps happening, tell {WHO_TO_ASK}.")


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
                  f"{WHO_TO_ASK}.")


# ── the 🧠 gesture — private channel / group DM / DM refusal ─────────────────────────────────────
PRIVATE_CHANNEL_REFUSAL = ("🧠 doesn't work here — a private channel's material would carry "
                          "permissions the brain can't yet translate into its own rules. Public "
                          "channels only, for now.")


def not_in_this_channels_groups(channel_name: str) -> str:
    """The 🧠 refusal when the reactor does not hold the groups this channel files at.

    A capture from a scoped channel is filed at that channel's groups (ADR 045 D2), and the door's
    rule is that you may file only what you could read afterwards — so somebody who cannot read
    the channel's material cannot capture it either. It names the CHANNEL, which they are already
    in and can see, and never the groups: which groups exist is not this message's to disclose.
    """
    where = f"#{channel_name}" if channel_name else "this channel"
    return (f"I can't capture that for you — {where} files into the brain at an audience you're "
            f"not in, so you wouldn't be able to read the page afterwards. Ask whoever looks "
            f"after the brain to add you, or ask someone in that audience to react instead.")


# ── the push channel ────────────────────────────────────────────────────────────────────────────
def filed(*, page_path: str, commit: str, anchor: str, source_page: str = "",
          anchor_reason: str = "", born: list[str] = ()) -> str:
    # `source_page` names the thread's own verbatim archive filed beside the synthesis; empty for
    # captures without one, and the card is unchanged.
    source_line = (f"Your thread is also archived word-for-word at `{source_page}`.\n\n"
                   if source_page else "")
    # `born` names the entities the librarian CREATED for this capture because the registry did
    # not know them — in the brain now, confirmed by the person who captured. Said here because the
    # registry grew on their say-so, and the sentence is where they learn it did.
    born_line = (f"It introduced {', '.join(f'*{name}*' for name in born)} as "
                 f"{'a new entity' if len(born) == 1 else 'new entities'} — created now and "
                 f"confirmed by you; nothing waits on anybody.\n\n"
                 if born else "")
    # Which entity a capture is about is a JUDGMENT (issue #77), so where one was explained the
    # explanation belongs beside the invitation to correct it: "if that's wrong, say so" is only
    # actionable for a reader who can see what the librarian thought. Empty for a capture that
    # named its entity plainly, which is most of them — a card that always carried an explanation
    # would train its reader past the one that matters.
    reason_clause = f" ({anchor_reason})" if anchor_reason else ""
    return (f"*filed* — this became a page: `{page_path}` @ `{commit}`\n\n"
            f"{source_line}{born_line}"
            f"The librarian read this as being about *{anchor}*{reason_clause} — if that's wrong, "
            f"tell {WHO_TO_ASK} so the page can be pointed at the right thing.\n\n"
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
           f"it keeps happening, tell {WHO_TO_ASK}{ref}.")


# Questions spend the `ask` bucket, stricter than the shared per-tool one. Each transport
# constructs its own limiter, so the budget is per surface — the copy must not promise otherwise.
RATE_LIMIT = ("You've hit the question limit — 10 questions a minute. Try again in a moment.")
