import os

import pytest

from stigmergy.knowledge import layered

PAGE = """---
id: page_1
type: entity
title: Acme
status: seed
created: '2026-09-01'
updated: '2026-09-01'
acl:
- deals
sources: []
entity_type: organization
aliases: []
---

# Acme

A restricted fact.
"""


def test_a_view_refuses_a_symlink_that_would_pull_another_layer_in(tmp_path):
    restricted = tmp_path / "layers" / "deals" / "wiki" / "entities"
    restricted.mkdir(parents=True)
    (restricted / "Acme.md").write_text(PAGE, encoding="utf-8")
    os.symlink(tmp_path / "layers" / "deals" / "wiki", tmp_path / "wiki")

    with pytest.raises(layered.LayerProjectionError):
        layered.build_view(str(tmp_path), "public", root=str(tmp_path.parent / "view"))
    assert not (tmp_path.parent / "view").exists()


def _page(root, path, kind, title, acl, body):
    extra = "entity_type: organization\naliases: []\n" if kind == "entity" else ""
    audience = "null" if acl is None else f"\n- {acl}"
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(f"---\nid: page_{abs(hash(path))}\ntype: {kind}\ntitle: {title}\nstatus: seed\n"
                      f"created: '2026-09-01'\nupdated: '2026-09-01'\nacl: {audience}\nsources: []\n{extra}---\n\n"
                      f"# {title}\n\n{body}\n", encoding="utf-8")


def test_a_name_two_layers_use_for_different_pages_is_not_a_violation(tmp_path):
    from stigmergy.knowledge.lint import check

    _page(tmp_path, "wiki/entities/Acme.md", "entity", "Acme", None, "A robot maker.")
    _page(tmp_path, "layers/deals/wiki/concepts/Acme.md", "concept", "Acme", "deals", "A pricing approach.")
    _page(tmp_path, "layers/deals/wiki/notes/Memo.md", "note", "Memo", "deals", "Priced with [[Acme]].")
    assert [item.code for item in check(str(tmp_path))] == []

    _page(tmp_path, "layers/deals/wiki/entities/Acme.md", "entity", "Acme", "deals", "Acme pays yearly.")
    assert [(item.path, item.code) for item in check(str(tmp_path))] == [
        ("layers/deals/wiki/notes/Memo.md", "ambiguous-link")]
