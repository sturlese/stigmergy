"""`review_decisions` — the append-only ledger answering "who approved this identity, and when".

**Not `dispositions.py`, which sits beside it.** That module moves a capture between queue STATES
(resolve, reject, requeue) and is about the material. This one records a governance DECISION about
an identity, and nothing here changes a row's status. They are next to each other because both are
written by the surfaces a human acts through; they answer different questions and share no state.

Below both writers on purpose: `stigmergy.entities` may not import `stigmergy.server`
(tests/test_architecture.py), so the only place all three minting doors can reach is here.
Moving it up breaks the CLI door silently — it would record nothing. See ADR 030.

**Append-only, and that is a property rather than a convention**: nothing here UPDATEs or DELETEs,
so a second decision on the same item is a second ROW. The history of a contested approval is the
point of the table.
"""
from psycopg.types.json import Jsonb

from stigmergy.capture.schema import startup_ddl_lock

# Here, not beside a writer: `stigmergy.entities` cannot import `stigmergy.server`, and a
# spelled-out verdict is one typo from a row no reader counts.
APPROVE, REJECT, REQUEST_CHANGES = "approve", "reject", "request_changes"
GENERIC_VERDICTS = (APPROVE, REJECT, REQUEST_CHANGES)

# WHICH DOOR recorded a verdict, for the same reason and in the same place as the verdicts: this
# table is append-only, so a door spelling itself `"console"` on Monday and `"admin"` on Tuesday
# leaves two permanent, unjoinable answers to "where did this decision come from". Closed on
# purpose — `record_decision` raises on anything else, because a wrong source is a bug in a door,
# never data a caller supplied.
SOURCE_MCP, SOURCE_SLACK, SOURCE_ADMIN, SOURCE_CLI = "mcp", "slack", "admin", "cli"
DECISION_SOURCES = (SOURCE_MCP, SOURCE_SLACK, SOURCE_ADMIN, SOURCE_CLI)

_REVIEW_DECISIONS_DDL = """
CREATE TABLE IF NOT EXISTS review_decisions (
    id BIGSERIAL PRIMARY KEY,
    item_kind TEXT NOT NULL,
    item_id TEXT NOT NULL,
    verdict TEXT NOT NULL,
    actor TEXT NOT NULL,
    notes TEXT NOT NULL DEFAULT '',
    extra JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""
_REVIEW_DECISIONS_INDEX = (
    "CREATE INDEX IF NOT EXISTS review_decisions_item_idx ON review_decisions (item_kind, item_id)"
)

_ALL_DDL = (_REVIEW_DECISIONS_DDL, _REVIEW_DECISIONS_INDEX)


def ensure_decisions_schema(conn) -> None:
    """Idempotent DDL for the ledger's one table, safe from two processes at once.

    Takes `schema.startup_ddl_lock` — the database's one DDL lock; see its docstring.
    """
    with startup_ddl_lock(conn) as cur:
        for statement in _ALL_DDL:
            cur.execute(statement)


def record_decision(conn, *, item_kind: str, item_id: str, verdict: str, actor: str, source: str,
                    notes: str = "", extra: dict | None = None) -> None:
    """The ONE write to the ledger, Postgres only.

    Called by every door that decides an identity, and by none that merely reads one. It carries no
    authorization of its own — deliberately, ADR 030 D2: each surface decides who may act by its
    own rules (a resolved MCP identity, an admin token, a steward's shell), and a permission check
    buried in the writer would be a fourth, invisible one that none of them could see or state.

    `source` names WHICH DOOR is recording, and is REQUIRED rather than defaulted: a default would
    be a lie on whichever door forgot to pass one, and this table cannot be corrected afterwards.
    It is validated against `DECISION_SOURCES` and raises `ValueError` — nothing a caller typed
    reaches here, so an unknown spelling is a bug in a door, not input to refuse politely.

    `extra` is the seam for per-kind detail, and stays that — an append-only table cannot be
    migrated later, so a field that turns out to be needed has nowhere else to go. It is merged in
    FIRST, so `source` is authoritative: a caller cannot override it through `extra`. The other
    direction let a door's own dict smuggle past the closed vocabulary above — bounced as an
    argument, stored as data — and a row naming a door nothing validated is permanent.
    """
    if source not in DECISION_SOURCES:
        raise ValueError(f"unknown decision source {source!r} (one of {', '.join(DECISION_SOURCES)})"
                         " — the ledger is append-only, so a door's own spelling cannot be "
                         "corrected after the fact")
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO review_decisions (item_kind, item_id, verdict, actor, notes, extra) "
            "VALUES (%s,%s,%s,%s,%s,%s)",
            (item_kind, item_id, verdict, actor, notes, Jsonb({**(extra or {}), "source": source})))


def latest_decisions(conn) -> dict[tuple[str, str], dict]:
    """The most recent decision per item — a rendering convenience, not a state machine.

    Here rather than beside its caller so every statement naming this table lives in the module
    that owns it: the append-only guarantee above is only checkable if there is one place to check.

    `source` is read out of `extra` and is `""` for every row written before the column existed —
    the ledger is never migrated, so those rows are permanent and every reader has to render one.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT ON (item_kind, item_id) item_kind, item_id, verdict, actor, "
            "extra->>'source', created_at FROM review_decisions "
            "ORDER BY item_kind, item_id, created_at DESC")
        return {(kind, item_id): {"verdict": verdict, "actor": actor, "source": source or "",
                                  "created_at": created_at}
                for kind, item_id, verdict, actor, source, created_at in cur.fetchall()}


_LATEST_FOR_ITEM = """
SELECT verdict, actor, extra->>'source', created_at FROM review_decisions
WHERE item_kind = %s AND item_id = %s
ORDER BY created_at DESC
LIMIT 1
"""


def latest_decision_for(conn, *, item_kind: str, item_id: str) -> dict | None:
    """The most recent decision on ONE item, in the same shape `latest_decisions` returns per item,
    or `None` if that item has never been decided.

    Not a convenience wrapper over it: `latest_decisions` is a `DISTINCT ON` over the WHOLE table,
    so asking it about a single item pays for every decision the ledger has ever held. This is the
    question `review_decisions_item_idx` exists for, and it is asked on a refusal path.

    `source` is `""` for the rows written before the column existed, exactly as above — the ledger
    is never migrated, so every reader has to render one.
    """
    with conn.cursor() as cur:
        cur.execute(_LATEST_FOR_ITEM, (item_kind, item_id))
        row = cur.fetchone()
    if row is None:
        return None
    verdict, actor, source, created_at = row
    return {"verdict": verdict, "actor": actor, "source": source or "", "created_at": created_at}
