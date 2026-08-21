"""`evals/eval_history.py` — the durable eval-score series.

`evals/` carries no `__init__.py` (it is a script directory, not an importable package in the
ordinary sense) but resolves as a namespace package under this suite's own
`pythonpath = ["src", "."]` (pyproject.toml). `run_qa.py`/`run_retrieval.py` themselves stay
untested here — the harness has no unit tests of its own, it IS the test, at system level. This
module is different: a small, pure library the two runners call into, exactly the seam worth
testing on its own rather than only by proxy through a real (keyed, slow) eval run.
"""
import json
import os
from datetime import UTC, datetime
from pathlib import Path

from evals import eval_history


def test_append_run_writes_one_json_line_with_ts_git_sha_suite_and_metrics(tmp_path):
    path = tmp_path / "history.ndjson"
    eval_history.append_run(suite="retrieval", git_sha="abc123", path=path,
                            now=datetime(2026, 8, 24, 5, 37, tzinfo=UTC),
                            metrics={"recall_at_5": 0.82, "k": 5})

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row == {"ts": "2026-08-24T05:37:00+00:00", "git_sha": "abc123", "suite": "retrieval",
                  "recall_at_5": 0.82, "k": 5}


def test_append_run_appends_rather_than_overwrites(tmp_path):
    path = tmp_path / "history.ndjson"
    eval_history.append_run(suite="qa", git_sha="sha1", path=path, metrics={"honesty": 0.9})
    eval_history.append_run(suite="qa", git_sha="sha2", path=path, metrics={"honesty": 0.95})

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["git_sha"] == "sha1"
    assert json.loads(lines[1])["git_sha"] == "sha2"


def test_append_run_survives_an_unwritable_target_without_raising(tmp_path, capsys):
    """The module docstring's own promise: an append failure warns and never fails the eval run."""
    missing_parent = tmp_path / "does-not-exist" / "history.ndjson"
    eval_history.append_run(suite="qa", git_sha="sha1", path=missing_parent, metrics={"honesty": 0.9})

    assert not missing_parent.exists()
    assert "warning" in capsys.readouterr().err


def test_read_history_returns_every_line_oldest_first(tmp_path):
    path = tmp_path / "history.ndjson"
    eval_history.append_run(suite="qa", git_sha="sha1", path=path, metrics={"honesty": 0.9})
    eval_history.append_run(suite="retrieval", git_sha="sha1", path=path,
                            metrics={"recall_at_5": 0.8})

    rows = eval_history.read_history(path)
    assert [r["suite"] for r in rows] == ["qa", "retrieval"]


def test_read_history_empty_on_a_missing_file():
    assert eval_history.read_history(Path("/nonexistent/path/history.ndjson")) == []


def test_read_history_skips_a_malformed_line_rather_than_raising(tmp_path):
    path = tmp_path / "history.ndjson"
    path.write_text('{"suite": "qa", "ts": "x"}\nnot json at all\n{"suite": "retrieval", "ts": "y"}\n',
                    encoding="utf-8")
    rows = eval_history.read_history(path)
    assert [r["suite"] for r in rows] == ["qa", "retrieval"]


def test_resolve_git_sha_returns_a_real_sha_inside_this_repo():
    sha = eval_history.resolve_git_sha(Path(__file__).resolve().parents[2])
    # `-dirty` when this checkout has uncommitted work, which is the normal state while a change
    # is in progress — the test must not depend on the working tree being clean.
    core = sha.removesuffix("-dirty")
    assert len(core) == 40
    assert all(c in "0123456789abcdef" for c in core)


def test_a_dirty_working_tree_is_marked_so_a_row_cannot_claim_a_commit_it_did_not_measure(tmp_path):
    """Two real rows once named a commit whose code they had not measured, because the eval ran
    against an uncommitted change. The series' whole promise is that an entry says what it was
    measured on."""
    import subprocess as sp
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t", "PATH": os.environ.get("PATH", "")}
    sp.run(["git", "init", "-q"], cwd=tmp_path, check=True, env=env)
    (tmp_path / "a.txt").write_text("one", encoding="utf-8")
    sp.run(["git", "add", "-A"], cwd=tmp_path, check=True, env=env)
    sp.run(["git", "commit", "-qm", "one"], cwd=tmp_path, check=True, env=env)

    clean = eval_history.resolve_git_sha(tmp_path)
    assert len(clean) == 40 and not clean.endswith("-dirty")

    (tmp_path / "a.txt").write_text("two", encoding="utf-8")
    dirty = eval_history.resolve_git_sha(tmp_path)
    assert dirty == f"{clean}-dirty"


def test_the_series_file_itself_never_counts_as_dirt(tmp_path):
    """`append_run` writes the tracked series file, so instrument A's own row would otherwise make
    instrument B's row `-dirty` on a clean checkout — which is what happened to the `126286a` qa
    row. "Dirty" must mean the measured CODE differs, not that the bookkeeping ran."""
    import subprocess as sp
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t", "PATH": os.environ.get("PATH", "")}
    sp.run(["git", "init", "-q"], cwd=tmp_path, check=True, env=env)
    series = tmp_path / "evals" / "history.ndjson"
    series.parent.mkdir(parents=True)
    series.write_text('{"suite": "qa"}\n', encoding="utf-8")
    (tmp_path / "code.py").write_text("x = 1\n", encoding="utf-8")
    sp.run(["git", "add", "-A"], cwd=tmp_path, check=True, env=env)
    sp.run(["git", "commit", "-qm", "one"], cwd=tmp_path, check=True, env=env)
    clean = eval_history.resolve_git_sha(tmp_path)

    # the instrument appends its own row: still clean, because the code did not move
    with series.open("a", encoding="utf-8") as f:
        f.write('{"suite": "retrieval"}\n')
    assert eval_history.resolve_git_sha(tmp_path) == clean

    # any OTHER tracked file moving is real dirt
    (tmp_path / "code.py").write_text("x = 2\n", encoding="utf-8")
    assert eval_history.resolve_git_sha(tmp_path) == f"{clean}-dirty"


def test_an_unprobeable_tree_reads_as_dirty_never_as_clean(tmp_path, monkeypatch):
    """A `git status` that fails (timeout under contention, index lock) must not be spelled the
    same as a verified-clean tree — that is the false precision the suffix exists to prevent.
    "Assume dirty when unsure" says it with one concept instead of inventing a third spelling."""
    import subprocess as sp
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t", "PATH": os.environ.get("PATH", "")}
    sp.run(["git", "init", "-q"], cwd=tmp_path, check=True, env=env)
    (tmp_path / "a.txt").write_text("one", encoding="utf-8")
    sp.run(["git", "add", "-A"], cwd=tmp_path, check=True, env=env)
    sp.run(["git", "commit", "-qm", "one"], cwd=tmp_path, check=True, env=env)

    real_run = eval_history.subprocess.run

    def fail_on_status(cmd, *a, **kw):
        if "status" in cmd:
            raise sp.TimeoutExpired(cmd, 5)
        return real_run(cmd, *a, **kw)

    monkeypatch.setattr(eval_history.subprocess, "run", fail_on_status)
    sha = eval_history.resolve_git_sha(tmp_path)
    assert sha.endswith("-dirty") and len(sha.removesuffix("-dirty")) == 40


def test_resolve_git_sha_is_best_effort_outside_any_repo(tmp_path):
    assert eval_history.resolve_git_sha(tmp_path) == ""


# ── the committed series itself, not just the function that appends to it ──────────────────────
# The gap this closes: `evals/history.ndjson` was committed carrying a `<<<<<<< Updated upstream`
# conflict marker from a stash pop, and 3,555 tests passed over it — because every test above
# drives `append_run` against a tmp_path and NOTHING read the real file. A series whose whole
# purpose is that two entries are comparable is worth one check that the series still parses.
def test_the_committed_series_is_valid_ndjson_in_chronological_order():
    """Every line of the real `evals/history.ndjson` is one JSON object, and timestamps ascend.

    Append-only means the file is only ever touched by a runner adding a line at the end and an
    operator committing it — so the two ways it breaks are a hand edit and a merge. Both show up
    here as a line that will not parse, or a timestamp that goes backwards.
    """
    raw = eval_history.HISTORY_PATH.read_text(encoding="utf-8").splitlines()
    assert raw, "the score series is empty — it is committed, not generated"

    rows = []
    for n, line in enumerate(raw, start=1):
        assert line.strip(), f"line {n} is blank; this file is one JSON object per line"
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError as ex:
            raise AssertionError(
                f"{eval_history.HISTORY_RELPATH} line {n} is not JSON ({ex.msg}): {line[:80]!r}"
            ) from ex

    stamps = [r["ts"] for r in rows]
    assert stamps == sorted(stamps), (
        "entries are out of order, so the file was edited rather than appended to:\n  "
        + "\n  ".join(f"line {n}: {a} then {b}"
                      for n, (a, b) in enumerate(zip(stamps, stamps[1:], strict=True), start=1)
                      if a > b))


def test_the_reader_still_reads_the_rows_written_before_ADR_041_retired_two_facets():
    """**The append-only file's own compatibility test, over the real file.**

    `history.ndjson` is years long and never rewritten, so sixteen `suite: "filing"` rows carry
    `park_question` and `reuse` — facets `run_filing` no longer scores, from runs whose statuses say
    `needs_input`. Nothing rewrites them: a recorded row says what was measured, and re-grading it
    against today's yardstick would destroy the only thing the series is for.

    So the READER has to stay indifferent to which facets a row names, and that is what this pins.
    `read_history` parses lines and never a schema, which is the property — a reader that grew a
    required-facet check would make every filing score recorded before the redesign unreadable, and
    the failure would look like an empty series rather than a broken parser.
    """
    from evals import run_filing

    rows = eval_history.read_history()
    filing = [r for r in rows if r.get("suite") == "filing" and r.get("facets")]
    assert filing, "the series carries no filing row at all — this check lost its subject"

    retired = {"park_question", "reuse"}
    legacy = [r for r in filing if retired & set(r["facets"])]
    assert legacy, ("no recorded row names a retired facet any more, so nothing here is proving "
                    "the reader tolerates one — either the file was rewritten, which it must never "
                    "be, or this test has outlived the rows it was written for")
    assert not retired & set(run_filing.FACETS), (
        "a retired facet is scored again; then these rows are not legacy and this test is wrong")
    for row in legacy:
        assert set(row["facets"]) == set(row["counts"]), row["ts"]
        assert row.get("statuses"), row["ts"]


def test_every_entry_says_what_it_was_measured_on():
    """`corpus` is the entry's answer to "is this number comparable to that one?".

    Nine early entries were measured against a private knowledge repo rather than the frozen
    corpus, and their `corpus` says so in words instead of naming a path nobody else can check
    out. That is the honest shape, and it must not silently become a path again.
    """
    rows = [json.loads(line) for line in
            eval_history.HISTORY_PATH.read_text(encoding="utf-8").splitlines()]
    missing = [r["ts"] for r in rows if not r.get("corpus")]
    assert not missing, f"entries with no corpus recorded: {missing}"
