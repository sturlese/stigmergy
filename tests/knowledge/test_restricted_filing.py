"""A restricted capture that mentions broader pages files its own knowledge and leaves every broader
page byte-identical: an update, delete, or contradiction that would move restricted material into a
broader page is dropped and recorded. Resolutions and recompiles stay strict and reject instead."""
from pathlib import Path

import pytest

from stigmergy.capture import schema
from stigmergy.knowledge import writer
from stigmergy.knowledge.pages import page_path, parse_page, render_page
from stigmergy.knowledge.plan import (
    ContradictionClaim,
    ContradictionProposal,
    FilingPlan,
    PageMutation,
)
from stigmergy.knowledge.write_guard import WriteContext

SOURCE = "sources/2026/05/00000000-0000-4000-8000-000000000017.md"
FINANCE = ("finance",)
OUTSIDE = "the page has readers outside the capture's audience"


def _write(root: Path, role: str, title: str, body: str, acl, **extra) -> str:
    rel = page_path(role, title)
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_page(path=rel, role=role, title=title, body=body, acl=acl, **extra),
                      encoding="utf-8")
    return rel


def _envelope(audience) -> schema.CaptureEnvelope:
    import datetime as dt
    import hashlib

    digest = hashlib.sha256(b"x").hexdigest()
    return schema.CaptureEnvelope(
        capture_id="00000000-0000-4000-8000-000000000017",
        idempotency_key="restricted-filing",
        actor=schema.Actor(subject="hannah", display_name="Hannah"),
        audience=audience,
        origin=schema.Origin(adapter="mcp", captured_at=dt.datetime(2026, 5, 6, tzinfo=dt.UTC),
                             title="Board pack"),
        artifacts=(schema.ArtifactRef(blob_ref=schema.content_ref(digest), sha256=digest, bytes=1,
                                      media_type=schema.MEDIA_MARKDOWN),),
    )


@pytest.fixture
def repo(tmp_path):
    public = _write(tmp_path, "entity", "Harbor Fund", "# Harbor Fund\n\n## What / Who\nA seed investor.",
                    None, entity_type="organization")
    team = _write(tmp_path, "note", "Finance plan", "# Finance plan\n\nThe finance plan.", FINANCE)
    return tmp_path, public, team


def _plan(public: str, *, extra=()) -> FilingPlan:
    return FilingPlan(
        summary="Filed the board pack",
        mutations=(
            PageMutation(action="create", role="note", title="Series A plan",
                         body=f"# Series A plan\n\n[[Harbor Fund]] will invest its pro-rata share. "
                              f"(Source: `{SOURCE}`)\n\n## Entities\n- [[Harbor Fund]] - investor",
                         reason="Restricted board decision"),
            PageMutation(action="update", path=public,
                         body="# Harbor Fund\n\n## What / Who\nA seed investor.\n\n## Facts\n"
                              f"- 2026-05-06: will invest 2 million euros. (Source: `{SOURCE}`)",
                         reason="Record the pro-rata commitment"),
            *extra,
        ),
    )


def _apply(root: Path, plan: FilingPlan, *, audience=FINANCE, groups=frozenset({"finance"}),
           drop_broader=True):
    skipped: list[str] = []
    reasons: dict[str, str] = {}
    writer._apply_filing_plan(
        str(root), plan, context=WriteContext(groups, audience), envelope=_envelope(audience),
        relative_source=SOURCE, reasons=reasons, allowed_contradiction_sources=frozenset({SOURCE}),
        skipped=skipped, drop_broader=drop_broader)
    return reasons, skipped


def test_a_restricted_capture_files_its_note_and_leaves_the_broader_page_untouched(repo):
    root, public, _team = repo
    before = (root / public).read_bytes()

    reasons, skipped = _apply(root, _plan(public))

    note = parse_page("wiki/notes/Series A plan.md", (root / "wiki/notes/Series A plan.md").read_text())
    assert note.acl == FINANCE
    assert "[[Harbor Fund]]" in note.body
    assert (root / public).read_bytes() == before
    assert public not in reasons
    assert skipped == [f"update to {public} was not applied: {OUTSIDE}"]


def test_a_restricted_capture_still_updates_a_page_of_its_own_audience(repo):
    root, public, team = repo
    update = PageMutation(action="update", path=team,
                          body=f"# Finance plan\n\nThe finance plan.\n\nThe round is 12 million. "
                               f"(Source: `{SOURCE}`)", reason="Add the round size")

    reasons, _skipped = _apply(root, _plan(public, extra=(update,)))

    assert team in reasons
    assert "12 million" in (root / team).read_text()


def test_a_contradiction_on_a_broader_page_is_dropped_and_recorded(repo):
    root, public, _team = repo
    before = (root / public).read_bytes()
    claim = ContradictionClaim(text="The fund invests 2 million.", source=SOURCE, date="2026-05-06")
    plan = _plan(public).model_copy(update={"contradictions": (ContradictionProposal(
        page_path=public, explanation="Two amounts disagree.",
        claims=(claim, claim.model_copy(update={"text": "The fund invests 3 million."}))),)})

    _reasons, skipped = _apply(root, plan)

    assert (root / public).read_bytes() == before
    assert f"contradiction on {public} was not recorded: {OUTSIDE}" in skipped


def test_a_delete_of_a_broader_page_is_dropped_and_recorded(repo):
    root, public, _team = repo
    delete = PageMutation(action="delete", path=public, reason="Consolidate")

    _reasons, skipped = _apply(root, _plan(public, extra=(delete,)))

    assert (root / public).is_file()
    assert f"delete of {public} was not applied: {OUTSIDE}" in skipped


def test_a_page_the_actor_cannot_read_still_rejects_the_plan(repo, tmp_path):
    root, public, _team = repo
    hidden = _write(root, "note", "Leadership plan", "# Leadership plan\n\nHidden.", ("leadership",))
    update = PageMutation(action="update", path=hidden, body="# Leadership plan\n\nChanged.",
                          reason="Probe")

    with pytest.raises(writer.KnowledgeWriteError):
        _apply(root, _plan(public, extra=(update,)))


def test_an_organization_wide_capture_still_updates_the_broad_page(repo):
    root, public, _team = repo
    plan = FilingPlan(summary="Open update", mutations=(
        PageMutation(action="update", path=public,
                     body=f"# Harbor Fund\n\n## What / Who\nA seed investor in Lisbon. (Source: `{SOURCE}`)",
                     reason="Open fact"),))

    reasons, skipped = _apply(root, plan, audience=None, groups=frozenset({"finance"}))

    assert public in reasons and skipped == []


def test_without_the_opt_in_an_update_to_a_broader_page_rejects_the_plan(repo):
    root, public, _team = repo
    before = (root / public).read_bytes()

    with pytest.raises(writer.KnowledgeWriteError):
        _apply(root, _plan(public), drop_broader=False)

    assert (root / public).read_bytes() == before


def test_a_hidden_and_a_missing_update_target_fail_identically(repo):
    root, public, _team = repo
    hidden = _write(root, "note", "Leadership plan", "# Leadership plan\n\nHidden.", ("leadership",))
    errors = []
    for path in (hidden, "wiki/notes/Never existed.md"):
        update = PageMutation(action="update", path=path, body="# X\n\nChanged.", reason="Probe")
        with pytest.raises(writer.KnowledgeWriteError) as error:
            _apply(root, FilingPlan(summary="Probe", mutations=(update,)))
        errors.append(str(error.value))
    assert errors[0] == errors[1]


def test_a_contradiction_on_a_hidden_page_rejects_the_plan_like_a_missing_one(repo):
    root, public, _team = repo
    hidden = _write(root, "note", "Leadership plan", "# Leadership plan\n\nHidden.", ("leadership",))
    claim = ContradictionClaim(text="A.", source=SOURCE, date="2026-05-06")
    errors = []
    for path in (hidden, "wiki/notes/Never existed.md"):
        plan = FilingPlan(summary="Probe", contradictions=(ContradictionProposal(
            page_path=path, explanation="Disagree.", claims=(claim, claim.model_copy(update={"text": "B."}))),))
        skipped: list[str] = []
        with pytest.raises(writer.KnowledgeWriteError) as error:
            writer._apply_filing_plan(
                str(root), plan, context=WriteContext(frozenset({"finance"}), FINANCE),
                envelope=_envelope(FINANCE), relative_source=SOURCE, reasons={},
                allowed_contradiction_sources=frozenset({SOURCE}), skipped=skipped, drop_broader=True)
        assert skipped == []
        errors.append(str(error.value))
    assert errors[0] == errors[1]


@pytest.mark.parametrize("variant", ["./{path}", "wiki//entities/Harbor Fund.md", "wiki/entities/harbor fund.md"])
def test_a_non_canonical_path_to_a_broader_page_rejects_the_plan_without_naming_it(repo, variant):
    root, public, _team = repo
    before = (root / public).read_bytes()
    update = PageMutation(action="update", path=variant.format(path=public),
                          body="# Harbor Fund\n\nChanged.", reason="Probe")
    skipped: list[str] = []
    with pytest.raises(writer.KnowledgeWriteError):
        writer._apply_filing_plan(
            str(root), FilingPlan(summary="Probe", mutations=(update,)),
            context=WriteContext(frozenset({"finance"}), FINANCE), envelope=_envelope(FINANCE),
            relative_source=SOURCE, reasons={}, allowed_contradiction_sources=frozenset({SOURCE}),
            skipped=skipped, drop_broader=True)
    assert (root / public).read_bytes() == before
    assert skipped == []
