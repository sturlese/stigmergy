from pathlib import Path

from stigmergy.knowledge import writer
from stigmergy.knowledge.pages import render_page
from stigmergy.knowledge.plan import FilingPlan, PageMutation
from stigmergy.knowledge.write_guard import WriteContext, may_link


def _page(root: Path, title: str, stem: str, acl: tuple[str, ...] | None) -> None:
    path = f"wiki/notes/{stem}.md"
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        render_page(path=path, role="note", title=title, body=f"# {title}\n\nBody.", acl=acl),
        encoding="utf-8",
    )


def _stems(root: Path, context: WriteContext, plan: FilingPlan | None = None) -> dict[str, str | None]:
    return writer._knowledge_link_stems(str(root), plan or FilingPlan(summary="Link check"), context)


def test_title_form_links_resolve_only_to_pages_the_write_may_link(tmp_path):
    _page(tmp_path, "Plan: Abierto", "Plan- Abierto", None)
    _page(tmp_path, "Plan: Finanzas", "Plan- Finanzas", ("finance",))
    engineering = WriteContext(actor_groups=frozenset({"engineering"}), content_acl=("engineering",))
    body = "See [[Plan: Abierto]] and [[Plan: Finanzas]]."

    canonical = writer._canonical_links(body, _stems(tmp_path, engineering))

    assert canonical == "See [[Plan- Abierto|Plan: Abierto]] and [[Plan: Finanzas]]."


def test_open_capture_cannot_link_to_restricted_knowledge_even_when_readable(tmp_path):
    _page(tmp_path, "Plan: Finanzas", "Plan- Finanzas", ("finance",))
    master = WriteContext(actor_groups=None, content_acl=None, unrestricted=True)

    assert may_link(master, ("finance",)) is False
    assert writer._canonical_links("[[Plan: Finanzas]]", _stems(tmp_path, master)) == "[[Plan: Finanzas]]"


def test_links_to_pages_created_in_the_same_plan_and_existing_stems_are_canonical(tmp_path):
    context = WriteContext(actor_groups=None, content_acl=None, unrestricted=True)
    plan = FilingPlan(
        summary="Two related notes",
        mutations=(
            PageMutation(
                action="create",
                role="note",
                title="Reunión: Pre Helmcode — 2026-09-23",
                body="# Reunión: Pre Helmcode — 2026-09-23\n\nBody.",
                entities=(),
                reason="Dated meeting",
            ),
        ),
    )
    stems = _stems(tmp_path, context, plan)

    assert writer._canonical_links("[[Reunión: Pre Helmcode — 2026-09-23#Next|the call]]", stems) == (
        "[[Reunión- Pre Helmcode — 2026-09-23#Next|the call]]"
    )
    assert writer._canonical_links("[[Reunión- Pre Helmcode — 2026-09-23]]", stems) == (
        "[[Reunión- Pre Helmcode — 2026-09-23]]"
    )
    assert writer._canonical_links("[[Marc Sturlese]]", stems) == "[[Marc Sturlese]]"
