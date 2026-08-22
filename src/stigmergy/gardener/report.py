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


def sweep_summary_text(changed: int, sampled: int, entity_pages: int = 0,
                       registered_entities: int = 0, *, sweep_failed: bool = False) -> str:
    """The model clause for `render_report`, built from `RunResult`'s own counts — what RAN, one
    clause per pass that did.

    `entity_pages` is the SECOND model pass and `registered_entities` the THIRD, each appended
    rather than folded in: both cover their whole population instead of sampling one, and they
    count DIFFERENT populations of the same zone — every entity page against only the pages the
    registry registers — so adding either to another number would misdescribe both. Zero of either
    means that pass had nothing to judge, and a clause about nothing is noise.

    `sweep_failed` drops the EDITORIAL clause only. The three passes fail independently, so one
    failing must not take another's account of itself out of the report with it — the failure
    itself is named by `_corpus_line`."""
    clauses = []
    if not sweep_failed:
        clauses.append(f"a model sweep over {changed} changed page(s) and {sampled} sampled "
                       f"unchanged page(s)")
    if entity_pages:
        clauses.append(f"a body sweep over {entity_pages} entity page(s)")
    if registered_entities:
        clauses.append(f"an identity sweep over {registered_entities} registered entity(ies)")
    return ", and ".join(clauses)


def _corpus_line(*, pages_checked: int, entities_checked: int, sweep_summary: str,
                 sweep_failed: bool = False, empty_body_failed: bool = False,
                 empty_body_deferred: int = 0, duplicate_entity_failed: bool = False,
                 duplicate_entity_deferred: int = 0) -> str:
    """What ran, then what did not. Each model pass names its own failure: a run summary that
    reads clean while a whole model pass never happened is the failure this check exists to end,
    reproduced one layer up."""
    line = (f"checked {pages_checked} pages, {entities_checked} entities — "
           f"{NUM_DETERMINISTIC_CHECKS} deterministic checks")
    if sweep_summary:
        line += f", plus {sweep_summary}"
    if sweep_failed:
        # A failed pass gets its own clause — "plus a model sweep over..." would imply it ran.
        line += "; the model sweep did NOT complete this run (see below)"
    if empty_body_failed:
        line += ("; the entity-body sweep did NOT complete this run — the entity pages it had not "
                 "reached were never judged (see below)")
    elif empty_body_deferred:
        line += (f"; {empty_body_deferred} entity page(s) were not judged this run (the run "
                 f"ceiling bound — nothing was found about them because nothing looked)")
    if duplicate_entity_failed:
        # A failed identity pass loses its WHOLE population, not a remainder: it is one call over
        # the registry, so there is no "pages it had not reached".
        line += ("; the identity sweep did NOT complete this run — no registered entity was "
                 "compared against another (see below)")
    elif duplicate_entity_deferred:
        line += (f"; {duplicate_entity_deferred} registered entity(ies) were not compared this run "
                 f"(the run ceiling bound — nothing was found about them because nothing looked)")
    return line


def render_report(*, run_id: int, completed_at: str, pages_checked: int, entities_checked: int,
                  findings: list[dict], sweep_summary: str = "", sweep_failed: bool = False,
                  empty_body_failed: bool = False, empty_body_deferred: int = 0,
                  duplicate_entity_failed: bool = False,
                  duplicate_entity_deferred: int = 0) -> str:
    """The full human-readable report — severity-grouped, worst news first. A failed pass never
    withholds or reshapes the findings; only the corpus line and the count line's annotation
    change."""
    lines = [f"# Gardener report — run #{run_id}, completed {completed_at}", "",
             _corpus_line(pages_checked=pages_checked, entities_checked=entities_checked,
                         sweep_summary=sweep_summary, sweep_failed=sweep_failed,
                         empty_body_failed=empty_body_failed,
                         empty_body_deferred=empty_body_deferred,
                         duplicate_entity_failed=duplicate_entity_failed,
                         duplicate_entity_deferred=duplicate_entity_deferred), ""]
    if not findings:
        lines.append(NO_FINDINGS_LINE)
        return "\n".join(lines).rstrip() + "\n"

    counts = _severity_counts(findings)
    count_line = (f"{len(findings)} finding(s): {counts[schema.SEVERITY_WARN]} warn, "
                 f"{counts[schema.SEVERITY_INFO]} info")
    if sweep_failed or empty_body_failed or duplicate_entity_failed:
        # "deterministic checks only" is a claim about the findings, not about the passes: another
        # model pass may have completed and contributed some.
        model_findings = sum(1 for f in findings if f.get("source") == schema.SOURCE_MODEL)
        count_line += (" — a model pass did not complete this run" if model_findings
                       else " — deterministic checks only")
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
