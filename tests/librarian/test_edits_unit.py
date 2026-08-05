"""`librarian.edits`: the declared additive edits, validated and performed by CODE.

These run over a plain directory rather than a git repo, because that is the whole surface: the
declaration says which page, which kind and which link, and this module answers "may I" and "here
it is". The git-level half — that the resulting diff passes `gate_body_rewrite` and reaches a
commit — is `test_processing_pg.py`'s, over real git, which is the only place it can be proven.

The mechanism exists because the previous one failed twice on the only two live runs it ever had:
the agent was allowed to make these edits itself, with a gate behind it, and it rewrote the body of
a human-authored page — then did it again on the corrective retry that was handed that exact
finding. So the tests below are as much about the REFUSALS as the writes: a declaration is
untrusted input, and half of it landing would be a worktree nobody can reason about.
"""
import pytest

from stigmergy.librarian import edits
from stigmergy.librarian import page as page_policy

EXISTING = "wiki/notes/Existing Note.md"
SECOND = "wiki/decisions/Existing Decision.md"
# An entity page is deliberately NOT a legal target: the zone gate confines every write to the six
# fast-lane folders, `wiki/entities/` is not one of them, and "nothing else may touch an existing
# page" is not narrowed for a convenient case.
ENTITY = "wiki/entities/Acme Corp.md"
NEW = "wiki/notes/A New Page.md"

PAGE_TEXT = """---
type: note
title: "Existing Note"
status: developing
related: ["[[Acme Corp]]"]
tags: [note]
---

# Existing Note

A paragraph a human wrote, which no librarian may touch.
"""


@pytest.fixture()
def worktree(tmp_path):
    """A knowledge tree with two existing in-lane pages, one entity page (a legal LINK target and an
    illegal EDIT target) and one page this capture just created — the shapes validation tells
    apart."""
    for rel, text in ((EXISTING, PAGE_TEXT),
                      (SECOND, PAGE_TEXT.replace("Existing Note", "Existing Decision")
                                        .replace("type: note", "type: decision")),
                      (ENTITY, PAGE_TEXT.replace("Existing Note", "Acme Corp")),
                      (NEW, PAGE_TEXT.replace("Existing Note", "A New Page"))):
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return tmp_path


def _edit(**over):
    base = {"path": EXISTING, "kind": "backlink", "link": "A New Page", "note": ""}
    base.update(over)
    return base


def _read(worktree, rel: str) -> str:
    return (worktree / rel).read_text(encoding="utf-8")


# ── the happy path, per kind ────────────────────────────────────────────────────────────────────
def test_a_backlink_adds_the_reciprocal_related_entry_and_nothing_else(worktree):
    changed, findings = edits.apply_declared(str(worktree), [_edit()], new_pages=[NEW])
    assert findings == []
    assert changed == [EXISTING]
    after = _read(worktree, EXISTING)
    assert page_policy.related_links(after) == ["[[Acme Corp]]", "[[A New Page]]"]
    assert "A paragraph a human wrote" in after
    assert "[!NOTE]" not in after


def test_an_overlap_adds_the_related_entry_AND_the_callout_because_the_contract_wants_both(
        worktree):
    """Criterion 11c: "both pages carry a mutual overlap callout AND a `related:` link". One
    declaration produces both halves, so a caller cannot get one and forget the other."""
    changed, findings = edits.apply_declared(
        str(worktree), [_edit(kind="overlap", note="covers the same ground")], new_pages=[NEW])
    assert findings == [] and changed == [EXISTING]
    after = _read(worktree, EXISTING)
    assert "[[A New Page]]" in page_policy.related_links(after)
    assert "> [!NOTE] Overlaps with [[A New Page]]" in after
    assert "covers the same ground" in after


def test_a_contradiction_uses_the_warning_callout_and_never_edits_the_older_claim(worktree):
    changed, _ = edits.apply_declared(
        str(worktree), [_edit(kind="contradiction", note="the capture says the opposite")],
        new_pages=[NEW])
    after = _read(worktree, EXISTING)
    assert changed == [EXISTING]
    assert "> [!WARNING] Contradiction with [[A New Page]]" in after
    # the older page's own sentence is untouched — never silently corrected
    assert "A paragraph a human wrote, which no librarian may touch." in after


def test_several_declarations_are_all_applied(worktree):
    changed, findings = edits.apply_declared(
        str(worktree),
        [_edit(), _edit(path=SECOND, kind="overlap", note="same client")],
        new_pages=[NEW])
    assert findings == []
    assert sorted(changed) == sorted([EXISTING, SECOND])


def test_a_declaration_that_changes_nothing_is_reported_as_unchanged_not_as_an_edit(worktree):
    """The link is already there. Writing the file anyway would put a no-op modification in the
    diff, which every gate then has to have an opinion about for no reason."""
    changed, _ = edits.apply_declared(str(worktree), [_edit(link="Acme Corp")], new_pages=[NEW])
    assert changed == []


def test_no_declarations_at_all_is_the_ordinary_case_and_writes_nothing(worktree):
    before = _read(worktree, EXISTING)
    changed, findings = edits.apply_declared(str(worktree), (), new_pages=[NEW])
    assert (changed, findings) == ([], [])
    assert _read(worktree, EXISTING) == before


# ── the refusals ────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("bad,code", [
    (_edit(path="ops/acl.json"), "outside-lane"),
    (_edit(path="wiki/entities/Acme Corp.md"), "outside-lane"),
    (_edit(path=".github/workflows/ci.yml"), "outside-lane"),
    (_edit(path="wiki/notes/.gitattributes"), "not-a-page"),
    (_edit(path="wiki/notes/README.txt"), "not-a-page"),
    (_edit(path="wiki/notes/Nowhere.md"), "missing-target"),
    (_edit(path=NEW), "own-page"),
    (_edit(link=""), "no-link"),
    (_edit(link="Not A Page Anywhere"), "dead-link"),
    (_edit(kind="overlap", note="   "), "no-note"),
    (_edit(kind="invented"), "unknown-kind"),
])
def test_a_bad_declaration_is_refused_by_name(worktree, bad, code):
    findings = edits.validate(str(worktree), [bad], new_pages=[NEW])
    assert [f.code for f in findings] == [code]
    assert all(f.gate == "edits" and f.severity == "veto" for f in findings)


def test_one_bad_declaration_refuses_the_whole_set_and_writes_nothing(worktree):
    """All-or-nothing on purpose: a half-applied set leaves a worktree whose diff nobody can reason
    about, and the corrective retry resets it anyway."""
    before = _read(worktree, EXISTING)
    changed, findings = edits.apply_declared(
        str(worktree), [_edit(), _edit(path="wiki/notes/Nowhere.md")], new_pages=[NEW])
    assert changed == []
    assert [f.code for f in findings] == ["missing-target"]
    assert _read(worktree, EXISTING) == before


def test_a_symlinked_target_is_refused_rather_than_written_through(worktree):
    """`open(p, "w")` follows a symlink. A page that is really a link elsewhere would be edited
    wherever it pointed, as the worker — so the target must be a file, not a link to one."""
    outside = worktree / "outside.md"
    outside.write_text("not a page\n", encoding="utf-8")
    link = worktree / "wiki" / "notes" / "Linked.md"
    link.symlink_to(outside)

    findings = edits.validate(str(worktree), [_edit(path="wiki/notes/Linked.md")],
                              new_pages=[NEW])
    assert [f.code for f in findings] == ["symlinked-target"]
    assert outside.read_text(encoding="utf-8") == "not a page\n"


@pytest.mark.parametrize("respelled,label", [
    ("wiki/notes/a new page.md", "lower-cased"),
    ("wiki/notes/A NEW PAGE.md", "upper-cased"),
])
def test_the_own_page_refusal_survives_a_respelling_of_the_new_pages_name(worktree, respelled,
                                                                         label):
    """The same defect as the confined-write one, in the other place that asks the same question.
    `new_pages` comes from `git diff --raw` and the check was `path in created`, an exact
    string test; on this filesystem `a new page.md` and `A New Page.md` are ONE file, so a
    declaration against the page this capture just created was validated as an edit to a DIFFERENT,
    pre-existing page — and then applied to the file the capture had written seconds earlier.

    Both questions go through `page.path_key` now, so they cannot answer differently."""
    findings = edits.validate(str(worktree), [_edit(path=respelled)], new_pages=[NEW])
    assert [f.code for f in findings] == ["own-page"], label


def test_a_symlinked_directory_component_is_refused_by_containment(worktree, tmp_path_factory):
    """`os.path.islink` guards the LEAF only, so a symlinked *directory* — a
    `wiki/concepts` link merged into the knowledge repo — was traversed and an existing `.md`
    behind it written through, outside the worktree, as the worker. Every check before this one is a
    check on the path's SHAPE, and a shape check cannot see a symlink.

    `tmp_path_factory`, not `tmp_path`: the worktree IS `tmp_path`, so a victim created under it
    would be genuinely inside the worktree and this test would assert nothing."""
    outside = tmp_path_factory.mktemp("elsewhere")
    (outside / "Victim.md").write_text("a page outside the worktree\n", encoding="utf-8")
    (worktree / "wiki" / "concepts").symlink_to(outside, target_is_directory=True)

    findings = edits.validate(str(worktree), [_edit(path="wiki/concepts/Victim.md")],
                              new_pages=[NEW])

    assert [f.code for f in findings] == ["outside-worktree"]
    assert (outside / "Victim.md").read_text(encoding="utf-8") == "a page outside the worktree\n"


def test_an_ordinary_in_lane_target_is_the_benign_twin_for_containment(worktree):
    """The specificity half: the containment check must not refuse the everyday case it sits in
    front of, or no declared edit could ever be applied."""
    assert edits.validate(str(worktree), [_edit()], new_pages=[NEW]) == []


def test_a_refusal_never_quotes_the_page_body_back(worktree):
    """The same discipline every other refusal follows: a path, a kind and a link name — never
    content, because content here is somebody's page and, on the added-line side, captured
    material."""
    findings = edits.validate(str(worktree), [_edit(path="wiki/notes/Nowhere.md")],
                             new_pages=[NEW])
    assert "A paragraph a human wrote" not in " ".join(f.message for f in findings)


# ── link resolution reads the real graph ────────────────────────────────────────────────────────
def test_page_names_reads_every_page_in_the_knowledge_tree(worktree):
    """Including pages OUTSIDE the creatable folders: an entity page cannot be edited and is perfectly
    linkable, so link resolution reads the whole graph while edit targets do not."""
    assert {"Existing Note", "Acme Corp", "A New Page"} <= edits.page_names(str(worktree))


def test_page_names_ignores_dotfiles(worktree):
    (worktree / "wiki" / "notes" / ".hidden.md").write_text("x", encoding="utf-8")
    assert ".hidden" not in edits.page_names(str(worktree))
