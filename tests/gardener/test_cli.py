"""`stigmergy-gardener` — the CLI entrypoint, driven in-process through `cli.main(argv)` against
real Postgres. `$STIGMERGY_INDEX_DSN` is already pinned to the test database for the whole session
(`tests/conftest.py::pytest_configure`), so no test here needs to pass `--dsn` explicitly. The
`conn`/`repo` fixtures come from `tests/gardener/conftest.py`.
"""
import ast
import datetime
import json
import os
import pathlib

from pydantic_ai.exceptions import AgentRunError

from stigmergy.digest import cli as digest_cli
from stigmergy.gardener import cli, sweep
from stigmergy.gardener.settings import SLACK_BOT_TOKEN_ENV
from stigmergy.slack.bolt_gateway import BoltSlackGateway
from tests.gardener import support


def _days_ago(n: int) -> str:
    # UTC, never local time: the checks this CLI drives age off Postgres's own `now()`/
    # `current_date` (Etc/UTC in this stack), so a fixture backdated from the MACHINE's local
    # calendar day drifts by one during the nightly window where local has already rolled to a new
    # day and UTC has not (e.g. 00:00-02:00 CEST) — an off-by-one age mismatch with nothing wrong
    # in the code. Do not simplify this back to `date.today()`.
    return (datetime.datetime.now(datetime.UTC).date() - datetime.timedelta(days=n)).isoformat()


# ── _gateway: the real-vs-none construction, without ever posting ─────────────────────────────
def test_gateway_is_none_when_no_bot_token_is_configured(monkeypatch):
    monkeypatch.delenv(SLACK_BOT_TOKEN_ENV, raising=False)
    assert cli._gateway() is None


def test_gateway_constructs_a_real_gateway_when_a_bot_token_is_configured(monkeypatch):
    monkeypatch.setenv(SLACK_BOT_TOKEN_ENV, "xoxb-test-token")
    gateway = cli._gateway()
    assert isinstance(gateway, BoltSlackGateway)


# ── connection failure: local and specific, mirroring capture.cli's own posture ────
def test_unreachable_database_prints_a_clean_message_and_exits_config(capsys):
    rc = cli.main(["--dsn", "postgresql://stigmergy:stigmergy@127.0.0.1:1/nope"])
    err = capsys.readouterr().err
    assert rc == cli.EXIT_CONFIG
    assert "stigmergy-gardener:" in err
    assert "cannot reach the queue database" in err
    assert "make db-up" in err


# ── found running the first real `stigmergy-gardener`: `_connect` used to ensure only some of the
# schemas the checks need, which is invisible over a MATURE database (every fixture here already
# ensures all of them via `support.connect_or_skip()`, which is what every OTHER test in this file
# runs against) and fatal on a fresh one — a bare `UndefinedTable` on the very first real run.
# Simulated here by dropping a schema the old `_connect` used to skip; the benign twin is every
# OTHER test in this file, which already proves the mature-database case works. ─────────────────
def test_connect_ensures_every_schema_a_fresh_database_is_missing(conn, capsys, repo):
    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS review_decisions CASCADE")
    support.write_registry(repo, {})
    support.write_page(repo, "wiki", "notes/orphan.md",
                       frontmatter={"type": "note", "title": "Orphan", "entity": [],
                                   "status": "developing", "updated": _days_ago(1)})
    support.rebuild_index(conn, repo)

    rc = cli.main(["--repo", repo])

    captured = capsys.readouterr()
    assert rc == 0
    assert captured.err == ""
    assert "orphan-page" in captured.out
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('review_decisions')")
        assert cur.fetchone()[0] == "review_decisions"


# ── the twin both CLIs declare in prose (`gardener/cli.py::_connect`: "Same shape and order as
# `digest/cli.py`", restated in both `index.md`s). Read off the AST rather than by running either
# command, because the failure this guards is a schema added to ONE `_connect`: invisible against
# every mature database, and an `UndefinedTable` the first time the sibling meets a fresh one. ────
def _ensure_calls(module) -> set[str]:
    """Every `ensure_*` function `module._connect` calls, by bare name — the two files import them
    differently, so `capture_schema.ensure_x` and a plain `ensure_x` must count as one fact."""
    tree = ast.parse(pathlib.Path(module.__file__).read_text(encoding="utf-8"))
    connect = next(node for node in ast.walk(tree)
                   if isinstance(node, ast.FunctionDef) and node.name == "_connect")
    called = {call.func.attr if isinstance(call.func, ast.Attribute)
              else getattr(call.func, "id", "")
              for call in ast.walk(connect) if isinstance(call, ast.Call)}
    return {name for name in called if name.startswith("ensure_")}


def test_the_two_cli_connect_twins_ensure_the_same_schemas():
    ensured = _ensure_calls(cli)
    # Pinned before it is compared: two empty sets are equal, so an extractor that silently stopped
    # seeing calls would otherwise read as agreement.
    assert ensured == {"ensure_capture_schema", "ensure_decisions_schema", "ensure_gardener_schema"}
    assert ensured == _ensure_calls(digest_cli)


def test_connect_interrupted_exits_130_with_the_generic_message(capsys, monkeypatch):
    def boom(_args):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "_connect", boom)

    rc = cli.main([])

    captured = capsys.readouterr()
    assert rc == cli.EXIT_INTERRUPTED
    assert captured.out == ""
    assert "stigmergy-gardener: interrupted while connecting to the queue database" in captured.err
    assert "Traceback" not in captured.err


def test_generic_interrupt_during_the_run_exits_130_stderr_only(conn, capsys, monkeypatch):
    def boom(_conn, _args):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "_run", boom)

    rc = cli.main([])

    captured = capsys.readouterr()
    assert rc == cli.EXIT_INTERRUPTED
    assert captured.out == ""
    assert "stigmergy-gardener: interrupted — nothing was written; re-run when ready." in captured.err
    assert "Traceback" not in captured.err


# ── --repo validation ─────────────────────────────────────────────────────────────────────────
def test_bad_repo_prints_a_clean_message_and_exits_config(conn, capsys, repo):
    missing = os.path.join(repo, "does-not-exist")
    rc = cli.main(["--repo", missing])
    err = capsys.readouterr().err
    assert rc == cli.EXIT_CONFIG
    assert "stigmergy-gardener:" in err
    assert missing in err


# ── a malformed threshold env var (StartupError) ─────────────────────────────────────────────
def test_bad_threshold_env_var_prints_a_clean_message_and_exits_config(
        conn, capsys, repo, monkeypatch):
    support.write_registry(repo, {})
    monkeypatch.setenv("STIGMERGY_GARDENER_AGING_SEED_DAYS", "not-a-number")

    rc = cli.main(["--repo", repo])

    err = capsys.readouterr().err
    assert rc == cli.EXIT_CONFIG
    assert "STIGMERGY_GARDENER_AGING_SEED_DAYS" in err


# ── the happy path: report, --json, exit 0 ──────────────────────────────────────────────────────
def test_happy_path_prints_the_severity_grouped_report_and_exits_zero(conn, capsys, repo):
    support.write_registry(repo, {})
    support.write_page(repo, "wiki", "notes/orphan.md",
                       frontmatter={"type": "note", "title": "Orphan", "entity": [],
                                   "status": "developing", "updated": _days_ago(1)})
    support.rebuild_index(conn, repo)

    rc = cli.main(["--repo", repo])

    out = capsys.readouterr().out
    assert rc == 0
    assert out.startswith("# Gardener report — run #")
    assert "checked 1 pages, 0 entities — 10 deterministic checks" in out
    assert "1 finding(s): 0 sla, 0 warn, 1 info" in out
    assert "## SLA (0)" in out
    assert "orphan-page" in out
    assert "wiki/notes/orphan.md" in out


def test_clean_run_prints_the_honest_no_findings_line_and_exits_zero(conn, capsys, repo):
    support.write_registry(repo, {})
    # A written body — an entity page that says nothing about itself is itself a finding now
    # (`model-empty-entity-body`), so a clean run needs a page with something on it.
    support.write_page(repo, "wiki", "entities/acme-corp.md",
                       frontmatter={"type": "entity", "title": "Acme Corp",
                                   "entity": ["acme-corp"], "status": "developing",
                                   "updated": _days_ago(1)},
                       body=support.written_entity_body("Acme Corp"))
    support.rebuild_index(conn, repo)

    rc = cli.main(["--repo", repo])

    out = capsys.readouterr().out
    assert rc == 0
    assert "no findings — every check came back clean this run" in out


def test_the_report_says_how_many_entity_pages_the_second_model_pass_judged(conn, capsys, repo):
    """End of the wire an operator reads: the corpus line names the body sweep and its count, so a
    pass that judged nothing (a corpus with no entity pages, a ceiling of zero pages left) is
    visible as such instead of hiding inside the sampled numbers."""
    support.write_registry(repo, {})
    support.write_page(repo, "wiki", "entities/acme-corp.md",
                       frontmatter={"type": "entity", "title": "Acme Corp",
                                   "entity": ["acme-corp"], "status": "developing",
                                   "updated": _days_ago(1)},
                       body=support.written_entity_body("Acme Corp"))
    support.rebuild_index(conn, repo)

    rc = cli.main(["--repo", repo])

    out = capsys.readouterr().out
    assert rc == 0
    assert "and a body sweep over 1 entity page(s)" in out


def test_json_flag_emits_machine_readable_findings(conn, capsys, repo):
    support.write_registry(repo, {})
    support.write_page(repo, "wiki", "notes/orphan.md",
                       frontmatter={"type": "note", "title": "Orphan", "entity": [],
                                   "status": "developing", "updated": _days_ago(1)})
    support.rebuild_index(conn, repo)

    rc = cli.main(["--repo", repo, "--json"])

    out = capsys.readouterr().out
    assert rc == 0
    payload = json.loads(out)
    assert isinstance(payload, list)
    assert len(payload) == 1
    assert payload[0]["check"] == "orphan-page"
    assert payload[0]["subject"] == "wiki/notes/orphan.md"
    assert payload[0]["id"] is not None


# ── end to end: both sources persist and the REPORT renders both, labeled ───────────────────────
def test_a_model_sourced_finding_is_labeled_inline_in_the_human_readable_report(conn, capsys, repo):
    """Both labels are printed inline, in the human-readable report, not only in `--json`: a label
    that exists only in the machine format is not rendered at all in the surface a human actually
    reads. Proven through the real CLI, not only `report.py`'s own synthetic-finding unit test
    (`test_source_tag_deterministic_vs_model`)."""
    support.write_registry(repo, {})
    p = support.write_page(repo, "wiki", "notes/changed.md",
                           frontmatter={"type": "note", "title": "t", "entity": [],
                                       "status": "developing", "updated": _days_ago(1)})
    support.rebuild_index(conn, repo)
    support.seed_filed_capture(conn, result_ref=f"{p}@sha0")

    rc = cli.main(["--repo", repo])

    out = capsys.readouterr().out
    assert rc == 0
    assert "[deterministic]" in out
    from stigmergy.gardener.settings import DEFAULT_GARDENER_MODEL
    assert f"[model: {DEFAULT_GARDENER_MODEL}]" in out


def test_json_flag_carries_model_id_for_a_model_sourced_finding(conn, capsys, repo):
    support.write_registry(repo, {})
    p = support.write_page(repo, "wiki", "notes/changed.md",
                           frontmatter={"type": "note", "title": "t", "entity": [],
                                       "status": "developing", "updated": _days_ago(1)})
    support.rebuild_index(conn, repo)
    support.seed_filed_capture(conn, result_ref=f"{p}@sha0")

    rc = cli.main(["--repo", repo, "--json"])

    out = capsys.readouterr().out
    assert rc == 0
    payload = json.loads(out)
    model_rows = [row for row in payload if row["source"] == "model"]
    assert len(model_rows) == 1
    from stigmergy.gardener.settings import DEFAULT_GARDENER_MODEL
    assert model_rows[0]["model_id"] == DEFAULT_GARDENER_MODEL



# ── a malformed channels file, end to end through the CLI ───────────────────────────────────────
def test_malformed_channels_file_does_not_fail_a_run_with_nothing_to_post(conn, capsys, repo):
    """`channels.channel_audiences` used to be resolved unconditionally, before the SLA
    short-circuit — an info/warn-only run (which never touches Slack at all) used to fail
    outright on a malformed `ops/slack-channels.json`, and lose its own report doing so (the
    `IdentityError` propagated past this module's own `print(report...)` calls in `_run`)."""
    support.write_registry(repo, {})
    support.write_page(repo, "wiki", "notes/orphan.md",
                       frontmatter={"type": "note", "title": "Orphan", "entity": [],
                                   "status": "developing", "updated": _days_ago(1)})
    support.rebuild_index(conn, repo)
    support.write_malformed_channels_file(repo)   # default --channels path: <repo>/ops/slack-channels.json

    rc = cli.main(["--repo", repo])

    captured = capsys.readouterr()
    assert rc == 0
    assert captured.err == ""
    assert "orphan-page" in captured.out



# ── a sweep outage: the deterministic findings survive ──────────────────────────────────────────
def test_sweep_outage_still_prints_deterministic_findings_then_exits_error(
        conn, capsys, repo, monkeypatch):
    """The run overall exits nonzero, but the deterministic checks are complete, correct and
    already persisted — the report shows them in full, labeled as sweep-incomplete, never
    withheld because a DIFFERENT, independent pass failed."""
    class _FlakyJudge:
        async def run(self, prompt, *, deps=None, usage_limits=None):
            raise AgentRunError("simulated model outage")

    monkeypatch.setattr(sweep, "build_judge", lambda model_name=None: _FlakyJudge())
    support.write_registry(repo, {})
    p = support.write_page(repo, "wiki", "notes/orphan.md",
                           frontmatter={"type": "note", "title": "Orphan", "entity": [],
                                       "status": "developing", "updated": _days_ago(1)})
    support.rebuild_index(conn, repo)
    support.seed_filed_capture(conn, result_ref=f"{p}@sha0")   # a `changed` page, so the sweep
                                                                # is actually attempted this run

    rc = cli.main(["--repo", repo])

    captured = capsys.readouterr()
    assert rc == cli.EXIT_ERROR
    assert "the model sweep did NOT complete this run (see below)" in captured.out
    assert "deterministic checks only" in captured.out
    assert "orphan-page" in captured.out
    assert "wiki/notes/orphan.md" in captured.out
    assert "stigmergy-gardener: the model sweep failed (AgentRunError)" in captured.err
    assert "already saved" in captured.err
    assert "job_runs" in captured.err


def test_an_empty_body_pass_failure_is_reported_on_stderr_and_exits_error(conn, capsys, repo,
                                                                            monkeypatch):
    """The SECOND model pass has its own failure message and its own exit path, and nothing
    exercised either. The contract is the one the editorial sweep already keeps: the findings that
    DID complete are printed and already saved, the failure is named on stderr with the pass that
    had it, and the process exits non-zero so a scheduler notices."""
    class _FlakyJudge:
        async def run(self, prompt, *, deps=None, usage_limits=None):
            raise AgentRunError("simulated model outage")

    monkeypatch.setattr(sweep, "build_empty_body_judge", lambda model_name=None: _FlakyJudge())
    support.write_registry(repo, {})
    support.write_page(repo, "wiki", "entities/acme-corp.md",
                       frontmatter={"type": "entity", "title": "Acme Corp",
                                   "entity": ["acme-corp"], "status": "developing",
                                   "updated": _days_ago(1)},
                       body=support.empty_entity_body("Acme Corp"))
    support.rebuild_index(conn, repo)

    rc = cli.main(["--repo", repo])

    captured = capsys.readouterr()
    assert rc == cli.EXIT_ERROR
    assert "stigmergy-gardener: the entity-body sweep failed (AgentRunError)" in captured.err
    assert "already saved" in captured.err
    assert "job_runs" in captured.err
    assert captured.out.startswith("# Gardener report — run #"), (
        "the deterministic report is printed whatever the second model pass did")


def test_sweep_success_with_no_changed_pages_prints_a_normal_report_and_exits_zero(
        conn, capsys, repo, monkeypatch):
    """No `capture_queue` row at all -> `changed` is empty, but `sampled` is NOT (the corpus has
    one page) -> `run_sweep` DOES call the judge (its short-circuit only skips a batch that is
    EMPTY OVERALL, `test_sweep.py::test_an_empty_batch_never_calls_the_judge_at_all` proves that
    narrower case) -> the SHIPPED offline double still contributes zero findings, by its own
    documented restraint (`FakeGardenerSweep` never fires on a sampled-only batch) -> a normal,
    successful run."""
    support.write_registry(repo, {})
    support.write_page(repo, "wiki", "notes/orphan.md",
                       frontmatter={"type": "note", "title": "Orphan", "entity": [],
                                   "status": "developing", "updated": _days_ago(1)})
    support.rebuild_index(conn, repo)

    rc = cli.main(["--repo", repo])

    captured = capsys.readouterr()
    assert rc == 0
    assert "did NOT complete" not in captured.out
    assert captured.err == ""
