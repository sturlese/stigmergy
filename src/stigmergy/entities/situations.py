"""Which parked rows are an identity decision — the steward's operational view of `triage`.

**One shared base, two semantic entry points, and a write guard** — the discipline `capture.queue`
already applies to submissions listing (`query_submissions` -> `list_own_submissions` /
`list_all_submissions`) and `capture.dispositions` applies to the drain. Nothing here re-queries
Postgres: `queue.query_submissions` stays the single place that knows about scope, paging,
ordering and the withheld-material rule, and this module adds only the one thing it does not know
— which parked rows are an ENTITY situation rather than something else parked.

The distinction matters because `stigmergy-queue list` and `stigmergy-entities list` answer different
questions about the same table. `stigmergy-queue list` is MANAGEMENT: everything, every state, so a
steward can see what the queue is doing. `stigmergy-entities list` is OPERATIONAL: only the rows this
tool can actually act on, because a list that shows rows `approve` will refuse teaches its reader
to ignore it. Writing that filter into the CLI would have been the anti-pattern — every screen
remembering to filter for itself — so it is a named function here and the CLI calls it by name.

**And the read is not the permission.** `require_situation` is the write guard: `approve` and
`reject` ask it before anything is validated, written or pushed, so a row that is `filed`,
`claimed` or simply not an identity question is refused with its actual state named. A steward can
READ any row through `stigmergy-queue show`; being able to read it is not permission to mint an
entity from it.

**Two kinds of row are entity situations, not one.**

- `unresolved-entity` — the agent could not resolve the name the material is about. This is the
  ask-back's terminus.
- `unsupported-type` — the fast lane does not file `person`/`team`/`product` pages, and the
  judgment "this is a page about one specific person" is an identity claim.

**Legacy rows are read tolerantly and this is a transition, not a permanent shape.** A row parked
before `schema.SITUATION_KEY` existed carries no code, so `classify` falls back to the
`open_question` prefix the single-subject report builders write. The fallback is narrow (a prefix,
not a search), it is only ever consulted when the key is absent, and it can be deleted once no such
`triage` row survives retention. It exists because the alternative is a steward being told that a
row this tool can perfectly well drain is not something it handles.
"""
from stigmergy.capture import queue, schema
from stigmergy.entities.errors import EntityError

# What `report.triage_entity` / `report.triage_type` have always written into `open_question`. The
# ONLY use of this is classifying rows written before the situation code existed; anything with a
# code is read from the code. Kept beside its own explanation so a future reader can tell it is a
# migration artifact rather than the intended contract.
_LEGACY_QUESTION_PREFIX = {
    "which entity is": schema.SITUATION_UNRESOLVED_ENTITY,
    "where does": schema.SITUATION_UNSUPPORTED_TYPE,
}

DEFAULT_LIST_LIMIT = 50


def classify(row: dict) -> str:
    """Which entity situation this row is, or `""` when it is not one.

    Duck-typed over both row shapes this package sees — `queue.query_submissions`' listed row and
    `queue.get_submission_trace`'s trace — because both carry `status` and `report` and neither
    needs to know which one a caller happened to have.
    """
    if (row or {}).get("status") != schema.TRIAGE:
        return ""
    report = row.get("report") or {}
    declared = str(report.get(schema.SITUATION_KEY) or "")
    if declared in schema.SITUATIONS:
        return declared
    question = str(report.get("open_question") or "").strip().lower()
    for prefix, situation in _LEGACY_QUESTION_PREFIX.items():
        if question.startswith(prefix):
            return situation
    return ""


def subjects_of(row: dict) -> list[str]:
    """Every unresolved name this row carries, independently actionable (see
    `capture.schema.SITUATION_NAMES_KEY`'s own docstring for the storage argument).

    `schema.SITUATION_NAMES_KEY` (a list) is authoritative when present — a meeting park can carry
    several unresolved names at once, each later approvable independently. A SINGLE-name park
    (`report.triage_entity`) carries no such key and falls back to the singular
    `SITUATION_NAME_KEY` as a one-element list. `[]` for `unsupported-type`, which has no NAME to
    place — see `subject_of` for that situation's own subject (the judged type).
    """
    if classify(row) != schema.SITUATION_UNRESOLVED_ENTITY:
        return []
    report = row.get("report") or {}
    names = report.get(schema.SITUATION_NAMES_KEY)
    if isinstance(names, list) and names:
        return [str(n) for n in names if str(n).strip()]
    single = str(report.get(schema.SITUATION_NAME_KEY) or "").strip()
    return [single] if single else []


def subject_of(row: dict) -> str:
    """What the situation is ABOUT, in the vocabulary of its kind: the unresolved name(s), or the
    type the fast lane will not file. `""` when the row records neither — which a legacy row does,
    and which is answered honestly rather than by parsing it back out of a sentence.

    ONE display string. For a MULTI-name meeting park this joins every name with `", "`, so a
    caller that consumes a single string (the `list` column, `show`'s headline) still renders
    something true rather than going blank; a caller that must act on each name INDEPENDENTLY
    (`entities.cli._print_next_commands`) uses `subjects_of` instead.
    """
    report = row.get("report") or {}
    if classify(row) == schema.SITUATION_UNRESOLVED_ENTITY:
        names = subjects_of(row)
        return ", ".join(names) if names else str(report.get(schema.SITUATION_NAME_KEY) or "")
    return str(report.get(schema.SITUATION_TYPE_KEY) or "")


def _situation_view(row: dict) -> dict:
    """A listed row plus the facts this tool sorts and renders on. Additive: the row keeps
    every field `queue._shape_listed` gave it, so `--json` here and `stigmergy-queue list --json`
    describe one row the same way. `subjects` is the per-name list `subject` collapses to a
    single display string — see both functions' own docstrings."""
    return {**row, "situation": classify(row), "subject": subject_of(row),
           "subjects": subjects_of(row)}


# ── the two semantic entry points ─────────────────────────────────────────────────────────────
def list_pending_situations(conn, *, limit: int = DEFAULT_LIST_LIMIT) -> list[dict]:
    """The OPERATIONAL list: parked rows a steward can act on with `approve`/`reject`.

    Filtered in Python rather than in SQL, deliberately. The predicate is "which kind of park is
    this", which lives in a JSONB report key with a legacy fallback — expressing that as SQL would
    put a second, differently-worded copy of `classify` in a query string where no test can reach
    it, and the `statuses=[TRIAGE]` filter below already narrows the scan to the handful of rows a
    pilot queue parks. If the queue ever parks enough rows for this to matter, the fix is an index
    on the report key, not a duplicated predicate.
    """
    rows = queue.query_submissions(conn, statuses=[schema.TRIAGE], limit=limit)
    return [view for row in rows if (view := _situation_view(row))["situation"]]


def get_situation(conn, submission_id: int) -> dict | None:
    """One situation in full — the trace, with the material's excerpt and the agent's rationale.

    `queue.get_submission_trace` unscoped (`submitter=None`): this is a steward's tool and the
    steward is by definition looking at somebody else's capture. The no-existence-leak rule that
    governs the same read on the WIRE (`server.service`) is a property of the network path, not of
    a local CLI whose operator already holds the DSN.
    """
    trace = queue.get_submission_trace(conn, submission_id)
    return None if trace is None else _situation_view(trace)


# ── the write guard ───────────────────────────────────────────────────────────────────────────
def require_situation(conn, submission_id: int, *, action: str) -> dict:
    """Refuse before anything is written unless this row really is a pending entity situation.

    Three distinct refusals, because they have three different next actions: the row does not
    exist; it exists but is not parked (a worker holds it, or it is already closed); it is parked
    but is not an identity question. Collapsing them would make "somebody is working on it right
    now" read as "it is gone" — the same argument `queue._not_parked_reason` makes about the
    dispositions, applied one layer up where the consequence is a git commit rather than an UPDATE.

    This does NOT replace the queue's own guard. `dispositions.requeue`/`reject` still decide the
    transition's legality in SQL, under a race this read cannot see; this one exists so a steward
    is told what is wrong BEFORE a page is written and a commit is made, instead of after.
    """
    row = get_situation(conn, submission_id)
    if row is None:
        raise EntityError(f"submission {submission_id} does not exist — there is nothing for "
                          f"`{action}` to act on")
    if row["status"] != schema.TRIAGE:
        raise EntityError(
            f"submission {submission_id} is {row['status']!r}, and `{action}` acts only on a row "
            f"parked in {schema.TRIAGE!r} for a steward to place. Check `stigmergy-queue show "
            f"{submission_id}` for what happened to it")
    if not row["situation"]:
        raise EntityError(
            f"submission {submission_id} is parked in {schema.TRIAGE!r} but is not an entity "
            f"situation — it is not waiting on an identity decision, so minting an entity from it "
            f"would register something nothing asked for. Drain it with `stigmergy-queue "
            f"requeue/resolve/reject {submission_id}` instead")
    return row
