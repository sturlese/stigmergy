"""Fragments cut by length and line structure only, and they reassemble the page (spec
retrieval-and-curation §3.2, BQ-05; the byte bound behind BQ-07)."""
import base64
import random
import time

import pytest

from stigmergy.index import fragments
from stigmergy.index.fragments import MAX_FRAGMENT_BYTES, WHOLE_SAFE_BYTES, fragment


def _words(count: int, seed: int = 0) -> str:
    rng = random.Random(seed)
    vocabulary = ["harbor", "crane", "berth", "pilot", "probe", "dwell", "alert", "budget", "route", "cargo"]
    return " ".join(rng.choice(vocabulary) for _ in range(count))


def _paragraphs(count: int, words: int, seed: int) -> str:
    return "\n\n".join(_words(words, seed + index) + "." for index in range(count))


def _assert_reassembles(body: str, pieces) -> None:
    assert pieces[0].start == 0 and pieces[-1].end == len(body)
    for previous, current in zip(pieces, pieces[1:], strict=False):
        assert current.start <= previous.end, "a gap would lose text"
        assert current.start > previous.start, "fragments advance"
        assert current.end > previous.end, "no fragment lies inside the one before it"
    for piece in pieces:
        assert piece.text == body[piece.start:piece.end]
        assert len(piece.text.encode("utf-8")) <= MAX_FRAGMENT_BYTES
    embedded = sum(len(piece.text.encode("utf-8")) for piece in pieces)
    assert embedded <= 1.4 * len(body.encode("utf-8")) + 1, "overlap never multiplies a page"
    rebuilt = pieces[0].text + "".join(
        current.text[previous.end - current.start:] for previous, current in zip(pieces, pieces[1:], strict=False))
    assert rebuilt == body


def test_a_short_page_is_one_fragment():
    body = "# Acme\n\nAcme is a customer.\n\n## Facts\n- 2026-03-01: signed.\n"
    pieces = fragment(body, source=False)
    assert [(p.start, p.end, p.trail) for p in pieces] == [(0, len(body), "")]


def test_a_long_page_splits_at_h2_and_h3_with_heading_trails():
    body = ("# Tidewell\n\n" + _paragraphs(2, 60, 1) + "\n\n## Decisions\n\n" + _paragraphs(3, 60, 2)
            + "\n\n### Pricing\n\n" + _paragraphs(2, 60, 3) + "\n\n#### Detail\n\n" + _paragraphs(1, 50, 4)
            + "\n\n## Facts\n\n" + _paragraphs(2, 60, 5) + "\n")
    pieces = fragment(body, source=False)
    assert [p.trail for p in pieces] == ["", "Decisions", "Decisions › Pricing", "Facts"]
    assert pieces[0].text.startswith("# Tidewell")
    assert "#### Detail" in pieces[2].text          # an H4 does not split
    _assert_reassembles(body, pieces)


def test_a_section_too_short_to_stand_alone_joins_the_next_one():
    body = ("# T\n\n" + _paragraphs(3, 80, 1) + "\n\n## Entities\n- [[Acme]]\n\n## Connections\n\n"
            + _paragraphs(3, 80, 2) + "\n")
    pieces = fragment(body, source=False)
    assert [p.trail for p in pieces] == ["", "Entities, Connections"]
    _assert_reassembles(body, pieces)


def test_a_long_section_becomes_overlapping_windows_cut_on_paragraphs_or_lines():
    body = "# Notes\n\n## Meeting\n\n" + _paragraphs(30, 45, 7) + "\n"
    pieces = fragment(body, source=False)
    assert len(pieces) > 3
    for previous, current in zip(pieces, pieces[1:], strict=False):
        assert body[current.start - 1] == "\n", "every window starts on a line"
        overlap = len(body[current.start:previous.end].split())
        assert 0 < overlap <= fragments.OVERLAP_CAP_WORDS
    assert all(len(p.text.split()) <= fragments.WINDOW_WORDS + fragments.OVERLAP_CAP_WORDS for p in pieces)
    _assert_reassembles(body, pieces)


def test_a_source_splits_at_every_heading_and_windows_where_there_is_none():
    body = ("\n# Board call\n\n## Artifact 1: transcript\n\n- Media type: text/plain\n\n"
            + "\n".join(f"Speaker {i % 3}: {_words(30, i)}" for i in range(80)) + "\n")
    pieces = fragment(body, source=True)
    assert len(pieces) > 3
    assert all(p.trail == "Board call › Artifact 1: transcript" for p in pieces[1:])
    _assert_reassembles(body, pieces)


def test_a_heading_inside_a_code_fence_is_text():
    body = "# T\n\n" + _paragraphs(4, 80, 1) + "\n\n```\n## not a heading\n```\n\n" + _paragraphs(4, 80, 2) + "\n"
    assert all("not a heading" not in p.trail for p in fragment(body, source=False))


@pytest.mark.parametrize("line", [
    _words(5000, 3),                                  # one line of many words
    "x" * 30_000,                                     # one unbroken run
    "数据" * 6000 + " " + _words(100, 2),            # a script without spaces
    "🙂" * 5000,                                      # four bytes a character
])
def test_a_line_longer_than_a_window_is_cut_between_words_or_at_the_byte_bound(line):
    body = f"# Dump\n\n{line}\n"
    pieces = fragment(body, source=True)
    assert len(pieces) > 1
    _assert_reassembles(body, pieces)


def test_bodies_of_every_shape_reassemble():
    rng = random.Random(11)
    for seed in range(40):
        blocks = []
        for index in range(rng.randint(1, 25)):
            kind = rng.random()
            if kind < 0.15:
                blocks.append(f"{'#' * rng.randint(1, 4)} Heading {index}")
            elif kind < 0.25:
                blocks.append("")
            else:
                blocks.append(_words(rng.randint(1, 600), seed * 100 + index))
        body = "\n".join(blocks) + ("\n" if rng.random() < 0.5 else "")
        for source in (False, True):
            pieces = fragment(body, source=source)
            if body:
                _assert_reassembles(body, pieces)


def test_the_embedded_text_names_the_page_and_the_heading_trail():
    piece = fragments.Fragment(2, 0, 4, "Decisions › Pricing", "text")
    assert fragments.embed_text("Tidewell", piece) == "Tidewell › Decisions › Pricing\ntext"
    assert fragments.embed_text("Tidewell", fragments.Fragment(0, 0, 4, "", "text")) == "Tidewell\ntext"


def _row(kind: str, body: str, path: str = "wiki/x.md"):
    from stigmergy.index.corpus import PageRow

    return PageRow(path=path, vpath=path, zone="wiki", page_id="p", title="T", body=body, type=kind)


def test_what_is_cut_follows_what_a_page_is():
    long_sections = ("# T\n\n## Facts\n\n" + _paragraphs(8, 60, 1) + "\n\n## Connections\n\n"
                     + _paragraphs(8, 60, 2) + "\n")
    assert len(fragments.pieces_of(_row("entity", long_sections))) == 1
    assert len(fragments.pieces_of(_row("entity", long_sections), cut=True)) > 2
    assert len(fragments.pieces_of(_row("note", long_sections))) > 2
    assert len(fragments.pieces_of(_row("concept", long_sections))) > 2
    assert len(fragments.pieces_of(_row("source", "# S\n\n## Artifact\n\n" + _words(200, 3)))) == 1
    assert len(fragments.pieces_of(_row("source", "# S\n\n" + _paragraphs(12, 60, 4)))) > 1


def test_a_capture_header_before_one_long_line_is_not_repeated_in_nested_windows():
    body = "# Call\n\n## Artifact 1: call.txt\n\n- Media type: text/plain\n\n" + _words(3000, 5) + "\n"
    pieces = fragment(body, source=True)
    _assert_reassembles(body, pieces)
    assert sum("Media type" in piece.text for piece in pieces) == 1


def _hostile(kind: str):
    rng = random.Random(3)
    cjk = "".join(chr(rng.randint(0x4E00, 0x9FFF)) for _ in range(30_000))
    if kind == "cjk":
        return "# 记录\n\n" + "\n".join(cjk[i:i + 300] for i in range(0, len(cjk), 300))
    if kind == "emoji":
        return "# Log\n\n" + "\n\n".join("🙂" * 900 for _ in range(8))
    if kind == "merged headings":
        return "\n".join(f"# {'H' * 3990}{index}\nshort." for index in range(20))
    if kind == "nested headings":
        return "\n".join(f"{'#' * level} {'N' * 3990}{level}" for level in range(1, 7)) + "\n" + _paragraphs(20, 60, 1)
    raise ValueError(kind)


@pytest.mark.parametrize("kind", ["cjk", "emoji", "merged headings", "nested headings"])
@pytest.mark.parametrize("role", ["source", "note", "entity"])
def test_every_cut_piece_embeds_within_the_bytes_the_model_always_accepts(kind, role):
    from stigmergy.index.corpus import PageRow

    body = _hostile(kind)
    row = PageRow(path="wiki/x.md", vpath="wiki/x.md", zone="wiki", page_id="p", title="T" * 5000, body=body,
                  type=role)
    pieces = fragments.page_fragments([row], cut={"wiki/x.md"})
    assert len(pieces) > 1
    assert max(len(piece.embed_text.encode("utf-8")) for piece in pieces) <= WHOLE_SAFE_BYTES


@pytest.mark.parametrize("kind", ["blank lines", "whitespace lines", "base64", "spaces then a word",
                                  "one run", "one CJK run"])
def test_a_page_of_wordless_or_one_word_lines_is_cut_in_bounded_time_and_size(kind):
    if kind == "spaces then a word":
        body = "# Upload\n" + " " * 2_000_000 + "word\n"
    elif kind == "one run":
        body = "# Upload\n" + "x" * 2_000_000 + "\n"
    elif kind == "one CJK run":
        body = "# Upload\n" + "数" * 700_000 + "\n"
    elif kind == "blank lines":
        body = "# Upload\n" + "\n" * 50_000
    elif kind == "whitespace lines":
        body = "# Upload\n" + "   \t \n" * 50_000
    else:
        encoded = base64.encodebytes(random.Random(1).randbytes(1_500_000)).decode()
        body = "# Upload\n\n" + encoded
    started = time.perf_counter()
    pieces = fragment(body, source=True)
    assert time.perf_counter() - started < 3.0      # linear work: well under a second even under coverage
    _assert_reassembles(body, pieces)
