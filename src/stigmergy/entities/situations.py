"""Which parked rows are an identity decision — the steward's operational view of `triage`.

One shared base, two semantic entry points, a write guard. Nothing here re-queries Postgres:
`queue.query_submissions` stays the single place that knows scope, paging, ordering and the
withheld-material rule; this module adds only "which parked rows are an ENTITY situation".
`stigmergy-entities list` is OPERATIONAL (only rows this tool can act on — a list showing rows
`approve` will refuse teaches its reader to ignore it), where `stigmergy-queue list` is
MANAGEMENT; the filter is a named function here, never re-derived in a CLI. The read is not the
permission: `require_situation` guards `approve`/`reject` before anything is written. What a mint
form may PREFILL is decided here too (`mint_name_prefill`), once, travels on the row
(`_situation_view`) and is read by both mint doors — a surface that re-derives it from the name
list is a second policy that can drift into minting a name nobody chose. Two kinds
of situation: `unresolved-entity` (the ask-back's terminus) and `unsupported-type` ("this is a
page about one specific person" is an identity claim). Legacy rows predating
`schema.SITUATION_KEY` are classified by an `open_question` prefix fallback — a transition,
deletable once no such `triage` row survives retention.
"""
from stigmergy.capture import queue, schema
from stigmergy.entities.errors import EntityError

# What `report.triage_entity` / `report.triage_type` write into `open_question`. Consulted ONLY
# for rows written before the situation code existed — a migration artifact, not the contract.
_LEGACY_QUESTION_PREFIX = {
    "which entity is": schema.SITUATION_UNRESOLVED_ENTITY,
    "where does": schema.SITUATION_UNSUPPORTED_TYPE,
}

DEFAULT_LIST_LIMIT = 50


def classify(row: dict) -> str:
    """Which entity situation this row is, or `""` when it is not one. Duck-typed over both row
    shapes this package sees (listed row and trace) — both carry `status` and `report`."""
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
    """Every unresolved name this row carries, independently actionable.

    `schema.SITUATION_NAMES_KEY` (a list) is authoritative when present, and is now the only key a
    park WRITES — an ordinary or meeting park carries every unresolved name, each approvable
    independently. The fallback to the singular `SITUATION_NAME_KEY` as a one-element list is
    PERMANENT and not a transition to finish: rows parked before the plural collapse keep the old
    key forever (they are never migrated), so deleting the fallback would silently blank a live
    steward's queue. `[]` for `unsupported-type`, which has no NAME to place (see `subject_of` for
    its subject, the judged type).
    """
    if classify(row) != schema.SITUATION_UNRESOLVED_ENTITY:
        return []
    report = row.get("report") or {}
    names = report.get(schema.SITUATION_NAMES_KEY)
    if isinstance(names, list) and names:
        return [str(n) for n in names if str(n).strip()]
    single = str(report.get(schema.SITUATION_NAME_KEY) or "").strip()
    return [single] if single else []


def mint_name_prefill(row: dict) -> str:
    """The ONE name a mint form may offer as its default, or `""` when no single string can be it.

    Submitting either mint door — the Slack modal, the admin console's Approve form — writes one
    entity as one signed commit, so the prefill is not a convenience: it is the value most stewards
    submit unchanged. Exactly one unresolved name is the only case where a default can be right.
    With several, `subject_of`'s joined display string is not any of their real names and each of
    them is only one accepted default away from a garbled entity in the knowledge repo, so the
    field is left EMPTY and the surfaces list the names instead; with none there is nothing to
    offer. A surface renders that listing when this answers `""` and names exist — it never counts
    again, or the rule is back to being decided per door.

    `schema.UNNAMED_ENTITY_PLACEHOLDER` is refused BY VALUE, the same rule and the same reason as
    `entities.cli._suggestable`: it is the librarian's own fallback word for a park that named
    nothing, syntactically an ordinary name, and a mint door is ONE unchanged click from signing it
    into the registry — where it would then resolve for every future capture containing that
    phrase. Refused here rather than at each door, because a door that forgot the check is a door
    that mints it.

    What this makes identical across the doors is the DECISION — whether a default is offered at
    all, and which name it is. Not the bytes: each transport still sanitizes what it renders on its
    own terms (the admin console strips control characters, Slack and MCP do not), so a ragged name
    reaches two forms differently. What that can no longer do is mint anything:
    `birth._refuse_control_characters` refuses C0/C1 at the terminal gate every door passes
    through, so a Slack steward accepting a ragged default gets a refusal naming the code point
    rather than a page whose filename nobody can type.

    Deliberately NOT tidied on the way out: `subjects_of` returns the plural key's entries
    unstripped, and nothing de-duplicates them, so `["  Jack  "]` prefills with its padding and
    `["Jack", "Jack"]` is a two-name park with an empty field. Both are pinned in
    `tests/entities/test_situations.py`; changing either changes what gets minted, which is a
    decision and not a consolidation. The placeholder check is the one exception, and it compares
    the STRIPPED value, so the padding cannot smuggle it past.
    """
    names = subjects_of(row)
    if len(names) != 1 or names[0].strip() == schema.UNNAMED_ENTITY_PLACEHOLDER:
        return ""
    return names[0]


def subject_of(row: dict) -> str:
    """What the situation is ABOUT: the unresolved name(s), or the type the fast lane will not
    file; `""` when the row records neither (a legacy row), answered honestly rather than parsed
    back out of a sentence. ONE display string — a multi-name park joins names with `", "` so a
    single-string consumer still renders something true; a caller acting on each name
    independently uses `subjects_of`.
    """
    report = row.get("report") or {}
    if classify(row) == schema.SITUATION_UNRESOLVED_ENTITY:
        names = subjects_of(row)
        return ", ".join(names) if names else str(report.get(schema.SITUATION_NAME_KEY) or "")
    return str(report.get(schema.SITUATION_TYPE_KEY) or "")


def _situation_view(row: dict) -> dict:
    """A listed row plus the facts this tool sorts and renders on. Additive: the row keeps every
    field `queue._shape_listed` gave it, so the two tools' `--json` describe one row the same
    way.

    `mint_name_prefill` travels HERE, beside `subject`/`subjects`, rather than being added by hand
    on each surface: a shaper that computes it itself decides it on whatever row it happens to
    hold, and two surfaces preprocessing that row differently is how one park ends up with two
    default names for a mint nothing can cancel.
    """
    return {**row, "situation": classify(row), "subject": subject_of(row),
           "subjects": subjects_of(row), "mint_name_prefill": mint_name_prefill(row)}


# ── the two semantic entry points ─────────────────────────────────────────────────────────────
def list_pending_situations(conn, *, limit: int = DEFAULT_LIST_LIMIT) -> list[dict]:
    """The OPERATIONAL list: parked rows a steward can act on with `approve`/`reject`.

    Filtered in Python deliberately: the predicate lives in a JSONB key with a legacy fallback,
    and SQL would be a second, differently-worded copy of `classify`; `statuses=[TRIAGE]` already
    narrows the scan. If volume ever matters, the fix is an index, not a duplicated predicate.
    """
    rows = queue.query_submissions(conn, statuses=[schema.TRIAGE], limit=limit)
    return [view for row in rows if (view := _situation_view(row))["situation"]]


def get_situation(conn, submission_id: int) -> dict | None:
    """One situation in full — the trace, with the material's excerpt and the agent's rationale.

    Unscoped (`submitter=None`): a steward is by definition reading somebody else's capture. The
    wire's no-existence-leak rule is a property of the network path, not of a local CLI whose
    operator already holds the DSN.
    """
    trace = queue.get_submission_trace(conn, submission_id)
    return None if trace is None else _situation_view(trace)


# ── the write guard ───────────────────────────────────────────────────────────────────────────
def require_situation(conn, submission_id: int, *, action: str) -> dict:
    """Refuse before anything is written unless this row really is a pending entity situation.

    Three distinct refusals with three different next actions: missing; not parked (a worker
    holds it, or it is closed); parked but not an identity question. This does NOT replace the
    queue's own guard — `dispositions.requeue`/`reject` still decide legality in SQL, under a
    race this read cannot see; this exists so a steward learns what is wrong BEFORE a page is
    written and a commit is made.
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
