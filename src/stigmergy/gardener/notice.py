"""The SLA Slack notice: exactly ONE message per run when `sla` findings fired, composed in THIS
package's own module — never appended to `stigmergy.slack.copy`, which is scoped to the
`stigmergy.slack` package's own surfaces — and posted through an injected `SlackGateway`, the same
dependency-injection seam `stigmergy.slack.app.build_context` already uses. Fully testable with
`FakeSlackGateway`; needs no real Slack credentials at all until a real run genuinely has an `sla`
finding to post (`require_channel`/`post_sla_notice`'s own `gateway is None` branch below are where
that requirement is enforced, lazily, never at startup).

Each copy choice carries its own reason, restated here because each is easy to "fix" by
well-meaning habit: `⚠️`, never `\U0001f514` — the bell means "a decision is waiting in
`review_queue`" everywhere else in this codebase, and no `review_decide` verdict ever closes an
`sla` finding, so reusing it would teach the wrong mental model the first time it is seen. The
emoji is always paired with the plain word "SLA". A numbered list, not bullets — this is the one
Slack surface where a reader might refer back to "item 2" out loud. A bare URL, never a
`<url|text>` markdown link (matching every existing doorbell filling's own convention; Slack
auto-links a bare URL on its own).

**This notice posts to the SAME Slack channel `stigmergy.digest` broadcasts to, so it needs the SAME
ACL scoping `digest`'s own package docstring requires of every page title/path it renders.** The
mechanical guard (`tests/test_architecture.py::ACL_REACHABILITY_EXCEPTIONS`) cannot see this on its
own: this notice reads findings rows, never `pages_index`, so a page path could otherwise reach a
channel whose audience the digest scopes titles for, entirely unscoped. `scope_findings_to_channel`
below is that defense: every SLA finding whose `_notice_page_paths` are not ALL
`server.acl.visible()` at the posting channel's audiences gets its notice-facing wording REDACTED
before `compose_notice` ever sees it — never dropped, so the alert survives (an aggregate count and
a check slug still reach the channel; see this module's `_redact` for the exact residual and
`run.run_gardener` for where `audiences` is resolved).

**`_notice_page_paths` is a LIST, never a scalar page path, and that shape is load-bearing.** The
wording this scoping protects can be composed from SEVERAL pages at once — a finding may name a
second, different page its text was composed from, or one it merely read a value out of. Against a
scalar key, a finding whose own subject page is unlabelled but whose text names a restricted page
posts that restricted page's identity unredacted: the key names the wrong — or an incomplete — set
of pages to check. So the key is always a list, `server.acl.all_visible` is the predicate, and a
finding is redacted unless EVERY path in its list is visible — never per-path. The general lesson,
worth carrying to the next sentence template that names a page: the scoping key's shape must match
the composition's shape.
"""
from stigmergy.gardener import schema
from stigmergy.gardener.errors import GardenerError
from stigmergy.gardener.settings import DIGEST_CHANNEL_ID_ENV
from stigmergy.server.acl import all_visible, visible
from stigmergy.slack.gateway import SlackGateway

WARNING_EMOJI = "⚠️"
REDACTED_ACTION = "details in `stigmergy-gardener`"


def sla_findings(findings: list[dict]) -> list[dict]:
    """The `sla`-severity subset, in the order given — the ONLY findings this notice ever
    mentions. A run with only info/warn findings posts nothing at all."""
    return [f for f in findings if f["severity"] == schema.SEVERITY_SLA]


def _visible_page_paths(conn, findings: list[dict], *, audiences: set[str]) -> set[str]:
    """The UNION of every `_notice_page_paths` list among `findings` that `acl.visible()` clears
    for `audiences` — mirrors `digest.sections._visible_pages`'s identical ACL-lookup shape,
    narrowed to the one fact this notice needs: a set of clear-to-name paths, never a title
    mapping. A path not (yet) indexed is absent from the result and therefore treated as NOT
    visible — fail closed, the same "excluded, never counted without being nameable" posture
    `_visible_pages` documents for itself. `scope_findings_to_channel` below is what turns "is this
    ONE path ok" into "are ALL of this finding's paths ok" (`server.acl.all_visible`) — this
    function only ever answers the first, cheaper question, once, over every path any finding
    names."""
    paths = {p for f in findings for p in f.get("_notice_page_paths") or []}
    if not paths:
        return set()
    with conn.cursor() as cur:
        cur.execute("SELECT path, acl FROM pages_index WHERE path = ANY(%s)", (sorted(paths),))
        rows = cur.fetchall()
    return {path for path, acl in rows if visible(acl, audiences)}


def _redact(finding: dict) -> dict:
    """A finding whose subject page this channel cannot see: the alert must survive — an aggregate
    count and a check slug still tell a reader that N such findings exist — but never the page
    path, never a url, never anything else `_notice_detail`/`_notice_action` would otherwise have
    named. Replaces both wholesale rather than scrubbing them in place: a partial scrub is exactly
    the kind of defense that looks complete and is not."""
    return {**finding,
           "_notice_detail": "redacted — the page this finding is about is not visible at this "
                             "channel's scope",
           "_notice_action": REDACTED_ACTION}


def scope_findings_to_channel(conn, findings: list[dict], *, audiences: set[str]) -> list[dict]:
    """Redacts the notice-facing wording of every SLA finding unless EVERY path in its
    `_notice_page_paths` is visible at `audiences` (`server.acl.all_visible` — the
    all-visible-or-nothing predicate, never a per-path decision) — called by `run.run_gardener`,
    BEFORE `compose_notice`, with the posting channel's own resolved audiences
    (`slack.channels.channel_audiences`). A finding with no `_notice_page_paths` at all is left
    untouched: there is nothing about it this function could redact. Non-SLA findings pass through
    unexamined; this is intentionally safe to call with a run's FULL finding list, not only its
    `sla_findings()` subset, so callers never have to remember the filtering order."""
    scoped = [f for f in findings if f.get("_notice_page_paths")]
    if not scoped:
        return findings
    ok_paths = _visible_page_paths(conn, findings, audiences=audiences)
    return [f if not f.get("_notice_page_paths") or all_visible(f["_notice_page_paths"], ok_paths)
           else _redact(f)
           for f in findings]


def compose_notice(sla: list[dict], *, run_date: str) -> str:
    """The exact postable text for `sla` (already filtered to `sla`-severity findings; never
    called with an empty list — `post_sla_notice` below is the guard). A finding may carry
    `_notice_detail`/`_notice_action`, this surface's OWN wording, distinct from what the terminal
    report prints; `detail`/`suggested_action` are the fallback for a check that has not set them,
    never silently omitted."""
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
    """Fail-closed, mirroring `slack.settings._require_env`'s exact "name the var, name the
    consequence, name the fix" shape — called only once an `sla` finding actually needs to post,
    never at startup (most runs have none and never touch Slack at all)."""
    if not digest_channel_id:
        raise GardenerError(
            f"${DIGEST_CHANNEL_ID_ENV} is not set — there is nowhere configured to post this "
            f"run's SLA finding(s) to. Set it to a Slack channel id (e.g. C0123456789).")
    return digest_channel_id


async def post_sla_notice(gateway: SlackGateway | None, *, channel: str,
                          findings: list[dict], run_date: str) -> dict | None:
    """Posts exactly ONE message when `findings` contains at least one `sla` finding; posts
    nothing, and needs no gateway or channel at all, otherwise — checked BEFORE either
    precondition below, so an info/warn-only run never needs Slack configured.

    `gateway=None` (no `$SLACK_BOT_TOKEN` configured, `cli.py`'s own decision to make) and an
    unset channel are two DIFFERENT missing preconditions, both refused loudly with the
    findings-are-already-saved reassurance — this is a run-level failure (`sla` findings exist and
    the required notice could not go out), never a silent skip.

    **This promise extends to the CALLER's own preconditions, not only this function's two.**
    `run.run_gardener` resolves `channels.channel_audiences` (needed for
    `scope_findings_to_channel`, which must run BEFORE `findings` reaches this function) no earlier
    than `sla_findings(findings)` being non-empty here would allow it to matter — the identical
    short-circuit, one level up, so a malformed channels file cannot fail a run this function itself
    was always going to let through untouched.
    """
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
