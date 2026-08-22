"""The repair ledger's contract: the DDL and the vocabularies.

Idempotent DDL behind `capture.schema.startup_ddl_lock`, the shared advisory lock — the same
posture `gardener/schema.py` takes, and for the same reason: two processes may start at once.

**A repair is applied or it is not; nothing waits** — ADR 044,
`docs/decisions/044-the-capture-is-the-approval.md`.
The worker derives a repair from a gardener finding, validates it against a real checkout, applies
it through the nine gates and records what happened here. There is no pending state, no verdict
and nobody to ask — the three outcomes are `applied`, `failed` and `skipped`.

The one design decision worth stating here rather than in prose elsewhere: **`content_key` is
permanent, and it is what stops the loop repeating itself.** It identifies a repair by what it
would DO, not by which finding suggested it, so an applied repair a person later reverted in git
is never re-derived, and a repair a gate refused is not retried every night. That is a deliberate
trade: the loop forgets nothing, so a `failed` row is where an operator looks when a finding stops
being answered. A `skipped` row carries no key (nothing was derived to key on) and carries its
reason instead.
"""
import hashlib

from stigmergy.capture.schema import startup_ddl_lock

JOB_NAME = "repair"

# ── kind — what a proposal WOULD DO ──────────────────────────────────────────────────────────
# `edits` is the librarian's three additive declared-edit shapes, applied by `edits.apply_declared`
# and proven additive by `gate_body_rewrite`. `entity-body` is the one kind that REPLACES text, and
# it is a separate kind rather than a fourth op precisely because it is a different question for
# the gates: it may only touch a page in the entity zone, only below that page's own H1, and only
# with the apply telling `GateContext.body_rewrite_allowed` which path was authorized (ADR 039,
# "entity-body: the second kind").
#
# The string doubles as the OP name inside such a repair's single op, deliberately: one
# vocabulary word for one shape means a surface listing a repair's ops names what happened
# without a second lookup table.
#
# `delete` is the ONE kind that breaks that doubling, and deliberately: one repair performs two
# different actions — pages removed, pages rewritten to stop pointing at them — so its ops carry
# `delete-page`/`scrub-page` (`repair.deletion.OP_NAMES`) and a reader can tell which is which.
# A single word there would hide the half of the blast radius that is not the pages anybody
# named (ADR 039, "delete: the third kind").
#
# `entity-alias` is the fourth, and it breaks the doubling for the same reason `delete` does: one
# repair performs four different actions — the survivor's page gains the absorbed entity's
# spellings, the absorbed page is marked superseded, every page anchored to it is re-anchored, and
# the derived registry is rebuilt — so its ops carry their own names
# (`repair.entity_alias.OP_NAMES`) and a reader can tell which is which
# (ADR 039, "entity-alias: the fourth kind").
KIND_EDITS = "edits"
KIND_ENTITY_BODY = "entity-body"
KIND_DELETE = "delete"
KIND_ENTITY_ALIAS = "entity-alias"
KINDS = (KIND_EDITS, KIND_ENTITY_BODY, KIND_DELETE, KIND_ENTITY_ALIAS)

# ── status — what happened to a derived repair, and the whole vocabulary of it ────────────────
# `applied` landed a commit. `failed` was derived and refused — by its own validator, by a gate,
# or by a fault — and the `error` column says which; the row is the operator-facing record, and
# its key is not retried. `skipped` never became a repair at all: a finding no kind can express, a
# ceiling that bound, a model that declined. Only `skipped` carries a `reason`.
STATUS_APPLIED = "applied"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"
STATUSES = (STATUS_APPLIED, STATUS_FAILED, STATUS_SKIPPED)

# The CHECK constraints are the vocabularies above, spelled for SQL. `repr`, not
# `capture.schema.sql_literals`: that helper SORTS, and a CHECK's definition string is committed
# to databases in DECLARATION order — sorting would change it. Safe only because both
# vocabularies are lowercase identifiers with no quote or backslash.
_KIND_SQL_LIST = ", ".join(repr(k) for k in KINDS)
_STATUS_SQL_LIST = ", ".join(repr(s) for s in STATUSES)

_REPAIRS_DDL = f"""
CREATE TABLE IF NOT EXISTS repairs (
    id BIGSERIAL PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    run_id BIGINT NOT NULL DEFAULT 0,
    finding_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
    finding_subjects JSONB NOT NULL DEFAULT '[]'::jsonb,
    kind TEXT NOT NULL DEFAULT '{KIND_EDITS}' CHECK (kind IN ({_KIND_SQL_LIST})),
    target_paths JSONB NOT NULL DEFAULT '[]'::jsonb,
    ops JSONB NOT NULL DEFAULT '[]'::jsonb,
    rationale TEXT NOT NULL DEFAULT '',
    content_key TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL CHECK (status IN ({_STATUS_SQL_LIST})),
    applied_commit TEXT NOT NULL DEFAULT '',
    diff TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    model_id TEXT NOT NULL DEFAULT ''
)
"""

# The rename, and the reason it is a rename rather than a fresh table beside the old one: the rows
# in `repair_proposals` are the record of every repair this deployment ever landed, and a new table
# would leave that history in a table nothing reads. Runs BEFORE the `CREATE TABLE IF NOT EXISTS`
# above, so a database that has the old name arrives at the new one with its rows, and a fresh
# database skips it entirely.
_RENAME_FROM_PROPOSALS = """
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class WHERE relname = 'repair_proposals' AND relkind = 'r')
       AND NOT EXISTS (SELECT 1 FROM pg_class WHERE relname = 'repairs' AND relkind = 'r') THEN
        ALTER TABLE repair_proposals RENAME TO repairs;
    END IF;
END $$
"""

# `CREATE TABLE IF NOT EXISTS` never adds a column to a table that already exists, so every column
# this version introduced is added explicitly — a renamed `repair_proposals` has none of them.
_REPAIRS_COLUMNS = (
    "ALTER TABLE repairs ADD COLUMN IF NOT EXISTS finding_subjects JSONB NOT NULL "
    "DEFAULT '[]'::jsonb",
    "ALTER TABLE repairs ADD COLUMN IF NOT EXISTS diff TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE repairs ADD COLUMN IF NOT EXISTS reason TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE repairs ALTER COLUMN content_key SET DEFAULT ''",
)

# What the three retired statuses become. A row that was waiting on somebody when this shipped was
# never applied and never refused: it is a repair that did not happen, which is what `skipped`
# means. The reason says so rather than leaving a status a reader has to remember the history of.
# Idempotent by construction — after the first run no row matches.
_MIGRATE_RETIRED_STATUSES = f"""
UPDATE repairs
   SET status = '{STATUS_SKIPPED}',
       reason = CASE WHEN reason <> '' THEN reason
                     ELSE 'this repair was waiting on a person when repairs began applying '
                          'themselves (ADR 044); it was never applied' END
 WHERE status IN ('pending', 'approved', 'rejected')
"""

# The columns of the decision that no longer happens. Dropped rather than left empty: a column
# nothing writes is a column a reader assumes something writes.
_DROP_DECISION_COLUMNS = (
    "ALTER TABLE repairs DROP COLUMN IF EXISTS decided_by",
    "ALTER TABLE repairs DROP COLUMN IF EXISTS decided_at",
    "ALTER TABLE repairs DROP COLUMN IF EXISTS notes",
)

# ONE row per content key, over every status that has one — the permanent memory the module
# docstring describes. Partial, because a `skipped` row has no ops to key on and several of them
# would otherwise collide on the empty string.
_REPAIRS_CONTENT_KEY_INDEX = (
    "CREATE UNIQUE INDEX IF NOT EXISTS repairs_content_key_idx "
    "ON repairs (content_key) WHERE content_key <> ''"
)
# The old partial index, whose predicate names a status that no longer exists. Dropped by name:
# left in place it would keep enforcing uniqueness over rows nothing can create, and a reader
# would find an index for a lifecycle the code has forgotten.
_DROP_PENDING_KEY_INDEX = "DROP INDEX IF EXISTS repair_proposals_pending_key_idx"
_REPAIRS_STATUS_INDEX = (
    "CREATE INDEX IF NOT EXISTS repairs_status_idx ON repairs (status, id)"
)

# The name Postgres itself gives the inline column CHECKs above, spelled out so the swaps below can
# reach a constraint on a table created — or renamed — before this version.
KIND_CHECK_NAME = "repairs_kind_check"
STATUS_CHECK_NAME = "repairs_status_check"

# The two non-additive migrations, and `capture.schema`'s `_CAPTURE_QUEUE_STATUS_CHECK` verbatim in
# shape because the problem is identical: `CREATE TABLE IF NOT EXISTS` never touches a table that
# already exists, so a vocabulary changed here and nowhere else would be refused by every deployed
# database — an IntegrityError on the first row of the new shape, in production, at night.
#
# ONE `DO` statement each, never a DROP-then-ADD pair: as two statements the table is briefly
# unconstrained, every process start takes an ACCESS EXCLUSIVE lock, and two concurrent starters
# race into `DuplicateObject`. The guard skips the swap once the existing definition already names
# every value (`quote_literal`, so one name cannot match inside another).
#
# The renamed table brings `repair_proposals_kind_check`/`_status_check` with it, so each swap also
# drops the old name — otherwise a row would have to satisfy both the old vocabulary and the new.
def _check_swap(column: str, name: str, old_name: str, values: str) -> str:
    return f"""
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint c
        WHERE c.conrelid = 'repairs'::regclass
          AND c.conname = '{name}'
          AND (SELECT bool_and(pg_get_constraintdef(c.oid) LIKE '%' || quote_literal(v) || '%')
               FROM unnest(ARRAY[{values}]) AS v)
    ) THEN
        ALTER TABLE repairs DROP CONSTRAINT IF EXISTS {old_name};
        ALTER TABLE repairs DROP CONSTRAINT IF EXISTS {name};
        ALTER TABLE repairs ADD CONSTRAINT {name} CHECK ({column} IN ({values}));
    END IF;
END $$
"""


_REPAIRS_KIND_CHECK = _check_swap("kind", KIND_CHECK_NAME, "repair_proposals_kind_check",
                                  _KIND_SQL_LIST)
_REPAIRS_STATUS_CHECK = _check_swap("status", STATUS_CHECK_NAME, "repair_proposals_status_check",
                                    _STATUS_SQL_LIST)

# Order is load-bearing: rename, then create, then columns, then the status migration, THEN the
# CHECK swaps — a swap run before the migration would refuse the rows the migration exists to fix.
_ALL_DDL = (_RENAME_FROM_PROPOSALS, _REPAIRS_DDL, *_REPAIRS_COLUMNS, _MIGRATE_RETIRED_STATUSES,
            *_DROP_DECISION_COLUMNS, _REPAIRS_KIND_CHECK, _REPAIRS_STATUS_CHECK,
            _DROP_PENDING_KEY_INDEX, _REPAIRS_CONTENT_KEY_INDEX, _REPAIRS_STATUS_INDEX)


def ensure_repair_schema(conn) -> None:
    """Idempotent DDL for `repairs` — safe on every startup and from two processes at once (the
    shared `startup_ddl_lock`), and safe on a database that still has `repair_proposals`."""
    with startup_ddl_lock(conn) as cur:
        for statement in _ALL_DDL:
            cur.execute(statement)


# ── the op record: the stored shape of ONE edit, and what identifies a proposal ───────────────
# Pure string and dict work, with no import of its own beyond hashlib, because BOTH ends of the
# loop need it: the proposer (which loads a model stack) and `remote.apply_via_clone` (which the
# review lane calls inside the MCP server process, and which must not drag one in). Living here
# rather than in `proposer.py` is what keeps the server's import graph honest.
#
# The stored key is `op`, not `kind`, so a persisted proposal is never mistaken for a librarian
# outcome's `edits` entry by anything reading JSON — `declared_edits` is the ONE translation
# between the two vocabularies, and both validations run on its output, so propose time and apply
# time cannot come to judge different things.
OP_KIND_KEY = "op"
# The stored op shape, PER KIND — every reader of an op (the console's cleaner, the applier) is
# shaped by which of these it is looking at, so the shapes are named rather than left implicit in
# separate reshapes. `entity-body` carries PROSE where the additive kinds carry a link and a note,
# and a reader that assumed one shape for both rendered an empty cell where the draft should have
# been.
EDIT_OP_FIELDS = (OP_KIND_KEY, "path", "link", "note")
ENTITY_BODY_OP_FIELDS = (OP_KIND_KEY, "path", "body_markdown", "role")
# The `delete` kind's two op names and two shapes. A removal names only the page; a scrub carries
# the bytes it was computed FROM (so "the corpus moved" is a fact rather than a guess) and the bytes
# it would write (so the apply's recomputation has something to byte-compare against).
#
# The NAMES live here rather than in `repair.deletion` because `content_key` below has to tell one
# from the other, and this module is the bottom of this package: it imports nothing of its own.
DELETE_OP_NAME = "delete-page"
SCRUB_OP_NAME = "scrub-page"
DELETE_OP_FIELDS = (OP_KIND_KEY, "path")
SCRUB_OP_FIELDS = (OP_KIND_KEY, "path", "expected_before_hash", "planned_after")
# The GROUP, beside the names, `ALIAS_IDENTITY_OP_NAMES` below being the precedent: four surfaces
# have to know "every op this kind performs" — the kind's own validator, the console's op cleaner
# and the applier — and each rebuilding the tuple is how a fifth op reaches a reader rendered as
# something else. `tests/test_architecture.py` pins the console's tables against these two.
DELETE_OP_NAMES = (DELETE_OP_NAME, SCRUB_OP_NAME)

# The `entity-alias` kind's four op names, and its ONE op shape. Every op in a merge carries the
# whole file it would write, exactly as a scrub does and for the identical reason: what each one
# writes depends on every OTHER page in the corpus, so the apply recomputes the plan and
# byte-compares it. The names live here for `content_key`'s sake — the key below has to tell the
# two IDENTITY ops from the rest — and this module is the bottom of the package: it imports
# nothing of its own.
ALIAS_OP_NAME = "alias-survivor"
RETIRE_OP_NAME = "retire-absorbed"
REANCHOR_OP_NAME = "reanchor-page"
REGISTRY_OP_NAME = "regenerate-registry"
ALIAS_OP_FIELDS = (OP_KIND_KEY, "path", "expected_before_hash", "planned_after")
# All four as one group, `DELETE_OP_NAMES` above and for the same reason.
ALIAS_OP_NAMES = (ALIAS_OP_NAME, RETIRE_OP_NAME, REANCHOR_OP_NAME, REGISTRY_OP_NAME)
# The two ops that say WHICH pair this merge is about, as against the pages it also touches.
ALIAS_IDENTITY_OP_NAMES = (ALIAS_OP_NAME, RETIRE_OP_NAME)


def merge_direction(ops) -> dict:
    """`{"survivor", "absorbed", "reanchored"}` for an `entity-alias` proposal — `{}` for any other
    kind, since no other proposal has a direction.

    **Why a reader exists at all, rather than each surface reading `ops` itself.** For every
    other kind the paths say what happens to them: a `backlink` names the page that gains a link, a
    `delete` names the page that goes. A merge names two entity pages and the whole decision is
    WHICH ONE SURVIVES — and in `target_paths`, a sorted list, that is invisible. A reader would
    otherwise have only the model-authored `rationale`, whose text is derived from two page bodies
    somebody else wrote; the direction is the half code owns, and it has to be on the same screen.

    Derived from `ops` and never from `target_paths`: the cross-check judges one of those against
    the other, and a display built from the thing being judged would let one stored column vouch
    for its own consistency with the other.
    """
    by_name = {str(o.get(OP_KIND_KEY, "")): str(o.get("path", "")) for o in (ops or ())}
    survivor, absorbed = by_name.get(ALIAS_OP_NAME, ""), by_name.get(RETIRE_OP_NAME, "")
    if not (survivor and absorbed):
        return {}
    return {"survivor": survivor, "absorbed": absorbed,
            "reanchored": sum(1 for o in ops or ()
                              if str(o.get(OP_KIND_KEY, "")) == REANCHOR_OP_NAME)}


def declared_edits(ops) -> list[dict]:
    """Stored ops -> the `edits.validate`/`edits.apply_declared` declaration shape."""
    return [{"kind": str(o.get(OP_KIND_KEY, "")), "path": str(o.get("path", "")),
             "link": str(o.get("link", "")), "note": str(o.get("note", ""))}
            for o in (ops or ())]


def target_paths(ops) -> list[str]:
    """The pages a proposal would touch, deduplicated and sorted — the second stored fact
    `remote.apply_via_clone` cross-checks the produced diff against.

    An op that carries BOTH the bytes it was computed from and the bytes it would write, and whose
    two agree, names a page this proposal would NOT touch, and it is excluded. That case is real
    rather than theoretical: an `entity-alias` merge regenerates `ops/entity-registry.json` every
    time and the file usually comes out identical (the absorbed entity declared no alias to move),
    so a `target_paths` that named it would demand a diff entry git will never produce and the
    cross-check would refuse every such merge. Both keys are required before an op is skipped, so
    the kinds whose ops carry neither are unaffected.
    """
    return sorted({str(o.get("path", "")) for o in (ops or ())
                   if str(o.get("path", "")) and not _unchanged(o)})


def _unchanged(op) -> bool:
    before = str(op.get("expected_before_hash", ""))
    if not before or "planned_after" not in op:
        return False
    return hashlib.sha256(str(op.get("planned_after", "")).encode("utf-8")).hexdigest() == before


def page_set_key(paths) -> str:
    """The comparable spelling of a SET OF PAGES — sorted, deduplicated, hashed.

    Here rather than in either caller because both ends of the memory have to spell it the same
    way: the ledger writes what a repair stood for, and the derivation asks whether a finding is
    already answered. A key built two ways is two keys, and the failure it produces is silent —
    the loop re-answering something it answered last night.
    """
    return hashlib.sha256("|".join(sorted({str(p) for p in (paths or ()) if p}))
                          .encode()).hexdigest()


def content_key(ops, *, kind: str = KIND_EDITS) -> str:
    """What identifies a repair: WHAT IT DOES, never which finding suggested it.

    Order-independent (the op lines are sorted) so two runs that derive the same repair in a
    different order collide as they should, and `note` is deliberately EXCLUDED: two repairs that
    add the same callout to the same page with differently-worded sentences are the same
    question answered twice, and a repair already applied must not land again tomorrow with the
    sentence reworded.

    The same exclusion does the same work for the other kinds, and it matters more there: an
    `entity-body` op has no `link`, so its key is `kind + path` and the DRAFTED BODY is not part of
    it. **A re-drafted body is the same repair** — a page whose body this loop already wrote is not
    rewritten tomorrow with the prose rearranged.

    `delete` needs one step more than an exclusion, and `entity-alias` needs the same step for the
    same reason: their keys are built from the ops that ARE the question. A deletion's key is its
    DELETIONS alone — the scrubs are a fact about the rest of the corpus rather than about the
    question, so keying on them would re-derive a settled deletion every time somebody linked to
    the doomed page. A merge's key is its two IDENTITY ops alone, and which pages are anchored to the
    absorbed entity moves the same way.

    `entity-alias` goes one step further still, and it is the only kind that does: its key drops
    the OP NAME as well, so the two paths are keyed as an unordered PAIR. Which of two entities
    survives is the model's judgment and it may legitimately come out the other way tomorrow — but
    a merge that happened — or was refused — settled the PAIR, and a key that carried the direction
    would let the loop merge them back the other way the moment the answer flipped (#69's
    `finding_subjects` is the pre-model half of the same memory).
    """
    if kind == KIND_ENTITY_ALIAS:
        pair = sorted({str(o.get("path", "")) for o in (ops or ())
                       if str(o.get(OP_KIND_KEY, "")) in ALIAS_IDENTITY_OP_NAMES})
        return hashlib.sha256(f"{kind}|{'|'.join(pair)}".encode()).hexdigest()
    keyed = [o for o in (ops or ())
             if kind != KIND_DELETE or str(o.get(OP_KIND_KEY, "")) == DELETE_OP_NAME]
    body = "|".join(sorted(f"{o.get(OP_KIND_KEY, '')}:{o.get('path', '')}:{o.get('link', '')}"
                           for o in keyed))
    return hashlib.sha256(f"{kind}|{body}".encode()).hexdigest()
