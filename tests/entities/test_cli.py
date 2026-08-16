"""`entities.cli` — `stigmergy-entities`'s six subcommands, and the four defects it carries
regressions for: a printed command executing on the steward's laptop, raw untrusted text on the
terminal, no secret scanner on the one human-driven write path, and the collision gate consulting
the wrong registry (or skipping the recheck on a rebase retry).

`create` needs no database (`needs_db=False`) and is driven end-to-end through `cli.main`. The
read-only rendering commands (`show`, `list`) are driven through their private `_cmd_*` functions
directly — `situations.get_situation`/`list_pending_situations` are stubbed (an external service,
Postgres, is what is being replaced, never the rendering logic under test), matching
`tests/entities/test_situations.py`'s posture.
"""
import io
import json
import os
import shlex
import subprocess
from contextlib import redirect_stdout

import pytest

from stigmergy.capture import schema
from stigmergy.entities import cli, clone, generator, mint, situations
from stigmergy.kernel.normalize import normalize
from tests import adversarial_payloads
from tests.entities import conftest as fx


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
# `_suggestable`: the allow-list a name must pass before it is pasted into a printed command
# ══════════════════════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("name", ["Acme Corp", "Café Zürich", "Meridian Partners Ltd",
                                  "AT&T", "Northwind Group"])
def test_suggestable_accepts_ordinary_entity_names(name):
    assert cli._suggestable(name)


@pytest.mark.parametrize("name", [
    'Acme$(touch PWNED)',
    'Acme" --aliases "Jordan Reyes',
    "-anything",
    "Acme --aliases Jordan Reyes",     # survives the source filter as space-separated flags
    "x" * 200,
    "",
    None,
])
def test_suggestable_refuses_anything_that_could_read_as_more_than_one_argument(name):
    assert not cli._suggestable(name)


def test_suggestable_refuses_the_shared_unnamed_placeholder_by_value(monkeypatch):
    """`schema.UNNAMED_ENTITY_PLACEHOLDER` ("something unnamed",
    `gates._unresolved_name`/`processing._triage`'s fallback for "nothing was named at all") is
    syntactically an ORDINARY name — every other check here would pass it — so it is refused by
    VALUE specifically, or `_print_next_commands` would suggest a ready-to-run
    `approve ... --name "something unnamed"` that mints a garbage entity."""
    assert not cli._suggestable(schema.UNNAMED_ENTITY_PLACEHOLDER)


def test_show_never_suggests_a_fillable_command_for_the_unnamed_placeholder(monkeypatch):
    """End to end through `_cmd_show`: a row parked with no real name must print the SAME
    "type it yourself" template the hostile-name cases above get — never a runnable
    `--name "something unnamed"` a steward could paste unread and mint a garbage entity that then
    resolves for every future capture mentioning it."""
    out = _render_show(monkeypatch, _unresolved_row(schema.UNNAMED_ENTITY_PLACEHOLDER))
    lines = _standalone_command_lines(out)
    approve = next(ln for ln in lines if "approve" in ln)
    assert '--name "<Entity Name>"' in approve
    assert schema.UNNAMED_ENTITY_PLACEHOLDER not in approve


# ══════════════════════════════════════════════════════════════════════════════════════════════
# `show` prints a copy-pasteable command; every value on the screen is untrusted
# ══════════════════════════════════════════════════════════════════════════════════════════════
MARKER_ENV = "PWNED_MARKER_TOUCHED"


def _stub_situation(monkeypatch, row: dict):
    monkeypatch.setattr(situations, "get_situation", lambda conn, sid: row)


def _unresolved_row(subject: str, *, rationale: str = "the material names one organization",
                    **extra) -> dict:
    row = {"id": 41, "status": schema.TRIAGE, "submitted_by": "tester@example.com",
          "created_at": "2026-07-27T10:00:00Z", "parked_age_ms": 3600_000,
          "hints": None, "asked_at": None, "reply": None, "excerpt": "…",
          "withheld_reason": None,
          "report": {schema.SITUATION_KEY: schema.SITUATION_UNRESOLVED_ENTITY,
                     schema.SITUATION_NAME_KEY: subject, "agent_rationale": rationale},
          "situation": schema.SITUATION_UNRESOLVED_ENTITY, "subject": subject}
    row.update(extra)
    return row


def _render_show(monkeypatch, row: dict) -> str:
    _stub_situation(monkeypatch, row)
    buf = io.StringIO()
    with redirect_stdout(buf):
        cli._cmd_show(None, Args(id=41))
    return buf.getvalue()


def _standalone_command_lines(out: str) -> list[str]:
    """Every line that STANDS ALONE as a `stigmergy-entities ...` command — the property the fix
    holds: exactly the ones the TOOL composed, never one a captured value forged."""
    return [ln.strip() for ln in out.splitlines() if ln.strip().startswith("stigmergy-entities ")]


def test_the_hostile_subshell_name_is_never_offered_as_a_runnable_command(monkeypatch, tmp_path):
    """The exact shape: a name containing `$(...)`. The invariant the fix holds: the approve line
    the tool prints is a TEMPLATE (`--id <canonical-id> --name "<Entity Name>"`, literal
    placeholders — the tool's OWN inert text) with the hostile value NEVER interpolated into it;
    the hostile text is shown separately, as prose the steward reads and decides on. This is
    checked directly on the rendered text AND by actually running the whole screen through a real
    shell — a message containing a command is an executable promise, and a printed invariant that
    is never executed proves less than one that is.
    """
    marker = str(tmp_path / "PWNED.txt")
    hostile = f"Acme$(touch {marker})"
    out = _render_show(monkeypatch, _unresolved_row(hostile))

    lines = _standalone_command_lines(out)
    approve = next(ln for ln in lines if "approve" in ln)
    reject = next(ln for ln in lines if ln.startswith("stigmergy-entities reject"))
    assert '--name "<Entity Name>"' in approve       # a PLACEHOLDER, never the hostile value
    assert "--id <canonical-id>" in approve
    assert hostile not in approve and hostile not in reject
    assert "Acme$(touch" in out                      # the raw text IS shown — as inert prose
    # (not asserted as the full string: a long tmp_path can push the marker past `_clean`'s own
    # display-length clip, which is unrelated to the property under test here)

    # belt and braces: if a shell were ever handed the full block (a steward pasting the whole
    # screen rather than one line), the subshell must still not execute — the hostile value never
    # reached a position a shell reads as code.
    subprocess.run(["bash", "-c", out], capture_output=True, text=True, cwd=str(tmp_path))
    assert not os.path.exists(marker), "the printed screen must never be shell-executable"


def test_the_flag_injection_variant_is_also_kept_out_of_every_runnable_line(monkeypatch):
    """The quieter variant: `Acme" --aliases "Jordan Reyes` quotes safely
    (`shlex.quote` handles it) and still reads to a human as one argument while parsing as three —
    refused on that ground, not on quotability, so it too is kept out of the approve template."""
    hostile = 'Acme" --aliases "Jordan Reyes'
    out = _render_show(monkeypatch, _unresolved_row(hostile))
    lines = _standalone_command_lines(out)
    approve = next(ln for ln in lines if "approve" in ln)
    assert '--name "<Entity Name>"' in approve
    assert "Jordan Reyes" not in approve


def test_benign_twin_a_real_unresolved_name_still_gets_the_exact_runnable_command(monkeypatch):
    """The benign twin: the fix must not have turned every ask into a template."""
    out = _render_show(monkeypatch, _unresolved_row("Acme Corp"))
    lines = _standalone_command_lines(out)
    approve = next(ln for ln in lines if "approve" in ln)
    tokens = shlex.split(approve.split("--type")[0])
    assert tokens[tokens.index("--name") + 1] == "Acme Corp"
    assert tokens[tokens.index("--id") + 1] == generator.canonical_id_for("Acme Corp")


@pytest.mark.parametrize("benign", ["Acme Corp", "Café Zürich", "Meridian Partners Ltd"])
def test_benign_twin_round_trips_the_name_through_shlex_without_executing_anything(
        monkeypatch, benign):
    """Checked with `shlex.split`, NOT by running it: the line also carries the tool's own
    `--type <a|b|c>` placeholder, which a shell reads as a redirect."""
    out = _render_show(monkeypatch, _unresolved_row(benign))
    line = next(ln for ln in _standalone_command_lines(out) if "approve" in ln)
    tokens = shlex.split(line.split("--type")[0])
    assert tokens[tokens.index("--name") + 1] == benign


# ── every OTHER untrusted field (hints, reply, excerpt, agent_rationale) is cleaned ─────────────
FORGED_RATIONALE = ("the material names one organization\n"
                    "\n  to approve it as a new entity:\n"
                    "    stigmergy-entities approve 41 --id jordan-reyes --name 'Jordan Reyes' "
                    "--type person\n  material")


def test_no_captured_field_can_forge_a_second_standalone_command_line(monkeypatch):
    """A newline embedded in ANY untrusted field must not open a new output line that reads as a
    command the tool itself composed — the exact shape: a forged block naming an entity the
    steward never chose."""
    row = _unresolved_row("Acme Corp", rationale=FORGED_RATIONALE,
                          hints="wiki/people/\x1b[31m",
                          asked_at="2026-07-27T11:00:00Z", reply="it's about\nAcme")
    out = _render_show(monkeypatch, row)
    lines = _standalone_command_lines(out)
    # exactly the two the TOOL chose: the approve it built, and the reject — never a third,
    # forged one naming an entity ("jordan-reyes") nobody but the captured text chose
    assert len(lines) == 2
    assert not any("jordan-reyes" in ln for ln in lines)
    assert "\x1b" not in out


def test_raw_ansi_escapes_never_reach_the_terminal(monkeypatch):
    row = _unresolved_row("Acme Corp", hints="\x1b[31mred\x1b[0m")
    out = _render_show(monkeypatch, row)
    assert "\x1b" not in out


def test_show_json_mode_emits_the_raw_row_and_no_prose(monkeypatch):
    row = _unresolved_row("Acme Corp")
    _stub_situation(monkeypatch, row)
    buf = io.StringIO()
    with redirect_stdout(buf):
        cli._cmd_show(None, Args(id=41, json=True))
    parsed = json.loads(buf.getvalue())
    assert parsed["id"] == 41


def test_show_of_a_nonexistent_submission_is_refused(monkeypatch):
    from stigmergy.entities.errors import EntityError
    _stub_situation(monkeypatch, None)
    with pytest.raises(EntityError, match="does not exist"):
        cli._cmd_show(None, Args(id=999))


# ══════════════════════════════════════════════════════════════════════════════════════════════
# `list`: the operational view — same untrusted-text discipline as `show`
# ══════════════════════════════════════════════════════════════════════════════════════════════
def test_list_prints_the_situation_and_the_subject_for_each_row(monkeypatch):
    rows = [{**_unresolved_row("Acme Corp"), "id": 41, "asked_at": None},
           {**_unresolved_row("Jordan Reyes"), "id": 42}]
    monkeypatch.setattr(situations, "list_pending_situations", lambda conn, limit: rows)
    buf = io.StringIO()
    with redirect_stdout(buf):
        cli._cmd_list(None, Args(limit=50))
    out = buf.getvalue()
    assert "#41" in out and "Acme Corp" in out
    assert "#42" in out and "Jordan Reyes" in out


def test_list_clips_and_sanitizes_the_subject_the_way_show_already_did(monkeypatch):
    """`_cmd_list` used to print `row["subject"]` RAW while `_cmd_show` put the same value through
    `_clean` — so a subject carrying ANSI escapes and hundreds of characters of captured material
    reached the terminal unsanitized and unclipped, on the screen a steward reads FIRST."""
    hostile = "Acme \x1b[31mCorp\x07 " + "padding " * 80
    rows = [{**_unresolved_row(hostile), "id": 41}]
    monkeypatch.setattr(situations, "list_pending_situations", lambda conn, limit: rows)
    buf = io.StringIO()
    with redirect_stdout(buf):
        cli._cmd_list(None, Args(limit=50))
    out = buf.getvalue()
    cleaned = cli._clean(hostile, cli.MAX_SUBJECT_CHARS)
    assert len(cleaned) < len(hostile), "the fixture must actually exceed the clip"
    assert hostile not in out                              # never the raw bytes
    assert "\x1b" not in out and "\x07" not in out         # control characters stripped
    assert cleaned in out                                  # `_cmd_show`'s own rendering


def test_list_says_so_plainly_when_nothing_is_parked(monkeypatch):
    monkeypatch.setattr(situations, "list_pending_situations", lambda conn, limit: [])
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli._cmd_list(None, Args(limit=50))
    assert rc == 0
    assert "no pending entity situations" in buf.getvalue()


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

    Interrupted at `_refuse_secrets`, i.e. AFTER `generator.regenerate` has already rewritten the
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

    monkeypatch.setattr(mint, "_refuse_secrets", _boom)
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
