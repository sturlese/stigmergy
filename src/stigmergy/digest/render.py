"""Deterministic Slack mrkdwn body assembly — pure text, no DB access: every function here takes
plain data (`sections.py`'s own dicts) and returns a string, the same `gardener.report` split
("this module only knows how to say them, which is what makes it testable with a synthetic finding
list and no Postgres fixture at all") applied one package over.

**Slack mrkdwn, composed as such from the start — never CommonMark, never converted.**
`*bold*` section headers, `•` bullets, no `#`/`##` headings, no fenced code blocks for tabular
content (Slack renders a fenced block in a fixed-width font that does not wrap — the literal
mechanism behind "no horizontal scroll"). This is the OPPOSITE convention from `gardener.report`
(a terminal reader, where `#`/`##` are correct) — two different readers in two different programs,
each matching its own siblings, never each other.

**Every corpus-derived string this module interpolates goes through `slack.mrkdwn.escape_mrkdwn`
first** — a page title (frontmatter a person or an `sources/` source wrote), a page path, a check
slug. `escape_mrkdwn`'s own docstring names exactly this class of string ("any text a client
generates itself... a reason, a title, a quote") as what it exists to protect; applying it here is
the same defensive posture `gardener.sweep.to_finding` already takes for model-sourced text, one
surface over — an injected page must not be able to print Slack markup into a message a channel
reads as the system speaking.
"""
from stigmergy.gardener import schema as gardener_schema
from stigmergy.slack.mrkdwn import escape_mrkdwn


def _fmt_path(path: str) -> str:
    return f"`{escape_mrkdwn(path)}`"


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
    # A run whose model sweep failed still has complete, trustworthy deterministic findings
    # (status='partial', gardener.run.run_gardener) — but a reader must not mistake "no sweep
    # findings this run" for "the sweep found nothing" when it may instead mean "the sweep did not
    # complete". Appended after the summary line in BOTH branches below, regardless of `total`.
    sweep_incomplete = health.get("sweep_incomplete", False)
    if total == 0:
        lines.append(f"Latest gardener run: {run_date} — 0 finding(s): every check came back "
                     f"clean")
        if sweep_incomplete:
            lines.append("(model sweep did not complete that run)")
        return lines

    counts = health["counts_by_severity"]
    lines.append(f"Latest gardener run: {run_date} — {total} finding(s): "
                f"{counts[gardener_schema.SEVERITY_SLA]} sla, "
                f"{counts[gardener_schema.SEVERITY_WARN]} warn, "
                f"{counts[gardener_schema.SEVERITY_INFO]} info")
    if sweep_incomplete:
        lines.append("(model sweep did not complete that run)")
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
        lines.append(f"• {n_pages} pages filed — {titles}")

    n_entities = deltas["entities_born_count"]
    # "approved", not "born": this counts `review_decisions` APPROVALS
    # (`sections._entities_born_count`). A server-driven approval mints in the same act since
    # ADR 030, but a CLI approve is still a separate, later commit with no ledger row of its own
    # (`sections.gather_corpus_deltas`'s own docstring), so an approved-never-minted
    # proposal remains representable and would read as "born" forever. The count is the best
    # available data — the registry carries no timestamps, so the decision ledger is the only
    # windowable source — so only the LABEL has to be exact.
    noun = "birth" if n_entities == 1 else "births"
    lines.append(f"• {n_entities} entity {noun} approved")
    return lines


def build_body(*, since, until, health: dict, deltas: dict) -> str:
    """The full digest — deterministic given its inputs: the SAME `since`/`until` and the same two
    section dicts (`health`, `deltas`) always produce the SAME string, byte for byte. `since`/
    `until` are plain `datetime`s this function never reads off the wall clock itself; `run.py`
    resolves both before calling this, which is what makes the `--dry-run` byte-identity STRUCTURAL
    rather than maintained by convention: this is the one function whose return value both the
    dry-run preview and the real post use, unmodified — the two marker lines `cli.py` prints around
    a dry-run preview live OUTSIDE this function entirely, never inside the string it returns."""
    lines = [f"*Stigmergy digest — {since.date().isoformat()} to {until.date().isoformat()}*", ""]
    lines += _render_health(health)
    lines.append("")
    lines += _render_deltas(deltas)
    return "\n".join(lines).rstrip()
