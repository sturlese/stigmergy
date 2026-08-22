"""`brain_delete` — a person's own deletion, decided and applied in ONE call (ADR 043 D2).

Against a REAL bare remote with the REAL gates and the REAL gitleaks pass: this door's whole claim
is that nothing was skipped by removing the second click, so a faked apply would prove none of it.
The model is the only double, `CLEAN_LLM=fake` through the package's own writer.

The four properties, and each is a thing the second click used to supply or was said to:

  · authorization runs IN THE ACT and before anything is cloned: an UNRESTRICTED identity may
    remove pages (ADR 044 D3 — the only kind that can see every page a removal touches, including
    the ones the sweep rewrites), and a scoped one is refused with the lane's anonymous sentence;
  · the row is born `approved` in the caller's name, applied in the same call, and never listed as
    pending — nobody is asked a question the caller already answered;
  · the diff comes back, because nobody read the written prose before it landed (D5);
  · a sweep that cannot be written lands nothing at all.
"""
import json

import pytest

from stigmergy.librarian import gitcmd
from stigmergy.repair import deletion
from stigmergy.repair import schema as repair_schema
from stigmergy.repair import store as repair_store
from stigmergy.server import review
from tests.repair import support as repair_support
from tests.server.conftest import ALICE, STEWARD
from tests.server.conftest import make_review_service as make_service

pytestmark = pytest.mark.usefixtures("require_gitleaks")

WHY = "the memo was superseded and nothing needs it any more"


@pytest.fixture(autouse=True)
def clean_llm(monkeypatch):
    """The suite is keyless by construction: this door builds a model-backed writer, and a machine
    with `CLEAN_LLM=openai` exported would otherwise turn it into one that spends money."""
    monkeypatch.setenv("CLEAN_LLM", "fake")


@pytest.fixture()
def indexed_pages(conn):
    """Rows this file puts in `pages_index`, removed again on teardown. The diffs are ACL-scoped
    through the index, so a page this server does not carry has no readable diff — which is the
    fail-closed reading `read_page` gives and the reason these rows are seeded rather than
    assumed."""
    paths = []

    def index(path, *, entity=(), acl=None):
        with conn.cursor() as cur:
            cur.execute("DELETE FROM pages_index WHERE path = %s", (path,))
            cur.execute(
                "INSERT INTO pages_index (path, page_id, zone, title, body, type, entity, acl, "
                "content_hash) VALUES (%s, %s, 'wiki', %s, '', 'note', %s, %s, '')",
                (path, path, path, list(entity), acl))
        paths.append(path)

    yield index
    with conn.cursor() as cur:
        cur.execute("DELETE FROM pages_index WHERE path = ANY(%s)", (paths,))


@pytest.fixture()
def corpus(env):
    """The fixture repo plus the proposer's skill (the writer reads the same brief) and the
    deletion corpus every test here deletes out of."""
    repair_support.write_skill(env.repo)
    from tests.librarian import support as librarian_support
    librarian_support.commit_and_push(env.repo, "test: add the repair-proposer skill")
    return repair_support.seed_deletion_corpus(env)


def _remote_paths(bare: str, ref: str = "main") -> list[str]:
    out = gitcmd.run("ls-tree", "-r", "-z", "--name-only", ref, cwd=bare).stdout
    return [path for path in out.split("\0") if path]


def _remote_page(bare: str, path: str, ref: str = "main") -> str:
    return gitcmd.run("show", f"{ref}:{path}", cwd=bare).stdout


def _delete(env, conn, paths, *, identity=STEWARD, why=WHY, audiences=None):
    return review.delete_pages(
        make_service(env, conn, identity_name=identity, audiences=audiences),
        paths=paths, why=why, source="mcp")


# ── the act ───────────────────────────────────────────────────────────────────────────────────
def test_an_unrestricted_callers_deletion_lands_as_one_commit_in_the_same_call(env, conn, corpus):
    """The whole door in one assertion set: the page is gone from the remote, the three pages that
    referred to it no longer do, and it took ONE call — no row waited on anybody, and the person
    who typed it was never asked to agree with themselves."""
    before = _remote_page(env.bare, corpus["in_prose"])

    result = _delete(env, conn, [corpus["doomed"]])

    assert result["deleted"] == [corpus["doomed"]]
    assert corpus["doomed"] not in _remote_paths(env.bare)
    assert result["commit"] == gitcmd.run("rev-parse", "main", cwd=env.bare).stdout.strip()
    stems = {repair_support.DOOMED_STEM}
    for path in (corpus["keeps_a_link"], corpus["in_prose"], corpus["only_related"]):
        assert not deletion.references(_remote_page(env.bare, path), stems), (
            f"{path} still refers to the page that is gone")
    assert f"as {repair_support.DOOMED_STEM} records" in _remote_page(env.bare, corpus["in_prose"]), (
        "the sentence survives the page it cited — reconciled, not shredded")
    assert "[[Existing Note]]" in _remote_page(env.bare, corpus["keeps_a_link"]), (
        "a sweep reconciles one reference, not the list it was in")
    assert before != _remote_page(env.bare, corpus["in_prose"])


def test_the_row_is_born_approved_in_the_callers_name_and_is_never_pending(env, conn, corpus):
    """ADR 043 D2's bookkeeping half. The console's history, the metrics and the ledger keep their
    source of truth — one `repair_proposals` row — and the inbox never lists a question whose
    answer was given in the same breath as the question."""
    assert repair_store.pending_proposals(conn) == []

    result = _delete(env, conn, [corpus["doomed"]])

    row = repair_store.proposal(conn, result["proposal_id"])
    assert row["status"] == repair_schema.STATUS_APPLIED
    assert row["decided_by"] == STEWARD
    assert row["applied_commit"] == result["commit"]
    assert row["kind"] == repair_schema.KIND_DELETE
    assert row["rationale"] == WHY
    assert repair_store.pending_proposals(conn) == [], "it was never a question for anybody"


def test_the_response_carries_the_diff_because_nobody_read_the_prose_first(env, conn, corpus,
                                                                           indexed_pages):
    """ADR 043 D5, and the reason it is a decision rather than an omission: the fidelity of a
    rewritten paragraph has no proof code can run, so the reading moves from before the push to
    after it — and the diff has to come back in the same breath, or it has not moved at all.

    FENCED, like every other surface that echoes a page: a diff carries the page's own bytes and
    fresh model output, and neither is an instruction to whoever reads this response."""
    rewritten = [corpus["keeps_a_link"], corpus["in_prose"], corpus["only_related"]]
    for path in rewritten:
        indexed_pages(path, entity=[])

    result = _delete(env, conn, [corpus["doomed"]])

    assert sorted(result["rewritten"]) == sorted(rewritten)
    assert result["withheld"] == []
    prose_diff = result["rewritten"][corpus["in_prose"]]
    assert "UNTRUSTED-DATA" in prose_diff
    assert repair_support.DOOMED_STEM in prose_diff, "the diff shows what the reference WAS"
    assert "git revert" in result["message"]


def test_a_diff_this_server_cannot_place_is_withheld_and_named(env, conn, corpus,
                                                              indexed_pages):
    """**The reading is still `acl.visible()`'s question, and it fails CLOSED.** The diffs are page
    bytes, so each one is asked of the caller's own audiences through the index — and a page this
    server's index does not carry answers "no", the same reading `read_page` gives. It is NAMED
    rather than dropped: it changed, the commit says so, and a reader who cannot see why must not
    be left thinking nothing happened to it."""
    # Two of the three rewritten pages are indexed; the third is not, so its diff cannot be placed.
    for path in (corpus["keeps_a_link"], corpus["only_related"]):
        indexed_pages(path, entity=[])

    result = _delete(env, conn, [corpus["doomed"]])

    assert result["withheld"] == [corpus["in_prose"]]
    assert corpus["in_prose"] not in result["rewritten"]
    assert sorted(result["rewritten"]) == sorted([corpus["keeps_a_link"],
                                                  corpus["only_related"]])
    assert "withheld" in result["message"]


# ── authorization, in the act ─────────────────────────────────────────────────────────────────
def test_a_scoped_caller_is_refused_before_anything_is_cloned(env, conn, corpus, monkeypatch):
    """OLD BEHAVIOUR: the per-path steward guard, run twice. ADR 044 D3 asks the one question the
    server can answer before it clones — is this identity unrestricted — because the pages a
    removal touches include every page that refers to them, which nothing knows until the clone.
    The sentence is the lane's own `NOT_YOURS_TO_DECIDE`, so "not authorized" and "no such page"
    stay indistinguishable, and the refusal costs no network leg."""
    def never(*_a, **_k):
        raise AssertionError("the repo was cloned for a caller who may not delete anything")

    monkeypatch.setattr(review.repair_remote, "cloned", never)

    with pytest.raises(review.ReviewError) as caught:
        _delete(env, conn, [corpus["doomed"]], identity=ALICE, audiences={"sales"})

    assert str(caught.value) == review.NOT_YOURS_TO_DECIDE
    assert repair_store.pending_proposals(conn) == []


def test_an_unattributed_call_is_refused(env, conn, corpus):
    """A deletion attributed to nobody would be a page removed with no answer to "who said so"."""
    service = make_service(env, conn, identity_name=STEWARD)
    service.identity = None

    with pytest.raises(review.ReviewError, match="unattributed"):
        review.delete_pages(service, paths=[corpus["doomed"]], why=WHY,
                            source="mcp")


# ── what the door refuses, and it refuses before it clones ────────────────────────────────────
@pytest.mark.parametrize("paths, why, phrase", [
    ([], WHY, "at least one page"),
    (["wiki/notes/A.md"], "   ", "needs a reason"),
    ([f"wiki/notes/{n}.md" for n in range(review.MAX_DELETED_PAGES + 1)], WHY, "at most"),
], ids=["no-page", "no-reason", "too-many"])
def test_a_malformed_deletion_is_refused_and_nothing_is_cloned(env, conn, monkeypatch, paths, why,
                                                               phrase):
    def never(*_a, **_k):
        raise AssertionError("the repo was cloned for a call that should have been refused")

    monkeypatch.setattr(review.repair_remote, "cloned", never)

    with pytest.raises(review.ReviewError, match=phrase):
        _delete(env, conn, paths, why=why)


def test_an_entity_page_is_refused_by_name_and_nothing_is_pushed(env, conn, corpus):
    """The refusal a person is most likely to meet, and the one that has to explain itself: an
    identity is retired through governance, not deleted."""
    before = gitcmd.run("rev-parse", "main", cwd=env.bare).stdout.strip()

    with pytest.raises(review.ReviewError, match="identity"):
        _delete(env, conn, ["wiki/entities/Acme Corp.md"])

    assert gitcmd.run("rev-parse", "main", cwd=env.bare).stdout.strip() == before
    assert repair_store.pending_proposals(conn) == []


def test_a_reason_carrying_a_credential_is_refused_before_it_reaches_a_commit_message(env, conn,
                                                                                       corpus):
    """`why` becomes a commit message and a ledger row, both permanent. It runs the same secrets
    scan every other free-text field on this lane runs, and it runs BEFORE the clone."""
    from tests import adversarial_payloads

    with pytest.raises(review.ReviewError):
        _delete(env, conn, [corpus["doomed"]],
                why=f"stale, and the token was {adversarial_payloads.GITHUB_PAT}")

    assert repair_store.pending_proposals(conn) == []


def test_a_sweep_the_writer_cannot_finish_lands_nothing_at_all(env, conn, corpus, monkeypatch):
    """No deterministic fallback (ADR 043 D1). `CLEAN_LLM=fake-flawed` hands every body back still
    naming the doomed page, twice, and the road ends in a refusal that names the page — the
    deletion does not happen, and neither does a half-swept corpus."""
    monkeypatch.setenv("CLEAN_LLM", "fake-flawed")
    before = gitcmd.run("rev-parse", "main", cwd=env.bare).stdout.strip()

    with pytest.raises(review.ReviewError) as caught:
        _delete(env, conn, [corpus["doomed"]])

    assert corpus["in_prose"] in str(caught.value)
    assert gitcmd.run("rev-parse", "main", cwd=env.bare).stdout.strip() == before
    assert corpus["doomed"] in _remote_paths(env.bare)
    assert repair_store.pending_proposals(conn) == []
    assert sum(repair_store.counts_by_status(conn).values()) == 0, (
        "a refusal before the row is inserted leaves no row at all")


def test_the_refusals_this_door_publishes_are_written_for_a_person(env, conn, corpus,
                                                                   monkeypatch):
    """Every sentence crosses to a person over MCP, so none may name this host's throwaway clone
    or hand out a command to run."""
    monkeypatch.setenv("CLEAN_LLM", "fake-flawed")
    said = []
    for paths, identity in (([corpus["doomed"]], STEWARD),
                            (["wiki/entities/Acme Corp.md"], STEWARD)):
        with pytest.raises(review.ReviewError) as caught:
            _delete(env, conn, paths, identity=identity)
        said.append(str(caught.value))

    assert len(said) == 2
    for message in said:
        repair_support.assert_person_facing(message)


# ── the audit row ─────────────────────────────────────────────────────────────────────────────
def test_the_audit_row_keeps_the_shape_and_never_the_reason(env, conn, corpus):
    """`why` is free text a person wrote about pages they read — a length and the paths are what
    an operator needs from `audit_log`, and the sentence itself lives in the ledger and the commit
    where it was written to be read."""
    class _Audit:
        rows: list = []

        def write(self, **kwargs):
            self.rows.append(kwargs)

    audit = _Audit()
    service = make_service(env, conn, identity_name=STEWARD)
    service.audit = audit

    service.delete_pages([corpus["doomed"]], WHY, source="mcp")

    (row,) = [r for r in audit.rows if r["tool"] == "brain_delete"]
    assert row["identity"] == STEWARD
    assert row["outcome"] == "ok"
    assert row["args"]["paths"] == [corpus["doomed"]]
    assert row["args"]["why_chars"] == len(WHY)
    assert WHY not in json.dumps(row["args"])
