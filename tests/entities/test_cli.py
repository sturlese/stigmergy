"""`entities.cli` — `stigmergy-entities`'s six subcommands, driven end-to-end through
`cli.main`: `create` (ADR 042 — a steward's registration becomes a CAPTURE the librarian writes
the page from; there is no deterministic birth any more, so the collision, secret-scan, drift and
rebase-retry regressions this file used to carry for it now live with the librarian's proposal
writer and the gates), the three decisions on what the librarian proposed (`approve`/`decline`/
`merge`, against a real bare remote and a real clone, which also write the review ledger),
`pending`, and `regenerate`.
"""
import io
import json
import os
import subprocess

import pytest

from stigmergy.capture import decisions
from stigmergy.capture import schema as capture_schema
from stigmergy.capture.evidence import MemoryEvidenceStore
from stigmergy.entities import cli, generator
from stigmergy.review_kinds import KIND_ALIAS_PROPOSAL, KIND_IDENTITY_PROPOSAL
from tests import testdb
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


# ══════════════════════════════════════════════════════════════════════════════════════════════
# `create` (ADR 042): a steward's registration is a CAPTURE — queued with the registration hints
# and what the steward said as its material; the librarian writes the page, and the entity is
# born confirmed by the steward. Real Postgres, the evidence store in memory (the bytes are not the
# claim here; the row and its hints are).
# ══════════════════════════════════════════════════════════════════════════════════════════════
@pytest.fixture()
def queue_conn(monkeypatch):
    conn = testdb.connect_or_skip("entities-create")
    capture_schema.ensure_capture_schema(conn)
    with conn.cursor() as cur:
        cur.execute("TRUNCATE capture_queue RESTART IDENTITY")
    monkeypatch.setattr(cli.evidence, "store_from_env", lambda env=None: MemoryEvidenceStore())
    yield conn
    conn.close()


def _queued(conn, submission_id):
    with conn.cursor() as cur:
        cur.execute("SELECT status, submitted_by, hints, payload FROM capture_queue WHERE id = %s",
                    (submission_id,))
        status, by, hints, payload = cur.fetchone()
    return status, by, hints, payload


def test_create_commissions_a_capture_carrying_the_registration_and_what_the_steward_said(queue_conn):
    """OLD BEHAVIOUR: `create` rendered the template with the name filled in and pushed a commit
    — an entity page with nothing said about the entity, twelve of which accumulated in the first
    brain. Now it queues ONE capture: `register_*` hints name the entity, type, spellings and door;
    the material is the steward's own account; `submitted_by` is the steward the page will be
    born confirmed by. Nothing touches git here."""
    rc, out, err = run_cli("--dsn", testdb.dsn(), "--json", "create", "--id", "globex",
                           "--name", "Globex", "--type", "organization", "--aliases", "Globex Corp, GX",
                           "--about", "Globex is the conglomerate we pilot reporting automation with.",
                           "--by", "steward@example.com")
    assert rc == 0, err
    ack = json.loads(out)
    assert ack["status"] == "queued" and ack["entity_id"] == "globex" and ack["name"] == "Globex"
    status, by, hints, payload = _queued(queue_conn, ack["id"])
    assert (status, by) == ("queued", "steward@example.com")
    registration = capture_schema.registration_from_hints(hints)
    assert registration == capture_schema.Registration(
        name="Globex", entity_type="organization", aliases=("Globex Corp", "GX"),
        source=decisions.SOURCE_CLI)
    assert hints["client"]["entity"] == "Globex"
    assert payload["text"].startswith("Globex is the conglomerate")


def test_create_prints_the_capture_to_follow_and_names_who_confirms_it(queue_conn):
    """A message containing a command is an executable promise: the sentence names
    `stigmergy-queue show <id>`, and the id it names is the row that was queued."""
    rc, out, _err = run_cli("--dsn", testdb.dsn(), "create", "--id", "globex", "--name", "Globex",
                            "--type", "organization", "--about", "A conglomerate.",
                            "--by", "steward@example.com")
    assert rc == 0
    assert "commissioned — capture #1" in out and "stigmergy-queue show 1" in out
    assert "born confirmed by steward@example.com" in out
    assert _queued(queue_conn, 1)[0] == "queued"


def test_create_refuses_an_empty_account_and_queues_nothing(queue_conn):
    """`--about` is required by the parser; an account that is whitespace gets past it and is
    refused by name, with nothing queued — a page with nothing said about the entity is the
    defect this command used to produce."""
    rc, _out, err = run_cli("--dsn", testdb.dsn(), "create", "--id", "globex", "--name", "Globex",
                            "--type", "organization", "--about", "   ", "--by", "steward@example.com")
    assert rc == cli.EXIT_REFUSED and "--about is empty" in err
    with queue_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM capture_queue")
        assert cur.fetchone()[0] == 0


def test_create_without_about_is_an_argparse_refusal_that_names_the_flag(queue_conn, capsys):
    with pytest.raises(SystemExit) as caught:
        cli.main(["--dsn", testdb.dsn(), "create", "--id", "globex", "--name", "Globex",
                  "--type", "organization", "--by", "steward@example.com"])
    assert caught.value.code == 2
    assert "--about" in capsys.readouterr().err


def test_create_refuses_an_id_that_is_not_the_names_slug(queue_conn):
    """The registry is derived from the page, so an id nothing regenerates would vanish at the
    next regenerate — refused before anything is queued, as it was refused before the mint."""
    rc, _out, err = run_cli("--dsn", testdb.dsn(), "create", "--id", "acme", "--name", "Globex",
                            "--type", "organization", "--about", "A conglomerate.",
                            "--by", "steward@example.com")
    assert rc == cli.EXIT_REFUSED and "not the slug of --name" in err and "'globex'" in err
