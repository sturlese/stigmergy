"""The librarian's OPERATOR surface, checked against the repo rather than against prose.

**A message containing a command is an executable promise**: if a message names a command, a test
runs that command and asserts it does what the message claims. The librarian's fail-closed refusals
point an operator somewhere — `worker._check_push_identity` names
`docs/reference/operator-runbook.md` — and a refusal that sends somebody to a target or a document
that does not exist is worse than one that says nothing, because it burns their trust in the next
message too.

`make` is invoked with `-n` (dry run) throughout: the point is that the target exists and does what
the message implies, not that a real filing run happens inside a test suite.

**The newest bearer of that rule is not in this file, deliberately.**
`agent.RETIRED_BACKENDS`' refusal prints two `fly` commands, a runbook section and a model id, and
all four are executed in `tests/librarian/test_backend_retirement.py` — beside the rest of the
retirement's contract, because that message is only readable as a whole. This note is the index
entry, so the doctrine's home still knows where its promises are being kept.
"""
import pathlib
import re
import shutil
import subprocess

import pytest

from stigmergy.librarian import agent as agent_module
from stigmergy.librarian import githubapp, pydantic_backend

ROOT = pathlib.Path(__file__).resolve().parents[2]
MAKEFILE = ROOT / "Makefile"
RUNBOOK = ROOT / "docs" / "reference" / "operator-runbook.md"
REFERENCE = ROOT / "docs" / "reference" / "librarian.md"
ADR = ROOT / "docs" / "decisions" / "015-librarian.md"


def _make(*args: str) -> subprocess.CompletedProcess:
    if shutil.which("make") is None:                       # pragma: no cover — every dev box has it
        pytest.skip("make is not on PATH")
    return subprocess.run(["make", "-n", *args], cwd=str(ROOT), capture_output=True, text=True)


# ── the COMMAND the agent-credential refusal named — GONE with the `sdk` backend ──────────────────
# **Three tests and one helper were deleted here, and the message they pinned no longer exists.**
#
#   `_credential_refusal`
#   `test_the_credential_refusal_names_both_ways_of_authenticating`
#   `test_the_command_the_credential_refusal_names_is_a_real_command`
#   `test_the_make_target_the_credential_refusal_names_actually_exists`
#
# They held `worker._check_agent_credential` to its own promise. It named TWO ways of
# authenticating the Claude Code CLI — three environment variables, or an interactive `claude`
# login — and one make target that exports the first; so a test asserted every variable appeared,
# a test ran `claude --version` when the CLI was on PATH, and a test ran `make -n` on the target
# it named. The refusal retired with the backend that needed a CLI at all.
#
# **The RULE they enforced outlives them** and is still enforced in this file by the push-identity
# refusal's runbook tests below. The credential doctrine itself moved to
# `tests/librarian/test_pydantic_preflight.py`, where the surviving pre-flight lives: a missing
# provider key is refused at STARTUP, the message names the VARIABLE, and it never offers
# `--backend double` as the way out.
#
# **Nothing replaced the `claude --version` test, and nothing should.** It was the only test in this
# repo that shelled out to a third-party binary it did not ship, and it existed because the refusal
# promised one — the CLI is gone, so there is no such promise left to keep.
#
# The RULE it came from is unretired and is being kept in three places, which is what to read
# instead of this deletion: a make target and a document below, and the retirement refusal's two
# `fly` commands over in `test_backend_retirement.py` (see this file's module docstring for why
# they live there). A refusal that promises a command still owes a test that runs it.


def _backends_named(invocation: str) -> list[str]:
    """Which shipped backends a recipe line names, off `agent.BACKENDS` rather than a literal.

    Extracted from the test below so the guard can be aimed at a line this suite controls — a
    derived assertion that has never been shown to fail on the thing it forbids is a derived
    assertion nobody has checked. `test_the_walk_target_guard_bites_on_the_backend_it_forbids`
    is that check.
    """
    return [b for b in agent_module.BACKENDS if f"--backend {b}" in invocation]


def test_the_walk_target_runs_the_librarian_with_the_real_agent_backend():
    """What the message CLAIMS the target does: gives a walk the environment and the real agent. A
    target that quietly ran the offline double would file fabricated pages into the company's repo, so
    a REAL backend being explicit in it is the assertion.

    The asserted value moved with the retirement (`--backend sdk` → `--backend pydantic`) and the
    argument did not move at all: it is about the double never being what a walk silently runs, not
    about which real backend is named. Read off `agent.BACKENDS` rather than typed, so the day a
    third backend arrives this asserts the target names one of them and not a stale string.
    """
    result = _make("librarian-walk")
    # The INVOCATION line, isolated from the guard message above it (which legitimately contains the
    # word "run" in prose — matching on the whole recipe made this test read the message, not the
    # command).
    invocation = next(line for line in result.stdout.splitlines()
                      if "stigmergy-librarian" in line and "echo" not in line)
    named = _backends_named(invocation)
    assert named, f"make librarian-walk names no known backend: {invocation!r}"
    assert "double" not in named, (
        "make librarian-walk runs the OFFLINE DOUBLE, which fabricates pages — this target files "
        "into the operator's real knowledge repo")
    assert invocation.rstrip().endswith("once") or " once " in invocation   # one item, by hand
    assert " run" not in invocation


@pytest.mark.parametrize("invocation, expected", [
    ("\t$(VENV)/bin/stigmergy-librarian --backend double once", ["double"]),
    ("\t$(VENV)/bin/stigmergy-librarian --backend pydantic once", ["pydantic"]),
    ("\t$(VENV)/bin/stigmergy-librarian once", []),
])
def test_the_walk_target_guard_bites_on_the_backend_it_forbids(invocation, expected):
    """**The benign twin AND the sabotage, for a guard that was rewritten during the retirement.**

    The assertion above stopped being a literal (`--backend sdk`) and became a derived one, and a
    derived assertion can go vacuous in a way a literal cannot — if it selected nothing, or matched
    a substring of something else, `assert "double" not in named` would pass on a Makefile that
    runs the double into somebody's real knowledge repo.

    So the predicate is aimed here at all three lines that matter: the forbidden one (which must be
    SEEN, or the guard cannot refuse it), the shipped one, and a line naming no backend at all
    (which the `assert named` half is what catches).
    """
    assert _backends_named(invocation) == expected


def test_the_status_target_runs_status_and_nothing_that_writes():
    result = _make("librarian-status")
    assert "stigmergy-librarian status" in result.stdout
    for writing in (" once", " run", "--backend"):
        assert writing not in result.stdout


_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _help() -> str:
    """`make help` with the colour codes stripped: the target name is wrapped in them, so a naive
    match on "name then whitespace" never fires."""
    out = subprocess.run(["make", "help"], cwd=str(ROOT), capture_output=True, text=True).stdout
    return _ANSI.sub("", out)


def test_the_new_targets_appear_in_make_help():
    """`make help` is the operator's map, and its listing pattern used to silently omit every
    target with a DIGIT in its name (`e2e`, `e2e-write`, `r2-smoke`). A new target that is not
    listed is a target nobody finds."""
    listing = _help()
    for target in ("librarian-walk", "librarian-status", "e2e-librarian", "e2e-write", "e2e"):
        assert re.search(rf"^\s+{re.escape(target)}\s", listing, re.M), \
            f"`make help` does not list {target}:\n{listing}"


def test_make_help_prints_target_names_and_not_the_makefile_name():
    """The regression the digit fix came with: `-include` puts a second file in `MAKEFILE_LIST`,
    grep across two files prefixes every match with its filename, and the first column printed
    "Makefile" for every row — on exactly the credentialed machines that need the help most."""
    listing = _help()
    assert "Makefile" not in listing
    assert re.search(r"^\s+help\s+Show this help", listing, re.M)


def test_every_phony_target_named_in_the_makefile_is_declared_phony():
    """A target that files or wipes must not be skipped because a same-named file happens to exist."""
    text = MAKEFILE.read_text(encoding="utf-8")
    phony = set(re.search(r"^\.PHONY:(.*)$", text, re.M).group(1).split())
    declared = set(re.findall(r"^([a-z0-9-]+):.*?##", text, re.M))
    assert declared <= phony, f"not declared .PHONY: {sorted(declared - phony)}"


# ── the COMMAND the meeting-only backend's refusal named — GONE with the refusal (ADR 033) ────────
# **Four tests were deleted here, and the message they pinned no longer exists.**
#
#   `test_the_script_the_meeting_only_refusal_names_is_in_this_repo`
#   `test_the_command_the_meeting_only_refusal_names_is_one_its_own_parser_accepts`
#   `test_the_model_the_refusal_offers_survives_the_next_two_refusals`
#   `test_the_refusal_echoes_the_operators_own_id_when_it_is_usable`
#
# They covered the one refusal in this package that printed a full COMMAND LINE rather than a make
# target or a document: `worker._check_pydantic_backend` used to refuse `backend="pydantic"` for any
# worker and redirect the operator to `python evals/run_filing.py --backend pydantic --kinds meeting
# --model <a priced id derived from theirs>`. The four tests asserted the whole promise — the script
# is in this repo, the flags parse through `run_filing.build_parser()`, the derived model id clears
# BOTH refusals below the one that printed it, and an already-correct id is echoed rather than
# substituted.
#
# ADR 033 D5 retired the refusal itself: that backend serves the ordinary flow now, so there is no
# limitation to redirect around, `_check_pydantic_backend` lost its `meeting_only` parameter, and
# `worker._usable_example` — the helper that derived the printed id — was removed with it. Nothing
# in the librarian prints a command line any more, which is why this section is a comment and not a
# thinner set of tests: there is no message left to hold to the promise.
#
# **The RULE they enforced outlives them**, and it is recorded in the two places it has to be: this
# file's own module docstring ("a message containing a command is an executable promise", still
# enforced for the credential refusal's `make librarian-walk` and the push-identity refusal's
# runbook), and `worker.py` where `_usable_example` used to be — a refusal whose own example fails
# the refusal below it is worse than one with no example. The next librarian message that prints a
# command owes this section back.
#
# What replaced the pre-flight coverage: `tests/librarian/test_pydantic_preflight.py` keeps every
# check that was always about the BACKEND (a provider-prefixed id, a configured price, the
# provider's own key), and adds the lifted state — a worker configured with this backend now PASSES
# the pre-flight it used to be refused by.


# ── the document the App refusal names ────────────────────────────────────────────────────────────
def test_the_runbook_the_push_identity_refusal_names_exists_and_covers_the_app():
    """The refusal says the setup procedure is in the runbook. So it has to be — and it has to name
    all three variables, since "half a configuration is a hard error" makes a partial list a trap."""
    assert RUNBOOK.exists()
    runbook = RUNBOOK.read_text(encoding="utf-8")
    # The runbook is organized by OPERATION — the App setup lives under Revocation's "The
    # librarian GitHub App + the Anthropic key" and the Deploy secrets inventory, rather than in a
    # section named after the feature. The OBLIGATION: the App and all its variables named.
    assert "The librarian GitHub App" in runbook
    assert "GitHub App" in runbook
    for name in (githubapp.APP_ID_ENV, githubapp.INSTALLATION_ID_ENV, githubapp.PRIVATE_KEY_ENV,
                 githubapp.PRIVATE_KEY_FILE_ENV):
        assert name in runbook, f"{name} is not documented in the runbook"


def test_the_runbook_documents_every_librarian_environment_variable():
    """A tunable nobody can find is a tunable that gets rediscovered by reading source. The reference
    doc carries the table; this asserts the two files between them cover the whole surface."""
    documented = RUNBOOK.read_text(encoding="utf-8") + REFERENCE.read_text(encoding="utf-8")
    from stigmergy.librarian import config
    for name in (config.REPO_ENV, config.REFUSED_DIFF_ROOT_ENV, "STIGMERGY_LIBRARIAN_BRANCH",
                 "STIGMERGY_LIBRARIAN_BACKEND", "STIGMERGY_LIBRARIAN_MODEL",
                 "STIGMERGY_LIBRARIAN_MAX_TURNS", "STIGMERGY_LIBRARIAN_MAX_TOOL_CALLS",
                 config.TIMEOUT_ENV, "STIGMERGY_LIBRARIAN_DEDUP_WINDOW_S",
                 "STIGMERGY_LIBRARIAN_WORKTREE_ROOT", "STIGMERGY_GITLEAKS_BIN",
                 # The filing agent's own credential. Derived from the live provider table — it
                 # used to come from `agent.CREDENTIAL_ENV`, the retired CLI's tuple, and it is the
                 # same variable either way because the shipped default is an `anthropic:` model.
                 pydantic_backend.PROVIDER_KEY_ENV["anthropic"]):
        assert name in documented, f"{name} is documented nowhere"


def test_the_runbook_warns_that_every_e2e_destroys_the_queue_and_the_index():
    """The warning used to cover the INDEX (a disposable cache, rebuildable from git) and not the
    QUEUE, which exists nowhere else until the librarian files it. All four e2e targets run
    `docker compose down -v`, and every one of them must be named in the one warning."""
    runbook = RUNBOOK.read_text(encoding="utf-8")
    warning = runbook[runbook.index("destroys the local queue AND the local index"):][:1600]
    for target in ("make e2e", "make e2e-write", "make e2e-librarian",
                   "make e2e-librarian-container"):
        assert target in warning, f"{target} is not named in the destroys-local-state warning"
    assert "cannot be rebuilt from git" in runbook          # the queue half, stated plainly
    assert "rebuild" in warning                             # ...and how to get the index back


def test_the_runbook_warns_that_two_identities_are_declared_and_names_the_fix():
    """`stigmergy/.mcp.json` registers `stigmergy` and `stigmergy-ana`, so a session picks one on its own and
    a capture can be attributed to the wrong identity. Naming the hazard is half of it; the fix —
    address the tool explicitly — is the half an operator can act on."""
    runbook = RUNBOOK.read_text(encoding="utf-8")
    assert ".mcp.json" in runbook
    assert "stigmergy-ana" in runbook
    assert "brain_submit" in runbook
    assert "explicitly" in runbook


def test_the_reference_doc_covers_the_surfaces_that_were_undocumented():
    """The four things a reader of the code would otherwise have to reverse-engineer: the
    diagnostics directory, the preamble, the sweep line, and the shape of a declared edit."""
    reference = REFERENCE.read_text(encoding="utf-8")
    assert "STIGMERGY_LIBRARIAN_REFUSED_DIFF_DIR" in reference
    assert "filing into" in reference and "origin/main@" in reference
    assert "the sweep line" in reference
    assert "backlink" in reference and "contradiction" in reference and "overlap" in reference


def test_the_reference_doc_explains_that_the_librarian_branches_from_the_remote():
    reference = REFERENCE.read_text(encoding="utf-8")
    assert "branches from the remote" in reference
    assert "invisible to the librarian" in reference
    assert "diverge" in reference


def test_the_adr_records_the_four_decisions_it_owes():
    """The four decisions this ADR owes a reader: why the agent judges and code vetoes, why an
    ephemeral worktree, why a failed verification bounces the capture instead of filing it with a
    banner, and why edits are declared."""
    adr = ADR.read_text(encoding="utf-8")
    assert "code vetoes" in adr.lower()
    assert "ephemeral" in adr and "worktree" in adr
    assert "banner" in adr and "bounces" in adr
    assert "DECLARED" in adr and "PERFORMED" in adr


def test_every_relative_link_in_every_live_doc_resolves():
    """A link to a file that does not exist is a dead end for whoever followed it.

    Two documents were once deleted on purpose and SEVEN references to them survived — six in live
    operator docs and one in a module docstring — dangling until somebody happened to read them
    all. This is the mechanical version of that reading pass: it costs one grep and cannot get
    bored.

    Scope is what a reader actually follows: `docs/`, every package code map, the eval docs and
    the two front doors. There is no exemption for superseded material — this repo keeps none, and
    an exemption nothing uses reads as coverage of a case that cannot arise.

    **Links that leave this repo are skipped, and that is not laziness.** A front door may
    deliberately route to a SIBLING checkout — the knowledge repo lives beside this one, exists on
    a developer's machine, and cannot exist in CI, where only this repository is cloned. Written
    without the skip, this test passed locally and went red on the first CI run, which is the
    definition of an environment-dependent test. Whether such a link SHOULD exist at all is a
    different rule, owned by `test_no_librarian_document_links_out_of_this_repo` below.
    """
    surfaces = [
        *(ROOT / "docs").rglob("*.md"),
        *(ROOT / "src" / "stigmergy").glob("*/index.md"),
        *(ROOT / "evals").glob("*.md"),
        ROOT / "README.md", ROOT / "CLAUDE.md",
    ]
    assert len(surfaces) > 30, f"the sweep found only {len(surfaces)} docs — it stopped seeing them"
    dead: list[str] = []
    for path in surfaces:
        for target in re.findall(r"\]\(([^)]+)\)", path.read_text(encoding="utf-8")):
            if target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            resolved = (path.parent / target.split("#")[0]).resolve()
            if ROOT not in resolved.parents and resolved != ROOT:
                continue                      # a sibling checkout — see the docstring
            if not resolved.exists():
                dead.append(f"{path.relative_to(ROOT)} -> {target}")
    assert not dead, "documentation links that resolve to nothing:\n  " + "\n  ".join(dead)


def test_no_librarian_document_links_out_of_this_repo():
    """This repo owns everything it documents. A link to another project is a link that can be
    archived or made private, taking the explanation with it."""
    # "Leaves the repo" is decided by where a link RESOLVES, never by how it is spelled. A
    # `../../` prefix used to stand in for the real test, and it was a proxy that had to break the
    # moment a document under `docs/reference/` wanted to point at the code it describes: two
    # levels up from there is the repository ROOT, not the outside. The resolution check below is
    # the honest form, and it is strictly stronger — a genuine escape (`../../../other-project`)
    # fails it whether or not the sibling checkout happens to exist on this machine.
    for path in (ADR, REFERENCE):
        for target in re.findall(r"\]\(([^)]+)\)", path.read_text(encoding="utf-8")):
            assert not target.startswith(("http://", "https://")), f"{path.name} -> {target}"
            resolved = (path.parent / target.split("#")[0]).resolve()
            assert resolved.exists(), f"{path.name} -> {target} does not resolve"
            assert ROOT in resolved.parents or resolved == ROOT, (
                f"{path.name} -> {target} leaves the repo")
