import hashlib
import hmac
import json
import urllib.error
import urllib.parse

import httpx
import pytest

from stigmergy.index import build, health, search, store
from stigmergy.index.backends.embedder import build_embedder
from stigmergy.server import webhook
from tests import testdb
from tests.server.conftest import build_test_http_app, run_http_server

SECRET = "test-webhook-secret"
REPO = "acme/knowledge"
BRANCH = "main"
def _sign(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _payload(*, sha="abc123", before="", repo=REPO, branch=BRANCH, changes=None):
    return {
        "ref": f"refs/heads/{branch}",
        "before": before,
        "after": sha,
        "repository": {"full_name": repo},
        "commits": changes or [],
    }


def _page(title: str, body: str, *, page_id="page_webhook", acl=None):
    return (
        "---\n"
        f"id: {page_id}\n"
        "type: note\n"
        f"title: {title}\n"
        "status: developing\n"
        "created: '2026-08-01'\n"
        "updated: '2026-08-24'\n"
        f"acl: {json.dumps(acl)}\n"
        "sources: []\n"
        "---\n\n"
        f"# {title}\n\n{body}\n"
    )


def _entity_page(title: str, body: str, *, page_id="page_webhook_entity", aliases=()):
    return (
        "---\n"
        f"id: {page_id}\n"
        "type: entity\n"
        f"title: {title}\n"
        "status: developing\n"
        "created: '2026-08-01'\n"
        "updated: '2026-08-24'\n"
        "acl: null\n"
        "sources: []\n"
        "entity_type: organization\n"
        f"aliases: {json.dumps(list(aliases))}\n"
        "---\n\n"
        f"# {title}\n\n{body}\n"
    )


class _Response:
    def __init__(self, text: str):
        self.body = text.encode()

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def _opener(contents: dict[str, str], seen: dict[str, str] | None = None):
    available = dict(contents)

    def open_request(request, timeout=30):
        del timeout
        parsed = urllib.parse.urlparse(request.full_url)
        path = urllib.parse.unquote(parsed.path.split("/contents/", 1)[1])
        if seen is not None:
            seen[path] = urllib.parse.parse_qs(parsed.query).get("ref", [""])[0]
        if path not in available:
            raise urllib.error.HTTPError(request.full_url, 404, "Not Found", {}, None)
        return _Response(available[path])

    return open_request


def _settings(**values):
    return webhook.WebhookSettings(secret=SECRET, repo=REPO, branch=BRANCH, **values)


@pytest.fixture(autouse=True)
def fake_installation_token(monkeypatch):
    monkeypatch.setattr(
        "stigmergy.librarian.githubapp.installation_token",
        lambda *_args, **_kwargs: "fake-token",
    )


@pytest.fixture()
def conn(tmp_path):
    repo = tmp_path / "repo"
    folder = repo / "wiki" / "notes"
    folder.mkdir(parents=True)
    (folder / "Seed.md").write_text(_page("Seed", "Seed body", page_id="page_seed"))
    connection = testdb.connect_or_skip("webhook")
    build.rebuild(connection, str(repo), build_embedder("fake"))
    yield connection
    connection.close()


@pytest.mark.parametrize(
    "secret,header,expected",
    [
        (SECRET, None, False),
        (SECRET, "sha1=bad", False),
        ("", "sha256=bad", False),
        (SECRET, "sha256=café", False),
    ],
)
def test_signature_refusals_are_closed(secret, header, expected):
    assert webhook.verify_signature(secret, b"body", header) is expected


def test_signature_accepts_exact_bytes_and_rejects_tampering():
    body = b'{"hello":"world"}'
    signature = _sign(body)
    assert webhook.verify_signature(SECRET, body, signature)
    assert not webhook.verify_signature(SECRET, body + b"!", signature)


@pytest.mark.parametrize("value", ["0", "-2", "invalid"])
def test_invalid_file_caps_fall_back_to_a_positive_default(value):
    settings = webhook.webhook_settings_from_env({webhook.WEBHOOK_FILE_CAP_ENV: value})
    assert settings.file_cap == webhook.DEFAULT_FILE_CAP


def test_changed_paths_use_the_last_state_in_a_push():
    payload = _payload(
        changes=[
            {"added": ["wiki/notes/A.md"], "modified": [], "removed": []},
            {"added": [], "modified": [], "removed": ["wiki/notes/A.md"]},
        ]
    )
    assert webhook.changed_paths_from_push(payload) == {"wiki/notes/A.md": "removed"}


def test_only_canonical_searchable_paths_enter_incremental_indexing():
    source = "sources/2026/08/60000000-0000-4000-8000-000000000001.md"
    changes = {
        "wiki/notes/A.md": "added",
        "wiki/concepts/B.md": "modified",
        source: "modified",
        "wiki/entities/Globex.md": "modified",
        "wiki/entities/nested/Globex.md": "added",
        "wiki/playbooks/C.md": "added",
        "ops/identities.json": "modified",
    }
    assert webhook.in_zone_changes(changes) == {
        "wiki/notes/A.md": "added",
        "wiki/concepts/B.md": "modified",
        source: "modified",
        "wiki/entities/Globex.md": "modified",
    }


def test_process_push_indexes_an_entity_page_and_resolves_the_note_that_links_it(conn):
    entity = "wiki/entities/Globex.md"
    note = "wiki/notes/Globex renewal.md"
    stats = webhook.process_push(
        conn,
        build_embedder("fake"),
        _payload(changes=[{"added": [entity, note], "modified": [], "removed": []}]),
        _settings(),
        opener=_opener({
            entity: _entity_page("Globex", "A customer organization.", aliases=["GLBX"]),
            note: _page("Globex renewal", "The [[Globex]] renewal is due.", page_id="page_globex_note"),
        }),
    )
    assert stats["upserted"] == 2

    rows = search.fetch_pages(conn, [entity, note])
    assert rows[entity]["type"] == "entity"
    assert rows[entity]["entity_type"] == "organization"
    assert rows[entity]["aliases"] == ["GLBX"]
    assert rows[entity]["inlinks"] == 1
    assert rows[note]["links"] == [entity]
    # the alias is part of the lexical document the push wrote
    fts_config = store.read_meta(conn)["fts_config"]
    assert search.fts_ranking(conn, "GLBX", fts_config) == [entity]


def test_process_push_adds_updates_and_deletes_one_page(conn):
    path = "wiki/notes/Webhook page.md"
    first = _page("Webhook page", "First body")
    add = _payload(changes=[{"added": [path], "modified": [], "removed": []}])
    stats = webhook.process_push(conn, build_embedder("fake"), add, _settings(), opener=_opener({path: first}))
    assert stats["upserted"] == 1

    second = _page("Webhook page", "Second body")
    modify = _payload(
        sha="abc124",
        before="abc123",
        changes=[{"added": [], "modified": [path], "removed": []}],
    )
    webhook.process_push(
        conn,
        build_embedder("fake"),
        modify,
        _settings(),
        opener=_opener({path: second}),
    )
    with conn.cursor() as cursor:
        cursor.execute("SELECT body FROM pages_index WHERE path = %s", (path,))
        assert "Second body" in cursor.fetchone()[0]

    remove = _payload(
        sha="abc125",
        before="abc124",
        changes=[{"added": [], "modified": [], "removed": [path]}],
    )
    assert webhook.process_push(conn, build_embedder("fake"), remove, _settings(), opener=_opener({}))[
        "deleted"
    ] == 1
    assert path not in store.existing_paths(conn)
    assert path not in {
        hit["path"] for hit in search.search(conn, "Second body", embedder=build_embedder("fake"))
    }


def test_incremental_link_resolution_uses_current_index_paths(conn):
    target = "wiki/notes/Target.md"
    linker = "wiki/notes/Linker.md"
    target_text = _page("Target", "Target body", page_id="page_target")
    linker_text = _page("Linker", "See [[Target]].", page_id="page_linker")
    webhook.process_push(
        conn,
        build_embedder("fake"),
        _payload(changes=[{"added": [target], "modified": [], "removed": []}]),
        _settings(),
        opener=_opener({target: target_text}),
    )
    webhook.process_push(
        conn,
        build_embedder("fake"),
        _payload(
            sha="abc124",
            before="abc123",
            changes=[{"added": [linker], "modified": [], "removed": []}],
        ),
        _settings(),
        opener=_opener({linker: linker_text}),
    )
    with conn.cursor() as cursor:
        cursor.execute("SELECT links FROM pages_index WHERE path = %s", (linker,))
        assert cursor.fetchone()[0] == [target]


def test_incremental_link_graph_converges_for_new_and_deleted_pages_in_one_push(conn):
    target = "wiki/notes/New target.md"
    linker = "wiki/notes/New linker.md"
    webhook.process_push(
        conn,
        build_embedder("fake"),
        _payload(
            changes=[
                {
                    "added": [target, linker],
                    "modified": [],
                    "removed": [],
                }
            ]
        ),
        _settings(),
        opener=_opener(
            {
                target: _page("New target", "Target body", page_id="page_new_target"),
                linker: _page(
                    "New linker",
                    "See [[New target]].",
                    page_id="page_new_linker",
                ),
            }
        ),
    )

    with conn.cursor() as cursor:
        cursor.execute(
            "SELECT path, links, inlinks FROM pages_index WHERE path = ANY(%s) ORDER BY path",
            ([target, linker],),
        )
        rows = {path: (links, inlinks) for path, links, inlinks in cursor.fetchall()}
    assert rows[linker] == ([target], 0)
    assert rows[target] == ([], 1)

    webhook.process_push(
        conn,
        build_embedder("fake"),
        _payload(
            sha="abc124",
            before="abc123",
            changes=[{"added": [], "modified": [], "removed": [target]}],
        ),
        _settings(),
        opener=_opener({}),
    )
    with conn.cursor() as cursor:
        cursor.execute("SELECT links FROM pages_index WHERE path = %s", (linker,))
        assert cursor.fetchone()[0] == []


def test_overflow_defers_every_change_and_marks_index_dirty(conn):
    paths = [f"wiki/notes/Page {index}.md" for index in range(3)]
    payload = _payload(sha="overflow-sha", changes=[{"added": paths, "modified": [], "removed": []}])
    stats = webhook.process_push(
        conn,
        build_embedder("fake"),
        payload,
        _settings(file_cap=2),
        opener=_opener({path: _page(f"Page {index}", "Body") for index, path in enumerate(paths)}),
    )
    assert stats["deferred"] is True
    state = health.read(conn)
    assert state["dirty"] is True
    assert state["repository_head_sha"] == "overflow-sha"
    assert not set(paths) & set(store.existing_paths(conn))


def test_truncated_commit_list_defers_without_partial_indexing(conn):
    path = "wiki/notes/Truncated.md"
    payload = _payload(
        sha="truncated-sha",
        changes=[{"added": [path], "modified": [], "removed": []}],
    )
    payload["size"] = 3

    stats = webhook.process_push(
        conn,
        build_embedder("fake"),
        payload,
        _settings(),
        opener=_opener({path: _page("Truncated", "Body")}),
    )

    assert stats["deferred_reason"] == "truncated_push"
    assert stats["included_commits"] == 1
    assert path not in store.existing_paths(conn)
    assert health.read(conn)["repository_head_sha"] == "truncated-sha"


def test_non_linear_push_is_deferred_without_reinstalling_stale_content(conn):
    path = "wiki/notes/Linear.md"
    current = _page("Linear", "Current body", page_id="page_linear")
    webhook.process_push(
        conn,
        build_embedder("fake"),
        _payload(changes=[{"added": [path], "modified": [], "removed": []}]),
        _settings(),
        opener=_opener({path: current}),
    )

    stale = _page("Linear", "Stale body", page_id="page_linear")
    stats = webhook.process_push(
        conn,
        build_embedder("fake"),
        _payload(
            sha="stale-sha",
            before="unrelated-sha",
            changes=[{"added": [], "modified": [path], "removed": []}],
        ),
        _settings(),
        opener=_opener({path: stale}),
    )

    assert stats["deferred_reason"] == "non_linear_push"
    with conn.cursor() as cursor:
        cursor.execute("SELECT body FROM pages_index WHERE path = %s", (path,))
        assert "Current body" in cursor.fetchone()[0]
    assert health.read(conn)["dirty"] is True


def test_phase_two_failure_rolls_back_deletion_and_upsert(conn, monkeypatch):
    victim = "wiki/notes/Victim.md"
    victim_text = _page("Victim", "Keep me", page_id="page_victim")
    webhook.process_push(
        conn,
        build_embedder("fake"),
        _payload(changes=[{"added": [victim], "modified": [], "removed": []}]),
        _settings(),
        opener=_opener({victim: victim_text}),
    )
    new = "wiki/notes/New.md"
    original = store.upsert_pages

    def fail(*_args, **_kwargs):
        raise RuntimeError("simulated upsert failure")

    monkeypatch.setattr(store, "upsert_pages", fail)
    payload = _payload(
        sha="abc124",
        before="abc123",
        changes=[{"added": [new], "modified": [], "removed": [victim]}],
    )
    with pytest.raises(RuntimeError, match="simulated"):
        webhook.process_push(
            conn,
            build_embedder("fake"),
            payload,
            _settings(),
            opener=_opener({new: _page("New", "New body", page_id="page_new")}),
        )
    monkeypatch.setattr(store, "upsert_pages", original)
    assert victim in store.existing_paths(conn)
    assert new not in store.existing_paths(conn)


def test_a_push_touching_files_outside_the_corpus_indexes_only_its_pages(conn):
    page = "wiki/notes/Current.md"
    seen = {}
    payload = _payload(
        sha="push-sha",
        changes=[{"added": [page], "modified": ["ops/identities.json"], "removed": []}],
    )
    stats = webhook.process_push(
        conn,
        build_embedder("fake"),
        payload,
        _settings(),
        opener=_opener({page: _page("Current", "Body")}, seen),
    )
    assert seen == {page: "push-sha"}
    assert stats["upserted"] == 1
    assert health.read(conn)["indexed_commit_sha"] == "push-sha"


def test_successful_delivery_id_is_recorded_atomically(conn):
    path = "wiki/notes/Deduplicated.md"
    delivery = "delivery-atomic-1"
    with conn.cursor() as cursor:
        cursor.execute("DELETE FROM webhook_deliveries WHERE delivery_id = %s", (delivery,))
    webhook.process_push(
        conn,
        build_embedder("fake"),
        _payload(changes=[{"added": [path], "modified": [], "removed": []}]),
        _settings(),
        delivery_id=delivery,
        opener=_opener({path: _page("Deduplicated", "Body")}),
    )
    assert store.delivery_already_applied(conn, delivery)


@pytest.fixture()
def webhook_environment(monkeypatch):
    """The suite's tenant owns `REPO`, so a delivery for it is applied and others are ignored."""
    monkeypatch.setenv(webhook.WEBHOOK_SECRET_ENV, SECRET)
    monkeypatch.setenv(webhook.WEBHOOK_BRANCH_ENV, BRANCH)
    conn = testdb.connect_or_skip("webhook")
    with conn.cursor() as cur:
        cur.execute("SELECT repo FROM tenants WHERE id = %s", (testdb.TEST_TENANT,))
        previous = cur.fetchone()[0]
        cur.execute("UPDATE tenants SET repo = %s WHERE id = %s", (REPO, testdb.TEST_TENANT))
    yield
    with conn.cursor() as cur:
        cur.execute("UPDATE tenants SET repo = %s WHERE id = %s", (previous, testdb.TEST_TENANT))
    conn.close()


def _headers(body: bytes, *, event="push", delivery="delivery-http"):
    return {
        "X-Hub-Signature-256": _sign(body),
        "X-GitHub-Event": event,
        "X-GitHub-Delivery": delivery,
    }


def test_http_endpoint_rejects_invalid_signature(fixture, webhook_environment):
    body = json.dumps(_payload()).encode()
    app = build_test_http_app(fixture, {})
    with run_http_server(app) as url:
        base = url.rsplit("/", 1)[0]
        response = httpx.post(
            f"{base}{webhook.WEBHOOK_PATH}",
            content=body,
            headers={**_headers(body), "X-Hub-Signature-256": "sha256=bad"},
        )
    assert response.status_code == 401


@pytest.mark.parametrize(
    "payload,event,ignored",
    [
        (_payload(), "ping", "event"),
        (_payload(repo="other/repo"), "push", "repository"),
        (_payload(branch="other"), "push", "ref"),
    ],
)
def test_http_endpoint_ignores_unrelated_deliveries(
    fixture, webhook_environment, payload, event, ignored
):
    body = json.dumps(payload).encode()
    app = build_test_http_app(fixture, {})
    with run_http_server(app) as url:
        base = url.rsplit("/", 1)[0]
        response = httpx.post(
            f"{base}{webhook.WEBHOOK_PATH}", content=body, headers=_headers(body, event=event)
        )
    assert response.status_code == 200
    assert ignored in response.json()["ignored"]


def test_http_endpoint_refuses_oversized_body_before_json_parsing(fixture, webhook_environment):
    body = b"x" * (webhook.MAX_BODY_BYTES + 1)
    app = build_test_http_app(fixture, {})
    with run_http_server(app) as url:
        base = url.rsplit("/", 1)[0]
        response = httpx.post(
            f"{base}{webhook.WEBHOOK_PATH}", content=body, headers=_headers(body)
        )
    assert response.status_code == 401
    assert response.json() == {"error": "unauthorized"}


def _source_page(capture_id: str, occurred_at: str) -> str:
    return (
        "---\n"
        f"id: {capture_id}\n"
        "type: source\n"
        "title: Call notes\n"
        "acl: null\n"
        "captured_at: '2026-09-30T08:00:00+00:00'\n"
        f"occurred_at: '{occurred_at}'\n"
        "---\n\n"
        "# Call notes\n\nWhat was said.\n"
    )


def _citing_page(title: str, source: str, *, page_id: str) -> str:
    return _page(title, "A conclusion.", page_id=page_id).replace(
        "sources: []", f"sources: [{source}]")


def test_process_push_dates_pages_by_the_sources_they_cite_including_already_indexed_ones(conn):
    capture_id = "70000000-0000-4000-8000-000000000001"
    source = f"sources/2026/09/{capture_id}.md"
    first, second = "wiki/notes/First conclusion.md", "wiki/notes/Second conclusion.md"
    webhook.process_push(
        conn, build_embedder("fake"),
        _payload(changes=[{"added": [source, first], "modified": [], "removed": []}]), _settings(),
        opener=_opener({source: _source_page(capture_id, "2026-03-14T09:30:00+00:00"),
                        first: _citing_page("First conclusion", source, page_id="page_first")}))
    webhook.process_push(
        conn, build_embedder("fake"),
        _payload(sha="abc124", before="abc123",
                 changes=[{"added": [second], "modified": [], "removed": []}]), _settings(),
        opener=_opener({second: _citing_page("Second conclusion", source, page_id="page_second")}))
    with conn.cursor() as cursor:
        cursor.execute("SELECT path, source_date, source_dates FROM pages_index WHERE path = ANY(%s)",
                       ([source, first, second],))
        dated = {path: (own, list(cited)) for path, own, cited in cursor.fetchall()}
    assert dated == {
        source: ("2026-03-14", ["2026-03-14"]),
        first: ("", ["2026-03-14"]),
        second: ("", ["2026-03-14"]),
    }


def test_process_push_snapshots_the_graph_measures_at_the_new_commit(conn):
    from stigmergy.index import measures

    path = "wiki/notes/Measured page.md"
    webhook.process_push(conn, build_embedder("fake"),
                         _payload(sha="f" * 40, changes=[{"added": [path], "modified": [], "removed": []}]),
                         _settings(), opener=_opener({path: _page("Measured page", "A body.")}))
    with conn.cursor() as cursor:
        cursor.execute("SELECT measures FROM graph_measures WHERE indexed_commit = %s AND scope = 'brain'"
                       " ORDER BY id DESC LIMIT 1", ("f" * 40,))
        snapshot = cursor.fetchone()
    assert snapshot is not None and snapshot[0]["pages_by_type"]["note"] == 2
    assert measures.record_snapshot(conn, "f" * 40) == 0


def test_process_push_replaces_and_removes_a_pages_fragments(conn):
    path = "wiki/notes/Long note.md"
    sections = "\n\n".join(f"## Part {n}\n\n" + " ".join(f"word{n}x{i}" for i in range(300)) for n in range(4))

    def fragments_of(path):
        with conn.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM page_fragments WHERE path = %s", (path,))
            return cursor.fetchone()[0]

    webhook.process_push(conn, build_embedder("fake"),
                         _payload(changes=[{"added": [path], "modified": [], "removed": []}]), _settings(),
                         opener=_opener({path: _page("Long note", sections)}))
    assert fragments_of(path) == 4
    webhook.process_push(conn, build_embedder("fake"),
                         _payload(sha="abc124", before="abc123",
                                  changes=[{"added": [], "modified": [path], "removed": []}]), _settings(),
                         opener=_opener({path: _page("Long note", "Now short.")}))
    assert fragments_of(path) == 1
    webhook.process_push(conn, build_embedder("fake"),
                         _payload(sha="abc125", before="abc124",
                                  changes=[{"added": [], "modified": [], "removed": [path]}]), _settings(),
                         opener=_opener({}))
    assert fragments_of(path) == 0
