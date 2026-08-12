"""The page contract's two mechanical primitives: the page-as-chunk size cap and the frontmatter
scalar emitter. Building a whole page belongs to whoever writes one, not here.
"""
import re

import yaml

# The page-as-chunk contract, enforced by the knowledge repo's linter: a body over MAX_BODY_LINES
# is split into cross-linked parts. SPLIT_CHUNK_LINES leaves room for per-part chrome inside the cap.
MAX_BODY_LINES = 150
SPLIT_CHUNK_LINES = 140

# A scalar is emitted plain (unquoted) only when it provably round-trips: it matches a restricted
# charset AND yaml.safe_load reads it back as the identical string. The round-trip catches every
# YAML 1.1 implicit type (dates, hex/underscored ints, true/on/~) a hand-maintained pattern list
# silently misses — an implicit-typed scalar would re-type on read and change the frontmatter's
# meaning.
_PLAIN_YAML = re.compile(r"[A-Za-z0-9][\w .\-/]*", re.UNICODE)


def _yaml(v) -> str:
    s = str(v)
    if s and _PLAIN_YAML.fullmatch(s):
        try:
            if yaml.safe_load(s) == s:
                return s
        except (yaml.YAMLError, ValueError):
            # ValueError too: an invalid date ("2026-02-30") matches YAML's timestamp regex but
            # makes datetime.date() raise; an over-limit int likewise. Not provably plain-safe —
            # fall through and quote (which always round-trips).
            pass
    return '"' + "".join(_escape_char(ch) for ch in s) + '"'


def _escape_char(ch: str) -> str:
    """One character, escaped for a YAML double-quoted scalar.

    Every C0/C1 control gets an escape, not just the three with a friendly spelling: YAML forbids
    a RAW non-printable inside a quoted scalar, and emitting one makes PyYAML refuse the whole
    document on read-back — breaking the "quoting always round-trips" promise above.
    """
    if ch in _NAMED_ESCAPES:
        return _NAMED_ESCAPES[ch]
    code = ord(ch)
    if code < 0x20 or code == 0x7F or 0x80 <= code <= 0x9F:
        return f"\\x{code:02x}"
    return ch


_NAMED_ESCAPES = {"\\": "\\\\", '"': '\\"', "\n": "\\n", "\t": "\\t", "\r": "\\r"}
