"""`digest.sections._filed_page_paths` — pure, no DB: the meeting-capture blind spot. Verified
against `librarian.report.filed_meeting`'s own real return shape
(`{"source_pages": [...], "meeting_page": ..., "decisions": [{"path": ..., "anchored_to": ...}]}`)
and `librarian.processing._file_meeting`'s own `result_ref = f"{meeting_page}@{sha}"`, both read
directly before these bounds were written."""
from stigmergy.digest.sections import _filed_page_paths


def test_ordinary_single_page_capture_resolves_from_result_ref():
    assert _filed_page_paths(None, "wiki/decisions/floor.md@abc123") == [
        "wiki/decisions/floor.md"]


def test_ordinary_capture_with_an_empty_report_dict_still_resolves_from_result_ref():
    assert _filed_page_paths({}, "wiki/decisions/floor.md@abc123") == [
        "wiki/decisions/floor.md"]


def test_unparseable_result_ref_yields_nothing_never_guessed_at():
    assert _filed_page_paths(None, "") == []
    assert _filed_page_paths(None, "no-at-sign-at-all") == []


def test_meeting_capture_expands_to_every_page_in_the_set_never_only_result_ref():
    """The blind spot itself: `result_ref` names ONLY the meeting page, but a meeting capture
    actually filed the source transcript(s) AND every decision page too — all of them must be
    resolved, not the one `result_ref` happens to name."""
    report = {
        "filed_meeting": {
            "source_pages": ["sources/meetings/2026-07-30-standup.md"],
            "meeting_page": "wiki/meetings/2026-07-30-standup.md",
            "decisions": [{"path": "wiki/decisions/a.md", "anchored_to": "x"},
                         {"path": "wiki/decisions/b.md", "anchored_to": "y"}],
        }
    }
    paths = _filed_page_paths(report, "wiki/meetings/2026-07-30-standup.md@abc123")
    assert paths == [
        "sources/meetings/2026-07-30-standup.md",
        "wiki/meetings/2026-07-30-standup.md",
        "wiki/decisions/a.md",
        "wiki/decisions/b.md",
    ]


def test_meeting_capture_with_a_split_source_and_zero_decisions():
    report = {
        "filed_meeting": {
            "source_pages": ["sources/meetings/2026-07-30-standup.md",
                            "sources/meetings/2026-07-30-standup-p2.md"],
            "meeting_page": "wiki/meetings/2026-07-30-standup.md",
            "decisions": [],
        }
    }
    paths = _filed_page_paths(report, "wiki/meetings/2026-07-30-standup.md@abc123")
    assert paths == [
        "sources/meetings/2026-07-30-standup.md",
        "sources/meetings/2026-07-30-standup-p2.md",
        "wiki/meetings/2026-07-30-standup.md",
    ]


def test_meeting_key_present_but_empty_falls_back_to_the_ordinary_result_ref_resolution():
    """An empty `filed_meeting` dict is falsy (`{}`), so this is really the "no filed_meeting key"
    path re-entered, resolving from `result_ref` exactly like an ordinary single-page capture —
    asserted explicitly so a future refactor cannot silently start treating "empty meeting dict"
    and "no meeting dict" differently."""
    assert _filed_page_paths({"filed_meeting": {}}, "wiki/meetings/x.md@sha") == [
        "wiki/meetings/x.md"]
