"""`POST /webhook/github`, exercised at two levels:

- the ASGI/HTTP level (a real uvicorn server, `build_test_http_app`) for HMAC verification, the
  bearer-middleware exemption, and event/branch/repository filtering — none of which ever
  reaches GitHub's API, so no stub is needed;
- `webhook.process_push` called directly for the indexing behavior itself (upsert, delete, the
  content_hash skip, the file cap) — `githubapp.installation_token` is monkeypatched to a fixed
  string (no real App, no real JWT) and `process_push`'s own `opener=` seam fakes the GitHub
  Contents API fetch, mirroring `githubapp.installation_token`'s own injectable-opener pattern.

No network reached anywhere in this file.
"""
import hashlib
import hmac
import json
import logging
import urllib.error
import urllib.parse

import httpx
import pytest

from stigmergy.index import store
from stigmergy.server import webhook
from tests.server.conftest import build_test_http_app, run_http_server

SECRET = "test-webhook-secret"
REPO = "acme/knowledge"
BRANCH = "main"


def _sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _push_payload(*, ref=f"refs/heads/{BRANCH}", repo_full_name=REPO, sha="abc123",
                  commits=None) -> dict:
    return {
        "ref": ref,
        "after": sha,
        "repository": {"full_name": repo_full_name},
        "commits": commits or [],
    }


# ── pure unit tests: signature, payload parsing, zone filtering ────────────────────────────────
def test_verify_signature_accepts_a_correctly_signed_body():
    body = b'{"hello": "world"}'
    header = _sign(SECRET, body)
    assert webhook.verify_signature(SECRET, body, header) is True


def test_verify_signature_rejects_a_wrong_secret():
    body = b'{"hello": "world"}'
    header = _sign("wrong-secret", body)
    assert webhook.verify_signature(SECRET, body, header) is False


def test_verify_signature_rejects_a_tampered_body():
    body = b'{"hello": "world"}'
    header = _sign(SECRET, body)
    tampered = b'{"hello": "world!"}'
    assert webhook.verify_signature(SECRET, tampered, header) is False


def test_verify_signature_rejects_an_absent_header():
    assert webhook.verify_signature(SECRET, b"x", None) is False
    assert webhook.verify_signature(SECRET, b"x", "") is False


def test_verify_signature_rejects_a_malformed_header():
    body = b"x"
    assert webhook.verify_signature(SECRET, body, "not-a-signature") is False
    assert webhook.verify_signature(SECRET, body, "sha1=deadbeef") is False


def test_verify_signature_fails_closed_when_no_secret_is_configured():
    body = b"x"
    header = _sign("", body)
    assert webhook.verify_signature("", body, header) is False


# ── `STIGMERGY_GITHUB_WEBHOOK_FILE_CAP` parsing ──────────────────────────────────────────────────
def test_webhook_settings_file_cap_zero_is_invalid_not_a_defer_everything_cap(caplog):
    """`"0".isdigit()` is `True`, so the OLD parsing read a cap of exactly 0 — `len(changes) >
    file_cap` is then true for ANY non-empty push, deferring every single one to the nightly
    rebuild forever. `0` (and any non-positive value) must be treated as INVALID configuration,
    falling back to `DEFAULT_FILE_CAP` with a logged warning, exactly like a non-numeric value."""
    with caplog.at_level(logging.WARNING):
        settings = webhook.webhook_settings_from_env({webhook.WEBHOOK_FILE_CAP_ENV: "0"})
    assert settings.file_cap == webhook.DEFAULT_FILE_CAP
    assert any("file" in r.message.lower() for r in caplog.records)


def test_webhook_settings_file_cap_negative_is_invalid(caplog):
    with caplog.at_level(logging.WARNING):
        settings = webhook.webhook_settings_from_env({webhook.WEBHOOK_FILE_CAP_ENV: "-5"})
    assert settings.file_cap == webhook.DEFAULT_FILE_CAP


def test_webhook_settings_file_cap_non_numeric_falls_back_with_a_warning(caplog):
    with caplog.at_level(logging.WARNING):
        settings = webhook.webhook_settings_from_env({webhook.WEBHOOK_FILE_CAP_ENV: "not-a-number"})
    assert settings.file_cap == webhook.DEFAULT_FILE_CAP
    assert any("file" in r.message.lower() for r in caplog.records)


def test_webhook_settings_file_cap_a_valid_positive_value_is_used():
    settings = webhook.webhook_settings_from_env({webhook.WEBHOOK_FILE_CAP_ENV: "7"})
    assert settings.file_cap == 7


def test_webhook_settings_file_cap_absent_uses_the_default_with_no_warning(caplog):
    with caplog.at_level(logging.WARNING):
        settings = webhook.webhook_settings_from_env({})
    assert settings.file_cap == webhook.DEFAULT_FILE_CAP
    assert caplog.records == []   # absent is normal, not a misconfiguration — no warning


def test_changed_paths_from_push_nets_a_path_touched_twice_to_its_last_state():
    payload = _push_payload(commits=[
        {"added": ["wiki/a.md"], "modified": [], "removed": []},
        {"added": [], "modified": [], "removed": ["wiki/a.md"]},
    ])
    assert webhook.changed_paths_from_push(payload) == {"wiki/a.md": "removed"}


def test_in_zone_changes_drops_paths_outside_the_indexed_zones():
    changes = {"wiki/a.md": "added", "ops/acl.json": "modified",
              "meta/notes.md": "added", "datasets/x.csv": "added",
              "sources/general/b.md": "modified", "views/c.md": "removed"}
    assert webhook.in_zone_changes(changes) == {
        "wiki/a.md": "added", "sources/general/b.md": "modified",
        "views/c.md": "removed"}


# ── process_push: the indexing behavior, with a faked GitHub Contents fetch ────────────────────
class _FakeGithubResponse:
    def __init__(self, body: bytes):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _fake_opener(file_contents: dict[str, str]):
    """`path -> raw text` at whatever sha the URL asks for (this fake ignores the sha, since the
    tests using it never change a path's content between two different shas)."""
    def opener(request, timeout=30):
        parsed = urllib.parse.urlparse(request.full_url)
        marker = "/contents/"
        path = urllib.parse.unquote(parsed.path.split(marker, 1)[1])
        if path not in file_contents:
            raise urllib.error.HTTPError(request.full_url, 404, "Not Found", {}, None)
        return _FakeGithubResponse(file_contents[path].encode("utf-8"))
    return opener


@pytest.fixture(autouse=True)
def _fake_installation_token(monkeypatch):
    """No real App, no real JWT: `installation_token()` is a fixed string for every test in this
    module — the same posture `test_githubapp.py`'s own `opener=` seam takes, one level up (this
    module never even reaches `installation_token`'s OWN network call, since the function itself
    is replaced, not merely its opener)."""
    monkeypatch.setattr("stigmergy.librarian.githubapp.installation_token", lambda *a, **kw: "fake-token")


@pytest.fixture(scope="module")
def webhook_conn(tmp_path_factory):
    """Its own tiny corpus, built once — never assumes another test FILE happened to run first
    and left `pages_index` populated (pytest's collection order is not a contract this suite
    should depend on). Every test in this module queries by its OWN specific `path`, never a
    total row count, so sharing this table with whatever else is in it is safe."""
    from stigmergy.index import build
    from stigmergy.index.backends.embedder import build_embedder
    from tests import testdb

    conn = testdb.connect_or_skip("index")
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('pages_index')")
        already_built = cur.fetchone()[0] is not None
    if not already_built:
        root = str(tmp_path_factory.mktemp("webhook-seed-corpus"))
        seed = f"{root}/wiki/webhook-seed/seed.md"
        import os
        os.makedirs(os.path.dirname(seed), exist_ok=True)
        with open(seed, "w", encoding="utf-8") as f:
            f.write("---\ntitle: Webhook Seed\nverification: verified\n---\nSeed body.")
        build.rebuild(conn, root, build_embedder("fake"))
    yield conn
    conn.close()


def _settings(**kw) -> webhook.WebhookSettings:
    return webhook.WebhookSettings(secret=SECRET, repo=REPO, branch=BRANCH, **kw)


def test_process_push_upserts_a_new_page(webhook_conn):
    from stigmergy.index.backends.embedder import build_embedder
    path = "wiki/webhook-test/new-page.md"
    text = "---\ntitle: Webhook New Page\nentity: webhooktest\nverification: verified\n---\nBody one."
    payload = _push_payload(commits=[{"added": [path], "modified": [], "removed": []}])
    opener = _fake_opener({path: text})

    stats = webhook.process_push(webhook_conn, build_embedder("fake"), payload, _settings(),
                                 opener=opener)

    assert stats["upserted"] == 1
    assert stats["deleted"] == 0
    with webhook_conn.cursor() as cur:
        cur.execute("SELECT title FROM pages_index WHERE path = %s", (path,))
        assert cur.fetchone()[0] == "Webhook New Page"
    store.delete_pages(webhook_conn, [path])   # leave the shared database clean


# ── rebuild-resolution and webhook-resolution agree on `links` ─────────────────────────────────
def test_webhook_link_resolution_matches_a_full_rebuild_on_the_same_corpus(webhook_conn, tmp_path):
    """Two resolution code paths (`corpus.load_pages`'s in-memory whole-corpus walk, and this
    module's `_resolve_outbound_links`'s one query against `pages_index`'s existing paths) must
    produce IDENTICAL `links` for the same corpus content — the standing risk of keeping two
    stem-resolution code paths at all. Both share `corpus.resolve_links`/`by_stem_index`, so this
    test proves that reuse holds end to end rather than assuming it from the shared import
    alone."""
    from stigmergy.index import corpus
    from stigmergy.index.backends.embedder import build_embedder

    target = "wiki/webhook-test/parity-target.md"
    linker = "wiki/webhook-test/parity-linker.md"
    target_text = "---\ntitle: Parity Target\nverification: verified\n---\nTarget body.\n"
    linker_text = ("---\ntitle: Parity Linker\nverification: verified\n---\n"
                  "Links to [[parity-target]] for the resolution parity check.\n")
    embedder = build_embedder("fake")

    # first push: both files land together (target not yet in `pages_index` when links resolve,
    # matching the documented "same-push siblings don't resolve to each other yet" gap).
    add_payload = _push_payload(
        commits=[{"added": [target, linker], "modified": [], "removed": []}])
    webhook.process_push(webhook_conn, embedder, add_payload, _settings(),
                         opener=_fake_opener({target: target_text, linker: linker_text}))

    # second push: edit `linker` only — by now `target` IS in `pages_index`, so THIS is the
    # resolution that matters: a single changed file's stems against the table's existing paths.
    edited_text = linker_text.replace("resolution parity check", "resolution parity check, edited")
    edit_payload = _push_payload(commits=[{"added": [], "modified": [linker], "removed": []}])
    webhook.process_push(webhook_conn, embedder, edit_payload, _settings(),
                         opener=_fake_opener({linker: edited_text}))

    with webhook_conn.cursor() as cur:
        cur.execute("SELECT links FROM pages_index WHERE path = %s", (linker,))
        webhook_links = cur.fetchone()[0]

    # ground truth: a full rebuild's in-memory resolution over the identical two files
    root = tmp_path / "repo"
    kdir = root / "wiki" / "webhook-test"
    kdir.mkdir(parents=True)
    (kdir / "parity-target.md").write_text(target_text)
    (kdir / "parity-linker.md").write_text(edited_text)
    rebuilt = {r.path: r for r in corpus.load_pages(str(root))}

    assert webhook_links == rebuilt[linker].links == [target]

    store.delete_pages(webhook_conn, [target, linker])   # leave the shared database clean


# ── the webhook window for chain siblings ──────────────────────────────────────────────────────
def test_process_push_stamping_the_primarys_superseded_by_propagates_to_an_indexed_sibling(
        webhook_conn):
    """Nothing reconstructs supersession at query time any more, so a push that stamps
    `superseded_by` on a split doc's PRIMARY must propagate to its already-indexed `#p<n>`
    siblings itself. The sibling here is NOT part of the second push at all; only the primary
    is."""
    from stigmergy.index.backends.embedder import build_embedder
    embedder = build_embedder("fake")
    primary_path = "wiki/webhook-test/chain-primary.md"
    part_path = "wiki/webhook-test/chain-part2.md"
    primary_text = ("---\nid: drive:primary-doc\ntitle: Primary Report\n"
                    "verification: verified\n---\nPrimary body.")
    part_text = ("---\nid: drive:primary-doc#p2\ntitle: Second Section\n"
                "verification: verified\n---\nPart two body.")
    seed_payload = _push_payload(
        commits=[{"added": [primary_path, part_path], "modified": [], "removed": []}])
    webhook.process_push(webhook_conn, embedder, seed_payload, _settings(),
                         opener=_fake_opener({primary_path: primary_text, part_path: part_text}))
    with webhook_conn.cursor() as cur:
        cur.execute("SELECT superseded_by FROM pages_index WHERE path = %s", (part_path,))
        assert cur.fetchone()[0] == ""   # sanity: unsuperseded before the stamping push

    stamped_primary_text = primary_text.replace(
        "id: drive:primary-doc\n", "id: drive:primary-doc\nsuperseded_by: drive:primary-doc-v2\n")
    stamp_payload = _push_payload(
        commits=[{"added": [], "modified": [primary_path], "removed": []}])
    webhook.process_push(webhook_conn, embedder, stamp_payload, _settings(),
                         opener=_fake_opener({primary_path: stamped_primary_text}))

    with webhook_conn.cursor() as cur:
        cur.execute("SELECT superseded_by FROM pages_index WHERE path = %s", (part_path,))
        # the sibling's row used to be left untouched by a push that never named it — stale at
        # the old value until the nightly rebuild came round.
        assert cur.fetchone()[0] == "drive:primary-doc-v2"

    store.delete_pages(webhook_conn, [primary_path, part_path])   # leave the shared database clean


def test_process_push_clearing_the_primarys_superseded_by_clears_the_sibling_too(webhook_conn):
    """The symmetric direction: a push that REMOVES the primary's `superseded_by` must clear the
    sibling's stale value too, not just stamp it — propagation is not merely "copy a truthy value
    once"."""
    from stigmergy.index.backends.embedder import build_embedder
    embedder = build_embedder("fake")
    primary_path = "wiki/webhook-test/clearing-primary.md"
    part_path = "wiki/webhook-test/clearing-part2.md"
    superseded_primary_text = (
        "---\nid: drive:clearing-doc\nsuperseded_by: drive:clearing-doc-v2\n"
        "title: Clearing Primary\nverification: verified\n---\nPrimary body.")
    part_text = ("---\nid: drive:clearing-doc#p2\ntitle: Clearing Second Section\n"
                "verification: verified\n---\nPart two body.")
    seed_payload = _push_payload(
        commits=[{"added": [primary_path, part_path], "modified": [], "removed": []}])
    webhook.process_push(webhook_conn, embedder, seed_payload, _settings(),
                         opener=_fake_opener({primary_path: superseded_primary_text,
                                              part_path: part_text}))
    with webhook_conn.cursor() as cur:
        cur.execute("SELECT superseded_by FROM pages_index WHERE path = %s", (part_path,))
        assert cur.fetchone()[0] == "drive:clearing-doc-v2"   # sanity: propagated on the seed push

    cleared_primary_text = (
        "---\nid: drive:clearing-doc\ntitle: Clearing Primary\nverification: verified\n---\n"
        "Primary body, un-superseded.")
    clear_payload = _push_payload(
        commits=[{"added": [], "modified": [primary_path], "removed": []}])
    webhook.process_push(webhook_conn, embedder, clear_payload, _settings(),
                         opener=_fake_opener({primary_path: cleared_primary_text}))

    with webhook_conn.cursor() as cur:
        cur.execute("SELECT superseded_by FROM pages_index WHERE path = %s", (part_path,))
        assert cur.fetchone()[0] == ""   # cleared, not left stale at the old value

    store.delete_pages(webhook_conn, [primary_path, part_path])   # leave the shared database clean


def test_process_push_deletes_a_removed_page(webhook_conn):
    from stigmergy.index.backends.embedder import build_embedder
    embedder = build_embedder("fake")
    path = "wiki/webhook-test/to-delete.md"
    text = "---\ntitle: To Delete\nentity: webhooktest\nverification: verified\n---\nBody."
    add_payload = _push_payload(commits=[{"added": [path], "modified": [], "removed": []}])
    webhook.process_push(webhook_conn, embedder, add_payload, _settings(),
                         opener=_fake_opener({path: text}))
    with webhook_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM pages_index WHERE path = %s", (path,))
        assert cur.fetchone()[0] == 1

    remove_payload = _push_payload(commits=[{"added": [], "modified": [], "removed": [path]}])
    stats = webhook.process_push(webhook_conn, embedder, remove_payload, _settings())

    assert stats["deleted"] == 1
    with webhook_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM pages_index WHERE path = %s", (path,))
        assert cur.fetchone()[0] == 0


def test_process_push_same_content_pushed_twice_embeds_once(webhook_conn, monkeypatch):
    """The content_hash skip: a second push with IDENTICAL content must not call the embedder
    again.

    `embedding_cache` is a SURVIVING table (`index/store.py`'s own docstring: never dropped by a
    rebuild), so a title/body pair reused verbatim across repeated runs of this same test — in
    the same database, across separate `pytest` invocations — would find its hash already cached
    from a PREVIOUS run and report `embedded == 0` on the very FIRST push too. A `uuid` in the
    body keeps this test's content_hash unique to this one run, which is what makes `embedded ==
    1` on the first push a real assertion rather than a coincidence of test history.
    """
    import uuid

    from stigmergy.index.backends.embedder import build_embedder
    embedder = build_embedder("fake")
    path = "wiki/webhook-test/stable.md"
    text = (f"---\ntitle: Stable Page\nentity: webhooktest\nverification: verified\n---\n"
           f"Unchanging body, marker {uuid.uuid4()}.")
    payload = _push_payload(commits=[{"added": [path], "modified": [], "removed": []}])

    stats1 = webhook.process_push(webhook_conn, embedder, payload, _settings(),
                                  opener=_fake_opener({path: text}))
    assert stats1["embedded"] == 1
    assert stats1["skipped"] == 0

    calls = []
    real_embed = embedder.embed

    def counting_embed(texts):
        calls.append(texts)
        return real_embed(texts)
    monkeypatch.setattr(embedder, "embed", counting_embed)

    stats2 = webhook.process_push(webhook_conn, embedder, payload, _settings(),
                                  opener=_fake_opener({path: text}))
    assert stats2["embedded"] == 0
    assert stats2["skipped"] == 1
    assert calls == []          # the embedder was never called a second time for this content

    store.delete_pages(webhook_conn, [path])


def test_process_push_above_the_file_cap_indexes_nothing(webhook_conn):
    from stigmergy.index.backends.embedder import build_embedder
    paths = [f"wiki/webhook-test/cap-{i}.md" for i in range(5)]
    payload = _push_payload(commits=[{"added": paths, "modified": [], "removed": []}])
    contents = {p: f"---\ntitle: Cap {i}\nverification: verified\n---\nBody {i}."
               for i, p in enumerate(paths)}

    stats = webhook.process_push(webhook_conn, build_embedder("fake"), payload,
                                 _settings(file_cap=2), opener=_fake_opener(contents))

    assert stats["deferred"] is True
    assert stats["changed_files"] == 5
    assert stats["upserted"] == 0 and stats["deleted"] == 0
    for path in paths:
        with webhook_conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM pages_index WHERE path = %s", (path,))
            assert cur.fetchone()[0] == 0


# ── a mid-run failure must not half-apply a push ───────────────────────────────────────────────
def test_process_push_rolls_back_a_deletion_when_the_upsert_side_fails_mid_run(webhook_conn, monkeypatch):
    """`delete_pages` used to commit independently of the upsert side (each of `store.
    delete_pages`/`store.store_embeddings`/`store.upsert_pages` opened its OWN transaction) — a
    failure partway through (embed, upsert) left the deletion applied and the upsert lost, exactly
    the "half-applied bulk change is worse than a stale one" the file-cap path already refuses
    wholesale for. A rename (`a.md` -> `b.md`) that fails mid-run must lose NEITHER page, not both:
    proven here by forcing `store.upsert_pages` to raise and asserting the delete this same push
    also carried was rolled back with it."""
    from stigmergy.index.backends.embedder import build_embedder
    embedder = build_embedder("fake")
    victim_path = "wiki/webhook-test/rollback-victim.md"
    victim_text = "---\ntitle: Rollback Victim\nverification: verified\n---\nBody."
    seed_payload = _push_payload(commits=[{"added": [victim_path], "modified": [], "removed": []}])
    webhook.process_push(webhook_conn, embedder, seed_payload, _settings(),
                         opener=_fake_opener({victim_path: victim_text}))
    with webhook_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM pages_index WHERE path = %s", (victim_path,))
        assert cur.fetchone()[0] == 1   # sanity: the page really is there before the failing push

    new_path = "wiki/webhook-test/rollback-new.md"
    new_text = "---\ntitle: Rollback New\nverification: verified\n---\nBody."
    push_payload = _push_payload(
        commits=[{"added": [new_path], "modified": [], "removed": [victim_path]}])

    def boom(*args, **kwargs):
        raise RuntimeError("simulated upsert failure mid-run")
    monkeypatch.setattr(store, "upsert_pages", boom)

    with pytest.raises(RuntimeError, match="simulated upsert failure"):
        webhook.process_push(webhook_conn, embedder, push_payload, _settings(),
                             opener=_fake_opener({new_path: new_text}))

    with webhook_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM pages_index WHERE path = %s", (victim_path,))
        assert cur.fetchone()[0] == 1   # the deletion was ROLLED BACK, not half-applied
        cur.execute("SELECT count(*) FROM pages_index WHERE path = %s", (new_path,))
        assert cur.fetchone()[0] == 0   # and the new page never landed either

    store.delete_pages(webhook_conn, [victim_path])   # leave the shared database clean


def test_process_push_ignores_paths_outside_the_indexed_zones(webhook_conn):
    from stigmergy.index.backends.embedder import build_embedder
    payload = _push_payload(commits=[{"added": ["ops/acl.json", "meta/x.md"],
                                      "modified": [], "removed": []}])
    stats = webhook.process_push(webhook_conn, build_embedder("fake"), payload, _settings())
    assert stats == {"sha": "abc123", "upserted": 0, "deleted": 0, "skipped": 0, "embedded": 0}


# ── the ASGI/HTTP level: HMAC, the middleware exemption, event/branch/repo filtering ────────────
@pytest.fixture()
def webhook_env(monkeypatch):
    monkeypatch.setenv(webhook.WEBHOOK_SECRET_ENV, SECRET)
    monkeypatch.setenv(webhook.WEBHOOK_REPO_ENV, REPO)
    monkeypatch.setenv(webhook.WEBHOOK_BRANCH_ENV, BRANCH)


def test_a_valid_signature_on_an_ignored_event_still_returns_200(fixture, webhook_env):
    """The benign twin: a WELL-FORMED, correctly-signed request that this endpoint has no work to
    do for still succeeds — GitHub disables endpoints that fail, so 'nothing to do' must never
    look like an error."""
    app = build_test_http_app(fixture, {})
    with run_http_server(app) as url:
        base = url.rsplit("/", 1)[0]
        body = json.dumps(_push_payload(commits=[])).encode()
        headers = {"X-Hub-Signature-256": _sign(SECRET, body),
                  "X-GitHub-Event": "push", "Content-Type": "application/json"}
        resp = httpx.post(f"{base}{webhook.WEBHOOK_PATH}", content=body, headers=headers)
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_an_absent_signature_gets_the_generic_401(fixture, webhook_env):
    app = build_test_http_app(fixture, {})
    with run_http_server(app) as url:
        base = url.rsplit("/", 1)[0]
        body = json.dumps(_push_payload()).encode()
        resp = httpx.post(f"{base}{webhook.WEBHOOK_PATH}", content=body,
                          headers={"X-GitHub-Event": "push"})
    assert resp.status_code == 401
    assert resp.json() == {"error": "unauthorized"}


def test_a_wrong_signature_gets_the_generic_401(fixture, webhook_env):
    app = build_test_http_app(fixture, {})
    with run_http_server(app) as url:
        base = url.rsplit("/", 1)[0]
        body = json.dumps(_push_payload()).encode()
        headers = {"X-Hub-Signature-256": _sign("wrong-secret", body), "X-GitHub-Event": "push"}
        resp = httpx.post(f"{base}{webhook.WEBHOOK_PATH}", content=body, headers=headers)
    assert resp.status_code == 401
    assert resp.json() == {"error": "unauthorized"}


def test_a_tampered_body_gets_the_generic_401(fixture, webhook_env):
    app = build_test_http_app(fixture, {})
    with run_http_server(app) as url:
        base = url.rsplit("/", 1)[0]
        body = json.dumps(_push_payload()).encode()
        signature = _sign(SECRET, body)
        tampered = body + b" "
        resp = httpx.post(f"{base}{webhook.WEBHOOK_PATH}", content=tampered,
                          headers={"X-Hub-Signature-256": signature, "X-GitHub-Event": "push"})
    assert resp.status_code == 401
    assert resp.json() == {"error": "unauthorized"}


def test_the_401_body_is_byte_identical_to_every_other_auth_failures_body(fixture, webhook_env):
    """The SAME generic 401 body every other auth failure returns — checked against a bearer-auth
    failure on an ordinary MCP route, not merely against this module's own constant (which could
    drift from `transport_http`'s independently)."""
    app = build_test_http_app(fixture, {})
    with run_http_server(app) as url:
        base = url.rsplit("/", 1)[0]
        webhook_resp = httpx.post(f"{base}{webhook.WEBHOOK_PATH}", content=b"{}",
                                  headers={"X-GitHub-Event": "push"})
        mcp_resp = httpx.post(url, content=b"{}",
                              headers={"Content-Type": "application/json"})
    assert webhook_resp.status_code == mcp_resp.status_code == 401
    assert webhook_resp.json() == mcp_resp.json() == {"error": "unauthorized"}


@pytest.mark.parametrize("event", ["ping", "pull_request", "issues"])
def test_a_non_push_event_returns_200_and_ignores(fixture, webhook_env, event):
    app = build_test_http_app(fixture, {})
    with run_http_server(app) as url:
        base = url.rsplit("/", 1)[0]
        body = json.dumps(_push_payload()).encode()
        headers = {"X-Hub-Signature-256": _sign(SECRET, body), "X-GitHub-Event": event}
        resp = httpx.post(f"{base}{webhook.WEBHOOK_PATH}", content=body, headers=headers)
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_a_push_to_another_branch_returns_200_and_ignores(fixture, webhook_env):
    app = build_test_http_app(fixture, {})
    with run_http_server(app) as url:
        base = url.rsplit("/", 1)[0]
        body = json.dumps(_push_payload(ref="refs/heads/some-feature-branch")).encode()
        headers = {"X-Hub-Signature-256": _sign(SECRET, body), "X-GitHub-Event": "push"}
        resp = httpx.post(f"{base}{webhook.WEBHOOK_PATH}", content=body, headers=headers)
    assert resp.status_code == 200
    assert resp.json()["ignored"]


def test_a_push_to_another_repository_returns_200_and_ignores(fixture, webhook_env):
    app = build_test_http_app(fixture, {})
    with run_http_server(app) as url:
        base = url.rsplit("/", 1)[0]
        body = json.dumps(_push_payload(repo_full_name="someone-else/unrelated")).encode()
        headers = {"X-Hub-Signature-256": _sign(SECRET, body), "X-GitHub-Event": "push"}
        resp = httpx.post(f"{base}{webhook.WEBHOOK_PATH}", content=body, headers=headers)
    assert resp.status_code == 200
    assert resp.json()["ignored"]


# ── the exemption is EXACT, every other route still requires a bearer token ────────────────────
def test_every_other_route_still_requires_a_bearer_token(fixture, webhook_env):
    """Driving every OTHER route without a token gets 401 — the exemption is for
    `/webhook/github` and nothing else, checked here against the MCP endpoint itself."""
    app = build_test_http_app(fixture, {})
    with run_http_server(app) as url:
        resp = httpx.post(url, content=b"{}", headers={"Content-Type": "application/json"})
    assert resp.status_code == 401


def test_a_prefix_of_the_webhook_path_is_not_exempt(fixture, webhook_env):
    """The exemption is an EXACT path match, never a prefix."""
    app = build_test_http_app(fixture, {})
    with run_http_server(app) as url:
        base = url.rsplit("/", 1)[0]
        resp = httpx.post(f"{base}{webhook.WEBHOOK_PATH}/extra", content=b"{}")
    assert resp.status_code in (401, 404)   # never 200 — either unauthenticated-refused or unrouted
    if resp.status_code == 401:
        assert resp.json() == {"error": "unauthorized"}


# ── unbounded body buffering on the one unauthenticated path ───────────────────────────────────
class _FakeAsyncStream:
    def __init__(self, chunks: list[bytes]):
        self._chunks = chunks

    async def __aiter__(self):
        for chunk in self._chunks:
            yield chunk


class _FakeStreamingRequest:
    """Just enough of Starlette's `Request` for `_read_body_capped` — it only ever calls
    `.stream()`, so nothing else needs faking."""

    def __init__(self, chunks: list[bytes]):
        self._chunks = chunks

    def stream(self):
        return _FakeAsyncStream(self._chunks)


def test_read_body_capped_returns_the_full_body_when_under_the_cap():
    import asyncio
    request = _FakeStreamingRequest([b"12345", b"67890"])
    assert asyncio.run(webhook._read_body_capped(request, max_bytes=100)) == b"1234567890"


def test_read_body_capped_returns_none_without_buffering_past_the_cap():
    """Streamed, not trusting `Content-Length` alone: the cap is enforced against bytes ACTUALLY
    read off the stream, chunk by chunk — proven here with a fake stream that carries no
    Content-Length at all."""
    import asyncio
    request = _FakeStreamingRequest([b"12345", b"67890", b"ABCDE"])   # 15 bytes total
    assert asyncio.run(webhook._read_body_capped(request, max_bytes=10)) is None


def test_an_oversized_body_gets_the_generic_401(fixture, webhook_env, monkeypatch):
    """The hard cap (26 MB in production; patched tiny here for test speed) refuses BEFORE HMAC
    verification ever runs — an oversized, entirely UNSIGNED body must look identical to any other
    401, never a distinct error that would tell an attacker their body was merely too big."""
    monkeypatch.setattr(webhook, "MAX_BODY_BYTES", 16)
    app = build_test_http_app(fixture, {})
    with run_http_server(app) as url:
        base = url.rsplit("/", 1)[0]
        body = b"x" * 64   # well over the patched cap
        resp = httpx.post(f"{base}{webhook.WEBHOOK_PATH}", content=body,
                          headers={"X-GitHub-Event": "push"})
    assert resp.status_code == 401
    assert resp.json() == {"error": "unauthorized"}


def test_an_oversized_but_correctly_signed_body_is_still_refused(fixture, webhook_env, monkeypatch):
    """Refusing on size happens BEFORE signature verification — even a body that WOULD have
    verified correctly is refused for its size alone, so a correctly-signed oversized push and an
    unsigned oversized push are indistinguishable (same body-size check, same generic 401)."""
    monkeypatch.setattr(webhook, "MAX_BODY_BYTES", 16)
    app = build_test_http_app(fixture, {})
    body = json.dumps(_push_payload(commits=[])).encode()   # comfortably over 16 bytes
    with run_http_server(app) as url:
        base = url.rsplit("/", 1)[0]
        headers = {"X-Hub-Signature-256": _sign(SECRET, body), "X-GitHub-Event": "push"}
        resp = httpx.post(f"{base}{webhook.WEBHOOK_PATH}", content=body, headers=headers)
    assert resp.status_code == 401
    assert resp.json() == {"error": "unauthorized"}


def test_a_body_at_or_under_the_cap_is_unaffected(fixture, webhook_env, monkeypatch):
    """The cap must not be off-by-one in the WRONG direction — an ordinary, correctly-signed,
    well-under-the-cap request still succeeds."""
    monkeypatch.setattr(webhook, "MAX_BODY_BYTES", 10_000)
    app = build_test_http_app(fixture, {})
    body = json.dumps(_push_payload(commits=[])).encode()
    with run_http_server(app) as url:
        base = url.rsplit("/", 1)[0]
        headers = {"X-Hub-Signature-256": _sign(SECRET, body), "X-GitHub-Event": "push"}
        resp = httpx.post(f"{base}{webhook.WEBHOOK_PATH}", content=body, headers=headers)
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


# ── capture -> searchable WITHOUT a rebuild ────────────────────────────────────────────────────
def test_a_page_the_webhook_indexes_is_searchable_immediately_no_rebuild(webhook_conn):
    """A commit landing in the knowledge repo is answerable by `ask` without a rebuild. The
    structural half of that promise is that `BrainService.search` reads `pages_index` live (no
    cache, `server/service.py`'s own docstring), so a page `process_push` just upserted is
    findable in the SAME process, in the SAME test, with no `stigmergy-index --rebuild` in
    between."""
    import uuid

    from stigmergy.index.backends.embedder import build_embedder
    from stigmergy.server.service import BrainService
    from stigmergy.server.settings import Settings

    marker = f"webhook-searchable-{uuid.uuid4()}"
    path = "wiki/webhook-test/searchable.md"
    text = (f"---\ntitle: Webhook Searchable Page\nentity: webhooktest\n"
           f"verification: verified\n---\nBody mentioning {marker} for retrieval.")
    payload = _push_payload(commits=[{"added": [path], "modified": [], "removed": []}])

    stats = webhook.process_push(webhook_conn, build_embedder("fake"), payload, _settings(),
                                 opener=_fake_opener({path: text}))
    assert stats["upserted"] == 1

    settings = Settings(llm="fake")
    svc = BrainService(settings, webhook_conn, build_embedder("fake"), audiences=None)
    result = svc.search(marker)
    assert any(h["path"] == path for h in result["hits"])

    store.delete_pages(webhook_conn, [path])


def test_a_processed_push_writes_a_job_runs_row(fixture, webhook_env):
    """Every invocation writes a `job_runs` row — asserted against the real endpoint, not just
    `process_push` in isolation."""
    from stigmergy.capture.schema import ensure_capture_schema

    app = build_test_http_app(fixture, {})
    with run_http_server(app) as url:
        base = url.rsplit("/", 1)[0]
        body = json.dumps(_push_payload(commits=[])).encode()
        headers = {"X-Hub-Signature-256": _sign(SECRET, body), "X-GitHub-Event": "push"}
        resp = httpx.post(f"{base}{webhook.WEBHOOK_PATH}", content=body, headers=headers)
    assert resp.status_code == 200

    from tests import testdb
    conn = testdb.connect_or_skip("index")
    ensure_capture_schema(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT stats FROM job_runs WHERE job = %s ORDER BY id DESC LIMIT 1",
                    (webhook.JOB_NAME,))
        row = cur.fetchone()
    assert row is not None
    assert row[0]["sha"] == "abc123"


# ── the two behaviours below, driven through the REAL endpoint (not `process_push` called
# directly, which every test above this section already covers) ────────────────────────────────
def test_an_over_cap_push_through_the_real_endpoint_marks_job_runs_deferred(
        webhook_conn, fixture, webhook_env, monkeypatch):
    """Post an over-cap push through the real endpoint and read back
    `job_runs.stats->>'deferred'` — end-to-end, where the file-cap test above calls
    `process_push` directly."""
    monkeypatch.setenv(webhook.WEBHOOK_FILE_CAP_ENV, "1")
    app = build_test_http_app(fixture, {})
    paths = [f"wiki/webhook-test/over-cap-http-{i}.md" for i in range(3)]
    payload = _push_payload(commits=[{"added": paths, "modified": [], "removed": []}])
    body = json.dumps(payload).encode()

    with run_http_server(app) as url:
        base = url.rsplit("/", 1)[0]
        headers = {"X-Hub-Signature-256": _sign(SECRET, body), "X-GitHub-Event": "push"}
        resp = httpx.post(f"{base}{webhook.WEBHOOK_PATH}", content=body, headers=headers)

    assert resp.status_code == 200
    assert resp.json().get("deferred") is True

    with webhook_conn.cursor() as cur:
        cur.execute("SELECT stats FROM job_runs WHERE job = %s ORDER BY id DESC LIMIT 1",
                   (webhook.JOB_NAME,))
        stats = cur.fetchone()[0]
    assert stats.get("deferred") is True
    for path in paths:
        with webhook_conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM pages_index WHERE path = %s", (path,))
            assert cur.fetchone()[0] == 0   # deferred — none of the over-cap paths were indexed


def test_an_out_of_zone_only_push_through_the_real_endpoint_leaves_pages_index_unchanged(
        webhook_conn, fixture, webhook_env):
    """Drive a push touching ONLY out-of-zone paths through the real endpoint and assert 200 with
    `pages_index` unchanged — at the HTTP level, where the zone-filter test above calls
    `process_push` directly."""
    app = build_test_http_app(fixture, {})
    payload = _push_payload(commits=[{"added": ["ops/acl.json", "meta/notes.md",
                                                "datasets/raw.csv"],
                                      "modified": [], "removed": []}])
    body = json.dumps(payload).encode()

    with webhook_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM pages_index")
        before = cur.fetchone()[0]

    with run_http_server(app) as url:
        base = url.rsplit("/", 1)[0]
        headers = {"X-Hub-Signature-256": _sign(SECRET, body), "X-GitHub-Event": "push"}
        resp = httpx.post(f"{base}{webhook.WEBHOOK_PATH}", content=body, headers=headers)

    assert resp.status_code == 200
    body_json = resp.json()
    assert body_json["upserted"] == 0 and body_json["deleted"] == 0

    with webhook_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM pages_index")
        after = cur.fetchone()[0]
    assert after == before   # pages_index is byte-for-byte unchanged: not one row touched


# ── the LIVE `-p<n>` convention propagates; directory-gated ────────────────────────────────────
def test_process_push_propagates_to_live_stem_convention_parts(webhook_conn):
    """The meeting flow's splitter writes id-less `<stem>-p<n>.md` files (page_id = suffixed
    stem), and the prefetch used to narrow to `#p` alone — which left this path inert over every
    part the system actually produces. A push stamping the primary must reach a `-p2` sibling
    too."""
    from stigmergy.index.backends.embedder import build_embedder
    embedder = build_embedder("fake")
    primary_path = "sources/webhook-test/split-transcript.md"
    part_path = "sources/webhook-test/split-transcript-p2.md"
    primary_text = "---\ntitle: Split Transcript\nverification: verified\n---\nPart one body."
    part_text = "---\ntitle: Split Transcript p2\nverification: verified\n---\nPart two body."
    seed = _push_payload(commits=[{"added": [primary_path, part_path], "modified": [],
                                   "removed": []}])
    webhook.process_push(webhook_conn, embedder, seed, _settings(),
                         opener=_fake_opener({primary_path: primary_text, part_path: part_text}))

    stamped = primary_text.replace(
        "---\ntitle:", "---\nsuperseded_by: split-transcript-v2\ntitle:", 1)
    stamp = _push_payload(commits=[{"added": [], "modified": [primary_path], "removed": []}])
    webhook.process_push(webhook_conn, embedder, stamp, _settings(),
                         opener=_fake_opener({primary_path: stamped}))

    with webhook_conn.cursor() as cur:
        cur.execute("SELECT superseded_by FROM pages_index WHERE path = %s", (part_path,))
        assert cur.fetchone()[0] == "split-transcript-v2"

    store.delete_pages(webhook_conn, [primary_path, part_path])


def test_process_push_never_propagates_across_directories(webhook_conn):
    """An id-less `-p2`-stemmed page in ANOTHER directory shares the chain base by stem fallback
    — it must never inherit a supersession from a primary it never belonged to. The directory
    gate mirrors `corpus.load_pages` and rank's collapse key."""
    from stigmergy.index.backends.embedder import build_embedder
    embedder = build_embedder("fake")
    primary_path = "wiki/webhook-a/quarterly-report.md"
    twin_path = "wiki/webhook-b/quarterly-report-p2.md"
    primary_text = "---\ntitle: Quarterly Report\nverification: verified\n---\nReport body."
    twin_text = "---\ntitle: Unrelated Note\nverification: verified\n---\nUnrelated body."
    seed = _push_payload(commits=[{"added": [primary_path, twin_path], "modified": [],
                                   "removed": []}])
    webhook.process_push(webhook_conn, embedder, seed, _settings(),
                         opener=_fake_opener({primary_path: primary_text, twin_path: twin_text}))

    stamped = primary_text.replace(
        "---\ntitle:", "---\nsuperseded_by: quarterly-report-v2\ntitle:", 1)
    stamp = _push_payload(commits=[{"added": [], "modified": [primary_path], "removed": []}])
    webhook.process_push(webhook_conn, embedder, stamp, _settings(),
                         opener=_fake_opener({primary_path: stamped}))

    with webhook_conn.cursor() as cur:
        cur.execute("SELECT superseded_by FROM pages_index WHERE path = %s", (twin_path,))
        assert cur.fetchone()[0] == ""   # the stranger keeps its own truth

    store.delete_pages(webhook_conn, [primary_path, twin_path])


def test_the_body_cap_is_sized_to_the_work_this_handler_does_not_to_githubs_ceiling():
    """It was 26 MB, sized against GitHub's 25 MB delivery ceiling — a bound on the wrong quantity.

    This is the one path on the public server exempt from bearer auth, on a 512 MB single-process
    machine, so the cap is the only thing between an anonymous caller and the memory of the process
    serving every authenticated identity. What the handler will ever ACT on is bounded by
    `DEFAULT_FILE_CAP` in-zone paths; a real push for a knowledge repo is kilobytes.
    """
    assert webhook.MAX_BODY_BYTES == 1024 * 1024
    # A realistic delivery — 50 changed paths, the cap `changed_paths_from_push` itself stops at —
    # has to fit with room to spare, or this bound would refuse real work.
    realistic = json.dumps({"ref": "refs/heads/main", "repository": {"full_name": "acme/knowledge"},
                            "commits": [{"added": [f"wiki/notes/page-{i}.md" for i in range(50)],
                                         "modified": [], "removed": []}]})
    assert len(realistic.encode()) * 4 < webhook.MAX_BODY_BYTES
