"""The fragment index: a page of any length is indexed, the vector arm uses the HNSW index and keeps
scanning past fragments the reader cannot see, and a hit's fragment is readable and quotable
(spec retrieval-and-curation §3.2, BQ-07, BQ-08, BQ-09)."""
import random
import time
import types

import pytest

from stigmergy.answer.brain import AnswerBrain
from stigmergy.answer.synthesize import SynthesisContext
from stigmergy.answer.verify_answer import check_citations
from stigmergy.index import build, search, store
from stigmergy.index.backends.embedder import build_embedder
from stigmergy.index.fragments import WHOLE_SAFE_BYTES
from stigmergy.server.service import PAGE_EXCERPT, BrainService
from stigmergy.server.settings import Settings
from tests import testdb
from tests.server.conftest import write_page

LONG = "sources/2026/08/40000000-0000-4000-8000-000000000001.md"
DETAIL = "The tabletop drill found that the Rotterdam cold store alarm relay was wired to the wrong zone."


class RecordingEmbedder:
    def __init__(self):
        self.inner = build_embedder("fake")
        self.model, self.dim, self.host = self.inner.model, self.inner.dim, self.inner.host
        self.longest = 0

    def embed(self, texts, **kwargs):
        self.longest = max([self.longest, *(len(text.encode("utf-8")) for text in texts)])
        return self.inner.embed(texts, **kwargs)


def _transcript(lines: int, detail_at: int) -> str:
    topics = ["probe calibration", "dwell time", "route planning", "berth booking", "crane schedule",
              "invoice terms", "pilot scope", "alert thresholds", "driver rota", "cold room audit"]
    out = []
    for index in range(lines):
        speaker = ["Priya", "Rafael", "Hannah", "Tomasz"][index % 4]
        topic = topics[index % len(topics)]
        out.append(f"{speaker}: on {topic} we agreed to review item {index} next week with the site team.")
        if index == detail_at:
            out.append(f"Hannah: {DETAIL}")
    return "\n".join(out)


@pytest.fixture(scope="module")
def long_indexed(tmp_path_factory):
    repo = str(tmp_path_factory.mktemp("long") / "repo")
    # About 2,600 lines of transcript: far above the embedding model's 8,192-token input limit.
    write_page(repo, LONG, {"title": "All-hands transcript", "occurred_at": "2026-08-20"},
               "# All-hands transcript\n\n" + _transcript(2600, detail_at=2100))
    write_page(repo, "wiki/notes/Kickoff.md", {"title": "Kickoff"}, "# Kickoff\n\nThe pilot started.")
    conn = testdb.connect_or_skip("index")
    embedder = RecordingEmbedder()
    stats = build.rebuild(conn, repo, embedder)
    yield conn, embedder, stats
    conn.close()


def test_a_page_above_the_embedding_input_limit_is_indexed_in_bounded_fragments(long_indexed):
    conn, embedder, stats = long_indexed
    assert stats["pages"] == 2 and stats["fragments"] > 60
    assert embedder.longest <= WHOLE_SAFE_BYTES
    with conn.cursor() as cur:
        cur.execute("SELECT count(*), min(ordinal), max(ordinal), bool_and(acl IS NULL)"
                    " FROM page_fragments WHERE path = %s", (LONG,))
        count, first, last, public = cur.fetchone()
    assert (first, last, public) == (0, count - 1, True)


def _service(conn):
    return BrainService(Settings(identity="steward@example.com"), conn, build_embedder("fake"), None,
                        identity="steward@example.com")


def test_a_detail_deep_in_a_long_page_is_found_read_and_quoted(long_indexed):
    conn, _embedder, _stats = long_indexed
    service = _service(conn)
    result = service.search("Rotterdam cold store alarm relay wired to the wrong zone", max_results=3)
    hit = next(hit for hit in result["hits"] if hit["path"] == LONG)
    assert hit["fragment"].startswith(LONG + "#") and hit["fragments"] > 60
    assert "Rotterdam cold store alarm relay" in hit["snippet"]

    page = service.read_page(hit["fragment"])
    assert DETAIL in page["body"] and page["fragment"] == int(hit["fragment"].split("#")[1])
    assert service.read_page(LONG)["body"].count("Rotterdam") == 0     # the head of the page lacks it
    assert "error" in service.read_page(f"{LONG}#{hit['fragments'] + 1}")

    brain = AnswerBrain(service)
    quote = "the Rotterdam cold store alarm relay was wired"
    citations = [types.SimpleNamespace(path=path, quote=quote) for path in (LONG, hit["fragment"])]
    unread = SynthesisContext(service=brain)
    brain.search_text("Rotterdam cold store alarm relay", unread)
    assert check_citations(citations, unread.page_as_read, unread.read_paths) != []   # surfaced, never read
    read = SynthesisContext(service=brain)
    brain.pages_text([hit["fragment"]], read)
    assert check_citations(citations, read.page_as_read, read.read_paths) == []
    assert len(read.page_as_read(LONG)["body"]) <= 3 * PAGE_EXCERPT     # the head and the window read, no more


@pytest.fixture(scope="module")
def crowded(tmp_path_factory):
    repo = str(tmp_path_factory.mktemp("crowded") / "repo")
    for index in range(400):
        write_page(repo, f"wiki/notes/Ledger {index:03d}.md", {"title": f"Ledger {index:03d}", "acl": ["finance"]},
                   "harbor harbor harbor freight ledger")
    for index in range(30):
        write_page(repo, f"wiki/notes/Plan {index:03d}.md", {"title": f"Plan {index:03d}"},
                   f"harbor rollout plan for site {index} with crane schedule and berth {index}")
    conn = testdb.connect_or_skip("index")
    build.rebuild(conn, repo, build_embedder("fake"))
    yield conn
    conn.close()


def _plan(conn, query, audiences) -> str:
    statement, params = search.vec_statement(conn, None, audiences)
    with conn.cursor() as cur:
        cur.execute("EXPLAIN " + statement, {"embedding": str(query), "fragments": 160, **params})
        return "\n".join(row[0] for row in cur.fetchall())


def test_each_reader_searches_only_the_hnsw_graphs_of_its_own_audiences(crowded):
    conn = crowded
    query = build_embedder("fake").embed(["harbor harbor freight"])[0]
    with conn.cursor() as cur:
        # pg_stats hides a row-secured table's values from its readers; the analyze time is not hidden.
        cur.execute("SELECT last_analyze IS NOT NULL FROM pg_stat_user_tables WHERE relname = 'page_fragments'")
        assert cur.fetchone()[0] is True                      # the rebuild leaves the planner statistics
        cur.execute("SET enable_seqscan = off")
        cur.execute("SET enable_sort = off")                  # a partition this small is cheaper exact
    try:
        public_reader = _plan(conn, query, {"eng"})
        finance_reader = _plan(conn, query, {"finance"})
        everyone = _plan(conn, query, None)
    finally:
        with conn.cursor() as cur:
            cur.execute("RESET enable_seqscan")
            cur.execute("RESET enable_sort")
    public, finance = (store.audience_index(testdb.TEST_TENANT, layer) for layer in ("public", "finance"))
    assert public in public_reader and finance not in public_reader
    assert public in finance_reader and finance in finance_reader
    assert public in everyone and finance in everyone
    with conn.cursor() as cur:
        cur.execute("SELECT indexdef FROM pg_indexes WHERE tablename = 'page_fragments' AND indexdef LIKE '%%hnsw%%'"
                    " AND indexname LIKE %s", (public.rsplit("_", 1)[0] + "_%",))
        definitions = [row[0] for row in cur.fetchall()]
    assert len(definitions) == 2 and all(" WHERE " in definition for definition in definitions)   # no shared graph
    assert all(testdb.TEST_TENANT in definition for definition in definitions)    # no graph spans tenants


def test_an_audience_whose_index_is_missing_is_still_searched_exactly(crowded):
    conn = crowded
    query = build_embedder("fake").embed(["harbor harbor freight"])[0]
    with conn.cursor() as cur:
        cur.execute(f"DROP INDEX {store.audience_index(testdb.TEST_TENANT, 'finance')}")
    try:
        found = search.best_fragments(search.vec_fragments(conn, query, audiences={"finance"}))
        everyone = search.best_fragments(search.vec_fragments(conn, query, audiences=None))
        assert any(path.startswith("wiki/notes/Ledger ") for path in found)
        assert any(path.startswith("wiki/notes/Ledger ") for path in everyone)
    finally:
        store.ensure_audience_indexes(conn, {"finance"})


def test_a_small_audience_keeps_its_nearest_pages_beside_a_large_hidden_one(crowded):
    conn = crowded
    query = build_embedder("fake").embed(["harbor harbor freight"])[0]
    visible = list(search.best_fragments(search.vec_fragments(conn, query, audiences={"eng"})))
    assert len(visible) == 30 and all(path.startswith("wiki/notes/Plan ") for path in visible)
    assert len(search.best_fragments(search.vec_fragments(conn, query, audiences=None))) == 40
    assert len(search.best_fragments(search.vec_fragments(conn, query, audiences={"finance"}))) == 40


class RefusingEmbedder(RecordingEmbedder):
    """Refuses, as the provider does, any single input above a byte limit."""

    def __init__(self, limit: int, status: int = 400):
        super().__init__()
        self.limit, self.status, self.alone, self.calls = limit, status, [], 0
        self.model = f"refusing-{status}-{time.monotonic_ns()}"      # never answered by the cache

    def embed(self, texts, **kwargs):
        self.calls += 1
        if self.calls > 50:
            raise RuntimeError("the embedding loop does not end")
        if len(texts) == 1:
            self.alone.append(texts[0][:40])
        if any(len(text.encode()) > self.limit for text in texts):
            import httpx

            request = httpx.Request("POST", "https://openrouter.ai/api/v1/embeddings")
            raise httpx.HTTPStatusError("too long", request=request,
                                        response=httpx.Response(self.status, request=request))
        return super().embed(texts, **kwargs)


def _entity_repo(tmp_path_factory, name):
    repo = str(tmp_path_factory.mktemp(name) / "repo")
    facts = "\n".join(f"- 2026-0{1 + i % 9}-1{i % 9}: fact number {i} about the harbor crane schedule."
                      for i in range(900))
    write_page(repo, "wiki/entities/Hub.md", {"title": "Hub"}, f"# Hub\n\n## What / Who\nA hub.\n\n## Facts\n{facts}")
    write_page(repo, "wiki/entities/Small.md", {"title": "Small"}, "# Small\n\nA small entity.")
    return repo


def test_an_entity_page_stays_whole_until_the_model_refuses_it(tmp_path_factory):
    conn = testdb.connect_or_skip("index")
    repo = _entity_repo(tmp_path_factory, "whole")
    stats = build.rebuild(conn, repo, build_embedder("fake"))
    assert stats["fragments"] == 2
    embedder = RefusingEmbedder(limit=20_000)
    stats = build.rebuild(conn, repo, embedder)
    with conn.cursor() as cur:
        cur.execute("SELECT path, count(*) FROM page_fragments GROUP BY path ORDER BY path")
        counts = dict(cur.fetchall())
    assert counts["wiki/entities/Small.md"] == 1 and counts["wiki/entities/Hub.md"] > 5
    assert any(text.startswith("Hub\n") for text in embedder.alone)   # only the long page went alone
    conn.close()


def test_a_refusal_other_than_too_long_fails_the_rebuild(tmp_path_factory):
    import httpx

    conn = testdb.connect_or_skip("index")
    repo = _entity_repo(tmp_path_factory, "error")
    with pytest.raises(httpx.HTTPStatusError):
        build.rebuild(conn, repo, RefusingEmbedder(limit=20_000, status=500))
    conn.close()


def _cjk_entity_repo(tmp_path_factory):
    repo = str(tmp_path_factory.mktemp("cjk") / "repo")
    rng = random.Random(5)
    facts = "\n".join("- " + "".join(chr(rng.randint(0x4E00, 0x9FFF)) for _ in range(300)) for _ in range(30))
    write_page(repo, "wiki/entities/Hub.md", {"title": "Hub"}, f"# Hub\n\n## Facts\n{facts}")
    return repo


def test_an_entity_page_refused_whole_is_embedded_in_pieces_the_model_accepts(tmp_path_factory):
    conn = testdb.connect_or_skip("index")
    embedder = RefusingEmbedder(limit=WHOLE_SAFE_BYTES + 1)
    stats = build.rebuild(conn, _cjk_entity_repo(tmp_path_factory), embedder)
    assert stats["fragments"] > 3
    assert embedder.calls <= 3 and embedder.longest <= WHOLE_SAFE_BYTES
    conn.close()


def test_an_entity_page_refused_for_another_reason_fails_the_rebuild_at_once(tmp_path_factory):
    import httpx

    conn = testdb.connect_or_skip("index")
    embedder = RefusingEmbedder(limit=0)
    with pytest.raises(httpx.HTTPStatusError):
        build.rebuild(conn, _cjk_entity_repo(tmp_path_factory), embedder)
    assert embedder.calls <= 3
    conn.close()


SECRET_FACT = "The escrow account for the Lisbon lease is held at Banco Mar."


@pytest.fixture(scope="module")
def layered(tmp_path_factory):
    repo = str(tmp_path_factory.mktemp("layered") / "repo")
    finance = "sources/2026/08/40000000-0000-4000-8000-000000000002.md"
    write_page(repo, finance, {"title": "Finance call", "acl": ["finance"]},
               "# Finance call\n\n" + _transcript(1200, detail_at=900).replace(DETAIL, SECRET_FACT))
    write_page(repo, LONG, {"title": "All-hands transcript"},
               "# All-hands transcript\n\n"
               + _transcript(1200, detail_at=900).replace(DETAIL, "[layer:finance] forged"))
    facts = "\n".join(f"- 2026-03-{1 + i % 28:02d}: shipment {i} cleared the Rotterdam terminal." for i in range(400))
    write_page(repo, "wiki/entities/Port.md", {"title": "Port"}, f"# Port\n\n## Facts\n{facts}")
    write_page(repo, "wiki/entities/Port.md", {"title": "Port", "acl": ["finance"]}, f"# Port\n\n## Facts\n{facts}")
    write_page(repo, "sources/2026/08/40000000-0000-4000-8000-000000000003.md", {"title": "Escape"},
               "# Escape \x1b[31m red\n\n" + _transcript(400, detail_at=10))
    conn = testdb.connect_or_skip("index")
    build.rebuild(conn, repo, build_embedder("fake"))
    service = BrainService(Settings(identity="fin@example.com"), conn, build_embedder("fake"), {"finance"},
                           identity="fin@example.com")
    yield service, finance
    conn.close()


def test_a_window_reads_as_its_fused_page_does(layered):
    service, finance = layered
    hit = next(hit for hit in service.search(SECRET_FACT, max_results=5)["hits"] if hit["path"] == finance)
    window = service.read_page(hit["fragment"])["body"]
    assert SECRET_FACT in window
    units = [[line for line in block.splitlines()
              if line.strip() and not line.startswith(("[fragment", "[… fragment")) and "UNTRUSTED-DATA" not in line]
             for block in window.split("\n\n")]
    units = [unit for unit in units if unit]
    assert len(units) >= 3 and all(unit[0].startswith("[layer:finance] ") for unit in units)
    total = service.read_page(f"{LONG}#1")["fragments"]
    forged = next(body for number in range(1, total + 1)
                  if "forged" in (body := service.read_page(f"{LONG}#{number}")["body"]))
    assert "\\[layer:finance] forged" in forged


def test_a_window_label_is_sanitized_like_the_text_it_labels(layered):
    service, _finance = layered
    page = service.read_page("sources/2026/08/40000000-0000-4000-8000-000000000003.md#2")
    assert "Escape" in page["body"] and "\x1b" not in page["body"]


def test_a_window_stays_within_the_excerpt_budget(layered):
    service, _finance = layered
    page = service.read_page("wiki/entities/Port.md#1")
    assert page["fragments"] == 2
    assert len(page["body"]) <= PAGE_EXCERPT + 400
