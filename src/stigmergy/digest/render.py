"""Deterministic Slack mrkdwn body assembly — pure text, no DB access: `sections.py`'s dicts in,
a string out. Slack mrkdwn composed as such from the start (`*bold*`, `•`; never `#`/`##`, never
fenced blocks, which do not wrap) — the opposite convention from `gardener.report`'s terminal
markdown. Every corpus-derived string passes `escape_mrkdwn` BEFORE composition: an injected page
must not print Slack markup into a message a channel reads as the system speaking.
"""
from stigmergy.gardener import schema as gardener_schema
from stigmergy.slack.mrkdwn import escape_mrkdwn


def _fmt_title(title: str) -> str:
    return f'"{escape_mrkdwn(title)}"'


def _plural(n: int, noun: str) -> str:
    return noun if n == 1 else f"{noun}s"


# ── corpus health ─────────────────────────────────────────────────────────────────────────────
def _render_health(health: dict) -> list[str]:
    lines = ["*Corpus health*"]
    state = health["state"]

    if state == "never_run":
        lines.append("No gardener run has ever completed — this section will show corpus health "
                     "once `stigmergy-gardener` runs at least once.")
        return lines

    if state == "stale":
        last_date = health["last_run_date"].isoformat()
        days = health["days_before_window"]
        lines.append(f"No gardener run in this window; last run {last_date} ({days} "
                     f"{_plural(days, 'day')} before this window started). Run "
                     f"`stigmergy-gardener` to refresh it.")
        return lines

    run_date = health["run_date"].isoformat()
    total = health["total"]
    # A reader must not mistake "no model findings" for "the model passes found nothing" when it
    # means "a pass did not complete" — appended in BOTH branches below, regardless of `total`,
    # and NAMING the pass, exactly as the terminal report does.
    incomplete = health.get("model_passes_incomplete") or []
    incomplete_line = ("(model pass(es) did not complete that run: "
                       + ", ".join(incomplete) + ")") if incomplete else ""
    if total == 0:
        lines.append(f"Latest gardener run: {run_date} — {total} {_plural(total, 'finding')}: "
                     f"every check came back clean")
        if incomplete_line:
            lines.append(incomplete_line)
        return lines

    counts = health["counts_by_severity"]
    lines.append(f"Latest gardener run: {run_date} — {total} {_plural(total, 'finding')}: "
                f"{counts[gardener_schema.SEVERITY_SLA]} sla, "
                f"{counts[gardener_schema.SEVERITY_WARN]} warn, "
                f"{counts[gardener_schema.SEVERITY_INFO]} info")
    if incomplete_line:
        lines.append(incomplete_line)
    for severity in (gardener_schema.SEVERITY_SLA, gardener_schema.SEVERITY_WARN):
        by_check = health["checks_by_severity"].get(severity) or {}
        if not by_check:
            continue
        parts = ", ".join(f"{escape_mrkdwn(chk)} ({n})" for chk, n in sorted(by_check.items()))
        lines.append(f"• {severity}: {parts}")
    info_count = counts[gardener_schema.SEVERITY_INFO]
    if info_count:
        lines.append(f"• info: {info_count} (full breakdown: `stigmergy-gardener`)")
    return lines


# ── corpus deltas ─────────────────────────────────────────────────────────────────────────────
def _render_deltas(deltas: dict) -> list[str]:
    lines = ["*Corpus deltas*"]

    n_pages = deltas["pages_filed_count"]
    if n_pages == 0:
        lines.append("• 0 pages filed")
    else:
        titles = ", ".join(_fmt_title(t) for t in deltas["pages_filed_titles"])
        lines.append(f"• {n_pages} {_plural(n_pages, 'page')} filed — {titles}")

    n_entities = deltas["entities_born_count"]
    # "born" is exact now (ADR 044): this sums what the window's FILINGS actually wrote, so every
    # counted birth is a page in the repo. It said "approved" while the count came off a ledger of
    # verdicts, where an approval could exist without a page.
    noun = "entity" if n_entities == 1 else "entities"
    lines.append(f"• {n_entities} {noun} born")
    return lines


def build_body(*, since, until, health: dict, deltas: dict) -> str:
    """The full digest — deterministic given its inputs; never reads the wall clock. The ONE
    function whose return value both the dry-run preview and the real post use unmodified —
    `cli.py`'s marker lines live outside it — which is what makes the byte-identity
    structural."""
    lines = [f"*Stigmergy digest — {since.date().isoformat()} to {until.date().isoformat()}*", ""]
    lines += _render_health(health)
    lines.append("")
    lines += _render_deltas(deltas)
    return "\n".join(lines).rstrip()
