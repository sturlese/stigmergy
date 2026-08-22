"""ADR 044 D3: ONE writer for the corpus, and it is the worker. A `delete` row is a person's own
removal, authorized at the door and PERFORMED here — planned, swept by a model, judged by the nine
gates, committed and pushed through the same lease-fenced seam every filing uses.

Against a REAL bare remote with the REAL gates and the REAL gitleaks pass, because that is the
whole claim: nothing was skipped by moving the act off the serving process. The model is the only
double (`CLEAN_LLM=fake`, the package's own writer), exactly as `tests/repair/` runs it.

What this module proves, in the order the flow meets it:

- the benign twin first — a queued removal is claimed and performed: the page is gone on the bare
  remote, every page that referred to it is rewritten, it took ONE commit, and the row lands
  `filed` carrying the paths that went and the per-page diffs of what a model wrote in the
  submitter's name (ADR 043 D5 — nobody read that prose before it landed, so the row IS the
  reading);
- the commit answers "who authorized this" with two different names: the AUTHOR is the librarian
  App, the `Approved-by:` trailer is the PERSON, and that distinction is the whole of ADR 043 D2 in
  the queued shape;
- a page that is not there, and a page the lane may not touch, land `rejected` with
  `reason_code == "unremovable"` and NOTHING pushed — a refusal the person who asked can act on,
  never a `failed` row that reads like an outage;
- a reason carrying a credential is refused before any tree is read at all: `why` becomes a commit
  message, where no gate looks;
- a sweep the writer cannot finish lands nothing — no deterministic fallback (ADR 043 D1), and no
  half-swept corpus.
"""
import pytest

from stigmergy.capture import queue, schema
from stigmergy.librarian import githubapp, processing, worker
from stigmergy.repair import deletion
from tests import adversarial_payloads as payloads
from tests.librarian import support
from tests.repair import support as repair_support

# The person whose judgment the row carries. A removal is the one write in this system a human
# decides, so every assertion about the commit is an assertion about this name.
PERSON = "steward@example.com"
WHY = "the memo was superseded and nothing needs it any more"

# What `githubapp.identity({})` answers with no App configured — the AUTHOR of every commit the
# worker pushes, and deliberately not a person. Read from the module rather than spelled, the same
# way `tests/repair/test_apply_pg.py` reads it.
APP_AUTHOR = githubapp.identity({})


@pytest.fixture(autouse=True)
def clean_llm(monkeypatch):
    """The suite is keyless by construction: the sweep builds a model-backed writer, and a machine
    with `CLEAN_LLM=openai` exported would otherwise turn it into one that spends money."""
    monkeypatch.setenv("CLEAN_LLM", "fake")


@pytest.fixture()
def rig(tmp_path, require_gitleaks):
    """`(env, deps, corpus)`: a real bare remote + clone carrying the repair-proposer skill (the
    sweep writer is briefed from the base commit, so it has to be committed), the deletion corpus
    every test here removes out of, and `Deps` wired to that pair."""
    env = repair_support.build_repo(tmp_path)
    corpus = repair_support.seed_deletion_corpus(env)
    settings = support.build_settings(env, worktree_root=str(tmp_path / "worktrees"))
    return env, support.build_deps(env, settings), corpus


def _queue_delete(conn, deps, paths, *, why: str = WHY, submitted_by: str = PERSON) -> dict:
    """One `delete` row, enqueued through the REAL queue primitive with the hints the door writes
    — the paths one per line, and the door that decided (`capture.schema.delete_paths` is the one
    parser, on this side too)."""
    return queue.submit(conn, deps.evidence, kind=schema.DELETE, material=why,
                        hints={"delete_paths": "\n".join(paths), "delete_source": "mcp"},
                        submitted_by=submitted_by)


def _process(conn, deps, item):
    claimed, result = worker.process_next(conn, deps)
    assert claimed["id"] == item["id"], "the worker claimed a different row than the one queued"
    return result


def _row(conn, submission_id: int) -> tuple:
    with conn.cursor() as cur:
        cur.execute("SELECT status, report FROM capture_queue WHERE id = %s", (submission_id,))
        return cur.fetchone()


# ── the benign twin: a queued removal is performed, whole, in one commit ───────────────────────
def test_a_queued_removal_is_claimed_and_performed_as_one_commit(rig, clean_queue):
    """The whole flow in one assertion set. The page is gone from the REMOTE (never the checkout:
    the worker writes in a detached worktree and pushes, so the checkout is the corpus BEFORE the
    removal), the three pages that referred to it no longer do, and it cost exactly one commit."""
    env, deps, corpus = rig
    before_head = repair_support.remote_head(env.bare)
    before_commits = repair_support.commit_count(env.bare)
    before_prose = repair_support.remote_page(env.bare, corpus["in_prose"])

    item = _queue_delete(clean_queue, deps, [corpus["doomed"]])
    result = _process(clean_queue, deps, item)

    assert result.status == schema.FILED, result.report.get("summary")
    assert corpus["doomed"] not in repair_support.remote_paths(env.bare)
    assert repair_support.commit_count(env.bare) == before_commits + 1
    assert repair_support.remote_head(env.bare) != before_head
    stems = {repair_support.DOOMED_STEM}
    for path in (corpus["keeps_a_link"], corpus["in_prose"], corpus["only_related"]):
        assert not deletion.references(repair_support.remote_page(env.bare, path), stems), (
            f"{path} still refers to the page that is gone")
    landed_prose = repair_support.remote_page(env.bare, corpus["in_prose"])
    assert f"as {repair_support.DOOMED_STEM} records" in landed_prose, (
        "the sentence survives the page it cited — reconciled, not shredded")
    assert "[[Existing Note]]" in repair_support.remote_page(env.bare, corpus["keeps_a_link"]), (
        "a sweep reconciles one reference, not the list it was in")
    assert landed_prose != before_prose


def test_the_row_lands_filed_carrying_what_went_and_the_diffs_of_what_was_written(rig,
                                                                                  clean_queue):
    """ADR 043 D5 in the queued shape: nobody reads the sweep's prose before it lands, so the
    reading happens afterwards — and it happens on the ROW, which is the only place the person who
    asked will look. Both halves are needed: a reader who saw only the diffs would not know what
    went, and one who saw only the paths would not know what a model wrote in their name."""
    env, deps, corpus = rig
    item = _queue_delete(clean_queue, deps, [corpus["doomed"]])

    result = _process(clean_queue, deps, item)

    assert result.report["deleted"] == [corpus["doomed"]]
    assert sorted(result.report["rewritten"]) == sorted(
        [corpus["keeps_a_link"], corpus["in_prose"], corpus["only_related"]])
    prose_diff = result.report["rewritten"][corpus["in_prose"]]
    assert prose_diff.startswith(f"--- {corpus['in_prose']}"), "a unified diff, not a summary"
    assert repair_support.DOOMED_STEM in prose_diff, "the diff shows what the reference WAS"
    assert result.result_ref.startswith(f"{corpus['doomed']}@")
    # The row, not only the return value: `brain_submissions` reads Postgres.
    status, report = _row(clean_queue, item["id"])
    assert status == schema.FILED
    assert report["deleted"] == [corpus["doomed"]]
    assert sorted(report["rewritten"]) == sorted(result.report["rewritten"])
    assert "git revert" in report["summary"], "the undo a person is owed, on the row they read"


def test_the_commit_is_authored_by_the_app_and_approved_by_the_person(rig, clean_queue):
    """ADR 043 D2, and the reason there are TWO names on one commit. The author is the librarian
    App, because the App's credential is what pushed; the `Approved-by:` trailer is the human,
    because a removal is the one write in this system a person decided. `git log` answers "who
    authorized this" with the trailer, and answering it with the author would name a bot."""
    env, deps, corpus = rig
    item = _queue_delete(clean_queue, deps, [corpus["doomed"]])

    _process(clean_queue, deps, item)

    message = repair_support.commit_message(env.bare)
    assert repair_support.commit_author(env.bare) == f"{APP_AUTHOR[0]} <{APP_AUTHOR[1]}>"
    assert PERSON not in repair_support.commit_author(env.bare), (
        "the person did not author this commit — the App did, with its own credential")
    trailers = [line for line in message.splitlines() if line.startswith("Approved-by:")]
    assert trailers == [f"Approved-by: {PERSON}"]
    assert f"Capture #{item['id']}." in message, "the row this removal came from, in the log"
    assert WHY in message, (
        "the reason a person gave IS the commit message body — it is what `git log` carries "
        "afterwards and the only thing a later reader will have")
    assert "Repair:" not in message, "a commit has ONE provenance line, not two"


def test_a_second_removal_of_the_same_page_is_refused_rather_than_re_performed(rig, clean_queue):
    """The retry a person makes when they do not see the first one land. The page is already gone,
    so the second row meets `missing-target` and lands `rejected` — the outcome that says "there is
    nothing there" rather than one that reads like a fault."""
    env, deps, corpus = rig
    first = _queue_delete(clean_queue, deps, [corpus["doomed"]])
    assert _process(clean_queue, deps, first).status == schema.FILED
    head_after_first = repair_support.remote_head(env.bare)

    second = _queue_delete(clean_queue, deps, [corpus["doomed"]], why="the same memo, again")
    result = _process(clean_queue, deps, second)

    assert result.status == schema.REJECTED
    assert result.report["reason_code"] == schema.REASON_UNREMOVABLE
    assert repair_support.remote_head(env.bare) == head_after_first, "nothing was pushed"


# ── what the worker refuses, and it refuses without pushing ────────────────────────────────────
def test_a_page_that_is_not_there_lands_rejected_and_nothing_is_pushed(rig, clean_queue):
    """The refusal a person meets after a typo. It is `rejected`, not `failed`: the row is theirs
    to act on, and the lane's own sentence — written to be published — travels verbatim into the
    report they read back."""
    env, deps, corpus = rig
    before = repair_support.remote_head(env.bare)
    item = _queue_delete(clean_queue, deps, ["wiki/notes/No Such Memo.md"])

    result = _process(clean_queue, deps, item)

    assert result.status == schema.REJECTED
    assert result.report["reason_code"] == schema.REASON_UNREMOVABLE
    assert "wiki/notes/No Such Memo.md" in result.report["summary"]
    assert repair_support.remote_head(env.bare) == before
    assert corpus["doomed"] in repair_support.remote_paths(env.bare)
    repair_support.assert_person_facing(result.report["summary"])


def test_a_path_the_lane_may_not_touch_lands_rejected_and_nothing_is_pushed(rig, clean_queue):
    """The door's zone check answers a NARROWER question than the applier's — "is this a corpus
    page at all", asked before anything is queued — so a `wiki/` path outside the deletable folders
    passes it and is refused HERE, in the tree that decides. Two checks, deliberately, and this is
    the one that has the tree."""
    env, deps, corpus = rig
    before = repair_support.remote_head(env.bare)
    item = _queue_delete(clean_queue, deps, ["wiki/people/Somebody.md"])

    result = _process(clean_queue, deps, item)

    assert result.status == schema.REJECTED
    assert result.report["reason_code"] == schema.REASON_UNREMOVABLE
    assert "wiki/people/Somebody.md" in result.report["summary"]
    assert repair_support.remote_head(env.bare) == before
    repair_support.assert_person_facing(result.report["summary"])


def test_a_reason_carrying_a_secret_is_rejected_before_any_tree_is_read(rig, clean_queue,
                                                                        monkeypatch):
    """`why` becomes a commit message, which is permanent and which no gate reads — so the reason
    goes through the SAME material scan every capture's text goes through, and it runs in
    `_pre_agent`, before a worktree exists at all. The `never` guard is what makes "before" an
    assertion rather than a claim about line order."""
    env, deps, corpus = rig

    def never(*_a, **_kw):
        raise AssertionError("a tree was checked out for a removal whose reason carries a secret")

    monkeypatch.setattr(processing.gitcmd, "ephemeral_worktree", never)
    before = repair_support.remote_head(env.bare)
    item = _queue_delete(clean_queue, deps, [corpus["doomed"]],
                         why=f"stale, and the token was {payloads.GITHUB_PAT}")

    result = _process(clean_queue, deps, item)

    assert result.status == schema.REJECTED
    assert result.report["reason_code"] == "secret"
    assert payloads.GITHUB_PAT not in result.report["summary"], (
        "a refusal that quotes the credential back is a second place it now lives")
    assert repair_support.remote_head(env.bare) == before


def test_a_sweep_the_writer_cannot_finish_lands_nothing_at_all(rig, clean_queue, monkeypatch):
    """No deterministic fallback (ADR 043 D1). `CLEAN_LLM=fake-flawed` hands every body back still
    naming the doomed page, twice, and the road ends in a refusal that names the page it could not
    reconcile — the deletion does not happen, and neither does a half-swept corpus."""
    env, deps, corpus = rig
    monkeypatch.setenv("CLEAN_LLM", "fake-flawed")
    before = repair_support.remote_head(env.bare)
    item = _queue_delete(clean_queue, deps, [corpus["doomed"]])

    result = _process(clean_queue, deps, item)

    assert result.status == schema.REJECTED
    assert result.report["reason_code"] == schema.REASON_UNREMOVABLE
    assert corpus["in_prose"] in result.report["summary"]
    assert repair_support.remote_head(env.bare) == before
    assert corpus["doomed"] in repair_support.remote_paths(env.bare)
    assert repair_support.remote_page(env.bare, corpus["in_prose"]) == (
        repair_support.page_text(env.repo, corpus["in_prose"])), (
        "not one of the pages the sweep touched was written — a partial sweep is the outcome "
        "this refusal exists to prevent")
    repair_support.assert_person_facing(result.report["summary"])
