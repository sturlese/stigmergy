"""The entity registry the server SERVES is the one on the knowledge repo's main, not the one the
image was built with.

The bug this file was written for (issue #74, found on the 2026-08-17 staging walkthrough): a
governed mint pushes an entity page AND the regenerated `ops/entity-registry.json` to main, the
push webhook refreshes `pages_index` within seconds so the PAGE is right — and
`describe_entity("ferrovial-nexus")` still answers `{"name": "", "type": "", "aliases": []}`,
because `entity_registry_path` is a plain read of a file baked at deploy time and nothing ever
refreshed it. The window was deploy-to-deploy, invisible, and it also silently disabled
entity-first search boosting for every entity minted since the rollout.

The road taken is the one the pages already ride: the registry is repo-derived data the server
reads, so it is cached in the derived index next to them — refreshed by the same webhook that
refreshes the pages, and reconciled by the same nightly `--rebuild`. That covers the `slack`
process group too, which has neither a checkout nor a webhook of its own and would otherwise have
stayed permanently stale.
"""
import json
import os

import pytest

from stigmergy.index import build, store
from stigmergy.index.backends.embedder import build_embedder
from stigmergy.server import webhook
from stigmergy.server.service import BrainService
from stigmergy.server.settings import Settings
from tests.server.conftest import write_page

REGISTRY_RELPATH = "ops/entity-registry.json"
ENTITY_PAGE = "wiki/entities/ferrovial-nexus.md"
NOTE_PAGE = "wiki/notes/ferrovial-nexus-kickoff.md"
# `cofers` needs an anchored page of its own, and the reason is the existence rule rather than
# convenience: `describe_entity` resolves through `scoped_entities()`, so an entity that only the
# REGISTRY knows is indistinguishable from one that does not exist (ADR 022). Without this page the
# two file-fallback twins below would be asserting against that rule instead of against which
# registry copy the server read.
COFERS_NOTE_PAGE = "wiki/notes/cofers-billing.md"

# What the deploy baked: the corpus already anchors pages to `ferrovial-nexus`, and the registry
# file in the image does not know the name. This is the staging state exactly.
BAKED_REGISTRY = {"entities": {
    "cofers": {"name": "Cofers", "type": "organization", "aliases": ["Cofers SL"]},
}}
# What main carries after the mint — one more entity, nothing else changed.
PUSHED_REGISTRY = {"entities": {
    **BAKED_REGISTRY["entities"],
    "ferrovial-nexus": {"name": "Ferrovial Nexus", "type": "organization",
                        "aliases": ["Nexus"]},
}}

STEWARD = "steward@example.com"


class _Fixture:
    def __init__(self, root: str):
        self.repo = os.path.join(root, "repo")
        ops = os.path.join(self.repo, "ops")
        os.makedirs(ops, exist_ok=True)
        self.baked_registry_path = os.path.join(root, "baked-entity-registry.json")
        with open(self.baked_registry_path, "w", encoding="utf-8") as f:
            f.write(json.dumps(BAKED_REGISTRY))
        self.identities_path = os.path.join(ops, "identities.json")
        with open(self.identities_path, "w", encoding="utf-8") as f:
            f.write(json.dumps({STEWARD: ["brain-admins"]}))

        write_page(self.repo, ENTITY_PAGE,
                   {"type": "entity", "title": "Ferrovial Nexus",
                    "entity": "['ferrovial-nexus']", "verification": "verified"},
                   "Ferrovial Nexus is a governed entity page.")
        write_page(self.repo, NOTE_PAGE,
                   {"type": "note", "title": "Ferrovial Nexus kickoff",
                    "entity": "['ferrovial-nexus']", "as_of": "2026-08-17",
                    "verification": "verified"},
                   "The kickoff note anchored to Ferrovial Nexus.")
        write_page(self.repo, COFERS_NOTE_PAGE,
                   {"type": "note", "title": "Cofers billing", "entity": "['cofers']",
                    "as_of": "2026-07-01", "verification": "verified"},
                   "A note anchored to Cofers, the entity the image was baked with.")


@pytest.fixture()
def freshness(tmp_path_factory):
    """A DEDICATED database connection and a rebuilt index: this file writes the registry snapshot,
    a singleton row every other server suite would then read. `function` scope plus an explicit
    clear on the way out keeps that blast radius inside this module."""
    from tests import testdb
    conn = testdb.connect_or_skip("index")
    fx = _Fixture(str(tmp_path_factory.mktemp("registry-freshness")))
    build.rebuild(conn, fx.repo, build_embedder("fake"))
    store.clear_ops_file(conn, store.ENTITY_REGISTRY_RELPATH)
    yield conn, fx
    store.clear_ops_file(conn, store.ENTITY_REGISTRY_RELPATH)
    conn.close()


def _service(conn, fx) -> BrainService:
    settings = Settings(identity=STEWARD, identities_path=fx.identities_path,
                        entity_registry_path=fx.baked_registry_path)
    return BrainService(settings, conn, build_embedder("fake"), None, identity=STEWARD)


def _mint_push(registry_text: str) -> tuple[dict, dict]:
    """The two files a governed mint commits together, as one push payload + its file contents."""
    entity_page_text = ("---\ntype: entity\ntitle: Ferrovial Nexus\n"
                        "entity: ['ferrovial-nexus']\nverification: verified\n---\n"
                        "Ferrovial Nexus is a governed entity page.")
    payload = {
        "ref": "refs/heads/main",
        "after": "4b49997aa9a7",
        "repository": {"full_name": "acme/knowledge"},
        "commits": [{"added": [ENTITY_PAGE], "modified": [REGISTRY_RELPATH], "removed": []}],
    }
    return payload, {ENTITY_PAGE: entity_page_text, REGISTRY_RELPATH: registry_text}


def _settings() -> webhook.WebhookSettings:
    return webhook.WebhookSettings(secret="s", repo="acme/knowledge", branch="main")


@pytest.fixture(autouse=True)
def _fake_installation_token(monkeypatch):
    monkeypatch.setattr("stigmergy.librarian.githubapp.installation_token",
                        lambda *a, **kw: "fake-token")


def test_a_mint_pushed_after_the_rollout_is_served_with_its_name_and_type(freshness):
    """RED before #74: the webhook refreshed the entity PAGE and ignored `ops/entity-registry.json`
    (not an indexed zone), so `describe_entity` served
    `{"id": "ferrovial-nexus", "name": "", "type": "", "aliases": []}` — the exact staging
    evidence — until the next deploy re-baked the file."""
    from tests.server.test_webhook import _fake_opener
    conn, fx = freshness
    payload, contents = _mint_push(json.dumps(PUSHED_REGISTRY))

    webhook.process_push(conn, build_embedder("fake"), payload, _settings(),
                         opener=_fake_opener(contents))

    out = _service(conn, fx).describe_entity("ferrovial-nexus")
    assert out["entity"]["name"] == "Ferrovial Nexus"
    assert out["entity"]["type"] == "organization"
    assert out["entity"]["aliases"] == ["Nexus"]


def test_the_refreshed_registry_also_resolves_an_alias_minted_after_the_rollout(freshness):
    """The half the staging report called out as SILENT: alias resolution and entity-first search
    boosting miss every entity minted since the rollout, and nothing anywhere says so."""
    from tests.server.test_webhook import _fake_opener
    conn, fx = freshness
    payload, contents = _mint_push(json.dumps(PUSHED_REGISTRY))
    webhook.process_push(conn, build_embedder("fake"), payload, _settings(),
                         opener=_fake_opener(contents))

    # "Nexus" is an ALIAS, known only to the pushed registry — resolvable only if the server reads
    # the pushed one.
    out = _service(conn, fx).describe_entity("Nexus")
    assert out["entity"]["id"] == "ferrovial-nexus"


def test_a_push_that_does_not_touch_the_registry_leaves_the_baked_file_answering(freshness):
    """The benign twin. An ordinary page push must not disturb the registry the server serves —
    the refresh is keyed on the registry PATH being in the push, never on "a push happened"."""
    from tests.server.test_webhook import _fake_opener
    conn, fx = freshness
    payload = {
        "ref": "refs/heads/main", "after": "deadbeef",
        "repository": {"full_name": "acme/knowledge"},
        "commits": [{"added": [], "modified": [NOTE_PAGE], "removed": []}],
    }
    note_text = ("---\ntype: note\ntitle: Ferrovial Nexus kickoff\n"
                 "entity: ['ferrovial-nexus']\nas_of: 2026-08-17\nverification: verified\n---\n"
                 "Edited body.")
    stats = webhook.process_push(conn, build_embedder("fake"), payload, _settings(),
                                 opener=_fake_opener({NOTE_PAGE: note_text}))

    assert "registry_refreshed" not in stats
    assert store.read_ops_file(conn, store.ENTITY_REGISTRY_RELPATH) is None
    # Still exactly what the image baked: `cofers` known, `ferrovial-nexus` bare.
    out = _service(conn, fx).describe_entity("cofers")
    assert out["entity"]["name"] == "Cofers"
    bare = _service(conn, fx).describe_entity("ferrovial-nexus")
    assert bare["entity"] == {"id": "ferrovial-nexus", "name": "", "type": "", "aliases": [],
                              "approved_by": "",
                              "page": bare["entity"]["page"]}


def test_with_no_snapshot_at_all_the_baked_file_is_still_the_answer(freshness):
    """The other benign twin, and the one that matters for a local `stigmergy-server --repo`: a
    database whose index predates this change (no snapshot row) must behave EXACTLY as before —
    the file road is the fallback, not a removed road."""
    conn, fx = freshness
    assert store.read_ops_file(conn, store.ENTITY_REGISTRY_RELPATH) is None
    out = _service(conn, fx).describe_entity("Cofers SL")   # a baked-file alias
    assert out["entity"]["id"] == "cofers"
    assert out["entity"]["name"] == "Cofers"


# ── a malformed SNAPSHOT: the risk surface this design ADDED ───────────────────────────────────
# Before #74 there was exactly one way for a malformed registry to reach a reader: a file an
# operator (or a deploy) put there. Now there is a second — bytes fetched over the network at a
# pushed sha, written into a row, read back by three tools — and it is the one nobody can inspect
# with `cat`. `tests/server/test_entity_tools_pg.py::test_list_entities_registry_malformed_raises
# _loudly` is this pair's benign twin on the FILE road; these are the snapshot's own.
#
# A registry regenerated mid-write, a truncated fetch, a hand-repaired table: the bytes below are
# a plausible one — a real registry cut off mid-record, so they also carry recognizable content
# whose absence from every caller-facing answer is assertable.
TRUNCATED_SNAPSHOT = '{"entities": {"ferrovial-nexus": {"name": "Ferrovial Nex'


def _mcp_text(service, tool: str, **args) -> str:
    """What a real MCP caller receives, through the REAL tool closure — `build_mcp` in process,
    the same closures both transports mount. The raw text, never parsed, because the assertion is
    about the whole payload's bytes."""
    import asyncio

    from stigmergy.server.mcp_server import build_mcp
    blocks, _ = asyncio.run(build_mcp(service).call_tool(tool, args))
    return blocks[0].text


@pytest.mark.parametrize("read", [
    lambda svc: svc.list_entities(),
    lambda svc: svc.describe_entity("cofers"),
    lambda svc: svc.search("what happened with cofers"),
], ids=["list_entities", "describe_entity", "search_brain"])
def test_a_malformed_snapshot_fails_loudly_as_registryerror_on_every_registry_reader(
        freshness, read):
    """All three registry readers, one posture. `list_entities` is the arm that used to reach the
    loader directly and let its path-bearing `ValueError` out; `describe_entity` and `search_brain`
    went through the converting helpers already. With a second, un-inspectable source of bytes the
    uniformity is the point: an operator who sees `RegistryError` knows the registry is the fault,
    whichever tool reported it."""
    from stigmergy.server.errors import RegistryError
    conn, fx = freshness
    store.write_ops_file(conn, store.ENTITY_REGISTRY_RELPATH, TRUNCATED_SNAPSHOT, "4b49997aa9a7")

    with pytest.raises(RegistryError):
        read(_service(conn, fx))


@pytest.mark.parametrize("tool, args", [
    ("list_entities", {}),
    ("describe_entity", {"entity": "cofers"}),
    ("search_brain", {"query": "what happened with cofers"}),
])
def test_a_malformed_snapshot_leaks_neither_a_path_nor_the_snapshot_bytes_to_a_caller(
        freshness, tool, args):
    """The confidentiality half, asserted on what the CLOSURE actually returns rather than on the
    exception type. Two things must not be in it: a filesystem path (the parser's message is
    written for an operator and names its source — that is why it becomes `RegistryError`), and
    the snapshot's own bytes, which are repo content this identity may have no right to see and
    which nothing has ACL-scoped on the way out of a parse failure."""
    conn, fx = freshness
    store.write_ops_file(conn, store.ENTITY_REGISTRY_RELPATH, TRUNCATED_SNAPSHOT, "4b49997aa9a7")

    out = _mcp_text(_service(conn, fx), tool, **args)

    assert "failed (RegistryError)" in out          # the class name, and nothing else
    assert fx.baked_registry_path not in out        # no --entity-registry path
    assert REGISTRY_RELPATH not in out              # nor the snapshot origin's own spelling
    assert "Ferrovial Nex" not in out               # nor one byte of the registry itself


def test_a_well_formed_snapshot_still_answers_through_the_same_closures(freshness):
    """The benign twin for the two above: the caller-facing road must still SERVE. A refusal that
    fires for a healthy registry costs every entity tool at once, and the malformed assertions
    above would pass just as well against a `describe_entity` that always errored."""
    conn, fx = freshness
    store.write_ops_file(conn, store.ENTITY_REGISTRY_RELPATH, json.dumps(PUSHED_REGISTRY), "4b49997aa9a7")

    out = _mcp_text(_service(conn, fx), "describe_entity", entity="Nexus")

    assert json.loads(out)["entity"]["name"] == "Ferrovial Nexus"
    assert "error" not in json.loads(out)


# ── the regression THIS design could reintroduce: a session-window staleness ───────────────────
def test_one_long_lived_service_sees_a_snapshot_written_after_its_first_call(freshness):
    """`build_service` builds ONE `BrainService` per stdio process, and that service lives for the
    whole session. Memoizing the registry source per SERVICE — rather than per tool call — would
    trade #74's deploy-window staleness for a session-window one: the same bug, a shorter window,
    and harder to see because it depends on which call happened first.

    So: call, mint, call again. The second call must see the mint."""
    conn, fx = freshness
    svc = _service(conn, fx)                       # ONE service for every call below
    before = svc.describe_entity("ferrovial-nexus")
    assert before["entity"]["name"] == ""          # the baked file, which never knew this entity

    store.write_ops_file(conn, store.ENTITY_REGISTRY_RELPATH, json.dumps(PUSHED_REGISTRY), "4b49997aa9a7")

    after = svc.describe_entity("ferrovial-nexus")
    assert after["entity"]["name"] == "Ferrovial Nexus"
    assert svc.describe_entity("Nexus")["entity"]["id"] == "ferrovial-nexus"


def test_one_tool_call_reads_the_registry_exactly_once(freshness, monkeypatch):
    """The twin, and the cost property the memo exists for: freshness per CALL, not per read.
    `describe_entity` consults the registry three times (aliases, then records, plus the absence
    branch's own resolution) — unmemoized that is three round trips to Postgres for bytes that
    cannot change inside one call, on the hot path of every entity tool. Exactly 1 is the
    assertion; 3 means the memo stopped working, and 0 means the snapshot road is no longer being
    consulted at all."""
    conn, fx = freshness
    store.write_ops_file(conn, store.ENTITY_REGISTRY_RELPATH, json.dumps(PUSHED_REGISTRY), "4b49997aa9a7")
    real = store.read_ops_file
    calls = []

    def counting(c, relpath):
        if relpath == store.ENTITY_REGISTRY_RELPATH:
            calls.append(1)
        return real(c, relpath)
    monkeypatch.setattr(store, "read_ops_file", counting)

    svc = _service(conn, fx)
    out = svc.describe_entity("Nexus")

    assert out["entity"]["name"] == "Ferrovial Nexus"      # it really did read the snapshot
    assert len(calls) == 1, f"the registry was read {len(calls)} times inside one tool call"
    svc.describe_entity("Nexus")
    assert len(calls) == 2, "the memo survived the tool call — that is a session-window staleness"


# ── the nightly reconciler's other direction, end to end ───────────────────────────────────────
def test_a_rebuild_from_a_repo_with_no_registry_hands_the_answer_back_to_the_file(freshness):
    """`rebuild()` makes the index match the checkout, and that has to include the registry: a
    snapshot answering from a registry the repo no longer has is the same deploy-time staleness the
    snapshot exists to end, pointed the other way. Cleared, the service falls back to its
    `--entity-registry` file — the pre-snapshot behaviour, and the honest floor.

    The repo this fixture rebuilds from carries pages and no `ops/entity-registry.json`, which is
    exactly the "before its first mint" state a real knowledge repo starts in."""
    conn, fx = freshness
    store.write_ops_file(conn, store.ENTITY_REGISTRY_RELPATH, json.dumps(PUSHED_REGISTRY), "4b49997aa9a7")
    assert _service(conn, fx).describe_entity("Nexus")["entity"]["id"] == "ferrovial-nexus"

    build.rebuild(conn, fx.repo, build_embedder("fake"))

    assert store.read_ops_file(conn, store.ENTITY_REGISTRY_RELPATH) is None
    svc = _service(conn, fx)
    assert svc.describe_entity("cofers")["entity"]["name"] == "Cofers"      # the baked file
    assert svc.describe_entity("ferrovial-nexus")["entity"]["name"] == ""   # which never knew it


# ── shared-database hygiene, pinned rather than remembered ─────────────────────────────────────
def test_every_test_module_that_writes_a_registry_snapshot_also_clears_it():
    """`entity_registry_snapshot` is a SINGLETON row in a database every suite shares, and the
    service prefers it over the file — so a module that leaves one behind silently changes what an
    unrelated suite's `describe_entity` resolves, in a way that depends on collection order and
    therefore reproduces on nobody's laptop.

    A source scan rather than a runtime guard on purpose: the failure it prevents is a test author
    forgetting the teardown, which no fixture in the forgetful module would run either. The pin is
    cheap and its message says what to do.

    **What this scan does NOT see, stated because a partial check that reads as total is worse than
    none**: `build.rebuild` mutates the same singleton without naming either function — it CACHES
    the checkout's registry, or CLEARS the row when the checkout has none. So every rebuilding
    module is a writer too, and no source scan can tell which of them carries an
    `ops/entity-registry.json`. The invariant that actually holds is the other one: a test whose
    outcome depends on the snapshot's state ARRANGES that state itself
    (`tests/admin/test_service_pg.py`'s loader-refusal test and this module's own fixture both do).
    The scan below closes the half it can close."""
    import pathlib
    tests_root = pathlib.Path(__file__).resolve().parents[1]
    offenders = []
    for path in sorted(tests_root.rglob("test_*.py")):
        text = path.read_text(encoding="utf-8")
        if "write_entity_registry(" in text and "clear_entity_registry(" not in text:
            offenders.append(str(path.relative_to(tests_root)))
    assert not offenders, (
        "these test modules write the entity-registry snapshot and never clear it — add a "
        "`store.clear_ops_file(conn, store.ENTITY_REGISTRY_RELPATH)` teardown, or the next suite to run inherits this "
        "one's registry:\n  " + "\n  ".join(offenders))
