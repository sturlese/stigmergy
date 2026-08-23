"""The index is a SIBLING of the write path, not a client of it.

`stigmergy.index` shares no code with the packages that produce what it indexes: it reads the
repo and nothing else, so a change to how pages are written can never silently change what
gets indexed. The rule worth pinning is **the index reaches for no writer** (`librarian`,
`entities`, `capture`). `stigmergy.kernel` is deliberately outside that rule: a
dependency-free module at the bottom of the stack can be imported by everyone precisely because
it imports no one.

The fake embedder follows the offline-double rule: production modules may only reach it
through a deferred import inside the dispatch (`build_embedder`), never at module level.
"""
import ast
import pathlib

import pytest

SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "stigmergy"
INDEX_SOURCES = sorted(p for p in (SRC / "index").rglob("*.py"))
_WRITERS = ("stigmergy.librarian", "stigmergy.entities", "stigmergy.capture")


def _module_level_imports(path: pathlib.Path) -> list[tuple[str, int]]:
    tree = ast.parse(path.read_text())
    found = []
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module:
            found.append((node.module, node.lineno))
        elif isinstance(node, ast.Import):
            found += [(a.name, node.lineno) for a in node.names]
    return found


def _all_imports(path: pathlib.Path) -> list[tuple[str, int]]:
    tree = ast.parse(path.read_text())
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            found.append((node.module, node.lineno))
        elif isinstance(node, ast.Import):
            found += [(a.name, node.lineno) for a in node.names]
    return found


def test_sources_found():
    assert INDEX_SOURCES, "layout moved and this test went blind"


@pytest.mark.parametrize("path", INDEX_SOURCES, ids=lambda p: str(p.relative_to(SRC)))
def test_the_index_never_imports_a_writer(path):
    """The index is BUILT from the repo, never from a writer's in-memory state. A reach into
    `librarian`/`entities`/`capture` would make the derived cache depend on the thing
    it is derived from, which is how "the index is a cache; git is the record" stops being true."""
    offenders = [f"{path.relative_to(SRC)}:{line} -> {mod}"
                 for mod, line in _all_imports(path) if mod.startswith(_WRITERS)]
    assert not offenders, "the index reached into a writer:\n  " + "\n  ".join(offenders)


def test_the_pipeline_it_used_to_be_a_sibling_of_is_gone():
    """A resurrection guard. `stigmergy.pipeline` — the ingestion pipeline the index was once the
    sibling of — does not exist, and reintroducing it would put a second writer of `pages_index`
    beside the repo walk. Stated rather than left to hold silently by absence."""
    assert not (SRC / "pipeline").exists()


def test_production_never_imports_the_fake_embedder_at_module_level():
    offenders = [
        f"{p.relative_to(SRC)}:{line} -> {mod}"
        for p in INDEX_SOURCES
        for mod, line in _module_level_imports(p)
        if mod.startswith("stigmergy.index.backends.fake_embedder")
    ]
    assert not offenders, (
        "production modules import the offline double at module level:\n  "
        + "\n  ".join(offenders)
        + "\n\nReach it through the deferred import inside build_embedder() instead.")
