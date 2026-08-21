"""The capture queue's contract: the durable DDL, the status machine, and the payload/hints shape
a submission takes.

DDL ownership: one idempotent pass (`CREATE TABLE IF NOT EXISTS` + `ADD COLUMN IF NOT EXISTS`) run
at startup by whoever opens the database — no migration framework. An index rebuild drops
`pages_index` by name; the tables here must survive it, and `DURABLE_TABLES` names them.

Nothing in a payload can set a server-computed field: `submitted_by` comes from the resolved caller
identity and is never read from client input — that structural fact is the security property, and
the refusals here are loud annotation on top of it.
"""
import dataclasses
import datetime
import hashlib
import logging
import re
from contextlib import contextmanager
from dataclasses import dataclass

from stigmergy.capture.errors import SubmissionRejected

log = logging.getLogger(__name__)

# ── the queue's vocabulary ────────────────────────────────────────────────────────────────────
QUEUED = "queued"
CLAIMED = "claimed"
FILED = "filed"
REJECTED = "rejected"
# LEGACY, read-only. A steward closed the row by hand, back when a capture could park on a
# person. Nothing writes it since the parked states retired: rows carrying it stay readable and
# purgeable, because rewriting a closed row's history would lie about what happened to it.
RESOLVED = "resolved"
FAILED = "failed"

STATUSES = (QUEUED, CLAIMED, FILED, REJECTED, RESOLVED, FAILED)

# The two states a capture used to wait on a person in, RETIRED: a name nothing resolves to is
# proposed as an entity and filed now (`librarian.identity`), and the steward confirms it from the
# inbox afterwards. Rows still in either state are returned to `queued` at startup
# (`_CAPTURE_QUEUE_PARKED_MIGRATION`) and re-filed under that rule; the status CHECK is swapped so
# the words cannot come back. Named rather than deleted so the migration and the CHECK guard spell
# them once.
RETIRED_STATUSES = ("needs_input", "triage")

# Terminal = will not move again on its own; retention purges terminal rows only.
TERMINAL_STATUSES = frozenset({FILED, REJECTED, RESOLVED, FAILED})

# States a claim can be finished into — every one terminal. `resolved` is absent: nothing reaches
# it any more.
FINISHED_STATUSES = frozenset({FILED, REJECTED, FAILED})

# ── why a refused row is where it is, as a CODE beside the sentence ───────────────────────────
# "May this row's material be echoed back" must branch on a code, never on prose. Declared here
# because the librarian writes the vocabulary and `capture` may not import `librarian`.
REASON_SECRET = "secret"
REASON_PII = "pii"
REASON_DUPLICATE = "duplicate"
REASON_STEERING = "steering"
# LEGACY, read-only: a steward declined a parked row by hand, when rows could park. Kept so the
# rows that carry it keep reading as a judgment call rather than a match — `queue._MATERIAL_WITHHELD`
# fails CLOSED on a `rejected` row with NO code. Not in `WITHHELD_REASONS`.
REASON_STEWARD = "steward"
# A drafted page whose frontmatter cannot be re-serialized after server-owned fields are stripped
# and restamped: content-caused, which is what routes it to `rejected` rather than `failed`.
REASON_MALFORMED_FRONTMATTER = "malformed-frontmatter"

REJECTION_REASONS = (REASON_SECRET, REASON_PII, REASON_DUPLICATE,
                     REASON_STEERING, REASON_STEWARD, REASON_MALFORMED_FRONTMATTER)
# The key the code travels under inside the `report` JSONB column.
REASON_CODE_KEY = "reason_code"

# The refusals whose point is that a value must not travel. Suppressed, not redacted: gitleaks
# reports a rule and a line, never a guaranteed span.
WITHHELD_REASONS = frozenset({REASON_SECRET, REASON_PII})

# What a read surface says in the excerpt's place — one sentence, in one place.
WITHHELD_MATERIAL_NOTE = (
    "withheld — this capture was refused as a secrets or personal-data match, so its own text and "
    "hint values are never echoed back; this row's refusal names what matched and where")

# ── the queue's echo window ───────────────────────────────────────────────────────────────────
# The secrets/PII gate runs only once a row leaves `claimed`, so the excerpt is withheld while
# `queued`/`claimed`. Keyed on "has the gate run", its own question, not on `TERMINAL_STATUSES`.
WITHHELD_PENDING_NOTE = (
    "not shown yet — nothing has scanned this material for secrets or personal data yet, so it "
    "stays out of this view while the capture is `queued` or `claimed`; it appears here as soon "
    "as the librarian has looked at it")

WITHHELD_UNSCANNED_NOTE = (
    "withheld — this capture's processing run failed before the secrets/personal-data scan "
    "reached it, so its material was never confirmed safe to echo back; unlike a queued capture "
    "this is not retried automatically, so ask an operator to look at this submission if you need "
    "to know what happened to it")

# The two states the gate has not yet run for — a different question from "is a human waited on".
GATE_NOT_YET_RUN_STATUSES = frozenset({QUEUED, CLAIMED})


def _reason_flagged(status: str, report) -> bool:
    """A secret/PII match, or a `rejected` row with no `reason_code` at all (fail-closed).

    `report` is whatever the JSONB column holds, which includes scalars and lists — only a dict
    can carry a reason code, and anything else carries none. Reading it unguarded crashed both
    queue read paths on such a row, where the SQL mirror (`report ->> 'reason_code'`, NULL on a
    non-object) had always answered calmly; the fail-closed branch below is what still withholds
    a `rejected` row whose report says nothing.
    """
    reason_code = report.get(REASON_CODE_KEY) if isinstance(report, dict) else None
    if reason_code in WITHHELD_REASONS:
        return True
    return status == REJECTED and reason_code is None


def withheld_reason(status: str, report) -> str:
    """THE sentence every read surface renders for withheld material. Priority: gate not yet run,
    then the `failed` residual, then a flagged match; every other state returns `""`.

    `report` is the raw JSONB value, not necessarily a dict — see `_reason_flagged`."""
    if status in GATE_NOT_YET_RUN_STATUSES:
        return WITHHELD_PENDING_NOTE
    if status == FAILED:
        return WITHHELD_UNSCANNED_NOTE
    return WITHHELD_MATERIAL_NOTE if _reason_flagged(status, report) else ""


# ── the `report` column's SHAPE ───────────────────────────────────────────────────────────────
# The vocabulary is the queue's; the wording belongs to the one writer, `librarian.report`.
SEARCHABILITY_NOTE = ("Becomes searchable at the next index rebuild or at the webhook's "
                      "incremental upsert, whichever lands first.")


def base_report(*, status: str, summary: str, **facts) -> dict:
    """THE shared shape: every terminal state goes through here, so none ships without the fields
    the others carry."""
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


# ── the row's own history: what HUMANS did to it ──────────────────────────────────────────────
# `trace` is a read-only record now: the events a human could perform on a row (ask, reply,
# requeue, resolve, reject) retired with the parked states, and the column keeps what was done
# before that so an old row still tells its story. The one writer left is the startup migration
# that returns a still-parked row to the queue, and it names itself in the event it appends.
MIGRATION_ACTOR = "migration"
MIGRATION_EVENT = "requeued"
MIGRATION_NOTE = ("the parked states retired — a capture about a name the registry does not know "
                  "is filed with the entity proposed, for a steward to confirm; re-filed under "
                  "that rule")

# ── what a steward types beside a decision ────────────────────────────────────────────────────
# One sentence quoted inside a sentence code composes — past this it reads as a document that
# lost its formatting.
MAX_NOTE_CHARS = 500


def clean_note(text: str, width: int = MAX_NOTE_CHARS) -> str:
    """THE seam every operator-typed string crosses on its way into a ledger row or a report:
    control characters stripped, newlines flattened, then clipped word-safe. Below every surface
    because a seam a caller can skip is not a seam — one CLI once cleaned its note while its
    sibling passed it raw, and ANSI escapes reached a reader's terminal. The exact expression
    `librarian.report._clean` uses, so the two packages' sentences render alike on one screen."""
    from stigmergy import text as textutil
    return textutil.clamp(textutil.sanitize(str(text or "")).replace("\n", " ").strip(), width)


# What `stigmergy-index --rebuild` must leave standing. `audit_log` is `server.audit`'s but named
# here: the assertion is about the DATABASE, not one package.
DURABLE_TABLES = ("capture_queue", "audit_log", "job_runs", "ingest_errors")

# ── the submission contract ───────────────────────────────────────────────────────────────────
# A kind names the SHAPE of the material and which reader claims the row, never a topic.
KINDS = ("raw", "page", "meeting", "drive")
RAW = "raw"
MEETING = "meeting"
# A Drive-fetched document: original bytes at blob_refs[1], the text manifest the row's material
# and dedup key at blob_refs[0]; the worker converts.
DRIVE = "drive"

# The kinds `brain_submit` may enqueue, where `kind` is MODEL-CHOSEN. Listed explicitly rather than
# left to `KINDS`: the drop CLIs are the only doors onto the meeting and drive flows.
MCP_SUBMIT_KINDS = ("raw", "page")

# The hard cap on captured text, in UTF-8 BYTES — what the row and the object store pay for.
MAX_MATERIAL_BYTES = 256 * 1024

# Hand-mirrors `stigmergy.server.service.MAX_ARG_CHARS` — `capture` may not import `server`.
MAX_HINT_CHARS = 8192

# Each hint kind is its own small, string-valued allowlist — additive, never a widened meaning for
# an existing key. The librarian reads hints as SUGGESTIONS.
HINT_KEYS = ("type", "path", "entity", "title")

# The Slack transport's capture provenance metadata.
SOURCE_HINT_KEYS = ("source_client", "source_permalink", "source_channel_id",
                    "source_channel_name", "source_thread_ts", "source_participants",
                    "source_message_timestamps")

# The meeting drop CLI's metadata: the date (every filed decision page's `as_of`), attendee names
# (hints, never identities) and a source label.
MEETING_HINT_KEYS = ("meeting_date", "attendees", "source_label")


# The subset of `SOURCE_HINT_KEYS` the fast lane TRUSTS: `source_client` turns the source-page
# attachment on, `source_permalink` lands as `url:` on a reader-facing page.
SOURCE_PROVENANCE_HINT_KEYS = frozenset({"source_client", "source_permalink"})

# The Slack door's name for itself, as `BrainService.door` spells it. Imported, never re-spelled.
SLACK_DOOR = "slack"

# The drive drop CLI's provenance: file id, display name (whose extension the worker converts by),
# webViewLink, mime type and modifiedTime.
DRIVE_HINT_KEYS = ("drive_file_id", "drive_name", "drive_url", "drive_mime", "drive_modified")

# The trusted pair, refused from EVERY client door: a hint a reader will trust must not be
# assertable from any client door. `drive_name` is required at the submit seam but trusted by
# nothing, a different property.
DRIVE_PROVENANCE_HINT_KEYS = frozenset({"drive_file_id", "drive_url"})

# The union `normalize_hints` checks a key against.
# A steward REGISTERING an entity (ADR 042): the two steward doors — the console's "Register an
# entity" and `stigmergy-entities create` — submit what the steward knows as a capture carrying
# these, and the librarian writes the entity's page from that material and the brain, born
# confirmed by the steward. Allowed here so `queue.submit` stores them; refused at the service
# seam for every client, because a registration is an act of authority and `brain_submit`
# attributes material, never authority.
REGISTER_HINT_KEYS = ("register_name", "register_type", "register_aliases", "register_source")
ALLOWED_HINT_KEYS = (HINT_KEYS + SOURCE_HINT_KEYS + MEETING_HINT_KEYS + DRIVE_HINT_KEYS
                     + REGISTER_HINT_KEYS)

# Fields a client may never assert: as an argument or a hint each is an explicit error; declared in
# a page's frontmatter each is recorded as a flagged hint and otherwise inert.
#
# **Every member MUST have a declared parameter on the `brain_submit` MCP tool** — FastMCP builds
# its argument model with `extra="ignore"`, so an undeclared field is stripped silently and
# declaring the parameter is the only way to REFUSE rather than quietly ignore.
ATTRIBUTION_FIELDS = frozenset({"submitted_by", "verification", "acl", "content_hash"})

# The queue's own columns, with NO tool parameter — listed so a caller addressing the queue by
# these names is refused loudly. NOT page frontmatter, where `id`/`status` are legitimate.
QUEUE_OWNED_COLUMNS = frozenset({
    "id", "status", "attempts", "blob_refs", "result_ref",
    "created_at", "claimed_at", "finished_at", "error",
})

# What `reject_server_owned_arguments` consults: a client may set none of them.
SERVER_OWNED_FIELDS = ATTRIBUTION_FIELDS | QUEUE_OWNED_COLUMNS

_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---[ \t]*(?:\n|$)", re.S)
_FM_LINE_RE = re.compile(r"^([A-Za-z_][\w.-]*)[ \t]*:[ \t]*(.*)$")


@dataclass(frozen=True)
class Submission:
    """A validated submission, ready to be archived and enqueued. Pure — `queue.submit` supplies
    the identity and Postgres the timestamps."""
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
    """`str` is wider than UTF-8: an unpaired surrogate has no encoding, and the digest, the blob
    and the payload all need bytes."""
    try:
        (material or "").encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def material_digest(material: str) -> tuple[str, int]:
    """`(sha256 hex, byte length)` of the material as archived. Shared with the server's audit-arg
    builder: the audit hash and the evidence key must be the same number or the trail breaks."""
    data = (material or "").encode("utf-8")
    return _sha256_hex(data), len(data)


def _present_and_plural(values: dict | None, keys) -> tuple[list[str], bool]:
    """Which of `keys` the caller actually set, sorted, and whether that is more than one. Only
    NON-None values count — a declared-but-unset parameter is not an assertion — and the three
    refusals below share this so they cannot disagree about what "present" means."""
    present = sorted(k for k, v in (values or {}).items() if v is not None and k in keys)
    return present, len(present) > 1


def reject_server_owned_arguments(args: dict) -> None:
    """Fail LOUDLY on any server-owned field arriving as a tool argument — the refusal the SDK
    cannot make. Only NON-None values count; the message names KEYS, never values. It can only
    refuse fields the caller's surface declares (see `ATTRIBUTION_FIELDS`)."""
    present, plural = _present_and_plural(args, SERVER_OWNED_FIELDS)
    if not present:
        return
    message = (f"{', '.join(present)} {'are' if plural else 'is'} set by the server, not by the "
               f"caller — remove {'them' if plural else 'it'} and resubmit")
    if "submitted_by" in present:
        message += (" (attribution comes from your resolved identity; submitting as someone else "
                    "requires their token, not their name)")
    raise SubmissionRejected(message)


def reject_source_provenance_hints(hints: dict | None, *, door: str) -> None:
    """Fail LOUDLY on `source_client`/`source_permalink` from any door but Slack's own, which would
    file a permalink nobody fetched on a reader-facing page. Called at `BrainService._submit`
    only, never inside `normalize_hints`/`queue.submit`."""
    if door == SLACK_DOOR:
        return
    present, plural = _present_and_plural(hints, SOURCE_PROVENANCE_HINT_KEYS)
    if not present:
        return
    raise SubmissionRejected(
        f"{', '.join(present)} {'are' if plural else 'is'} set by the Slack transport itself, "
        f"not by the caller — remove {'them' if plural else 'it'} and resubmit (this pair decides "
        f"whether the capture files a `sources/` page recording Slack provenance; asserting it "
        f"from another door would put a permalink nobody fetched on a reader-facing page)")


def normalize_hints(hints: dict | None, material: str) -> dict:
    """Validate the caller's hints and record what the material DECLARES about itself, as
    `{"client", "declared_frontmatter", "flagged"}`. The two checks consult DIFFERENT constants: a
    `hints` key addresses THIS QUEUE, so the whole `SERVER_OWNED_FIELDS` union is refused, while
    frontmatter describes a DOCUMENT, where `id`/`status` are ordinary page-contract fields."""
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
        # ATTRIBUTION_FIELDS, not the union — see the docstring.
        "flagged": sorted(k for k in declared if k in ATTRIBUTION_FIELDS),
    }


def declared_frontmatter(material: str) -> dict[str, str]:
    """Top-level `key: value` pairs of a leading `---` block. Deliberately not YAML: the input is
    attacker-controlled and `yaml.safe_load` still expands anchors/aliases. Best-effort by design
    — attribution never reads this dict, so a missed flag loses a note, not a guarantee."""
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


# Beside `fromisoformat`, which also accepts basic-format `20260729` on 3.11+.
_MEETING_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def validate_meeting_date(value: str) -> str:
    """A meeting date, refused unless it is a real `YYYY-MM-DD` calendar date. Raises
    `SubmissionRejected`, so "no row and no object" holds here too; `prepare_submission` is the
    seam every caller crosses, the CLI's early call a convenience."""
    text = str(value or "").strip()
    if not _MEETING_DATE_RE.match(text):
        raise SubmissionRejected(
            f"a meeting date must be YYYY-MM-DD (got {value!r}) — it becomes `as_of` on every "
            f"decision page this meeting files")
    try:
        datetime.date.fromisoformat(text)
    except ValueError:
        raise SubmissionRejected(f"{value!r} is not a real calendar date") from None
    return text


def reject_drive_provenance_hints(hints: dict | None) -> None:
    """Fail LOUDLY on `drive_file_id`/`drive_url` from ANY client door: the one legitimate asserter
    (`stigmergy-drive drop`) never passes through `BrainService._submit`."""
    present, plural = _present_and_plural(hints, DRIVE_PROVENANCE_HINT_KEYS)
    if not present:
        return
    raise SubmissionRejected(
        f"{', '.join(present)} {'are' if plural else 'is'} set by the stigmergy-drive operator "
        f"CLI itself, not by a caller — remove {'them' if plural else 'it'} and resubmit (Drive "
        f"provenance on a reader-facing source page must come from a fetch that actually "
        f"happened)")


def reject_registration_hints(hints: dict | None) -> None:
    """Fail LOUDLY on any `register_*` hint from ANY client door: the two legitimate asserters
    (the console's Register an entity, `stigmergy-entities create`) never pass through
    `BrainService._submit`, and a registration is a steward's act, not a submitter's suggestion."""
    present, plural = _present_and_plural(hints, REGISTER_HINT_KEYS)
    if not present:
        return
    raise SubmissionRejected(
        f"{', '.join(present)} {'are' if plural else 'is'} set by the steward doors themselves "
        f"(the console's Register an entity, `stigmergy-entities create`), not by a caller — "
        f"remove {'them' if plural else 'it'} and resubmit. To introduce an entity, capture what "
        f"you know about it: the librarian proposes it and a steward confirms")


@dataclasses.dataclass(frozen=True)
class Registration:
    """What a steward asked the librarian to register, read off a capture's hints: the entity's
    name and type as the steward spelled them, the spellings they listed, and which door asked
    (the ledger's `source`). The capture's `submitted_by` is the steward, and therefore the
    `approved_by` the page is born with."""
    name: str
    entity_type: str
    aliases: tuple
    source: str


# The doors a registration can come from — the ledger's own spellings (`capture.decisions` names
# the same two, and refuses any other after the push, which is too late to learn it).
REGISTRATION_SOURCES = ("admin", "cli")


def registration_hints(*, name: str, entity_type: str, aliases=(), source: str) -> dict:
    """The hints a steward door submits with a registration — built here so both doors build the
    same ones. `entity` rides along as the ordinary hint it is, so the haystack the proposal writer
    checks a proposed name against carries the steward's spelling."""
    if source not in REGISTRATION_SOURCES:
        raise ValueError(f"a registration comes from one of {', '.join(REGISTRATION_SOURCES)}, "
                         f"not {source!r} — the ledger records the door by that name")
    return {"entity": name, "register_name": name, "register_type": entity_type,
            "register_aliases": ", ".join(a for a in aliases if a), "register_source": source}


def registration_from_hints(hints: dict | None) -> Registration | None:
    """The registration a stored capture carries, or `None` for every ordinary capture."""
    client = (hints or {}).get("client") or {}
    name = " ".join(str(client.get("register_name") or "").split())
    if not name:
        return None
    aliases = tuple(a.strip() for a in str(client.get("register_aliases") or "").split(",")
                    if a.strip())
    return Registration(name=name, entity_type=str(client.get("register_type") or "").strip().lower(),
                        aliases=aliases, source=str(client.get("register_source") or "").strip())


def _require_drive_hints(client: dict) -> None:
    """`kind == DRIVE` requires both hints at THIS seam, which every caller of `queue.submit`
    crosses. `drive_name` carries the extension conversion dispatches on; missing, it falls
    through to the `text` method and files a PDF's raw bytes as prose."""
    if not str(client.get("drive_file_id") or "").strip():
        raise SubmissionRejected(
            "a drive submission requires hints['drive_file_id'] — the Drive file this capture "
            "was fetched from")
    if not str(client.get("drive_name") or "").strip():
        raise SubmissionRejected(
            "a drive submission requires hints['drive_name'] — the file's display name; its "
            "extension is what the worker's conversion dispatches on")


def _require_meeting_hints(client: dict) -> None:
    """`kind == MEETING` requires both at THIS seam, which every caller of `queue.submit` crosses:
    a missing `meeting_date` silently degrades every filed page's `as_of` to today."""
    if not str(client.get("title") or "").strip():
        raise SubmissionRejected(
            "a meeting submission requires hints['title'] — the meeting's title, used as this "
            "capture's source and meeting page identity")
    validate_meeting_date(client.get("meeting_date") or "")


def prepare_submission(kind: str, material: str, hints: dict | None = None) -> Submission:
    """Validate a submission and build its payload/hints. Raises `SubmissionRejected` BEFORE any
    blob or row is written — that ordering is what makes "no row and no blob" true."""
    if kind not in KINDS:
        raise SubmissionRejected(f"unknown kind {kind!r} (allowed: {', '.join(KINDS)})")
    if not isinstance(material, str) or not material.strip():
        raise SubmissionRejected("material is empty — there is nothing to capture")
    # A lone surrogate is legal `str` that survives JSON-RPC; unrefused, `.encode()` raises
    # `UnicodeEncodeError`, which every door's `except CaptureError` misses.
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


def sql_literals(values) -> str:
    """A fixed, importable set of words as a SQL literal list — `'a', 'b'`, sorted so the emitted
    statement is the same on every process. For VOCABULARIES declared in this module only: they
    are code, never input, which is what makes interpolating them instead of binding them safe.
    """
    return ", ".join(f"'{v}'" for v in sorted(values))


# ── DDL (idempotent; owned here, never dropped by an index rebuild) ───────────────────────────
# NAMED because adding a status must replace it on existing tables, and the name is exactly what
# Postgres derives for an unnamed column check here — so the two are the same object.
_STATUS_CHECK_NAME = "capture_queue_status_check"
# Declaration order, not `sql_literals`' sorted one: `STATUSES` is the vocabulary in the order a
# reader meets it, and this CHECK is the one place that order is already committed to a database.
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
# `report` — the librarian's structured account of one item, NULL when never processed. `error`
# beside it keeps its one-line human meaning.
_CAPTURE_QUEUE_REPORT_COLUMN = """
ALTER TABLE capture_queue ADD COLUMN IF NOT EXISTS report JSONB
"""

# The retired human loop's columns, still CREATED on a fresh database and never dropped: the
# rows written while captures could park keep their `asked_at`, `parked_at`, `reply`, `trace` and
# `outcome`, and a reader of an old row must find the columns it expects. Nothing writes the first
# four any more; `trace` gains one event per migrated row (below) and nothing after.
_CAPTURE_QUEUE_HUMAN_LOOP_COLUMNS = (
    "ALTER TABLE capture_queue ADD COLUMN IF NOT EXISTS asked_at TIMESTAMPTZ",
    "ALTER TABLE capture_queue ADD COLUMN IF NOT EXISTS parked_at TIMESTAMPTZ",
    "ALTER TABLE capture_queue ADD COLUMN IF NOT EXISTS reply TEXT",
    "ALTER TABLE capture_queue ADD COLUMN IF NOT EXISTS trace JSONB",
)
_CAPTURE_QUEUE_OUTCOME_COLUMN = (
    "ALTER TABLE capture_queue ADD COLUMN IF NOT EXISTS outcome JSONB"
)

# A row still parked when the parked states retired goes back to the queue, to be filed under the
# rule that replaced the park — nothing a person was waiting on is lost, it is simply filed. Runs
# BEFORE the CHECK swap below, which would otherwise refuse to constrain a table holding a word it
# no longer lists; a no-op on every later start, because no row can enter the retired states.
_RETIRED_LITERALS = ", ".join(f"'{s}'" for s in RETIRED_STATUSES)
_CAPTURE_QUEUE_PARKED_MIGRATION = f"""
UPDATE capture_queue
SET status = '{QUEUED}', error = '', claimed_at = NULL, parked_at = NULL,
    trace = COALESCE(trace, '[]'::jsonb) || jsonb_build_array(jsonb_build_object(
        'at', to_jsonb(now()), 'event', '{MIGRATION_EVENT}', 'actor', '{MIGRATION_ACTOR}',
        'note', '{MIGRATION_NOTE}'))
WHERE status IN ({_RETIRED_LITERALS})
"""

# The one non-additive migration: a status that arrives or retires must REPLACE this CHECK, which
# cannot be edited in place. It must stay ONE `DO` statement — one transaction even on the
# autocommit connection this runs on. As a DROP-then-ADD pair it reintroduces three failures: the
# live queue is briefly unconstrained, every process start takes the ACCESS EXCLUSIVE lock, and
# two concurrent starters race into `DuplicateObject`. The guard skips the swap once the constraint
# names every status in `STATUSES` and none in `RETIRED_STATUSES` (`quote_literal` keeps one name
# from matching inside another) — both halves, or a constraint still admitting a retired word would
# read as current forever.
_CAPTURE_QUEUE_STATUS_CHECK = f"""
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint c
        WHERE c.conrelid = 'capture_queue'::regclass
          AND c.conname = '{_STATUS_CHECK_NAME}'
          AND (SELECT bool_and(pg_get_constraintdef(c.oid) LIKE '%' || quote_literal(s) || '%')
               FROM unnest(ARRAY[{_STATUS_LITERALS}]) AS s)
          AND NOT (SELECT bool_or(pg_get_constraintdef(c.oid) LIKE '%' || quote_literal(r) || '%')
                   FROM unnest(ARRAY[{_RETIRED_LITERALS}]) AS r)
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
# The expiry sweep's index: the worker sweeps on EVERY claim, which is what earns it one.
_QUEUE_CLAIMED_INDEX = """
CREATE INDEX IF NOT EXISTS capture_queue_status_claimed_idx
    ON capture_queue (status, claimed_at)
"""

# `job_runs` records each processing run, `ingest_errors` each failed item with stage and attempts.
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
            _CAPTURE_QUEUE_PARKED_MIGRATION, _CAPTURE_QUEUE_STATUS_CHECK,
            _QUEUE_STATUS_INDEX, _QUEUE_SUBMITTER_INDEX, _QUEUE_CLAIMED_INDEX,
            _JOB_RUNS_DDL, _JOB_RUNS_INDEX, _INGEST_ERRORS_DDL, _INGEST_ERRORS_INDEX)


# ── the whole DDL run as ONE critical section ─────────────────────────────────────────────────
# `IF NOT EXISTS` is a check, not a lock: two sessions can both pass it and the loser dies with
# `UniqueViolation`/`DuplicateTable`, which cannot be told from a genuine schema conflict. The key
# must stay a LITERAL — derived from the DDL text, two releases mid-rollout would take DIFFERENT
# locks. It is the whole DATABASE's key: `server.audit.ensure_audit_table` takes the same one.
_STARTUP_DDL_LOCK_KEY = int.from_bytes(b"SYNCDDL", "big")


@contextmanager
def startup_ddl_lock(conn):
    """Hold the startup-DDL advisory lock for the block, yielding a cursor to run the DDL on.
    Session-scoped, not xact-scoped: on this autocommit connection a transaction-scoped lock would
    be released by the statement that took it, which is why the `finally` is load-bearing."""
    with conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_lock(%s::bigint)", (_STARTUP_DDL_LOCK_KEY,))
        try:
            yield cur
        finally:
            try:
                cur.execute("SELECT pg_advisory_unlock(%s::bigint)", (_STARTUP_DDL_LOCK_KEY,))
            except Exception:  # noqa: BLE001 — cleanup must never mask the DDL failure above
                log.warning("could not release the startup-DDL advisory lock; it is released when "
                            "this connection closes", exc_info=True)


def ensure_capture_schema(conn) -> None:
    """Idempotent DDL for the durable write-path tables — safe on every startup and from two
    processes at once. Never drops a TABLE; the one drop is the status-CHECK swap, and a second
    PLACE that runs DDL needs the same lock."""
    with startup_ddl_lock(conn) as cur:
        for statement in _ALL_DDL:
            cur.execute(statement)
