"""`stigmergy-repair`: the three commands, and the one thing it deliberately cannot do.

The preview is the part worth pinning hardest. A steward reads it to decide whether to authorize a
change, so it has to render what the APPLIER will perform — and it renders from the stored ops
alone, with no git and no clone, which is exactly how it could drift.
"""
import json

import pytest

from stigmergy.librarian import page as page_policy
from stigmergy.repair import cli, deletion, schema, store
from stigmergy.repair import settings as settings_module
from stigmergy.repair.errors import RepairError
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


def test_the_commands_it_does_offer_all_parse():
    """The benign twin: a parser that rejected everything would pass the test above. `delete` is
    the one verb that CREATES a proposal from a terminal, and it is the same authority level as
    `propose` — it inserts a pending row and applies nothing."""
    for argv in (["propose"], ["list"], ["show", "1"], ["delete", "wiki/notes/X.md", "--why", "w"]):
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


# ── the second kind renders as what it is, not as a mangled edit ──────────────────────────────
BODY_OPS = [{"op": schema.KIND_ENTITY_BODY, "path": "wiki/entities/Meridian Partners.md",
             "body_markdown": "## What / Who\n\nA freight broker.\n", "role": "A freight broker."}]


def test_a_body_draft_previews_as_the_draft_a_steward_would_be_approving():
    """The steward reading the draft IS the check for this kind, so the preview has to SHOW it.
    Rendered from the ops alone, like every other preview — no git, no clone: a preview derived
    some other way could differ from the thing the apply performs."""
    lines = cli.preview({"kind": schema.KIND_ENTITY_BODY, "ops": BODY_OPS})

    assert lines[0] == "--- wiki/entities/Meridian Partners.md"
    assert any("A freight broker." in line for line in lines)
    assert any("role:" in line for line in lines)
    assert all(line.startswith(("+", "-", " ")) for line in lines)


def test_a_body_preview_never_renders_the_additive_shape():
    """The defect this exists to prevent: `preview` emitted `related: [[…]]` for EVERY op, so a
    body draft previewed as a link to nothing — a steward approving what the preview showed would
    be approving a change that does not exist."""
    lines = cli.preview({"kind": schema.KIND_ENTITY_BODY, "ops": BODY_OPS})
    assert not any("related: [[" in line for line in lines)


def test_the_list_column_fits_every_kind_the_code_can_write():
    """A column width narrower than a value is a value that shifts every row after it. Derived
    from `schema.KINDS`, so a third kind widens the column instead of breaking the alignment."""
    assert max(len(kind) for kind in schema.KINDS) == cli.KIND_WIDTH


def test_the_list_prints_a_body_proposal_on_one_aligned_line(conn, capsys, monkeypatch):
    proposal_id = store.insert_proposal(
        conn, run_id=1, finding_ids=[], target_paths=["wiki/entities/Meridian Partners.md"],
        ops=BODY_OPS, rationale="the page is still its template", content_key="body-key",
        kind=schema.KIND_ENTITY_BODY)

    assert _run(conn, ["list"], monkeypatch) == 0

    out = capsys.readouterr().out
    assert f"#{proposal_id}" in out
    assert schema.KIND_ENTITY_BODY in out
    assert "1 op(s) on wiki/entities/Meridian Partners.md" in out


# ── the third kind, and the ONE verb that creates a proposal from a terminal ───────────────────
DELETE_ARGS = ["--why", "the memo was superseded and nothing needs it any more"]


def _delete(conn, repo, paths, monkeypatch, extra=DELETE_ARGS):
    return _run(conn, ["--repo", repo, "delete", *paths, *extra], monkeypatch)


def test_delete_stores_one_pending_proposal_carrying_the_whole_sweep(conn, repo_env, capsys,
                                                                     monkeypatch):
    """The CLI is the only door that CREATES a deletion, and it stores the whole blast radius:
    `target_paths` has to carry every page the sweep touches, or the review lane's per-path steward
    guard would authorize a rewrite of pages nobody's steward saw."""
    pages = support.seed_deletion_corpus(repo_env)

    assert _delete(conn, repo_env.repo, [pages["doomed"]], monkeypatch) == 0
    capsys.readouterr()

    (row,) = store.pending_proposals(conn)
    assert row["kind"] == schema.KIND_DELETE
    assert row["target_paths"] == sorted([pages["doomed"], pages["keeps_a_link"],
                                          pages["in_prose"], pages["only_related"]])
    assert deletion.deleted_paths(row["ops"]) == [pages["doomed"]]
    assert row["rationale"] == "the memo was superseded and nothing needs it any more"


def test_the_row_a_human_typed_records_that_no_model_proposed_it(conn, repo_env, capsys,
                                                                 monkeypatch):
    """An empty `model_id` is a statement, not a gap: `delete` is the only kind no model can
    propose, and the column is where that stays true after the terminal session is gone."""
    pages = support.seed_deletion_corpus(repo_env)
    _delete(conn, repo_env.repo, [pages["doomed"]], monkeypatch)
    capsys.readouterr()

    assert store.pending_proposals(conn)[0]["model_id"] == ""


def test_delete_prints_the_plan_before_anybody_decides_it(conn, repo_env, capsys, monkeypatch):
    """A person typing this has to be able to read what they just proposed — the pages that go and
    the pages that get rewritten — without going and looking it up somewhere else."""
    pages = support.seed_deletion_corpus(repo_env)

    _delete(conn, repo_env.repo, [pages["doomed"]], monkeypatch)

    out = capsys.readouterr().out
    assert pages["doomed"] in out
    assert pages["in_prose"] in out
    assert "3" in out, "the number of pages that would be rewritten"
    assert "nothing has changed" in out, "a proposal is not an apply, and the output says so"


def test_delete_needs_a_reason_and_stores_nothing_without_one(conn, repo_env, monkeypatch):
    pages = support.seed_deletion_corpus(repo_env)

    with pytest.raises(RepairError, match="reason"):
        _delete(conn, repo_env.repo, [pages["doomed"]], monkeypatch, extra=["--why", "   "])

    assert store.pending_proposals(conn) == []


def test_delete_refuses_an_entity_page_and_stores_nothing(conn, repo_env, monkeypatch):
    """The refusal a person is most likely to meet, and the one that has to explain itself: an
    identity is retired through governance, not deleted."""
    support.seed_deletion_corpus(repo_env)

    with pytest.raises(RepairError, match="identity"):
        _delete(conn, repo_env.repo, ["wiki/entities/Acme Corp.md"], monkeypatch)

    assert store.pending_proposals(conn) == []


def test_delete_refuses_a_plan_over_its_ceiling_and_stores_nothing(conn, repo_env, monkeypatch):
    """One approval is one decision a person can actually have read. The bound is on the STORED
    PLAN's bytes, because that is what a sweep costs a steward and a database row alike."""
    pages = support.seed_deletion_corpus(repo_env)
    monkeypatch.setenv(settings_module.MAX_PLAN_BYTES_ENV, "10")

    with pytest.raises(RepairError, match="ceiling"):
        _delete(conn, repo_env.repo, [pages["doomed"]], monkeypatch)

    assert store.pending_proposals(conn) == []


def test_delete_refuses_when_the_same_deletion_is_already_waiting(conn, repo_env, capsys,
                                                                  monkeypatch):
    """The UNIQUE index would refuse the second insert as a database error. A person typing a
    command gets a sentence instead, and it says where the first one is."""
    pages = support.seed_deletion_corpus(repo_env)
    _delete(conn, repo_env.repo, [pages["doomed"]], monkeypatch)
    capsys.readouterr()

    with pytest.raises(RepairError, match="already waiting"):
        _delete(conn, repo_env.repo, [pages["doomed"]], monkeypatch)

    assert len(store.pending_proposals(conn)) == 1


DELETE_OPS = [{"op": deletion.OP_DELETE, "path": "wiki/notes/Doomed.md"},
              {"op": deletion.OP_SCRUB, "path": "wiki/notes/Cites It.md",
               "expected_before_hash": "abc", "planned_after": "---\ntype: note\n---\n\n# X\n"}]


def test_a_delete_previews_as_the_pages_that_go_and_the_pages_that_change():
    """A steward reading this is authorizing a removal, so the preview says which pages STOP
    EXISTING first and separately — the one consequence no other kind has."""
    lines = cli.preview({"kind": schema.KIND_DELETE, "ops": DELETE_OPS})

    assert lines[0] == "--- wiki/notes/Doomed.md"
    assert "removed" in lines[1]
    assert lines[2] == "--- wiki/notes/Cites It.md"
    assert "link" in lines[3]


def test_a_delete_preview_never_renders_the_additive_shape_or_the_planned_bytes():
    """Two defects at once. The additive shape would show a link that does not exist; the planned
    bytes would put a whole page into a scan a steward reads to decide which row to open."""
    lines = cli.preview({"kind": schema.KIND_DELETE, "ops": DELETE_OPS})

    assert not any("related: [[" in line for line in lines)
    assert not any("# X" in line for line in lines)
