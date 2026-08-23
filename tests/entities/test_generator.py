"""`entities.generator` — the registry derived from `wiki/entities/*.md`, and its
idempotence/drift proof.

Reads real files off a real (throwaway) git checkout — the generator itself never touches git, so
these tests build the ON-DISK shape directly (`tests/entities/conftest.py`'s `build_repo`) rather
than pushing anything through the remote the fixture also sets up.
"""
import json
import os

import pytest

from stigmergy.entities import birth, generator
from stigmergy.entities.errors import EntityError
from stigmergy.kernel.normalize import normalize
from tests.entities import conftest as fx


def _write_page(clone: str, name: str, entity_type: str, aliases=()) -> None:
    path = os.path.join(clone, "wiki", "entities", f"{name}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(fx.page_text(name, entity_type, aliases))


# ── read_entity_pages: the two ways a page can be unreadable-as-an-identity ──────────────────────
def test_read_entity_pages_returns_every_page_sorted_by_id(repo):
    _remote, clone = repo
    entities = generator.read_entity_pages(clone)
    assert [e.canonical_id for e in entities] == ["jordan-reyes", "stigmergy"]
    steward = next(e for e in entities if e.canonical_id == "jordan-reyes")
    assert steward.name == "Jordan Reyes"
    assert steward.aliases == ("Jordan Reyes Gaya",)
    assert steward.entity_type == "person"


def test_read_entity_pages_is_empty_when_the_folder_does_not_exist(tmp_path):
    assert generator.read_entity_pages(str(tmp_path)) == []


def test_a_page_with_no_title_is_an_error_not_a_silent_skip(repo):
    """Module docstring: "a page this cannot read is an ERROR, never a skip" — a silently dropped
    entity page is a registry that quietly stops resolving a name the graph used to anchor on."""
    _remote, clone = repo
    path = os.path.join(clone, "wiki", "entities", "No Title.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write('---\ntype: entity\ntitle: ""\nentity_type: organization\n---\n\n# body\n')
    with pytest.raises(EntityError, match="no `title`"):
        generator.read_entity_pages(clone)


def test_two_pages_that_slug_to_the_same_id_is_an_error_naming_both(repo):
    """Two DISTINCT filenames (case-insensitive collision is a different rule, in `birth`'s file
    check) whose titles still slug to one id: a trailing comma `slugify` strips."""
    _remote, clone = repo
    _write_page(clone, "Acme", "organization")
    _write_page(clone, "Acme,", "organization")     # same slug: "acme"
    with pytest.raises(EntityError, match="both produce"):
        generator.read_entity_pages(clone)


# ══════════════════════════════════════════════════════════════════════════════════════════════
# `read_entity_pages` used to refuse duplicate SLUGS but not duplicate MATCHER keys.
# `Acme` and `Acme Corp.` keep distinct ids (slugify does not strip "corp") while claiming one
# `normalize` key — a registry that LOOKS unambiguous and resolves last-wins.
# ══════════════════════════════════════════════════════════════════════════════════════════════
def test_duplicate_match_keys_are_refused_even_when_the_ids_are_distinct(repo):
    _remote, clone = repo
    _write_page(clone, "Acme", "organization")
    _write_page(clone, "Acme Corp.", "organization")
    # the ids really are distinct — proving this is the MATCHER collision, not the id one
    assert generator.canonical_id_for("Acme") != generator.canonical_id_for("Acme Corp.")
    assert normalize("Acme") == normalize("Acme Corp.")
    with pytest.raises(EntityError, match="resolution"):
        generator.read_entity_pages(clone)


def test_the_benign_twin_two_genuinely_distinct_entities_still_passes(repo):
    """The benign twin: two entities that really are two entities must not trip the rule."""
    _remote, clone = repo
    _write_page(clone, "Acme", "organization")
    _write_page(clone, "Zenith Systems", "organization")
    entities = generator.read_entity_pages(clone)
    ids = {e.canonical_id for e in entities}
    assert {"acme", "zenith-systems"} <= ids


# ── registry_of / derive_registry: the shared indexing code, exercised both ways ─────────────────
def test_derive_registry_indexes_ids_names_and_aliases(repo):
    _remote, clone = repo
    reg = generator.derive_registry(clone)
    assert reg.canonical_id("Jordan Reyes") == "jordan-reyes"
    assert reg.canonical_id("Jordan Reyes Gaya") == "jordan-reyes"   # the alias
    assert reg.canonical_id("jordan-reyes") == "jordan-reyes"        # the id itself


def test_committed_registry_of_a_missing_file_is_empty(tmp_path):
    reg = generator.committed_registry(str(tmp_path))
    assert reg.entities == {}


def test_a_project_entity_round_trips_from_birth_to_the_registry(repo):
    """The seventh entity type reaches the registry as itself.

    `project` is the first value added after `ENTITY_TYPES` shipped closed, and it crosses three
    spellings of the vocabulary on the way to being resolvable: `birth.prepare` validates it
    against the generator's tuple, `birth.render_page` writes it into the page's `entity_type`,
    and `read_entity_pages` reads it back as the registry's `type`. A type accepted at the mint
    gate but lost before the registry would mint a page nothing can anchor through — mentions
    resolve against `ops/entity-registry.json`, never against the page. Written through the
    real template the fixture repo carries, because the template is what a real mint renders.
    """
    _remote, clone = repo
    with open(os.path.join(clone, "ops", "templates", "entity.md"), encoding="utf-8") as f:
        template = f.read()
    proposal = birth.prepare(canonical_id="atlas", name="Atlas", entity_type="project",
                             registry=generator.derive_registry(clone))
    with open(os.path.join(clone, "wiki", "entities", "Atlas.md"), "w", encoding="utf-8") as f:
        f.write(birth.render_page(template, proposal, today="2026-08-16",
                                  body=birth.prepare_body(summary="Atlas is a project.")))

    reg = generator.derive_registry(clone)
    assert reg.entities["atlas"]["type"] == "project"
    assert reg.canonical_id("Atlas") == "atlas"


# ── compare/check: every divergence names the fix command ────────────────────────────────────────
def test_check_reports_no_drift_on_a_freshly_seeded_repo(repo):
    _remote, clone = repo
    outcome = generator.check(clone)
    assert outcome.divergences == []
    assert outcome.page_count == 2
    assert outcome.changed is False


def test_check_reports_an_unregistered_page_and_names_the_fix(repo):
    _remote, clone = repo
    _write_page(clone, "Globex", "organization")
    outcome = generator.check(clone)
    assert outcome.changed is True
    [div] = [d for d in outcome.divergences if d.entity == "globex"]
    assert "does not register" in div.message
    assert generator.FIX_COMMAND in div.message


def test_check_reports_a_registry_entry_with_no_page(repo):
    _remote, clone = repo
    registry_path = generator.registry_path(clone)
    with open(registry_path) as f:
        data = json.load(f)
    data["entities"]["ghost"] = {"name": "Ghost", "type": "organization", "aliases": []}
    with open(registry_path, "w") as f:
        json.dump(data, f)
    outcome = generator.check(clone)
    [div] = [d for d in outcome.divergences if d.entity == "ghost"]
    assert "no page" in div.message
    assert generator.FIX_COMMAND in div.message


@pytest.mark.parametrize("mutate,expect_snippet", [
    (lambda data: data["entities"]["stigmergy"].update(type="tool"), "entity_type"),
    (lambda data: data["entities"]["stigmergy"].update(aliases=[]), "alias"),
])
def test_check_reports_type_and_alias_drift(repo, mutate, expect_snippet):
    _remote, clone = repo
    registry_path = generator.registry_path(clone)
    with open(registry_path) as f:
        data = json.load(f)
    mutate(data)
    with open(registry_path, "w") as f:
        json.dump(data, f)
    outcome = generator.check(clone)
    assert outcome.changed is True
    assert any(expect_snippet in d.message for d in outcome.divergences)
    assert all(generator.FIX_COMMAND in d.message for d in outcome.divergences)


# ── regenerate is idempotent and --check is its own proof ────────────────────────────────────────
def test_regenerate_on_a_clean_repo_writes_nothing_new(repo):
    _remote, clone = repo
    before = generator.snapshot(clone)
    outcome = generator.regenerate(clone)
    assert outcome.changed is False
    assert generator.snapshot(clone) == before


def test_regenerate_fixes_a_planted_drift_and_a_second_run_is_a_noop(repo):
    """Idempotence, run TWICE: the second run must report `changed=False` — the property a test
    that only asserts "does not raise" leaves entirely unproven."""
    _remote, clone = repo
    _write_page(clone, "Globex", "organization")
    first = generator.regenerate(clone)
    assert first.changed is True
    second = generator.regenerate(clone)
    assert second.changed is False
    assert generator.check(clone).divergences == []


def test_check_after_regenerate_finds_no_drift_ever_again(repo):
    """`--check`'s own proof of idempotence: once regenerated, `--check` is clean."""
    _remote, clone = repo
    _write_page(clone, "Globex", "organization", aliases=["Globex Corp"])
    generator.regenerate(clone)
    outcome = generator.check(clone)
    assert outcome.divergences == []
    assert outcome.page_count == 3


def test_regenerate_survives_an_interrupted_write_by_never_leaving_a_half_written_file(repo, monkeypatch):
    """`save_registry` writes a temp file and `os.replace`s it (generator.regenerate's docstring):
    an interrupted regeneration must leave the PREVIOUS registry intact. Simulated by making the
    replace step itself raise, which is the one place a real interruption could land."""
    _remote, clone = repo
    before = generator.snapshot(clone)
    real_replace = os.replace

    def _boom(*a, **kw):
        raise OSError("simulated interruption")

    monkeypatch.setattr(os, "replace", _boom)
    with pytest.raises(OSError):
        generator.regenerate(clone)
    monkeypatch.setattr(os, "replace", real_replace)
    assert generator.snapshot(clone) == before


# ══════════════════════════════════════════════════════════════════════════════════════════════
# READ-ONLY against the real knowledge repo. Never writes to it — every call below is a pure
# read (`generator.check`/`snapshot`/`derive_registry`), and `save_registry` below is pointed at
# a TEMPORARY path, never at the real repo's own file.
# ══════════════════════════════════════════════════════════════════════════════════════════════
REAL_REPO = os.environ.get("STIGMERGY_REPO", "../stigmergy-brain")


def _real_repo_or_skip():
    """The live-repo guard: what these tests need is a POPULATION, not merely a directory.

    Checking that `wiki/entities/` EXISTS is not enough. The entity zone survives an emptied
    corpus as a `.gitkeep` skeleton, so the directory can be there while holding no entity at
    all — and an empty registry has no drift and no colliding match keys BY CONSTRUCTION.
    Asserting over zero entities measures the seed, not the code.

    Skipping rather than weakening keeps the check honest AND self-re-arming: the moment the
    first entity is minted through the governed door, both tests start running again with no
    edit."""
    entities_dir = os.path.join(REAL_REPO, "wiki", "entities")
    if not os.path.isdir(entities_dir):
        pytest.skip(f"no knowledge repo at {REAL_REPO} on this machine — this is the read-only "
                    f"check against a live knowledge repo, skipped where it does not exist")
    if not [n for n in os.listdir(entities_dir) if n.endswith(".md")]:
        pytest.skip(f"{REAL_REPO} holds no entity pages — drift and collision are vacuous over "
                    f"zero entities. Re-arms as soon as one is minted.")
    return REAL_REPO


def test_the_real_repos_registry_has_no_drift():
    repo = _real_repo_or_skip()
    outcome = generator.check(repo)
    assert outcome.page_count >= 1
    assert outcome.divergences == [], [d.message for d in outcome.divergences]


def test_the_real_repos_registry_is_byte_identical_to_a_fresh_regeneration(tmp_path):
    """The stronger half of idempotence ("one canonicalization commit allowed if bytes differ"):
    on the real repo, bytes do not even differ. `save_registry` is pointed at a path under
    `tmp_path`, never at the real file — this never writes to the knowledge repo."""
    repo = _real_repo_or_skip()
    from stigmergy.kernel.registry import save_registry

    on_disk = generator.snapshot(repo)
    assert on_disk is not None
    derived = generator.derive_registry(repo)
    scratch = str(tmp_path / "entity-registry.json")
    save_registry(scratch, derived)
    with open(scratch, encoding="utf-8") as f:
        regenerated = f.read()
    assert regenerated == on_disk


def test_the_real_repo_has_no_duplicate_match_keys():
    """Read-only against the live corpus: `read_entity_pages` must not raise on the real repo —
    whatever entities exist there today collide with nothing."""
    repo = _real_repo_or_skip()
    entities = generator.read_entity_pages(repo)      # must not raise
    # `>= 1`, deliberately: the guard above skips an empty repo entirely, and pinning a specific
    # set of entity names would make this a test of the seed rather than of the collision rule.
    # The property is "whatever is there today collides with nothing".
    assert len(entities) >= 1


# ── the lifecycle, page side: `approved_by` names who introduced an identity ─────────────────────
def test_approved_by_is_read_off_the_page_and_absent_reads_as_nobody(repo):
    """OLD BEHAVIOUR: `approved_by: ""` present-and-empty meant a PROPOSAL waiting on a steward,
    and a page's extra spellings could sit on `proposed_aliases` until one confirmed them. the
    capture-is-the-approval change:
    the capture is the approval, so there is no waiting state and no second alias list — the field
    is read as one fact, "who introduced this identity", and a page written before it existed names
    nobody in particular.
    """
    _remote, clone = repo
    _write_page(clone, "Scircle", "organization", aliases=["S-Circle"])
    path = os.path.join(clone, "wiki", "entities", "Scircle.md")
    with open(path, encoding="utf-8") as f:
        text = f.read()
    with open(path, "w", encoding="utf-8") as f:
        f.write(text.replace('role: ""', f'role: ""\n{generator.APPROVED_BY_KEY}: "marc"'))
    _write_page(clone, "Globex", "organization")
    path = os.path.join(clone, "wiki", "entities", "Globex.md")
    with open(path, encoding="utf-8") as f:
        text = f.read()
    with open(path, "w", encoding="utf-8") as f:
        f.write(text.replace('role: ""', f'role: ""\n{generator.APPROVED_BY_KEY}: ""'))

    by_id = {e.canonical_id: e for e in generator.read_entity_pages(clone)}
    assert by_id["scircle"].approved_by == "marc"
    assert by_id["scircle"].aliases == ("S-Circle",)
    assert by_id["globex"].approved_by == ""            # present and empty: nobody in particular
    assert by_id["jordan-reyes"].approved_by == ""      # absent entirely: the same thing

    reg = generator.derive_registry(clone)
    assert reg.entities["scircle"]["approved_by"] == "marc"
    assert reg.entities["globex"]["approved_by"] == ""
    assert reg.canonical_id("S-Circle") == "scircle"    # a spelling resolves, born or inherited


def test_check_reports_a_lifecycle_the_registry_has_not_followed(repo):
    """The page and the registry disagreeing about who introduced an identity means one of the two
    was written by hand — a registry fact like the type, and drift that names whose the fix is."""
    _remote, clone = repo
    page = os.path.join(clone, "wiki", "entities", "Stigmergy.md")
    with open(page, encoding="utf-8") as f:
        text = f.read()
    with open(page, "w", encoding="utf-8") as f:
        f.write(text.replace('role: ""', f'role: ""\n{generator.APPROVED_BY_KEY}: "marc"'))
    messages = [d.message for d in generator.check(clone).divergences]
    assert any("introduced by marc" in m and "introduced before approvals were recorded" in m
               and generator.FIX_COMMAND in m for m in messages), messages
    # ...and regenerating clears it, which is what makes the check falsifiable
    generator.regenerate(clone)
    assert generator.check(clone).divergences == []
