"""CommonMark -> Slack `mrkdwn`, a pure text transform, covering what this system actually emits:

  - bold      `**text**`    -> `*text*`
  - links     `[text](url)` -> `<url|text>`
  - lists     `- item`      -> `• item`   (`1. item` needs no change)
  - headings  `# text`      -> `*text*`   (mrkdwn has no headings; bold is the honest substitute)
  - code      inline/fenced spans byte-identical; only a fenced block's language tag is dropped

Code spans are carved out into placeholders before the prose transforms run and spliced back
after — a regex bold/link rewrite has no notion of "inside code" on its own. Deliberately NOT
handled: CommonMark tables (mrkdwn has none; the answering agent is told not to write one) and
single-`*`/`_` italics (ambiguous with the bold marker once `**` becomes `*`; left literal rather
than guessed at).
"""
import re

# A control character cannot collide with real content (`stigmergy.text.sanitize` strips them from
# page-derived text upstream), so splicing back is a plain positional replace, never a search for
# user-influenceable text.
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
    """Order matters: code spans are carved out FIRST (fenced before inline — a fenced block may
    contain a lone backtick), the prose transforms run on what's left, then every span is spliced
    back. Both carve-outs share ONE store: two lists would each count from zero, giving two spans
    the identical placeholder token and letting one silently clobber the other's slot."""
    text = markdown or ""
    store: list = []
    text = _carve(_FENCED_CODE_RE, text, store, _strip_fence_lang)
    text = _carve(_INLINE_CODE_RE, text, store)

    # A heading's own bold markers are DROPPED — mrkdwn has exactly one emphasis level, and
    # wrapping still-`**`-marked text yields asterisks that re-pair wrongly (`**A* and *B**`) and
    # render as stray literals.
    text = _HEADING_RE.sub(lambda m: f"*{_BOLD_RE.sub(r'\1', m.group(2))}*", text)
    text = _BULLET_RE.sub(lambda m: f"{m.group(1)}• ", text)
    text = _BOLD_RE.sub(lambda m: f"*{m.group(1)}*", text)
    text = _LINK_RE.sub(lambda m: f"<{m.group(2)}|{m.group(1)}>" if m.group(1) else f"<{m.group(2)}>",
                        text)

    for i, original in enumerate(store):
        text = text.replace(_PLACEHOLDER.format(i), original)
    return text


# Slack mrkdwn's own three special characters — `&`, `<`, `>` — must be entity-escaped in any
# text a client generates itself. `&` first, or the entities just inserted for `<`/`>` would be
# re-escaped.
def escape_mrkdwn(text: str) -> str:
    """Slack's required escaping for plain text interpolated into a mrkdwn string — never apply
    to `to_mrkdwn` output (it would double-escape the `<url|text>` link syntax that conversion
    introduces)."""
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
