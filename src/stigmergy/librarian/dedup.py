"""Dedup, three levels, cheapest first.

1. **Retry collapse** — identical content from the SAME submitter inside a short window is one
   capture arriving twice, not two captures. Deterministic, and it runs BEFORE the agent, so a
   double-tap costs nothing. The second row reaches `filed` with the SAME `result_ref` as the
   first: the material genuinely is filed, at that page. `rejected` was considered and refused —
   resubmitting identical material is ordinary behavior and telling that person their capture
   was rejected reads as a penalty for a retry.
2. **Already filed** — content whose hash matches a page already filed, outside that window or
   from someone else. `rejected`, with a pointer to the page. Also deterministic, also before
   the agent.
3. **Near-duplicate** — the agent's judgment against the graph: filed, with a mutual overlap
   callout and `related:` on both sides. Nothing deleted, nothing overwritten. That level lives
   in the agent and the report, not here — it is a meaning problem, and meaning problems belong
   to the agent.

**Keyed on the queue's own hash, not on a page field.** `capture_queue.payload->>'sha256'` is
computed at submit time by `schema.prepare_submission` and is the same number the evidence key
is built from. Grepping the repo for a `content_hash` frontmatter field was the alternative; it
is slower, and that field belongs to source pages rather than to fast-lane captures.

**A purged row cannot be matched**, and that is correct rather than unfortunate: retention nulls
`payload` on terminal rows after 30 days, so the hash is gone. Pointing a submitter at a page
whose provenance the system can no longer establish would be a worse answer than treating the
capture as new.

**The window is measured between the two SUBMISSIONS, never against the clock the worker happens
to run on**, and that is a correction. It used to read `created_at > now() - window`, where `now()`
is *processing* time — so the window closed as the queue lagged. Draining by hand with
`stigmergy-librarian once`, minutes apart: two rows submitted five seconds apart (a real network
glitch) were processed twenty minutes later, level 1 no longer matched anything, and level 2 —
which has no window at all — answered both rows with "rejected, this matches a page already in
the graph". The whole point of level 1 is that a retrying person is not told "rejected", and it
was unreachable in practice. Anchoring on the current row's own `created_at` makes the check what
its definition always said it was: a property of the two submissions, independent of when
anything got around to filing them.
"""
from dataclasses import dataclass

from stigmergy.capture import schema

# One query, two semantic entry points over it. The scoping difference — "mine, within a window of
# THIS submission" vs "anyone's, ever" — is the whole distinction between a retry and a
# re-submission, so it is made once here by name rather than re-remembered at each call site.
_MATCHING_FILED = f"""
SELECT id, result_ref, submitted_by, finished_at
FROM capture_queue
WHERE payload ->> 'sha256' = %(digest)s
  AND kind = %(kind)s
  AND status = '{schema.FILED}'
  AND result_ref <> ''
  AND id <> %(exclude_id)s
  AND (%(submitter)s::text IS NULL OR submitted_by = %(submitter)s)
  AND (%(window_s)s::int IS NULL
       OR (created_at <= %(anchor)s::timestamptz
           AND created_at > %(anchor)s::timestamptz
                            - make_interval(secs => %(window_s)s)))
ORDER BY id
LIMIT 1
"""


@dataclass(frozen=True)
class Match:
    """A prior filed submission carrying the same content."""
    submission_id: int
    result_ref: str
    submitted_by: str
    as_of: str

    @property
    def page_path(self) -> str:
        """`result_ref` is `<page path>@<sha>`; the page is what a human is pointed at."""
        return self.result_ref.rsplit("@", 1)[0] if "@" in self.result_ref else self.result_ref

    @property
    def commit(self) -> str:
        return self.result_ref.rsplit("@", 1)[1] if "@" in self.result_ref else ""


def query_filed_with_digest(conn, *, digest: str, kind: str, exclude_id: int,
                            submitter: str | None = None,
                            window_s: int | None = None,
                            anchor: str | None = None) -> Match | None:
    """THE shared base: the earliest FILED submission whose material hashes to `digest`.

    `submitter=None` means "any identity". `window_s=None` means "any time"; with a window,
    `anchor` is the timestamp the window is measured back from — the CURRENT submission's
    `created_at`, never the wall clock (see the module docstring). Callers do not build this call
    themselves — they go through the two wrappers below.

    **`kind` is REQUIRED, not optional.** Two captures can share a material
    digest while meaning entirely different things to the system that files them: a meeting drop's
    digest is the archived TRANSCRIPT's hash, and an ordinary capture's is the pasted TEXT's — the
    same bytes submitted once as `kind="raw"` and once as `kind="meeting"` are not a retry of one
    another, they are two different requests that happen to carry identical content. Before this,
    a meeting drop whose digest matched an already-filed ORDINARY page collapsed onto that page's
    single `result_ref` — a `filed` report naming one page for a meeting capture that never
    produced a page SET at all (`report.filed_meeting`'s own shape, silently skipped).
    """
    with conn.cursor() as cur:
        cur.execute(_MATCHING_FILED, {"digest": digest, "kind": kind, "exclude_id": exclude_id,
                                      "submitter": submitter, "window_s": window_s,
                                      "anchor": anchor})
        row = cur.fetchone()
    if row is None:
        return None
    submission_id, result_ref, submitted_by, finished_at = row
    return Match(submission_id, result_ref or "", submitted_by or "",
                 finished_at.date().isoformat() if finished_at else "")


def find_retry(conn, item: dict, *, window_s: int) -> Match | None:
    """LEVEL 1 — the same person's identical material, submitted within `window_s` of THIS row,
    of the SAME kind. A retry.

    Runs before level 2 and, now that the window is anchored to this row's own submission time,
    actually gets the chance to: a row with no `created_at` (nothing in production produces one)
    falls through to level 2 rather than silently widening the window to "ever".
    """
    digest = (item.get("payload") or {}).get("sha256") or ""
    anchor = item.get("created_at") or ""
    if not digest or not anchor:
        return None
    return query_filed_with_digest(conn, digest=digest, kind=item["kind"], exclude_id=item["id"],
                                   submitter=item["submitted_by"], window_s=window_s,
                                   anchor=anchor)


def find_already_filed(conn, item: dict) -> Match | None:
    """LEVEL 2 — the same material, of the SAME kind, already in the graph, whoever filed it and
    whenever."""
    digest = (item.get("payload") or {}).get("sha256") or ""
    if not digest:
        return None
    return query_filed_with_digest(conn, digest=digest, kind=item["kind"], exclude_id=item["id"])
