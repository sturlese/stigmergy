"""`docs/`'s checkable claims, held against the code — the same bargain `test_readme_claims.py`
strikes for the front page, extended to the 60k words behind it.

`docs/reference/` describes what the system does TODAY, and it is the largest hand-maintained
surface in this repository: a quarter of all commits touch it. Prose that costs that much to keep
true is prose nobody can afford to VERIFY by reading — so the parts of it a machine can settle are
settled here, and a reader gets to trust the rest because the checkable parts are checked.

**`docs/decisions/` is deliberately exempt from the existence checks below.** An ADR is a dated
record of a decision, not a description of the present: ADR 023 names `stigmergy.loop`, which was
deleted, and says so in its own second line. Asserting that an ADR's identifiers still resolve
would force the records to be rewritten every time the system moves past them, which is the one
thing an ADR must never be. Their links still have to resolve, and the index still has to list
them — that is all.

Scope, as next door: only claims with a single unambiguous source of truth. Design prose belongs
to `test_architecture.py`, which checks the design rather than the sentence about it.
"""
import pathlib
import re
import tomllib

import pytest

from stigmergy.capture.schema import REJECTION_REASONS
from stigmergy.gardener.checks import ALL_CHECK_SLUGS
from stigmergy.librarian.gates import ALL_GATES
from stigmergy.librarian.page import FAST_LANE_TYPES, PAGE_TYPES

ROOT = pathlib.Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
REFERENCE = DOCS / "reference"
DECISIONS = DOCS / "decisions"
INDEX = DOCS / "README.md"

WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
         "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13}

ALL_DOCS = sorted(DOCS.rglob("*.md"))


def _text(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8")


def _rel(path: pathlib.Path) -> str:
    return str(path.relative_to(ROOT))


# ── the index knows exactly which documents exist ─────────────────────────────────────────────
# `docs/README.md` is a table of contents someone has to remember to update, which is the same
# shape of promise the README's package table makes — and it had already drifted: ADR 030 shipped
# and the table did not learn about it, so the one page whose job is "here is everything" was
# quietly missing a record.

def _linked(pattern: str) -> set[str]:
    found = set(re.findall(pattern, _text(INDEX), re.MULTILINE))
    assert found, f"docs/README.md no longer links documents matching {pattern!r} — this check " \
                  f"has lost its source of truth and would otherwise pass by matching nothing"
    return found


@pytest.mark.parametrize("directory, pattern", [
    (REFERENCE, r"^\| \[`reference/([a-z0-9-]+\.md)`\]"),
    (DECISIONS, r"^\| \[\d{3}\]\(\./decisions/([0-9a-z-]+\.md)\)"),
])
def test_the_index_lists_exactly_the_documents_that_exist(directory, pattern):
    listed = _linked(pattern)
    real = {p.name for p in directory.glob("*.md")}
    assert listed == real, (
        f"docs/README.md and {_rel(directory)}/ disagree. "
        f"Listed but absent: {sorted(listed - real)}. Present but unlisted: {sorted(real - listed)}")


# ── every path a document points at is really there ───────────────────────────────────────────

def _broken_links(docs) -> list[str]:
    broken = []
    for doc in docs:
        for target in re.findall(r"\]\(([^)#\s]+)(?:#[^)]*)?\)", _text(doc)):
            if target.startswith(("http://", "https://", "mailto:")):
                continue
            if not (doc.parent / target).exists():
                broken.append(f"{_rel(doc)} -> {target}")
    return broken


def test_every_relative_link_in_the_docs_resolves():
    """A renamed file leaves a link that looks live and is not. Anchors are not checked — a
    heading is prose and moves for good reasons; a PATH is a fact."""
    broken = _broken_links(ALL_DOCS)
    assert not broken, "links in docs/ that point at nothing:\n  " + "\n  ".join(broken)


# The same check, over the markdown that does NOT live in `docs/`. It was scoped to `docs/` for no
# better reason than that being where this file started, which left the two most-read pages in the
# repository — the front page and the contributing guide — plus ~3,500 lines of package code map
# with no link check at all. A code map's links are the ones most likely to rot, because they are
# relative paths climbing out of `src/` and every one of them breaks the moment a package moves.
ROOT_DOCS = sorted(p for p in ROOT.glob("*.md"))
CODE_MAPS = sorted((ROOT / "src" / "stigmergy").glob("*/index.md")) + [ROOT / "evals" / "index.md"]


@pytest.mark.parametrize("label, docs", [
    ("the root documents", ROOT_DOCS),
    ("the package code maps", CODE_MAPS),
], ids=lambda v: v if isinstance(v, str) else "")
def test_every_relative_link_outside_docs_resolves(label, docs):
    assert docs, f"{label}: found none — this check has lost its source of truth"
    broken = _broken_links(docs)
    assert not broken, f"links in {label} that point at nothing:\n  " + "\n  ".join(broken)


# A code map's whole job is to say what each module is FOR, so it names modules constantly, in
# backticks, and a renamed or deleted one leaves a sentence that reads authoritative and points at
# nothing. This is deliberately an EXISTENCE check and no more: what a module does is prose no test
# can settle, which is why these files still have to be read by a person.
_MODULE_TOKEN = re.compile(r"`([A-Za-z0-9_][A-Za-z0-9_./-]*\.py)`")


def _python_files_that_exist() -> set[str]:
    """Every Python file in the project under both spellings a map uses — the bare name
    (`converters.py`) and one directory of context (`backends/embedder.py`)."""
    names = set()
    for path in ROOT.rglob("*.py"):
        if ".venv" in path.parts or "__pycache__" in path.parts:
            continue
        names.add(path.name)
        names.add(f"{path.parent.name}/{path.name}")
    return names


def test_every_python_module_a_code_map_names_exists():
    known = _python_files_that_exist()
    assert known, "no Python files found — this check has lost its source of truth"
    ghosts = sorted({f"{_rel(doc)}: {token}"
                     for doc in CODE_MAPS
                     for token in _MODULE_TOKEN.findall(_text(doc))
                     if token not in known and token.rsplit("/", 1)[-1] not in known})
    assert not ghosts, ("code maps name Python modules that do not exist — a rename or a deletion "
                        "left the sentence behind:\n  " + "\n  ".join(ghosts))


# ── what the reference docs tell an operator to run, and to set ───────────────────────────────
# Both checks are scoped to `reference/` on purpose: these are instructions someone follows, and
# an instruction naming something that no longer exists costs a debugging session. The class is
# not hypothetical here — a refusal message advertising a dead environment variable was a filed
# bug, found by a human reading it, not by the suite.

def _fenced_command_lines(doc: pathlib.Path) -> list[str]:
    """Every line inside a fenced block, indented fences included — the runbook writes its fences
    inside numbered lists, so a parser anchored at column zero skipped exactly the commands an
    operator is being walked through. Verified by mutation: it silently saw nothing."""
    body = _text(doc)
    return [line.strip().removeprefix("$ ").strip()
            for block in re.findall(r"^[ \t]*```[a-z]*\n(.*?)^[ \t]*```", body,
                                    re.MULTILINE | re.DOTALL)
            for line in block.splitlines()]


def test_every_stigmergy_command_the_reference_docs_run_is_a_real_entry_point():
    entry_points = set(tomllib.loads(_text(ROOT / "pyproject.toml"))["project"]["scripts"])
    assert entry_points, "pyproject.toml declares no console scripts — this check lost its source"
    ghosts = []
    for doc in sorted(REFERENCE.glob("*.md")):
        for line in _fenced_command_lines(doc):
            command = line.split()[0].rsplit("/", 1)[-1].rstrip(":,.") if line.split() else ""
            if command.startswith("stigmergy-") and command not in entry_points:
                ghosts.append(f"{_rel(doc)}: {command}")
    assert not ghosts, ("the reference docs tell someone to run commands that do not exist:\n  "
                        + "\n  ".join(ghosts))


# The environment names these guards can SEE (issue #111). `STIGMERGY_*` is the house prefix;
# the model-seam family is enumerated BY NAME rather than wildcarded, because the literal scan
# cannot tell an env var from a module constant — `EMBED_BATCH` is code, not configuration, and
# a wildcard would demand `.env.example` lines for things no operator can set. Adding a seam env
# means adding it here, which is the point: invisible-to-the-guards was the defect.
_SEAM_ENVS = ("EMBED_BASE_URL", "EMBED_API_KEY", "EMBED_MODEL", "EMBED_DIMENSIONS",
              "CLEAN_LLM", "CLEAN_MODEL", "CLEAN_REASONING_EFFORT",
              "ANSWER_LLM", "ANSWER_MODEL", "ANSWER_REASONING_EFFORT", "VISION_MODEL")
_ENV_NAME_RE = re.compile(r"\bSTIGMERGY_[A-Z0-9_]+\b|\b(?:" + "|".join(_SEAM_ENVS) + r")\b")


def test_the_guards_see_the_model_seam_family():
    """The fix's own pin, both halves: every enumerated seam env is visible to the pattern the
    two guards share, and a module CONSTANT that merely wears the family's prefix is not — the
    benign twin that keeps the enumeration from quietly becoming a wildcard."""
    for name in _SEAM_ENVS:
        assert _ENV_NAME_RE.search(f"set {name} first"), name
    assert not _ENV_NAME_RE.search("EMBED_BATCH is a module constant, not configuration")


def test_every_environment_variable_the_reference_docs_name_is_read_somewhere():
    """`src/` and `tests/` are where Python reads one; `.github/workflows/` is where this repo's
    own CI does (`STIGMERGY_READONLY_PAT` is a repository secret and appears in no Python at all).
    A variable in neither is one an operator would set to no effect.

    There is no third place any more: `deploy/workflows/` held cron templates that read a handful
    of variables Python never did, and ADR 044 moved every unattended pass into the worker."""
    def declared(paths) -> set[str]:
        return {name for path in paths if path.is_file()
                for name in _ENV_NAME_RE.findall(
                    path.read_text(encoding="utf-8", errors="ignore"))}

    read = (declared((ROOT / "src").rglob("*.py"))
            | declared((ROOT / "tests").rglob("*.py"))
            | declared((ROOT / ".github" / "workflows").rglob("*.yml")))
    assert read, "no STIGMERGY_* variable found in the code — this check lost its source of truth"

    ghosts = sorted({f"{_rel(doc)}: {name}"
                     for doc in sorted(REFERENCE.glob("*.md"))
                     for name in _ENV_NAME_RE.findall(_text(doc))
                     if name not in read})
    assert not ghosts, ("the reference docs name variables nothing reads:\n  " + "\n  ".join(ghosts))


# ── the OTHER direction: a setting the code reads that nothing offers an operator ──────────────
# Everything above this line asks "does the code have what the docs claim?". This asks the reverse,
# and it is the question that actually bites: a variable the code reads and no document mentions is
# invisible until someone's deployment misbehaves in a way they cannot explain. The class is not
# hypothetical — `STIGMERGY_LIBRARIAN_BACKEND` defaults to the offline TEST DOUBLE, so a deployment
# that never sets it files fabricated pages and looks healthy doing it, and `.env.example` did not
# mention it.
#
# `.env.example` is the surface being checked because it is the one an operator copies. Naming a
# variable in a COMMENT counts: that is how the file already handles everything with a working
# default, and a commented line is still a line someone reads.

CONFIG_EXAMPLE = ROOT / ".env.example"

# Each entry needs a reason, and the pruning test below deletes it for you when it stops being
# needed — the same bargain `tests/test_architecture.py` strikes with its exception lists.
CONFIG_EXCEPTIONS = {
    "STIGMERGY_LIBRARIAN_REQUIRE_REMOTE_BASE":
        "process-injected by `bootstrap.worker_env` for the deployed worker and documented as "
        "never-by-hand. Offering it in the file an operator edits would invite exactly the misuse "
        "`docs/reference/librarian.md` spends a section forbidding.",
}


def _configured_names() -> set[str]:
    return set(_ENV_NAME_RE.findall(_text(CONFIG_EXAMPLE)))


def _names_read_by_the_code() -> set[str]:
    """Every `STIGMERGY_*` (and enumerated seam-env — issue #111) literal in `src/`.
    Literal-scanned rather than parsed, exactly like the
    docs check next door — which is also why a variable name must never be wrapped across a line
    break: this cannot see one, and neither can a human grepping for it."""
    return {name for path in (ROOT / "src").rglob("*.py")
            for name in _ENV_NAME_RE.findall(
                path.read_text(encoding="utf-8", errors="ignore"))}


def test_every_setting_the_code_reads_is_offered_to_an_operator():
    read = _names_read_by_the_code()
    assert read, "no STIGMERGY_* name found in src/ — this check has lost its source of truth"
    undocumented = sorted(read - _configured_names() - set(CONFIG_EXCEPTIONS))
    assert not undocumented, (
        ".env.example never mentions these, and the code reads them:\n  "
        + "\n  ".join(undocumented)
        + "\nAdd a line (commented is fine, and is the convention for anything with a default), "
          "or add it to CONFIG_EXCEPTIONS with the reason it must not be offered.")


def test_no_config_exception_outlives_its_reason():
    """A permission slip nobody prunes stops being a decision and becomes furniture."""
    read = _names_read_by_the_code()
    stale = sorted(name for name in CONFIG_EXCEPTIONS
                   if name in read and name in _configured_names())
    assert not stale, (f"these are in .env.example now, so their exception is dead weight — "
                       f"delete the entry: {stale}")


# ── the counts ────────────────────────────────────────────────────────────────────────────────

def _claimed(doc: pathlib.Path, noun: str) -> int:
    """The number `doc` claims for `noun` — the one NEAREST it, reading backwards.

    Its twin next door scans forwards from a number to the noun, which is right for a README
    where each count sits in its own sentence and wrong here: `**8 gates**, **10 MCP tools**` in
    the runbook's opening line answered "how many MCP tools" with 8, and "three of the seven page
    types" answered "how many page types" with three. The qualifier of a noun is the number
    closest to it, so that is what is read.

    Raises when the claim has been reworded out of existence, for the reason its twin does: a
    check that silently stops checking reads as coverage and is worse than no check."""
    text = _text(doc)
    for noun_match in re.finditer(noun, text, re.IGNORECASE):
        window = text[max(0, noun_match.start() - 40):noun_match.start()]
        numbers = re.findall(rf"\b({'|'.join(WORDS)}|\d+)\b", window, re.IGNORECASE)
        if numbers and "." not in window[window.rfind(numbers[-1]):]:
            token = numbers[-1].lower()
            return WORDS.get(token) or int(token)
    raise AssertionError(
        f"{_rel(doc)} no longer states a count for {noun!r} — update or delete this check")


def _mcp_tool_count() -> int:
    source = _text(ROOT / "tests" / "server" / "test_mcp_adapter.py")
    start = source.index("assert names == {", source.index("def test_the_mounted_tool_list"))
    pinned = set(re.findall(r'"(\w+)"', source[start:source.index("}", start)]))
    assert pinned, "could not read the pinned tool list — this check has lost its source of truth"
    return len(pinned)


@pytest.mark.parametrize("doc, noun, expected", [
    (INDEX, "gates", len(ALL_GATES)),
    (INDEX, "deterministic corpus-health checks", len(ALL_CHECK_SLUGS)),
    (REFERENCE / "operator-runbook.md", "gates", len(ALL_GATES)),
    (REFERENCE / "operator-runbook.md", "MCP tools", _mcp_tool_count()),
    (REFERENCE / "gardener-digest.md", "deterministic checks", len(ALL_CHECK_SLUGS)),
    (REFERENCE / "page-contract.md", "page types", len(PAGE_TYPES)),
    (REFERENCE / "knowledge-repo.md", "page types", len(FAST_LANE_TYPES)),
    (REFERENCE / "librarian.md", "values of", len(REJECTION_REASONS)),
], ids=lambda v: v.name if isinstance(v, pathlib.Path) else str(v))
def test_a_documented_count_matches_the_code_it_counts(doc, noun, expected):
    assert _claimed(doc, noun) == expected
