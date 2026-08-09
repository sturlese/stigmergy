"""The capture queue's contract: the durable DDL, the status machine, and the payload/hints
shape a submission takes.

**DDL ownership, not a migration framework.** This module owns its four tables the same way
`stigmergy.index.store` owns the index DDL and
`stigmergy.server.audit.ensure_audit_table` owns `audit_log`: one idempotent
`CREATE TABLE IF NOT EXISTS` run at startup by whoever opens the database. Four tables do not
justify a migration tool, and introducing one would put a second, competing notion of "who owns
the schema" beside the one the repo already has.

**The queue is the durable half of a database whose other half is a cache.** `init_schema` in
`stigmergy.index.store` drops `pages_index` BY NAME on every rebuild — targeted on purpose. The
tables here must survive that, so `DURABLE_TABLES` names them explicitly: it is what a test
asserts against after a rebuild, and what any future "just rebuild the schema" shortcut has to
reckon with.

**The status enum is written here in full, including the states this module never sets itself.**
`queued`, `claimed` and `failed` are the transitions the queue performs; `filed`, `rejected`,
`resolved`, `needs_input` and `triage` are the contract the librarian fills. They are declared
here because the librarian cannot be built or tested against a queue whose vocabulary is still
being invented, and because a `CHECK` constraint that has to be migrated later is worse than one
written once — see `_CAPTURE_QUEUE_STATUS_CHECK` for the guarded, single-statement replacement
that lands a new status on a table that already exists without ever leaving it unconstrained.

**Nothing in a payload can set a server-computed field.** Two independent mechanisms,
deliberately: (1) `submitted_by` is taken from the resolved caller identity by the
service layer and is never read from client input at all — this is the structural guarantee;
(2) `reject_server_owned_arguments` / `normalize_hints` refuse a server-owned field arriving as
an argument or a hint, and `declared_frontmatter` records what a pre-drafted page DECLARES as a
flagged hint. (2) is annotation and loud refusal; the security property rests on (1), which is
why a best-effort frontmatter scan is enough (see `declared_frontmatter`).
"""
import hashlib
import logging
import re
from contextlib import contextmanager
from dataclasses import dataclass

from stigmergy.capture.errors import ReplyRejected, SubmissionRejected

log = logging.getLogger(__name__)

# ── the queue's vocabulary ────────────────────────────────────────────────────────────────────
QUEUED = "queued"
CLAIMED = "claimed"
FILED = "filed"
REJECTED = "rejected"
# "A steward handled this outside the fast lane". Its own terminal state rather than a reuse of
# `rejected`: telling a submitter their work was REFUSED when it was in fact filed by hand is a
# lie, and honest states are this project's UX doctrine. `rejected` keeps its meaning — a steward
# declining a capture reuses it, with the actor and the reason recorded (attribution, not
# authorization).
RESOLVED = "resolved"
NEEDS_INPUT = "needs_input"
TRIAGE = "triage"
FAILED = "failed"

STATUSES = (QUEUED, CLAIMED, FILED, REJECTED, RESOLVED, NEEDS_INPUT, TRIAGE, FAILED)

# Terminal = the row is done and will not move again on its own. `needs_input` and `triage` are
# deliberately NOT terminal: they are parked awaiting a human (an ask-back answer, a steward's
# placement decision), and retention must not delete the material a human is about to be asked
# about. Retention purges TERMINAL rows only — and `resolved` sits on the ORDINARY window,
# because a steward-handled row is done in exactly the sense retention means.
TERMINAL_STATUSES = frozenset({FILED, REJECTED, RESOLVED, FAILED})

# The two states a human is being waited on in, and the only two a steward disposition may move a
# row OUT of. Named once, here, rather than re-listed at each surface: the queue's
# `dispose` guard, the CLI's refusal sentence, the parked-age computation and the "who is being
# waited on" rendering all ask the same question, and a second spelling of this pair is how one of
# them ends up admitting a `claimed` row.
PARKED_STATUSES = frozenset({NEEDS_INPUT, TRIAGE})

# States a claim can be finished into (`finish()`); the parked pair keep `finished_at` NULL.
#
# **`resolved` is deliberately absent.** It is a steward's disposition on a row nobody holds a lease
# on, so it must not be reachable through the lease-fenced transition at all: `finish()` requires an
# `expected_attempts` fence, and a steward path that had to supply one would be faking a lease it
# does not hold. It has its own guarded transition — `queue.dispose` — which refuses a
# `claimed` row instead of pretending to own it.
FINISHED_STATUSES = frozenset({FILED, REJECTED, FAILED}) | PARKED_STATUSES

# ── why a refused row is where it is, as a CODE beside the sentence ───────────────────────────
# `error` and `report["summary"]` carry the sentence a person reads; these are its machine-readable
# half, written by `librarian.report` and read by the queue's list surface.
#
# They exist because the read path has to settle exactly one question about a refused row — **may
# this row's captured material be echoed back** — and until now there was nothing structured to
# settle it on. `report["stage"]` is written by `report.failed_system` and by nothing else, so a
# secrets refusal carried no signal at all (verified on a real walk row), and matching on the
# summary's prose would make a confidentiality property depend on wording an edit can change.
#
# Declared HERE, in the queue's contract module, for the same reason the status enum is: the
# librarian fills this vocabulary and the queue's read path consumes it, and `capture` may not
# import `librarian` (`tests/test_architecture.py`). Same posture, one level down.
REASON_SECRET = "secret"
REASON_PII = "pii"
REASON_DUPLICATE = "duplicate"
REASON_STEERING = "steering"
# A steward declined the capture by hand. It exists for the same reason the other five do —
# `queue._MATERIAL_WITHHELD` fails CLOSED on a `rejected` row carrying NO reason code, so a steward
# rejection without one would silently suppress the submitter's own excerpt and hints. It is not in
# `WITHHELD_REASONS`: a steward's judgment call says nothing about whether the material is safe to
# read back, and it is the submitter's own writing.
REASON_STEWARD = "steward"
# A drafted page whose frontmatter could not be turned into valid YAML after server-owned fields
# were stripped and restamped — content-caused (almost always a
# list-shaped field, e.g. `entity:`, whose continuation lines were not indented under its key the
# way this repo's line-based dialect expects), not a librarian bug. Used to route to `failed`
# ("the librarian broke") because `gate_frontmatter`'s `unparseable` finding carries no signal
# distinguishing "the capture's own shape" from "a stamping bug" — this reason code is that signal.
REASON_MALFORMED_FRONTMATTER = "malformed-frontmatter"

REJECTION_REASONS = (REASON_SECRET, REASON_PII, REASON_DUPLICATE,
                     REASON_STEERING, REASON_STEWARD, REASON_MALFORMED_FRONTMATTER)
# The key the code travels under inside the `report` JSONB column.
REASON_CODE_KEY = "reason_code"

# ── which KIND of parked situation a `triage` row is ──────────────────────────────────────────
# The same doctrine as `REASON_CODE_KEY` one block up, applied to the other parked state: the
# summary sentence is written for a person, and a READ path that has to BRANCH on which kind of
# park this is may only branch on a code. `stigmergy-entities list` is that read path — it selects
# the rows a steward can act on with `approve`, and selecting them by matching the prose of
# `report.triage_entity`'s `open_question` would make governance tooling depend on wording an
# edit is entitled to change.
#
# Declared here rather than in `librarian.agent` (where the AGENT's identical `triage.kind`
# vocabulary lives) for the reason the status enum and the reason codes are: `librarian` writes
# these values, `capture`'s read path and `entities` consume them, and neither of those two may
# import `librarian`. The strings are deliberately IDENTICAL to `agent.TRIAGE_KINDS` — one
# vocabulary, travelling from the agent's declaration through code's routing into the row.
SITUATION_UNRESOLVED_ENTITY = "unresolved-entity"
SITUATION_UNSUPPORTED_TYPE = "unsupported-type"
SITUATIONS = (SITUATION_UNRESOLVED_ENTITY, SITUATION_UNSUPPORTED_TYPE)
SITUATION_KEY = "situation"

# The two facts a steward needs BESIDE the sentence, for the same reason the code sits beside the
# refusal: `entities show` names the unresolved name and the judged type, and reading them back out
# of a prose summary is parsing, not reading.
SITUATION_NAME_KEY = "entity_name"      # `unresolved-entity`: the name nothing registers
SITUATION_TYPE_KEY = "judged_type"      # `unsupported-type`: the type the fast lane will not file

# `SITUATION_NAME_KEY` was built for exactly ONE unresolved name, and every reader of it —
# `entities.situations`, `stigmergy-entities show`'s `_print_next_commands`,
# `doorbell_entity_proposal` — is single-name shaped. A parked MEETING can carry several (a call
# naming two customers and an unregistered project code). The resolution, recorded once for the
# readers that need to know it:
#
# `SITUATION_NAME_KEY` keeps its single-string contract UNCHANGED — every existing writer and
# reader is untouched, and a non-meeting park still carries exactly one name under this key.
# `SITUATION_NAMES_KEY` is a NEW, ADDITIVE key: a JSON-encodable list of names, written ONLY when a
# parked capture has more than one unresolved name (`report.triage_entity_multi`). When present, it
# is authoritative and a reader must iterate it INDEPENDENTLY per name — `entities.cli.
# _print_next_commands` checks and prints (or refuses) each name on its own, so a steward approving
# one name is never blocked because a second one also happens to fail `_suggestable`. When absent,
# a reader falls back to the singular `SITUATION_NAME_KEY`, which is what keeps every existing,
# single-name row (and every non-meeting flow, forever) working unchanged. Two keys rather than
# widening the one key to "always a list" specifically to avoid a migration: every row already
# written, and every row `triage_entity` (singular) writes, still parses as a single string with
# no reader-side branch on shape.
SITUATION_NAMES_KEY = "entity_names"

# The fallback word for "nothing was named at all" — an agent-declared park with no name
# (`processing._triage`) and a `gate_anchoring` veto that names no locator
# (`gates._unresolved_name`) both spell it identically, one word for one situation rather than two.
# Shared here specifically so `entities.cli._suggestable` can refuse it BY
# VALUE: it is syntactically a perfectly ordinary name (letters and a space), so the allow-list that
# guards a printed `--name` argument would otherwise happily suggest `stigmergy-entities approve ...
# --name "something unnamed"` — a ready-to-run command that mints a garbage entity nobody meant to
# register, which then resolves for every future capture that happens to mention it. The park
# itself stays correct — a capture with nothing to anchor to still must not be silently filed;
# only the CLI's willingness to hand this ONE value back as a fillable suggestion changes.
UNNAMED_ENTITY_PLACEHOLDER = "something unnamed"

# The refusals whose whole point is that a value must not travel. A row refused for either one
# carries captured material that the system has already declared unsafe to hand back, so the read
# path serves NEITHER the excerpt NOR the submitter's own hint values for it — not redacted, not
# truncated around the match, suppressed. gitleaks reports a rule and a line, not a guaranteed
# span, so a redaction here would be a guess presented as a guarantee.
WITHHELD_REASONS = frozenset({REASON_SECRET, REASON_PII})

# What a read surface says in the excerpt's place. One sentence, in one place, rendered by every
# surface that lists submissions (`brain_submissions`, `stigmergy-queue list`) — the same discipline
# `librarian.report` applies to a terminal outcome's wording, for the same reason: two surfaces
# describing one row must not drift into saying different things about it.
WITHHELD_MATERIAL_NOTE = (
    "withheld — this capture was refused as a secrets or personal-data match, so its own text and "
    "hint values are never echoed back; this row's refusal names what matched and where")

# ── the queue's ECHO WINDOW, closed for a different reason ─────────────────────────────────────
# A row sitting in `queued` or `claimed` would echo its material in full through
# `brain_submissions` and `stigmergy-queue list`, and nothing has scanned it for a secret or
# personal data yet — the gate runs at the FIRST step of `librarian.processing.process_item`,
# which only starts once a row LEAVES `claimed`. The window is therefore keyed on "has the gate
# run", not on `TERMINAL_STATUSES`: keyed on the latter it would keep withholding through
# `needs_input`/`triage` — exactly the two states where a submitter must read what they sent to
# answer the librarian's question, or a steward must to triage it. So the excerpt is withheld
# while `status IN ('queued', 'claimed')` and shown in every other state EXCEPT one more:
#
# `failed` is an ACCEPTED RESIDUAL, not an oversight. A run that fails before it reaches the gate
# (a crash, a config fault, exhausted delivery attempts) leaves material the gate never looked at,
# and unlike `queued`/`claimed` there is no automatic next pass coming — `queue.dispose` only
# reaches a PARKED row (`needs_input`/`triage`), never a `failed` one, so nothing will ever look at
# it on its own. It is withheld too, with its OWN sentence that says so plainly (recorded, not
# silently folded into the pending case, which would falsely promise it will "appear as soon as
# the librarian has looked at it" — it will not, without an operator's help).
#
# This is a THIRD, DISTINCT reason from `WITHHELD_MATERIAL_NOTE` above, and it must never reuse
# that sentence: telling the submitter of an ordinary, unscanned, possibly entirely benign
# `queued` capture that it "was refused as a secrets or personal-data match" is false and needlessly
# alarming. Two different reasons for an absence get two different sentences — this project's
# honest-states doctrine, not a copy preference.
WITHHELD_PENDING_NOTE = (
    "not shown yet — nothing has scanned this material for secrets or personal data yet, so it "
    "stays out of this view while the capture is `queued` or `claimed`; it appears here as soon "
    "as the librarian has looked at it")

WITHHELD_UNSCANNED_NOTE = (
    "withheld — this capture's processing run failed before the secrets/personal-data scan "
    "reached it, so its material was never confirmed safe to echo back; unlike a queued capture "
    "this is not retried automatically, so ask an operator to look at this submission if you need "
    "to know what happened to it")

# The two states the gate has not yet run for at all — named once, so the read path and
# any future caller asking the same question ("has the gate looked at this row yet") consult one
# set rather than re-deriving it from `TERMINAL_STATUSES`/`PARKED_STATUSES`, which answer a
# different question (is a human being waited on) that happens to overlap here but is not the same
# fact.
GATE_NOT_YET_RUN_STATUSES = frozenset({QUEUED, CLAIMED})


def _reason_flagged(status: str, report: dict | None) -> bool:
    """Is this row's material flagged for a MATCH (`WITHHELD_MATERIAL_NOTE`): a secret/PII match,
    or a `rejected` row carrying no `reason_code` at all (fail-closed
    — see `capture.queue._MATERIAL_WITHHELD`'s own comment for why that case cannot be told apart
    from a genuine match without reading prose). Pure and DB-free: `report` is already the decoded
    JSONB dict a caller has in hand (`_shape_listed`'s row, `get_submission_trace`'s trace), so no
    second SQL round-trip is needed just to pick a sentence."""
    reason_code = (report or {}).get(REASON_CODE_KEY)
    if reason_code in WITHHELD_REASONS:
        return True
    return status == REJECTED and reason_code is None


def withheld_reason(status: str, report: dict | None) -> str:
    """THE one function that turns "is this row's material withheld, and why" into the sentence
    every read surface renders. `capture.queue`'s `_shape_listed` and `get_submission_trace` both
    call it, so the rule is decided once instead of in two places that could disagree. Three
    non-overlapping reasons, in priority order:

    1. `status in GATE_NOT_YET_RUN_STATUSES` (`queued`/`claimed`): the gate has not run at all yet
       — `WITHHELD_PENDING_NOTE`.
    2. `status == FAILED`: the accepted residual — `WITHHELD_UNSCANNED_NOTE`.
    3. otherwise, `_reason_flagged`: a genuine secret/PII match (or the null-reason-code
       fail-closed case) — `WITHHELD_MATERIAL_NOTE`.

    Everything else — `needs_input`, `triage`, `filed`, `resolved`, and a `rejected` row for any
    OTHER reason (duplicate, steering, a steward's own judgment) — returns `""`: the gate has run,
    or a human looked at the row directly.
    """
    if status in GATE_NOT_YET_RUN_STATUSES:
        return WITHHELD_PENDING_NOTE
    if status == FAILED:
        return WITHHELD_UNSCANNED_NOTE
    return WITHHELD_MATERIAL_NOTE if _reason_flagged(status, report) else ""


# ── the `report` column's SHAPE, and the one sentence two packages have to share ──────────────
# `base_report` used to live in `librarian.report`, which is where every report SENTENCE is still
# composed and where it is still re-exported from. It lives down here for a reason that is not
# tidiness: `resolved` and a steward's `rejected` are reports composed by the STEWARD's tooling
# (`capture.dispositions`), and `capture` may not import `librarian` — so the shape had to live in
# the package that owns the column it is stored in, or be written twice.
#
# Same posture as the status enum and the reason codes above: the vocabulary is the QUEUE's, the
# wording is each writer's. `librarian.report` keeps every fast-lane sentence; `capture.dispositions`
# keeps the two a steward authors; both build the same shape, so `render_prose`,
# `server.service._neutralize_report` and `brain_submissions` see one object either way.
SEARCHABILITY_NOTE = ("Becomes searchable at the next index rebuild or at the webhook's "
                      "incremental upsert, whichever lands first.")


def base_report(*, status: str, summary: str, **facts) -> dict:
    """THE shared shape. Every terminal (and parked) state goes through here, so no state can
    quietly ship without the fields the others carry."""
    report = {
        "status": status,
        "summary": summary,
        "page_path": "",
        "commit": "",
        "anchored_to": "",
        "links_created": [],
        "overlaps_flagged": [],
        "pages_edited": [],
        "agent_rationale": "",
        "findings": [],
    }
    report.update(facts)
    return report


# ── ask-back: the reply channel's own vocabulary ──────────────────────────────────────────────
# The MCP tool a `needs_input` row's question tells the submitter to run, and the exact invocation
# it states. Written HERE for the third time for the same reason the status enum is: `librarian`
# composes the question, `server` shapes the row for the wire and mounts the tool, and neither may
# import the other. Three surfaces spelling one callable string by hand is how a message ends up
# promising a command that does not exist — and a message containing a command is an executable
# promise.
REPLY_TOOL = "brain_reply"


def reply_invocation(submission_id) -> str:
    """The exact call a submitter runs to answer this row's one question.

    Kept on its own line by every renderer, and exposed as a STRUCTURED sibling of the question
    (`server.service._shape_submission`'s `reply_hint`) as well as inside its prose. The prose is
    read by a person — often relayed through their own LLM session, which has no reason to preserve
    a literal string — and the structured field is what a careful reader (or a future non-agent
    consumer) can rely on without depending on that fidelity. Same doctrine as `reason_code` sitting
    beside a refusal sentence: the sentence is for a person, the fact is for a reader that branches.
    """
    return f'{REPLY_TOOL}(submission_id={submission_id}, answer="<your answer>")'


# ── the row's own history: what HUMANS did to it ──────────────────────────────────────────────
# `audit_log` records the CALL (who invoked which tool, when, with what outcome); `capture_queue.
# trace` records the ROW (what it was asked, what was answered, which steward disposed of it and
# why). Both are needed and neither substitutes for the other: an audit row cannot answer "what has
# happened to submission 14" without a join nobody writes, and a steward deciding what to do with a
# parked row is asking exactly that question.
#
# Deliberately NOT a record of the fast lane's own outcomes — `filed`/`rejected`/`failed` are fully
# described by `report`, and duplicating them here would create two accounts of one event. The one
# machine-written entry is `asked`, because a question waiting on a person is a fact about the row's
# relationship to a HUMAN, which is what this column is for.
EVENT_ASKED = "asked"
EVENT_REPLIED = "replied"
EVENT_REQUEUED = "requeued"
EVENT_RESOLVED = "resolved"
EVENT_REJECTED = "rejected"
TRACE_EVENTS = (EVENT_ASKED, EVENT_REPLIED, EVENT_REQUEUED, EVENT_RESOLVED, EVENT_REJECTED)

# The actor recorded for the one event no human performs.
ACTOR_LIBRARIAN = "librarian"

# Bounds on the column, both of them. A steward can requeue a row as often as they like, so the
# event list needs a ceiling; and the `asked` event's note carries the question verbatim (which is
# what keeps the whole journey readable after the row has moved on and `report` has been rewritten
# by the next pass), so a note needs one too. Oldest events are dropped, never newest: the recent
# history is the one a steward is acting on.
MAX_TRACE_EVENTS = 20
MAX_TRACE_NOTE_CHARS = 2000

# The bound on one reply. Proportioned like the other free-text ceilings in this system rather than
# picked: `librarian.agent.MAX_PROSE_LEN` (2000) is what a model may write into a report field, and
# an answer to "which of these entities is this about?" is a far smaller thing than that — the
# useful answer is a name or one sentence. Hand-mirrored rather than imported, exactly like
# `MAX_HINT_CHARS` mirrors `server.service.MAX_ARG_CHARS`: `capture` sits below both packages and
# may import neither. If one moves, this comment is the pointer to the other.
MAX_REPLY_CHARS = 2000


def prepare_reply(answer: str) -> str:
    """Validate a submitter's reply. Raises `ReplyRejected` — always BEFORE the row is touched.

    Deliberately in the same voice as `prepare_submission`'s refusals, and safe to echo verbatim
    over HTTP for the same reason: it names a static limit and the caller's own size, never another
    identity, a path or whether any row exists.
    """
    if not isinstance(answer, str) or not answer.strip():
        raise ReplyRejected("answer is empty — there is nothing to record")
    if len(answer) > MAX_REPLY_CHARS:
        raise ReplyRejected(
            f"answer too long: {len(answer)} characters (max {MAX_REPLY_CHARS}) — say which entity, "
            f"or that it's new, in fewer words")
    return answer


# What `stigmergy-index --rebuild` must leave standing. `audit_log` belongs to
# `stigmergy.server.audit` but is named here because this is the one place the durable/disposable
# boundary is written down — the assertion is about the DATABASE, not about one package.
DURABLE_TABLES = ("capture_queue", "audit_log", "job_runs", "ingest_errors")

# ── the submission contract ───────────────────────────────────────────────────────────────────
# The vocabulary here is the SHAPE of the material and the flow that reads it, never a topic.
# `raw`/`page` describe what the fast lane's ordinary agent receives (unstructured text, a
# pre-drafted page); `meeting` describes a transcript that the WORKER routes to a dedicated flow
# (`librarian.processing`'s meeting path) rather than the ordinary one-page agent. Read it as
# "which reader claims this row", not as "what is this row about" — a transcript is not a topic
# any more than `page` is, and any future kind is expected to name a flow the same way.
KINDS = ("raw", "page", "meeting", "drive")
MEETING = "meeting"
# A Drive-fetched DOCUMENT (ADR 028) — original bytes in evidence (blob_refs[1]), a
# deterministic text manifest as the row's material (blob_refs[0], what dedup keys on), and the
# WORKER converts bytes → text before the ordinary fast-lane flow runs with the source
# attachment ON. Same reading as every kind: which reader claims this row, never a topic.
DRIVE = "drive"

# The kinds `brain_submit` (the MCP transport, where `kind` is a MODEL-CHOSEN argument) may
# enqueue. `KINDS` growing to include `"meeting"` made it acceptable to every caller of
# `queue.submit`, including this one — the drop CLI is meant to be the only door onto the meeting
# flow, and the runbook says so, but nothing enforced it at the one seam a steered MCP session
# could actually reach. Restricted explicitly here rather than left to `KINDS`, which the CLI (an
# operator's own direct-DB tool, not a model-facing one) still needs the full set for.
MCP_SUBMIT_KINDS = ("raw", "page")

# The hard cap on captured text: one pasted conversation would otherwise inflate a
# queue row, an audit row and a bucket object at once. Measured in UTF-8 BYTES, not characters —
# characters are what a client counts, bytes are what the database, the JSONB row and the object
# store actually pay for.
MAX_MATERIAL_BYTES = 256 * 1024

# The bound on any single hint value. Sibling of `stigmergy.server.service.MAX_ARG_CHARS` (8192),
# deliberately NOT imported: `capture` sits below `server` and may not import it
# (`tests/test_architecture.py`). Mirrored by hand, like the answer layer's other hand-mirrored
# constants — if one moves, this comment is the pointer to the other.
MAX_HINT_CHARS = 8192

# The client's own hints: a suggested type, path, entity and title. An allowlist, not a free-form
# bag — an unknown key gets a clear error naming the allowed ones, the same posture `search_brain`
# already takes for an unknown filter name. The librarian reads these as SUGGESTIONS: its judgment
# decides placement, hints never bind it.
HINT_KEYS = ("type", "path", "entity", "title")

# The Slack transport's capture PROVENANCE — permalink, channel id/name, thread
# ts, participant display names, message timestamps, and the fact that the client is the Slack
# app. A different KIND of hint from `HINT_KEYS` above (provenance metadata, never a page-
# placement suggestion), carried through the SAME allowlist-and-string-value seam rather than a
# second one: compatible extension, not a widened meaning for the original four. Every value here
# is still a plain string, exactly like `HINT_KEYS`'s (a list — participant names, message
# timestamps — is joined into one comma-separated string by the caller before it reaches this
# module, never a structure this layer has to parse per element); this is what keeps
# `normalize_hints`'s value validation completely unchanged for old and new keys alike.
SOURCE_HINT_KEYS = ("source_client", "source_permalink", "source_channel_id",
                    "source_channel_name", "source_thread_ts", "source_participants",
                    "source_message_timestamps")

# What a caller's `hints` dict may name, across both allowlists. `HINT_KEYS` itself is UNCHANGED
# (existing callers, and the test pinning its exact four names, are unaffected) — this is the
# union `normalize_hints` actually checks a key against.
# The meeting drop CLI's own metadata — the meeting's date (becomes every filed decision page's
# `as_of`), attendee names (HINTS for the agent, never identities: they resolve nothing and
# authorize nothing) and a source label (`granola-manual` is the only value produced today,
# carried as data rather than hardcoded so a future automated drop can name itself without a
# schema change). Same allowlist-and-string-value seam `SOURCE_HINT_KEYS` already established for
# Slack provenance — compatible extension, not a third mechanism.
MEETING_HINT_KEYS = ("meeting_date", "attendees", "source_label")


# The subset of `SOURCE_HINT_KEYS` the fast lane TRUSTS. `source_client == "slack"` is what turns
# the source-page attachment ON (`librarian.processing._source_attachment`), and
# `source_permalink` lands as `url:` on a READER-FACING `sources/slack/` page — provenance a filed
# page asserts, not decoration in the agent's prompt. That is the provenance-vs-suggestion line:
# the other five source hints remain suggestions nothing downstream trusts, so they stay
# ordinary. Refused
# at the seam a CLIENT can reach (`BrainService._submit`) for every door but Slack's own —
# `reject_source_provenance_hints` below; the Slack transport composes these hints itself, in
# server code, from Slack's API responses (`slack.capture._material_and_hints`).
SOURCE_PROVENANCE_HINT_KEYS = frozenset({"source_client", "source_permalink"})

# The Slack door's name for itself, as `BrainService.door` spells it — the one door whose
# services may assert `SOURCE_PROVENANCE_HINT_KEYS`. Owned here because this module owns the
# seam that reads it; `slack.context.build_service` and the librarian's trigger both import it
# rather than re-spelling the string.
SLACK_DOOR = "slack"

# The drive drop CLI's own provenance (ADR 028 D7) — the Drive file's id, display name (the
# extension the worker converts by), webViewLink (lands as `url:` on the reader-facing
# `sources/drive/` parts — the "binary stays in Drive" click-away), mime type and modifiedTime.
# Same allowlist-and-string-value seam as `SOURCE_HINT_KEYS` and `MEETING_HINT_KEYS`: a new
# small allowlist, string-valued, additive — never a widened meaning for an existing key.
DRIVE_HINT_KEYS = ("drive_file_id", "drive_name", "drive_url", "drive_mime", "drive_modified")

# The subset a downstream reader TRUSTS, the same pattern one allowlist up: the two that decide
# or decorate the reader-facing source attachment (ADR 028 D7 — `drive_url` lands as `url:` on
# the `sources/drive/` parts). The flow key itself is the ROW'S OWN `kind == DRIVE` —
# server-asserted by the operator CLI, unreachable through `brain_submit` (`MCP_SUBMIT_KINDS`) —
# so unlike Slack's pair these two decide nothing on a row a client can create; they are refused
# at the client seam anyway, loudly, because a hint a reader will ever trust must not be
# assertable from any client door at all.
#
# `drive_name` is deliberately NOT in this pair. It is REQUIRED at the submit seam for a drive
# row (`_require_drive_hints` — its extension is what the worker's conversion dispatches on),
# but any door may send it and nothing downstream trusts it as provenance. Required-at-the-seam
# and refused-at-the-door are different properties; the gate below refuses only the pair.
DRIVE_PROVENANCE_HINT_KEYS = frozenset({"drive_file_id", "drive_url"})

# What a caller's `hints` dict may name, across all four allowlists. `HINT_KEYS` itself is
# UNCHANGED for the same reason `SOURCE_HINT_KEYS`'s own comment gives.
ALLOWED_HINT_KEYS = HINT_KEYS + SOURCE_HINT_KEYS + MEETING_HINT_KEYS + DRIVE_HINT_KEYS

# Fields a client may never assert. Arriving as a tool argument or a hint, each one is an explicit
# error; declared in a pre-drafted page's frontmatter, each one is recorded as a flagged hint and
# otherwise inert.
# **Every member of this set MUST have a declared parameter on the `brain_submit` MCP tool.**
#
# That is an invariant, not a convention, and it is the reason this constant exists separately
# from the queue's columns below. FastMCP builds a tool's argument model with pydantic's default
# `extra="ignore"`, so an argument the tool signature does not NAME is stripped by the SDK before
# the server ever sees it — silently, with no error and nothing in the response. Declaring the
# parameter is therefore the only way to REFUSE a field rather than quietly ignore it.
#
# That mistake has been made here once already: only `submitted_by` was declared, so
# `verification`/`acl`/`content_hash` were accepted in silence. Adding a field here without adding
# its parameter re-opens exactly that hole — which is why the set is enumerable and a test walks
# it.
#
# Three of the four are computed by the server or the librarian and may never be asserted by a
# client OR by a document: who submitted it, who may see it, what it hashes to. `verification` is
# listed for the opposite reason — NOTHING computes a verdict, so a page asserting one would be
# claiming a guarantee this system does not make.
ATTRIBUTION_FIELDS = frozenset({"submitted_by", "verification", "acl", "content_hash"})

# The queue's own columns, which deliberately have NO tool parameter: a client has no vocabulary
# for them, so there is nothing to declare and nothing to refuse at the argument boundary — they
# are unreachable by construction. They are listed so that a caller addressing the QUEUE by these
# names (an argument, or a key inside `hints`) is refused loudly rather than confusingly.
#
# Note what they are NOT: a page's own frontmatter. `id` and `status` are legitimate page-contract
# fields there (`corpus.PageRow.page_id` reads frontmatter `id`; `status` is the page's
# draft/developing/canonical lifecycle), and they mean something entirely different from this
# queue row's id and state. See `normalize_hints` for where that distinction is applied.
QUEUE_OWNED_COLUMNS = frozenset({
    "id", "status", "attempts", "blob_refs", "result_ref",
    "created_at", "claimed_at", "finished_at", "error",
})

# The union — what `reject_server_owned_arguments` consults, unchanged. A client addressing this
# server may not set any of them, whichever half they come from.
SERVER_OWNED_FIELDS = ATTRIBUTION_FIELDS | QUEUE_OWNED_COLUMNS

_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---[ \t]*(?:\n|$)", re.S)
_FM_LINE_RE = re.compile(r"^([A-Za-z_][\w.-]*)[ \t]*:[ \t]*(.*)$")


@dataclass(frozen=True)
class Submission:
    """A validated submission, ready to be archived and enqueued. Pure — no DB, no network, no
    clock: `queue.submit` supplies the identity and the timestamps come from Postgres."""
    kind: str
    material: str
    material_bytes: bytes
    digest: str          # sha256 hex of `material_bytes` — the evidence key and the audit hash
    size: int            # len(material_bytes)
    payload: dict        # the jsonb column the librarian consumes
    hints: dict          # the jsonb column: client hints + declared frontmatter + flags


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _is_encodable(material: str) -> bool:
    """Can this string be archived at all? `str` is wider than UTF-8: an unpaired surrogate has no
    encoding, and every byte-shaped thing downstream (the digest, the evidence blob, the payload)
    needs one."""
    try:
        (material or "").encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def material_digest(material: str) -> tuple[str, int]:
    """`(sha256 hex, byte length)` of the material as it will be archived.

    One definition, used by `prepare_submission` AND by the server's audit-arg builder: the hash
    the audit row records and the hash the evidence key is built from have to be the same number,
    or the trail from "who submitted what, when" to "which object holds it" does not join up.
    """
    data = (material or "").encode("utf-8")
    return _sha256_hex(data), len(data)


def reject_server_owned_arguments(args: dict) -> None:
    """Fail LOUDLY on any server-owned field arriving as a tool argument.

    The MCP SDK will not do this for us: FastMCP builds the tool's argument model with pydantic's
    default `extra="ignore"`, so an unexpected `submitted_by` in the arguments would be dropped
    silently — quietly ignored, which is precisely the outcome a caller must never get. The tool
    and the service therefore accept the field explicitly so they can refuse it; this is that
    refusal.

    Only NON-None values count, so the ordinary call (the parameter defaulted, never sent) is
    untouched. The message names the offending KEYS and nothing else — never the values, which
    are somebody's identity, ACL labels or trust claim, and never a path or an internal detail.

    Caveat worth stating where the mechanism lives: this can only refuse fields the caller's
    surface actually declares. A field the MCP tool signature does not name is dropped by the SDK
    before the server sees it (pydantic `extra="ignore"`), so it is silently inert rather than
    loudly refused. That is acceptable because the security property is structural — nothing reads
    client input into a server-computed column — and all four `ATTRIBUTION_FIELDS` are declared.
    See `docs/reference/capture.md`, "the residual".
    """
    present = sorted(k for k, v in args.items() if v is not None and k in SERVER_OWNED_FIELDS)
    if not present:
        return
    plural = len(present) > 1
    message = (f"{', '.join(present)} {'are' if plural else 'is'} set by the server, not by the "
               f"caller — remove {'them' if plural else 'it'} and resubmit")
    if "submitted_by" in present:
        message += (" (attribution comes from your resolved identity; submitting as someone else "
                    "requires their token, not their name)")
    raise SubmissionRejected(message)


def reject_source_provenance_hints(hints: dict | None, *, door: str) -> None:
    """Fail LOUDLY on `source_client`/`source_permalink` arriving from any door but Slack's own.

    These are the two `SOURCE_HINT_KEYS` the fast lane TRUSTS (see
    `SOURCE_PROVENANCE_HINT_KEYS`' own comment): `source_client`
    decides whether a `sources/slack/` page is attached, and `source_permalink` is written as that
    page's `url:`. A `brain_submit` caller asserting them would get the verbatim material filed as
    a reader-facing "captured Slack thread" pointing at a permalink nobody fetched — Slack
    provenance on a page that never crossed the Slack door.

    `door` is `BrainService.door`: `""` for every client-facing service (stdio, HTTP), `SLACK_DOOR`
    only when `slack.context.SlackContext.build_service` built it — server code, whose hints come
    from Slack's own API responses, not from a caller's arguments. Called at the same seam as its
    server-owned-argument refusal (`BrainService._submit`), never inside
    `normalize_hints`/`queue.submit`.

    Only NON-None values count, and the message names the offending KEYS only — no values —
    matching both siblings' posture.
    """
    if door == SLACK_DOOR:
        return
    present = sorted(k for k, v in (hints or {}).items()
                     if v is not None and k in SOURCE_PROVENANCE_HINT_KEYS)
    if not present:
        return
    plural = len(present) > 1
    raise SubmissionRejected(
        f"{', '.join(present)} {'are' if plural else 'is'} set by the Slack transport itself, "
        f"not by the caller — remove {'them' if plural else 'it'} and resubmit (this pair decides "
        f"whether the capture files a `sources/` page recording Slack provenance; asserting it "
        f"from another door would put a permalink nobody fetched on a reader-facing page)")


def normalize_hints(hints: dict | None, material: str) -> dict:
    """Validate the caller's hints and record what the material DECLARES about itself.

    Returns the stable three-key shape the librarian reads:

        {"client": {...},                 # the caller's own suggestions, validated
         "declared_frontmatter": {...},   # what the material's frontmatter says, recorded verbatim
         "flagged": [...]}                # the server-owned subset of the above — never trusted

    A server-owned key inside `hints` is refused the same way an argument is; an
    unknown key gets a clear error listing the allowed ones. Declared frontmatter is only ever
    RECORDED: `flagged` exists so the librarian, a steward and an audit can all see that a
    document tried to assert its own `submitted_by`/`acl`/`verification`/`content_hash`.

    **The two checks below consult DIFFERENT constants, and that is the point.** A `hints` key is
    the client addressing THIS QUEUE, so the whole union is refused — `hints={"status": …}` is
    somebody reaching for a queue column and gets a loud refusal. Frontmatter is a document
    describing ITSELF, where `id` and `status` are ordinary page-contract fields (a pre-drafted
    page saying `status: developing` is doing the normal thing), so only `ATTRIBUTION_FIELDS` is
    flagged there. Flagging the union would tell that submitter their own `id`/`status` "are the
    server's" — a false accusation on a legitimate page, and noise the librarian would have to
    learn to ignore.
    """
    client: dict[str, str] = {}
    for key, value in (hints or {}).items():
        key = str(key)
        if key in SERVER_OWNED_FIELDS:
            raise SubmissionRejected(
                f"hints may not carry {key}: it is set by the server, not by the caller")
        if key not in ALLOWED_HINT_KEYS:
            raise SubmissionRejected(
                f"unknown hint {key!r} (allowed: {', '.join(ALLOWED_HINT_KEYS)})")
        if value is None:
            continue
        if not isinstance(value, str):
            raise SubmissionRejected(f"hint {key!r} must be a string, got {type(value).__name__}")
        if len(value) > MAX_HINT_CHARS:
            raise SubmissionRejected(f"hint {key!r} too long (max {MAX_HINT_CHARS} characters)")
        client[key] = value

    declared = declared_frontmatter(material)
    return {
        "client": client,
        "declared_frontmatter": declared,
        # ATTRIBUTION_FIELDS, not the union — see the docstring: a page's own `id`/`status` are
        # its to declare, and flagging them would accuse a legitimate page of forgery.
        "flagged": sorted(k for k in declared if k in ATTRIBUTION_FIELDS),
    }


def declared_frontmatter(material: str) -> dict[str, str]:
    """Top-level `key: value` pairs of a leading `---` block, as flat strings.

    **Deliberately not YAML.** `stigmergy.index.corpus.split_frontmatter` uses `yaml.safe_load`,
    which is right for a trusted repo checkout; this input is attacker-controlled
    text arriving over a public HTTP boundary, and `safe_load` still expands anchors and aliases
    (a 256 KB billion-laughs payload is well inside our size cap). A shallow, non-recursive scan
    cannot be made to allocate.

    Best-effort BY DESIGN: an exotically-quoted or multi-line `submitted_by:` may go unflagged,
    and that is harmless — the flag is an ANNOTATION. Attribution never reads this dict; it comes
    from the resolved caller identity, so a missed flag loses a note, never the security
    property. The material itself is stored verbatim either way.
    """
    match = _FRONTMATTER_RE.match(material)
    if not match:
        return {}
    declared: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if not line.strip() or line[:1] in (" ", "\t", "#", "-"):
            continue                        # nested value, list item or comment: top level only
        field = _FM_LINE_RE.match(line)
        if field:
            declared[field.group(1)] = field.group(2).strip()[:MAX_HINT_CHARS]
    return declared


# `--date` becomes every filed decision page's `as_of`, so a wrong format silently accepted (or
# crashing downstream with no reference back to the flag) would violate this codebase's own
# fail-closed-on-bad-input instinct.
# `\d{4}-\d{2}-\d{2}` rather than `datetime.date.fromisoformat` alone: the stdlib parser also
# accepts basic-format `20260729` (no dashes) on 3.11+, which is not the format the flag documents.
_MEETING_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def validate_meeting_date(value: str) -> str:
    """A meeting date, refused if it is not a real `YYYY-MM-DD` calendar date.

    Two checks, both load-bearing: the SHAPE (four digits, dash, two digits, dash, two digits —
    refuses `07-29-2026`, `2026/07/29`, a timestamp) and that it names a real date at all (refuses
    `2026-02-30`). Raises `SubmissionRejected`, the same exception every other drop-time refusal
    raises, so the CLI's "no row and no object" guarantee holds for this refusal too.

    **Called from TWO seams, not one.** `meeting_cli._cmd_drop` calls this directly, first, so a
    malformed `--date` is refused before any file is even read — message quality for the operator
    CLI's own early-exit ordering. `prepare_submission`
    (below) calls it again, unconditionally, for every `kind == MEETING` submission — the seam
    every caller of `queue.submit` passes through regardless of transport, which is what makes the
    CLI's early copy a convenience rather than the control.
    """
    import datetime as _datetime

    text = str(value or "").strip()
    if not _MEETING_DATE_RE.match(text):
        raise SubmissionRejected(
            f"a meeting date must be YYYY-MM-DD (got {value!r}) — it becomes `as_of` on every "
            f"decision page this meeting files")
    try:
        _datetime.date.fromisoformat(text)
    except ValueError:
        raise SubmissionRejected(f"{value!r} is not a real calendar date") from None
    return text


def reject_drive_provenance_hints(hints: dict | None) -> None:
    """Fail LOUDLY on `drive_file_id`/`drive_url` arriving from ANY client door — the posture
    `reject_source_provenance_hints` takes for Slack's pair, with no door exception at all: the
    one legitimate asserter (`stigmergy-drive drop`, an operator CLI with direct DB access) never
    passes through `BrainService._submit`, so a service seeing either key is always a caller
    trying to dress a capture in Drive provenance nobody fetched. Called beside its sibling in
    `BrainService._submit`; never inside `normalize_hints`/`queue.submit`, which the CLI still
    passes through."""
    present = sorted(k for k, v in (hints or {}).items()
                     if v is not None and k in DRIVE_PROVENANCE_HINT_KEYS)
    if not present:
        return
    plural = len(present) > 1
    raise SubmissionRejected(
        f"{', '.join(present)} {'are' if plural else 'is'} set by the stigmergy-drive operator "
        f"CLI itself, not by a caller — remove {'them' if plural else 'it'} and resubmit (Drive "
        f"provenance on a reader-facing source page must come from a fetch that actually "
        f"happened)")


def _require_drive_hints(client: dict) -> None:
    """`kind == DRIVE` requires `drive_file_id` and `drive_name` at THIS seam — the one every
    caller of `queue.submit` passes through — never only in the drop CLI's own early copy, for the
    reason `_require_meeting_hints` records. `drive_name` carries the extension the worker's
    conversion dispatches on (`kernel.converters.method_for_ext`); a missing one would not fail
    closed — it would fall through to the `text` method and file a PDF's raw bytes as prose."""
    if not str(client.get("drive_file_id") or "").strip():
        raise SubmissionRejected(
            "a drive submission requires hints['drive_file_id'] — the Drive file this capture "
            "was fetched from")
    if not str(client.get("drive_name") or "").strip():
        raise SubmissionRejected(
            "a drive submission requires hints['drive_name'] — the file's display name; its "
            "extension is what the worker's conversion dispatches on")


def _require_meeting_hints(client: dict) -> None:
    """`kind == MEETING` requires a `title` and a valid `meeting_date` at THIS seam —
    `prepare_submission`, which every caller of `queue.submit` passes through
    (`meeting_cli._cmd_drop` AND `server.service._submit`, the MCP transport's `brain_submit`) —
    not only inside the drop CLI's own early copy. Without it a steered MCP session could reach
    `queue.submit(kind="meeting", hints={"meeting_date": "2026-01-01\\nfoo: bar"})` directly: it
    happened to fail CLOSED, but by ACCIDENT, via `gate_frontmatter`'s stamped-value
    equality check aimed at an entirely different attack, and untested. A MISSING `meeting_date`
    is worse: nothing refused it at all, and `processing._stamp_meeting`'s
    `meeting_meta.get("meeting_date") or deps.as_of()` fallback silently degraded every page's
    `as_of` to today — a wrongly-dated meeting set, filed as if nothing were wrong.

    `title` gets no format check (any non-empty string is a title); `meeting_date` gets the same
    `validate_meeting_date` the CLI itself calls, so a caller of EITHER seam sees the same rule.
    """
    if not str(client.get("title") or "").strip():
        raise SubmissionRejected(
            "a meeting submission requires hints['title'] — the meeting's title, used as this "
            "capture's source and meeting page identity")
    validate_meeting_date(client.get("meeting_date") or "")


def prepare_submission(kind: str, material: str, hints: dict | None = None) -> Submission:
    """Validate a submission and build its payload/hints. Raises `SubmissionRejected` — always
    BEFORE any blob or row is written, which is what makes "no row and no blob" true for every
    refusal."""
    if kind not in KINDS:
        raise SubmissionRejected(f"unknown kind {kind!r} (allowed: {', '.join(KINDS)})")
    if not isinstance(material, str) or not material.strip():
        raise SubmissionRejected("material is empty — there is nothing to capture")
    # Refused HERE, as a rejection, because the very next line encodes it. A lone surrogate is a
    # legal `str` that `json.loads` will happily produce (`"\ud800"`), so it survives any JSON-RPC
    # transport and reaches this seam — where `.encode("utf-8")` raised `UnicodeEncodeError`, a
    # `ValueError` and not a `CaptureError`. Every door's `except CaptureError` missed it and it
    # landed in the generic handler that says "cannot reach the queue database or evidence store",
    # so a rejected INPUT was reported to the operator as the infrastructure being down.
    if not _is_encodable(material):
        raise SubmissionRejected(
            "material contains unpaired surrogate characters, which are not text this can archive "
            "— re-send it as valid UTF-8")
    digest, size = material_digest(material)
    if size > MAX_MATERIAL_BYTES:
        raise SubmissionRejected(
            f"material too large: {size} bytes (max {MAX_MATERIAL_BYTES}) — submit the part "
            "worth keeping, not the whole transcript")
    normalized_hints = normalize_hints(hints, material)
    if kind == MEETING:
        _require_meeting_hints(normalized_hints["client"])
    if kind == DRIVE:
        _require_drive_hints(normalized_hints["client"])
    return Submission(
        kind=kind,
        material=material,
        material_bytes=material.encode("utf-8"),
        digest=digest,
        size=size,
        payload={"kind": kind, "text": material, "sha256": digest, "bytes": size},
        hints=normalized_hints,
    )


# ── DDL (idempotent; owned here, never dropped by an index rebuild) ───────────────────────────
# The status CHECK is NAMED rather than left to Postgres, because adding a status has to replace
# it on tables that already exist and an auto-generated name is a guess.
# `capture_queue_status_check` is exactly the name Postgres derives for an unnamed column check on
# this column, so naming it here is not a rename: an existing table's constraint and this one are
# the same object.
_STATUS_CHECK_NAME = "capture_queue_status_check"
_STATUS_LITERALS = ", ".join(f"'{s}'" for s in STATUSES)

_CAPTURE_QUEUE_DDL = f"""
CREATE TABLE IF NOT EXISTS capture_queue (
    id BIGSERIAL PRIMARY KEY,
    kind TEXT NOT NULL,
    payload JSONB,                                  -- NULLable: retention deletes it in place
    blob_refs TEXT[] NOT NULL DEFAULT '{{}}',       -- evidence keys (sha256/<ab>/<cd>/<hash>)
    submitted_by TEXT NOT NULL,                     -- the SERVER's resolved identity, always
    hints JSONB,                                    -- NULLable: retention deletes it in place
    status TEXT NOT NULL DEFAULT '{QUEUED}'
        CONSTRAINT {_STATUS_CHECK_NAME} CHECK (status IN ({_STATUS_LITERALS})),
    attempts INTEGER NOT NULL DEFAULT 0,            -- deliveries, incremented on each claim
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    claimed_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    result_ref TEXT NOT NULL DEFAULT '',            -- the filed page / commit
    error TEXT NOT NULL DEFAULT '',                 -- why the row is where it is (see below)
    report JSONB                                    -- the librarian's structured report
)
"""
# `report` is an ADDITIVE column: the librarian's structured account of one item — page, commit,
# anchoring, links, overlaps, pages edited, findings — plus the one human sentence that renders
# it. NULL on anything still in flight, which is why it is nullable and why no backfill exists: a
# row that was never processed by a librarian has no report, and saying so with NULL is more
# honest than an empty object.
#
# It sits BESIDE `error` rather than replacing it. `error` keeps its own meaning exactly — the
# one-line human reason a row is where it is, rendered as `question` for `needs_input` and as
# `error` for `failed`/`rejected` — so every consumer of `error` keeps working unchanged and
# simply gains a field. The structured fact set has nowhere to live in a TEXT column, and stuffing
# JSON into a column named `error` would fork one field into two meanings again.
_CAPTURE_QUEUE_REPORT_COLUMN = """
ALTER TABLE capture_queue ADD COLUMN IF NOT EXISTS report JSONB
"""
# `error` carries "why is this row where it is" for every non-happy state, and is rendered by two
# names on the way out (`server.service.BrainService.submissions`): as `question` for a
# `needs_input` row (the ask-back) and as `error` for `failed`/`rejected`. One column, two
# semantic renderings — deliberately: a `question` column beside it would fork the same field
# into two.

# ── the human loop's additive columns (the `ADD COLUMN IF NOT EXISTS` pattern) ─────────────────
# Four facts the queue could not previously carry, each nullable, each meaningless on an older row
# — which is why none of them needs a backfill: NULL says "this never happened to this row", which
# is exactly true of every capture written before the human loop existed.
#
#  * `asked_at`   — when this capture's ONE ask-back question was asked. The durable half of the
#                   one-ask budget: one ask per capture, ever. Set on the FIRST transition
#                   into `needs_input` and NEVER cleared — not by a reply, not by a requeue, not by
#                   a lease redelivery — which is what makes the budget survive all three. A
#                   boolean would have done the same job and answered no question a steward asks.
#  * `parked_at`  — when the row entered its CURRENT park. Distinct from `asked_at` (which is about
#                   the budget and outlives the park) and from `created_at` (which is when the
#                   material arrived, not when a human started being waited on). `finished_at` was
#                   not an option: it stays NULL for parked rows on purpose, because retention
#                   counts from it.
#  * `reply`      — the submitter's answer, verbatim and bounded (`MAX_REPLY_CHARS`). On the row
#                   rather than only in `trace` because the worker reads it on the hot path, and a
#                   JSONB dig for the one value every next pass needs would be the wrong shape.
#  * `trace`      — the append-only record of what HUMANS did to this row: asked, replied,
#                   requeued, resolved, rejected — each with an actor and a note. `audit_log`
#                   records the CALL; this records the row's own history, which is what
#                   `stigmergy-queue show` prints and what a steward reads before disposing of it.
_CAPTURE_QUEUE_HUMAN_LOOP_COLUMNS = (
    "ALTER TABLE capture_queue ADD COLUMN IF NOT EXISTS asked_at TIMESTAMPTZ",
    "ALTER TABLE capture_queue ADD COLUMN IF NOT EXISTS parked_at TIMESTAMPTZ",
    "ALTER TABLE capture_queue ADD COLUMN IF NOT EXISTS reply TEXT",
    "ALTER TABLE capture_queue ADD COLUMN IF NOT EXISTS trace JSONB",
)

# ── the parked agent outcome ──────────────────────────────────────────────────────────────────
#
# `outcome` — the agent's own structured account of the last pass over THIS capture, kept across a
# park so a re-file can reuse it instead of asking the model to read the material again.
#
# **Why this column exists.** Measured on a real long transcript: a pass distilled six decisions
# and was refused `anchoring/unresolved` — nothing was wrong with the distillation; one entity
# simply did not exist in the registry yet. A steward minted it and requeued. The next pass threw
# that distillation away, re-read the material from scratch, and produced THREE. Two of the lost
# decisions were ones an attendee confirmed were really taken.
#
# So the park->resolve->re-file loop — exactly the path built for the normal case — silently cost
# knowledge every time it ran, and it gets worse with meeting size: the longer the transcript the
# more a fresh read can drop, and the more likely a park is in the first place (more names, more
# chances one is unregistered). Re-running the model was not merely wasteful (a measured 6:51
# against 2:07); it was LOSSY, and invisibly so, because the second result looks perfectly
# plausible on its own.
#
# **Why a column of its own rather than a key inside `report`.** `report` crosses to the submitter
# through `brain_submissions`; this holds the full distillation, including every drafted page body.
# Those are different audiences and different lifetimes, and folding one into the other would ship
# the whole draft to a surface that asks for a summary.
#
# NULLable with no backfill, for the usual reason and one better: NULL means "no pass has ever
# produced a reusable account of this row", which is exactly true of every capture written before
# this column existed — and it is also the value that makes the reuse path a no-op, so an older
# row behaves precisely as it did yesterday.
_CAPTURE_QUEUE_OUTCOME_COLUMN = (
    "ALTER TABLE capture_queue ADD COLUMN IF NOT EXISTS outcome JSONB"
)

# The one genuinely non-additive migration: a new status has to join the CHECK constraint, and a
# constraint cannot be extended in place.
#
# **ONE statement, therefore ONE transaction — and that is the whole of the fix.** This used to be a
# PAIR of statements (`DROP CONSTRAINT IF EXISTS`, then `ADD CONSTRAINT`) sitting in `_ALL_DDL`,
# reasoned about as idempotent because the drop makes the add safe to repeat. Idempotent it was;
# atomic it was not. `_ALL_DDL` runs on an AUTOCOMMIT connection (`index.store.connect` — autocommit
# on purpose, so a reader never sits idle-in-transaction holding a lock a rebuild would block
# behind), so the drop committed on its own and the add was a second transaction — against the LIVE
# queue, on every process start: the server's, the worker's, `stigmergy-queue`'s and
# `stigmergy-entities`'. Three consequences, and the composition starts the server and the worker
# together, so none of them was hypothetical:
#
#   * between the two commits, `capture_queue` had NO status constraint at all;
#   * `ADD CONSTRAINT` takes ACCESS EXCLUSIVE and validates every row, so the full table lock was
#     taken on every CLI invocation against production rather than once — a steward typing a command
#     while a worker held a row lock could stall the queue behind it;
#   * two processes starting together raced: A DROP -> B DROP -> A ADD -> B ADD, and the second
#     ADD died with `DuplicateObject` (there is no `ADD CONSTRAINT IF NOT EXISTS` to hide behind).
#
# A `DO` block is a single statement, so the drop and the add are one transaction even under
# autocommit. A concurrent starter either sees the finished constraint and skips, or blocks on the
# table lock until the whole swap has committed and then redoes it atomically; neither can observe
# the table unconstrained, and neither can add a constraint the other already added.
#
# **And after the first run it does nothing at all.** The guard asks whether the constraint already
# in place mentions every status THIS release knows about — built from `STATUSES`, never a
# hand-written list, so a ninth status is one edit in one place and this migration wakes up by
# itself. `quote_literal` matches the quoted form `pg_get_constraintdef` renders, so no status name
# can satisfy the test by being a substring of another. A fresh database, whose table is created by
# `_CAPTURE_QUEUE_DDL` with this constraint already on it, therefore skips the swap on its very
# first startup.
#
# **Order matters.** This runs AFTER the table exists and BEFORE anything writes a status the old
# constraint did not allow. A
# process still running the previous release keeps working across it: the predicate only ever
# widens, so every status the old code writes is still accepted — the expand half of an
# expand/contract with no contract half to schedule (nothing is being removed).
_CAPTURE_QUEUE_STATUS_CHECK = f"""
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint c
        WHERE c.conrelid = 'capture_queue'::regclass
          AND c.conname = '{_STATUS_CHECK_NAME}'
          AND (SELECT bool_and(pg_get_constraintdef(c.oid) LIKE '%' || quote_literal(s) || '%')
               FROM unnest(ARRAY[{_STATUS_LITERALS}]) AS s)
    ) THEN
        ALTER TABLE capture_queue DROP CONSTRAINT IF EXISTS {_STATUS_CHECK_NAME};
        ALTER TABLE capture_queue ADD CONSTRAINT {_STATUS_CHECK_NAME}
            CHECK (status IN ({_STATUS_LITERALS}));
    END IF;
END $$
"""

_QUEUE_STATUS_INDEX = """
CREATE INDEX IF NOT EXISTS capture_queue_status_created_idx
    ON capture_queue (status, created_at)
"""
_QUEUE_SUBMITTER_INDEX = """
CREATE INDEX IF NOT EXISTS capture_queue_submitter_created_idx
    ON capture_queue (submitted_by, created_at DESC)
"""
# The expiry sweep's index. `release_expired` scans
# `status = 'claimed' AND claimed_at < now() - interval`. As an operator-typed
# `stigmergy-queue reclaim` that was an occasional seq scan over a small table; the librarian's
# worker runs it on EVERY claim, i.e. once per poll interval forever — which is what earns it a
# real index.
_QUEUE_CLAIMED_INDEX = """
CREATE INDEX IF NOT EXISTS capture_queue_status_claimed_idx
    ON capture_queue (status, claimed_at)
"""

# The operational spine. `job_runs` records each processing run and `ingest_errors` each failed
# item with its stage and attempt count — the instruments the capture -> page latency target is
# measured with.
_JOB_RUNS_DDL = """
CREATE TABLE IF NOT EXISTS job_runs (
    id BIGSERIAL PRIMARY KEY,
    job TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,
    stats JSONB NOT NULL DEFAULT '{}'::jsonb,
    error TEXT NOT NULL DEFAULT ''
)
"""
_JOB_RUNS_INDEX = """
CREATE INDEX IF NOT EXISTS job_runs_job_started_idx ON job_runs (job, started_at DESC)
"""
_INGEST_ERRORS_DDL = """
CREATE TABLE IF NOT EXISTS ingest_errors (
    id BIGSERIAL PRIMARY KEY,
    source TEXT NOT NULL,
    source_doc_id TEXT NOT NULL,
    stage TEXT NOT NULL,
    error TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved BOOLEAN NOT NULL DEFAULT false
)
"""
_INGEST_ERRORS_INDEX = """
CREATE INDEX IF NOT EXISTS ingest_errors_source_doc_idx
    ON ingest_errors (source, source_doc_id, last_at DESC)
"""

_ALL_DDL = (_CAPTURE_QUEUE_DDL, _CAPTURE_QUEUE_REPORT_COLUMN,
            *_CAPTURE_QUEUE_HUMAN_LOOP_COLUMNS, _CAPTURE_QUEUE_OUTCOME_COLUMN,
            _CAPTURE_QUEUE_STATUS_CHECK,
            _QUEUE_STATUS_INDEX, _QUEUE_SUBMITTER_INDEX, _QUEUE_CLAIMED_INDEX,
            _JOB_RUNS_DDL, _JOB_RUNS_INDEX, _INGEST_ERRORS_DDL, _INGEST_ERRORS_INDEX)


# ── the whole DDL run as ONE critical section, and why "all IF NOT EXISTS" is not enough ──────
# **`IF NOT EXISTS` is a check, not a lock, and the check is not atomic against a concurrent
# CREATOR.** This is the counter-intuitive part, and it is precisely what kept the defect
# invisible: every statement above reads as self-guarding, so the loop looks safe to run from the
# four processes that run it. It is not. `CREATE INDEX IF NOT EXISTS` looks the name up, finds
# nothing, and only then inserts into `pg_class` — two sessions can BOTH pass the "does not exist"
# test before either has committed, and the loser dies with `UniqueViolation` on `pg_class`'s own
# unique index. `CREATE TABLE IF NOT EXISTS` (`DuplicateTable`) and `ADD COLUMN IF NOT EXISTS` have
# the same shape. Postgres documents those forms as race-safe against a concurrent DROP, never
# against a concurrent CREATE.
#
# `_CAPTURE_QUEUE_STATUS_CHECK`'s `DO` block closed this for exactly ONE statement — the constraint
# swap — because that one had a loud failure mode (`DuplicateObject`, with no `IF NOT EXISTS` to
# hide behind) and so was the one anybody looked at. Its siblings in the same loop were left
# exposed, and wrapping each of them in its own `DO` block would not have helped: a guard inside a
# block has the same read-then-write gap the bare statement does.
#
# **When it bites, which is the other half of why nobody saw it.** Any already-migrated database
# already has these indexes, so a repeated startup finds them and skips. The race needs a
# genuinely FRESH database initialized by two processes at once: the first-ever `docker compose up`
# (the composition brings the server and the librarian up together) and the first deploy of
# `fly.toml`'s two process groups (`app` and `worker`). Invisible in every environment that already
# exists; fires on the first boot of every new one.
#
# **An advisory lock rather than tolerating `UniqueViolation`/`DuplicateTable` per statement**, on
# purpose: it makes the migration one critical section instead of making each statement
# individually forgiving. A swallowed `UniqueViolation` cannot distinguish a benign concurrent
# creator from a genuine schema conflict — an object of that name that is NOT the one this release
# expects — and would report success either way. The lock says "one process migrates at a time",
# which is the property actually wanted; the tolerated exception would say "any collision here is
# fine", which is not true.
#
# The key is a LITERAL and must stay one. Deriving it from the DDL text or the table names would
# make it change with the schema, so two releases mid-rollout would take DIFFERENT locks and stop
# excluding each other at exactly the moment a migration is in flight. `b"SYNCDDL"` read big-endian
# is 7 bytes, comfortably inside int8, and it is the whole DATABASE's key — `audit_log`'s DDL
# (`stigmergy.server.audit.ensure_audit_table`, this function's sibling, called on the same
# connection one line earlier at every server startup) takes the same one, because "one process
# migrates at a time" is a property of the database, not of one table.
_STARTUP_DDL_LOCK_KEY = int.from_bytes(b"SYNCDDL", "big")


@contextmanager
def startup_ddl_lock(conn):
    """Hold the startup-DDL advisory lock for the duration of the block, yielding a cursor to run
    the DDL on. Shared by `ensure_capture_schema` and `stigmergy.server.audit.ensure_audit_table`.

    Session-scoped (`pg_advisory_lock`, not `pg_advisory_xact_lock`) because this runs on an
    AUTOCOMMIT connection (`index.store.connect`, autocommit on purpose) where every statement is
    its own transaction — a transaction-scoped lock would be released by the very statement that
    took it. Session scope is also what makes the `finally` load-bearing rather than decorative: on
    a pooled or otherwise reused connection a lock left held outlives the failed startup and blocks
    the next one forever. A process that dies outright is safe either way — the backend ends and
    the lock goes with it.

    Blocking, not `pg_try_advisory_lock`: a starter that could not take the lock has nothing useful
    to do but wait, and failing startup because another process is mid-migration would turn a
    correctness fix into an availability bug.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_lock(%s::bigint)", (_STARTUP_DDL_LOCK_KEY,))
        try:
            yield cur
        finally:
            try:
                cur.execute("SELECT pg_advisory_unlock(%s::bigint)", (_STARTUP_DDL_LOCK_KEY,))
            except Exception:  # noqa: BLE001 — cleanup must never mask the DDL failure above
                # The only ways this fails are a connection already broken, or (on a caller that
                # is not autocommit) one sitting in a transaction the failed statement aborted. In
                # both cases the DDL error is the one the operator needs to see, and the lock is
                # session-scoped, so closing the connection releases it regardless.
                log.warning("could not release the startup-DDL advisory lock; it is released when "
                            "this connection closes", exc_info=True)


def ensure_capture_schema(conn) -> None:
    """Idempotent DDL for the durable write-path tables — safe to call on every startup, from
    every transport and from the CLI (mirrors `audit.ensure_audit_table`), and safe to call from
    two processes at once.

    **Never drops a TABLE**: the queue outlives every index rebuild, and everything here is
    `CREATE TABLE IF NOT EXISTS` / `ADD COLUMN IF NOT EXISTS`. It is not true that nothing is ever
    dropped — `_CAPTURE_QUEUE_STATUS_CHECK` replaces the status CHECK constraint on a table created
    before `resolved` existed, because a constraint cannot be widened in place. That drop and its
    re-add are one statement, so they are one transaction even on the autocommit connection this
    runs on, and the whole block is skipped once the constraint already names every status. Read
    the comment above that constant before adding anything else non-additive here; the reasoning is
    about atomicity and locks, not about idempotence alone.

    **`IF NOT EXISTS` does NOT make this concurrency-safe on its own** — see `startup_ddl_lock`,
    which is why the loop runs inside one. Adding a statement here needs no special care; adding a
    second PLACE that runs DDL against these tables needs the same lock.
    """
    with startup_ddl_lock(conn) as cur:
        for statement in _ALL_DDL:
            cur.execute(statement)
