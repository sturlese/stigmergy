"""`gardener.notice.scope_findings_to_channel` against real Postgres — the ACL check needs a real
`pages_index` row to resolve `_notice_page_paths` against, the same reason
`digest.sections._visible_pages` needs one.

As in `test_notice.py`, the SLA findings here are synthetic: no live check produces one.
"""
from stigmergy.gardener import notice, schema
from tests.gardener import support


def _finding(check="contradiction-sla-open", page_path="wiki/x.md", **extra):
    f = {"check": check, "severity": schema.SEVERITY_SLA, "source": schema.SOURCE_DETERMINISTIC,
        "subject": page_path, "detail": "real detail naming the page",
        "suggested_action": "real action naming the page",
        "_notice_detail": f"real notice wording naming {page_path}",
        "_notice_action": f"real notice action naming {page_path}",
        "_notice_page_paths": [page_path]}
    f.update(extra)
    return f


# ── the redaction itself, and the benign twin it requires ───────────────────────────────────────
def test_redacts_a_finding_whose_page_is_not_visible_at_the_given_audiences(conn, repo):
    path = support.write_labelled_page(repo, "leadership/beta-pilot-scope.md", title="Scope",
                                       acl=["leadership"])
    support.rebuild_index(conn, repo)
    finding = _finding(page_path=path)

    scoped = notice.scope_findings_to_channel(conn, [finding], audiences=set())

    assert len(scoped) == 1
    result = scoped[0]
    assert result["_notice_detail"] == (
        "redacted — the page this finding is about is not visible at this channel's scope")
    assert result["_notice_action"] == "details in `stigmergy-gardener`"
    assert path not in result["_notice_detail"]
    assert path not in result["_notice_action"]
    # the report-facing fields (never scoped — the terminal report has no caller identity to scope
    # to, `gardener/__init__.py`'s own module docstring) are untouched.
    assert result["detail"] == finding["detail"]
    assert result["suggested_action"] == finding["suggested_action"]
    assert result["subject"] == finding["subject"]


def test_the_benign_twin_a_visible_page_is_never_redacted(conn, repo):
    """The identical labelled page as above, this time WITH the matching audience — mirrors
    `tests/digest/test_sections_pg.py`'s own labelled-page benign twin. A test that only proves a
    gate fires measures its sensitivity and never its specificity."""
    path = support.write_labelled_page(repo, "leadership/beta-pilot-scope.md", title="Scope",
                                       acl=["leadership"])
    support.rebuild_index(conn, repo)
    finding = _finding(page_path=path)

    scoped = notice.scope_findings_to_channel(conn, [finding], audiences={"leadership"})

    assert scoped == [finding]


def test_an_unlabelled_page_is_never_redacted_regardless_of_audiences(conn, repo):
    """`acl IS NULL` -> open to everyone (`server.acl.visible`'s own truth table) — the empty
    audience default must not redact a page that carries no label at all."""
    path = support.unlabelled_page(repo, "notes/open.md", title="Open")
    support.rebuild_index(conn, repo)
    finding = _finding(page_path=path)

    scoped = notice.scope_findings_to_channel(conn, [finding], audiences=set())

    assert scoped == [finding]


def test_a_page_not_indexed_at_all_is_redacted_fail_closed(conn, repo):
    """Fail-closed, mirroring `digest.sections._visible_pages`'s identical posture: a page not
    (yet) indexed cannot be proven visible, so it must never post unredacted."""
    support.unlabelled_page(repo, "notes/unrelated.md", title="Unrelated")   # rebuild needs >=1 page
    support.rebuild_index(conn, repo)
    finding = _finding(page_path="wiki/notes/does-not-exist.md")

    scoped = notice.scope_findings_to_channel(conn, [finding], audiences=set())

    assert scoped[0]["_notice_action"] == "details in `stigmergy-gardener`"


def test_findings_with_no_notice_page_paths_pass_through_untouched(conn):
    """An SLA finding with nothing page-shaped to scope — nothing here to redact, and nothing
    here to crash on either."""
    finding = {"check": "some-future-check", "severity": schema.SEVERITY_SLA,
              "source": schema.SOURCE_DETERMINISTIC, "subject": "", "detail": "d",
              "suggested_action": "a"}

    scoped = notice.scope_findings_to_channel(conn, [finding], audiences=set())

    assert scoped == [finding]






