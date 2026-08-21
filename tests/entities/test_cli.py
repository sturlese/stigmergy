"""`entities.cli` — `stigmergy-entities`'s six subcommands, driven end-to-end through
`cli.main` against a real bare remote and a real clone: the birth (`create`), the three decisions
on what the librarian proposed (`approve`/`decline`/`merge`, which also write the review ledger),
`pending`, and `regenerate`. The defects it carries regressions for: no secret scanner on a
human-driven write path, the collision gate consulting the wrong registry (or skipping the recheck
on a rebase retry), and an interrupted command leaving the steward's clone dirty.
"""
import io
import json
import os
import subprocess

import pytest

from stigmergy.capture import decisions
from stigmergy.entities import cli, clone, generator, mint
from stigmergy.kernel.normalize import normalize
from stigmergy.review_kinds import KIND_ALIAS_PROPOSAL, KIND_IDENTITY_PROPOSAL
from tests import adversarial_payloads, testdb
from tests.entities import conftest as fx
from tests.entities.test_decide import _commit_all, _note, _proposed_page, _write


class Args:
    """A stand-in for `argparse.Namespace` — only the attributes each `_cmd_*` reads."""

    def __init__(self, **kwargs):
        self.json = False
        for key, value in kwargs.items():
            setattr(self, key, value)


def run_cli(*argv) -> tuple[int, str, str]:
    """`cli.main(argv)`, capturing stdout/stderr — for the commands that need no database
    (`create`, `regenerate`), driven exactly as an operator would."""
    out, err = io.StringIO(), io.StringIO()
    import contextlib
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = cli.main(list(argv))
    return rc, out.getvalue(), err.getvalue()


# ══════════════════════════════════════════════════════════════════════════════════════════════
# `create`: the birth path with no queue row — driven end-to-end through `cli.main`, no database
# ══════════════════════════════════════════════════════════════════════════════════════════════
def test_create_benign_twin_mints_commits_and_pushes(repo):
    """Resolve-before-mint's benign twin at the CLI layer: a genuinely new entity passes end to
    end."""
    remote, steward = repo
    rc, out, _err = run_cli("--repo", steward, "--branch", "main", "create",
                            "--id", "globex", "--name", "Globex", "--type", "organization",
                            "--aliases", "Globex Corporation", "--today", "2026-07-27")
    assert rc == 0, out
    assert "Globex" in fx.remote_files(remote)[0] or any(
        "Globex" in p for p in fx.remote_files(remote))
    body = subprocess.run(["git", "show", "main:wiki/entities/Globex.md"], cwd=remote,
                          capture_output=True, text=True, check=True).stdout
    assert 'title: "Globex"' in body
    trailer = subprocess.run(["git", "log", "-1", "--format=%an <%ae>"], cwd=remote,
                             capture_output=True, text=True, check=True).stdout.strip()
    assert trailer == f"{fx.STEWARD_NAME} <{fx.STEWARD_EMAIL}>"


def test_create_json_mode_reports_the_commit_and_the_entity(repo):
    _remote, steward = repo
    rc, out, _err = run_cli("--repo", steward, "--branch", "main", "--json", "create",
                            "--id", "globex", "--name", "Globex", "--type", "organization",
                            "--today", "2026-07-27")
    assert rc == 0
    payload = json.loads(out)
    assert payload["entity_id"] == "globex"
    assert len(payload["commit"]) == 40


# ── refuses a collision, naming it, at the CLI layer ─────────────────────────────────────────────
def test_create_refuses_a_collision_and_names_the_registered_entry(repo):
    _remote, steward = repo
    rc, _out, err = run_cli("--repo", steward, "--branch", "main", "create",
                            "--id", "jordan-reyes", "--name", "Jordan Reyes",
                            "--type", "person", "--today", "2026-07-27")
    assert rc == cli.EXIT_REFUSED
    assert "already resolves to the registered entity" in err
    assert "jordan-reyes" in err


# ── the CLI door keeps its full local diagnostics (issue #57, ADR 030's two-door amendment) ─────
def test_the_cli_door_still_names_the_stewards_own_clone_when_the_template_is_missing(repo):
    """The SERVER door now maps this refusal's TYPE (`TemplateMissingError`) to a sentence written
    for a steward with no clone. This door must not have moved a byte: the operator running
    `stigmergy-entities` IS standing in the clone the message names, and the path is the whole
    diagnosis — which of their checkouts is missing the template.

    Pinned byte-for-byte rather than by substring, because the two doors' wordings are now free to
    diverge and nothing else would notice this one drifting toward the other. Reached through
    `cli.main` (a real refusal, a real exit code, the real stderr line), not by asserting on the
    exception, so what is pinned is what an operator actually reads.
    """
    _remote, steward = repo
    os.remove(os.path.join(steward, "ops", "templates", "entity.md"))
    fx.git("commit", "--quiet", "--all", "-m", "chore: drop the entity template", cwd=steward)
    fx.git("push", "--quiet", "origin", "main", cwd=steward)

    rc, _out, err = run_cli("--repo", steward, "--branch", "main", "create",
                            "--id", "globex", "--name", "Globex", "--type", "organization",
                            "--today", "2026-07-27")

    assert rc == cli.EXIT_REFUSED
    assert err == (
        f"stigmergy-entities: {mint.TEMPLATE_RELPATH} is missing from {steward} — a new entity "
        f"page is that template with its identity fields filled in, and this command does not "
        f"carry its own copy (the template is the knowledge repo's own source of truth for the "
        f"page's shape)\n")


# ══════════════════════════════════════════════════════════════════════════════════════════════
# The one human-driven write path used to run no secret scanner
# ══════════════════════════════════════════════════════════════════════════════════════════════
# The shared fixture PAT, NOT a second literal of the same shape. `adversarial_payloads`' own
# docstring makes the argument: the constant lives in one place "since each copy would need its
# own exemption and each exemption is a place a real credential could later hide." A copy was
# written here once anyway, and the repo-wide `gitleaks detect` in CI is what caught it.
SEEDED_SECRET = f"the client's deploy bot, token {adversarial_payloads.GITHUB_PAT}"


def test_create_refuses_a_seeded_secret_in_role_and_names_the_rule(repo, require_gitleaks):
    """Assert the MECHANISM fired, not just the outcome: the message must name gitleaks' OWN rule
    id, not merely say "refused" for some other reason. `ghp_...` is a github-pat shape chosen for
    this reproduction (not real, not valid anywhere) — never the well-known
    `AKIAIOSFODNN7EXAMPLE`, which is on gitleaks' own allowlist and would prove nothing."""
    remote, steward = repo
    rc, _out, err = run_cli("--repo", steward, "--branch", "main", "create",
                            "--id", "globex", "--name", "Globex", "--type", "organization",
                            "--role", SEEDED_SECRET, "--today", "2026-07-27")
    assert rc == cli.EXIT_REFUSED
    assert "secret scanner matched" in err
    assert "rule: github-pat" in err
    assert "Globex" not in fx.remote_files(remote)
    # the rollback: no orphaned page, no leftover registry edit
    status = subprocess.run(["git", "status", "--porcelain"], cwd=steward,
                            capture_output=True, text=True, check=True).stdout.strip()
    assert status == "", f"the clone was left dirty after a refused create: {status!r}"


def test_create_benign_twin_an_ordinary_role_with_no_secret_shape_passes(repo, require_gitleaks):
    """The benign twin: the scanner must not have become trigger-happy on ordinary prose."""
    _remote, steward = repo
    rc, _out, err = run_cli("--repo", steward, "--branch", "main", "create",
                            "--id", "globex", "--name", "Globex", "--type", "organization",
                            "--role", "the client's deploy tooling vendor",
                            "--today", "2026-07-27")
    assert rc == 0, err


# ══════════════════════════════════════════════════════════════════════════════════════════════
# The collision gate used to consult the COMMITTED registry while the commit published the
# DERIVED one — an unregistered page (drift) made the gate blind to exactly the collision
# `--check` exists to find.
# ══════════════════════════════════════════════════════════════════════════════════════════════
def test_create_refuses_to_mint_into_a_clone_with_pre_existing_drift(tmp_path):
    remote, steward = fx.build_repo(str(tmp_path / "git"),
                                    extra_pages=[("Acme Corp", "organization", ())])
    drift = generator.check(steward)
    assert drift.divergences, "the fixture must start drifted for this reproduction to mean anything"

    rc, _out, err = run_cli("--repo", steward, "--branch", "main", "create",
                            "--id", "acme", "--name", "Acme", "--type", "organization",
                            "--today", "2026-07-27")

    assert rc == cli.EXIT_REFUSED
    assert generator.FIX_COMMAND in err
    # the regression this pins: the OLD code passed both checks here and published TWO registry
    # entries whose matcher keys collapse onto one ("acme" / "Acme Corp") — the fix refuses before
    # any of that, so nothing new landed on the remote at all.
    assert "Acme.md" not in fx.remote_files(remote)
    registry = fx.remote_registry(remote)
    assert "acme" not in registry["entities"]


def test_create_benign_twin_a_clean_clone_with_no_drift_still_mints(repo):
    _remote, steward = repo
    assert generator.check(steward).divergences == []
    rc, _out, err = run_cli("--repo", steward, "--branch", "main", "create",
                            "--id", "globex", "--name", "Globex", "--type", "organization",
                            "--today", "2026-07-27")
    assert rc == 0, err


# ══════════════════════════════════════════════════════════════════════════════════════════════
# The post-rebase retry used to regenerate without re-checking collisions. A retry path needs a
# test that actually loses the race, so the race is forced deterministically by landing steward
# A's commit inside steward B's `write_page` — the window `commit_and_push`'s retry loop exists for.
# ══════════════════════════════════════════════════════════════════════════════════════════════
def _race(tmp_path, label: str, b_args: list[str]) -> tuple[str, str, int]:
    remote, steward_b = fx.build_repo(str(tmp_path / f"git-{label}"))
    steward_a = fx.clone_of(remote, str(tmp_path / f"clone-a-{label}"), name="Steward A",
                            email="a@example.com")

    original_write_page = clone.write_page
    landed = []

    def racing_write_page(repo, relpath, text):
        if not landed:
            landed.append(True)
            rc, _o, _e = run_cli("--repo", steward_a, "--branch", "main", "create",
                                 "--id", "acme", "--name", "Acme", "--type", "organization",
                                 "--today", "2026-07-27")
            landed.append(rc)
        return original_write_page(repo, relpath, text)

    import stigmergy.entities.cli as cli_mod
    cli_mod.clone.write_page = racing_write_page
    try:
        rc, _out, err = run_cli("--repo", steward_b, "--branch", "main", "create", *b_args,
                                "--today", "2026-07-27")
    finally:
        cli_mod.clone.write_page = original_write_page
    return remote, err, rc


def test_the_post_rebase_retry_still_refuses_an_alias_that_now_collides(tmp_path):
    """Steward A mints `Acme`; steward B (racing) mints `Zenith Systems` with `Acme` as an alias —
    B's own preflight passed against a registry that no longer exists by the time B's commit would
    land. The FIX must refuse B at the retry, naming the collision, rather than silently landing
    an ambiguous `by_alias` entry (last-wins)."""
    remote, err, rc = _race(tmp_path, "attack",
                            ["--id", "zenith-systems", "--name", "Zenith Systems",
                             "--type", "organization", "--aliases", "Acme"])

    assert rc == cli.EXIT_REFUSED, err
    assert "collision did not exist when the command started" in err

    registry = fx.remote_registry(remote)
    keys: dict[str, list[str]] = {}
    for canonical_id, entity in registry["entities"].items():
        for spelling in (entity["name"], *entity.get("aliases", ())):
            keys.setdefault(normalize(spelling), []).append(canonical_id)
    ambiguous = {k: v for k, v in keys.items() if len(set(v)) > 1}
    assert not ambiguous, f"a spelling resolves to more than one entity: {ambiguous}"


def test_the_post_rebase_retry_benign_twin_two_unrelated_entities_both_land(tmp_path):
    """The benign twin — the race the retry loop was BUILT for: two stewards, two unrelated
    entities. B must still fetch, rebase, regenerate and land — the fix must not turn every race
    into a refusal."""
    remote, err, rc = _race(tmp_path, "benign",
                            ["--id", "zenith-systems", "--name", "Zenith Systems",
                             "--type", "organization"])
    assert rc == 0, err
    registry = fx.remote_registry(remote)
    assert {"acme", "zenith-systems"} <= set(registry["entities"])


# ══════════════════════════════════════════════════════════════════════════════════════════════
# `--repo`: what the three operator CLIs agree a checkout is
# ══════════════════════════════════════════════════════════════════════════════════════════════
def test_repo_accepts_a_real_git_worktree_checkout(repo, tmp_path):
    """**Old behaviour: `isdir(os.path.join(path, ".git"))` refused a genuine worktree.** A
    `git worktree add` checkout carries a `.git` FILE (a `gitdir:` pointer), not a directory, so
    this door told a steward working in one that it "is not a git checkout" — while
    `stigmergy-views`, pointed at the same directory, accepted it. Two commands, one checkout, two
    answers about what it is.

    A REAL worktree, not a hand-written `.git` file: what is being pinned is that git's own
    on-disk shape is accepted, and a fixture that writes the pointer itself only proves the
    predicate matches the fixture.
    """
    _remote, steward = repo
    worktree = str(tmp_path / "worktree")
    subprocess.run(["git", "worktree", "add", "-b", "steward-wt", worktree], cwd=steward,
                   check=True, capture_output=True, text=True)
    assert os.path.isfile(os.path.join(worktree, ".git")), "fixture: git must have written a FILE"

    assert cli._repo(Args(repo=worktree)) == os.path.abspath(worktree)


def test_repo_still_refuses_a_plain_directory_in_the_same_words(tmp_path):
    """The benign twin: widening to `exists` must not turn the guard off. A directory with no
    `.git` at all is still refused, and still with the sentence a steward may already have
    searched for."""
    from stigmergy.entities.errors import EntityError

    plain = str(tmp_path / "not-a-checkout")
    os.makedirs(plain)
    with pytest.raises(EntityError) as caught:
        cli._repo(Args(repo=plain))
    assert str(caught.value) == (
        f"{os.path.abspath(plain)} is not a git checkout — `--repo` (or $STIGMERGY_REPO) must "
        f"point at your clone of the knowledge repo, because every command here commits to it "
        f"with your own git identity")


@pytest.mark.parametrize("content, label", [
    (b"notes to self\n", "junk text"),
    (b"\x00\x01\x02\xff\xfe", "binary"),
    (b"", "empty"),
])
def test_repo_refuses_a_directory_whose_dot_git_is_a_junk_file(tmp_path, content, label):
    """**Old behaviour: `os.path.exists(path/".git")` accepted ANY `.git` entry.** Widening from
    `isdir` to `exists` for the sake of `git worktree add` also accepted a stray FILE named `.git`
    — a leftover, a note, a binary — as a checkout. The command then committed with the steward's
    own identity into a directory git does not manage, and the refusal a steward could have acted
    on never fired.

    A real worktree's `.git` file is a `gitdir:` pointer, so that prefix is the requirement now;
    an unreadable or binary one refuses rather than raising, because a predicate that throws is
    worse for the caller than one that says no. Its benign twin is
    `test_repo_accepts_a_real_git_worktree_checkout` above, which is driven by git itself."""
    from stigmergy.entities.errors import EntityError

    junk = tmp_path / f"junk-{label}"
    junk.mkdir()
    (junk / ".git").write_bytes(content)

    with pytest.raises(EntityError, match="is not a git checkout"):
        cli._repo(Args(repo=str(junk)))


def test_the_checkout_predicate_answers_false_for_an_unreadable_dot_git_never_raises(tmp_path):
    """The predicate is a PREDICATE (see its docstring): reading the pointer must not turn an
    odd filesystem into an exception the CLIs have no arm for — each of them writes its own
    refusal around a `False`, and a raise from here would surface as a traceback instead."""
    from stigmergy.librarian import config as librarian_config

    dangling = tmp_path / "dangling"
    dangling.mkdir()
    os.symlink(str(tmp_path / "nowhere"), str(dangling / ".git"))

    assert librarian_config.is_repo_checkout(str(dangling)) is False


# ══════════════════════════════════════════════════════════════════════════════════════════════
# `regenerate`: idempotence's own CLI surface
# ══════════════════════════════════════════════════════════════════════════════════════════════
def test_regenerate_check_is_clean_on_a_fresh_repo(repo):
    _remote, steward = repo
    rc, out, _err = run_cli("--repo", steward, "regenerate", "--check")
    assert rc == 0
    assert "no drift" in out


def test_regenerate_check_exits_non_zero_on_a_planted_drift_and_names_it(repo):
    _remote, steward = repo
    with open(os.path.join(steward, "wiki", "entities", "Globex.md"), "w") as f:
        f.write(fx.page_text("Globex", "organization", []))
    rc, _out, err = run_cli("--repo", steward, "regenerate", "--check")
    assert rc == cli.EXIT_REFUSED
    assert "Globex" in err
    assert generator.FIX_COMMAND in err


def test_regenerate_without_check_writes_locally_but_does_not_commit(repo):
    _remote, steward = repo
    with open(os.path.join(steward, "wiki", "entities", "Globex.md"), "w") as f:
        f.write(fx.page_text("Globex", "organization", []))
    rc, out, _err = run_cli("--repo", steward, "regenerate")
    assert rc == 0
    assert "NOT committed" in out
    status = subprocess.run(["git", "status", "--porcelain"], cwd=steward,
                            capture_output=True, text=True, check=True).stdout
    assert "ops/entity-registry.json" in status   # written, but uncommitted


def test_regenerate_check_json_mode(repo):
    _remote, steward = repo
    rc, out, _err = run_cli("--repo", steward, "--json", "regenerate", "--check")
    assert rc == 0
    payload = json.loads(out)
    assert payload["drift"] is False


# ══════════════════════════════════════════════════════════════════════════════════════════════
# interrupt handling: a REAL KeyboardInterrupt during the write path, never a stubbed handler
# ══════════════════════════════════════════════════════════════════════════════════════════════
def test_a_keyboard_interrupt_during_create_is_answered_cleanly_not_with_a_traceback(
        repo, monkeypatch):
    _remote, steward = repo

    def _boom(*a, **kw):
        raise KeyboardInterrupt

    monkeypatch.setattr(clone, "commit_and_push", _boom)
    rc, _out, err = run_cli("--repo", steward, "--branch", "main", "create",
                            "--id", "globex", "--name", "Globex", "--type", "organization",
                            "--today", "2026-07-27")
    assert rc == cli.EXIT_INTERRUPTED
    assert "Traceback" not in err
    assert "interrupted" in err
    assert "not pushed" in err or "local clone" in err


def test_a_keyboard_interrupt_before_the_commit_rolls_the_clone_back(repo, monkeypatch):
    """**Old behaviour: Ctrl-C during the pre-commit gates left the clone dirty.** `mint`'s
    rollback arm was `except Exception`, which cannot see a `KeyboardInterrupt` — so an operator
    who interrupted the window between `write_page` and the commit (the registry regeneration and
    the gitleaks scan, the slowest thing here and therefore the likeliest moment to hit Ctrl-C)
    was left with an untracked entity page AND a rewritten `ops/entity-registry.json`. The next
    `create`/`approve` then refused on `ensure_clean` — a dirty tree it had made itself, blamed on
    the steward's own work. `views/regenerate.run` names `KeyboardInterrupt` explicitly for the
    identical window; this one only had to widen to `BaseException`.

    Interrupted at `refuse_secrets`, i.e. AFTER `generator.regenerate` has already rewritten the
    registry: interrupting the try block's first statement would restore a registry nothing had
    touched yet and prove nothing about the rollback.
    """
    _remote, steward = repo
    registry_path = os.path.join(steward, "ops", "entity-registry.json")
    page_path = os.path.join(steward, "wiki", "entities", "Globex.md")
    with open(registry_path, encoding="utf-8") as f:
        registry_before = f.read()

    def _boom(*a, **kw):
        raise KeyboardInterrupt

    monkeypatch.setattr(mint, "refuse_secrets", _boom)
    rc, _out, _err = run_cli("--repo", steward, "--branch", "main", "create",
                             "--id", "globex", "--name", "Globex", "--type", "organization",
                             "--today", "2026-07-27")

    assert rc == cli.EXIT_INTERRUPTED
    assert not os.path.exists(page_path), "the page this command wrote must not survive its own abort"
    with open(registry_path, encoding="utf-8") as f:
        assert f.read() == registry_before, "the registry must be back to its pre-mint bytes"
    status = subprocess.run(["git", "status", "--porcelain"], cwd=steward,
                            capture_output=True, text=True, check=True).stdout
    assert status == "", f"the clone must be clean again, not {status!r}"


# ══════════════════════════════════════════════════════════════════════════════════════════════
# the decisions: approve / decline / merge land a commit AND a ledger row; pending reads the clone
# ══════════════════════════════════════════════════════════════════════════════════════════════
@pytest.fixture()
def ledger():
    conn = testdb.connect_or_skip("entities-cli")
    from stigmergy.capture import schema as capture_schema
    capture_schema.ensure_capture_schema(conn)
    decisions.ensure_decisions_schema(conn)
    with conn.cursor() as cur:
        cur.execute("DELETE FROM review_decisions")
    yield conn
    conn.close()


@pytest.fixture()
def proposed(repo):
    """A clone the librarian left a proposal in: `Ledgerly` (proposed, one proposed spelling) and a
    note anchored to it; `Stigmergy` carries a proposed spelling too."""
    remote, clone_path = repo
    _write(clone_path, "wiki/entities/Ledgerly.md",
           _proposed_page("Ledgerly", proposed_aliases=["LDG"]))
    text = open(os.path.join(clone_path, "wiki/entities/Stigmergy.md"), encoding="utf-8").read()
    _write(clone_path, "wiki/entities/Stigmergy.md",
           text.replace("related: []", 'related: []\nproposed_aliases: ["Stig"]'))
    _write(clone_path, "wiki/notes/Ledgerly kickoff.md", _note("Ledgerly kickoff", ["ledgerly"]))
    generator.regenerate(clone_path)
    _commit_all(clone_path, "feat(note): the librarian proposed Ledgerly")
    return remote, clone_path


def _latest(conn, kind, item_id):
    return decisions.latest_decision_for(conn, item_kind=kind, item_id=item_id)


def test_pending_lists_the_proposed_identities_and_spellings_from_the_clone(proposed):
    _remote, steward = proposed
    rc, out, _err = run_cli("--repo", steward, "--json", "pending")
    assert rc == 0
    payload = json.loads(out)
    assert [e["id"] for e in payload["entities"]] == ["ledgerly"]
    assert {(a["entity_id"], a["alias"]) for a in payload["aliases"]} == {
        ("ledgerly", "LDG"), ("stigmergy", "Stig")}
    rc, out, _err = run_cli("--repo", steward, "pending")
    assert rc == 0 and "ledgerly" in out and "stigmergy-entities approve <id>" in out


def test_pending_says_so_plainly_when_everything_is_confirmed(repo):
    _remote, steward = repo
    rc, out, _err = run_cli("--repo", steward, "pending")
    assert rc == 0 and "nothing pending" in out


def test_approve_confirms_the_identity_pushes_and_records_the_ledger_row(proposed, ledger):
    remote, steward = proposed
    rc, out, _err = run_cli("--dsn", testdb.dsn(), "--repo", steward, "--json", "approve",
                            "ledgerly", "--today", "2026-08-21")
    assert rc == 0, out
    payload = json.loads(out)
    assert payload["kind"] == "approve-entity" and len(payload["commit"]) == 40
    entry = fx.remote_registry(remote)["entities"]["ledgerly"]
    assert entry["proposed"] is False
    assert entry["approved_by"] == fx.STEWARD_EMAIL
    row = _latest(ledger, KIND_IDENTITY_PROPOSAL, "ledgerly")
    assert row["verdict"] == decisions.APPROVE and row["source"] == decisions.SOURCE_CLI
    assert row["extra"]["commit"] == payload["commit"]


def test_decline_removes_the_page_reanchors_the_note_and_records_a_reject_the_librarian_reads(
        proposed, ledger):
    """The ledger row is the half that matters: `librarian.identity` reads the latest
    `identity-proposal` decision and refuses to propose `ledgerly` again."""
    remote, steward = proposed
    rc, out, _err = run_cli("--dsn", testdb.dsn(), "--repo", steward, "decline", "ledgerly",
                            "--reason", "a typo, not a company", "--by", "Ana",
                            "--today", "2026-08-21")
    assert rc == 0, out
    assert "declined" in out and "re-anchored: wiki/notes/Ledgerly kickoff.md" in out
    assert "wiki/entities/Ledgerly.md" not in fx.remote_files(remote)
    row = _latest(ledger, KIND_IDENTITY_PROPOSAL, "ledgerly")
    assert row["verdict"] == decisions.REJECT and row["actor"] == "Ana"
    assert row["notes"] == "a typo, not a company"


def test_merge_folds_the_proposal_into_the_survivor_and_records_where_it_went(proposed, ledger):
    remote, steward = proposed
    rc, out, _err = run_cli("--dsn", testdb.dsn(), "--repo", steward, "--json", "merge",
                            "ledgerly", "--into", "stigmergy", "--today", "2026-08-21")
    assert rc == 0, out
    payload = json.loads(out)
    assert payload["into"] == "stigmergy"
    survivor = fx.remote_registry(remote)["entities"]["stigmergy"]
    assert "Ledgerly" in survivor["aliases"] and "LDG" in survivor["aliases"]
    row = _latest(ledger, KIND_IDENTITY_PROPOSAL, "ledgerly")
    assert row["verdict"] == decisions.MERGE and row["extra"]["into"] == "stigmergy"


def test_alias_decisions_go_through_the_same_door_under_their_own_item_id(proposed, ledger):
    remote, steward = proposed
    rc, _out, _err = run_cli("--dsn", testdb.dsn(), "--repo", steward, "approve", "stigmergy",
                             "--alias", "Stig", "--today", "2026-08-21")
    assert rc == 0
    rc, _out, _err = run_cli("--dsn", testdb.dsn(), "--repo", steward, "decline", "ledgerly",
                             "--alias", "LDG", "--today", "2026-08-21")
    assert rc == 0
    registry = fx.remote_registry(remote)["entities"]
    assert "Stig" in registry["stigmergy"]["aliases"]
    assert registry["ledgerly"]["proposed_aliases"] == []
    assert _latest(ledger, KIND_ALIAS_PROPOSAL, "stigmergy:Stig")["verdict"] == decisions.APPROVE
    assert _latest(ledger, KIND_ALIAS_PROPOSAL, "ledgerly:LDG")["verdict"] == decisions.REJECT


def test_a_decision_on_a_confirmed_entity_is_refused_with_exit_1_and_no_ledger_row(proposed, ledger):
    _remote, steward = proposed
    rc, _out, err = run_cli("--dsn", testdb.dsn(), "--repo", steward, "decline", "jordan-reyes",
                            "--today", "2026-08-21")
    assert rc == cli.EXIT_REFUSED
    assert "confirmed entity, not a proposal" in err and "Traceback" not in err
    assert _latest(ledger, KIND_IDENTITY_PROPOSAL, "jordan-reyes") is None


def test_a_decision_needs_the_database_and_says_why(proposed):
    """A decline nobody recorded is one the librarian re-proposes: the ledger row is not optional,
    so the command refuses to run rather than pushing half a decision."""
    _remote, steward = proposed
    rc, _out, err = run_cli("--dsn", "postgresql://stigmergy:stigmergy@localhost:1/stigmergy_test",
                            "--repo", steward, "decline", "ledgerly")
    assert rc == cli.EXIT_CANNOT_RUN
    assert "review ledger" in err
