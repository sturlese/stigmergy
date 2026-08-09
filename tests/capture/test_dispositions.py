"""`capture.dispositions`' two composed sentences (`resolved_report`/`rejected_report`) — pure
functions, no database, no network, mirroring `test_schema.py`'s own posture.

The submitter's view shows what happened and where the material went, with `--page`/`--commit`
echoed in the report — and that starts here: `_cmd_resolve` only ever passes through what the
steward typed, so the four shapes below are the whole of what a submitter can be told.
"""
import pytest

from stigmergy.capture import dispositions, schema


# ── resolved_report: four shapes, one honest sentence each ──────────────────────────────────────
def test_resolved_report_with_page_and_commit_names_both_and_says_not_searchable():
    out = dispositions.resolved_report(submission_id=6, actor="steward", note="folded into the page",
                                       page="wiki/entities/Jordan Reyes.md", commit="abc123")
    assert out["status"] == schema.RESOLVED
    assert "wiki/entities/Jordan Reyes.md@abc123" in out["summary"]
    # this used to assert `schema.NOT_SEARCHABLE in out["summary"]` — true whatever the
    # constant said, so it never pinned the WORDING. The constant used to claim the page was
    # "invisible to search_brain/ask until the next index rebuild", which is false on
    # webhook-enabled deployments (observed live on staging: searchable seconds after the
    # librarian's push while this report still promised invisibility). The wording is pinned
    # LITERALLY here — never through the constant — so the promise is executable, not circular.
    assert "at the next index rebuild" in out["summary"]
    assert "incremental upsert" in out["summary"]
    assert "whichever lands first" in out["summary"]
    assert "invisible to search_brain" not in out["summary"]
    assert out["page_path"] == "wiki/entities/Jordan Reyes.md"
    assert out["commit"] == "abc123"
    assert "folded into the page" in out["summary"]
    assert "steward" in out["summary"]
    # never says filed or rejected — both would be false (module docstring)
    assert schema.FILED not in out["summary"]
    assert schema.REJECTED not in out["summary"]


def test_resolved_report_with_commit_only_names_it_without_claiming_a_page():
    out = dispositions.resolved_report(submission_id=6, actor="steward", note="an edit, no new page",
                                       commit="abc123")
    assert "Committed as abc123" in out["summary"]
    assert out["page_path"] == ""
    assert out["commit"] == "abc123"
    # the commit-only shape composes its own copy of the wording contract
    # (dispositions.py's `resolved_report`, `elif commit:` branch) — pinned literally here too.
    assert "at the next index rebuild" in out["summary"]
    assert "incremental upsert" in out["summary"]
    assert "whichever lands first" in out["summary"]
    assert "invisible to search_brain" not in out["summary"]


def test_resolved_report_with_page_only_names_it_without_claiming_a_commit():
    out = dispositions.resolved_report(submission_id=6, actor="steward", note="filed by hand",
                                       page="wiki/entities/Jordan Reyes.md")
    assert "wiki/entities/Jordan Reyes.md" in out["summary"]
    assert out["page_path"] == "wiki/entities/Jordan Reyes.md"
    assert out["commit"] == ""
    # the page-only shape composes its own copy of the wording contract
    # (dispositions.py's `resolved_report`, `elif page:` branch) — pinned literally here too.
    assert "at the next index rebuild" in out["summary"]
    assert "incremental upsert" in out["summary"]
    assert "whichever lands first" in out["summary"]
    assert "invisible to search_brain" not in out["summary"]


def test_resolved_report_with_neither_says_so_plainly_and_names_who_to_ask():
    """The missing-pointer case (`_cmd_resolve`'s own warning): a real finding, not decoration — a
    submitter's report must not silently claim more than the steward actually gave it."""
    out = dispositions.resolved_report(submission_id=6, actor="steward", note="handled it")
    assert out["page_path"] == ""
    assert out["commit"] == ""
    assert "No page or commit is recorded" in out["summary"]
    assert "ask steward directly" in out["summary"]


def test_resolved_report_clamps_a_long_actor_and_note():
    out = dispositions.resolved_report(submission_id=6, actor="x" * 200, note="y" * 1000)
    assert len(out["summary"]) < 2000   # bounded, not a document-length quote


# ── rejected_report: a human's judgment call, not a gate's ──────────────────────────────────────
def test_rejected_report_names_the_actor_and_the_reason_and_reads_as_a_judgment_call():
    out = dispositions.rejected_report(submission_id=9, actor="steward", reason="not brain material")
    assert out["status"] == schema.REJECTED
    assert "not brain material" in out["summary"]
    assert "steward" in out["summary"]
    assert "steward's judgment call, not an automatic check" in out["summary"]
    assert "follow up with steward directly" in out["summary"]


def test_rejected_report_never_ends_with_fix_and_resubmit():
    """Deliberately a different sentence SHAPE from every automatic rejection (module docstring):
    there is no gate to satisfy, so the automatic "fix this and resubmit" framing would send this
    person round a loop with nothing at the other end."""
    out = dispositions.rejected_report(submission_id=9, actor="steward", reason="wrong venue")
    assert "resubmit" not in out["summary"]


def test_rejected_report_carries_the_steward_reason_code():
    """Mechanical necessity, not merely honesty (module docstring): `queue`'s read path withholds
    the material of ANY `rejected` row carrying no `reason_code` at all — a steward rejection
    without one would silently suppress the submitter's own excerpt."""
    out = dispositions.rejected_report(submission_id=9, actor="steward", reason="wrong venue")
    assert out[schema.REASON_CODE_KEY] == schema.REASON_STEWARD


# ── adversarial: ANSI escapes and newlines in a steward's free text ─────────────────────────────
def test_resolved_report_clean_sanitizes_control_characters_and_flattens_newlines_itself():
    """The sanitize seam lives DOWN in `dispositions.clean` itself, below every CLI, because a
    seam a caller has to remember is one `stigmergy-entities reject --reason` skipped in practice
    (module docstring: "a seam a CLI has to remember to call is one ... can skip, and did").
    `dispositions.clean` used to be "length safety only"; it is now the one place control
    characters are scrubbed, whichever caller reaches it and whether or not that caller remembered
    its own cleaning.

    Regression coverage: if `dispositions.clean` ever again stopped calling `rank.sanitize` (or a
    CLI's own `_note`/`_clean` became the only place this ran), this test goes red — which is
    exactly the shape of the old defect, where `stigmergy-entities reject --reason` reached the
    submitter unsanitized because it called `dispositions.reject` directly, bypassing
    `capture.cli`'s seam entirely.
    """
    ansi_note = "handled it \x1b[31mred\x1b[0m\nline two\x07"
    out = dispositions.resolved_report(submission_id=1, actor="steward", note=ansi_note,
                                       page="wiki/entities/X.md", commit="abc123")
    assert "\x1b" not in out["summary"]
    assert "\x07" not in out["summary"]
    assert "\n" not in out["summary"]
    # the newline is FLATTENED, not deleted — the steward's two lines both survive as one sentence
    assert "handled it" in out["summary"]
    assert "red" in out["summary"]
    assert "line two" in out["summary"]
    # and the same is true of `steward_note`, the field a future surface may render on its own
    assert "\x1b" not in out["steward_note"]
    assert "\x07" not in out["steward_note"]
    assert "\n" not in out["steward_note"]


def test_rejected_report_also_sanitizes_control_characters_in_the_reason_and_steward_note():
    """The sibling shape of the test above, over `rejected_report` — the other entry point
    `dispositions.clean` guards."""
    ansi_reason = "not brain material \x1b[31m(spam)\x1b[0m\nplease resubmit properly"
    out = dispositions.rejected_report(submission_id=9, actor="steward", reason=ansi_reason)
    assert "\x1b" not in out["summary"]
    assert "\n" not in out["summary"]
    assert "\x1b" not in out["steward_note"]
    assert "not brain material" in out["summary"]
    assert "(spam)" in out["summary"]


def test_the_stigmergy_entities_reject_path_is_covered_with_no_cli_and_no_db():
    """The other half of the same property: `stigmergy-entities reject` calls `dispositions.reject`
    (the library function) DIRECTLY — `entities.cli._cmd_reject` never passes through
    `capture.cli`'s `_note`/`_clean` at all (see `entities/cli.py`'s module docstring: "`reject`
    does not write a transition. It calls `capture.dispositions.reject`"). So the property that
    matters for that CLI is not "does `entities.cli` clean its input" — it does not, by design —
    it is "does `dispositions.reject` clean it regardless of who calls it". Proven here by calling
    the library function exactly as `entities.cli._cmd_reject` does: no CLI, no argparse, no
    database — a pure call, faking nothing that could be integrated instead. `dispositions.reject`
    is real production code and a Postgres connection is the only thing it needs that this test
    does not have to provide, because `rejected_report` — what the CLI ultimately renders — is
    exercised directly.
    """
    hostile_reason = 'Acme$(touch PWNED)\x1b[31m --aliases "Jordan Reyes'
    out = dispositions.rejected_report(submission_id=41, actor="steward <steward@example.com>",
                                       reason=hostile_reason)
    assert "\x1b" not in out["summary"]
    assert "$(touch PWNED)" in out["summary"]      # the TEXT survives — only control bytes are cut
    assert out[schema.REASON_CODE_KEY] == schema.REASON_STEWARD


@pytest.mark.parametrize("width_note", [
    ("resolve", lambda note: dispositions.resolved_report(
        submission_id=1, actor="steward", note=note, page="p.md", commit="c")),
    ("reject", lambda note: dispositions.rejected_report(submission_id=1, actor="steward",
                                                         reason=note)),
])
def test_a_note_at_exactly_the_max_note_chars_boundary_is_not_truncated(width_note):
    """The benign twin of the clamp: a note that FITS must survive whole."""
    _, build = width_note
    note = "y" * dispositions.MAX_NOTE_CHARS
    out = build(note)
    assert note in out["summary"]
    assert "…" not in out["summary"]


def test_the_commit_and_page_a_steward_types_cross_the_same_seam_the_note_does():
    """OLD BEHAVIOUR: `--commit` reached the summary raw (ANSI escapes, BEL and newlines all
    survived), and BOTH `--page` and `--commit` landed raw in the report's own `page_path`/`commit`
    fields — cleaned only where the summary quoted the page, and never for the commit.

    `clean`'s docstring calls itself THE seam every operator-typed string that reaches a
    submitter's report crosses, and states why it sits below the CLIs: "a rule each CLI has to
    remember is a rule the next CLI forgets". These two were the fields the seam itself forgot.
    Nothing validates that `--commit` is a sha — `stigmergy-queue resolve --commit "$(...)"` takes
    whatever the steward typed — so this is the same channel `--note` already crosses."""
    hostile = "abc\x1b[31mRED\x1b[0m\nsecond line\x07"
    out = dispositions.resolved_report(submission_id=7, actor="steward@example.com",
                                       note="handled by hand", page=f"wiki/{hostile}.md",
                                       commit=hostile)
    for field in (out["summary"], out["page_path"], out["commit"]):
        assert "\x1b" not in field
        assert "\x07" not in field
        assert "\n" not in field
    assert "RED" in out["commit"]        # the TEXT survives — only control bytes are cut


def test_an_ordinary_page_and_sha_are_reported_and_referenced_untouched(monkeypatch):
    """The benign twin: the real shape of both fields — a plain path and a 40-hex sha — reaches the
    submitter whole, and `result_ref` still spells the `<page>@<sha>` convention every surface
    reads."""
    calls = {}
    monkeypatch.setattr(dispositions.queue, "dispose",
                        lambda conn, sid, **kw: calls.update(kw) or {"ok": True})
    sha = "a" * 40
    dispositions.resolve(None, 7, actor="steward@example.com", note="handled",
                         page="wiki/notes/q3-plan.md", commit=sha)
    assert calls["result_ref"] == f"wiki/notes/q3-plan.md@{sha}"
    assert calls["report"]["page_path"] == "wiki/notes/q3-plan.md"
    assert calls["report"]["commit"] == sha
    assert f"wiki/notes/q3-plan.md@{sha}" in calls["report"]["summary"]
