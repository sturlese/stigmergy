"""The proposal store's contract: the DDL and the vocabularies.

Idempotent DDL behind `capture.schema.startup_ddl_lock`, the shared advisory lock — the same
posture `gardener/schema.py` takes, and for the same reason: two processes may start at once.

The one design decision worth stating here rather than in prose elsewhere: **a REJECTED row is
the dismissal memory.** `content_key` identifies a proposal by what it would DO, not by which
finding suggested it, and the proposer skips a key held by a pending, approved, rejected or
applied row. "Reviewed and declined" therefore finally exists as a durable fact, and a steward who
says no once is not asked the same question again the next night. The UNIQUE index is narrower on
purpose (pending only): re-proposing after a rejection is a decision a human makes, and the index
must not turn it into a database error.

`failed` is the one status the memory does NOT hold, and the asymmetry is the point: a rejection is
a human saying no, while a failed apply is a human having said YES to something that then hit a
gate, a race or a fault. The row stays as the operator-visible record; the SKIP does not, or the
one repair a steward actively wanted would be the one the loop can never offer again.
"""
import hashlib

from stigmergy.capture.schema import startup_ddl_lock

JOB_NAME = "repair-propose"

# ── kind — what a proposal WOULD DO ──────────────────────────────────────────────────────────
# `edits` is the librarian's three additive declared-edit shapes, applied by `edits.apply_declared`
# and proven additive by `gate_body_rewrite`. `entity-body` is the one kind that REPLACES text, and
# it is a separate kind rather than a fourth op precisely because it is a different question for
# the gates: it may only touch a page in the entity zone, only below that page's own H1, and only
# with the apply telling `GateContext.body_rewrite_allowed` which path was authorized (ADR 039,
# "entity-body: the second kind").
#
# The string doubles as the OP name inside such a proposal's single op, deliberately: one
# vocabulary word for one shape means `ops_preview.kinds` in the review lane names the thing a
# steward is being asked about without a second lookup table.
#
# `delete` is the ONE kind that breaks that doubling, and deliberately: one approval performs two
# different actions — pages removed, pages rewritten to stop pointing at them — so its ops carry
# `delete-page`/`scrub-page` (`repair.deletion.OP_NAMES`) and `ops_preview.kinds` tells a steward
# which is which. A single word there would hide the half of the blast radius that is not the
# pages they named (ADR 039, "delete: the third kind").
#
# `entity-alias` is the fourth, and it breaks the doubling for the same reason `delete` does: one
# approval performs four different actions — the survivor's page gains the absorbed entity's
# spellings, the absorbed page is marked superseded, every page anchored to it is re-anchored, and
# the derived registry is rebuilt — so its ops carry their own names
# (`repair.entity_alias.OP_NAMES`) and `ops_preview.kinds` tells a steward which is which
# (ADR 039, "entity-alias: the fourth kind").
KIND_EDITS = "edits"
KIND_ENTITY_BODY = "entity-body"
KIND_DELETE = "delete"
KIND_ENTITY_ALIAS = "entity-alias"
KINDS = (KIND_EDITS, KIND_ENTITY_BODY, KIND_DELETE, KIND_ENTITY_ALIAS)

# ── status — the lifecycle. `failed` is terminal for the ROW, never for the finding: a steward
# may propose again, and the `error` column says what went wrong. An approved proposal whose
# apply failed does NOT revert to pending; a silent revert would hide that a gate refused. ─────
STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
STATUS_APPLIED = "applied"
STATUS_FAILED = "failed"
STATUSES = (STATUS_PENDING, STATUS_APPROVED, STATUS_REJECTED, STATUS_APPLIED, STATUS_FAILED)

# The two verdicts a steward may return. `applied`/`failed` are outcomes code writes, never
# something a human hands in.
DECIDABLE = (STATUS_APPROVED, STATUS_REJECTED)

# The CHECK constraints are the vocabularies above, spelled for SQL. `repr`, not
# `capture.schema.sql_literals`: that helper SORTS, and a CHECK's definition string is committed
# to databases in DECLARATION order — sorting would change it. Safe only because both
# vocabularies are lowercase identifiers with no quote or backslash.
_KIND_SQL_LIST = ", ".join(repr(k) for k in KINDS)
_STATUS_SQL_LIST = ", ".join(repr(s) for s in STATUSES)

_REPAIR_PROPOSALS_DDL = f"""
CREATE TABLE IF NOT EXISTS repair_proposals (
    id BIGSERIAL PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    run_id BIGINT NOT NULL DEFAULT 0,
    finding_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
    kind TEXT NOT NULL DEFAULT '{KIND_EDITS}' CHECK (kind IN ({_KIND_SQL_LIST})),
    target_paths JSONB NOT NULL,
    ops JSONB NOT NULL,
    rationale TEXT NOT NULL DEFAULT '',
    content_key TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT '{STATUS_PENDING}' CHECK (status IN ({_STATUS_SQL_LIST})),
    decided_by TEXT NOT NULL DEFAULT '',
    decided_at TIMESTAMPTZ,
    notes TEXT NOT NULL DEFAULT '',
    applied_commit TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    model_id TEXT NOT NULL DEFAULT '',
    finding_subjects JSONB NOT NULL DEFAULT '[]'::jsonb
)
"""

# WHAT THE FINDINGS NAMED, as against `target_paths` (what the answer would EDIT). The two are
# routinely different — an `orphan-page` finding names the page nothing links to, and the repair
# edits the page that ought to link to it — so a dismissal memory keyed only on `target_paths`
# recognised neither that shape nor a one-sided answer to a two-page finding, and sent the same
# declined repair to the model every night under each new finding id.
#
# A LIST OF LISTS, one entry per finding answered, and never their union: a proposal answering two
# findings has to dismiss BOTH, while a union would dismiss only a hypothetical third finding
# naming all of those pages at once, which is not a finding anything produces.
#
# `ADD COLUMN IF NOT EXISTS` for the reason `gardener/schema.py` states: `CREATE TABLE IF NOT
# EXISTS` never adds a column to a table that already exists, and the `'[]'` default fills every
# pre-existing row — a proposal stored before this column reads as "named no subject", which falls
# back to the `target_paths` half exactly as it did before.
_REPAIR_PROPOSALS_FINDING_SUBJECTS_COLUMN = (
    "ALTER TABLE repair_proposals ADD COLUMN IF NOT EXISTS finding_subjects JSONB NOT NULL "
    "DEFAULT '[]'::jsonb"
)

# One PENDING proposal per content key — a second propose run that re-derives the same edit is a
# no-op, not a second question. Deliberately NOT unique across every status: the dismissal memory
# is enforced in `proposer.py` (which skips a key with any prior row) so a human who decides to
# re-propose after a rejection meets a decision, never a constraint violation.
_REPAIR_PROPOSALS_PENDING_KEY_INDEX = (
    f"CREATE UNIQUE INDEX IF NOT EXISTS repair_proposals_pending_key_idx "
    f"ON repair_proposals (content_key) WHERE status IN ('{STATUS_PENDING}')"
)
_REPAIR_PROPOSALS_STATUS_INDEX = (
    "CREATE INDEX IF NOT EXISTS repair_proposals_status_idx ON repair_proposals (status, id)"
)

# The name Postgres itself gives the inline column CHECK above, spelled out so the swap below can
# reach the constraint on a table created before this constant existed.
KIND_CHECK_NAME = "repair_proposals_kind_check"

# The one non-additive migration, and `capture.schema`'s `_CAPTURE_QUEUE_STATUS_CHECK` verbatim in
# shape because the problem is identical: `CREATE TABLE IF NOT EXISTS` never touches a table that
# already exists, so a KIND added to `KINDS` and to nothing else would be refused by every deployed
# database — an IntegrityError on the first proposal of the new kind, in production, at night.
#
# ONE `DO` statement, never a DROP-then-ADD pair: as two statements the table is briefly
# unconstrained, every process start takes an ACCESS EXCLUSIVE lock, and two concurrent starters
# race into `DuplicateObject`. The guard skips the swap once the existing definition already names
# every kind (`quote_literal`, so one name cannot match inside another).
_REPAIR_PROPOSALS_KIND_CHECK = f"""
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint c
        WHERE c.conrelid = 'repair_proposals'::regclass
          AND c.conname = '{KIND_CHECK_NAME}'
          AND (SELECT bool_and(pg_get_constraintdef(c.oid) LIKE '%' || quote_literal(k) || '%')
               FROM unnest(ARRAY[{_KIND_SQL_LIST}]) AS k)
    ) THEN
        ALTER TABLE repair_proposals DROP CONSTRAINT IF EXISTS {KIND_CHECK_NAME};
        ALTER TABLE repair_proposals ADD CONSTRAINT {KIND_CHECK_NAME}
            CHECK (kind IN ({_KIND_SQL_LIST}));
    END IF;
END $$
"""

_ALL_DDL = (_REPAIR_PROPOSALS_DDL, _REPAIR_PROPOSALS_FINDING_SUBJECTS_COLUMN,
            _REPAIR_PROPOSALS_KIND_CHECK, _REPAIR_PROPOSALS_PENDING_KEY_INDEX,
            _REPAIR_PROPOSALS_STATUS_INDEX)


def ensure_repair_schema(conn) -> None:
    """Idempotent DDL for `repair_proposals` — safe on every startup and from two processes at
    once (the shared `startup_ddl_lock`)."""
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
# The stored op shape, PER KIND — every reader of an op (the CLI preview, the console's cleaner,
# the review lane's `ops_preview`) is shaped by which of these it is looking at, so the two are
# named rather than left implicit in four separate reshapes. `entity-body` carries PROSE where the
# additive kinds carry a link and a note, and a reader that assumed one shape for both showed a
# steward an empty cell where the draft should have been.
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
# The two ops that say WHICH pair this merge is about — the question a steward answers, as against
# the pages the answer would also touch.
ALIAS_IDENTITY_OP_NAMES = (ALIAS_OP_NAME, RETIRE_OP_NAME)


def merge_direction(ops) -> dict:
    """`{"survivor", "absorbed", "reanchored"}` for an `entity-alias` proposal — `{}` for any other
    kind, since no other proposal has a direction.

    **Why a reader exists at all, rather than a review surface reading `ops` itself.** For every
    other kind the paths say what happens to them: a `backlink` names the page that gains a link, a
    `delete` names the page that goes. A merge names two entity pages and the whole decision is
    WHICH ONE SURVIVES — and in `target_paths`, a sorted list, that is invisible. A steward
    approving from the review lane would otherwise read only the model-authored `rationale`, whose
    text is derived from two page bodies somebody else wrote; the direction is the half code owns
    and it has to be on the same screen.

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


def content_key(ops, *, kind: str = KIND_EDITS) -> str:
    """What identifies a proposal: WHAT IT WOULD DO, never which finding suggested it.

    Order-independent (the op lines are sorted) so two runs that derive the same repair in a
    different order collide as they should, and `note` is deliberately EXCLUDED: two proposals
    that add the same callout to the same page with differently-worded sentences are the same
    question asked twice, and a steward who declined it once should not meet a rephrasing of it
    tomorrow.

    The same exclusion does the same work for the other kinds, and it matters more there: an
    `entity-body` op has no `link`, so its key is `kind + path` and the DRAFTED BODY is not part of
    it. **A re-drafted body is the same question** — a steward who read a draft for a page and
    decided it needs writing by a person is not asked again tomorrow with the prose rearranged.

    `delete` needs one step more than an exclusion, and `entity-alias` needs the same step for the
    same reason: their keys are built from the ops that ARE the question. A deletion's key is its
    DELETIONS alone — the scrubs are a fact about the rest of the corpus rather than about the
    question, so keying on them would re-ask a declined deletion every time somebody linked to the
    doomed page. A merge's key is its two IDENTITY ops alone, and which pages are anchored to the
    absorbed entity moves the same way.

    `entity-alias` goes one step further still, and it is the only kind that does: its key drops
    the OP NAME as well, so the two paths are keyed as an unordered PAIR. Which of two entities
    survives is the model's judgment and it may legitimately come out the other way tomorrow — but
    a steward who declined the merge declined the PAIR, and a key that carried the direction would
    ask them again the moment the answer flipped (#69's `finding_subjects` for the pre-model half
    of the same memory).
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
