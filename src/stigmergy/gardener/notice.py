"""The SLA Slack notice: exactly ONE message per run when `sla` findings fired, composed here
(never in `stigmergy.slack.copy`) and posted through an injected `SlackGateway`. Credentials are
required lazily, only when a real `sla` finding needs to post.

Copy constraints: `⚠️` paired with the word "SLA", never the bell — the bell means "a decision is
waiting in `review_queue`" everywhere else and no verdict closes an `sla` finding.

This notice posts to the channel `stigmergy.digest` broadcasts to, so it needs the same ACL
scoping — and no mechanical guard sees this path, because the notice reads findings rows, never
`pages_index`. `scope_findings_to_channel` REDACTS (never drops) any SLA finding whose
`_notice_page_paths` are not all visible at the posting channel's audiences.

`_notice_page_paths` is a LIST, never a scalar: the protected wording can be composed from
several pages, and a scalar key would let a restricted page named in the text post unredacted.
`all_visible` is the predicate — every path visible, or the finding is redacted.
"""
from stigmergy.gardener import schema
from stigmergy.gardener.errors import GardenerError
from stigmergy.gardener.settings import DIGEST_CHANNEL_ID_ENV
from stigmergy.server.acl import all_visible, visible
from stigmergy.slack.gateway import SlackGateway

WARNING_EMOJI = "⚠️"
REDACTED_ACTION = "details in `stigmergy-gardener`"


def sla_findings(findings: list[dict]) -> list[dict]:
    """The `sla`-severity subset — the only findings this notice ever mentions."""
    return [f for f in findings if f["severity"] == schema.SEVERITY_SLA]


def _visible_page_paths(conn, findings: list[dict], *, audiences: set[str]) -> set[str]:
    """Every `_notice_page_paths` entry that `acl.visible()` clears for `audiences`. A path not
    (yet) indexed is absent and therefore NOT visible — fail closed."""
    paths = {p for f in findings for p in f.get("_notice_page_paths") or []}
    if not paths:
        return set()
    with conn.cursor() as cur:
        cur.execute("SELECT path, acl FROM pages_index WHERE path = ANY(%s)", (sorted(paths),))
        rows = cur.fetchall()
    return {path for path, acl in rows if visible(acl, audiences)}


def _redact(finding: dict) -> dict:
    """The alert survives (count + check slug), the wording does not. Both fields are replaced
    wholesale, never scrubbed in place — a partial scrub looks complete and is not."""
    return {**finding,
           "_notice_detail": "redacted — the page this finding is about is not visible at this "
                             "channel's scope",
           "_notice_action": REDACTED_ACTION}


def scope_findings_to_channel(conn, findings: list[dict], *, audiences: set[str]) -> list[dict]:
    """Redacts the notice-facing wording of every finding unless EVERY path in its
    `_notice_page_paths` is visible at `audiences` — called before `compose_notice` with the
    posting channel's resolved audiences. A finding with no `_notice_page_paths` passes through
    untouched; safe to call with a run's full finding list."""
    scoped = [f for f in findings if f.get("_notice_page_paths")]
    if not scoped:
        return findings
    ok_paths = _visible_page_paths(conn, findings, audiences=audiences)
    return [f if not f.get("_notice_page_paths") or all_visible(f["_notice_page_paths"], ok_paths)
           else _redact(f)
           for f in findings]


def compose_notice(sla: list[dict], *, run_date: str) -> str:
    """The exact postable text (never called with an empty list — `post_sla_notice` guards).
    `_notice_detail`/`_notice_action` are this surface's own wording; `detail`/
    `suggested_action` are the fallback, never silently omitted."""
    plural = "" if len(sla) == 1 else "s"
    lines = [f"{WARNING_EMOJI} SLA: stigmergy-gardener found {len(sla)} issue{plural} this run "
            f"({run_date})", ""]
    for i, finding in enumerate(sla, start=1):
        detail = finding.get("_notice_detail", finding["detail"])
        action = finding.get("_notice_action", finding["suggested_action"])
        lines.append(f"{i}. {finding['check']} — {detail}. {action}")
    lines.append("")
    lines.append("Full report: `stigmergy-gardener`")
    return "\n".join(lines)


def require_channel(digest_channel_id: str) -> str:
    """Fail closed — called only once an `sla` finding actually needs to post, never at
    startup."""
    if not digest_channel_id:
        raise GardenerError(
            f"${DIGEST_CHANNEL_ID_ENV} is not set — there is nowhere configured to post this "
            f"run's SLA finding(s) to. Set it to a Slack channel id (e.g. C0123456789).")
    return digest_channel_id


async def post_sla_notice(gateway: SlackGateway | None, *, channel: str,
                          findings: list[dict], run_date: str) -> dict | None:
    """Posts exactly ONE message when `findings` contains an `sla` finding; otherwise posts
    nothing and needs no gateway or channel at all — the sla check runs first, so an
    info/warn-only run never needs Slack configured. A missing gateway or channel with `sla`
    findings present is a loud run-level failure, never a silent skip. The caller mirrors the
    same short-circuit for resolving channel audiences."""
    sla = sla_findings(findings)
    if not sla:
        return None
    if gateway is None:
        raise GardenerError(
            "an `sla` finding fired this run but no Slack bot token is configured "
            "($SLACK_BOT_TOKEN) — the notice cannot be posted. The findings above are already "
            "saved; set the token and re-run, or post the notice by hand from the report.")
    channel = require_channel(channel)
    text = compose_notice(sla, run_date=run_date)
    return await gateway.chat_post_message(channel, text=text)
