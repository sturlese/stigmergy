"""The `delete` kind's automatic road: exact-duplicate `sources/` pages, derived by CODE.

This is the ONE proposal in the whole loop that no model is asked about, and the reason is stated
rather than assumed: two `sources/` pages declaring the same `content_hash:` are the same captured
document filed twice, and which copy goes is a lookup — inbound links, then age — not a judgment.
A model asked that question would be re-deriving a fact the frontmatter already states, and would
sometimes get it wrong.

The two properties this file exists for:

  · **the model road can never produce a deletion.** Not "does not today" — `validate_batch` drops
    one by name, in every spelling, so a compromised skill or a confused model reaches nothing.
  · **the automatic road proposes and never applies.** It stores a PENDING row exactly as every
    other road does, and a steward decides it in the review lane like any other repair.

Real findings, a real checkout, the offline double for the additive road that runs beside this one.
"""
import asyncio
import os

import pytest

from stigmergy.repair import deletion, proposer, schema, store
from tests.repair import support

# Two spellings of the same bytes; the third is a genuinely different document.
HASH_A = "1111111111111111111111111111111111111111111111111111111111111111"
HASH_B = "2222222222222222222222222222222222222222222222222222222222222222"


def _propose(conn, settings):
    return asyncio.run(proposer.propose_from_findings(conn, settings=settings))


def _seed_run(conn) -> int:
    """The propose pass runs off a completed gardener run whichever roads it takes, so every test
    here needs one even though this road reads no findings at all."""
    return support.seed_gardener_run(conn)


# ── the model may never propose a deletion, in any spelling ───────────────────────────────────
@pytest.mark.parametrize("spelling", [schema.KIND_DELETE, deletion.OP_DELETE, deletion.OP_SCRUB,
                                      "remove", "delete_page"])
def test_a_model_answer_claiming_a_deletion_is_dropped_by_name(spelling):
    """OLD BEHAVIOUR: an out-of-vocabulary op came back as `op 'delete-page' is not one of
    (...)` — true, and useless. That reason reads as a SPELLING mistake, so the one retry's job
    becomes finding the right word for a road that does not exist. Naming deletion specifically is
    what turns a guessing game into a closed door."""
    spec = proposer.ProposalSpec(
        finding_ids=[1],
        ops=[proposer.EditOp(op=spelling, path=support.NOTE_A, link="Existing Note")],
        rationale="the model would like to delete something")

    accepted, rejected = proposer.validate_batch(
        proposer.ProposalBatch(proposals=[spec]), corpus_paths={support.NOTE_A},
        link_names={"Existing Note"}, finding_ids={1}, max_ops=6, max_proposals=20)

    assert accepted == []
    assert any(proposer.NO_MODEL_DELETIONS in reason for reason in rejected[0]["reasons"])


def test_the_additive_vocabulary_is_still_accepted(conn):
    """The benign twin for the refusal above: a validator that dropped everything would pass it."""
    spec = proposer.ProposalSpec(
        finding_ids=[1],
        ops=[proposer.EditOp(op="backlink", path=support.NOTE_A, link="Existing Note")],
        rationale="these two should point at each other")

    accepted, rejected = proposer.validate_batch(
        proposer.ProposalBatch(proposals=[spec]), corpus_paths={support.NOTE_A},
        link_names={"Existing Note"}, finding_ids={1}, max_ops=6, max_proposals=20)

    assert rejected == []
    assert len(accepted) == 1


# ── the survivor is chosen by links first ─────────────────────────────────────────────────────
def test_the_copy_the_corpus_cites_survives_and_the_other_is_proposed_for_deletion(
        conn, repo_env, settings):
    """Rule one, and the one that matters most: deleting the cited copy would scrub the citation
    off every page that made it, which is a bigger change to the corpus than the duplicate is."""
    support.write_source(repo_env, "Cited Copy", content_hash=HASH_A, push=False)
    support.write_source(repo_env, "Uncited Copy", content_hash=HASH_A, push=False)
    support.write_note(repo_env, "Reads The Document", related=["Cited Copy"], push=False)
    support.librarian_support.commit_and_push(repo_env.repo, "test: two filings of one document")
    _seed_run(conn)

    result = _propose(conn, settings)

    assert result.proposed == 1
    (row,) = store.pending_proposals(conn)
    assert row["kind"] == schema.KIND_DELETE
    assert deletion.deleted_paths(row["ops"]) == ["sources/Uncited Copy.md"]
    assert "Cited Copy" in row["rationale"]


def test_on_a_tie_the_older_filing_stays_and_the_newer_one_goes(conn, repo_env, settings):
    """Rule two. Nothing links to either, so the corpus has not voted — and the later filing is
    the accident, while any external reference to this document was likelier made against the
    earlier one."""
    support.write_source(repo_env, "First Filing", content_hash=HASH_A,
                         extracted_at="2026-01-01T00:00:00Z", push=False)
    support.write_source(repo_env, "Second Filing", content_hash=HASH_A,
                         extracted_at="2026-06-01T00:00:00Z", push=False)
    support.librarian_support.commit_and_push(repo_env.repo, "test: one document, filed twice")
    _seed_run(conn)

    _propose(conn, settings)

    (row,) = store.pending_proposals(conn)
    assert deletion.deleted_paths(row["ops"]) == ["sources/Second Filing.md"]
    assert "the older filing stays" in row["rationale"]


def test_two_different_documents_are_not_duplicates(conn, repo_env, settings):
    """The benign twin for the whole road: the rule is EXACT hash equality, and a road that
    proposed deleting anything else would be deleting somebody's evidence."""
    support.write_source(repo_env, "One Document", content_hash=HASH_A, push=False)
    support.write_source(repo_env, "Another Document", content_hash=HASH_B, push=False)
    support.librarian_support.commit_and_push(repo_env.repo, "test: two real documents")
    _seed_run(conn)

    result = _propose(conn, settings)

    assert result.proposed == 0
    assert store.pending_proposals(conn) == []


def test_the_derived_proposal_records_that_no_model_was_asked(conn, repo_env, settings):
    """`model_id` is where "a model proposed this" stays true after the run. An empty one is the
    durable statement that this proposal is code's — and `delete` is the only kind for which that
    can be said."""
    support.write_source(repo_env, "First Filing", content_hash=HASH_A,
                         extracted_at="2026-01-01T00:00:00Z", push=False)
    support.write_source(repo_env, "Second Filing", content_hash=HASH_A,
                         extracted_at="2026-06-01T00:00:00Z", push=False)
    support.librarian_support.commit_and_push(repo_env.repo, "test: one document, filed twice")
    _seed_run(conn)

    _propose(conn, settings)

    (row,) = store.pending_proposals(conn)
    assert row["model_id"] == ""
    assert row["finding_ids"] == []
    assert row["finding_subjects"] == [["sources/Second Filing.md"]]


def test_a_duplicate_a_steward_has_already_decided_is_not_proposed_again(conn, repo_env, settings):
    """The dismissal memory reaches this road too, and it has to: a duplicate pair does not stop
    being a duplicate pair because somebody said no, so without the memory it would be the one
    question a steward is asked every single night."""
    support.write_source(repo_env, "First Filing", content_hash=HASH_A,
                         extracted_at="2026-01-01T00:00:00Z", push=False)
    support.write_source(repo_env, "Second Filing", content_hash=HASH_A,
                         extracted_at="2026-06-01T00:00:00Z", push=False)
    support.librarian_support.commit_and_push(repo_env.repo, "test: one document, filed twice")
    _seed_run(conn)
    _propose(conn, settings)
    (row,) = store.pending_proposals(conn)
    store.mark_decided(conn, row["id"], status=schema.STATUS_REJECTED, decided_by=support.STEWARD)

    result = _propose(conn, settings)

    assert result.proposed == 0
    assert store.pending_proposals(conn) == []


def test_the_run_ceiling_bounds_this_road_like_every_other(conn, repo_env, settings):
    """One night, one budget. A corpus that turned out to hold forty duplicate filings must not
    produce forty approvals in one inbox — the rest are proposed by the next run, once these have
    been decided."""
    from dataclasses import replace
    for n in range(3):
        support.write_source(repo_env, f"Copy {n} of A", content_hash=HASH_A,
                             extracted_at=f"2026-0{n + 1}-01T00:00:00Z", push=False)
        support.write_source(repo_env, f"Copy {n} of B", content_hash=HASH_B,
                             extracted_at=f"2026-0{n + 1}-01T00:00:00Z", push=False)
    support.librarian_support.commit_and_push(repo_env.repo, "test: two documents, filed thrice")
    _seed_run(conn)

    result = _propose(conn, replace(settings, max_proposals_per_run=1))

    assert result.proposed == 1
    assert any("run-ceiling-reached" in reason for reason in result.skip_reasons)


def test_a_duplicate_the_sweep_cannot_clear_is_recorded_rather_than_crashing_the_run(
        conn, repo_env, settings):
    """A nightly job that raised on one awkward page would stop proposing anything at all. The
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
    support.librarian_support.commit_and_push(repo_env.repo, "test: an unreachable reference")
    _seed_run(conn)

    result = _propose(conn, settings)

    assert result.proposed == 0
    assert any("Odd Reference" in reason for reason in result.skip_reasons)
