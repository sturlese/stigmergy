"""The steward's drain: the three things a human can do with a parked row, and what each one
tells the submitter.

**Three semantic entry points over ONE guarded transition.** `queue.dispose` owns exactly one
question — is this move legal — and answers it in SQL, so a `claimed` row can never be disposed of
by a steward who read it a moment before a worker claimed it. This module owns the other two: which
terminal (or non-terminal) state each business intent lands in, and the sentence the submitter
reads about it. Callers — today only `stigmergy-queue` — name an intent and never a status:

    requeue <id> --by <who> [--note]                 triage/needs_input -> queued
    resolve <id> --by <who> --note [--page|--commit] triage/needs_input -> resolved
    reject  <id> --by <who> --reason                 triage/needs_input -> rejected

**`resolved` is its own terminal state and not a reuse of `rejected`.** They are different news:
`resolved` means a steward handled the material BY HAND — it was used, it probably has a page —
and telling that submitter their work was "rejected" would be a lie about the one thing they care
about. `reject` keeps `rejected`, with the actor and the reason recorded: attribution, not
authorization — the CLI does not check who you are, it records who you said you were.

**Why the wording lives here and not in `librarian.report`.** Every fast-lane sentence is composed
there, and that rule is untouched: nothing in the librarian composes wording anywhere else. But
these two sentences are composed by the STEWARD's tooling, `capture` may not import `librarian`
(the layering the architecture tests pin), and writing them in the librarian would put a sentence
the CLI has to render behind an import the CLI may not make. So the SHAPE moved down to
`capture.schema` (`base_report`, `SEARCHABILITY_NOTE`) where the column that stores it is defined, and
each package composes its own states — one place per package, and the two packages cannot see each
other's. `render_prose` and `brain_submissions` read one object either way.

**The steward's own words are the one free text in this system that a fixed vocabulary does not
produce.** `--note` and `--reason` exist precisely so a human can say something the code cannot,
and they reach the submitter's report verbatim — a channel neither gitleaks nor the PII gate ever
sees, because it never touches the material path at all. Two consequences, both taken:

- they are SANITIZED and BOUNDED by `clean` below, which every entry point here runs them through —
  the same `stigmergy.text.sanitize` seam every echoed value in this repo crosses. It sits BELOW the
  CLIs on purpose: `stigmergy-queue` cleaned its `--reason` and `stigmergy-entities` did not, and a rule
  each CLI has to remember is a rule the next CLI forgets;
- the `--reason`/`--note` help text says out loud that the value is read by the submitter, so a
  steward is warned at the moment they are typing rather than in a doc they may not reopen.

What is NOT done, deliberately: running gitleaks over the note. It would put a subprocess on a
path that has none, and the scanner's own configuration is a base-commit input the CLI does not
resolve.
"""
from stigmergy import text as textutil
from stigmergy.capture import queue, schema

# What a steward may type into a submitter's report. Far smaller than `MAX_REPLY_CHARS`, and for a
# different reason: a reply is an ANSWER whose length is the submitter's business, while this is one
# sentence quoted inside a sentence code composes. Past this it stops reading as a note and starts
# reading as a document that lost its formatting.
MAX_NOTE_CHARS = 500

# The three intents, and the state each one lands in. A mapping rather than three constants so the
# CLI's `choices=` and this module's dispatch cannot drift into disagreeing about what exists.
REQUEUE, RESOLVE, REJECT = "requeue", "resolve", "reject"
DISPOSITIONS = (REQUEUE, RESOLVE, REJECT)


def clean(text: str, width: int = MAX_NOTE_CHARS) -> str:
    """THE seam. Every operator-typed string that reaches a submitter's report crosses this line:
    control characters stripped, newlines flattened into the sentence they are quoted inside, and
    only then clipped — word-safe, at `stigmergy.text.clamp`.

    **It is here, below the CLIs, because a seam a caller can skip is not a seam.** It used to
    clamp a length and nothing else, on the argument that `capture.cli._note` had already done the
    cleaning — true of `stigmergy-queue reject --reason`, and false of `stigmergy-entities reject
    --reason`, which calls `reject(reason=args.reason)` raw. So ANSI escapes pasted into one CLI
    reached the submitter's terminal through `rejected_report` -> `brain_submissions` while the
    identical flag on its sibling was clean (`service._neutralize_report` handles the fence token,
    not control characters). Moving the cleaning down means no CLI has to remember, present or
    future: `capture.cli._note` is now an alias for this function, and a third caller inherits the
    seam by calling `requeue`/`resolve`/`reject` at all.

    The exact expression `librarian.report._clean` uses, deliberately — the two packages compose
    the two halves of one submitter's report, and a difference in how they clean the same kind of
    text would show up as a rendering inconsistency in a single screen.

    **What this does NOT do is make the text safe to have written.** `--note` and `--reason` reach
    the submitter verbatim and are the only text in this system not built from a fixed vocabulary,
    on a channel gitleaks and the PII gate never see. The `--help` for both says so.
    """
    return textutil.clamp(textutil.sanitize(str(text or "")).replace("\n", " ").strip(), width)


# ── the two sentences a steward authors ───────────────────────────────────────────────────────
def resolved_report(*, submission_id: int, actor: str, note: str, page: str = "",
                    commit: str = "") -> dict:
    """`resolved` — a steward handled this outside the fast lane.

    Three shapes, because the honest sentence depends on what the steward actually left behind, and
    a template that pretended otherwise would be silent exactly where it matters most:

    - **page and commit** — the material went somewhere and this names it, with `SEARCHABILITY_NOTE`
      reused verbatim (not rephrased) as the second clause of the same sentence, exactly as `filed`
      carries it. A page a steward folded material into is as invisible to `search_brain` as one
      the librarian filed, and a submitter told "resolved, here is the page" would otherwise go
      looking for it in the brain and not find it.
    - **commit only** — the change is nameable but the page is not (an edit the note describes).
    - **neither** — a genuine resolution that produced no artifact. It says so plainly and names
      who to ask, rather than leaving a reader to infer that "resolved" means "filed somewhere".

    Never says "rejected" and never says "filed": both would be false, which is the whole reason
    this state exists.

    No verification verdict is claimed, because there is none to claim: a steward wrote whatever
    they wrote, under their own name.

    `--page` and `--commit` are operator-typed too, and they cross `clean` here rather than at
    their display sites. They used to cross it only where the SUMMARY quotes them, so the same
    values landed raw in `page_path`/`commit` — the report FIELDS every surface renders — and
    `--commit` reached the summary raw as well, on the assumption that a sha cannot carry anything.
    Nothing checks that it is a sha: `stigmergy-queue resolve --commit "$(...)"` takes whatever the
    steward typed, so ANSI escapes and newlines reached the submitter's terminal through exactly
    the channel this seam exists to close. Cleaning them once, here, means `resolve` can build
    `result_ref` from the cleaned values instead of remembering to clean them a third time.
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

    Deliberately a different sentence SHAPE from every automatic rejection, because the corrective
    action is different: there is no gate to satisfy and no locator to check, so "fix it and
    resubmit" — which every automatic `rejected` ends with — would send this person round a loop
    with nothing at the other end. It says whose judgment it was and who to argue with.

    Carries `reason_code = steward` for a mechanical reason as well as an honest one: `queue`'s read
    path withholds the material of a `rejected` row that carries NO code at all (fail-closed, since
    it cannot know which refusal put it there), so a steward rejection without one would silently
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
    """Back into the queue for the librarian to try again.

    The only disposition that is not terminal, and the only one that writes NO report: the row is
    about to be processed again and whatever it lands in will compose its own. The stale question
    (or park sentence) in `error` IS cleared, though — leaving it would render on a `queued` row as
    though something had gone wrong with a capture that is simply waiting its turn.
    """
    return queue.dispose(conn, submission_id, status=schema.QUEUED, actor=actor,
                         event=schema.EVENT_REQUEUED, action=REQUEUE, note=clean(note), error="")


def resolve(conn, submission_id: int, *, actor: str, note: str, page: str = "",
            commit: str = "") -> dict:
    """Closed as `resolved`: handled by hand, honestly reported, terminal for retention."""
    report = resolved_report(submission_id=submission_id, actor=actor, note=note, page=page,
                             commit=commit)
    # `result_ref` follows the `filed` convention (`<page path>@<sha>`) so the one field every
    # surface already reads for "where did the material go" answers it here too — and stays empty
    # rather than inventing half of it when the steward named only one of the two. Built from the
    # REPORT's own values, not from the arguments: those crossed `clean` on the way into the
    # report, and rebuilding this from the raw arguments would put the uncleaned text back on a
    # second field beside the cleaned one.
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
