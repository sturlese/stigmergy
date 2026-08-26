import shutil
import subprocess
import time
from datetime import date
from pathlib import Path

import pytest

from stigmergy.index import build, health, search, store
from stigmergy.index.backends.embedder import build_embedder
from stigmergy.index.errors import EmptyIndexError, StigmergyIndexError
from stigmergy.server import webhook
from tests import testdb

FIXTURE = str(Path(__file__).parent / "fixtures" / "repo")
FIXTURE_PAGES = 10
GLOBEX_ID = "ent_30000000-0000-4000-8000-000000000001"
INITECH_ID = "ent_30000000-0000-4000-8000-000000000002"


def _connect_or_skip():
    return testdb.connect_or_skip("index")


def _commit_repo(repo, message):
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Index Test",
            "-c",
            "user.email=index@example.invalid",
            "commit",
            "-qm",
            message,
        ],
        cwd=repo,
        check=True,
    )
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def _git_fixture(tmp_path):
    repo = tmp_path / "brain"
    shutil.copytree(FIXTURE, repo)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    return repo, _commit_repo(repo, "fixture")


def _page(page_id, title, body):
    return (
        "---\n"
        f"id: {page_id}\n"
        "type: note\n"
        f"title: {title}\n"
        "status: developing\n"
        "created: '2026-08-01'\n"
        "updated: '2026-08-24'\n"
        "acl: null\n"
        "entity: []\n"
        "sources: []\n"
        "---\n\n"
        f"# {title}\n\n{body}\n"
    )


class _CallbackEmbedder:
    model = "fake-hashed-bow-256"
    host = ""

    def __init__(self, callback):
        self.callback = callback
        self.delegate = build_embedder("fake")
        self.fired = False

    def embed(self, texts):
        if not self.fired:
            self.fired = True
            self.callback()
        return self.delegate.embed(texts)


def _configured_upstream_checkout(tmp_path):
    upstream = tmp_path / "upstream.git"
    subprocess.run(["git", "init", "--bare", "-q", "-b", "release", str(upstream)], check=True)
    seed, _head = _git_fixture(tmp_path)
    subprocess.run(["git", "remote", "add", "deploy", str(upstream)], cwd=seed, check=True)
    subprocess.run(["git", "push", "-q", "-u", "deploy", "main:release"], cwd=seed, check=True)
    checkout = tmp_path / "staging"
    subprocess.run(["git", "clone", "-q", str(upstream), str(checkout)], check=True)
    subprocess.run(["git", "branch", "-m", "main"], cwd=checkout, check=True)
    subprocess.run(["git", "remote", "rename", "origin", "deploy"], cwd=checkout, check=True)
    assert subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"],
        cwd=checkout,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip() == "deploy/release"
    return seed, checkout


@pytest.fixture(scope="module")
def conn():
    connection = _connect_or_skip()
    stats = build.rebuild(connection, FIXTURE, build_embedder("fake"))
    assert stats["pages"] == FIXTURE_PAGES
    yield connection
    connection.close()


def test_rebuild_populates_only_wiki_and_sources(conn):
    assert store.page_count(conn) == FIXTURE_PAGES
    with conn.cursor() as cursor:
        cursor.execute("SELECT zone, count(*) FROM pages_index GROUP BY zone ORDER BY zone")
        assert dict(cursor.fetchall()) == {"sources": 6, "wiki": 4}


def test_acl_links_and_hashes_are_stored(conn):
    with conn.cursor() as cursor:
        cursor.execute(
            "SELECT acl, inlinks, links, content_hash FROM pages_index "
            "WHERE path = 'wiki/concepts/Support refunds.md'"
        )
        acl, inlinks, links, content_hash = cursor.fetchone()
    assert acl is None
    assert inlinks == 1
    assert links == []
    assert content_hash.startswith("sha256:")


def test_lexical_and_vector_indexes_exist(conn):
    with conn.cursor() as cursor:
        cursor.execute("SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'pages_index'")
        indexes = {name: definition.lower() for name, definition in cursor.fetchall()}
    assert "using gin" in indexes["pages_index_tsv_gin"]
    assert "using hnsw" in indexes["pages_index_embedding_hnsw"]
    assert "halfvec_cosine_ops" in indexes["pages_index_embedding_hnsw"]
    assert "links" in indexes["pages_index_links_gin"]


def test_schema_supports_the_production_embedding_dimension(conn):
    class Rollback(Exception):
        pass

    with pytest.raises(Rollback), conn.transaction():
        store.init_schema(conn, dim=3072, model="text-embedding-3-large", fts_config="english")
        store.create_search_indexes(conn)
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT format_type(atttypid, atttypmod) FROM pg_attribute "
                "WHERE attrelid = 'pages_index'::regclass AND attname = 'embedding'"
            )
            assert cursor.fetchone()[0] == "halfvec(3072)"
        raise Rollback()


def test_hybrid_search_returns_explainable_hits(conn):
    hits = search.search(
        conn,
        "How did Globex quarterly revenue perform?",
        today=date(2026, 8, 24),
    )
    assert hits
    assert all(hit["score"] > 0 and hit["arms"] and "snippet" in hit for hit in hits)


def test_filters_apply_to_both_search_arms(conn):
    hits = search.search(conn, "renewal coordination", k=10, filters={"entity": GLOBEX_ID})
    assert hits
    assert all(GLOBEX_ID in hit["entity"] for hit in hits)
    with pytest.raises(ValueError, match="unknown filter"):
        search.search(conn, "query", filters={"body": "value"})


def test_acl_filters_both_rankings_before_candidate_fusion(conn):
    hidden = "wiki/notes/Globex and Initech renewal.md"

    engineering = search.search_arms(
        conn,
        "renewal coordination",
        k=10,
        audiences={"engineering"},
    )
    finance = search.search_arms(
        conn,
        "renewal coordination",
        k=10,
        audiences={"finance"},
    )

    assert hidden not in engineering["fts"]
    assert hidden not in engineering["vec"]
    assert hidden in set(finance["fts"]) | set(finance["vec"])


def test_multi_entity_page_is_found_by_either_anchor(conn):
    expected = "wiki/notes/Globex and Initech renewal.md"
    for entity_id in (GLOBEX_ID, INITECH_ID):
        hits = search.search(conn, "renewal coordination", k=10, filters={"entity": entity_id})
        assert expected in [hit["path"] for hit in hits]


def test_entity_ids_are_part_of_the_lexical_document(conn):
    hits = search.search(conn, INITECH_ID, k=10)
    assert "wiki/notes/Globex and Initech renewal.md" in [hit["path"] for hit in hits]


def test_url_text_does_not_break_full_text_search(conn):
    ranking = search.fts_ranking(
        conn,
        "see https://example.com/globex/report.pdf",
        store.read_meta(conn)["fts_config"],
    )
    assert isinstance(ranking, list)


def test_unchanged_rebuild_reuses_embedding_cache(conn):
    before = search.search(conn, "refund annual plans", k=10)
    stats = build.rebuild(conn, FIXTURE, build_embedder("fake"))
    assert stats["embedded"] == 0
    assert stats["cached"] == FIXTURE_PAGES
    after = search.search(conn, "refund annual plans", k=10)
    assert [(hit["path"], hit["score"]) for hit in before] == [
        (hit["path"], hit["score"]) for hit in after
    ]


def test_rebuild_removes_deleted_pages_from_search(conn, tmp_path):
    repo = tmp_path / "brain"
    shutil.copytree(FIXTURE, repo)
    victim = "wiki/notes/Globex and Initech renewal.md"
    embedder = build_embedder("fake")
    try:
        build.rebuild(conn, str(repo), embedder)
        assert victim in {
            hit["path"] for hit in search.search(conn, "Globex Initech renewal", k=20)
        }

        (repo / victim).unlink()
        build.rebuild(conn, str(repo), embedder)

        assert victim not in store.existing_paths(conn)
        assert victim not in {
            hit["path"] for hit in search.search(conn, "Globex Initech renewal", k=20)
        }
    finally:
        build.rebuild(conn, FIXTURE, embedder)


def test_rebuild_cli(capsys, tmp_path):
    from stigmergy.index import cli

    repo = tmp_path / "brain"
    shutil.copytree(FIXTURE, repo)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Index Test",
            "-c",
            "user.email=index@example.invalid",
            "commit",
            "-qm",
            "test fixture",
        ],
        cwd=repo,
        check=True,
    )

    cli.index_main(["--rebuild", "--repo", str(repo), "--embedder", "fake"])
    assert f"indexed {FIXTURE_PAGES} pages" in capsys.readouterr().out


def test_empty_index_fails_loudly_and_can_be_rebuilt(conn):
    with conn.cursor() as cursor:
        cursor.execute("DROP TABLE IF EXISTS index_meta")
    with pytest.raises(EmptyIndexError):
        search.search(conn, "anything")
    build.rebuild(conn, FIXTURE, build_embedder("fake"))


class _WrongHostEmbedder:
    host = "https://other.example/v1"
    model = "fake-hashed-bow-256"

    def embed(self, _texts):
        raise AssertionError("host mismatch must be checked before embedding")


def test_embedding_host_mismatch_fails_before_query_embedding(conn):
    build.rebuild(conn, FIXTURE, build_embedder("fake"))
    with pytest.raises(StigmergyIndexError, match="not provably the same vector space"):
        search.search_arms(conn, "globex", embedder=_WrongHostEmbedder())


def test_committed_snapshot_within_the_input_bounds_rebuilds(conn, tmp_path):
    repo, _head = _git_fixture(tmp_path)
    try:
        stats = build.rebuild(conn, str(repo), build_embedder("fake"), require_repository_head=True)
        assert stats["pages"] == FIXTURE_PAGES
    finally:
        build.rebuild(conn, FIXTURE, build_embedder("fake"))


def test_rebuild_refuses_a_clean_checkout_behind_its_remote_before_replacing_the_index(conn, tmp_path):
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-q", "-b", "main", str(origin)], check=True)
    seed, initial_head = _git_fixture(tmp_path)
    subprocess.run(["git", "remote", "add", "origin", str(origin)], cwd=seed, check=True)
    subprocess.run(["git", "push", "-q", "-u", "origin", "main"], cwd=seed, check=True)

    checkout = tmp_path / "staging"
    subprocess.run(["git", "clone", "-q", str(origin), str(checkout)], check=True)
    (seed / "wiki" / "notes" / "Remote newer.md").write_text(
        _page("page_remote_newer", "Remote newer", "Only the remote checkout has this page.")
    )
    remote_head = _commit_repo(seed, "remote newer page")
    subprocess.run(["git", "push", "-q", "origin", "main"], cwd=seed, check=True)
    subprocess.run(["git", "fetch", "-q", "origin", "main"], cwd=checkout, check=True)
    assert subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=checkout, check=True, capture_output=True, text=True
    ).stdout.strip() == initial_head
    assert subprocess.run(
        ["git", "rev-parse", "origin/main"], cwd=checkout, check=True, capture_output=True, text=True
    ).stdout.strip() == remote_head

    previous_paths = store.existing_paths(conn)
    previous_health = health.read(conn)
    try:
        with pytest.raises(StigmergyIndexError, match="behind.*origin/main"):
            build.rebuild(conn, str(checkout), build_embedder("fake"), require_repository_head=True)
        assert store.existing_paths(conn) == previous_paths
        assert health.read(conn) == previous_health
    finally:
        build.rebuild(conn, FIXTURE, build_embedder("fake"))


def test_rebuild_refuses_a_clean_checkout_behind_its_configured_non_origin_upstream(conn, tmp_path):
    seed, checkout = _configured_upstream_checkout(tmp_path)
    (seed / "wiki" / "notes" / "Upstream newer.md").write_text(
        _page("page_upstream_newer", "Upstream newer", "Only the configured upstream has this page.")
    )
    _commit_repo(seed, "upstream newer page")
    subprocess.run(["git", "push", "-q", "deploy", "main:release"], cwd=seed, check=True)
    subprocess.run(["git", "fetch", "-q", "deploy", "release"], cwd=checkout, check=True)

    previous_paths = store.existing_paths(conn)
    previous_health = health.read(conn)
    try:
        with pytest.raises(StigmergyIndexError, match="behind.*deploy/release"):
            build.rebuild(conn, str(checkout), build_embedder("fake"), require_repository_head=True)
        assert store.existing_paths(conn) == previous_paths
        assert health.read(conn) == previous_health
    finally:
        build.rebuild(conn, FIXTURE, build_embedder("fake"))


def test_rebuild_refuses_an_unresolved_configured_upstream_before_replacing_the_index(conn, tmp_path):
    repo, _head = _git_fixture(tmp_path)
    subprocess.run(["git", "config", "branch.main.remote", "deploy"], cwd=repo, check=True)
    subprocess.run(["git", "config", "branch.main.merge", "refs/heads/release"], cwd=repo, check=True)

    previous_paths = store.existing_paths(conn)
    previous_health = health.read(conn)
    try:
        with pytest.raises(StigmergyIndexError, match="configured repository upstream cannot be resolved"):
            build.rebuild(conn, str(repo), build_embedder("fake"), require_repository_head=True)
        assert store.existing_paths(conn) == previous_paths
        assert health.read(conn) == previous_health
    finally:
        build.rebuild(conn, FIXTURE, build_embedder("fake"))


def test_rebuild_refuses_when_its_remote_tracking_tip_moves_during_embedding(conn, tmp_path):
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-q", "-b", "main", str(origin)], check=True)
    seed, _head = _git_fixture(tmp_path)
    (seed / "wiki" / "notes" / "Embedding baseline.md").write_text(
        _page("page_embedding_baseline", "Embedding baseline", "This page triggers embedding.")
    )
    _commit_repo(seed, "embedding baseline")
    subprocess.run(["git", "remote", "add", "origin", str(origin)], cwd=seed, check=True)
    subprocess.run(["git", "push", "-q", "-u", "origin", "main"], cwd=seed, check=True)
    checkout = tmp_path / "staging"
    subprocess.run(["git", "clone", "-q", str(origin), str(checkout)], check=True)

    def advance_remote_tip():
        (seed / "wiki" / "notes" / "Remote race.md").write_text(
            _page("page_remote_race", "Remote race", "The remote advanced during embedding.")
        )
        _commit_repo(seed, "remote race")
        subprocess.run(["git", "push", "-q", "origin", "main"], cwd=seed, check=True)
        subprocess.run(["git", "fetch", "-q", "origin", "main"], cwd=checkout, check=True)

    previous_paths = store.existing_paths(conn)
    previous_health = health.read(conn)
    try:
        with pytest.raises(StigmergyIndexError, match="behind.*origin/main"):
            build.rebuild(
                conn,
                str(checkout),
                _CallbackEmbedder(advance_remote_tip),
                require_repository_head=True,
            )
        assert store.existing_paths(conn) == previous_paths
        assert health.read(conn) == previous_health
    finally:
        build.rebuild(conn, FIXTURE, build_embedder("fake"))


def test_rebuild_refuses_when_its_configured_non_origin_upstream_moves_during_embedding(
    conn, tmp_path
):
    seed, checkout = _configured_upstream_checkout(tmp_path)
    (seed / "wiki" / "notes" / "Embedding baseline.md").write_text(
        _page("page_embedding_baseline", "Embedding baseline", "This page triggers embedding.")
    )
    _commit_repo(seed, "embedding baseline")
    subprocess.run(["git", "push", "-q", "deploy", "main:release"], cwd=seed, check=True)
    subprocess.run(["git", "fetch", "-q", "deploy", "release"], cwd=checkout, check=True)
    subprocess.run(["git", "merge", "--ff-only", "deploy/release"], cwd=checkout, check=True)

    def advance_upstream_tip():
        (seed / "wiki" / "notes" / "Upstream race.md").write_text(
            _page("page_upstream_race", "Upstream race", "The upstream advanced during embedding.")
        )
        _commit_repo(seed, "upstream race")
        subprocess.run(["git", "push", "-q", "deploy", "main:release"], cwd=seed, check=True)
        subprocess.run(["git", "fetch", "-q", "deploy", "release"], cwd=checkout, check=True)

    previous_paths = store.existing_paths(conn)
    previous_health = health.read(conn)
    try:
        with pytest.raises(StigmergyIndexError, match="behind.*deploy/release"):
            build.rebuild(
                conn,
                str(checkout),
                _CallbackEmbedder(advance_upstream_tip),
                require_repository_head=True,
            )
        assert store.existing_paths(conn) == previous_paths
        assert health.read(conn) == previous_health
    finally:
        build.rebuild(conn, FIXTURE, build_embedder("fake"))


def test_rebuild_refuses_when_local_head_moves_during_embedding(conn, tmp_path):
    repo, _head = _git_fixture(tmp_path)
    candidate = repo / "wiki" / "notes" / "Embedding baseline.md"
    candidate.write_text(_page("page_embedding_baseline", "Embedding baseline", "This page triggers embedding."))
    _commit_repo(repo, "embedding baseline")

    def advance_local_head():
        candidate.write_text(_page("page_embedding_baseline", "Embedding baseline", "The local HEAD advanced."))
        _commit_repo(repo, "local race")

    previous_paths = store.existing_paths(conn)
    previous_health = health.read(conn)
    try:
        with pytest.raises(StigmergyIndexError, match="HEAD changed during the rebuild"):
            build.rebuild(
                conn,
                str(repo),
                _CallbackEmbedder(advance_local_head),
                require_repository_head=True,
            )
        assert store.existing_paths(conn) == previous_paths
        assert health.read(conn) == previous_health
    finally:
        build.rebuild(conn, FIXTURE, build_embedder("fake"))


def test_committed_snapshot_rejects_an_oversized_indexable_blob_before_replacement(
    conn, tmp_path, monkeypatch
):
    repo, _head = _git_fixture(tmp_path)
    victim = "wiki/notes/Oversized.md"
    monkeypatch.setattr(build, "MAX_INDEXABLE_MARKDOWN_BYTES", 1_024)
    (repo / victim).write_text(
        _page(
            "page_oversized_blob",
            "Oversized",
            "x" * 1_024,
        )
    )
    _commit_repo(repo, "oversized page")
    previous_paths = store.existing_paths(conn)
    try:
        with pytest.raises(StigmergyIndexError, match="size limit"):
            build.rebuild(conn, str(repo), build_embedder("fake"), require_repository_head=True)
        assert store.existing_paths(conn) == previous_paths
    finally:
        build.rebuild(conn, FIXTURE, build_embedder("fake"))


def test_committed_snapshot_rejects_an_oversized_aggregate_before_replacement(
    conn, tmp_path, monkeypatch
):
    repo, _head = _git_fixture(tmp_path)
    body = "x" * 400
    for index in range(3):
        (repo / f"wiki/notes/Aggregate-{index}.md").write_text(
            _page(f"page_aggregate_{index}", f"Aggregate {index}", body)
        )
    _commit_repo(repo, "oversized aggregate")
    previous_paths = store.existing_paths(conn)
    try:
        with monkeypatch.context() as limits:
            limits.setattr(build, "MAX_INDEXABLE_MARKDOWN_BYTES", 1_024)
            limits.setattr(build, "MAX_COMMITTED_INDEX_BYTES", 1_024)
            with pytest.raises(StigmergyIndexError, match="size limit"):
                build.rebuild(conn, str(repo), build_embedder("fake"), require_repository_head=True)
            assert store.existing_paths(conn) == previous_paths
    finally:
        build.rebuild(conn, FIXTURE, build_embedder("fake"))


def test_rebuild_aborts_when_a_webhook_advances_index_health_after_its_snapshot(
    conn, tmp_path, monkeypatch
):
    class Response:
        def __init__(self, body):
            self.body = body.encode()

        def read(self):
            return self.body

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    class InterleavingEmbedder:
        model = "fake-hashed-bow-256"
        host = ""

        def __init__(self, callback):
            self.callback = callback
            self.delegate = build_embedder("fake")
            self.fired = False

        def embed(self, texts):
            if not self.fired:
                self.fired = True
                self.callback()
            return self.delegate.embed(texts)

    repo, initial_head = _git_fixture(tmp_path)
    concurrent = _connect_or_skip()
    race_path = "wiki/notes/Webhook state.md"
    race_text = _page("page_webhook_race", "Webhook state", "The webhook state must survive.")
    callback_stats = None

    def opener(request, timeout=30):
        del timeout
        assert request.full_url.endswith(f"/{race_path.replace(' ', '%20')}?ref=webhook-newer")
        return Response(race_text)

    def apply_webhook():
        nonlocal callback_stats
        callback_stats = webhook.process_push(
            concurrent,
            build_embedder("fake"),
            {
                "after": "webhook-newer",
                "before": initial_head,
                "commits": [{"added": [race_path], "modified": [], "removed": []}],
            },
            webhook.WebhookSettings(repo="acme/knowledge"),
            opener=opener,
        )

    monkeypatch.setattr(
        "stigmergy.librarian.githubapp.installation_token", lambda: "test-installation-token"
    )
    try:
        build.rebuild(conn, str(repo), build_embedder("fake"), require_repository_head=True)
        candidate = repo / "wiki" / "notes" / "Globex and Initech renewal.md"
        candidate.write_text(
            candidate.read_text().replace("renewal", f"rebuild candidate {time.monotonic_ns()}", 1)
        )
        _commit_repo(repo, "rebuild candidate")
        rebuild_error = None
        try:
            build.rebuild(conn, str(repo), InterleavingEmbedder(apply_webhook), require_repository_head=True)
        except StigmergyIndexError as error:
            rebuild_error = error
        assert callback_stats is not None
        assert callback_stats["upserted"] == 1
        assert isinstance(rebuild_error, StigmergyIndexError)
        assert "index health changed" in str(rebuild_error)
        with conn.cursor() as cursor:
            cursor.execute("SELECT body FROM pages_index WHERE path = %s", (race_path,))
            assert "webhook state must survive" in cursor.fetchone()[0].lower()
        state = health.read(conn)
        assert state["indexed_commit_sha"] == "webhook-newer"
        assert state["dirty"] is False
    finally:
        concurrent.close()
        build.rebuild(conn, FIXTURE, build_embedder("fake"))


def test_committed_snapshot_rejects_excessive_eligible_entries_before_replacement(
    conn, tmp_path, monkeypatch
):
    repo, _head = _git_fixture(tmp_path)
    for index in range(2):
        (repo / f"wiki/notes/Eligible-{index}.md").write_text(
            _page(f"page_eligible_{index}", f"Eligible {index}", "Indexable entry.")
        )
    _commit_repo(repo, "too many eligible entries")
    previous_paths = store.existing_paths(conn)
    try:
        with monkeypatch.context() as limits:
            limits.setattr(build, "MAX_COMMITTED_INDEX_ENTRIES", 1, raising=False)
            with pytest.raises(StigmergyIndexError, match="entries exceed the limit"):
                build.rebuild(conn, str(repo), build_embedder("fake"), require_repository_head=True)
            assert store.existing_paths(conn) == previous_paths
    finally:
        build.rebuild(conn, FIXTURE, build_embedder("fake"))


def test_committed_snapshot_rejects_excessive_watched_tree_entries_before_replacement(
    conn, tmp_path, monkeypatch
):
    repo, _head = _git_fixture(tmp_path)
    for index in range(2):
        (repo / f"wiki/notes/ignored-{index}.txt").write_text("Not indexable, but watched.")
    _commit_repo(repo, "too many watched entries")
    previous_paths = store.existing_paths(conn)
    try:
        with monkeypatch.context() as limits:
            limits.setattr(build, "MAX_COMMITTED_TREE_ENTRIES", 1, raising=False)
            with pytest.raises(StigmergyIndexError, match="repository tree exceeds the limit"):
                build.rebuild(conn, str(repo), build_embedder("fake"), require_repository_head=True)
            assert store.existing_paths(conn) == previous_paths
    finally:
        build.rebuild(conn, FIXTURE, build_embedder("fake"))


def test_committed_snapshot_does_not_capture_multi_blob_content_in_one_subprocess_result(
    conn, tmp_path, monkeypatch
):
    repo, _head = _git_fixture(tmp_path)
    original_popen = build.subprocess.Popen
    writes: list[bytes] = []
    flushes: list[None] = []

    class TrackingWriter:
        def __init__(self, stream):
            self._stream = stream

        def write(self, data):
            writes.append(bytes(data))
            return self._stream.write(data)

        def flush(self):
            flushes.append(None)
            return self._stream.flush()

        def __getattr__(self, name):
            return getattr(self._stream, name)

    def tracking_popen(*args, **kwargs):
        process = original_popen(*args, **kwargs)
        if args[0][1:] == ["cat-file", "--batch"]:
            assert process.stdin is not None
            process.stdin = TrackingWriter(process.stdin)
        return process

    monkeypatch.setattr(build.subprocess, "Popen", tracking_popen)
    try:
        stats = build.rebuild(conn, str(repo), build_embedder("fake"), require_repository_head=True)
        assert stats["pages"] == FIXTURE_PAGES
    finally:
        build.rebuild(conn, FIXTURE, build_embedder("fake"))
    assert len(writes) > 1
    assert all(write.endswith(b"\n") and write.count(b"\n") == 1 for write in writes)
    assert len(flushes) == len(writes)
