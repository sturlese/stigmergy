"""Cut a stored page into fragments by length and line structure only: never by meaning.

Wiki pages split at H2 and H3 headings, sources at every heading; a short page is one fragment, a
long section is cut into overlapping windows, and a section too short to stand alone joins the next.
Cuts fall on paragraph and line boundaries; a long line is the one place a cut falls between words.
Spans are character offsets into the body, so every fragment is an exact slice and consecutive
spans overlap or touch: the fragments reassemble the body. Sizes are UTF-8 bytes, which bound the
embedding model's tokens in any script.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from itertools import accumulate

SHORT_PAGE_WORDS = 400
SECTION_MAX_WORDS = 400
WINDOW_WORDS = 350
OVERLAP_WORDS = 50
OVERLAP_CAP_WORDS = 100
MIN_SECTION_WORDS = 40
MAX_FRAGMENT_BYTES = 4000
# A cut piece's title, trail, and text together stay within `WHOLE_SAFE_BYTES`.
TITLE_CHARS = 200
HEADING_CHARS = 120
TRAIL_CHARS = 300

_HEADING = re.compile(r"^(#{1,6})[ \t]+(\S.*?)[ \t#]*$")
_REFERENCE = re.compile(r"^(?P<path>.+\.md)#(?P<number>[1-9][0-9]{0,5})$")
_FENCE = re.compile(r"^\s{0,3}(```|~~~)")
_WORD = re.compile(r"\S+")


@dataclass(frozen=True)
class Fragment:
    ordinal: int
    start: int
    end: int
    trail: str
    text: str


@dataclass
class _Line:
    start: int
    end: int
    words: int
    blank: bool
    size: int          # UTF-8 bytes


@dataclass
class _Section:
    first: int
    last: int          # exclusive line index
    trail: str


def reference(path: str, number: int) -> str:
    """How a reader names fragment `number` (1-based, in reader order) of the page at `path`."""
    return f"{path}#{number}"


def parse_reference(text: str) -> tuple[str, int | None]:
    """`(page path, fragment number)` for `path#n`; `(text, None)` for anything else."""
    match = _REFERENCE.match(text)
    return (match["path"], int(match["number"])) if match else (text, None)


def embed_text(title: str, fragment: Fragment) -> str:
    """The text a fragment is embedded as: its page title, its heading trail, and its words."""
    head = f"{title[:TITLE_CHARS]} › {fragment.trail}" if fragment.trail else title[:TITLE_CHARS]
    return f"{head}\n{fragment.text}"


def fragment(body: str, *, source: bool) -> list[Fragment]:
    """The fragments of one stored body, in order."""
    if not body:
        return []
    lines = _lines(body)
    if sum(line.words for line in lines) <= SHORT_PAGE_WORDS and _size(body) <= MAX_FRAGMENT_BYTES:
        return [Fragment(0, 0, len(body), "", body)]
    spans: list[tuple[int, int, str]] = []
    for section in _merged(_sections(body, lines, source=source), lines):
        spans.extend(_windows(lines, section))
    return [Fragment(index, start, end, trail, body[start:end]) for index, (start, end, trail) in enumerate(spans)]


def _size(text: str) -> int:
    return len(text.encode("utf-8"))


def _lines(body: str) -> list[_Line]:
    """Every line with its offsets; a line longer than a window becomes pieces cut between words."""
    lines: list[_Line] = []
    offset = 0
    for raw in body.splitlines(keepends=True):
        end = offset + len(raw)
        words = len(_WORD.findall(raw))
        size = _size(raw)
        if words <= WINDOW_WORDS and size <= MAX_FRAGMENT_BYTES:
            lines.append(_Line(offset, end, words, not words, size))
        else:
            lines.extend(_pieces(raw, offset))
        offset = end
    return lines


def _pieces(raw: str, offset: int) -> list[_Line]:
    """A long line cut between words into pieces of at most `OVERLAP_CAP_WORDS` words, so windows
    over it overlap by whole pieces; a run without spaces is cut at a character within the bytes."""
    # A character is at least one byte, so nothing past this many characters can fit in a piece;
    # every scan stops there and a piece costs the same however long the line is.
    reach = MAX_FRAGMENT_BYTES + 1
    pieces: list[_Line] = []
    start = 0
    while start < len(raw):
        stop, count, size = start, 0, 0
        for match in _WORD.finditer(raw, start, start + reach):
            grown = size + _size(raw[stop:match.end()])
            if count == OVERLAP_CAP_WORDS or grown > MAX_FRAGMENT_BYTES:
                break
            stop, count, size = match.end(), count + 1, grown
        if stop == start:                       # one unbroken run longer than the byte bound
            stop = _within(raw, start, MAX_FRAGMENT_BYTES)
            size = _size(raw[start:stop])
        next_word = _WORD.search(raw, stop, stop + reach)
        end = next_word.start() if next_word else min(len(raw), stop + reach)
        if size + _size(raw[stop:end]) > MAX_FRAGMENT_BYTES:      # the whitespace opens the next piece
            end = stop
        pieces.append(_Line(offset + start, offset + end, max(1, count), False, _size(raw[start:end])))
        start = end
    return pieces


def _within(text: str, start: int, limit: int) -> int:
    """The furthest end whose slice from `start` fits in `limit` bytes, cut on a character."""
    return start + len(text[start:start + limit].encode("utf-8")[:limit].decode("utf-8", "ignore"))


def _sections(body: str, lines: list[_Line], *, source: bool) -> list[_Section]:
    """Sections split at H2 and H3 for wiki pages and at every heading for sources; text before the
    first split heading is the lead. A heading inside a code fence is text."""
    starts: list[tuple[int, str]] = [(0, "")]
    trail: dict[int, str] = {}
    fenced = False
    for index, line in enumerate(lines):
        text = body[line.start:line.end].rstrip("\r\n")
        if _FENCE.match(text):
            fenced = not fenced
            continue
        match = None if fenced else _HEADING.match(text)
        if not match:
            continue
        level = len(match.group(1))
        if not source and level not in (2, 3):
            continue
        trail = {depth: name for depth, name in trail.items() if depth < level}
        trail[level] = match.group(2).strip()[:HEADING_CHARS]
        name = " › ".join(trail[depth] for depth in sorted(trail) if (source or depth >= 2))[:TRAIL_CHARS]
        if index == 0:
            starts[0] = (0, name)
        else:
            starts.append((index, name))
    bounds = [first for first, _name in starts[1:]] + [len(lines)]
    return [_Section(first, last, name) for (first, name), last in zip(starts, bounds, strict=True) if last > first]


def _merged(sections: list[_Section], lines: list[_Line]) -> list[_Section]:
    """A section under the minimum joins the next one; a short last section joins the previous."""
    merged: list[_Section] = []
    pending: _Section | None = None
    for section in sections:
        if pending is not None:
            section = _Section(pending.first, section.last, _joined(pending.trail, section.trail))
            pending = None
        if _words(lines, section) < MIN_SECTION_WORDS:
            pending = section
        else:
            merged.append(section)
    if pending is not None:
        if merged:
            last = merged.pop()
            merged.append(_Section(last.first, pending.last, _joined(last.trail, pending.trail)))
        else:
            merged.append(pending)
    return merged


def _joined(first: str, second: str) -> str:
    """The trail of two merged sections; a heading already in the other's trail is not repeated."""
    if not first or second.startswith(first):
        return second
    if not second or first.startswith(second):
        return first
    return f"{first}, {second}"[:TRAIL_CHARS]


def _words(lines: list[_Line], section: _Section) -> int:
    return sum(line.words for line in lines[section.first:section.last])


def _windows(lines: list[_Line], section: _Section) -> list[tuple[int, int, str]]:
    """One span for a section that fits, else windows of about `WINDOW_WORDS` that end on a
    paragraph boundary when one is near and start with about `OVERLAP_WORDS` of the previous one.
    The overlap never exceeds a quarter of its window in words or bytes, so each window advances by
    at least three quarters of itself, whatever the lines hold."""
    first, last = section.first, section.last
    cumulative = [0, *accumulate(line.size for line in lines[first:last])]

    def size(start: int, stop: int) -> int:
        return cumulative[stop - first] - cumulative[start - first]

    if _words(lines, section) <= SECTION_MAX_WORDS and size(first, last) <= MAX_FRAGMENT_BYTES:
        return [(lines[first].start, lines[last - 1].end, section.trail)]
    spans = []
    begin = first
    while begin < last:
        stop, words = begin, 0
        while stop < last and (stop == begin or (words + lines[stop].words <= WINDOW_WORDS
                                                 and size(begin, stop + 1) <= MAX_FRAGMENT_BYTES)):
            words += lines[stop].words
            stop += 1
        if stop < last:
            paragraph = max((index for index in range(begin + 1, stop) if lines[index].blank), default=None)
            if paragraph is not None and sum(line.words for line in lines[begin:paragraph]) >= WINDOW_WORDS * 0.6:
                stop = paragraph + 1
                words = sum(line.words for line in lines[begin:stop])
        spans.append((lines[begin].start, lines[stop - 1].end, section.trail))
        if stop >= last:
            break
        word_room = min(OVERLAP_CAP_WORDS, words // 4)
        byte_room = size(begin, stop) // 4
        back, carried = stop, 0
        while (back - 1 > begin and carried < OVERLAP_WORDS and carried + lines[back - 1].words <= word_room
               and size(back - 1, stop) <= byte_room):
            back -= 1
            carried += lines[back].words
        # Overlap only where the next line still fits beside it, or the next window lies inside this one.
        if carried + lines[stop].words > WINDOW_WORDS or size(back, stop + 1) > MAX_FRAGMENT_BYTES:
            back = stop
        begin = back
    return spans


@dataclass(frozen=True)
class FragmentRow:
    """One fragment as the index stores it, with its page's identity and audience."""

    path: str
    vpath: str
    ordinal: int
    acl: list[str] | None
    type: str
    trail: str
    text: str
    embed_text: str
    content_hash: str


# Every token of the embedding model covers at least one UTF-8 byte, so this many bytes always fit.
WHOLE_SAFE_BYTES = 8000


def pieces_of(row, *, cut: bool = False) -> list[Fragment]:
    """How one stored page is cut: an entity page whole unless `cut` (the model refused it whole), a
    source whole while it is short, and notes, concepts, and long sources at their structure."""
    body = row.body
    if not body:
        return []
    if row.type == "entity" and not cut:
        return [Fragment(0, 0, len(body), "", body)]
    return fragment(body, source=row.type == "source")


def page_fragments(rows, *, cut=frozenset()) -> list[FragmentRow]:
    """Every fragment of every stored page row (`corpus.PageRow`), in path and ordinal order; `cut`
    names the entity pages the embedding model refused whole."""
    out = []
    for row in rows:
        for piece in pieces_of(row, cut=row.path in cut):
            text = embed_text(row.title, piece)
            out.append(FragmentRow(
                path=row.path, vpath=row.vpath, ordinal=piece.ordinal, acl=row.acl, type=row.type,
                trail=piece.trail, text=piece.text, embed_text=text,
                content_hash="sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()))
    return out
