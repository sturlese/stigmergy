"""The severity-grouped report — text only, no DB access, no side effects: every function here
takes plain data and returns a string. `run.py` gathers the numbers; this module only knows how to
say them, which is what makes it testable with a synthetic finding list and no Postgres fixture at
all.

`#`/`##` markdown headers are correct HERE, because the reader is a terminal and never Slack. This
is the OPPOSITE convention from `digest.render`, which composes Slack mrkdwn (`*bold*` headers, no
literal `#`) — two different readers in two different programs, each matching its own siblings,
not each other.
"""
import json

from stigmergy.gardener import schema
from stigmergy.gardener.checks import ALL_CHECK_SLUGS

# One slug per deterministic check, so the tuple's own length IS the count the report prints — and
# cannot drift from it. A hand-written number here is exactly what goes stale when a check is added
# or retired.
NUM_DETERMINISTIC_CHECKS = len(ALL_CHECK_SLUGS)

# `capture/cli.py::_KIND_WIDTH`'s own precedent, reused for the exact reason its comment states:
# `stigmergy-queue list` shipped with a hardcoded width sized for the vocabulary of the day and broke
# alignment the day a longer value joined it. Computed from the FULL vocabulary, never from what
# happens to be present in one run's findings, so alignment stays stable run to run.
CHECK_WIDTH = max(len(slug) for slug in ALL_CHECK_SLUGS)

# Seven of the eight check slugs carry no runnable fix — `stale-view` is the only one that hands
# over a pasteable command — and a field literally named `suggested_action` needs that said once,
# up front, rather than discovered finding by finding.
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
    """`[deterministic]` or `[model: {model_id}]` — printed inline in the human-readable report,
    not only in `--json`: a label that exists only in the machine format is not rendered at all in
    the surface a human actually reads."""
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
    """Alphabetical by check slug, then by subject — deterministic, so two runs over an unchanged
    corpus produce a byte-identical grouping."""
    return (finding["check"], finding.get("subject") or "")


def _render_severity_section(severity: str, findings: list[dict]) -> list[str]:
    group = sorted((f for f in findings if f["severity"] == severity), key=_sort_key)
    lines = [f"## {severity.upper()} ({len(group)})"]
    if not group:
        # The section header is never silently absent, whether the group holds zero findings or
        # many — silence is not an outcome.
        lines.append("none this run")
        return lines
    for finding in group:
        lines.append(_finding_line(finding))
        lines.append(f"  action: {finding.get('suggested_action', '')}")
    return lines


def sweep_summary_text(changed: int, sampled: int) -> str:
    """"a model sweep over N changed page(s) and M sampled unchanged page(s)" — the
    ready-to-hand-to-`render_report` string a caller (`cli.py`) builds from `RunResult`'s own
    `sweep_changed_count`/`sweep_sampled_count`. Lives here, not in `cli.py` or `run.py`, because
    this module is the one that "only knows how to say them" (its own docstring) — the sentence's
    wording is a rendering decision, not an orchestration one."""
    return f"a model sweep over {changed} changed page(s) and {sampled} sampled unchanged page(s)"


def _corpus_line(*, pages_checked: int, entities_checked: int, sweep_summary: str,
                 sweep_failed: bool = False) -> str:
    line = (f"checked {pages_checked} pages, {entities_checked} entities — "
           f"{NUM_DETERMINISTIC_CHECKS} deterministic checks")
    if sweep_failed:
        # A failed sweep gets its OWN clause, never the "plus a model sweep over..." extension
        # below: that phrasing implies the sweep DID run and describes what it covered, which is
        # dishonest for a pass that produced nothing this run.
        line += "; the model sweep did NOT complete this run (see below)"
    elif sweep_summary:
        # An empty `sweep_summary` is a legitimate value — a caller that has nothing to say about
        # the sweep simply omits the clause.
        line += f", plus {sweep_summary}"
    return line


def render_report(*, run_id: int, completed_at: str, pages_checked: int, entities_checked: int,
                  findings: list[dict], sweep_summary: str = "",
                  sweep_failed: bool = False) -> str:
    """The full human-readable report — severity-grouped, SLA first: worst news first.
    `sweep_failed=True` never withholds or reshapes the findings themselves — every finding
    actually persisted this run still prints in full; only the corpus line and the finding-count
    line's own trailing annotation change, so a reader who scans past the header still sees the
    truth without reading `cli.py`'s separate stderr line first."""
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
    """One JSON object per finding — the same fields the report's table carries, plus `id`/
    `run_id`/`created_at`; no prose wrapper. `suggested_action` is present and populated for EVERY
    finding, sentence or command alike (never `null` for the sentence-only checks — an absent
    field would read as "nothing to do", which is false).

    `model_id` is `""` for a deterministic finding, never omitted: it is one of the fields the
    table carries. The human-readable report is what actually labels a finding's source
    (`_source_tag` — a machine-only label does not count as rendered), but a machine reader of
    `--json` deserves the fact `source` alone cannot carry: WHICH model, not merely THAT one
    ran."""
    payload = [
        {"id": f.get("id"), "run_id": f.get("run_id"), "check": f["check"],
         "severity": f["severity"], "source": f.get("source", schema.SOURCE_DETERMINISTIC),
         "subject": f.get("subject", ""), "detail": f.get("detail", ""),
         "suggested_action": f.get("suggested_action", ""), "model_id": f.get("model_id", ""),
         "created_at": _json_datetime(f.get("created_at"))}
        for f in findings
    ]
    return json.dumps(payload, ensure_ascii=False, indent=2)
