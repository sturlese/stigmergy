import datetime as dt
from pathlib import Path

from stigmergy.knowledge import context
from stigmergy.knowledge.pages import render_page


def _page(root, title, acl, body):
    relative = f"wiki/notes/{title}.md"
    path = Path(root, relative)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        render_page(
            path=relative,
            role="note",
            title=title,
            body=f"# {title}\n\n{body}",
            acl=acl,
            created=dt.date(2026, 8, 24),
            updated=dt.date(2026, 8, 24),
        ),
        encoding="utf-8",
    )


def test_context_includes_visible_narrower_page_that_capture_may_update(tmp_path):
    _page(tmp_path, "Finance plan", ("finance",), "Launch alpha on Friday.")

    result = context.filing_context(
        str(tmp_path),
        source_text="Alpha launch moved to Monday.",
        capture_acl=("finance", "leadership"),
        actor_groups=frozenset({"finance"}),
    )

    assert result["candidates"][0]["path"] == "wiki/notes/Finance plan.md"
    assert result["candidates"][0]["context_may_flow_to_capture"] is False
    assert result["candidates"][0]["capture_may_update"] is True


def test_context_excludes_pages_the_actor_cannot_read(tmp_path):
    _page(tmp_path, "Finance plan", ("finance",), "Launch alpha on Friday.")

    result = context.filing_context(
        str(tmp_path),
        source_text="Alpha launch moved to Monday.",
        capture_acl=("engineering",),
        actor_groups=frozenset({"engineering"}),
    )

    assert result["candidates"] == []


def test_context_is_trimmed_before_rendering(tmp_path, monkeypatch):
    monkeypatch.setattr(context, "MAX_PLANNER_CONTEXT_BYTES", 500)
    _page(tmp_path, "Large plan", None, "alpha " * 300)

    result = context.filing_context(
        str(tmp_path),
        source_text="alpha",
        capture_acl=None,
        actor_groups=None,
    )

    assert result["truncated"] is True
    assert len(context.render_context(result).encode("utf-8")) <= 500
