"""The librarian's OPERATOR surface, checked against the repo rather than against prose.

**A message containing a command is an executable promise**: if a message names a command, a test
runs that command and asserts it does what the message claims. Two of the librarian's
fail-closed refusals point an operator somewhere — `worker._check_agent_credential` names
`make librarian-walk`, `worker._check_push_identity` names `docs/reference/operator-runbook.md` — and
a refusal that sends somebody to a target or a document that does not exist is worse than one that
says nothing, because it burns their trust in the next message too.

`make` is invoked with `-n` (dry run) throughout: the point is that the target exists and does what
the message implies, not that a real filing run happens inside a test suite.
"""
import pathlib
import re
import shutil
import subprocess

import pytest

from stigmergy.librarian import agent as agent_module
from stigmergy.librarian import githubapp, worker
from stigmergy.librarian.errors import LibrarianConfigError

ROOT = pathlib.Path(__file__).resolve().parents[2]
MAKEFILE = ROOT / "Makefile"
RUNBOOK = ROOT / "docs" / "reference" / "operator-runbook.md"
REFERENCE = ROOT / "docs" / "reference" / "librarian.md"
ADR = ROOT / "docs" / "decisions" / "015-librarian.md"


def _make(*args: str) -> subprocess.CompletedProcess:
    if shutil.which("make") is None:                       # pragma: no cover — every dev box has it
        pytest.skip("make is not on PATH")
    return subprocess.run(["make", "-n", *args], cwd=str(ROOT), capture_output=True, text=True)


def _credential_refusal() -> str:
    with pytest.raises(LibrarianConfigError) as exc_info:
        worker._check_agent_credential({})
    return str(exc_info.value)


# ── the OTHER way of having a credential, which the refusal used not to mention ────────────────────
# `agent.credential_status` has three answers, and one of them — an interactive CLI login, which on
# macOS is the Keychain — is how most machines are actually authenticated. The refusal named only the
# environment variables, so an operator whose laptop has the working configuration was told to go get
# a key they do not need. Both routes are named now, and both halves of the message are asserted: the
# variables come from the module's own tuple, and the command is a command, so a test runs it.
def test_the_credential_refusal_names_both_ways_of_authenticating():
    message = _credential_refusal()
    for name in agent_module.CREDENTIAL_ENV:
        assert name in message
    assert "`claude`" in message, (
        "the refusal no longer mentions the interactive login — `credential_status` accepts it "
        "(CREDENTIAL_AMBIENT), so a refusal that omits it sends a correctly-configured operator "
        "hunting for a key they do not need")


def test_the_command_the_credential_refusal_names_is_a_real_command():
    """The executable promise, as far as it can honestly go here: authenticating cannot run in a
    suite, but `claude` either exists as a runnable command or the message names something that
    does not."""
    named = re.findall(r"`(claude)`", _credential_refusal())
    assert named, "update this test with whatever command the refusal now names"
    found = shutil.which("claude")
    if found is None:
        pytest.skip("the Claude Code CLI is not on PATH on this machine (CI); nothing to run")
    result = subprocess.run([found, "--version"], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, f"`claude --version` exited {result.returncode}: {result.stderr}"


# ── the target the credential refusal names ───────────────────────────────────────────────────────
def test_the_make_target_the_credential_refusal_names_actually_exists():
    """The message says `make librarian-walk` includes and exports the local env file. If that target
    did not exist, an operator following the one instruction in the refusal would get
    "No rule to make target"."""
    named = re.findall(r"`make ([a-z0-9-]+)`", _credential_refusal())
    assert named, "the credential refusal no longer names a make target — update this test with it"
    for target in named:
        result = _make(target)
        assert "No rule to make target" not in result.stderr, f"make {target}: {result.stderr}"


def test_the_walk_target_runs_the_librarian_with_the_real_agent_backend():
    """What the message CLAIMS the target does: gives a walk the environment and the real agent. A
    target that quietly ran the offline double would file fabricated pages into the company's repo, so
    `--backend sdk` being explicit in it is the assertion."""
    result = _make("librarian-walk")
    # The INVOCATION line, isolated from the guard message above it (which legitimately contains the
    # word "run" in prose — matching on the whole recipe made this test read the message, not the
    # command).
    invocation = next(line for line in result.stdout.splitlines()
                      if "stigmergy-librarian" in line and "echo" not in line)
    assert "--backend sdk" in invocation
    assert invocation.rstrip().endswith("once") or " once " in invocation   # one item, by hand
    assert " run" not in invocation


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
                 *agent_module.CREDENTIAL_ENV[:1]):
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
