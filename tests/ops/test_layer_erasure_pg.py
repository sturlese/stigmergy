"""Erasing a layer on real Git and Postgres (specs/data-erasure.md §3.3, ER-01 to ER-04, ER-06)."""
import gzip
import os
import subprocess
from dataclasses import dataclass
from types import SimpleNamespace

import pytest
from psycopg.types.json import Jsonb

from stigmergy.admin.schema import ensure_admin_schema
from stigmergy.capture import evidence, queue, schema
from stigmergy.capture.errors import LayerErasing
from stigmergy.capture.schema import Actor
from stigmergy.capture.service import CaptureService
from stigmergy.capture.source import source_path
from stigmergy.changes.store import record_change
from stigmergy.index import build
from stigmergy.index import health as index_health
from stigmergy.index.backends.embedder import build_embedder
from stigmergy.index.errors import StigmergyIndexError
from stigmergy.knowledge.plan import FilingPlan, PageMutation
from stigmergy.knowledge.planner import ScriptedPlanner
from stigmergy.knowledge.writer import WriterDeps
from stigmergy.librarian import config, worker
from stigmergy.ops import erase_layer, erasure
from stigmergy.server.audit import ensure_audit_table
from stigmergy.server.service import BrainService
from tests.capture.conftest import clean_queue, conn
from tests.knowledge.conftest import target_repo

__all__ = ["clean_queue", "conn", "target_repo"]

MARKER = "QUOKKA-7731"
ERASED, KEPT = "finance", "engineering"
BOB = Actor(subject="bob", display_name="Bob")
ALICE = Actor(subject="alice", display_name="Alice")
SHARED_TEXT = "Shared quarterly figures: revenue grew four percent."
DEAL = f"layers/{ERASED}/wiki/notes/Quokka deal.md"
MEETING = f"layers/{KEPT}/wiki/notes/Tessera meeting.md"
GIT_ENV = {**os.environ, "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@example.com",
           "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@example.com"}


@dataclass
class Corpus:
    conn: object
    repo: str
    bare: str
    store: evidence.MemoryEvidenceStore
    erased_captures: list
    kept_captures: list
    first_erased_commit: str


def _git(cwd, *args, check=True):
    return subprocess.run(["git", *args], cwd=cwd, check=check, capture_output=True, text=True,
                          env=GIT_ENV).stdout.strip()


def _note(title: str, text: str, entity: str) -> PageMutation:
    return PageMutation(action="create", role="note", title=title, reason="note",
                        body=f"# {title}\n\n{text} (Source: `{{source}}`)\n\n## Entities\n"
                             f"- [[{entity}]] - named in the source")


def _entity(title: str, text: str, note: str) -> PageMutation:
    return PageMutation(action="create", role="entity", title=title, entity_type="organization",
                        reason="entity",
                        body=f"# {title}\n\n## What / Who\n{text} (Source: `{{source}}`)\n\n"
                             f"## Connections\n- [[{note}]] - where it appears")


def _submit(conn, store, *, actor, layer, key, text, title=None):
    receipt = CaptureService(conn, store).capture_text(
        actor=actor, audience=(layer,), adapter="mcp", text=text, idempotency_key=key, title=title)
    return receipt, source_path(schema.parse_capture(receipt["request"]))


def _process(conn, repo, store, mutations=(), *, source=""):
    plan = FilingPlan(summary="Filed", mutations=tuple(
        m.model_copy(update={"body": m.body.replace("{source}", source)}) for m in mutations))
    settings = config.Settings(repo=str(repo), branch="main", backend="scripted")
    item, outcome = worker.process_next(conn, WriterDeps(settings, store, ScriptedPlanner(plan),
                                                         str(repo)))
    assert outcome.status == schema.LANDED, outcome
    return item


def _file(conn, repo, store, *, actor, layer, key, text, title=None, mutations=()):
    receipt, source = _submit(conn, store, actor=actor, layer=layer, key=key, text=text, title=title)
    return _process(conn, repo, store, mutations, source=source)


def _embedder():
    return build_embedder("fake")


@pytest.fixture()
def corpus(clean_queue, target_repo, tmp_path_factory):
    conn, repo = clean_queue, str(target_repo)
    ensure_admin_schema(conn)
    ensure_audit_table(conn)
    with conn.cursor() as cursor:
        for table in ("layer_erasures", "graph_measures", "audit_log", "admin_actions",
                      "embedding_cache"):
            if _table(cursor, table):
                cursor.execute(f"DELETE FROM {table}")
    bare = str(tmp_path_factory.mktemp("origin") / "brain.git")
    _git(repo, "clone", "--bare", "--quiet", repo, bare)
    _git(repo, "remote", "add", "origin", bare)
    _git(repo, "fetch", "--quiet", "origin")
    _git(repo, "branch", "--quiet", "--set-upstream-to=origin/main", "main")
    store = evidence.MemoryEvidenceStore()

    deal = _file(conn, repo, store, actor=BOB, layer=ERASED, key="quokka",
                 text=f"Board minutes {MARKER}: the Quokka deal closes in May.",
                 title=f"Quokka minutes {MARKER}",
                 mutations=(_note("Quokka deal", f"{MARKER}: the Quokka deal closes in May.",
                                  "Quokka Holdings"),
                            _entity("Quokka Holdings", f"The buyer in deal {MARKER}.",
                                    "Quokka deal")))
    figures = _file(conn, repo, store, actor=BOB, layer=ERASED, key="finance-figures",
                    text=SHARED_TEXT)
    tessera = _file(conn, repo, store, actor=ALICE, layer=KEPT, key="tessera",
                    text="Tessera presented its pilot on 3 March 2026.",
                    mutations=(_note("Tessera meeting", "Tessera presented its pilot.", "Tessera"),
                               _entity("Tessera", "An edtech company.", "Tessera meeting")))
    shared = _file(conn, repo, store, actor=ALICE, layer=KEPT, key="engineering-figures",
                   text=SHARED_TEXT)

    # A garden-like commit that touches both layers at once.
    _git(repo, "fetch", "--quiet", "origin")
    _git(repo, "checkout", "--quiet", "-B", "main", "origin/main")
    parent = _git(repo, "rev-parse", "HEAD")
    for path in (DEAL, MEETING):
        with open(os.path.join(repo, path), "a", encoding="utf-8") as handle:
            handle.write("\nReviewed.\n")
    _git(repo, "commit", "--quiet", "-am", "Tidied two notes")
    _git(repo, "push", "--quiet", "origin", "main")
    record_change(conn, store, repo=repo, trigger="garden", actor="librarian",
                  parent_commit_sha=parent, commit_sha=_git(repo, "rev-parse", "HEAD"),
                  summary="Tidied two notes")

    # Every kind of ref that can hold the layer: another branch, an annotated tag, a pull ref.
    first = deal["commit_sha"]
    _git(repo, "tag", "-a", "v1", "-m", "first release", first)
    _git(repo, "push", "--quiet", "origin", f"{first}:refs/heads/archive", "refs/tags/v1")
    _git(bare, "update-ref", "refs/pull/7/head", first)

    stalled, _ = _submit(conn, store, actor=BOB, layer=ERASED, key="stalled",
                         text=f"Scanned minutes {MARKER}.")
    ocr = store.put(f"Readable text of the scan {MARKER}".encode())

    build.rebuild(conn, repo, _embedder())
    erased_ids = [str(deal["id"]), str(figures["id"]), str(stalled["id"])]
    with conn.cursor() as cursor:
        cursor.execute("UPDATE capture_queue SET status = 'failed', extraction = %s WHERE id = %s",
                       (Jsonb({"artifacts": [{"readable_ref": ocr}]}), stalled["id"]))
        cursor.execute("INSERT INTO graph_measures (indexed_commit, scope, measures) "
                       "VALUES (%s, 'public', '{}'), (%s, %s, '{}')",
                       (tessera["commit_sha"], tessera["commit_sha"], ERASED))
        cursor.execute(
            "INSERT INTO audit_log (identity, tool, args, duration_ms, outcome, result) "
            "VALUES ('bob', 'read_page', %s, 1, 'ok', %s)",
            (Jsonb({"path": "wiki/notes/Quokka deal.md"}),
             Jsonb({"title": "Quokka deal", "excerpt": f"{MARKER}: the deal"})))
        cursor.execute(
            "INSERT INTO audit_log (identity, tool, args, duration_ms, outcome, result) "
            "VALUES ('alice', 'search_brain', %s, 1, 'ok', %s)",
            (Jsonb({"query": "pilot"}), Jsonb({"hits": [MEETING.split("/", 2)[2]]})))
        cursor.execute(
            "INSERT INTO admin_actions (actor, action, args, outcome) VALUES ('morgan', 'capture.retry', "
            "%s, 'ok')", (Jsonb({"capture": erased_ids[0]}),))
        cursor.execute(
            "INSERT INTO job_runs (id, job, status, stats) VALUES (gen_random_uuid(), 'garden', "
            "'succeeded', %s)", (Jsonb({"findings": [DEAL]}),))
    return Corpus(conn, repo, bare, store, erased_ids, [str(tessera["id"]), str(shared["id"])],
                  first)


def _table(cursor, name: str) -> bool:
    cursor.execute("SELECT to_regclass(%s) IS NOT NULL", (name,))
    return bool(cursor.fetchone()[0])


def _run(corpus):
    with corpus.conn.cursor() as cursor:                  # skip the backoff
        cursor.execute("UPDATE layer_erasures SET next_attempt_at = now()")
    return erase_layer.run_next(corpus.conn, repo=corpus.repo, branch="main", evidence=corpus.store,
                                embedder_factory=_embedder, on_output=lambda _line: None)


def _erase(corpus):
    erasure.request(corpus.conn, ERASED, actor="morgan")
    return _run(corpus)


def _land_kept(corpus, key: str) -> dict:
    return _file(corpus.conn, corpus.repo, corpus.store, actor=ALICE, layer=KEPT, key=key,
                 text=f"Tessera follow-up {key}.")


def _kept_state(corpus) -> dict:
    """What a reader of the kept layer sees, and the kept layer's history, minus commit ids."""
    conn, repo = corpus.conn, corpus.repo
    with conn.cursor() as cursor:
        cursor.execute("SELECT path, content_hash, type, acl, links, inlinks, aliases FROM pages_index "
                       "WHERE path NOT LIKE %s ORDER BY path", (f"layers/{ERASED}/%",))
        index_rows = cursor.fetchall()
        cursor.execute("SELECT original_ref, readable_ref FROM capture_artifacts "
                       "WHERE capture_id = ANY(%s::uuid[]) ORDER BY 1", (corpus.kept_captures,))
        originals = cursor.fetchall()
    reader = BrainService(SimpleNamespace(), conn, _embedder(), audiences={KEPT})
    tree = {path: _git(repo, "show", f"main:{path}")
            for path in _git(repo, "ls-tree", "-r", "--name-only", "main").splitlines()
            if not path.startswith(f"layers/{ERASED}/")}
    return {
        "tree": tree,
        "history": _git(repo, "log", "-p", "--format=%s", "main", "--", f"layers/{KEPT}", "wiki"),
        "index": index_rows,
        "originals": originals,
        "stored": all(corpus.store.exists(ref) for pair in originals for ref in pair if ref),
        "hits": [hit["path"] for hit in reader.search("Tessera pilot")["hits"]],
    }


def _assert_nothing_left(corpus, *also: str) -> None:
    """ER-01: no reachable commit or blob, no row, and no stored object names the layer."""
    needles = [MARKER, *corpus.erased_captures, *also]
    clone = corpus.bare + "-clone"
    _git(os.path.dirname(corpus.bare), "clone", "--bare", "--quiet", corpus.bare, clone)
    for repo, listing in ((clone, ("rev-list", "--objects", "--all")),
                          (corpus.repo, None)):
        if listing:
            objects = "\n".join(line.split()[0] for line in _git(repo, *listing).splitlines())
            dump = subprocess.run(["git", "cat-file", "--batch"], cwd=repo, input=objects.encode(),
                                  capture_output=True, check=True).stdout
        else:          # the writer's checkout: every object it still stores, reachable or not
            dump = subprocess.run(["git", "cat-file", "--batch-all-objects", "--batch"], cwd=repo,
                                  capture_output=True, check=True).stdout
        for needle in [*needles, f"layers/{ERASED}/"]:
            assert needle.encode() not in dump, (repo, needle)
    with corpus.conn.cursor() as cursor:
        cursor.execute("SELECT tablename FROM pg_tables WHERE schemaname = current_schema()")
        tables = [row[0] for row in cursor.fetchall()]
        for table in tables:
            cursor.execute(f'SELECT count(*) FROM "{table}" t WHERE t::text LIKE ANY(%s)',
                           ([f"%{needle}%" for needle in [*needles, f"layers/{ERASED}/"]],))
            assert cursor.fetchone()[0] == 0, table
    for key, data in corpus.store.objects.items():
        text = gzip.decompress(data) if data[:2] == b"\x1f\x8b" else data
        for needle in needles:
            assert needle.encode() not in text, key


def test_erasing_a_layer_leaves_nothing_of_it_and_keeps_every_other_layer(corpus):
    conn = corpus.conn
    with conn.cursor() as cursor:
        cursor.execute("SELECT original_ref FROM capture_artifacts WHERE capture_id = ANY(%s::uuid[]) "
                       "INTERSECT SELECT original_ref FROM capture_artifacts "
                       "WHERE capture_id = ANY(%s::uuid[])",
                       (corpus.erased_captures, corpus.kept_captures))
        shared = [row[0] for row in cursor.fetchall()]
    assert shared, "the fixture must share one original between the layers"
    before = _kept_state(corpus)
    assert any(path.endswith("Tessera meeting.md") for path in before["hits"])
    late, _ = _submit(conn, corpus.store, actor=BOB, layer=ERASED, key="late",
                      text=f"A late finance note {MARKER}.")
    # Queued in the kept layer, byte-identical to an original of the erased layer.
    waiting, waiting_source = _submit(conn, corpus.store, actor=ALICE, layer=KEPT, key="late-kept",
                                      text=SHARED_TEXT)
    assert waiting["request"]["artifacts"][0]["blob_ref"] == shared[0]

    erasure.request(conn, ERASED, actor="morgan")
    with pytest.raises(LayerErasing):                       # ER-03: the layer is closed
        _submit(conn, corpus.store, actor=BOB, layer=ERASED, key="refused", text="Too late.")
    with conn.transaction(force_rollback=True):             # ER-03: its queued work is not claimed
        assert str(queue.claim_next(conn, operation_budget_s=60)["id"]) == str(waiting["id"])
    done = _run(corpus)

    assert done["status"] == erasure.SUCCEEDED, done["error"]
    receipt = done["receipt"]
    assert receipt["captures_deleted"] == 4 and receipt["changes_rewritten"] == 1
    assert receipt["commits_removed"] >= 2 and receipt["objects_deleted"] >= 3
    assert receipt["remaining"][0]["first_changed_commits"]
    head = _git(corpus.bare, "rev-parse", "main")
    assert receipt["new_head"] == head == _git(corpus.repo, "rev-parse", "HEAD")
    assert _git(corpus.bare, "rev-parse", "archive") != corpus.first_erased_commit
    assert corpus.store.exists(shared[0])
    with conn.cursor() as cursor:
        cursor.execute("SELECT status FROM capture_queue WHERE id = %s", (waiting["id"],))
        assert cursor.fetchone() == ("queued",)             # ER-03: the kept layer's capture waits
        cursor.execute("SELECT action, args->>'new_head' FROM admin_actions "
                       "WHERE action = 'layer.erased'")
        assert cursor.fetchall() == [("layer.erased", head)]
        cursor.execute("SELECT count(*) FROM audit_log WHERE result::text LIKE '%Tessera meeting%'")
        assert cursor.fetchone()[0] == 1                    # the kept layer's log row stays
        cursor.execute("SELECT scope, indexed_commit FROM graph_measures ORDER BY id")
        snapshots = cursor.fetchall()
    assert ERASED not in {scope for scope, _ in snapshots}
    assert {scope for scope, commit in snapshots if commit == head} == {"brain", "public", KEPT}
    earlier = [(scope, commit) for scope, commit in snapshots if commit != head]
    assert [scope for scope, _ in earlier] == ["public"]    # the earlier series stays, remapped
    assert _git(corpus.repo, "cat-file", "-t", earlier[0][1]) == "commit"

    _assert_nothing_left(corpus, str(late["id"]))           # ER-01
    assert _kept_state(corpus) == before                    # ER-02

    health = index_health.read(conn)                        # ER-06
    assert health["healthy"] and not health["dirty"]
    assert health["indexed_commit_sha"] == health["repository_head_sha"] == head
    _assert_changes_resolve(corpus)

    landed = _process(conn, corpus.repo, corpus.store, source=waiting_source)   # ER-03: then lands
    assert str(landed["id"]) == str(waiting["id"])
    assert _git(corpus.bare, "rev-parse", "main") == landed["commit_sha"]


def _assert_changes_resolve(corpus) -> None:
    with corpus.conn.cursor() as cursor:
        cursor.execute("SELECT commit_sha, parent_commit_sha FROM knowledge_changes")
        changes = cursor.fetchall()
    assert changes
    for commit, parent in changes:
        assert _git(corpus.repo, "cat-file", "-t", commit) == "commit"
        assert _git(corpus.repo, "rev-parse", f"{commit}^") == parent


def test_a_refused_push_changes_nothing_and_a_retry_resumes_the_same_rewrite(corpus):
    conn = corpus.conn
    hook = os.path.join(corpus.bare, "hooks", "pre-receive")
    with open(hook, "w", encoding="utf-8") as handle:
        handle.write("#!/bin/sh\nexit 1\n")
    os.chmod(hook, 0o755)
    refs = _git(corpus.bare, "for-each-ref")
    objects = dict(corpus.store.objects)
    with conn.cursor() as cursor:
        cursor.execute("SELECT id, commit_sha FROM knowledge_changes ORDER BY id")
        changes = cursor.fetchall()

    ended = _erase(corpus)
    for attempt in range(2, erasure.MAX_ATTEMPTS + 1):
        assert ended["status"] == erasure.QUEUED and "not pushed" in ended["error"]
        ended = _run(corpus)
        assert ended["attempts"] == attempt
    assert ended["status"] == erasure.FAILED and not ended["pushed"]
    assert _git(corpus.bare, "for-each-ref") == refs                    # ER-04
    assert corpus.store.objects == objects
    with conn.cursor() as cursor:
        cursor.execute("SELECT id, commit_sha FROM knowledge_changes ORDER BY id")
        assert cursor.fetchall() == changes
        cursor.execute("SELECT count(*) FROM capture_queue WHERE id = ANY(%s::uuid[])",
                       (corpus.erased_captures,))
        assert cursor.fetchone()[0] == len(corpus.erased_captures)
    assert not erasure.is_erasing(conn, ERASED)

    os.remove(hook)
    again = erasure.request(conn, ERASED, actor="morgan")   # nothing was pushed: it starts afresh
    assert again["id"] != ended["id"] and again["status"] == erasure.QUEUED
    done = _run(corpus)
    assert done["status"] == erasure.SUCCEEDED, done["error"]
    _assert_nothing_left(corpus)


def test_a_ref_moved_during_the_rewrite_is_never_overwritten(corpus, monkeypatch):
    real = erase_layer._filter
    concurrent = {}

    def while_filtering(repo, folder):
        real(repo, folder)
        if not concurrent:                       # another writer pushes during the first rewrite
            clone = corpus.bare + "-concurrent"
            _git(os.path.dirname(corpus.bare), "clone", "--quiet", corpus.bare, clone)
            with open(os.path.join(clone, "wiki-note.md"), "w", encoding="utf-8") as handle:
                handle.write("concurrent\n")
            _git(clone, "add", ".")
            _git(clone, "commit", "--quiet", "-m", "concurrent")
            _git(clone, "push", "--quiet", "origin", "main")
            concurrent["head"] = _git(clone, "rev-parse", "HEAD")
    monkeypatch.setattr(erase_layer, "_filter", while_filtering)

    stopped = _erase(corpus)

    assert stopped["status"] == erasure.QUEUED and "not pushed" in stopped["error"]
    assert _git(corpus.bare, "rev-parse", "main") == concurrent["head"]          # ER-04
    done = _run(corpus)
    assert done["status"] == erasure.SUCCEEDED, done["error"]
    assert _git(corpus.bare, "merge-base", "--is-ancestor", done["new_head"], "main") == ""
    assert _git(corpus.bare, "log", "-1", "--format=%s", "main") == "concurrent"
    _assert_nothing_left(corpus)


@pytest.mark.parametrize("stop", ["before the push was recorded", "inside the row transaction",
                                  "before the objects were deleted"])
def test_a_job_stopped_after_the_push_completes_when_run_again(corpus, monkeypatch, stop):
    target, attribute = {
        "before the push was recorded": (erasure, "mark_pushed"),
        "inside the row transaction": (erase_layer, "_rewrite_ledger"),
        "before the objects were deleted": (erase_layer, "_collect_garbage"),
    }[stop]
    real, calls = getattr(target, attribute), []

    def stops_once(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("the writer stopped")
        return real(*args, **kwargs)
    monkeypatch.setattr(target, attribute, stops_once)

    stopped = _erase(corpus)
    assert stopped["status"] == erasure.QUEUED and stopped["new_head"]
    assert _git(corpus.bare, "rev-parse", "main") == stopped["new_head"]
    assert stopped["pushed"] is (stop != "before the push was recorded")
    assert erasure.is_erasing(corpus.conn, ERASED)
    later = _land_kept(corpus, "between")         # other layers keep filing on the rewritten history

    done = _run(corpus)
    assert done["status"] == erasure.SUCCEEDED, done["error"]
    assert done["receipt"]["new_head"] == stopped["new_head"]
    assert _git(corpus.bare, "rev-parse", "main") == later["commit_sha"]
    _assert_nothing_left(corpus)
    _assert_changes_resolve(corpus)


def test_after_the_push_a_request_never_fails_and_backs_off(corpus, monkeypatch):
    def broken(*_args, **_kwargs):
        raise RuntimeError("the index is unavailable")
    monkeypatch.setattr(erase_layer, "_rebuild_index", broken)

    ended = _erase(corpus)
    for _ in range(erasure.MAX_ATTEMPTS):
        assert ended["status"] == erasure.QUEUED and ended["pushed"]
        assert ended["next_attempt_at"] > ended["started_at"]
        assert not erasure.pending(corpus.conn)            # it waits out the backoff
        ended = _run(corpus)
    assert ended["status"] == erasure.QUEUED and ended["attempts"] == erasure.MAX_ATTEMPTS + 1
    assert erasure.is_erasing(corpus.conn, ERASED)

    monkeypatch.undo()
    done = _run(corpus)
    assert done["status"] == erasure.SUCCEEDED, done["error"]
    _assert_nothing_left(corpus)


def test_a_push_that_landed_unrecorded_never_fails_the_request(corpus, monkeypatch):
    def unrecorded(*_args, **_kwargs):
        raise RuntimeError("the writer stopped before recording the push")
    monkeypatch.setattr(erasure, "mark_pushed", unrecorded)

    ended = _erase(corpus)
    for _ in range(erasure.MAX_ATTEMPTS):
        ended = _run(corpus)

    assert ended["status"] == erasure.QUEUED and not ended["pushed"]
    assert ended["attempts"] == erasure.MAX_ATTEMPTS + 1
    assert erasure.is_erasing(corpus.conn, ERASED)          # the layer never reopens
    monkeypatch.undo()
    done = _run(corpus)
    assert done["status"] == erasure.SUCCEEDED, done["error"]
    _assert_nothing_left(corpus)
    _assert_changes_resolve(corpus)


def test_a_second_erasure_waits_for_a_pushed_one_to_finish(corpus, monkeypatch):
    def broken(*_args, **_kwargs):
        raise RuntimeError("the index is unavailable")
    monkeypatch.setattr(erase_layer, "_rebuild_index", broken)
    first = _erase(corpus)
    assert first["pushed"] and first["status"] == erasure.QUEUED
    second = erasure.request(corpus.conn, KEPT, actor="morgan")
    with corpus.conn.cursor() as cursor:
        cursor.execute("UPDATE layer_erasures SET next_attempt_at = now() WHERE id = %s",
                       (second["id"],))

    assert not erasure.pending(corpus.conn) and erasure.claim(corpus.conn) is None
    monkeypatch.undo()
    assert _run(corpus)["layer"] == ERASED
    assert erasure.latest(corpus.conn, ERASED)["status"] == erasure.SUCCEEDED
    assert erasure.claim(corpus.conn)["id"] == second["id"]


def test_a_second_erasure_waits_for_a_push_that_may_have_landed(corpus, monkeypatch):
    real, calls = erasure.mark_pushed, []

    def unrecorded_once(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("the writer stopped before recording the push")
        return real(*args, **kwargs)
    monkeypatch.setattr(erasure, "mark_pushed", unrecorded_once)
    first = _erase(corpus)
    assert not first["pushed"] and first["new_head"]
    second = erasure.request(corpus.conn, KEPT, actor="morgan")
    with corpus.conn.cursor() as cursor:
        cursor.execute("UPDATE layer_erasures SET next_attempt_at = now() WHERE id = %s",
                       (second["id"],))

    assert erasure.claim(corpus.conn) is None
    assert _run(corpus)["status"] == erasure.SUCCEEDED
    _assert_changes_resolve(corpus)
    assert erasure.claim(corpus.conn)["id"] == second["id"]


def test_an_object_stored_again_before_it_is_collected_is_kept(corpus, monkeypatch):
    churn = "Quarterly churn stayed under two percent."
    _submit(corpus.conn, corpus.store, actor=BOB, layer=ERASED, key="churn", text=churn)
    real, calls = erase_layer._collect_garbage, []

    def stops_once(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("the writer stopped")
        return real(*args, **kwargs)
    monkeypatch.setattr(erase_layer, "_collect_garbage", stops_once)
    _erase(corpus)
    kept = _file(corpus.conn, corpus.repo, corpus.store, actor=ALICE, layer=KEPT, key="churn-kept",
                 text=churn)

    done = _run(corpus)

    assert done["status"] == erasure.SUCCEEDED, done["error"]
    with corpus.conn.cursor() as cursor:
        cursor.execute("SELECT original_ref FROM capture_artifacts WHERE capture_id = %s",
                       (kept["id"],))
        assert corpus.store.exists(cursor.fetchone()[0])                         # ER-02


def test_a_request_the_writer_keeps_dying_on_before_its_push_is_given_up(corpus):
    item = erasure.request(corpus.conn, ERASED, actor="morgan")
    with corpus.conn.cursor() as cursor:
        cursor.execute("UPDATE layer_erasures SET status = 'running', attempts = %s, "
                       "next_attempt_at = now() WHERE id = %s", (erasure.MAX_ATTEMPTS, item["id"]))

    assert erasure.claim(corpus.conn) is None
    assert erasure.latest(corpus.conn, ERASED)["status"] == erasure.FAILED
    assert not erasure.is_erasing(corpus.conn, ERASED)


def test_the_forced_push_reaching_the_webhook_during_the_rebuild_does_not_fail_the_job(corpus,
                                                                                       monkeypatch):
    real, calls = build.rebuild, []

    def moved_once(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise StigmergyIndexError("index health changed during the rebuild")
        return real(*args, **kwargs)
    monkeypatch.setattr(build, "rebuild", moved_once)

    done = _erase(corpus)

    assert done["status"] == erasure.SUCCEEDED and done["attempts"] == 1 and len(calls) == 2
