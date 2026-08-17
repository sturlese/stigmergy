"""`stigmergy-repair`: the three commands, and the one thing it deliberately cannot do.

The preview is the part worth pinning hardest. A steward reads it to decide whether to authorize a
change, so it has to render what the APPLIER will perform — and it renders from the stored ops
alone, with no git and no clone, which is exactly how it could drift.
"""
import json

import pytest

from stigmergy.librarian import page as page_policy
from stigmergy.repair import cli, schema, store
from tests.repair import support


def _run(conn, argv, monkeypatch):
    """The CLI's command functions take `(conn, args)`; `main` owns the connection. Driving the
    parser and dispatching by hand is what lets these run on the suite's own connection instead of
    opening a second one — the same shape sibling CLI suites use."""
    args = cli.build_parser().parse_args(argv)
    return args.fn(conn, args)


OPS = [{"op": "contradiction", "path": "wiki/notes/Existing Note.md", "link": "Other Page",
        "note": "these two   disagree\nabout the date"}]


def _insert(conn, ops=OPS, key="k"):
    return store.insert_proposal(
        conn, run_id=3, finding_ids=[11], target_paths=schema.target_paths(ops), ops=ops,
        rationale="the pages disagree and nothing says so", content_key=key, model_id="m")


# ── there is no apply, and that is the design ─────────────────────────────────────────────────
def test_the_cli_offers_no_way_to_apply_a_proposal():
    """A terminal knows who is typing and not what they are allowed to approve. An `apply` here
    would be an authorization decision made by whoever has shell access, which is precisely the
    decision the review lane exists to make properly."""
    parser = cli.build_parser()
    for forbidden in ("apply", "approve", "reject"):
        with pytest.raises(SystemExit):
            parser.parse_args([forbidden])


def test_the_three_commands_it_does_offer_all_parse():
    """The benign twin: a parser that rejected everything would pass the test above."""
    for argv in (["propose"], ["list"], ["show", "1"]):
        assert cli.build_parser().parse_args(argv).fn is not None


# ── show ──────────────────────────────────────────────────────────────────────────────────────
def test_show_renders_what_the_applier_would_actually_add(conn, capsys, monkeypatch):
    """The preview composes the SAME callout `page.with_callout` appends and the same `related:`
    entry `page.with_related_link` adds — from the op, with no git anywhere near it."""
    proposal_id = _insert(conn)
    assert _run(conn, ["show", str(proposal_id)], monkeypatch) == 0

    out = capsys.readouterr().out
    assert f"proposal #{proposal_id}  {schema.STATUS_PENDING}" in out
    assert "+   related: [[Other Page]]" in out
    assert "+   > [!WARNING] Contradiction with [[Other Page]]" in out
    # The note is collapsed onto one line, exactly as the applier collapses it.
    assert "+   > these two disagree about the date" in out


def test_the_preview_is_additive_for_every_op_kind():
    """Every line a preview can emit is a `+`. This vocabulary cannot express a removal, and a
    preview that could show one would be describing a change the applier cannot make."""
    ops = [{"op": kind, "path": "wiki/notes/x.md", "link": "Y", "note": "n"}
           for kind in page_policy.EDIT_KINDS]
    lines = cli.preview({"ops": ops})
    assert lines, "a proposal with ops must preview as something"
    assert all(line.startswith(("+", "---")) for line in lines)


def test_a_backlink_previews_as_the_link_alone_and_no_callout():
    lines = cli.preview({"ops": [{"op": "backlink", "path": "wiki/notes/x.md", "link": "Y",
                                  "note": ""}]})
    assert lines == ["--- wiki/notes/x.md", "+   related: [[Y]]"]


def test_show_reports_an_unknown_id_rather_than_printing_an_empty_proposal(conn, monkeypatch):
    from stigmergy.repair.errors import RepairError
    with pytest.raises(RepairError, match="does not exist"):
        _run(conn, ["show", "424242"], monkeypatch)


def test_show_json_is_the_whole_row(conn, capsys, monkeypatch):
    proposal_id = _insert(conn)
    _run(conn, ["--json", "show", str(proposal_id)], monkeypatch)
    row = json.loads(capsys.readouterr().out)
    assert row["id"] == proposal_id
    assert row["ops"] == OPS


# ── list ──────────────────────────────────────────────────────────────────────────────────────
def test_list_says_so_plainly_when_nothing_waits(conn, capsys, monkeypatch):
    assert _run(conn, ["list"], monkeypatch) == 0
    assert "no proposals waiting on a steward" in capsys.readouterr().out


def test_list_shows_what_waits_and_what_was_recently_decided(conn, capsys, monkeypatch):
    waiting = _insert(conn, key="waiting")
    declined = _insert(conn, key="declined")
    store.mark_decided(conn, declined, status=schema.STATUS_REJECTED,
                       decided_by=support.STEWARD, notes="the mention is deliberate")

    _run(conn, ["list"], monkeypatch)

    out = capsys.readouterr().out
    assert f"#{waiting}" in out
    assert f"#{declined}" in out and schema.STATUS_REJECTED in out
    assert support.STEWARD in out, "a decision without its decider is not a record"


def test_list_json_separates_what_waits_from_what_was_decided(conn, capsys, monkeypatch):
    waiting = _insert(conn, key="waiting")
    declined = _insert(conn, key="declined")
    store.mark_decided(conn, declined, status=schema.STATUS_REJECTED, decided_by="s", notes="no")

    _run(conn, ["--json", "list"], monkeypatch)

    payload = json.loads(capsys.readouterr().out)
    assert [row["id"] for row in payload["pending"]] == [waiting]
    assert [row["id"] for row in payload["recent"]] == [declined]


# ── propose ───────────────────────────────────────────────────────────────────────────────────
def test_propose_refuses_a_repo_that_is_not_a_git_checkout(conn, tmp_path, monkeypatch):
    """A proposal is validated against the pages that are actually COMMITTED. A bare directory of
    markdown would answer every question against a corpus the apply will never see."""
    from stigmergy.repair.errors import RepairError
    monkeypatch.setenv("STIGMERGY_REPO", str(tmp_path / "not-a-checkout"))
    with pytest.raises(RepairError, match="not a git checkout"):
        _run(conn, ["propose"], monkeypatch)


def test_propose_prints_the_counters_it_recorded(conn, repo_env, capsys, monkeypatch):
    monkeypatch.setenv("STIGMERGY_REPO", repo_env.repo)
    run_id = support.seed_gardener_run(conn)
    support.seed_unlinked_mention(conn, run_id)

    assert _run(conn, ["propose"], monkeypatch) == 0

    out = capsys.readouterr().out
    assert "1 proposable finding(s)" in out
    assert "1 proposed" in out
    assert "stigmergy-repair show <id>" in out, (
        "a message naming what to do next has to name a command that exists")


def test_propose_json_carries_the_same_counters_as_the_job_row(conn, repo_env, capsys,
                                                                monkeypatch):
    monkeypatch.setenv("STIGMERGY_REPO", repo_env.repo)
    run_id = support.seed_gardener_run(conn)
    support.seed_unlinked_mention(conn, run_id)

    _run(conn, ["--json", "propose"], monkeypatch)

    payload = json.loads(capsys.readouterr().out)
    with conn.cursor() as cur:
        cur.execute("SELECT stats FROM job_runs WHERE id = %s", (payload["run_id"],))
        stats = cur.fetchone()[0]
    assert {k: payload[k] for k in stats} == stats


def test_the_command_the_propose_output_names_is_one_the_parser_accepts(conn, repo_env, capsys,
                                                                        monkeypatch):
    """A message containing a command is an executable promise: if the output tells an operator to
    run something, the parser has to accept it."""
    monkeypatch.setenv("STIGMERGY_REPO", repo_env.repo)
    run_id = support.seed_gardener_run(conn)
    support.seed_unlinked_mention(conn, run_id)
    _run(conn, ["propose"], monkeypatch)
    capsys.readouterr()

    (row,) = store.pending_proposals(conn)
    args = cli.build_parser().parse_args(["show", str(row["id"])])
    assert args.fn(conn, args) == 0
