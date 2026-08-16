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


def record_decision(conn, *, item_kind: str, item_id: str, verdict: str, actor: str,
                    notes: str = "", extra: dict | None = None) -> None:
    """The ONE write to the ledger, Postgres only.

    Called by every door that decides an identity, and by none that merely reads one. It carries no
    authorization of its own — deliberately, ADR 030 D2: each surface decides who may act by its
    own rules (a resolved MCP identity, an admin token, a steward's shell), and a permission check
    buried in the writer would be a fourth, invisible one that none of them could see or state.

    `extra` is the seam for per-kind detail. An append-only table cannot be migrated later, so a
    field that turns out to be needed has nowhere else to go.
    """
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO review_decisions (item_kind, item_id, verdict, actor, notes, extra) "
            "VALUES (%s,%s,%s,%s,%s,%s)",
            (item_kind, item_id, verdict, actor, notes, Jsonb(extra) if extra else None))


def latest_decisions(conn) -> dict[tuple[str, str], dict]:
    """The most recent decision per item — a rendering convenience, not a state machine.

    Here rather than beside its caller so every statement naming this table lives in the module
    that owns it: the append-only guarantee above is only checkable if there is one place to check.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT ON (item_kind, item_id) item_kind, item_id, verdict, actor, "
            "created_at FROM review_decisions ORDER BY item_kind, item_id, created_at DESC")
        return {(kind, item_id): {"verdict": verdict, "actor": actor, "created_at": created_at}
                for kind, item_id, verdict, actor, created_at in cur.fetchall()}
