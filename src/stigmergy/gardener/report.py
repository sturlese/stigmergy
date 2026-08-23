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
    "most of what follows is a judgment call, not a one-paste fix: a finding names what to go "
    "look at, and says so in its own words on the rare occasion it can name a command.")

NO_FINDINGS_LINE = "no findings — every check came back clean this run"


def _severity_counts(findings: list[dict]) -> dict[str, int]:
    counts = dict.fromkeys(schema.SEVERITIES, 0)
    for f in findings:
        counts[f["severity"]] = counts.get(f["severity"], 0) + 1
    return counts


def _finding_line(finding: dict) -> str:
    # No source tag. This report renders ONE run — the one just completed — and every check that
    # runs is deterministic, so the tag could only ever print the same word on every line.
    # `source` still reaches an operator through `render_json` and the admin console, which are
    # the surfaces that can show a run old enough to hold a retired model pass's findings.
    slug = finding["check"].ljust(CHECK_WIDTH)
    subject = finding.get("subject") or ""
    detail = finding.get("detail") or ""
    body = f"{subject} — {detail}" if subject else detail
    return f"[{finding['severity'].upper()}] {slug}  {body}"


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


def _corpus_line(*, pages_checked: int, entities_checked: int) -> str:
    """What ran. Every check is deterministic and the run either completed or raised, so there is
    no "what did not happen" half to this line any more — the failure that shape existed for (a
    summary reading clean while a whole pass never happened) cannot occur when nothing in the run
    is optional."""
    return (f"checked {pages_checked} pages, {entities_checked} entities — "
            f"{NUM_DETERMINISTIC_CHECKS} deterministic checks")


def render_report(*, run_id: int, completed_at: str, pages_checked: int, entities_checked: int,
                  findings: list[dict]) -> str:
    """The full human-readable report — severity-grouped, worst news first."""
    lines = [f"# Gardener report — run #{run_id}, completed {completed_at}", "",
             _corpus_line(pages_checked=pages_checked, entities_checked=entities_checked), ""]
    if not findings:
        lines.append(NO_FINDINGS_LINE)
        return "\n".join(lines).rstrip() + "\n"

    counts = _severity_counts(findings)
    lines.append(f"{len(findings)} finding(s): {counts[schema.SEVERITY_WARN]} warn, "
                 f"{counts[schema.SEVERITY_INFO]} info")
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
    finding — an absent field would read as "nothing to do", which is false.

    `source` and `model_id` are still emitted, and are `"deterministic"`/`""` for every finding a
    run produces now that the model passes are gone. They stay because they are real columns and
    this is a machine contract: dropping a key breaks a consumer parsing it, while a constant one
    breaks nobody. A row a retired model pass wrote keeps its own `source`/`model_id` in the
    table, and reads back through `store.findings_for_run` unchanged."""
    payload = [
        {"id": f.get("id"), "run_id": f.get("run_id"), "check": f["check"],
         "severity": f["severity"], "source": f.get("source", schema.SOURCE_DETERMINISTIC),
         "subject": f.get("subject", ""), "detail": f.get("detail", ""),
         "suggested_action": f.get("suggested_action", ""), "model_id": f.get("model_id", ""),
         "created_at": _json_datetime(f.get("created_at"))}
        for f in findings
    ]
    return json.dumps(payload, ensure_ascii=False, indent=2)
