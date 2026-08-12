"""Dedup, three levels, cheapest first. Levels 1-2 match only `status='filed'` rows.

1. **Retry collapse** — identical content from the SAME submitter inside a short window is one
   capture arriving twice. The second row reaches `filed` with the SAME `result_ref`, never
   `rejected`: resubmitting is ordinary behavior and must not read as a penalty for a retry.
2. **Already filed** — the same hash outside that window or from someone else: `rejected`, with a
   pointer to the page. Deterministic and before the agent, like level 1.
3. **Near-duplicate** — the agent's judgment against the graph. It lives in the agent and the
   report, not here.

Keyed on `capture_queue.payload->>'sha256'`, computed at submit time. A purged row therefore
cannot be matched — retention nulls `payload` after 30 days — which is correct: pointing a
submitter at a page whose provenance is no longer establishable is worse than treating the
capture as new.

The window is measured between the two SUBMISSIONS (the current row's own `created_at`), never
against the worker's clock, or the window closes as the queue lags and level 1 becomes
unreachable — leaving level 2, which has no window, to answer a retry with "rejected".
"""
from dataclasses import dataclass

from stigmergy.capture import schema

# One query, two semantic entry points over it: "mine, within a window of THIS submission" vs
# "anyone's, ever" is the whole distinction between a retry and a re-submission.
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

    `submitter=None` means "any identity"; `window_s=None` means "any time", and with a window
    `anchor` is the CURRENT submission's `created_at`, never the wall clock. Callers go through
    the two wrappers below.

    `kind` is REQUIRED: the same bytes submitted as `raw` and as `meeting` are two different
    requests, not a retry — collapsing them would report one page for a meeting capture that
    should have produced a page SET.
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
    """LEVEL 1 — the same person's identical material, of the SAME kind, submitted within
    `window_s` of THIS row. A retry.

    A row with no `created_at` falls through to level 2 rather than silently widening the window
    to "ever".
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
