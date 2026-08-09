"""The page contract's two mechanical primitives: the size cap and the frontmatter scalar emitter.

Two things, and nothing else: the page-as-chunk numbers the librarian's splitter enforces, and
the YAML scalar emitter every frontmatter writer shares. Building a whole page belongs to whoever
writes one, not here.
"""
import re

import yaml

# The page-as-chunk contract, enforced by the knowledge repo's linter: a body over
# MAX_BODY_LINES is split into cross-linked parts (`librarian.processing`'s source-page splitter).
# SPLIT_CHUNK_LINES leaves room for the per-part chrome (H1, banner, continuation links) inside
# the cap.
MAX_BODY_LINES = 150
SPLIT_CHUNK_LINES = 140

# A scalar is emitted plain (unquoted) only when it provably round-trips: it matches a restricted
# charset AND yaml.safe_load reads it back as the identical string. The round-trip catches every
# YAML 1.1 implicit type — dates (2001-12-14), hex/binary/underscored ints (0x1F, 1_000), bool/null
# words (true/on/~) — that a hand-maintained pattern list silently misses; an implicit-typed scalar
# would re-type on read and the frontmatter (a contract the index and the linter both parse) would
# change meaning.
_PLAIN_YAML = re.compile(r"[A-Za-z0-9][\w .\-/]*", re.UNICODE)


def _yaml(v) -> str:
    s = str(v)
    if s and _PLAIN_YAML.fullmatch(s):
        try:
            if yaml.safe_load(s) == s:
                return s
        except (yaml.YAMLError, ValueError):
            # An invalid date ("0000-00-00", "2026-02-30") matches YAML's timestamp regex but makes
            # datetime.date() raise a bare ValueError; an over-limit int likewise. Either way the
            # scalar is not provably plain-safe — fall through and quote (which always round-trips).
            pass
    return '"' + "".join(_escape_char(ch) for ch in s) + '"'


def _escape_char(ch: str) -> str:
    """One character, escaped for a YAML double-quoted scalar.

    Every C0/C1 control gets an escape, not just the three with a friendly spelling. YAML forbids a
    RAW non-printable inside a quoted scalar, so emitting one made PyYAML refuse the whole document
    with `ReaderError` on read-back — breaking the "quote (which always round-trips)" promise the
    fall-through above is written on, for the one input class that cannot be spotted by eye.
    """
    if ch in _NAMED_ESCAPES:
        return _NAMED_ESCAPES[ch]
    code = ord(ch)
    if code < 0x20 or code == 0x7F or 0x80 <= code <= 0x9F:
        return f"\\x{code:02x}"
    return ch


_NAMED_ESCAPES = {"\\": "\\\\", '"': '\\"', "\n": "\\n", "\t": "\\t", "\r": "\\r"}
