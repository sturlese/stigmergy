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

from stigmergy.index import corpus, store
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
    # `partition` splits on the FIRST `=`, so everything after it is the digest verbatim — a
    # second `sha256=` is part of the digest, not a second scheme to be re-parsed.
    assert webhook.verify_signature(SECRET, body, "sha256=sha256=x") is False


def test_verify_signature_fails_closed_when_no_secret_is_configured():
    body = b"x"
    header = _sign("", body)
    assert webhook.verify_signature("", body, header) is False


@pytest.mark.parametrize("digest", ["ÿÿÿÿÿÿÿÿ", "café", "夜", "\ud800"],
                         ids=["high-latin-1", "latin-1-accent", "beyond-latin-1", "lone-surrogate"])
def test_verify_signature_fails_closed_on_a_non_ascii_digest(digest):
    """OLD BEHAVIOUR: every one of these ESCAPED the function — `hmac.compare_digest` raises
    `TypeError` when either `str` argument holds a non-ASCII character.

    This is load-bearing rather than tidy. The contract is that this function NEVER raises: "every
    malformed shape fails closed to `False`, the same outcome a wrong digest gets, so there is no
    second, more-specific error an attacker could distinguish" — and a raised exception is exactly
    that second, more-specific outcome.

    The arms are deliberately of two kinds, because the promise is about every `str`, not only the
    reachable ones. `high-latin-1` is the REACHABLE shape: it is byte-for-byte what a real header
    of `\\xff` bytes becomes once Starlette decodes it as latin-1 (see `transport_http`'s own
    `.decode("latin-1")`), and it is what the HTTP-level test below sends over the wire.
    `beyond-latin-1` and `lone-surrogate` are NOT reachable through that decode — no byte maps
    above `U+00FF` — but they are legal `str` values a future or non-HTTP caller could pass, and
    the lone surrogate is the one that survives a naive `.encode("utf-8")` fix by raising
    `UnicodeEncodeError` instead. They are here to hold the promise as an absolute.
    """
    assert webhook.verify_signature(SECRET, b'{"hello": "world"}', f"sha256={digest}") is False


def test_verify_signature_still_accepts_a_valid_signature_compared_as_bytes():
    """The benign twin for the encode above: the real delivery must still verify, and a digest
    differing in one hex character must still be refused — the comparison has to keep MEANING the
    same thing, not merely stop raising."""
    body = b'{"hello": "world"}'
    good = _sign(SECRET, body)
    assert webhook.verify_signature(SECRET, body, good) is True

    flipped = good[:-1] + ("0" if good[-1] != "0" else "1")
    assert webhook.verify_signature(SECRET, body, flipped) is False


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


def test_a_raw_non_ascii_signature_header_gets_the_generic_401_not_a_500(fixture, webhook_env):
    """OLD BEHAVIOUR: eight raw bytes turned this endpoint's generic 401 into a 500.

    Header bytes >= 0x80 are legal on the wire and Starlette hands them to the handler as a
    latin-1 `str`, so `hmac.compare_digest` raised `TypeError` — which nothing here catches
    (`verify_signature` is called outside the endpoint's only `try`). The result was an
    unauthenticated, no-secret-knowledge way to get a distinguishable response out of the one
    public route on this server, defeating exactly the property
    `test_the_401_body_is_byte_identical_to_every_other_auth_failures_body` exists to hold.

    Asserted at the HTTP level on purpose: the unit test above pins the function, but only a real
    request proves the raw bytes survive the wire and reach it as the shape that used to crash.
    """
    app = build_test_http_app(fixture, {})
    with run_http_server(app) as url:
        base = url.rsplit("/", 1)[0]
        body = json.dumps(_push_payload()).encode()
        resp = httpx.post(f"{base}{webhook.WEBHOOK_PATH}", content=body,
                          headers={b"X-Hub-Signature-256": b"sha256=" + b"\xff" * 8,
                                   b"X-GitHub-Event": b"push"})

    assert resp.status_code == 401, resp.text
    assert resp.json() == {"error": "unauthorized"}


@pytest.mark.parametrize("body", [b"123", b"[]", b'"a string"', b"null"],
                         ids=["number", "list", "string", "null"])
def test_a_signed_body_that_is_not_a_json_object_is_ignored_not_a_500(fixture, webhook_env, body):
    """OLD BEHAVIOUR: a raw `500 Internal Server Error`.

    `json.loads` is guarded, but the guard ends at the parse: `payload.get("repository")` runs
    OUTSIDE it. Every one of these parses fine and is simply not a mapping, so `.get` raised
    `AttributeError` and escaped the handler — the same shape of gap as the signature crash above,
    one step further down the same function.

    This one needs the webhook secret to reach (GitHub itself, or an operator with the secret), so
    it is a robustness fault rather than the unauthenticated hole. It still matters: a 500 is what
    GitHub shows in its delivery log and what an operator would be asked to redeliver, for a body
    there was never anything to do with.

    The verdict matches the unparseable-body branch immediately above it, and for the same stated
    reason: a signature that verified over something that is not a GitHub delivery is nothing to
    act on, and never an error to GitHub.
    """
    app = build_test_http_app(fixture, {})
    with run_http_server(app) as url:
        base = url.rsplit("/", 1)[0]
        resp = httpx.post(f"{base}{webhook.WEBHOOK_PATH}", content=body,
                          headers={"X-Hub-Signature-256": _sign(SECRET, body),
                                   "X-GitHub-Event": "push"})

    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True


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


def test_the_webhook_indexes_exactly_the_population_the_full_rebuild_does(tmp_path):
    """OLD BEHAVIOUR: a push could upsert rows the full rebuild never produces — open to everyone.

    `in_zone_changes` filtered by zone PREFIX alone, while `corpus.load_pages` globs `*.md` and
    skips any dot-name. So `wiki/data.csv`, `wiki/.hidden.md` and `wiki/.obsidian/workspace.json`
    were fetched through the Contents API and written into `pages_index` — and `page_row` finds no
    frontmatter in them, so `acl` came out `None`, which is the OPEN value at
    `server.acl.visible()`. A spreadsheet export committed under `wiki/` became searchable and
    readable by every client until the nightly rebuild silently dropped the row again.

    The transience is the nasty part: the leak repairs itself overnight, so nobody catches it. And
    `.obsidian/` is not hypothetical — it lives inside the vault of exactly the editor this page
    format is written for.

    The two populations are asserted against each other rather than against a hand-written list,
    because "the same population" is the actual contract.
    """
    (tmp_path / "wiki").mkdir()
    (tmp_path / "wiki" / ".obsidian").mkdir()
    (tmp_path / "wiki" / "a.md").write_text("---\ntitle: A\n---\nbody\n", encoding="utf-8")
    (tmp_path / "wiki" / "data.csv").write_text("salary,amount\nceo,900000\n", encoding="utf-8")
    (tmp_path / "wiki" / ".hidden.md").write_text("---\ntitle: H\n---\nx\n", encoding="utf-8")
    (tmp_path / "wiki" / ".obsidian" / "workspace.json").write_text("{}", encoding="utf-8")
    # A `.md` page inside a DOT-DIRECTORY, which is the case the first attempt at this fix got
    # wrong in the other direction: `rglob("*.md")` DESCENDS into a dot-directory and only the
    # file's own name is checked, so the rebuild indexes this page. Excluding every dot-path
    # component here would have left the rebuild indexing it while its edits — and its DELETIONS —
    # stopped arriving, so a restricted page removed from the repo stays readable until the next
    # nightly rebuild. Divergence in either direction is the same defect.
    (tmp_path / "wiki" / ".obsidian" / "note.md").write_text(
        "---\ntitle: N\n---\nx\n", encoding="utf-8")

    pushed = ["wiki/a.md", "wiki/data.csv", "wiki/.hidden.md",
              "wiki/.obsidian/workspace.json", "wiki/.obsidian/note.md"]
    indexed_by_webhook = set(webhook.in_zone_changes({p: "added" for p in pushed}))
    indexed_by_rebuild = {r.path for r in corpus.load_pages(str(tmp_path))}

    assert indexed_by_webhook == indexed_by_rebuild == {"wiki/a.md", "wiki/.obsidian/note.md"}


def test_an_ordinary_markdown_push_is_still_in_zone():
    """The benign twin: the whole point of this filter is to let real pages through, and a removal
    carries no file on disk to inspect — it must still reach the delete path."""
    changes = {"wiki/notes/a.md": "added", "sources/meetings/b.md": "modified",
               "views/acme.md": "removed", "ops/acl.json": "modified", "README.md": "modified"}
    assert webhook.in_zone_changes(changes) == {
        "wiki/notes/a.md": "added", "sources/meetings/b.md": "modified", "views/acme.md": "removed"}


# ── the entity-registry snapshot the same delivery refreshes (issue #74) ───────────────────────
# `ops/` is not one of `corpus.ZONES`, so `ops/entity-registry.json` is invisible to every filter
# written for pages — precisely how the deploy-window staleness stayed hidden. The registry is
# therefore keyed off the RAW changed paths, fetched at the pushed sha, and written inside phase
# 2's transaction, next to the entity page a governed mint pushes beside it.
REGISTRY_RELPATH = "ops/entity-registry.json"
SEEDED_REGISTRY = '{"entities": {"seeded": {"name": "Seeded Corp", "type": "organization"}}}'
PUSHED_REGISTRY = '{"entities": {"pushed": {"name": "Pushed Corp", "type": "organization"}}}'


@pytest.fixture()
def snapshot(webhook_conn):
    """The singleton registry row, cleared on the way IN and on the way OUT. `entity_registry_
    snapshot` is one row in a database every suite shares and the server PREFERS it over its
    `--entity-registry` file, so a leftover here changes what an unrelated suite's
    `describe_entity` resolves — order-dependently, which is the kind of failure that reproduces
    on nobody's laptop."""
    for relpath in store.OPS_FILE_RELPATHS:
        store.clear_ops_file(webhook_conn, relpath)
    yield webhook_conn
    for relpath in store.OPS_FILE_RELPATHS:
        store.clear_ops_file(webhook_conn, relpath)


@pytest.fixture()
def pushed_pages(webhook_conn):
    """Every page path a test in this section pushes, deleted on TEARDOWN whatever the outcome.

    Not a cleanup line at the end of the test body, which is what the older tests here do: that
    line never runs when an assertion above it fails, and the row it leaves in a database every
    suite shares makes the NEXT run of the same test fail for a reason that has nothing to do with
    the code under test. (Observed while mutation-checking these very tests — a deliberately broken
    build left a page behind, and the repaired build then failed.)"""
    paths: list[str] = []
    yield paths
    store.delete_pages(webhook_conn, paths)


def _registry_content(conn) -> str | None:
    return store.read_ops_file(conn, store.ENTITY_REGISTRY_RELPATH)


def _token_spy(monkeypatch) -> list:
    """Counts every `installation_token()` a delivery mints, replacing the module-wide fake (which
    is a `lambda`, and therefore counts nothing)."""
    calls = []

    def spy(*a, **kw):
        calls.append(1)
        return "fake-token"
    monkeypatch.setattr("stigmergy.librarian.githubapp.installation_token", spy)
    return calls


def test_ops_files_pushed_reads_the_raw_paths_and_only_added_or_modified():
    """The predicate itself, now over every cached ops file. `added`/`modified` refresh; a
    `removed` does NOT, because no governed door deletes any of these files — a removal is either
    a rename away (the nightly rebuild reconciles from the checkout, per file) or an accident, and
    blanking an entity roster — or an identity roster — the moment one lands is the worse of the
    two answers. The last assertion is the one that pins WHERE it is asked: the page filter drops
    these paths entirely, so a predicate built on `in_zone_changes` could never fire at all."""
    assert webhook.ops_files_pushed({REGISTRY_RELPATH: "added"}) == [REGISTRY_RELPATH]
    assert webhook.ops_files_pushed({REGISTRY_RELPATH: "modified"}) == [REGISTRY_RELPATH]
    assert webhook.ops_files_pushed({REGISTRY_RELPATH: "removed"}) == []
    assert webhook.ops_files_pushed({"wiki/notes/a.md": "modified"}) == []
    assert webhook.ops_files_pushed({}) == []
    assert webhook.ops_files_pushed(
        {store.IDENTITIES_RELPATH: "modified", store.SLACK_CHANNELS_RELPATH: "added",
         REGISTRY_RELPATH: "modified"}) == list(store.OPS_FILE_RELPATHS)
    assert webhook.in_zone_changes({REGISTRY_RELPATH: "modified"}) == {}


def test_a_push_that_removes_the_registry_leaves_the_snapshot_untouched(snapshot):
    """A `removed` must be inert, not destructive. The snapshot is asserted BYTE-identical rather
    than merely present: "we kept a registry" and "we kept THIS registry" are different promises,
    and only the second one keeps every entity's name resolving. `registry_refreshed` stays out of
    the stats, so `job_runs` never claims a refresh that did not happen."""
    from stigmergy.index.backends.embedder import build_embedder
    store.write_ops_file(snapshot, store.ENTITY_REGISTRY_RELPATH, SEEDED_REGISTRY, "seed-sha")
    payload = _push_payload(commits=[{"added": [], "modified": [],
                                      "removed": [REGISTRY_RELPATH]}])

    stats = webhook.process_push(snapshot, build_embedder("fake"), payload, _settings(),
                                 opener=_fake_opener({}))

    assert "registry_refreshed" not in stats
    assert _registry_content(snapshot) == SEEDED_REGISTRY


def test_a_registry_only_push_refreshes_and_mints_exactly_one_token(snapshot, monkeypatch):
    """A push can carry the registry and NO page at all — a regenerate, or a re-mint of an
    already-indexed entity page. It must still refresh (the whole point of the fix), and it must
    still be able to fetch: the token used to be minted inside the page-upsert branch, where a
    registry-only push would have found none.

    Exactly ONE token per delivery, and the count is the assertion. A GitHub App installation
    token is a network round trip and a rate-limited credential; minting a second one per delivery
    is invisible in every functional assertion and is paid on every push forever."""
    from stigmergy.index.backends.embedder import build_embedder
    calls = _token_spy(monkeypatch)
    payload = _push_payload(commits=[{"added": [], "modified": [REGISTRY_RELPATH],
                                      "removed": []}])

    stats = webhook.process_push(snapshot, build_embedder("fake"), payload, _settings(),
                                 opener=_fake_opener({REGISTRY_RELPATH: PUSHED_REGISTRY}))

    assert stats["registry_refreshed"] is True
    assert stats["upserted"] == 0 and stats["deleted"] == 0
    assert _registry_content(snapshot) == PUSHED_REGISTRY
    assert len(calls) == 1, f"a registry-only delivery minted {len(calls)} installation tokens"


def test_a_mint_push_carrying_a_page_and_the_registry_mints_exactly_one_token(snapshot, monkeypatch,
                                                                              pushed_pages):
    """The shape a governed mint actually pushes: the entity page and the regenerated registry in
    one commit. Both land, off ONE token — the mixed twin of the registry-only count above, and
    the arm that would catch a second mint added beside the registry fetch."""
    from stigmergy.index.backends.embedder import build_embedder
    calls = _token_spy(monkeypatch)
    path = "wiki/webhook-test/registry-mint.md"
    pushed_pages.append(path)
    text = ("---\ntitle: Registry Mint\nentity: pushed\nverification: verified\n---\n"
            "The entity page a mint pushes beside the registry.")
    payload = _push_payload(commits=[{"added": [path], "modified": [REGISTRY_RELPATH],
                                      "removed": []}])

    stats = webhook.process_push(snapshot, build_embedder("fake"), payload, _settings(),
                                 opener=_fake_opener({path: text,
                                                      REGISTRY_RELPATH: PUSHED_REGISTRY}))

    assert stats["upserted"] == 1 and stats["registry_refreshed"] is True
    assert _registry_content(snapshot) == PUSHED_REGISTRY
    assert len(calls) == 1, f"a mint delivery minted {len(calls)} installation tokens"


def test_a_push_with_nothing_to_fetch_mints_no_token_at_all(snapshot, monkeypatch):
    """The benign twin of the two counts above, pointed the other way: a delivery that fetches
    nothing — a deletion-only push — must mint NOTHING. Moving the mint out of the page branch to
    cover the registry-only case is exactly the change that could have made every delivery pay for
    a credential it never uses."""
    from stigmergy.index.backends.embedder import build_embedder
    calls = _token_spy(monkeypatch)
    payload = _push_payload(commits=[{"added": [], "modified": [],
                                      "removed": ["wiki/webhook-test/never-existed.md"]}])

    webhook.process_push(snapshot, build_embedder("fake"), payload, _settings(),
                         opener=_fake_opener({}))

    assert calls == []


def test_an_over_cap_push_that_also_touches_the_registry_defers_the_registry_too(snapshot,
                                                                                 pushed_pages):
    """The cap's promise is that a push lands WHOLE or not at all, and the registry rides it: half
    of a bulk change is worse than a stale one, and a registry refreshed against pages that were
    deferred is exactly a half-applied push. The nightly rebuild reconciles both together."""
    from stigmergy.index.backends.embedder import build_embedder
    store.write_ops_file(snapshot, store.ENTITY_REGISTRY_RELPATH, SEEDED_REGISTRY, "seed-sha")
    paths = [f"wiki/webhook-test/registry-cap-{i}.md" for i in range(5)]
    pushed_pages.extend(paths)      # deferred means none of them land — unless the cap regresses
    contents = {p: f"---\ntitle: Cap {i}\nverification: verified\n---\nBody {i}."
                for i, p in enumerate(paths)}
    contents[REGISTRY_RELPATH] = PUSHED_REGISTRY
    payload = _push_payload(commits=[{"added": paths, "modified": [REGISTRY_RELPATH],
                                      "removed": []}])

    stats = webhook.process_push(snapshot, build_embedder("fake"), payload,
                                 _settings(file_cap=2), opener=_fake_opener(contents))

    assert stats["deferred"] is True
    assert "registry_refreshed" not in stats
    assert _registry_content(snapshot) == SEEDED_REGISTRY
    for path in paths:
        with snapshot.cursor() as cur:
            cur.execute("SELECT count(*) FROM pages_index WHERE path = %s", (path,))
            assert cur.fetchone()[0] == 0


def test_a_failing_registry_write_rolls_the_page_back_with_it(snapshot, monkeypatch, pushed_pages):
    """Phase 2 is ONE transaction and the registry is inside it: the entity page and the metadata
    that names it land together or neither does, so no reader ever sees a page whose entity has no
    name — the exact half-state issue #74 reported, which a snapshot written OUTSIDE the
    transaction would have reintroduced from the other side.

    Proven by forcing the registry write to fail, because that is the direction the existing
    rollback test (`..._rolls_back_a_deletion_when_the_upsert_side_fails_mid_run`) cannot reach:
    the registry write is the LAST statement in the block, so only a failure there proves the page
    upsert before it is genuinely inside the same transaction."""
    from stigmergy.index.backends.embedder import build_embedder
    store.write_ops_file(snapshot, store.ENTITY_REGISTRY_RELPATH, SEEDED_REGISTRY, "seed-sha")
    path = "wiki/webhook-test/registry-rollback.md"
    pushed_pages.append(path)
    text = ("---\ntitle: Registry Rollback\nentity: pushed\nverification: verified\n---\n"
            "The page that must not survive its registry's failure.")
    payload = _push_payload(commits=[{"added": [path], "modified": [REGISTRY_RELPATH],
                                      "removed": []}])
    with snapshot.cursor() as cur:      # the row must be absent BEFORE, or "0 after" proves nothing
        cur.execute("SELECT count(*) FROM pages_index WHERE path = %s", (path,))
        assert cur.fetchone()[0] == 0

    def boom(*args, **kwargs):
        raise RuntimeError("simulated registry-snapshot failure mid-transaction")
    monkeypatch.setattr(store, "write_ops_file", boom)

    with pytest.raises(RuntimeError, match="simulated registry-snapshot failure"):
        webhook.process_push(snapshot, build_embedder("fake"), payload, _settings(),
                             opener=_fake_opener({path: text,
                                                  REGISTRY_RELPATH: PUSHED_REGISTRY}))

    monkeypatch.undo()
    with snapshot.cursor() as cur:
        cur.execute("SELECT count(*) FROM pages_index WHERE path = %s", (path,))
        assert cur.fetchone()[0] == 0             # the page rolled back WITH the registry write
    assert _registry_content(snapshot) == SEEDED_REGISTRY


# ── the ops files are fetched at the BRANCH ref, and that is the replay defense ────────────────
def _capturing_opener(file_contents: dict[str, str], seen_refs: dict[str, str]):
    """`_fake_opener`, additionally recording WHICH ref each path was fetched at — the fact the
    branch-ref tests are about."""
    def opener(request, timeout=30):
        parsed = urllib.parse.urlparse(request.full_url)
        path = urllib.parse.unquote(parsed.path.split("/contents/", 1)[1])
        seen_refs[path] = urllib.parse.parse_qs(parsed.query).get("ref", [""])[0]
        if path not in file_contents:
            raise urllib.error.HTTPError(request.full_url, 404, "Not Found", {}, None)
        return _FakeGithubResponse(file_contents[path].encode("utf-8"))
    return opener


def test_ops_files_are_fetched_at_the_branch_ref_and_pages_at_the_pushed_sha(snapshot):
    """**The replay defense for the access files, pinned at the seam it lives in.** A page is
    fetched at the delivery's own sha — its consistency story is the delivery's path list. An ops
    file is fetched at the BRANCH ref, so a replayed or delayed delivery re-fetches what the
    branch says NOW and no historical roster is ever installable through this endpoint. Pinning
    the URL is pinning the defense: `ref=<sha>` here would be issue #79 item 1 reopened."""
    from stigmergy.index.backends.embedder import build_embedder
    page = "wiki/notes/a-note.md"
    seen: dict[str, str] = {}
    payload = _push_payload(sha="deadbeef", commits=[{
        "added": [page, store.IDENTITIES_RELPATH], "modified": [], "removed": []}])

    webhook.process_push(snapshot, build_embedder("fake"), payload, _settings(),
                         opener=_capturing_opener({
                             page: "---\ntitle: A Note\n---\nbody",
                             store.IDENTITIES_RELPATH: '{"ana@example.com": ["finance"]}'}, seen))

    assert seen[page] == "deadbeef"
    assert seen[store.IDENTITIES_RELPATH] == BRANCH
    store.delete_pages(snapshot, [page])


def test_a_replayed_delivery_reinstalls_what_the_branch_says_now_not_what_it_said_then(snapshot):
    """The defense OBSERVED, not inferred from a URL: an old delivery replayed after a revocation
    re-fetches the roster at the branch ref, so what lands is the CURRENT file — the replay is a
    no-op, not a restoration. (With the sha road this same replay would have re-installed the
    pre-revocation roster; that road no longer exists for these files.)"""
    from stigmergy.index.backends.embedder import build_embedder
    old_payload = _push_payload(sha="oldsha111", commits=[{
        "added": [store.IDENTITIES_RELPATH], "modified": [], "removed": []}])
    current = '{"steward@example.com": "*"}'    # ana was revoked since that delivery

    webhook.process_push(snapshot, build_embedder("fake"), old_payload, _settings(),
                         opener=_fake_opener({store.IDENTITIES_RELPATH: current}))

    assert store.read_ops_file(snapshot, store.IDENTITIES_RELPATH) == current


def test_a_push_carrying_identities_and_channels_refreshes_both_snapshots(snapshot):
    from stigmergy.index.backends.embedder import build_embedder
    identities = '{"ana@example.com": ["finance"]}'
    channels = '{"C123": ["finance"]}'
    payload = _push_payload(commits=[{
        "added": [store.IDENTITIES_RELPATH], "modified": [store.SLACK_CHANNELS_RELPATH],
        "removed": []}])

    stats = webhook.process_push(snapshot, build_embedder("fake"), payload, _settings(),
                                 opener=_fake_opener({store.IDENTITIES_RELPATH: identities,
                                                      store.SLACK_CHANNELS_RELPATH: channels}))

    assert store.read_ops_file(snapshot, store.IDENTITIES_RELPATH) == identities
    assert store.read_ops_file(snapshot, store.SLACK_CHANNELS_RELPATH) == channels
    assert stats["ops_files_refreshed"] == sorted([store.IDENTITIES_RELPATH,
                                                   store.SLACK_CHANNELS_RELPATH])
    assert "registry_refreshed" not in stats


# ── the delivery-id dedupe: the page road's own replay defense ─────────────────────────────────
def test_a_delivery_id_already_applied_is_acknowledged_and_not_reapplied(snapshot):
    """A replayed page delivery re-fetches content at its own OLD sha — a downgrade, and for a
    `removed` list a re-deletion — so a delivery id that already COMMITTED is refused a second
    apply. Proven at the `process_push` + `delivery_already_applied` seam the endpoint runs."""
    from stigmergy.index.backends.embedder import build_embedder
    page = "wiki/notes/replayed.md"
    payload = _push_payload(sha="abc111", commits=[{"added": [page], "modified": [],
                                                    "removed": []}])
    opener = _fake_opener({page: "---\ntitle: Replayed\n---\nversion one"})
    with snapshot.cursor() as cur:   # arrange: the shared database remembers earlier runs
        cur.execute("DELETE FROM webhook_deliveries WHERE delivery_id IN (%s, %s)",
                    ("delivery-1", "delivery-2"))

    assert store.delivery_already_applied(snapshot, "delivery-1") is False
    webhook.process_push(snapshot, build_embedder("fake"), payload, _settings(),
                         delivery_id="delivery-1", opener=opener)

    assert store.delivery_already_applied(snapshot, "delivery-1") is True
    # The benign twin: a DIFFERENT delivery is not a replay.
    assert store.delivery_already_applied(snapshot, "delivery-2") is False
    store.delete_pages(snapshot, [page])
    with snapshot.cursor() as cur:
        cur.execute("DELETE FROM webhook_deliveries WHERE delivery_id = %s", ("delivery-1",))


def test_a_failed_delivery_records_nothing_so_manual_redelivery_still_works(snapshot,
                                                                            monkeypatch):
    """GitHub's redelivery button is the LEGITIMATE sender of a repeated id, and it exists for
    deliveries that failed. The id is recorded inside phase 2's transaction, so an apply that
    rolls back never records itself — asserted by making the upsert blow up mid-transaction."""
    from stigmergy.index.backends.embedder import build_embedder
    page = "wiki/notes/failing.md"
    payload = _push_payload(commits=[{"added": [page], "modified": [], "removed": []}])

    def boom(*a, **kw):
        raise RuntimeError("phase 2 dies mid-transaction")

    monkeypatch.setattr(store, "upsert_pages", boom)
    with pytest.raises(RuntimeError):
        webhook.process_push(snapshot, build_embedder("fake"), payload, _settings(),
                             delivery_id="delivery-3",
                             opener=_fake_opener({page: "---\ntitle: F\n---\nbody"}))

    assert store.delivery_already_applied(snapshot, "delivery-3") is False


def test_an_empty_delivery_id_is_never_deduped(snapshot):
    """The guard's own benign twin: an origin that stamps no id gets no dedupe — two id-less
    deliveries must both apply, never collide on `""`."""
    assert store.delivery_already_applied(snapshot, "") is False
    with snapshot.cursor() as cur:
        store.record_delivery(cur, "")
    assert store.delivery_already_applied(snapshot, "") is False


def test_a_duplicate_delivery_through_the_real_endpoint_is_acknowledged_and_not_reapplied(
        webhook_conn, fixture, webhook_env, monkeypatch):
    """The endpoint half of the dedupe: same signed body, same `X-GitHub-Delivery` id, twice.
    The second response is a 200 (GitHub must never see an error for a replay) that says
    `duplicate delivery`, and the page fetcher is never called again — asserted by making the
    second fetch impossible."""
    page = "wiki/webhook-test/deduped.md"
    payload = _push_payload(commits=[{"added": [page], "modified": [], "removed": []}])
    body = json.dumps(payload).encode()
    headers = {"X-Hub-Signature-256": _sign(SECRET, body), "X-GitHub-Event": "push",
               "X-GitHub-Delivery": "dedupe-e2e-1"}
    texts = {page: "---\ntitle: Deduped\n---\nversion one"}
    monkeypatch.setattr(webhook, "_fetch_file_content",
                        lambda repo, path, sha, token, opener=None: texts[path])
    app = build_test_http_app(fixture, {})

    try:
        with run_http_server(app) as url:
            base = url.rsplit("/", 1)[0]
            first = httpx.post(f"{base}{webhook.WEBHOOK_PATH}", content=body, headers=headers)
            texts.clear()   # a re-apply would now KeyError inside the endpoint -> 500
            second = httpx.post(f"{base}{webhook.WEBHOOK_PATH}", content=body, headers=headers)

        assert first.status_code == 200 and first.json().get("upserted") == 1
        assert second.status_code == 200
        assert second.json().get("ignored") == "duplicate delivery"
    finally:
        store.delete_pages(webhook_conn, [page])
        with webhook_conn.cursor() as cur:
            cur.execute("DELETE FROM webhook_deliveries WHERE delivery_id = %s",
                        ("dedupe-e2e-1",))
