"""`repair.entity_body` — the one op in this loop that REPLACES text, and the narrow shape that
makes it judgeable.

Real files in a real checkout, no doubles: every rule here is about bytes on disk (which line of
frontmatter survived, where the body was cut, what the page looked like before), and a stubbed
filesystem would prove none of them.

The property the whole module exists for: **everything above and including the page's own `# Title`
survives byte for byte, and the frontmatter changes on exactly two lines or not at all.** The gate
(`gates.gate_body_rewrite`, with this path in `body_rewrite_allowed`) proves that again against the
diff; this proves it about the writer, so a failure says which of the two is wrong.
"""
import os

import pytest

from stigmergy.librarian import page as page_policy
from stigmergy.repair import entity_body, schema
from stigmergy.repair.errors import RepairError
from tests.repair import support

DRAFT = ("## What / Who\n\nA freight broker the renewal pipeline runs through.\n\n"
         "## Facts\n\n- It renewed in Q3 — [[Existing Note]]\n")


def _op(path=support.ENTITY_PAGE, body=DRAFT, role=""):
    return {"op": schema.KIND_ENTITY_BODY, "path": path, "body_markdown": body, "role": role}


def _codes(findings):
    return sorted({f.code for f in findings})


# ── the writer: what survives, and what does not ──────────────────────────────────────────────
def test_the_rewrite_keeps_everything_down_to_the_h1_and_replaces_only_what_follows(repo_env):
    support.seed_entity(repo_env)
    before = support.page_text(repo_env.repo, support.ENTITY_PAGE)

    edited, findings = entity_body.apply_declared(repo_env.repo, [_op()], today="2026-08-17")

    assert (edited, findings) == ([support.ENTITY_PAGE], [])
    after = support.page_text(repo_env.repo, support.ENTITY_PAGE)
    head_before, head_after = before.split("\n# ", 1)[0], after.split("\n# ", 1)[0]
    assert head_after == head_before.replace("updated: 2026-01-01", "updated: 2026-08-17"), (
        "everything above the H1 is preserved byte for byte except the one `updated:` line")
    assert f"\n# {support.ENTITY_STEM}\n" in after, "the page's own H1 is the page's own"
    assert "A freight broker" in after
    assert "<One clear paragraph" not in after, "the placeholder body is what this replaces"


def test_the_frontmatter_differs_on_exactly_the_updated_line(repo_env):
    """Line-level, not substring-level: the whole safety argument of this kind is that a steward
    approving a body draft is not also approving a change to `entity:`, `acl:` or `status:`."""
    support.seed_entity(repo_env)
    before = page_policy.frontmatter_lines(support.page_text(repo_env.repo, support.ENTITY_PAGE))

    entity_body.apply_declared(repo_env.repo, [_op()], today="2026-08-17")

    after = page_policy.frontmatter_lines(support.page_text(repo_env.repo, support.ENTITY_PAGE))
    assert set(before) - set(after) == {"updated: 2026-01-01"}
    assert set(after) - set(before) == {"updated: 2026-08-17"}


def test_a_role_is_written_when_the_page_declares_an_empty_one(repo_env):
    support.seed_entity(repo_env)

    entity_body.apply_declared(repo_env.repo, [_op(role="A freight broker in the north-west.")],
                               today="2026-08-17")

    lines = page_policy.frontmatter_lines(support.page_text(repo_env.repo, support.ENTITY_PAGE))
    assert 'role: "A freight broker in the north-west."' in lines


def test_a_role_on_a_page_that_already_has_one_is_refused_and_nothing_is_written(repo_env):
    """The rule that keeps this kind from being a frontmatter editor: a role somebody wrote is a
    statement of identity, and replacing it is not a body draft. Refused at validate time, so the
    proposal is never stored and the page is never opened."""
    support.seed_entity(repo_env, role="The incumbent broker.")
    before = support.page_text(repo_env.repo, support.ENTITY_PAGE)

    edited, findings = entity_body.apply_declared(repo_env.repo, [_op(role="something else")],
                                                  today="2026-08-17")

    assert edited == []
    assert _codes(findings) == ["role-already-set"]
    assert support.page_text(repo_env.repo, support.ENTITY_PAGE) == before


def test_an_empty_role_leaves_the_role_line_exactly_as_it_was(repo_env):
    """The benign twin of the rule above: the ordinary proposal carries no role at all, and it
    must not rewrite the line to a different spelling of empty."""
    support.seed_entity(repo_env, role="The incumbent broker.")

    entity_body.apply_declared(repo_env.repo, [_op()], today="2026-08-17")

    lines = page_policy.frontmatter_lines(support.page_text(repo_env.repo, support.ENTITY_PAGE))
    assert 'role: "The incumbent broker."' in lines


# ── validation: what may never be stored, let alone applied ───────────────────────────────────
def test_a_well_formed_op_validates_clean(repo_env):
    """The benign twin for every refusal below. A validator that refused a legitimate draft would
    make the kind inert while looking exactly as healthy as one that works."""
    support.seed_entity(repo_env)
    assert entity_body.validate(repo_env.repo, [_op()]) == []


@pytest.mark.parametrize("op, code", [
    (_op(path=support.NOTE_A), "outside-lane"),
    (_op(path="wiki/entities/Nobody Minted This.md"), "missing-target"),
    (_op(path="wiki/entities/notes.txt"), "not-a-page"),
    (_op(body="   \n\n"), "empty-body"),
    (_op(body="## Facts\n\n---\n\ntype: note\n"), "body-frontmatter-fence"),
    (_op(body="# Meridian Partners\n\nthe page's own H1, drafted again\n"), "body-h1"),
    (_op(body="## Facts\n\n- see [[A Page That Does Not Exist]]\n"), "dead-link"),
    (_op(body="## Facts\n\n" + "- a line\n" * 200), "body-too-many-lines"),
    (_op(body="## Facts\n\n" + "x" * (entity_body.MAX_BODY_BYTES + 1) + "\n"), "body-too-long"),
    (_op(role="a role\nsubmitted_by: somebody@example.com"), "role-not-one-line"),
    (_op(role="r" * (entity_body.MAX_ROLE_CHARS + 1)), "role-too-long"),
])
def test_validate_refuses_by_name(repo_env, op, code):
    support.seed_entity(repo_env)
    assert code in _codes(entity_body.validate(repo_env.repo, [op]))


def test_a_proposal_of_this_kind_carries_exactly_one_op(repo_env):
    """ONE page, one draft, one approval. A steward approving a body draft is reading THAT page's
    prose; two drafts in one proposal is two judgments behind one button."""
    support.seed_entity(repo_env)
    assert "one-op" in _codes(entity_body.validate(repo_env.repo, [_op(), _op()]))
    assert "one-op" in _codes(entity_body.validate(repo_env.repo, []))


def test_a_page_whose_type_is_not_entity_is_refused_even_inside_the_entity_zone(repo_env):
    """The zone is a folder and the type is a declaration; this kind needs both. A page filed in
    the entity folder with another type is a contract-linter problem, not a body to draft."""
    support.seed_entity(repo_env)
    path = os.path.join(repo_env.repo, support.ENTITY_PAGE)
    with open(path, encoding="utf-8") as f:
        text = f.read()
    with open(path, "w", encoding="utf-8") as f:
        f.write(text.replace("type: entity", "type: note", 1))

    assert "not-an-entity-page" in _codes(entity_body.validate(repo_env.repo, [_op()]))


def test_a_page_with_no_h1_is_refused_rather_than_guessed_at(repo_env):
    """The writer cuts at the page's own H1. A page without one has no cut point, and inventing a
    title is exactly the identity decision entity birth reserves for a steward."""
    support.seed_entity(repo_env, body="Just prose, no heading at all.\n")
    assert "no-h1" in _codes(entity_body.validate(repo_env.repo, [_op()]))


def test_a_page_with_no_updated_line_is_refused_rather_than_gaining_one(repo_env):
    """`updated:` is rewritten IN PLACE. A page without the line would need one appended, which
    moves frontmatter the steward did not read — refused instead."""
    support.seed_entity(repo_env, drop_updated=True)
    assert "no-updated-line" in _codes(entity_body.validate(repo_env.repo, [_op()]))


def test_a_page_with_no_frontmatter_block_is_refused_rather_than_guessed_at(repo_env):
    """The writer rebuilds the file from its own line indices, so "where does the frontmatter end"
    has to be a fact, not a guess. A page that opens with no `---` has no boundary between what it
    declares and what it says, and a rewrite that guessed at one would land somewhere nobody
    predicted."""
    support.seed_entity(repo_env)
    path = os.path.join(repo_env.repo, support.ENTITY_PAGE)
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"# {support.ENTITY_STEM}\n\n<One clear paragraph.>\n")

    assert "no-frontmatter" in _codes(entity_body.validate(repo_env.repo, [_op()]))


def test_a_symlinked_entity_page_is_refused_before_it_is_opened(repo_env):
    """`edits.validate`'s own rule, for the same reason: `open(p, "w")` writes THROUGH a page
    swapped for a link, and the write must not depend on a gate running around it.

    The link points INSIDE the worktree deliberately — that is the case containment does not
    catch (`gather.confined_page`'s own reasoning: a link back inside is contained and still is
    not the bytes git tracks), and it is the one that would rewrite somebody else's page."""
    support.seed_entity(repo_env)
    full = os.path.join(repo_env.repo, support.ENTITY_PAGE)
    os.remove(full)
    os.symlink(os.path.join(repo_env.repo, support.NOTE_A), full)

    assert "symlinked-target" in _codes(entity_body.validate(repo_env.repo, [_op()]))


def test_a_malformed_apply_date_is_refused_rather_than_written_into_the_page(repo_env):
    """`today` becomes a frontmatter LINE. Anything but a plain date is a line-oriented write into
    somebody's page — `entities.birth._clean_today`'s reasoning, and the same guard."""
    support.seed_entity(repo_env)
    with pytest.raises(RepairError, match="date"):
        entity_body.apply_declared(repo_env.repo, [_op()],
                                   today="2026-08-17\nsubmitted_by: nobody@example.com")


def test_apply_declared_writes_nothing_when_validation_refuses(repo_env):
    """All-or-nothing, `edits.apply_declared`'s own posture: a half-applied rewrite is a worktree
    nobody can reason about, and here it would be a page with its body gone."""
    support.seed_entity(repo_env)
    before = support.page_text(repo_env.repo, support.ENTITY_PAGE)

    edited, findings = entity_body.apply_declared(repo_env.repo, [_op(body="")], today="2026-08-17")

    assert edited == [] and findings
    assert support.page_text(repo_env.repo, support.ENTITY_PAGE) == before
