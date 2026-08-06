"""`README.md`'s countable claims, checked against the code.

The README is the first file every reader opens, so a false claim there is a false premise
everywhere. Four of its load-bearing statements had already drifted from the code before this file
existed — a package described as present that had no source at all, and three counts that were
each off by one or more. Every one of them was mechanically checkable.

So they are checked here. The point is not the specific numbers; it is that a claim about how many
of something exist must be derivable from the thing itself, or it will rot the moment the thing
changes. A README that says "eight gates" and a `gates.py` that ships nine should not both be
green.

Scope, deliberately narrow: only claims with a single unambiguous source of truth in the code.
Prose that describes DESIGN ("the librarian never imports the server") belongs in
`test_architecture.py`, which checks the design directly rather than the sentence about it.
"""
import pathlib
import re

import pytest

from stigmergy.gardener.checks import ALL_CHECK_SLUGS
from stigmergy.librarian.gates import ALL_GATES
from stigmergy.librarian.page import FAST_LANE_TYPES
from stigmergy.review_kinds import ITEM_KINDS

ROOT = pathlib.Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
STIGMERGY = ROOT / "src" / "stigmergy"

WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
         "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13}


@pytest.fixture(scope="module")
def readme() -> str:
    return README.read_text(encoding="utf-8")


def _claimed(text: str, noun: str) -> int:
    """The number the README claims for `noun`, spelled as a word or as digits.

    Raises rather than returning a default: a claim that has been REWORDED out of existence must
    fail loudly here, because the alternative is a check that silently stops checking — exactly
    the failure mode this file was written for."""
    pattern = rf"\b({'|'.join(WORDS)}|\d+)\b[^.\n]{{0,40}}?{noun}"
    match = re.search(pattern, text, re.IGNORECASE)
    assert match, f"README no longer states a count for {noun!r} — update or delete this check"
    token = match.group(1).lower()
    return WORDS.get(token) or int(token)


def test_the_mcp_tool_count_matches_the_pinned_tool_list(readme):
    """The tool list itself is pinned by `tests/server/test_mcp_adapter.py`; this pins the README's
    COUNT to that same list, so adding a surface without updating the front door turns red."""
    source = (ROOT / "tests" / "server" / "test_mcp_adapter.py").read_text(encoding="utf-8")
    start = source.index("assert names == {", source.index("def test_the_mounted_tool_list"))
    pinned = set(re.findall(r'"(\w+)"', source[start:source.index("}", start)]))
    assert pinned, "could not read the pinned tool list — this check has lost its source of truth"
    assert _claimed(readme, "MCP tools") == len(pinned)


def test_the_gate_count_matches_all_gates(readme):
    assert _claimed(readme, "gates") == len(ALL_GATES)


def test_the_gardener_check_count_matches_all_check_slugs(readme):
    assert _claimed(readme, "deterministic checks") == len(ALL_CHECK_SLUGS)


def _packages_listed_in_the_readme(readme: str) -> set[str]:
    """The package names the README's own table claims exist.

    The table is bounded by its `#### src/stigmergy/` heading and the next heading, so a second
    table underneath it (the `tests/`, `evals/`, `docs/` one) can never leak entries in — those
    are not packages, and counting them would make the reverse check below unfalsifiable.

    It used to be an indented code block, parsed by leading whitespace. Both readings are equally
    fragile against a rewrite of the front page, which is why the assertion below fires on an
    EMPTY result: a parser that quietly matches nothing turns both of these tests permanently
    green, which is worse than the drift they exist to catch.
    """
    block = readme[readme.index("#### `src/stigmergy/`"):]
    rest = block[len("#### `src/stigmergy/`"):]
    end = rest.find("\n#")
    return set(re.findall(r"^\|\s*`([a-z_]+)/`", rest[:end] if end != -1 else rest, re.MULTILINE))


def test_every_package_the_readme_lists_actually_exists(readme):
    """The failure this catches: a package deleted whole, still described in the table as present
    and dormant. Nothing else in the suite reads the README, so nothing else noticed."""
    listed = _packages_listed_in_the_readme(readme)
    assert listed, "the README's package table no longer parses — update this check"
    missing = sorted(p for p in listed if not (STIGMERGY / p / "__init__.py").is_file())
    assert not missing, f"README lists packages that do not exist: {missing}"


def test_every_package_that_exists_is_listed_in_the_readme(readme):
    """The other direction: a package added without the front door catching up."""
    listed = _packages_listed_in_the_readme(readme)
    assert listed, "the README's package table no longer parses — update this check"
    real = {p.name for p in STIGMERGY.iterdir()
            if p.is_dir() and (p / "__init__.py").is_file() and not p.name.startswith("_")}
    assert not sorted(real - listed), f"packages missing from the README table: {sorted(real - listed)}"


def test_the_fast_lane_and_item_kind_vocabularies_are_small_and_stated_once():
    """Not a README claim — a guard on the two counts that drifted the most widely in prose
    (eleven sites said "six fast-lane types" while there were three). Pinning them here means the
    next change to either has one obvious place that turns red."""
    assert len(FAST_LANE_TYPES) == 3
    assert len(ITEM_KINDS) == 2


# ── the architecture diagrams ─────────────────────────────────────────────────────────────────
# GitHub renders these; nothing else does. A block that stops parsing there degrades in the one
# place a newcomer meets the project, and does it silently — the README still looks fine in an
# editor. Validating Mermaid properly needs a browser, which this suite will not grow a dependency
# on, so this pins the drift that is actually likely: a node renamed and its `class` line left
# behind, which Mermaid ignores in silence rather than reporting.
def _mermaid_blocks(readme: str) -> list[str]:
    return re.findall(r"```mermaid\n(.*?)```", readme, re.DOTALL)


def test_the_architecture_section_still_carries_its_diagrams(readme):
    blocks = _mermaid_blocks(readme)
    assert len(blocks) == 3, (
        f"expected the shape, the write path and the read path — found {len(blocks)} mermaid "
        f"blocks. If a diagram was deliberately dropped, drop its claim here too.")


@pytest.mark.parametrize("n", range(3))
def test_every_node_a_diagram_styles_is_a_node_it_defines(readme, n):
    """`class A,B human` naming a node that no longer exists is not an error in Mermaid: the
    styling is dropped and the diagram renders in default grey, so the colour convention the
    section explains quietly stops being true for that box."""
    block = _mermaid_blocks(readme)[n]
    defined = set(re.findall(r"^\s{4}([A-Z][A-Z0-9]*)[\[\{\(]", block, re.MULTILINE))
    assert defined, f"diagram {n} defines no nodes — this check has lost its subject"
    styled = {name
              for line in re.findall(r"^\s{4}class\s+([A-Za-z0-9,]+)\s+\w+$", block, re.MULTILINE)
              for name in line.split(",")}
    assert not styled - defined, (
        f"diagram {n} styles nodes it does not define: {sorted(styled - defined)} — the colour "
        f"convention silently stops applying to them")
