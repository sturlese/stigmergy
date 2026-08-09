"""`librarian.acl_rules`: the on-disk-dialect adapter over `kernel.acl`, and its fail-closed
posture on anything malformed. `resolve()`'s happy path is already exercised
end to end by `test_processing_pg.py`'s fixture repo (the on-disk dialect, `ops/acl.json`); this
file targets the adapter's edge cases directly.
"""
import json

import pytest

from stigmergy.librarian import acl_rules
from stigmergy.librarian.errors import LibrarianConfigError


def _write(tmp_path, content: dict) -> str:
    path = tmp_path / "acl.json"
    path.write_text(json.dumps(content), encoding="utf-8")
    return str(path)


# ── load(): missing / malformed / dialect variants ──────────────────────────────────────────
def test_load_with_no_path_at_all_returns_none_open_corpus():
    assert acl_rules.load(None) is None


def test_load_with_a_path_that_does_not_exist_returns_none():
    assert acl_rules.load("/does/not/exist/acl.json") is None


def test_load_raises_on_unparseable_json(tmp_path):
    path = tmp_path / "acl.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(LibrarianConfigError, match="unreadable"):
        acl_rules.load(str(path))


def test_load_raises_when_the_top_level_is_not_an_object(tmp_path):
    path = _write(tmp_path, [])
    with pytest.raises(LibrarianConfigError, match="top level must be an object"):
        acl_rules.load(path)


def test_load_raises_when_rules_is_not_a_list(tmp_path):
    path = _write(tmp_path, {"rules": "not-a-list"})
    with pytest.raises(LibrarianConfigError, match="'rules' must be a list"):
        acl_rules.load(path)


def test_load_raises_when_a_rule_is_not_an_object(tmp_path):
    path = _write(tmp_path, {"rules": ["not-a-dict"]})
    with pytest.raises(LibrarianConfigError, match="must be an object"):
        acl_rules.load(path)


def test_load_raises_when_an_on_disk_rule_has_neither_matcher_nor_path(tmp_path):
    path = _write(tmp_path, {"rules": [{"acl": ["finance"]}]})
    with pytest.raises(LibrarianConfigError, match="neither a reader matcher"):
        acl_rules.load(path)


def test_load_raises_on_a_wildcard_in_the_middle_of_a_path_pattern(tmp_path):
    """The resolver matches by prefix; a pattern like `wiki/*/finance/**` is not faithfully
    translatable and must be refused rather than guessed at (acl_rules.py's own docstring:
    "guessing wrong on an access-control file is the one failure mode it must not have")."""
    path = _write(tmp_path, {"rules": [{"path": "wiki/*/finance/**", "acl": []}]})
    with pytest.raises(LibrarianConfigError, match="not a plain prefix"):
        acl_rules.load(path)


def test_load_raises_when_a_rule_has_no_audiences_and_no_acl_list(tmp_path):
    path = _write(tmp_path, {"rules": [{"path": "wiki/**"}]})
    with pytest.raises(LibrarianConfigError, match="no 'audiences' and no 'acl'"):
        acl_rules.load(path)


def test_load_raises_when_default_is_not_a_list(tmp_path):
    path = _write(tmp_path, {"default": "all", "rules": []})
    with pytest.raises(LibrarianConfigError, match="'default' must be a list"):
        acl_rules.load(path)


def test_load_the_on_disk_dialect_translates_path_and_acl_into_path_prefix_and_audiences(tmp_path):
    path = _write(tmp_path, {"default": [], "rules": [
        {"path": "wiki/finance/**", "acl": ["finance", "leadership"]},
    ]})
    config = acl_rules.load(path)
    assert acl_rules.resolve(config, "wiki/finance/q3.md") == ["finance", "leadership"]
    assert acl_rules.resolve(config, "wiki/notes/ordinary.md") is None


def test_load_the_on_disk_dialect_with_an_empty_acl_list_resolves_to_open(tmp_path):
    """Empty means open (module docstring): a resolved empty list yields `None`, the page
    contract's own way to spell "no `acl:` line"."""
    path = _write(tmp_path, {"default": [], "rules": [{"path": "wiki/**", "acl": []}]})
    config = acl_rules.load(path)
    assert acl_rules.resolve(config, "wiki/notes/ordinary.md") is None


def test_load_the_reader_dialect_directly_still_works(tmp_path):
    path = _write(tmp_path, {"default": ["all"], "rules": [
        {"path_prefix": "wiki/finance", "audiences": ["finance"]},
    ]})
    config = acl_rules.load(path)
    assert acl_rules.resolve(config, "wiki/finance/q3.md") == ["finance"]
    assert acl_rules.resolve(config, "wiki/notes/ordinary.md") == ["all"]


def test_load_rejects_an_invalid_audience_label_containing_a_comma(tmp_path):
    """`acl_rules.load`'s own docstring promises "Raises `LibrarianConfigError` on anything
    malformed", and label validation is delegated to `kernel.acl._check_labels`, which raises a
    bare `ValueError` — not a `LibrarianConfigError`, not even a `LibrarianError` subclass. Since
    `cli.py.main()` only catches `LibrarianConfigError`/`LibrarianError` around startup, an
    unwrapped delegation meant a malformed audience label in `ops/acl.json` (a comma, or a blank
    label) crashed the CLI with a raw Python traceback instead of the clean, loud, one-line config
    error every other malformed ACL config produces — a refusal answered with a traceback, at a
    different seam. `_guard_delegation` wraps BOTH delegations — this branch's `_check_labels` and
    the pure-reader-dialect branch's `acl_model.load_acl_config_text` — and this pins that the
    promised type is what a caller actually gets."""
    path = _write(tmp_path, {"default": [], "rules": [
        {"path": "wiki/**", "acl": ["finance,leadership"]},
    ]})
    with pytest.raises(LibrarianConfigError):
        acl_rules.load(path)


# ── resolve(): the label lookup itself ──────────────────────────────────────────────────────
def test_resolve_with_no_config_at_all_is_open():
    assert acl_rules.resolve(None, "wiki/notes/anything.md") is None


# ── a reader matcher is not, by itself, a reader rule ─────────────────────────────────────────
def test_a_half_migrated_rule_keeps_the_audiences_it_names_in_acl(tmp_path):
    """OLD BEHAVIOUR: `{"path_prefix": ..., "acl": [...]}` was adopted verbatim because it carries
    a reader MATCHER, the final normalization then kept only `(*_MATCHERS, "audiences")` — dropping
    `acl` — and `resolve_acl`'s `rule["audiences"]` raised a bare `KeyError` on every page the rule
    matched. Not a `LibrarianError`, so it escaped every handler, per item, from a config that had
    passed the startup validation whose whole job is to be the loud place this is caught.

    Dialect detection is per KEY, so a rule can migrate its matcher and not its labels. The
    module's own docstring promises exactly this shape is readable ("a half-migrated file is still
    readable"); translating `acl` here is the same faithful translation `_translate_rule` already
    performs for the fully on-disk dialect."""
    path = _write(tmp_path, {"default": [], "rules": [
        {"path_prefix": "wiki/private", "acl": ["leadership"]},
    ]})
    config = acl_rules.load(path)
    assert acl_rules.resolve(config, "wiki/private/board.md") == ["leadership"]
    assert acl_rules.resolve(config, "wiki/notes/ordinary.md") is None


def test_a_rule_with_a_matcher_and_no_audience_at_all_is_refused_at_startup(tmp_path):
    """OLD BEHAVIOUR: accepted at startup, then `KeyError: 'audiences'` per matching item.

    The other half of the same defect, and the worse one when the rule does NOT match: it fell
    through to `default`, which in the real `ops/acl.json` is `[]` — OPEN. A rule written to
    restrict something must never resolve to something weaker than intended; that is why the
    unsupported-matcher check above already refuses rather than letting a never-matching rule
    through, and this shape had escaped it."""
    path = _write(tmp_path, {"default": [], "rules": [{"path_contains": "board"}]})
    with pytest.raises(LibrarianConfigError, match="names no audience"):
        acl_rules.load(path)


def test_a_rule_naming_two_different_audiences_is_refused_rather_than_guessed_at(tmp_path):
    path = _write(tmp_path, {"default": [], "rules": [
        {"path_prefix": "wiki/private", "audiences": ["leadership"], "acl": ["all"]},
    ]})
    with pytest.raises(LibrarianConfigError, match="different audiences"):
        acl_rules.load(path)


def test_the_same_audiences_in_a_different_order_are_the_same_rule(tmp_path):
    """The benign twin of that refusal, and the one it got wrong first time: the check compared
    ORDERED lists, so `["a","b"]` vs `["b","a"]` stopped the worker — over a config that had been
    resolving correctly — with a message that rendered both sides `sorted()`, telling the operator
    to reconcile two lists it had just printed identical. An audience list is a SET everywhere it
    is used: `resolve_acl` hands it on and `server.acl.visible()` does membership."""
    path = _write(tmp_path, {"default": ["all"], "rules": [
        {"path_prefix": "wiki/private", "audiences": ["finance", "leadership"],
         "acl": ["leadership", "finance"]},
    ]})
    config = acl_rules.load(path)
    assert acl_rules.resolve(config, "wiki/private/board.md") == ["finance", "leadership"]


def test_a_pure_reader_rule_is_still_adopted_unchanged(tmp_path):
    """The benign twin — it passes without the fix, which is the point: tightening what a rule
    must name may not disturb the dialect this adapter exists to pass through untouched."""
    path = _write(tmp_path, {"default": ["all"], "rules": [
        {"path_prefix": "wiki/finance", "audiences": ["finance"]},
    ]})
    config = acl_rules.load(path)
    assert acl_rules.resolve(config, "wiki/finance/q3.md") == ["finance"]
    assert acl_rules.resolve(config, "wiki/elsewhere.md") == ["all"]


def test_an_empty_acl_on_a_half_migrated_rule_still_spells_open(tmp_path):
    """Empty audiences are how the on-disk dialect says "open", and a half-migrated rule may say it
    too — the refusal above is for a rule that names no audience AT ALL, which is a different
    thing from one that names none deliberately. (This one also fails without the fix: on `main`
    the `acl` key was dropped and the rule reached `resolve_acl` with no `audiences` to read.)"""
    path = _write(tmp_path, {"default": ["all"], "rules": [
        {"path_prefix": "wiki/open", "acl": []},
    ]})
    config = acl_rules.load(path)
    assert acl_rules.resolve(config, "wiki/open/notes.md") is None
    assert acl_rules.resolve(config, "wiki/elsewhere.md") == ["all"]
