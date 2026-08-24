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
from tests.index.support import write_controls
from tests.server.conftest import build_test_http_app, run_http_server

SECRET = "test-webhook-secret"
REPO = "acme/knowledge"
BRANCH = "main"
CONTROL_TEXTS = {
    store.ENTITY_REGISTRY_RELPATH: '{"version":1,"entities":{},"redirects":{}}',
    store.IDENTITIES_RELPATH: json.dumps(
        {
            "test-master": {
                "display_name": "Test Master",
                "groups": ["brain-admins", "finance"],
                "default_audience": None,
            }
        }
    ),
    store.SLACK_CHANNELS_RELPATH: "{}",
}


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


def _page(title: str, body: str, *, page_id="page_webhook", acl=None, entities=()):
    return (
        "---\n"
        f"id: {page_id}\n"
        "type: note\n"
        f"title: {title}\n"
        "status: developing\n"
        "created: '2026-08-01'\n"
        "updated: '2026-08-24'\n"
        f"acl: {json.dumps(acl)}\n"
        f"entity: {json.dumps(list(entities))}\n"
        "sources: []\n"
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
    available = {**CONTROL_TEXTS, **contents}

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
    write_controls(repo)
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
        "wiki/entities/ent_x.md": "modified",
        "wiki/playbooks/C.md": "added",
        "ops/identities.json": "modified",
    }
    assert webhook.in_zone_changes(changes) == {
        "wiki/notes/A.md": "added",
        "wiki/concepts/B.md": "modified",
        source: "modified",
    }


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


def test_removed_control_file_defers_to_a_full_rebuild(conn):
    payload = _payload(
        sha="removed-control-sha",
        changes=[
            {
                "added": [],
                "modified": [],
                "removed": [store.IDENTITIES_RELPATH],
            }
        ],
    )

    stats = webhook.process_push(conn, build_embedder("fake"), payload, _settings())

    assert stats["deferred_reason"] == "control_file_removed"
    assert health.read(conn)["dirty"] is True


def test_refused_control_file_defers_without_advancing_the_index(conn):
    previous = '{"master": {}}\n'
    store.write_ops_file(conn, store.IDENTITIES_RELPATH, previous, "previous-sha")
    oversized = "x" * (store.MAX_OPS_FILE_BYTES + 1)
    payload = _payload(
        sha="oversized-control-sha",
        changes=[
            {
                "added": [],
                "modified": [store.IDENTITIES_RELPATH],
                "removed": [],
            }
        ],
    )

    stats = webhook.process_push(
        conn,
        build_embedder("fake"),
        payload,
        _settings(),
        opener=_opener({store.IDENTITIES_RELPATH: oversized}),
    )

    assert stats["deferred_reason"] == "control_file_refused"
    assert store.read_ops_file(conn, store.IDENTITIES_RELPATH) == previous
    assert health.read(conn)["indexed_commit_sha"] == ""


@pytest.mark.parametrize(
    ("relative,invalid"),
    [
        (store.IDENTITIES_RELPATH, "{}"),
        (store.SLACK_CHANNELS_RELPATH, '{"C1":["unknown-group"]}'),
    ],
)
def test_invalid_control_set_defers_and_preserves_previous_snapshots(
    conn, relative, invalid
):
    previous = {
        path: store.read_ops_file(conn, path) for path in store.OPS_FILE_RELPATHS
    }
    payload = _payload(
        sha="invalid-control-sha",
        changes=[{"added": [], "modified": [relative], "removed": []}],
    )

    stats = webhook.process_push(
        conn,
        build_embedder("fake"),
        payload,
        _settings(),
        opener=_opener({relative: invalid}),
    )

    assert stats["deferred_reason"] == "control_file_invalid"
    assert {
        path: store.read_ops_file(conn, path) for path in store.OPS_FILE_RELPATHS
    } == previous


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


def test_ops_files_and_pages_refresh_from_the_same_push_sha(conn):
    page = "wiki/notes/Current.md"
    seen = {}
    identities = '{"ana@example.com":{"display_name":"Ana","groups":[],"default_audience":null}}'
    payload = _payload(
        sha="push-sha",
        changes=[
            {
                "added": [page],
                "modified": [store.IDENTITIES_RELPATH],
                "removed": [],
            }
        ],
    )
    stats = webhook.process_push(
        conn,
        build_embedder("fake"),
        payload,
        _settings(),
        opener=_opener({page: _page("Current", "Body"), store.IDENTITIES_RELPATH: identities}, seen),
    )
    assert seen[page] == "push-sha"
    assert seen[store.IDENTITIES_RELPATH] == "push-sha"
    assert store.read_ops_file(conn, store.IDENTITIES_RELPATH) == identities
    assert store.IDENTITIES_RELPATH in stats["ops_files_refreshed"]


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
    monkeypatch.setenv(webhook.WEBHOOK_SECRET_ENV, SECRET)
    monkeypatch.setenv(webhook.WEBHOOK_REPO_ENV, REPO)
    monkeypatch.setenv(webhook.WEBHOOK_BRANCH_ENV, BRANCH)


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
