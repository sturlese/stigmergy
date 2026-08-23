"""`kernel.page` — what remains of the old ingest page builder: the page-as-chunk numbers and
the frontmatter scalar emitter.

The `build_page`/`build_pages` suite that used to live here went with its subject, the ingest
pipeline. The properties worth keeping are `_yaml`'s own: `kernel/index.md` names it the one
scalar emitter any frontmatter writer should reuse rather than re-derive, and the knowledge repo's
own linter parses its output back as YAML — so they are tested directly here rather than through a
builder that no longer exists.
"""
import yaml

from stigmergy.kernel.page import MAX_BODY_LINES, SPLIT_CHUNK_LINES, _yaml


def _round_trip(value: str) -> object:
    """What a consumer reads back when `_yaml` emits `value` as a frontmatter scalar."""
    return yaml.safe_load(f"key: {_yaml(value)}")["key"]


def test_hostile_scalars_survive_the_frontmatter_round_trip():
    """Titles/tags/names with YAML-special characters must still yield PARSEABLE frontmatter that
    reads back as the identical string — the page contract is consumed as YAML by the index, the
    gates and the knowledge repo's own linter."""
    for hostile in ['Project "Phoenix": Q1 plan', "a: b", "c,d", 'AT&T "wireless"',
                    "*URGENT* payroll", 'local://Clients/a"b.md', "back\\slash",
                    "line\nbreak", "tab\there"]:
        assert _round_trip(hostile) == hostile, hostile


def test_yaml_1_1_implicit_typed_scalars_stay_strings():
    """Scalars that LOOK like YAML 1.1 implicit types — ISO dates, hex/binary/underscored ints,
    bool/null words, and invalid dates that make `datetime.date()` raise a bare ValueError — must
    round-trip as STRINGS, never re-typing on read. A hand-maintained pattern list silently missed
    several of these, which is why the emitter checks the round-trip itself."""
    for hostile in ["2001-12-14", "0x1F", "0b101", "1_000", "0000-00-00", "2026-02-30",
                    "On", "true", "no", "~", "null"]:
        assert _round_trip(hostile) == hostile, hostile


def test_a_plainly_safe_scalar_is_emitted_unquoted():
    """The emitter quotes only what it must: an ordinary value stays plain, so a human diffing a
    frontmatter block is not reading escapes that buy nothing."""
    assert _yaml("Borealis Dynamics") == "Borealis Dynamics"
    assert _yaml("entities/borealis-dynamics.md") == "entities/borealis-dynamics.md"


def test_the_split_budget_leaves_room_for_the_per_part_chrome():
    """`SPLIT_CHUNK_LINES` is the body budget one split part gets; `MAX_BODY_LINES` is the cap the
    knowledge repo's linter enforces. The gap between them is the per-part chrome (H1, banner,
    continuation links) — if they were equal, every part the splitter emitted would land exactly at
    the cap and the chrome would push it over."""
    assert SPLIT_CHUNK_LINES < MAX_BODY_LINES


def test_control_characters_still_yield_parseable_frontmatter():
    """OLD BEHAVIOUR: PyYAML refused the WHOLE document with `ReaderError`.

    Only `\\n`, `\\t` and `\\r` were escaped, so every other C0/C1 control was emitted RAW inside a
    double-quoted scalar — which YAML forbids. That breaks the "quote (which always round-trips)"
    promise the plain/quoted decision above is written on. It matters beyond hygiene: any
    frontmatter writer built through `_yaml`, `acl:` included, could otherwise emit a whole block
    no consumer could parse from a single control character in a title — and an unparseable page
    is exactly what `corpus.page_row` now has to fail closed on.
    """
    for hostile in ["x\x1by", "a\x00b", "d\x7fe", "\x9bcsi"]:
        assert _round_trip(hostile) == hostile, hostile
