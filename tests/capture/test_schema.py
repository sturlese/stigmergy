"""`stigmergy.capture.schema` — the pure submission contract. No database, no network, keyless:
`prepare_submission`/`reject_server_owned_arguments`/`normalize_hints` are all plain functions
over strings and dicts, so nothing here needs an API key.

Two properties start here — attribution cannot be forged, and forged frontmatter is inert. This
is where the refusal and the flagging are actually decided; the adversarial cat. 7 suite
(`tests/capture/test_adversarial_cat7.py`) exercises the security-relevant edges of this same
module in more depth and is the permanent home of the forged-frontmatter case.
"""
import hashlib

import pytest

from stigmergy.capture import schema
from stigmergy.capture.errors import ReplyRejected, SubmissionRejected


# ── the vocabulary itself: these constants are the contract other modules (the queue DDL, the
# librarian) build against, so a drift here is exactly what this test is for ─────────────────────
def test_the_status_vocabulary_is_written_in_full():
    # `resolved` — a steward's disposition outside the fast lane — sits between `rejected` and
    # `needs_input`, matching `schema.py`'s own declaration order.
    assert schema.STATUSES == ("queued", "claimed", "filed", "rejected", "resolved",
                               "needs_input", "triage", "failed")


def test_terminal_statuses_are_exactly_the_four_finished_states():
    # `resolved` belongs to the terminal set — a steward-handled row is done in exactly the sense
    # retention means (it purges `resolved` on the ordinary window).
    assert {"filed", "rejected", "resolved", "failed"} == schema.TERMINAL_STATUSES


def test_finished_statuses_add_the_parked_pair_but_never_resolved():
    """`FINISHED_STATUSES` is what `queue.finish()` — the LEASE-FENCED transition — may finish a
    claim into. `resolved` is deliberately absent from it even though it is terminal: it is a
    steward's disposition on a row nobody holds a lease on, so it must not be reachable through a
    transition that requires an `expected_attempts` fence the steward path does not hold
    (`schema.py`'s own `FINISHED_STATUSES` docstring). It has its own guarded transition instead
    (`queue.dispose`)."""
    assert schema.TERMINAL_STATUSES - {"resolved"} | {"needs_input", "triage"} \
        == schema.FINISHED_STATUSES
    assert "resolved" not in schema.FINISHED_STATUSES
    # the parked pair are NOT terminal — retention must never purge material a human is waiting on
    assert "needs_input" not in schema.TERMINAL_STATUSES
    assert "triage" not in schema.TERMINAL_STATUSES


def test_durable_tables_names_exactly_the_four_the_index_rebuild_must_not_take():
    assert schema.DURABLE_TABLES == ("capture_queue", "audit_log", "job_runs", "ingest_errors")


def test_kinds_are_raw_page_meeting_and_drive():
    """`"meeting"` and `"drive"` (ADR 028) name the shape of the material and the flow that reads
    it, per `schema.KINDS`'s own docstring, never a topic. A contract change here is tracked
    rather than silent: each drop CLI is the only writer of its kind."""
    assert schema.KINDS == ("raw", "page", "meeting", "drive")
    assert schema.MEETING == "meeting"
    assert schema.DRIVE == "drive"


def test_mcp_submit_kinds_excludes_meeting_and_drive():
    """`brain_submit` (`kind` a MODEL-CHOSEN MCP argument) must not accept every value `KINDS`
    grows to include — `"meeting"` and `"drive"` are the drop CLIs' own kinds, and each CLI is
    "the only door" onto its flow."""
    assert schema.MCP_SUBMIT_KINDS == ("raw", "page")
    assert schema.MEETING not in schema.MCP_SUBMIT_KINDS
    assert schema.DRIVE not in schema.MCP_SUBMIT_KINDS


# ── kind=="meeting" is validated at the ENQUEUE SEAM, not only inside the drop CLI's own early
# copy — every caller of `queue.submit` passes through `prepare_submission` ─────────────────────
def test_prepare_submission_refuses_a_meeting_with_no_title_hint():
    with pytest.raises(SubmissionRejected, match="title"):
        schema.prepare_submission(schema.MEETING, "a transcript",
                                  {"meeting_date": "2026-07-29"})


def test_prepare_submission_refuses_a_meeting_with_no_meeting_date_hint():
    with pytest.raises(SubmissionRejected, match="meeting date"):
        schema.prepare_submission(schema.MEETING, "a transcript", {"title": "Q3 sync"})


def test_prepare_submission_refuses_a_malformed_meeting_date_hint():
    """The steered-MCP-session shape: a `meeting_date` carrying a newline (an
    attempt to smuggle a second frontmatter-looking line into what becomes `as_of`) is refused by
    the SAME shape check the CLI's `--date` flag goes through, not silently accepted."""
    with pytest.raises(SubmissionRejected, match="YYYY-MM-DD"):
        schema.prepare_submission(schema.MEETING, "a transcript",
                                  {"title": "Q3 sync", "meeting_date": "2026-01-01\nfoo: bar"})


def test_prepare_submission_accepts_a_well_formed_meeting():
    """The benign twin: a meeting submission carrying both required hints, well-formed, is not
    refused by this rule."""
    submission = schema.prepare_submission(schema.MEETING, "a transcript",
                                           {"title": "Q3 sync", "meeting_date": "2026-07-29"})
    assert submission.kind == schema.MEETING
    assert submission.hints["client"]["meeting_date"] == "2026-07-29"


def test_prepare_submission_does_not_require_meeting_hints_for_other_kinds():
    """The benign twin's other half: an ordinary `raw`/`page` submission is never asked for a
    title/meeting_date it has no business carrying."""
    submission = schema.prepare_submission("raw", "an ordinary capture", {})
    assert submission.kind == "raw"


# ── ADR 028: kind=="drive" is validated at the SAME enqueue seam, day one ───────────────────────
def test_prepare_submission_refuses_a_drive_row_with_no_file_id_hint():
    with pytest.raises(SubmissionRejected, match="drive_file_id"):
        schema.prepare_submission(schema.DRIVE, "a manifest", {"drive_name": "deck.pdf"})


def test_prepare_submission_refuses_a_drive_row_with_no_name_hint():
    """`drive_name` carries the extension conversion dispatches on — absent, it would not fail
    closed (the `text` fallback would file a PDF's raw bytes as prose), so it is required here."""
    with pytest.raises(SubmissionRejected, match="drive_name"):
        schema.prepare_submission(schema.DRIVE, "a manifest", {"drive_file_id": "X"})


def test_prepare_submission_accepts_a_well_formed_drive_row():
    submission = schema.prepare_submission(schema.DRIVE, "a manifest",
                                           {"drive_file_id": "X", "drive_name": "deck.pdf"})
    assert submission.kind == schema.DRIVE


def test_reject_drive_provenance_hints_refuses_the_trusted_pair_and_only_it():
    """The trusted-subset pattern, third application (ADR 028 D7): the two keys a downstream
    reader trusts are refused loudly; the three plain-metadata drive hints stay ordinary
    suggestions."""
    with pytest.raises(SubmissionRejected, match="drive_file_id, drive_url"):
        schema.reject_drive_provenance_hints({"drive_file_id": "X", "drive_url": "https://x"})
    schema.reject_drive_provenance_hints({"drive_mime": "application/pdf",
                                          "drive_modified": "2026-08-01", "drive_name": "a.pdf"})
    schema.reject_drive_provenance_hints(None)


def test_hint_keys_are_the_four_allowlisted_names():
    assert schema.HINT_KEYS == ("type", "path", "entity", "title")


def test_server_owned_fields_names_the_four_attribution_fields():
    for field in ("submitted_by", "verification", "acl", "content_hash"):
        assert field in schema.SERVER_OWNED_FIELDS


# ── material_digest: the ONE hash definition the audit row and the evidence key both use ────────
def test_material_digest_matches_hashlib_sha256_of_the_utf8_bytes():
    digest, size = schema.material_digest("hello brain")
    assert digest == hashlib.sha256(b"hello brain").hexdigest()
    assert size == len(b"hello brain")


def test_material_digest_counts_utf8_bytes_not_characters():
    """A single accented character is one CHARACTER but two BYTES in UTF-8 — the cap and the hash
    both have to be byte-based, because bytes are what the database and the object store pay
    for."""
    text = "é"  # U+00E9, 2 bytes in UTF-8
    digest, size = schema.material_digest(text)
    assert size == 2
    assert digest == hashlib.sha256("é".encode()).hexdigest()


# ── reject_server_owned_arguments: the loud half of "attribution cannot be forged" ──────────────
def test_reject_server_owned_arguments_allows_the_ordinary_call():
    """The ordinary call never sets ANY of these — `submitted_by=None` is the default, never sent
    by a well-behaved client. None values must never trip the refusal."""
    schema.reject_server_owned_arguments({"submitted_by": None})   # must not raise


def test_reject_server_owned_arguments_refuses_a_non_none_submitted_by():
    with pytest.raises(SubmissionRejected, match="submitted_by"):
        schema.reject_server_owned_arguments({"submitted_by": "ceo@example.com"})


@pytest.mark.parametrize("field", sorted(schema.SERVER_OWNED_FIELDS))
def test_reject_server_owned_arguments_refuses_every_named_field(field):
    with pytest.raises(SubmissionRejected, match=field):
        schema.reject_server_owned_arguments({field: "anything"})


def test_reject_server_owned_arguments_lists_every_offending_key_sorted():
    with pytest.raises(SubmissionRejected) as exc_info:
        schema.reject_server_owned_arguments({"verification": "verified", "acl": "['x']"})
    assert "acl, verification" in str(exc_info.value)   # sorted, both named


def test_reject_server_owned_arguments_message_never_echoes_the_forged_value():
    """Safety: the message is echoed to the caller verbatim over HTTP — it may name the KEY,
    never the value the caller tried to claim (a forged identity name is somebody's identity)."""
    with pytest.raises(SubmissionRejected) as exc_info:
        schema.reject_server_owned_arguments({"submitted_by": "ceo@example.com"})
    assert "ceo@example.com" not in str(exc_info.value)


def test_reject_server_owned_arguments_ignores_ordinary_unrelated_keys():
    schema.reject_server_owned_arguments({"kind": "raw", "material": "text"})   # must not raise


# ── verification/acl/content_hash join submitted_by as declared, refusable fields ───────────────
@pytest.mark.parametrize("field", ["verification", "acl", "content_hash"])
def test_reject_server_owned_arguments_refuses_each_of_the_three_trust_fields_alone(field):
    with pytest.raises(SubmissionRejected, match=field):
        schema.reject_server_owned_arguments({field: "anything", **{
            k: None for k in ("submitted_by", "verification", "acl", "content_hash") if k != field}})


def test_reject_server_owned_arguments_pluralizes_correctly_for_a_single_field():
    with pytest.raises(SubmissionRejected) as exc_info:
        schema.reject_server_owned_arguments({"verification": "verified"})
    message = str(exc_info.value)
    assert "verification is set by the server" in message
    assert "remove it and resubmit" in message
    assert " are " not in message and "remove them" not in message


def test_reject_server_owned_arguments_pluralizes_correctly_for_all_four_at_once():
    with pytest.raises(SubmissionRejected) as exc_info:
        schema.reject_server_owned_arguments({"submitted_by": "ceo@example.com",
                                              "verification": "verified", "acl": ["leadership"],
                                              "content_hash": "deadbeef"})
    message = str(exc_info.value)
    assert message.startswith("acl, content_hash, submitted_by, verification are set by the "
                              "server, not by the caller — remove them and resubmit")
    # the attribution-specific parenthetical is appended only because submitted_by is among them
    assert "submitting as someone else requires their token" in message


def test_reject_server_owned_arguments_attribution_parenthetical_only_when_submitted_by_present():
    """`verification`/`acl`/`content_hash` alone are trust/access claims, not attribution — the
    'submitting as someone else requires their token' sentence is specific to `submitted_by` and
    must not be tacked onto a refusal that never mentioned it."""
    with pytest.raises(SubmissionRejected) as exc_info:
        schema.reject_server_owned_arguments({"verification": "verified", "acl": ["leadership"],
                                              "content_hash": "deadbeef"})
    message = str(exc_info.value)
    assert "submitted_by" not in message
    assert "token" not in message and "someone else" not in message


# ── reject_source_provenance_hints: the 🧠 source page's trigger cannot be forged ────────────────
def test_reject_source_provenance_hints_allows_the_ordinary_call_neither_key_sent():
    schema.reject_source_provenance_hints(None, door="")                 # must not raise
    schema.reject_source_provenance_hints({}, door="")                   # must not raise
    schema.reject_source_provenance_hints({"type": "note"}, door="")     # unrelated, ignored


@pytest.mark.parametrize("hints", [
    {"source_client": "slack"},
    {"source_permalink": "https://example.slack.com/archives/C1/p1"},
    {"source_client": "slack",
     "source_permalink": "https://example.slack.com/archives/C1/p1"},
], ids=["source_client-alone", "source_permalink-alone", "both-together"])
def test_reject_source_provenance_hints_refuses_both_keys_from_a_clientfacing_door(hints):
    with pytest.raises(SubmissionRejected):
        schema.reject_source_provenance_hints(hints, door="")


def test_reject_source_provenance_hints_accepts_everything_from_the_slack_door():
    """The whole point of `door`: the Slack transport composes these hints itself, in server code,
    and must keep submitting them through the SAME `BrainService.submit` seam it has always used."""
    hints = {"source_client": "slack",
             "source_permalink": "https://example.slack.com/archives/C1/p1",
             "source_channel_id": "C1"}
    schema.reject_source_provenance_hints(hints, door=schema.SLACK_DOOR)     # must not raise


def test_reject_source_provenance_hints_names_the_keys_and_never_echoes_the_value():
    """Same posture as both sibling refusals: the message names the KEY, never the claim."""
    with pytest.raises(SubmissionRejected, match="source_permalink") as exc_info:
        schema.reject_source_provenance_hints(
            {"source_permalink": "https://evil.example/forged"}, door="")
    assert "evil.example" not in str(exc_info.value)


def test_reject_source_provenance_hints_leaves_the_five_untrusted_source_hints_alone():
    """The scope decision, falsifiable: only the two keys something downstream TRUSTS are refused
    — `source_client` switches the attachment on, `source_permalink` lands as `url:` on a
    reader-facing page. The other five source hints stay ordinary suggestions and must sail
    through from any door."""
    schema.reject_source_provenance_hints(
        {"source_channel_id": "C1", "source_channel_name": "dealflow",
         "source_thread_ts": "1722.1", "source_participants": "Dana Ruiz",
         "source_message_timestamps": "1722.1, 1722.2"}, door="")             # must not raise


def test_reject_source_provenance_hints_none_values_are_the_ordinary_unset_shape():
    schema.reject_source_provenance_hints(
        {"source_client": None, "source_permalink": None}, door="")           # must not raise


def test_source_provenance_hint_keys_is_exactly_the_pair_the_writer_trusts():
    """Pinned so a future source hint joining the refusal must be a reviewed decision that
    something downstream started trusting it."""
    assert frozenset({"source_client", "source_permalink"}) == schema.SOURCE_PROVENANCE_HINT_KEYS


# ── normalize_hints: the client-hints allowlist + the frontmatter recording ─────────────────────
def test_normalize_hints_accepts_the_allowed_keys():
    out = schema.normalize_hints({"type": "decision", "title": "t"}, "plain material")
    assert out["client"] == {"type": "decision", "title": "t"}
    assert out["declared_frontmatter"] == {}
    assert out["flagged"] == []


def test_normalize_hints_defaults_none_to_the_empty_client_dict():
    out = schema.normalize_hints(None, "plain material")
    assert out["client"] == {}


def test_normalize_hints_refuses_a_server_owned_key():
    with pytest.raises(SubmissionRejected, match="submitted_by"):
        schema.normalize_hints({"submitted_by": "ceo@example.com"}, "m")


def test_normalize_hints_refuses_an_unknown_key_and_lists_the_allowed_ones():
    with pytest.raises(SubmissionRejected) as exc_info:
        schema.normalize_hints({"nickname": "x"}, "m")
    message = str(exc_info.value)
    assert "nickname" in message
    for key in schema.HINT_KEYS:
        assert key in message


def test_normalize_hints_refuses_a_non_string_value():
    with pytest.raises(SubmissionRejected, match="must be a string"):
        schema.normalize_hints({"title": 12345}, "m")


def test_normalize_hints_refuses_an_oversized_value():
    with pytest.raises(SubmissionRejected, match="too long"):
        schema.normalize_hints({"title": "x" * (schema.MAX_HINT_CHARS + 1)}, "m")


def test_normalize_hints_accepts_a_value_at_exactly_the_boundary():
    out = schema.normalize_hints({"title": "x" * schema.MAX_HINT_CHARS}, "m")
    assert out["client"]["title"] == "x" * schema.MAX_HINT_CHARS


def test_normalize_hints_drops_a_none_valued_key_silently():
    out = schema.normalize_hints({"title": None, "type": "decision"}, "m")
    assert out["client"] == {"type": "decision"}


def test_normalize_hints_records_declared_frontmatter_and_flags_the_server_owned_subset():
    material = "---\nsubmitted_by: ceo@example.com\ntype: decision\n---\n\nbody\n"
    out = schema.normalize_hints(None, material)
    assert out["declared_frontmatter"] == {"submitted_by": "ceo@example.com", "type": "decision"}
    assert out["flagged"] == ["submitted_by"]                 # only the server-owned subset
    assert out["client"] == {}                                # client hints untouched by the scan


# ── the ATTRIBUTION_FIELDS/QUEUE_OWNED_COLUMNS split, applied to `flagged`: a page's own `id`/
# `status` are legitimate page-contract fields (corpus.PageRow.page_id reads frontmatter `id`;
# `status` is the draft/developing/canonical lifecycle) — flagging them would falsely accuse an
# ordinary, honest page of forgery. Only ATTRIBUTION_FIELDS is flagged; the hints-KEY check below
# still consults the full union, because a `hints` key IS the client addressing this queue. ────
def test_normalize_hints_does_not_flag_a_pages_own_id_or_status_in_frontmatter():
    material = "---\nid: initech-2026-kpi\nstatus: developing\ntype: report\n---\n\nbody\n"
    out = schema.normalize_hints(None, material)
    assert out["declared_frontmatter"] == {
        "id": "initech-2026-kpi", "status": "developing", "type": "report"}
    assert out["flagged"] == []                    # recorded, but never accused


def test_normalize_hints_still_flags_real_attribution_fields_alongside_a_pages_own_id_and_status():
    material = ("---\nid: initech-2026-kpi\nstatus: developing\nsubmitted_by: ceo@example.com\n"
               "acl: [leadership]\n---\n\nbody\n")
    out = schema.normalize_hints(None, material)
    assert out["flagged"] == ["acl", "submitted_by"]   # id/status never join, forged fields still do


def test_normalize_hints_hints_key_still_refuses_every_queue_owned_column_despite_never_flagging_it():
    """The asymmetry is intentional, not an oversight — both halves are load-bearing and both are
    tested: frontmatter never flags a queue-internal column (above), but a `hints` KEY of the same
    name is still a client addressing the QUEUE and is still refused loudly."""
    for field in sorted(schema.QUEUE_OWNED_COLUMNS):
        with pytest.raises(SubmissionRejected, match=field):
            schema.normalize_hints({field: "anything"}, "m")


def test_normalize_hints_hints_key_still_refuses_every_attribution_field_too():
    for field in sorted(schema.ATTRIBUTION_FIELDS):
        with pytest.raises(SubmissionRejected, match=field):
            schema.normalize_hints({field: "anything"}, "m")


# ── declared_frontmatter: the deliberately-non-YAML scan ────────────────────────────────────────
def test_declared_frontmatter_returns_empty_without_a_leading_delimiter():
    assert schema.declared_frontmatter("no frontmatter here\njust body text") == {}


def test_declared_frontmatter_parses_flat_top_level_pairs():
    material = "---\nsubmitted_by: ceo@example.com\nverification: verified\ntype: decision\n---\n\nbody\n"
    assert schema.declared_frontmatter(material) == {
        "submitted_by": "ceo@example.com", "verification": "verified", "type": "decision"}


def test_declared_frontmatter_ignores_indented_and_list_and_comment_lines():
    material = ("---\n"
                "acl:\n"
                "  - leadership\n"
                "# a comment line\n"
                "type: decision\n"
                "---\n\nbody\n")
    assert schema.declared_frontmatter(material) == {"acl": "", "type": "decision"}


def test_declared_frontmatter_truncates_an_oversized_value():
    material = f"---\ntitle: {'x' * (schema.MAX_HINT_CHARS + 50)}\n---\n\nbody\n"
    out = schema.declared_frontmatter(material)
    assert len(out["title"]) == schema.MAX_HINT_CHARS


def test_declared_frontmatter_ignores_a_line_with_no_colon():
    material = "---\nnot a key value line\ntype: decision\n---\n\nbody\n"
    assert schema.declared_frontmatter(material) == {"type": "decision"}


# ── prepare_submission: the whole contract wired together ───────────────────────────────────────
def test_prepare_submission_rejects_an_unknown_kind():
    with pytest.raises(SubmissionRejected, match="unknown kind"):
        schema.prepare_submission("audio", "material")


def test_prepare_submission_rejects_empty_material():
    with pytest.raises(SubmissionRejected, match="empty"):
        schema.prepare_submission("raw", "")


def test_prepare_submission_rejects_whitespace_only_material():
    with pytest.raises(SubmissionRejected, match="empty"):
        schema.prepare_submission("raw", "   \n\t  ")


def test_prepare_submission_rejects_non_string_material():
    with pytest.raises(SubmissionRejected):
        schema.prepare_submission("raw", None)   # type: ignore[arg-type]


def test_prepare_submission_rejects_material_over_the_byte_cap():
    huge = "x" * (schema.MAX_MATERIAL_BYTES + 1)
    with pytest.raises(SubmissionRejected, match="too large"):
        schema.prepare_submission("raw", huge)


def test_prepare_submission_accepts_material_at_exactly_the_byte_cap():
    exact = "x" * schema.MAX_MATERIAL_BYTES
    submission = schema.prepare_submission("raw", exact)
    assert submission.size == schema.MAX_MATERIAL_BYTES


def test_prepare_submission_builds_the_expected_payload_and_digest():
    submission = schema.prepare_submission("raw", "a decision was made")
    digest, size = schema.material_digest("a decision was made")
    assert submission.kind == "raw"
    assert submission.digest == digest
    assert submission.size == size
    assert submission.payload == {"kind": "raw", "text": "a decision was made",
                                  "sha256": digest, "bytes": size}
    assert submission.hints == {"client": {}, "declared_frontmatter": {}, "flagged": []}


def test_prepare_submission_stores_the_material_verbatim_even_with_forged_frontmatter():
    """The security-load-bearing behavior: the material is archived AS-IS — flagging never
    rewrites or strips the payload."""
    material = "---\nsubmitted_by: ceo@example.com\n---\n\nbody\n"
    submission = schema.prepare_submission("page", material)
    assert submission.payload["text"] == material
    assert submission.hints["flagged"] == ["submitted_by"]


# ── prepare_reply: the ask-back answer's own validation ─────────────────────────────────────────
# `BrainService.reply` calls this before touching a row (`_reply`'s docstring: "Identity first,
# state second"), so a bug here is a bug in the FIRST gate an attacker-reachable write goes
# through.
def test_prepare_reply_rejects_empty_answer():
    with pytest.raises(ReplyRejected, match="empty"):
        schema.prepare_reply("")


def test_prepare_reply_rejects_whitespace_only_answer():
    with pytest.raises(ReplyRejected, match="empty"):
        schema.prepare_reply("   \n\t  ")


def test_prepare_reply_rejects_a_non_string_answer():
    with pytest.raises(ReplyRejected):
        schema.prepare_reply(None)   # type: ignore[arg-type]


def test_prepare_reply_rejects_an_answer_over_the_character_cap():
    huge = "x" * (schema.MAX_REPLY_CHARS + 1)
    with pytest.raises(ReplyRejected, match="too long"):
        schema.prepare_reply(huge)


def test_prepare_reply_accepts_an_answer_at_exactly_the_character_cap():
    exact = "x" * schema.MAX_REPLY_CHARS
    assert schema.prepare_reply(exact) == exact


def test_prepare_reply_returns_the_answer_verbatim_for_an_ordinary_reply():
    """The benign twin: the ordinary case — naming a registered entity — passes through
    completely unchanged, nothing stripped or rewritten."""
    assert schema.prepare_reply("Acme Corp") == "Acme Corp"


def test_prepare_reply_message_never_echoes_another_identity_or_a_path():
    """Safe to echo verbatim over HTTP: the refusal names only a static limit and the
    caller's own length, matching `prepare_submission`'s refusals in voice and in safety."""
    huge = "x" * (schema.MAX_REPLY_CHARS + 1)
    with pytest.raises(ReplyRejected) as exc_info:
        schema.prepare_reply(huge)
    message = str(exc_info.value)
    assert str(schema.MAX_REPLY_CHARS) in message
    assert str(len(huge)) in message


# ── reply_invocation: the ONE spelling of the command every surface states — a message
# containing a command is an executable promise ──────────────────────────────────────────────────
def test_reply_invocation_is_exactly_the_callable_string():
    assert schema.reply_invocation(42) == 'brain_reply(submission_id=42, answer="<your answer>")'
    assert schema.REPLY_TOOL in schema.reply_invocation(42)
