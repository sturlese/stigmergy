"""The steward's drain: the three things a human can do with a parked row, and what each one
tells the submitter.

Three semantic entry points over ONE guarded transition (`queue.dispose`, which decides legality
in SQL). Callers name an intent, never a status:

    requeue <id> --by <who> [--note]                 triage/needs_input -> queued
    resolve <id> --by <who> --note [--page|--commit] triage/needs_input -> resolved
    reject  <id> --by <who> --reason                 triage/needs_input -> rejected

`resolved` is not a reuse of `rejected`: telling a submitter their hand-filed material was
"rejected" would be a lie. `--by` is attribution, not authorization — recorded, never checked.

The wording lives here, not in `librarian.report`: these sentences are the steward tooling's,
and `capture` may not import `librarian` — the shared SHAPE sits in `capture.schema`
(`base_report`, `SEARCHABILITY_NOTE`) beside the column that stores it.

`--note`/`--reason` are the one free text a fixed vocabulary does not produce, reaching the
submitter verbatim on a channel neither gitleaks nor the PII gate sees. They are sanitized and
bounded by `clean` below (below the CLIs, so none can skip it), and their help text warns the
steward at typing time. Deliberately NOT gitleaks-scanned: that would put a subprocess on a
path that has none, with a base-commit input the CLI does not resolve.
"""
from stigmergy import text as textutil
from stigmergy.capture import queue, schema

# What a steward may type into a submitter's report: one sentence quoted inside a sentence code
# composes — past this it reads as a document that lost its formatting.
MAX_NOTE_CHARS = 500

# The three intents. A tuple rather than three loose constants so the CLI's `choices=` and this
# module's dispatch cannot drift.
REQUEUE, RESOLVE, REJECT = "requeue", "resolve", "reject"
DISPOSITIONS = (REQUEUE, RESOLVE, REJECT)


def clean(text: str, width: int = MAX_NOTE_CHARS) -> str:
    """THE seam every operator-typed string crosses on its way into a submitter's report:
    control characters stripped, newlines flattened, then clipped word-safe.

    Below the CLIs because a seam a caller can skip is not a seam — one CLI once cleaned its
    `--reason` while its sibling passed it raw, and ANSI escapes reached a submitter's terminal.
    The exact expression `librarian.report._clean` uses, deliberately: the two packages compose
    the two halves of one report, and a difference would render on a single screen. This does
    NOT make the text safe to have written — the channel is unscanned, and the `--help` says so.
    """
    return textutil.clamp(textutil.sanitize(str(text or "")).replace("\n", " ").strip(), width)


# ── the two sentences a steward authors ───────────────────────────────────────────────────────
def resolved_report(*, submission_id: int, actor: str, note: str, page: str = "",
                    commit: str = "") -> dict:
    """`resolved` — a steward handled this outside the fast lane.

    Three shapes, because the honest sentence depends on what the steward left behind: page and
    commit (named, with `SEARCHABILITY_NOTE` reused verbatim — a steward-folded page is as
    invisible to search as a filed one); commit only; neither (says so plainly and names who to
    ask). Never says "rejected" and never says "filed" — both would be false. No verification
    verdict is claimed, because there is none to claim.

    `--page`/`--commit` are operator-typed and cross `clean` HERE, not at display sites: nothing
    checks that a "commit" is a sha, so ANSI escapes could ride it, and cleaning once lets
    `resolve` build `result_ref` from the cleaned values.
    """
    who = clean(actor, 120)
    said = clean(note)
    page = clean(page, 200)
    commit = clean(commit, 120)
    head = (f"{schema.RESOLVED} — a steward ({who}) looked at capture #{submission_id} and handled "
            f"it outside the fast lane")
    if page and commit:
        summary = f"{head}: {said} Folded into {page}@{commit}. {schema.SEARCHABILITY_NOTE}"
    elif commit:
        summary = f"{head}: {said} Committed as {commit}. {schema.SEARCHABILITY_NOTE}"
    elif page:
        summary = f"{head}: {said} The material is in {page}. {schema.SEARCHABILITY_NOTE}"
    else:
        summary = (f"{head}: {said} No page or commit is recorded for this one — ask {who} directly "
                   f"if you want to know exactly what happened to the material.")
    return schema.base_report(status=schema.RESOLVED, summary=summary, page_path=page,
                              commit=commit, resolved_by=who, steward_note=said)


def rejected_report(*, submission_id: int, actor: str, reason: str) -> dict:
    """`rejected`, by a human rather than by a gate.

    A different sentence shape from every automatic rejection: there is no gate to satisfy, so
    "fix it and resubmit" would loop to nothing — it names whose judgment it was and who to
    argue with. Carries `reason_code = steward` because the read path withholds the material of
    a `rejected` row with NO code (fail-closed); without one, a steward rejection would silently
    suppress the submitter's own excerpt.
    """
    who = clean(actor, 120)
    summary = (f"{schema.REJECTED} — a steward ({who}) reviewed capture #{submission_id} and "
               f"declined it: {clean(reason)} Nothing was filed and no partial page exists; your "
               f"material stays archived. This was a steward's judgment call, not an automatic "
               f"check — if you think it's a mistake, follow up with {who} directly.")
    return schema.base_report(status=schema.REJECTED, summary=summary,
                              **{schema.REASON_CODE_KEY: schema.REASON_STEWARD},
                              rejected_by=who, steward_note=clean(reason))


# ── the three semantic entry points ───────────────────────────────────────────────────────────
def requeue(conn, submission_id: int, *, actor: str, note: str = "") -> dict:
    """Back into the queue for the librarian to try again. The only non-terminal disposition,
    and the only one that writes NO report — the next pass composes its own. The stale question
    in `error` IS cleared: left, it would render on a `queued` row as though something were
    wrong."""
    return queue.dispose(conn, submission_id, status=schema.QUEUED, actor=actor,
                         event=schema.EVENT_REQUEUED, action=REQUEUE, note=clean(note), error="")


def resolve(conn, submission_id: int, *, actor: str, note: str, page: str = "",
            commit: str = "") -> dict:
    """Closed as `resolved`: handled by hand, honestly reported, terminal for retention."""
    report = resolved_report(submission_id=submission_id, actor=actor, note=note, page=page,
                             commit=commit)
    # `result_ref` follows the `filed` convention (`<page path>@<sha>`). Built from the REPORT's
    # values, not the arguments: those crossed `clean` on the way in, and rebuilding from the
    # raw arguments would put uncleaned text back beside the cleaned field.
    ref_page, ref_commit = report["page_path"], report["commit"]
    result_ref = f"{ref_page}@{ref_commit}" if ref_page and ref_commit else (
        ref_page or ref_commit or "")
    return queue.dispose(conn, submission_id, status=schema.RESOLVED, actor=actor,
                         event=schema.EVENT_RESOLVED, action=RESOLVE, note=clean(note),
                         error=report["summary"], result_ref=result_ref, report=report)


def reject(conn, submission_id: int, *, actor: str, reason: str) -> dict:
    """Closed as `rejected`, with the steward named and their reason recorded."""
    report = rejected_report(submission_id=submission_id, actor=actor, reason=reason)
    return queue.dispose(conn, submission_id, status=schema.REJECTED, actor=actor,
                         event=schema.EVENT_REJECTED, action=REJECT, note=clean(reason),
                         error=report["summary"], report=report)
