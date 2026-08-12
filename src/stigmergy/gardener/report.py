"""The severity-grouped terminal report — text only, no DB access: plain data in, a string out.
Markdown `#`/`##` headers are correct HERE (a terminal reader) — the opposite convention from
`digest.render`'s Slack mrkdwn.
"""
import json

from stigmergy.gardener import schema
from stigmergy.gardener.checks import ALL_CHECK_SLUGS

# Computed from the slug tuple, so the printed count cannot drift from the checks that ran.
NUM_DETERMINISTIC_CHECKS = len(ALL_CHECK_SLUGS)

# Computed from the FULL vocabulary, never from one run's findings, so alignment is stable.
CHECK_WIDTH = max(len(slug) for slug in ALL_CHECK_SLUGS)

JUDGMENT_CALL_PREAMBLE = (
    "most of what follows is a judgment call, not a one-paste fix: only `stale-view` names a "
    "runnable command below. Everything else names what to go look at.")

NO_FINDINGS_LINE = "no findings — every check came back clean this run"


def _severity_counts(findings: list[dict]) -> dict[str, int]:
    counts = dict.fromkeys(schema.SEVERITIES, 0)
    for f in findings:
        counts[f["severity"]] = counts.get(f["severity"], 0) + 1
    return counts


def _source_tag(finding: dict) -> str:
    """`[deterministic]` or `[model: {model_id}]`, printed inline in the human-readable report."""
    if finding.get("source") == schema.SOURCE_MODEL:
        return f"[model: {finding.get('model_id', '?')}]"
    return "[deterministic]"


def _finding_line(finding: dict) -> str:
    slug = finding["check"].ljust(CHECK_WIDTH)
    subject = finding.get("subject") or ""
    detail = finding.get("detail") or ""
    body = f"{subject} — {detail}" if subject else detail
    return f"[{finding['severity'].upper()}] {slug}  {body}  {_source_tag(finding)}"


def _sort_key(finding: dict) -> tuple[str, str]:
    """Slug then subject — two runs over an unchanged corpus group byte-identically."""
    return (finding["check"], finding.get("subject") or "")


def _render_severity_section(severity: str, findings: list[dict]) -> list[str]:
    group = sorted((f for f in findings if f["severity"] == severity), key=_sort_key)
    lines = [f"## {severity.upper()} ({len(group)})"]
    if not group:
        # The section header is never silently absent — silence is not an outcome.
        lines.append("none this run")
        return lines
    for finding in group:
        lines.append(_finding_line(finding))
        lines.append(f"  action: {finding.get('suggested_action', '')}")
    return lines


def sweep_summary_text(changed: int, sampled: int) -> str:
    """The sweep clause for `render_report`, built from `RunResult`'s two sweep counts."""
    return f"a model sweep over {changed} changed page(s) and {sampled} sampled unchanged page(s)"


def _corpus_line(*, pages_checked: int, entities_checked: int, sweep_summary: str,
                 sweep_failed: bool = False) -> str:
    line = (f"checked {pages_checked} pages, {entities_checked} entities — "
           f"{NUM_DETERMINISTIC_CHECKS} deterministic checks")
    if sweep_failed:
        # A failed sweep gets its own clause — "plus a model sweep over..." would imply it ran.
        line += "; the model sweep did NOT complete this run (see below)"
    elif sweep_summary:
        line += f", plus {sweep_summary}"
    return line


def render_report(*, run_id: int, completed_at: str, pages_checked: int, entities_checked: int,
                  findings: list[dict], sweep_summary: str = "",
                  sweep_failed: bool = False) -> str:
    """The full human-readable report — severity-grouped, worst news first. `sweep_failed=True`
    never withholds or reshapes the findings; only the corpus line and the count line's
    annotation change."""
    lines = [f"# Gardener report — run #{run_id}, completed {completed_at}", "",
             _corpus_line(pages_checked=pages_checked, entities_checked=entities_checked,
                         sweep_summary=sweep_summary, sweep_failed=sweep_failed), ""]
    if not findings:
        lines.append(NO_FINDINGS_LINE)
        return "\n".join(lines).rstrip() + "\n"

    counts = _severity_counts(findings)
    count_line = (f"{len(findings)} finding(s): {counts[schema.SEVERITY_SLA]} sla, "
                 f"{counts[schema.SEVERITY_WARN]} warn, {counts[schema.SEVERITY_INFO]} info")
    if sweep_failed:
        count_line += " — deterministic checks only"
    lines.append(count_line)
    lines.append("")
    lines.append(JUDGMENT_CALL_PREAMBLE)
    lines.append("")
    for severity in schema.SEVERITY_ORDER:
        lines += _render_severity_section(severity, findings)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _json_datetime(value):
    return value.isoformat() if hasattr(value, "isoformat") else value


def render_json(findings: list[dict]) -> str:
    """One JSON object per finding, no prose wrapper. `suggested_action` is populated for every
    finding — an absent field would read as "nothing to do", which is false. `model_id` is `""`
    for a deterministic finding, never omitted."""
    payload = [
        {"id": f.get("id"), "run_id": f.get("run_id"), "check": f["check"],
         "severity": f["severity"], "source": f.get("source", schema.SOURCE_DETERMINISTIC),
         "subject": f.get("subject", ""), "detail": f.get("detail", ""),
         "suggested_action": f.get("suggested_action", ""), "model_id": f.get("model_id", ""),
         "created_at": _json_datetime(f.get("created_at"))}
        for f in findings
    ]
    return json.dumps(payload, ensure_ascii=False, indent=2)
