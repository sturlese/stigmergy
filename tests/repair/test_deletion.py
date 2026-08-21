"""The `delete` kind's CODE half, as a pure function of a worktree: which pages go, which pages
refer to them, each referring page's frontmatter scrubbed, and every bound the written half has to
satisfy (ADR 043 D1). The bodies are the writer's — `test_sweep.py` — and arrive here only as
bytes `validate` judges.

No Postgres, no git, no model. The property this file exists to hold is the one the frozen
contract linter judges: **after the sweep, nothing in the corpus refers to a page that is gone.**
So every link question is asked the way `stigmergy_lint.py` asks it — `Path(target).stem` for a
link target, code fences and inline code stripped first — and the tests say so, because a scanner
that drifts from the linter produces a plan that passes propose time and vetoes at apply time.
"""
import hashlib
import json
import os

import pytest

from stigmergy.repair import deletion
from stigmergy.repair.errors import RepairError


def _write(root: str, relpath: str, text: str) -> str:
    path = os.path.join(root, *relpath.split("/"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return relpath


def _page(title: str, *, page_type: str = "note", related=(), sources=(), body: str = "",
          extra=()) -> str:
    front = [f"type: {page_type}", f'title: "{title}"', "status: developing",
             "created: 2026-01-01", "updated: 2026-01-01", "tags: [note]",
             f"related: {json.dumps(list(related), ensure_ascii=False)}",
             f"sources: {json.dumps(list(sources), ensure_ascii=False)}", *extra]
    return "---\n" + "\n".join(front) + "\n---\n\n" + (body or f"# {title}\n\nSomething.\n")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _scrub_op(ops, path: str) -> dict:
    return next(o for o in ops if o["path"] == path and o["op"] == deletion.OP_SCRUB)


def _after(ops, path: str) -> str:
    return _scrub_op(ops, path)["planned_after"]


# ── what a plan IS ────────────────────────────────────────────────────────────────────────────
def test_the_plan_deletes_the_named_page_and_scrubs_every_page_that_names_it(tmp_path):
    """Red before `deletion.py` existed: nothing computed a sweep at all, so a deletion was either
    one file removed and a corpus full of dead links, or a judgment call for a model."""
    root = str(tmp_path)
    _write(root, "wiki/notes/Doomed.md", _page("Doomed"))
    _write(root, "wiki/notes/Cites It.md",
           _page("Cites It", related=["[[Doomed]]"], body="# Cites It\n\nSee [[Doomed]].\n"))
    _write(root, "wiki/notes/Ignores It.md", _page("Ignores It"))

    ops = deletion.plan(root, ["wiki/notes/Doomed.md"])

    assert [o["op"] for o in ops] == [deletion.OP_DELETE, deletion.OP_SCRUB]
    assert ops[0] == {"op": deletion.OP_DELETE, "path": "wiki/notes/Doomed.md"}
    assert ops[1]["path"] == "wiki/notes/Cites It.md"
    assert "wiki/notes/Ignores It.md" not in deletion.scrubbed_paths(ops)


def test_the_scrub_op_carries_the_bytes_it_was_computed_from_and_the_bytes_it_would_write(tmp_path):
    """`expected_before_hash` is what makes "the corpus moved under this proposal" a fact rather
    than a guess; `planned_after` is code's half — the frontmatter scrubbed, the body VERBATIM,
    because the body is the writer's and this is the page it is handed."""
    root = str(tmp_path)
    _write(root, "wiki/notes/Doomed.md", _page("Doomed"))
    before = _page("Cites It", related=["[[Doomed]]"], body="# Cites It\n\nSee [[Doomed]].\n")
    _write(root, "wiki/notes/Cites It.md", before)

    ops = deletion.plan(root, ["wiki/notes/Doomed.md"])

    op = _scrub_op(ops, "wiki/notes/Cites It.md")
    assert op["expected_before_hash"] == _sha(before)
    assert op["planned_after"] != before
    assert "[[Doomed]]" not in op["planned_after"].split("---", 2)[1], "the frontmatter is scrubbed"
    assert "See [[Doomed]]." in op["planned_after"], "the body is the writer's, handed verbatim"


# ── the frontmatter half: `related:`, `sources:`, and the supersession pointers ────────────────
def test_a_related_entry_naming_a_deleted_page_goes_and_every_other_entry_stays(tmp_path):
    root = str(tmp_path)
    _write(root, "wiki/notes/Doomed.md", _page("Doomed"))
    _write(root, "wiki/notes/Keeper.md", _page("Keeper"))
    _write(root, "wiki/notes/Cites It.md",
           _page("Cites It", related=["[[Doomed]]", "[[Keeper]]"]))

    ops = deletion.plan(root, ["wiki/notes/Doomed.md"])

    assert 'related: ["[[Keeper]]"]' in _after(ops, "wiki/notes/Cites It.md")


def test_a_sources_entry_naming_a_deleted_page_goes_the_same_way(tmp_path):
    """`sources:` is the same shape of declaration as `related:` and the linter reads a wikilink in
    either — so the sweep needs both, not the one the additive vocabulary happens to write."""
    root = str(tmp_path)
    _write(root, "sources/Transcript.md",
           "---\ntype: source\ntitle: \"Transcript\"\ntags: [source]\n"
           'content_hash: "sha256:abc"\ntier: 1\nsource_kind: upload\n---\n\n# Transcript\n')
    _write(root, "wiki/notes/Cites It.md", _page("Cites It", sources=["[[Transcript]]"]))

    ops = deletion.plan(root, ["sources/Transcript.md"])

    after = _after(ops, "wiki/notes/Cites It.md")
    assert "[[Transcript]]" not in after
    assert "sources:" not in after, "an emptied list field's line goes rather than staying empty"


def test_an_emptied_list_field_keeps_the_other_field_intact(tmp_path):
    """The benign half of the line above: emptying `sources:` must not disturb `related:`."""
    root = str(tmp_path)
    _write(root, "wiki/notes/Doomed.md", _page("Doomed"))
    _write(root, "wiki/notes/Keeper.md", _page("Keeper"))
    _write(root, "wiki/notes/Cites It.md",
           _page("Cites It", related=["[[Keeper]]"], sources=["[[Doomed]]"]))

    after = _after(deletion.plan(root, ["wiki/notes/Doomed.md"]), "wiki/notes/Cites It.md")

    assert 'related: ["[[Keeper]]"]' in after
    assert "sources:" not in after


def test_a_block_style_list_loses_only_the_item_that_names_the_deleted_page(tmp_path):
    """A hand-written page spells its lists over several lines. Dropping the ITEM LINE keeps the
    page a human diffs, where re-emitting the whole list as a flow sequence would rewrite lines
    nobody asked about."""
    root = str(tmp_path)
    _write(root, "wiki/notes/Doomed.md", _page("Doomed"))
    _write(root, "wiki/notes/Keeper.md", _page("Keeper"))
    _write(root, "wiki/notes/Cites It.md",
           "---\ntype: note\ntitle: \"Cites It\"\nstatus: developing\ncreated: 2026-01-01\n"
           "updated: 2026-01-01\ntags: [note]\nrelated:\n  - \"[[Keeper]]\"\n  - \"[[Doomed]]\"\n"
           "sources: []\n---\n\n# Cites It\n")

    after = _after(deletion.plan(root, ["wiki/notes/Doomed.md"]), "wiki/notes/Cites It.md")

    assert '  - "[[Keeper]]"' in after
    assert "Doomed" not in after


def test_a_supersession_pointer_at_a_deleted_page_goes(tmp_path):
    """A pointer to a page that no longer exists is dead whether or not it is spelled as a
    wikilink: `supersedes:` names a page, and the page is gone."""
    root = str(tmp_path)
    _write(root, "wiki/decisions/Old.md", _page("Old", page_type="decision"))
    _write(root, "wiki/decisions/New.md",
           _page("New", page_type="decision", extra=['supersedes: "Old"']))
    _write(root, "wiki/decisions/Newer.md",
           _page("Newer", page_type="decision", extra=['superseded_by: "[[Old]]"']))

    ops = deletion.plan(root, ["wiki/decisions/Old.md"])

    assert "supersedes:" not in _after(ops, "wiki/decisions/New.md")
    assert "superseded_by:" not in _after(ops, "wiki/decisions/Newer.md")


def test_a_supersession_pointer_at_a_surviving_page_is_left_alone(tmp_path):
    """The benign twin: the sweep reads the pointer's VALUE, it does not drop the field on sight."""
    root = str(tmp_path)
    _write(root, "wiki/decisions/Old.md", _page("Old", page_type="decision"))
    _write(root, "wiki/decisions/Other.md", _page("Other", page_type="decision"))
    _write(root, "wiki/decisions/New.md",
           _page("New", page_type="decision", related=["[[Old]]"],
                 extra=['supersedes: "Other"']))

    after = _after(deletion.plan(root, ["wiki/decisions/Old.md"]), "wiki/decisions/New.md")

    assert 'supersedes: "Other"' in after


# ── the body half is the writer's: code counts a reference and rewrites nothing ──────────────
def test_a_body_that_refers_to_the_deleted_page_puts_the_page_in_the_plan_verbatim(tmp_path):
    """Before ADR 043 this is where `[[Doomed]]` became `Doomed`. Code no longer touches a body: it
    notices the reference, hands the page to the writer with its body exactly as it was, and holds
    the writer's answer to `validate`'s bounds (`test_sweep.py`)."""
    root = str(tmp_path)
    _write(root, "wiki/notes/Doomed.md", _page("Doomed"))
    body = ("# Cites It\n\nWe agreed with [[Doomed]] and with [[Doomed|the broker]], "
            "and embedded ![[Doomed]] too.\n")
    _write(root, "wiki/notes/Cites It.md", _page("Cites It", body=body))

    ops = deletion.plan(root, ["wiki/notes/Doomed.md"])

    assert deletion.scrubbed_paths(ops) == ["wiki/notes/Cites It.md"]
    assert _after(ops, "wiki/notes/Cites It.md").endswith(body)


def test_a_wikilink_inside_code_is_not_a_link(tmp_path):
    """The frozen linter blanks fenced blocks and inline code before it looks for links, so a
    `[[Doomed]]` in a code sample is not a reference. Matching the linter here is not politeness —
    a scanner that sees MORE links than the linter hands the writer pages for no reason, and one
    that sees FEWER leaves a veto at apply time."""
    root = str(tmp_path)
    _write(root, "wiki/notes/Doomed.md", _page("Doomed"))
    body = ("# Cites It\n\nThe syntax is `[[Doomed]]`, as in:\n\n"
            "```\nrelated: [[Doomed]]\n```\n\nand really [[Doomed]].\n")
    _write(root, "wiki/notes/Cites It.md", _page("Cites It", body=body))

    assert deletion.references(body, {"Doomed"})
    assert not deletion.references(body.replace("and really [[Doomed]].", ""), {"Doomed"})


def test_a_page_whose_only_mention_is_inside_code_is_not_scrubbed_at_all(tmp_path):
    """The benign twin for the rule above, at the level of the PLAN: a page the linter would never
    call dead-linked must not appear in the blast radius, or every approval is wider than the
    change it authorizes."""
    root = str(tmp_path)
    _write(root, "wiki/notes/Doomed.md", _page("Doomed"))
    _write(root, "wiki/notes/Only Code.md",
           _page("Only Code", body="# Only Code\n\nWrite `[[Doomed]]` to link it.\n"))

    ops = deletion.plan(root, ["wiki/notes/Doomed.md"])

    assert deletion.scrubbed_paths(ops) == []


# ── the stem rule, taken from the linter and not invented ─────────────────────────────────────
@pytest.mark.parametrize("spelling", ["[[Doomed]]", "[[Doomed.md]]", "[[wiki/notes/Doomed.md]]",
                                      "[[Doomed#What it says]]", "[[ Doomed ]]"])
def test_every_spelling_the_linter_resolves_to_the_deleted_page_is_scrubbed(tmp_path, spelling):
    """`stigmergy_lint.link_targets` splits the alias and the anchor off, strips, and takes
    `Path(target).stem` — so all of these resolve to the same page and all of them die with it."""
    root = str(tmp_path)
    _write(root, "wiki/notes/Doomed.md", _page("Doomed"))
    _write(root, "wiki/notes/Cites It.md",
           _page("Cites It", body=f"# Cites It\n\nSee {spelling} for the rest.\n"))

    ops = deletion.plan(root, ["wiki/notes/Doomed.md"])

    assert deletion.scrubbed_paths(ops) == ["wiki/notes/Cites It.md"]
    assert deletion.references(_after(ops, "wiki/notes/Cites It.md"), {"Doomed"})


def test_a_link_to_a_different_page_with_a_similar_name_is_left_alone(tmp_path):
    """Specificity. `Doomed Too` is not `Doomed`, and a substring rule would unlink half a corpus."""
    root = str(tmp_path)
    _write(root, "wiki/notes/Doomed.md", _page("Doomed"))
    _write(root, "wiki/notes/Doomed Too.md", _page("Doomed Too"))
    _write(root, "wiki/notes/Cites It.md",
           _page("Cites It", related=["[[Doomed Too]]"],
                 body="# Cites It\n\nSee [[Doomed Too]].\n"))

    assert deletion.plan(root, ["wiki/notes/Doomed.md"])[1:] == []


# ── more than one page in one sweep ───────────────────────────────────────────────────────────
def test_a_page_naming_two_deleted_pages_is_scrubbed_once_for_both(tmp_path):
    """One page, one planned set of bytes. Two scrub ops for the same path would be two writes of
    the same file, and the second would only see the first one's result."""
    root = str(tmp_path)
    _write(root, "wiki/notes/One.md", _page("One"))
    _write(root, "wiki/notes/Two.md", _page("Two"))
    _write(root, "wiki/notes/Cites Both.md",
           _page("Cites Both", related=["[[One]]", "[[Two]]"],
                 body="# Cites Both\n\nSee [[One]] and [[Two]].\n"))

    ops = deletion.plan(root, ["wiki/notes/One.md", "wiki/notes/Two.md"])

    assert deletion.deleted_paths(ops) == ["wiki/notes/One.md", "wiki/notes/Two.md"]
    assert deletion.scrubbed_paths(ops) == ["wiki/notes/Cites Both.md"]
    after = _after(ops, "wiki/notes/Cites Both.md")
    assert "See [[One]] and [[Two]]." in after, "the body is the writer's, handed verbatim"
    assert "related:" not in after


def test_a_deleted_page_naming_another_deleted_page_is_never_scrubbed(tmp_path):
    """A page that is going does not get rewritten on its way out — the plan would then carry
    planned bytes for a file that must not exist afterwards."""
    root = str(tmp_path)
    _write(root, "wiki/notes/One.md", _page("One", related=["[[Two]]"]))
    _write(root, "wiki/notes/Two.md", _page("Two", related=["[[One]]"]))

    ops = deletion.plan(root, ["wiki/notes/One.md", "wiki/notes/Two.md"])

    assert deletion.scrubbed_paths(ops) == []
    assert len(ops) == 2


def test_the_plan_is_ordered_and_reproducible(tmp_path):
    """The stored plan is what a steward's attention and the content key are measured on, so the
    order is part of the contract: two runs over the same bytes produce the same list, not the
    same set."""
    root = str(tmp_path)
    _write(root, "wiki/notes/One.md", _page("One"))
    _write(root, "wiki/notes/Two.md", _page("Two"))
    for name in ("Zulu", "Alpha", "Mike"):
        _write(root, f"wiki/notes/{name}.md", _page(name, related=["[[One]]", "[[Two]]"]))

    first = deletion.plan(root, ["wiki/notes/Two.md", "wiki/notes/One.md"])

    assert first == deletion.plan(root, ["wiki/notes/One.md", "wiki/notes/Two.md"])
    assert [o["path"] for o in first] == [
        "wiki/notes/One.md", "wiki/notes/Two.md",
        "wiki/notes/Alpha.md", "wiki/notes/Mike.md", "wiki/notes/Zulu.md"]


# ── what may never be deleted ─────────────────────────────────────────────────────────────────
def test_an_entity_page_is_refused_by_name(tmp_path):
    """An identity is retired through governance, not deletion (ADR 016). Structural, not a
    convention: the entity zone is not in the deletable set at all."""
    root = str(tmp_path)
    _write(root, "wiki/entities/Acme Corp.md", _page("Acme Corp", page_type="entity"))

    with pytest.raises(RepairError) as caught:
        deletion.plan(root, ["wiki/entities/Acme Corp.md"])

    assert "identity" in str(caught.value)


@pytest.mark.parametrize("path", ["ops/entity-registry.json", ".claude/skills/librarian/SKILL.md",
                                  "wiki/notes/.hidden.md", "README.md"])
def test_nothing_outside_the_corpus_zones_can_be_deleted(tmp_path, path):
    """The blast radius of this kind is the corpus and nothing else. A whitelist, so a zone added
    tomorrow is undeletable by default."""
    root = str(tmp_path)
    _write(root, path, "whatever\n")
    _write(root, "wiki/notes/Anchor.md", _page("Anchor"))

    with pytest.raises(RepairError):
        deletion.plan(root, [path])


def test_a_target_that_does_not_exist_is_refused_before_anything_is_computed(tmp_path):
    root = str(tmp_path)
    _write(root, "wiki/notes/Anchor.md", _page("Anchor"))

    with pytest.raises(RepairError, match="does not exist"):
        deletion.plan(root, ["wiki/notes/Gone Already.md"])


def test_a_symlinked_target_is_refused(tmp_path):
    """`os.remove` on a symlink removes the LINK, so the page it points at survives and the repo
    loses a pointer instead of a page — a deletion that did not delete what it named."""
    root = str(tmp_path)
    _write(root, "wiki/notes/Real.md", _page("Real"))
    os.symlink(os.path.join(root, "wiki/notes/Real.md"), os.path.join(root, "wiki/notes/Link.md"))

    with pytest.raises(RepairError, match="symlink"):
        deletion.plan(root, ["wiki/notes/Link.md"])


def test_a_reference_in_a_frontmatter_field_this_kind_does_not_rewrite_refuses_the_plan(tmp_path):
    """The self-check that keeps this kind honest: frontmatter is code's half and code knows four
    fields; the writer never sees it. A wikilink in any OTHER field would survive the sweep as a
    dead link, and the contract linter would veto the apply — so the plan refuses to exist rather
    than becoming a question whose answer cannot be carried out. (A reference in a BODY is never
    unremovable any more: a writer reconciles anything.)"""
    root = str(tmp_path)
    _write(root, "wiki/notes/Doomed.md", _page("Doomed"))
    _write(root, "wiki/notes/Odd.md", _page("Odd", extra=['aliases: ["[[Doomed]]"]']))

    with pytest.raises(RepairError) as caught:
        deletion.plan(root, ["wiki/notes/Doomed.md"])

    assert "wiki/notes/Odd.md" in str(caught.value)


# ── the readers every other module goes through ───────────────────────────────────────────────
def test_the_touched_set_is_the_deleted_pages_and_the_scrubbed_ones(tmp_path):
    """`target_paths` is what the steward guard authorizes against, so it has to be the FULL blast
    radius — a steward of the deleted page is not automatically a steward of every page that
    mentioned it."""
    root = str(tmp_path)
    _write(root, "wiki/notes/Doomed.md", _page("Doomed"))
    _write(root, "wiki/decisions/Cites It.md",
           _page("Cites It", page_type="decision", related=["[[Doomed]]"]))

    ops = deletion.plan(root, ["wiki/notes/Doomed.md"])

    from stigmergy.repair import schema
    assert schema.target_paths(ops) == ["wiki/decisions/Cites It.md", "wiki/notes/Doomed.md"]
    assert deletion.expected_bytes(ops) == {
        "wiki/decisions/Cites It.md": _after(ops, "wiki/decisions/Cites It.md")}


def test_a_plan_bigger_than_its_ceiling_is_named_rather_than_stored(tmp_path):
    """One approval is one page's worth of a steward's attention. The stored plan carries every
    scrubbed page's full planned bytes, so an unbounded sweep is an unbounded row, an unreadable
    console panel and an approval nobody can actually have read."""
    root = str(tmp_path)
    _write(root, "wiki/notes/Doomed.md", _page("Doomed"))
    _write(root, "wiki/notes/Cites It.md", _page("Cites It", related=["[[Doomed]]"]))
    ops = deletion.plan(root, ["wiki/notes/Doomed.md"])

    assert deletion.oversize_reason(ops, 1_000_000) == ""
    assert "ceiling" in deletion.oversize_reason(ops, 10)


# ── the validator both ends run ───────────────────────────────────────────────────────────────
def _corpus(tmp_path):
    root = str(tmp_path)
    _write(root, "wiki/notes/Doomed.md", _page("Doomed"))
    _write(root, "wiki/notes/Cites It.md", _page("Cites It", related=["[[Doomed]]"]))
    _write(root, "ops/entity-registry.json", "{}\n")
    return root, deletion.plan(root, ["wiki/notes/Doomed.md"])


def test_a_well_formed_plan_validates(tmp_path):
    """The benign twin every refusal below needs: a validator that refused everything would pass
    each of them and be worthless."""
    root, ops = _corpus(tmp_path)
    assert deletion.validate(root, ops) == []


def test_a_scrub_op_naming_something_outside_the_corpus_is_refused(tmp_path):
    """A scrub's lane is WIDER than a deletion's — an entity page may perfectly well cite a note
    that is going — so the shape that has to be impossible is a plan rewriting `ops/`. The gates
    would refuse it too, at the cost of a clone and a steward's approval; this refuses it before a
    row exists."""
    root, ops = _corpus(tmp_path)
    tampered = [*ops, {"op": deletion.OP_SCRUB, "path": "ops/entity-registry.json",
                       "expected_before_hash": "x", "planned_after": "{}\n"}]

    findings = deletion.validate(root, tampered)

    assert [f.code for f in findings] == ["outside-corpus"]


def test_an_op_name_this_kind_does_not_perform_is_refused_by_name(tmp_path):
    root, ops = _corpus(tmp_path)
    tampered = [*ops, {"op": "rewrite-page", "path": "wiki/notes/Cites It.md"}]

    assert [f.code for f in deletion.validate(root, tampered)] == ["unknown-kind"]


def test_a_plan_that_removes_nothing_is_refused(tmp_path):
    """A proposal whose ops are all scrubs is a rewrite of other people's pages wearing a
    deletion's name, and the approval it would ask for says "remove"."""
    root, ops = _corpus(tmp_path)
    scrubs = [o for o in ops if o["op"] == deletion.OP_SCRUB]

    assert [f.code for f in deletion.validate(root, scrubs)] == ["no-deletion"]


def test_the_same_page_twice_in_one_plan_is_refused(tmp_path):
    """A page is deleted once and rewritten once; a second op for the same path would only ever
    see the first one's result."""
    root, ops = _corpus(tmp_path)

    findings = deletion.validate(root, [*ops, ops[0]])

    assert [f.code for f in findings] == ["duplicate-path"]


def test_a_planned_page_whose_frontmatter_is_not_codes_own_scrub_is_refused(tmp_path):
    """ADR 043 D1's second bound: the writer owns the body and nothing else. A stored plan whose
    frontmatter differs from code's scrub of the page as it stands — a line added, an entry kept —
    is refused by name at both ends."""
    root, ops = _corpus(tmp_path)
    op = _scrub_op(ops, "wiki/notes/Cites It.md")
    op["planned_after"] = op["planned_after"].replace("status: developing", "status: canonical")

    assert [f.code for f in deletion.validate(root, ops)] == [deletion.FRONTMATTER_REWRITTEN_CODE]


def test_a_planned_page_that_still_refers_to_the_deleted_page_is_refused(tmp_path):
    """The third bound, and the one the writer's retry is about: a body handed back still naming a
    going page is the dead link this kind exists to prevent, whichever shape it takes."""
    root, ops = _corpus(tmp_path)
    op = _scrub_op(ops, "wiki/notes/Cites It.md")
    op["planned_after"] += "\nRead [the memo](wiki/notes/Doomed.md).\n"

    assert [f.code for f in deletion.validate(root, ops)] == [deletion.REFERENCE_SURVIVES_CODE]


def test_a_scrub_carrying_no_planned_bytes_is_refused(tmp_path):
    """`planned_after` is the whole of what the apply byte-compares its recomputation against, so
    an op without it is an approval nothing could prove."""
    root, ops = _corpus(tmp_path)
    stripped = [{**o, "planned_after": ""} if o["op"] == deletion.OP_SCRUB else o for o in ops]

    assert [f.code for f in deletion.validate(root, stripped)] == ["no-planned-bytes"]


def test_the_apply_refuses_a_plan_the_corpus_has_moved_under(tmp_path):
    """The latecomer: the stored plan is still WELL-FORMED — every path exists, every op is known —
    and a page the plan never rewrote now refers to the going page. ADR 039 B4 caught this by
    recomputing the whole plan; a written plan cannot be recomputed, so the apply walks the corpus
    for exactly this (ADR 043 D3)."""
    root, ops = _corpus(tmp_path)
    _write(root, "wiki/notes/A Latecomer.md", _page("A Latecomer", related=["[[Doomed]]"]))

    touched, findings = deletion.apply_declared(root, ops)

    assert touched == []
    assert [f.code for f in findings] == [deletion.PLAN_DRIFT_CODE]
    assert "A Latecomer" in findings[0].message
    assert os.path.exists(os.path.join(root, "wiki/notes/Doomed.md")), "nothing was performed"


def test_the_apply_refuses_a_plan_whose_page_changed_since_it_was_written(tmp_path):
    """The other half of B4's question, answered by the base hash every scrub op carries: the
    page the plan would rewrite is not the page the writer read, so the bytes it would land are a
    rewrite of a page nobody read."""
    root, ops = _corpus(tmp_path)
    with open(os.path.join(root, "wiki/notes/Cites It.md"), "a", encoding="utf-8") as f:
        f.write("\nA sentence added after the plan was made.\n")

    touched, findings = deletion.apply_declared(root, ops)

    assert touched == []
    assert [f.code for f in findings] == [deletion.PLAN_DRIFT_CODE]
    assert "wiki/notes/Cites It.md" in findings[0].message


def test_the_apply_performs_the_plan_when_the_corpus_has_not_moved(tmp_path):
    """The benign twin, and the half a broken recomputation would silence."""
    root, ops = _corpus(tmp_path)

    touched, findings = deletion.apply_declared(root, ops)

    assert findings == []
    assert touched == ["wiki/notes/Cites It.md", "wiki/notes/Doomed.md"]
    assert not os.path.exists(os.path.join(root, "wiki/notes/Doomed.md"))
    with open(os.path.join(root, "wiki/notes/Cites It.md"), encoding="utf-8") as f:
        assert "Doomed" not in f.read()


# ── the deterministic duplicate road, as pure functions ───────────────────────────────────────
def _source(title: str, *, digest: str, extracted_at: str = "2026-01-01T00:00:00Z") -> str:
    return ("---\ntype: source\n" + f'title: "{title}"\n' + "tags: [source]\n"
            + f'content_hash: "sha256:{digest}"\n' + f'extracted_at: "{extracted_at}"\n'
            + "tier: 1\n---\n\n" + f"# {title}\n\nThe document.\n")


def test_pages_with_different_content_hashes_are_never_a_duplicate_group(tmp_path):
    root = str(tmp_path)
    _write(root, "sources/One.md", _source("One", digest="aaa"))
    _write(root, "sources/Two.md", _source("Two", digest="bbb"))
    _write(root, "wiki/notes/Anchor.md", _page("Anchor"))

    assert deletion.duplicate_source_groups(root) == []


def test_a_wiki_page_is_never_a_duplicate_however_its_hashes_read(tmp_path):
    """The road is `sources/` only. A `wiki/` page is somebody's writing, and two of them saying
    the same thing is an editorial question rather than a filing accident."""
    root = str(tmp_path)
    _write(root, "wiki/notes/One.md", _page("One", extra=['content_hash: "sha256:aaa"']))
    _write(root, "wiki/notes/Two.md", _page("Two", extra=['content_hash: "sha256:aaa"']))

    assert deletion.duplicate_source_groups(root) == []


def test_three_filings_of_one_document_keep_exactly_one(tmp_path):
    """A group is one question, whatever its size: the corpus keeps the copy it cites and every
    other filing goes in the same proposal."""
    root = str(tmp_path)
    for name in ("First", "Second", "Third"):
        _write(root, f"sources/{name}.md", _source(name, digest="aaa"))
    _write(root, "wiki/notes/Reader.md", _page("Reader", related=["[[Second]]"]))

    assert deletion.duplicate_source_groups(root) == [
        ("sources/Second.md", ["sources/First.md", "sources/Third.md"])]


def test_a_page_that_mentions_nothing_going_is_left_out_of_the_plan_byte_for_byte(tmp_path):
    """The blast radius is the whole argument for this kind, so a page the sweep touches for a
    reason nobody can name is a defect even when the reason is one byte. Three shapes that a
    careless reassembly would each have rewritten: no trailing newline after the closing fence, a
    block-style list, and a page with no frontmatter at all."""
    root = str(tmp_path)
    _write(root, "wiki/notes/Doomed.md", _page("Doomed"))
    _write(root, "wiki/notes/No Trailing Newline.md",
           '---\ntype: note\ntitle: "No Trailing Newline"\ntags: [note]\n---')
    _write(root, "wiki/notes/Block List.md",
           '---\ntype: note\ntitle: "Block List"\ntags: [note]\nrelated:\n  - "[[Keeper]]"\n---\n\n'
           "# Block List\n")
    _write(root, "wiki/notes/Keeper.md", _page("Keeper"))
    _write(root, "wiki/notes/No Frontmatter.md", "# No Frontmatter\n\nJust prose.\n")

    ops = deletion.plan(root, ["wiki/notes/Doomed.md"])

    assert deletion.scrubbed_paths(ops) == []


@pytest.mark.parametrize("spelling, stem", [
    ("[the memo](wiki/notes/Doomed.md)", "Doomed"),
    ("[the memo](../notes/Doomed.md)", "Doomed"),
    ("[the memo](/wiki/notes/Doomed.md#what-it-says)", "Doomed"),
    # The three shapes a name with a SPACE arrives in. The bare-space one is not well-formed
    # markdown and a reader sees a reference anyway — red before the scanner stopped stopping at
    # whitespace, and invisible downstream too, since the contract linter counts no markdown link
    # at all.
    ("[the memo](wiki/notes/Doomed Memo.md)", "Doomed Memo"),
    ("[the memo](wiki/notes/Doomed%20Memo.md)", "Doomed Memo"),
    ("[the memo](<wiki/notes/Doomed Memo.md>)", "Doomed Memo"),
    ('[the memo](wiki/notes/Doomed Memo.md "the memo")', "Doomed Memo"),
])
def test_every_markdown_link_shape_at_a_going_page_counts_as_a_reference(spelling, stem):
    """The one shape this scanner sees that the frozen linter does not, and the reason it does:
    the view that named the removed SpaceX note listed it three times, once as a markdown link, and
    nothing anywhere would have caught it. Matching a little MORE than markdown does is the safe
    direction — a false positive only hands a page to the writer, which reconciles what it finds."""
    assert deletion.references(f"# X\n\nRead {spelling} first.\n", {stem})


@pytest.mark.parametrize("spelling", [
    "[another](wiki/notes/Doomed Too.md)", "[another](wiki/notes/Other.md)", "(Doomed)",
    "the Doomed memo", "`[the memo](wiki/notes/Doomed.md)`",
])
def test_a_markdown_link_at_something_else_is_not_a_reference(spelling):
    """The benign twin: a scanner that answered True for everything would pass the rows above and
    hand the writer the whole corpus. A bare NAME is not a reference either — this kind reconciles
    links, and rewriting every sentence that happens to say a page's name is a different change."""
    assert not deletion.references(f"# X\n\nRead {spelling} first.\n", {"Doomed"})


def test_link_stem_keeps_a_dotted_title_whole_and_strips_only_a_path_and_an_md():
    """The link question, asked the linter's way — and the linter changed its answer: it used
    `Path(target).stem`, so `[[Booking.com]]` resolved to `Booking` and `[[Acme Inc. Invoices]]` to
    `Acme Inc`, and this mirror amputated with it on purpose. Now both keep the dots and take only
    the last path segment minus a trailing `.md`; a mirror that still amputated would plan a scrub
    of links the gate no longer sees as naming the deleted page."""
    assert deletion.link_stem("Booking.com") == "Booking.com"
    assert deletion.link_stem("Acme Inc. Invoice Management With Hermes") == "Acme Inc. Invoice Management With Hermes"
    assert deletion.link_stem("wiki/entities/Acme Inc..md") == "Acme Inc."
    assert deletion.link_stem("Another Page.md|shown as|x") == "Another Page"
    assert deletion.link_stem("Another Page#Section") == "Another Page"
    assert deletion.link_stem("") == ""
