"""`stigmergy-index --check` — the substrate lint.

Strategy: build a small clean corpus into the real Postgres (fake embedder, keyless), assert
zero findings, then inject each corruption class by SQL — the way real corruption arrives:
not through the builder (which is correct), but through drift the builder never sees again
(partial writes, hand surgery, a webhook half-applied). Each class asserts its finding and its
severity; the CLI's exit contract is pinned through the same `run_checks` the CLI calls.
"""
import json
import os

import pytest

from stigmergy.index import build, check, store
from stigmergy.index.backends.embedder import build_embedder
from tests import testdb


def _connect_or_skip():
    return testdb.connect_or_skip("index")


def _write(root, rel, front, body):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fm = "\n".join(f"{k}: {v}" for k, v in front.items())
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"---\n{fm}\n---\n{body}")


@pytest.fixture()
def checked(tmp_path):
    """A fresh, CLEAN corpus per test — checks mutate rows, so no module-scoped sharing."""
    root = str(tmp_path / "repo")
    _write(root, "wiki/notes/alpha.md", {"title": "Alpha", "entity": "vantage"}, "alpha body")
    _write(root, "wiki/notes/beta.md", {"title": "Beta"}, "beta body")
    _write(root, "sources/meetings/x-transcript.md", {"title": "X"}, "part one")
    _write(root, "sources/meetings/x-transcript-p2.md", {"title": "X p2"}, "part two")
    ops = os.path.join(root, "ops")
    os.makedirs(ops, exist_ok=True)
    registry_path = os.path.join(ops, "entity-registry.json")
    with open(registry_path, "w", encoding="utf-8") as f:
        json.dump({"entities": {"vantage": {"name": "Vantage", "type": "organization",
                                           "aliases": []}}}, f)
    conn = _connect_or_skip()
    build.rebuild(conn, root, build_embedder("fake"))
    yield conn, registry_path
    # This repo CARRIES `ops/entity-registry.json`, so the rebuild above cached it into the
    # `entity_registry_snapshot` singleton every suite shares — and `check.served_registry` prefers
    # that snapshot over the file a caller names. Left behind, it decides what an unrelated
    # module's substrate check lints, by collection order.
    store.clear_entity_registry(conn)
    conn.close()


def _by_check(findings):
    out = {}
    for f in findings:
        out.setdefault(f["check"], []).append(f)
    return out


def test_a_clean_index_has_zero_findings(checked):
    conn, registry = checked
    assert check.run_checks(conn, registry_path=registry) == []


def test_duplicate_page_id_is_an_error(checked):
    conn, registry = checked
    with conn.cursor() as cur:
        cur.execute("UPDATE pages_index SET page_id = 'alpha' WHERE path = 'wiki/notes/beta.md'")
    found = _by_check(check.run_checks(conn, registry_path=registry))
    assert found["duplicate-page-id"][0]["severity"] == "error"
    assert "alpha" in found["duplicate-page-id"][0]["detail"]


def test_an_orphan_continuation_part_is_an_error(checked):
    conn, registry = checked
    with conn.cursor() as cur:
        cur.execute("DELETE FROM pages_index WHERE path = 'sources/meetings/x-transcript.md'")
    found = _by_check(check.run_checks(conn, registry_path=registry))
    assert found["orphan-continuation-part"][0]["severity"] == "error"
    assert "x-transcript-p2" in found["orphan-continuation-part"][0]["detail"]


def test_a_part_beside_its_primary_is_not_an_orphan(checked):
    conn, registry = checked
    assert "orphan-continuation-part" not in _by_check(check.run_checks(conn, registry))


def test_an_arm_invisible_page_is_an_error(checked):
    conn, registry = checked
    with conn.cursor() as cur:
        cur.execute("UPDATE pages_index SET embedding = NULL WHERE path = 'wiki/notes/alpha.md'")
        cur.execute("UPDATE pages_index SET tsv = ''::tsvector WHERE path = 'wiki/notes/beta.md'")
    found = _by_check(check.run_checks(conn, registry_path=registry))
    assert found["missing-embedding"][0]["severity"] == "error"
    assert "alpha" in found["missing-embedding"][0]["detail"]
    assert found["empty-tsv"][0]["severity"] == "error"
    assert "beta" in found["empty-tsv"][0]["detail"]


def test_a_dangling_supersession_is_a_warning(checked):
    conn, registry = checked
    with conn.cursor() as cur:
        cur.execute("UPDATE pages_index SET superseded_by = 'ghost-successor'"
                    " WHERE path = 'wiki/notes/alpha.md'")
    found = _by_check(check.run_checks(conn, registry_path=registry))
    assert found["dangling-superseded-by"][0]["severity"] == "warn"


def test_an_anchored_but_unregistered_entity_is_a_warning(checked):
    conn, registry = checked
    with conn.cursor() as cur:
        cur.execute("UPDATE pages_index SET entity = ARRAY['ghost-corp']"
                    " WHERE path = 'wiki/notes/beta.md'")
    found = _by_check(check.run_checks(conn, registry_path=registry))
    assert found["anchored-but-unregistered"][0]["severity"] == "warn"
    assert "ghost-corp" in found["anchored-but-unregistered"][0]["detail"]


def test_no_registry_skips_coverage_instead_of_inventing_findings(checked):
    conn, _ = checked
    assert "anchored-but-unregistered" not in _by_check(check.run_checks(conn, None))


def test_a_malformed_registry_raises_loudly(checked, tmp_path):
    """The message is the LOADER's now (`kernel.registry.load_registry`), not a second one this
    module used to spell for itself — the check reads the registry the way the server does."""
    conn, _ = checked
    bad = tmp_path / "registry.json"
    bad.write_text('{"not-entities": []}')
    with pytest.raises(ValueError, match="top-level 'entities' object is required"):
        check.run_checks(conn, registry_path=str(bad))


def test_a_nameless_entity_is_refused_the_way_the_loader_refuses_it(checked, tmp_path):
    """OLD BEHAVIOUR: accepted in silence — `registry_ids` returned `{'vantage'}` and the check
    reported a clean substrate.

    The lint hand-parsed the registry (`json.load` + a top-level shape assertion) instead of
    reading it through `kernel.registry.load_registry`, so it validated STRICTLY LESS than the
    loader every consumer actually uses. An entity with no `name` passed the lint and then made
    `load_registry` raise at server startup: `stigmergy-index --check` blessed a substrate the
    server refuses to load, which is the one thing this lint exists to prevent.
    """
    conn, _ = checked
    nameless = tmp_path / "registry.json"
    nameless.write_text('{"entities": {"vantage": {"type": "organization", "aliases": []}}}')

    with pytest.raises(ValueError, match="needs at least a 'name'"):
        check.run_checks(conn, registry_path=str(nameless))


def test_findings_order_errors_first(checked):
    conn, registry = checked
    with conn.cursor() as cur:
        cur.execute("UPDATE pages_index SET superseded_by = 'ghost'"
                    " WHERE path = 'wiki/notes/alpha.md'")
        cur.execute("UPDATE pages_index SET embedding = NULL WHERE path = 'wiki/notes/beta.md'")
    findings = check.run_checks(conn, registry_path=registry)
    assert [f["severity"] for f in findings] == ["error", "warn"]


def test_render_states_clean_and_counts():
    assert "0 findings — clean" in check.render([], 17)
    rendered = check.render([{"severity": "error", "check": "x", "detail": "d"}], 3)
    assert "1 finding(s)" in rendered and "[ERROR] x: d" in rendered
