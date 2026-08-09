"""CommonMark -> Slack `mrkdwn`, a pure text transform. `answer_markdown` is CommonMark; Slack
speaks `mrkdwn`, which is a different language.

The differences that matter for what this system actually emits (the answering agent's
`answer_markdown`, and every hand-written copy string in `stigmergy.slack.copy`):

  - bold      `**text**`        -> `*text*`
  - links     `[text](url)`     -> `<url|text>`
  - lists     `- item`          -> `• item`      (Slack has no auto-bullet; `1. item` needs
                                                   no change — a leading digit already reads fine)
  - headings  `# text`          -> `*text*`      (mrkdwn has no headings; bold is the honest
                                                   substitute, never a bare hash left in-band)
  - code      `` `x` `` / fenced ``` blocks       syntax unchanged — mrkdwn's inline and fenced
                                                   code spans are byte-identical to CommonMark's;
                                                   only a fenced block's language tag is dropped

Code spans (inline and fenced) are protected from every other transform: a bold marker or a link
bracket INSIDE a code span must render literally, not be rewritten. This is done by carving code
spans out into placeholders before the prose transforms run, and splicing the original text back
in afterwards — the only correct order, since a regex-based bold/link rewrite has no notion of
"inside code" on its own.

Deliberately NOT handled, since the constructs this system emits are bold, links, lists, inline
code and fenced blocks: CommonMark tables (mrkdwn has none, and the answering agent's system
prompt already tells it not to write one) and single-asterisk/underscore italics (ambiguous with
the bold marker once `**` has been rewritten to `*`; left as literal text rather than guessed at).
"""
import re

# Placeholders can't collide with anything a real answer could contain: a control character
# (never valid in the markdown this system generates or accepts — `stigmergy.text.sanitize` already
# strips them from any page-derived text upstream) bracketing an ordinal, so splicing back is a
# plain positional replace, not a search for user-influenceable text.
_PLACEHOLDER = "\x00{}\x00"

_FENCED_CODE_RE = re.compile(r"```.*?\n?.*?```", re.S)
_INLINE_CODE_RE = re.compile(r"`[^`\n]+`")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*", re.S)
_LINK_RE = re.compile(r"\[([^\]\n]*)\]\(([^)\s]+)\)")
_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.*)$", re.MULTILINE)
_BULLET_RE = re.compile(r"^(\s*)[-*+][ \t]+", re.MULTILINE)


def _strip_fence_lang(fenced: str) -> str:
    """```lang\ncode\n``` -> ```\ncode\n``` — Slack does not syntax-highlight by language tag, and
    leaving one in means the tag renders as the first line of literal text inside the block."""
    match = re.match(r"```([^\n`]*)\n", fenced)
    if not match or not match.group(1).strip():
        return fenced
    return "```\n" + fenced[match.end():]


def _carve(pattern: re.Pattern, text: str, store: list, transform=lambda s: s) -> str:
    """Replace every match with a numbered placeholder, appending the (transformed) original to
    `store` in the same order — so splicing back is `store[i]` at each placeholder's index."""
    def _sub(m):
        store.append(transform(m.group(0)))
        return _PLACEHOLDER.format(len(store) - 1)
    return pattern.sub(_sub, text)


def to_mrkdwn(markdown: str) -> str:
    """The whole conversion, construct by construct. Order matters: code spans are carved out
    FIRST (so nothing inside one is ever touched), fenced before inline (a fenced block may
    contain a lone backtick that would otherwise look like an unterminated inline span), then the
    prose transforms run on what's left, then every carved span is spliced back.

    Both carve-outs share ONE store/index space (`store`), not two independent ones: two separate
    lists would each start counting from zero, so a fenced span at index 0 and an inline span at
    index 0 would carry the IDENTICAL placeholder token — and splicing one back would silently
    overwrite the other's slot. A single shared store makes every placeholder's index globally
    unique, so splice-back order does not matter and no span can clobber another's."""
    text = markdown or ""
    store: list = []
    text = _carve(_FENCED_CODE_RE, text, store, _strip_fence_lang)
    text = _carve(_INLINE_CODE_RE, text, store)

    # The heading's own bold markers are DROPPED, not kept: mrkdwn has exactly one emphasis level,
    # so a heading that is already bold cannot be bolded again. Wrapping the still-`**`-marked text
    # produced `***X***`, which `_BOLD_RE` below then re-paired from the left into `**X**` — and for
    # a heading with two bold runs (`### **A** and **B**`) into `**A* and *B**`, whose asterisks do
    # not pair at all, so Slack renders stray literal ones mid-answer.
    text = _HEADING_RE.sub(lambda m: f"*{_BOLD_RE.sub(r'\1', m.group(2))}*", text)
    text = _BULLET_RE.sub(lambda m: f"{m.group(1)}• ", text)
    text = _BOLD_RE.sub(lambda m: f"*{m.group(1)}*", text)
    text = _LINK_RE.sub(lambda m: f"<{m.group(2)}|{m.group(1)}>" if m.group(1) else f"<{m.group(2)}>",
                        text)

    for i, original in enumerate(store):
        text = text.replace(_PLACEHOLDER.format(i), original)
    return text


# Slack mrkdwn's own three special characters — `&`, `<`, `>` — must be entity-escaped in any text
# a client generates itself (Slack's docs: "as if it were HTML"), which is EVERY string this
# renderer builds from a template (a reason, a title, a quote) rather than from `to_mrkdwn` above
# (which already operates on text meant to already be mrkdwn-shaped prose, not raw user data
# destined for one slot). Escaping order matters: `&` first, or the entities just inserted for
# `<`/`>` would themselves be re-escaped.
def escape_mrkdwn(text: str) -> str:
    """Slack's required escaping for any plain text interpolated into a mrkdwn string — never
    apply this to output already produced by `to_mrkdwn` (it would double-escape the very
    characters that conversion introduces, e.g. the `<url|text>` link syntax)."""
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
