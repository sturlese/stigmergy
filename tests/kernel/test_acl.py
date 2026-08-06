"""ACL resolution: config validation, first-match rules, view intersection, wiring."""
import json

import pytest

from stigmergy.kernel.acl import load_acl_config, resolve_acl, view_acl


def _config(tmp_path, cfg):
    p = tmp_path / "acl.json"
    p.write_text(json.dumps(cfg))
    return str(p)


def test_load_acl_config_none_and_validation(tmp_path):
    assert load_acl_config(None) is None
    ok = load_acl_config(_config(tmp_path, {
        "default": ["all"],
        "rules": [{"unit": "Finance", "audiences": ["finance"]}]}))
    assert ok["default"] == ["all"]
    with pytest.raises(ValueError, match="non-empty 'audiences'"):
        load_acl_config(_config(tmp_path, {"rules": [{"unit": "X", "audiences": []}]}))
    with pytest.raises(ValueError, match="rule needs one of"):
        load_acl_config(_config(tmp_path, {"rules": [{"audiences": ["a"]}]}))
    with pytest.raises(ValueError, match="'default' must be"):
        load_acl_config(_config(tmp_path, {"default": [], "rules": []}))


def test_load_acl_config_rejects_labels_that_break_csv_serialization(tmp_path):
    """Audience labels travel comma-separated downstream — the `acl: [a, b]` flow list in page
    frontmatter, and the `acl` column the index reads it into: a comma inside a label would
    silently split into two audiences at enforcement time; an empty label vanishes. Both must
    fail loudly at config load."""
    with pytest.raises(ValueError, match="invalid audience label"):
        load_acl_config(_config(tmp_path, {
            "rules": [{"unit": "X", "audiences": ["sales,leadership"]}]}))
    with pytest.raises(ValueError, match="invalid audience label"):
        load_acl_config(_config(tmp_path, {"default": ["  "], "rules": []}))


def test_resolve_acl_first_match_wins(tmp_path):
    cfg = load_acl_config(_config(tmp_path, {
        "default": ["all"],
        "rules": [
            {"path_contains": "board", "audiences": ["leadership"]},
            {"unit": "Clients", "audiences": ["sales", "leadership"]},
            {"entity_kind": "prospect", "audiences": ["sales"]},
        ]}))
    assert resolve_acl(cfg, "/X/Clients/board minutes.pdf", "Clients", None) == ["leadership"]
    assert resolve_acl(cfg, "/X/Clients/1. Acme/report.pdf", "Clients", "tracked") == ["sales", "leadership"]
    assert resolve_acl(cfg, "/X/Pipeline/Evaluating/Hooli/deck.pdf", "Pipeline", "prospect") == ["sales"]
    assert resolve_acl(cfg, "/X/Product/roadmap.md", "Product", None) == ["all"]
    assert resolve_acl(None, "/anything", "Clients", None) is None      # ACLs off -> no field


def test_view_acl_is_intersection():
    assert view_acl([["sales", "leadership"], ["finance", "leadership"]]) == ["leadership"]
    assert view_acl([["sales"], None]) == ["sales"]                  # None members don't restrict
    assert view_acl([None, None]) is None                            # open members -> open view
    assert view_acl([["sales"], ["finance"]]) == []                  # disjoint -> restricted, never open


# `test_visible_rule` lived here, over `kernel.acl.visible`. That predicate is gone — it had no
# production caller, it was the fail-open half, and it sat in the module every package may import
# (a pre-publication audit; `tests/test_contract_parity.py` carries the record and fails if one
# comes back). The truth table it asserted is not lost: `tests/server/test_acl_visibility.py` pins
# every one of its cases against `server.acl.visible`, which is now the only implementation.
# Two tests used to close this file — `test_worker_stamps_pages_facts_and_result` and
# `test_view_page_carries_intersection`. They drove an ingest worker and a legacy view builder
# end to end, and neither exists any more. The PROPERTIES they held are still held, by tests that
# run against what does exist: the stamped-audience half by `tests/librarian/`
# (`acl_rules.resolve` feeding `processing._stamp`), and the intersection half by
# `tests/views/test_render.py` plus `tests/test_contract_parity.py`'s `[]`-stays-`[]` invariant.
# Recorded rather than dropped in silence — a check that stops running must be impossible to miss.
