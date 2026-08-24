"""User-facing Slack copy shared by renderers and handlers."""
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
PRIVATE_CHANNEL_REFUSAL = (
    "🧠 capture is not configured for this channel or this person. Ask the brain operator to "
    "map the channel to an audience you belong to."
)


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
