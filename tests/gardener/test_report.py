"""`gardener.report` — pure rendering: no DB, no side effects, synthetic finding dicts only."""
import datetime
import json

from stigmergy.gardener import report, schema
from stigmergy.gardener.checks import ALL_CHECK_SLUGS


def _finding(check="stale-view", severity=schema.SEVERITY_WARN, subject="wiki/x.md",
            detail="something", suggested_action="do something",
            source=schema.SOURCE_DETERMINISTIC, **extra):
    f = {"check": check, "severity": severity, "source": source, "subject": subject,
        "detail": detail, "suggested_action": suggested_action}
    f.update(extra)
    return f


def _render(findings, **overrides):
    kwargs = {"run_id": 128, "completed_at": "2026-07-31T05:07:03Z", "pages_checked": 412,
             "entities_checked": 38, "findings": findings}
    kwargs.update(overrides)
    return report.render_report(**kwargs)


# ── the empty state ─────────────────────────────────────────────────────────────────────────────
def test_no_findings_is_one_honest_line_never_silence():
    text = _render([])
    assert report.NO_FINDINGS_LINE in text
    assert "## SLA" not in text
    assert "## WARN" not in text
    assert "## INFO" not in text
    assert "finding(s):" not in text
    assert report.JUDGMENT_CALL_PREAMBLE not in text


# ── the header ────────────────────────────────────────────────────────────────────────────────
def test_header_names_the_run_and_the_corpus_counts():
    text = _render([])
    assert "# Gardener report — run #128, completed 2026-07-31T05:07:03Z" in text
    assert "checked 412 pages, 38 entities — 8 deterministic checks" in text


def test_sweep_summary_is_absent_when_the_caller_supplies_none():
    text = _render([])
    assert "plus" not in text


def test_sweep_summary_extends_the_corpus_line_when_a_caller_supplies_one():
    text = _render([], sweep_summary="a model sweep over 3 changed page(s)")
    assert ("8 deterministic checks, plus a model sweep over 3 changed page(s)") in text


# ── severity counts and the judgment-call preamble ──────────────────────────────────────────────
def test_severity_counts_and_judgment_preamble_present_when_findings_exist():
    findings = [_finding(severity=schema.SEVERITY_SLA), _finding(severity=schema.SEVERITY_WARN),
               _finding(severity=schema.SEVERITY_WARN), _finding(severity=schema.SEVERITY_INFO)]
    text = _render(findings)
    assert "4 finding(s): 1 sla, 2 warn, 1 info" in text
    assert report.JUDGMENT_CALL_PREAMBLE in text



def test_zero_severity_sections_still_print_their_header_and_none_this_run():
    findings = [_finding(severity=schema.SEVERITY_WARN)]
    text = _render(findings)
    sla_block = text.split("## SLA (0)")[1].split("## WARN")[0]
    info_block = text.split("## INFO (0)")[1]
    assert "none this run" in sla_block
    assert "none this run" in info_block


# ── grouping and sort order: by check slug, alphabetical, then by subject ───────────────────────
def test_findings_within_a_group_sort_by_check_slug_then_subject():
    findings = [
        _finding(check="stale-view", subject="b.md", severity=schema.SEVERITY_WARN),
        _finding(check="aging-seed", subject="z.md", severity=schema.SEVERITY_WARN),
        _finding(check="aging-seed", subject="a.md", severity=schema.SEVERITY_WARN),
    ]
    text = _render(findings)
    warn_block = text.split("## WARN")[1].split("## INFO")[0]
    assert warn_block.index("aging-seed") < warn_block.index("stale-view")
    assert warn_block.index("a.md") < warn_block.index("z.md")


def test_two_runs_over_the_same_findings_render_byte_identical():
    findings = [_finding(check="stale-view", subject="b.md"),
               _finding(check="aging-seed", subject="a.md")]
    assert _render(list(findings)) == _render(list(reversed(findings)))


# ── the finding line itself ─────────────────────────────────────────────────────────────────────
def test_finding_line_shows_subject_dash_detail_and_source_tag():
    findings = [_finding(check="aging-seed", subject="wiki/product/pricing-model.md",
                         detail="seed, updated 2026-03-02, 151 days ago (threshold 90)")]
    text = _render(findings)
    assert ("wiki/product/pricing-model.md — seed, updated 2026-03-02, 151 days ago "
           "(threshold 90)") in text
    assert "[deterministic]" in text


def test_finding_line_omits_the_subject_dash_when_subject_is_empty():
    findings = [_finding(subject="", check="company-wide-fraction",
                         detail="38% of the last 20 filings declared company-wide")]
    text = _render(findings)
    assert "— 38% of the last 20" not in text
    assert "38% of the last 20 filings declared company-wide" in text


def test_check_slug_column_is_padded_to_the_full_vocabulary_width_not_hardcoded():
    """`capture/cli.py::_KIND_WIDTH`'s own precedent — the width comes from `ALL_CHECK_SLUGS`,
    computed, not from whatever happens to be present in THIS run's findings."""
    assert max(len(s) for s in ALL_CHECK_SLUGS) == report.CHECK_WIDTH
    findings = [_finding(check="aging-seed")]  # a short slug, in a run with only itself present
    text = _render(findings)
    line = next(line for line in text.splitlines() if line.startswith("[WARN]"))
    assert line.startswith(f"[WARN] {'aging-seed'.ljust(report.CHECK_WIDTH)}  ")


def test_action_line_prints_suggested_action_verbatim_backticks_and_all():
    findings = [_finding(suggested_action="`stigmergy-views regenerate --entity acme-corp`")]
    text = _render(findings)
    assert "  action: `stigmergy-views regenerate --entity acme-corp`" in text


def test_source_tag_deterministic_vs_model():
    findings = [_finding(source=schema.SOURCE_DETERMINISTIC),
               _finding(check="model-contradiction", source=schema.SOURCE_MODEL,
                       model_id="gpt-5.4-mini")]
    text = _render(findings)
    assert "[deterministic]" in text
    assert "[model: gpt-5.4-mini]" in text


# ── --json ──────────────────────────────────────────────────────────────────────────────────────
def test_render_json_is_a_bare_array_one_object_per_finding():
    findings = [{"id": 891, "run_id": 128, "check": "aging-seed", "severity": "warn",
                "source": "deterministic", "subject": "wiki/x.md",
                "detail": "seed, updated 2026-03-02, 151 days ago (threshold 90)",
                "suggested_action": "no command runs itself", "model_id": "",
                "created_at": None}]
    payload = json.loads(report.render_json(findings))
    assert payload == findings


def test_render_json_carries_model_id_for_a_model_sourced_finding():
    findings = [{"check": "model-contradiction", "severity": "warn", "source": "model",
                "subject": "wiki/a.md", "detail": "d", "suggested_action": "a",
                "model_id": "gpt-5.4-mini", "created_at": None}]
    payload = json.loads(report.render_json(findings))
    assert payload[0]["model_id"] == "gpt-5.4-mini"


def test_render_json_never_omits_suggested_action_for_a_sentence_only_finding():
    findings = [{"check": "dead-vocabulary", "severity": "info", "source": "deterministic",
                "subject": "meridian-partners", "detail": "d",
                "suggested_action": "no command retires an entity"}]
    payload = json.loads(report.render_json(findings))
    assert payload[0]["suggested_action"] == "no command retires an entity"
    assert "suggested_action" in payload[0]


def test_render_json_serializes_datetime_created_at_to_iso_string():
    findings = [{"check": "stale-view", "severity": "warn", "source": "deterministic",
                "subject": "x.md", "detail": "d", "suggested_action": "a",
                "created_at": datetime.datetime(2026, 7, 31, 5, 7, 3,
                                                tzinfo=datetime.UTC)}]
    payload = json.loads(report.render_json(findings))
    assert payload[0]["created_at"] == "2026-07-31T05:07:03+00:00"


def test_render_json_on_empty_findings_is_an_empty_array():
    assert json.loads(report.render_json([])) == []
