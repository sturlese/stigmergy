"""The `delete` kind's automatic road: exact-duplicate `sources/` pages, derived by CODE and
removed in the same pass.

This is the ONE repair in the whole loop that no model is asked WHICH pages to remove, and the
reason is stated rather than assumed: two `sources/` pages declaring the same `content_hash:` are
the same captured document filed twice, and which copy goes is a lookup — inbound links, then age —
not a judgment. A model asked that question would be re-deriving a fact the frontmatter already
states, and would sometimes get it wrong.

The two properties this file exists for:

  · **the model road can never produce a deletion.** Not "does not today" — `validate_batch` drops
    one by name, in every spelling, so a compromised skill or a confused model reaches nothing.
  · **this road removes pages with nobody asked** (ADR 044), so every test says what reached the
    remote: which page is gone from the tree, and what the pages that cited it now say.

Real findings, a real checkout, the offline doubles for the model, the real gates, and a real bare
remote as the last word.
"""
import os

import pytest

from stigmergy.repair import deletion, schema, store
from stigmergy.repair import run as repair_run
from tests import adversarial_payloads
from tests.librarian import support as librarian_support
from tests.repair import support

# Every pass here applies through the nine gates, so a machine without gitleaks cannot exercise any
# of it: skip on a laptop, FAIL in CI.
pytestmark = pytest.mark.usefixtures("require_gitleaks")

# Two spellings of the same bytes; the third is a genuinely different document.
HASH_A = "1111111111111111111111111111111111111111111111111111111111111111"
HASH_B = "2222222222222222222222222222222222222222222222222222222222222222"


def _seed_run(conn) -> int:
    """The pass runs off a completed gardener run whichever roads it takes, so every test here
    needs one even though this road reads no findings at all."""
    return support.seed_gardener_run(conn)


def _two_filings(repo_env, *, first="First Filing", second="Second Filing") -> None:
    """One document filed twice, nothing pointing at either — the tie the age rule breaks."""
    support.write_source(repo_env, first, content_hash=HASH_A,
                         extracted_at="2026-01-01T00:00:00Z", push=False)
    support.write_source(repo_env, second, content_hash=HASH_A,
                         extracted_at="2026-06-01T00:00:00Z", push=False)
    librarian_support.commit_and_push(repo_env.repo, "test: one document, filed twice")


# ── the model may never propose a deletion, in any spelling ───────────────────────────────────
@pytest.mark.parametrize("spelling", [schema.KIND_DELETE, deletion.OP_DELETE, deletion.OP_SCRUB,
                                      "remove", "delete_page"])
def test_a_model_answer_claiming_a_deletion_is_dropped_by_name(spelling):
    """OLD BEHAVIOUR: an out-of-vocabulary op came back as `op 'delete-page' is not one of
    (...)` — true, and useless. That reason reads as a SPELLING mistake, so the one retry's job
    becomes finding the right word for a road that does not exist. Naming deletion specifically is
    what turns a guessing game into a closed door — and under ADR 044 the door matters more, since
    what is on the other side of it is a commit nobody reviews."""
    spec = repair_run.ProposalSpec(
        finding_ids=[1],
        ops=[repair_run.EditOp(op=spelling, path=support.NOTE_A, link="Existing Note")],
        rationale="the model would like to delete something")

    accepted, rejected = repair_run.validate_batch(
        repair_run.ProposalBatch(proposals=[spec]), corpus_paths={support.NOTE_A},
        link_names={"Existing Note"}, finding_ids={1}, max_ops=6, max_proposals=20)

    assert accepted == []
    assert any(repair_run.NO_MODEL_DELETIONS in reason for reason in rejected[0]["reasons"])


def test_the_additive_vocabulary_is_still_accepted():
    """The benign twin for the refusal above: a validator that dropped everything would pass it."""
    spec = repair_run.ProposalSpec(
        finding_ids=[1],
        ops=[repair_run.EditOp(op="backlink", path=support.NOTE_A, link="Existing Note")],
        rationale="these two should point at each other")

    accepted, rejected = repair_run.validate_batch(
        repair_run.ProposalBatch(proposals=[spec]), corpus_paths={support.NOTE_A},
        link_names={"Existing Note"}, finding_ids={1}, max_ops=6, max_proposals=20)

    assert rejected == []
    assert len(accepted) == 1


# ── the survivor is chosen by links first ─────────────────────────────────────────────────────
def test_the_copy_the_corpus_cites_survives_and_the_other_leaves_the_tree(conn, repo_env,
                                                                          settings):
    """Rule one, and the one that matters most: deleting the cited copy would scrub the citation
    off every page that made it, which is a bigger change to the corpus than the duplicate is."""
    support.write_source(repo_env, "Cited Copy", content_hash=HASH_A, push=False)
    support.write_source(repo_env, "Uncited Copy", content_hash=HASH_A, push=False)
    support.write_note(repo_env, "Reads The Document", related=["Cited Copy"], push=False)
    librarian_support.commit_and_push(repo_env.repo, "test: two filings of one document")
    _seed_run(conn)

    result = support.run_pass(conn, repo_env, settings)

    assert result.applied == 1
    (row,) = store.recent(conn)
    assert row["status"] == schema.STATUS_APPLIED
    assert row["kind"] == schema.KIND_DELETE
    assert deletion.deleted_paths(row["ops"]) == ["sources/Uncited Copy.md"]
    assert "Cited Copy" in row["rationale"]
    paths = support.remote_paths(repo_env.bare)
    assert "sources/Uncited Copy.md" not in paths
    assert "sources/Cited Copy.md" in paths, "the copy the corpus cites is the copy that stays"


def test_on_a_tie_the_older_filing_stays_and_the_newer_one_goes(conn, repo_env, settings):
    """Rule two. Nothing links to either, so the corpus has not voted — and the later filing is
    the accident, while any external reference to this document was likelier made against the
    earlier one."""
    _two_filings(repo_env)
    _seed_run(conn)

    support.run_pass(conn, repo_env, settings)

    (row,) = store.recent(conn)
    assert deletion.deleted_paths(row["ops"]) == ["sources/Second Filing.md"]
    assert "the older filing stays" in row["rationale"]
    paths = support.remote_paths(repo_env.bare)
    assert "sources/Second Filing.md" not in paths
    assert "sources/First Filing.md" in paths


def test_two_different_documents_are_not_duplicates(conn, repo_env, settings):
    """The benign twin for the whole road, and ADR 044 is what sharpens it: the rule is EXACT hash
    equality, and a road that removed anything else would be deleting somebody's evidence with
    nobody in the way."""
    support.write_source(repo_env, "One Document", content_hash=HASH_A, push=False)
    support.write_source(repo_env, "Another Document", content_hash=HASH_B, push=False)
    librarian_support.commit_and_push(repo_env.repo, "test: two real documents")
    _seed_run(conn)
    before = support.commit_count(repo_env.bare)

    result = support.run_pass(conn, repo_env, settings)

    assert result.applied == 0
    assert store.recent(conn) == []
    assert support.commit_count(repo_env.bare) == before
    for path in ("sources/One Document.md", "sources/Another Document.md"):
        assert path in support.remote_paths(repo_env.bare)


def test_the_derived_repair_records_that_no_model_was_asked(conn, repo_env, settings):
    """`model_id` is where "a model wrote something here" stays true after the pass is gone.
    Nothing refers to either copy, so no page had to be written and the column is empty — which is
    also the durable statement that no model chose this deletion, the one thing that is true of
    this kind whatever the column says (ADR 043 D1)."""
    _two_filings(repo_env)
    _seed_run(conn)

    support.run_pass(conn, repo_env, settings)

    (row,) = store.recent(conn)
    assert row["model_id"] == ""
    assert row["finding_ids"] == []
    assert row["finding_subjects"] == [["sources/Second Filing.md"]]


def test_the_pages_that_cited_the_doomed_copy_are_WRITTEN_and_land_reconciled(conn, repo_env,
                                                                               settings):
    """The nightly road takes the same split as the act road (ADR 043 D1): code drops the
    frontmatter entry, and the sweep writer reconciles the BODY — so the bytes a model wrote are
    the bytes that land, and `model_id` names who wrote them.

    Red before ADR 043: this road stored a body with `[[Uncited Copy]]` unlinked and the sentence
    around it untouched. Red before ADR 044: nothing here reached a page at all until somebody
    approved it, so the reconciliation was only ever asserted against a stored plan."""
    support.write_source(repo_env, "Cited Copy", content_hash=HASH_A, push=False)
    support.write_source(repo_env, "Uncited Copy", content_hash=HASH_A, push=False)
    support.write_note(repo_env, "Reads The Document", related=["Cited Copy"], push=False,
                       body="# Reads The Document\n\n## What it says\n\nThe filing is recorded "
                            "in [[Uncited Copy]], which the team read.\n\n"
                            + "\n".join(f"- padding line {n}." for n in range(1, 26)) + "\n")
    librarian_support.commit_and_push(repo_env.repo, "test: a page citing the doomed copy")
    _seed_run(conn)

    result = support.run_pass(conn, repo_env, settings)

    assert result.applied == 1
    (row,) = store.recent(conn)
    assert deletion.deleted_paths(row["ops"]) == ["sources/Uncited Copy.md"]
    assert row["model_id"] == settings.model, "a model wrote the page that stays, and says so"
    assert any(call["road"] == schema.KIND_DELETE for call in result.model_calls)
    landed = support.remote_page(repo_env.bare, "wiki/notes/Reads The Document.md")
    assert not deletion.references(landed, {"Uncited Copy"})
    assert "which the team read" in landed, "the sentence survives the page it cited"


def test_a_duplicate_whose_pages_the_writer_cannot_reconcile_is_recorded_and_nothing_is_removed(
        conn, repo_env, settings, monkeypatch):
    """No deterministic fallback on this road either. `CLEAN_LLM=fake-flawed` hands the body back
    still naming the doomed copy, twice; nothing is derived, nothing is removed, the reason names
    the page, and the additive road beside it keeps running."""
    monkeypatch.setenv("CLEAN_LLM", "fake-flawed")
    support.write_source(repo_env, "Cited Copy", content_hash=HASH_A, push=False)
    support.write_source(repo_env, "Uncited Copy", content_hash=HASH_A, push=False)
    support.write_note(repo_env, "Reads The Document", related=["Cited Copy"], push=False,
                       body="# Reads The Document\n\n## What it says\n\nSee [[Uncited Copy]].\n\n"
                            + "\n".join(f"- padding line {n}." for n in range(1, 26)) + "\n")
    librarian_support.commit_and_push(repo_env.repo, "test: a page citing the doomed copy")
    _seed_run(conn)
    before = support.commit_count(repo_env.bare)

    result = support.run_pass(conn, repo_env, settings)

    assert [r for r in store.recent(conn) if r["kind"] == schema.KIND_DELETE] == []
    assert support.commit_count(repo_env.bare) == before
    assert "sources/Uncited Copy.md" in support.remote_paths(repo_env.bare)
    assert any("Reads The Document" in reason for reason in result.skip_reasons), (
        result.skip_reasons)


def test_a_duplicate_the_gates_refused_is_recorded_failed_and_never_derived_again(
        conn, repo_env, settings):
    """**The memory this road cannot do without, and the only state in which it is observable.** A
    deletion that LANDS removes the duplicate, so the next pass finds no group and the memory is
    moot; a deletion the gates refused leaves both copies exactly where they were, and without the
    memory this road would re-derive it, re-write the referring pages with a model call and be
    refused again — every night, forever.

    The refusal is reached through the real machinery rather than arranged: the page that cites the
    doomed copy carries a credential on the very line the sweep rewrites, so the scrub's added line
    trips the same gitleaks pass a filing goes through. Nothing is pushed, the row says which gate
    said no, and its key is remembered like an applied one's.
    """
    support.write_source(repo_env, "Cited Copy", content_hash=HASH_A, push=False)
    support.write_source(repo_env, "Uncited Copy", content_hash=HASH_A, push=False)
    support.write_note(repo_env, "Reads The Document", related=["Cited Copy"], push=False,
                       body=f"# Reads The Document\n\n## What it says\n\nThe filing is recorded in "
                            f"[[Uncited Copy]] and the deploy token was "
                            f"{adversarial_payloads.GITHUB_PAT}.\n\n"
                            + "\n".join(f"- padding line {n}." for n in range(1, 26)) + "\n")
    librarian_support.commit_and_push(repo_env.repo, "test: a citing page carrying a credential")
    _seed_run(conn)
    before = support.commit_count(repo_env.bare)

    first = support.run_pass(conn, repo_env, settings)

    assert (first.applied, first.failed) == (0, 1)
    (row,) = store.recent(conn)
    assert row["status"] == schema.STATUS_FAILED
    assert "secrets/" in row["error"]
    assert adversarial_payloads.GITHUB_PAT not in row["error"]
    assert support.commit_count(repo_env.bare) == before
    assert "sources/Uncited Copy.md" in support.remote_paths(repo_env.bare)

    _seed_run(conn)
    again = support.run_pass(conn, repo_env, settings)

    assert (again.applied, again.failed) == (0, 0)
    assert len(store.recent(conn)) == 1, "the refused deletion was derived a second time"
    assert again.model_calls == [], "and it was re-written by a model on the way there"


def test_the_pass_ceiling_bounds_this_road_like_every_other(conn, repo_env, settings):
    """One pass, one bounded blast radius. A corpus that turned out to hold forty duplicate filings
    must not lose forty pages in one afternoon — the rest go on the next pass, and what this one
    deferred is counted in `job_runs.stats` rather than dropped in silence."""
    from dataclasses import replace
    for n in range(3):
        support.write_source(repo_env, f"Copy {n} of A", content_hash=HASH_A,
                             extracted_at=f"2026-0{n + 1}-01T00:00:00Z", push=False)
        support.write_source(repo_env, f"Copy {n} of B", content_hash=HASH_B,
                             extracted_at=f"2026-0{n + 1}-01T00:00:00Z", push=False)
    librarian_support.commit_and_push(repo_env.repo, "test: two documents, filed thrice")
    _seed_run(conn)
    before = support.commit_count(repo_env.bare)

    result = support.run_pass(conn, repo_env, replace(settings, max_repairs_per_run=1))

    assert result.applied == 1
    assert support.commit_count(repo_env.bare) == before + 1
    assert any("run-ceiling-reached" in reason for reason in result.skip_reasons)


def test_a_duplicate_the_sweep_cannot_clear_is_recorded_rather_than_crashing_the_pass(
        conn, repo_env, settings):
    """A nightly pass that raised on one awkward page would stop repairing anything at all. The
    refusal is a recorded skip with the page's own name in it, and the additive road beside this
    one keeps running."""
    support.write_source(repo_env, "Cited Copy", content_hash=HASH_A, push=False)
    support.write_source(repo_env, "Odd Copy", content_hash=HASH_A, push=False)
    support.write_note(repo_env, "Reader One", related=["Cited Copy"], push=False)
    support.write_note(repo_env, "Reader Two", related=["Cited Copy"], push=False)
    # A wikilink in `aliases:` — a field this sweep knows nothing about, and one the contract
    # linter still resolves, so the reference would survive the deletion as a dead link.
    support.write_note(repo_env, "Odd Reference", push=False)
    odd = os.path.join(repo_env.repo, "wiki", "notes", "Odd Reference.md")
    with open(odd, encoding="utf-8") as f:
        text = f.read()
    with open(odd, "w", encoding="utf-8") as f:
        f.write(text.replace("sources: []", 'aliases: ["[[Odd Copy]]"]'))
    librarian_support.commit_and_push(repo_env.repo, "test: an unreachable reference")
    _seed_run(conn)
    before = support.commit_count(repo_env.bare)

    result = support.run_pass(conn, repo_env, settings)

    assert result.applied == 0
    assert support.commit_count(repo_env.bare) == before
    assert any("Odd Reference" in reason for reason in result.skip_reasons)
