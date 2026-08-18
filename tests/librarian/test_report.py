"""`librarian.report`: pure functions, one fact set rendered two ways. The end-to-end processing
tests already prove these are called correctly from `processing.py`; this file targets the
rendering rules themselves directly — the wording contract report.py's own
docstring states (state the fact never the implication, name the literal enum value, never echo
an offending value, `failed` never shares a sentence shape with `rejected`).
"""
from stigmergy.capture import schema
from stigmergy.kernel import registry as registry_module
from stigmergy.librarian import report


def _registry() -> registry_module.Registry:
    """A `Registry` keyed by the production indexer, never by hand-filling a lookup map.

    `kernel.registry` keys TWO maps now — the narrow resolution one `canonical_id` reads and the
    coarse collision one the mint gate reads — and a fixture that filled one of them by hand would
    let this file agree with itself about a fold production does differently.
    """
    reg = registry_module.Registry()
    _register(reg, "acme", "Acme Corp")
    return reg


def _register(reg: registry_module.Registry, entity_id: str, name: str) -> None:
    reg.entities[entity_id] = {"name": name, "type": "organization", "aliases": []}
    registry_module.index_entity(reg, entity_id, reg.entities[entity_id])


def test_filed_names_the_page_the_commit_and_says_the_brain_cannot_answer_about_it_yet():
    out = report.filed(page_path="wiki/notes/X.md", commit="abc123",
                       anchoring={"kind": "entity", "entities": ["Acme Corp"]}, links=[],
                       overlaps=[], findings=[])
    assert out["summary"].startswith(schema.FILED)
    assert "wiki/notes/X.md@abc123" in out["summary"]
    # this used to assert `report.NOT_SEARCHABLE in out["summary"]` — true whatever the
    # constant said, so it never pinned the WORDING. The constant used to claim the page was
    # "invisible to search_brain/ask until the next index rebuild", which is false on
    # webhook-enabled deployments (observed live on staging: searchable seconds after the
    # librarian's push while this report still promised invisibility). The wording is pinned
    # LITERALLY here — never through the constant — so the promise is executable, not circular.
    assert "at the next index rebuild" in out["summary"]
    assert "incremental upsert" in out["summary"]
    assert "whichever lands first" in out["summary"]
    assert "invisible to search_brain" not in out["summary"]
    assert out["anchored_to"] == "Acme Corp"
    assert "source_pages" not in out          # absent entirely on an ordinary filing


def test_filed_with_source_pages_names_them_in_the_summary_and_the_fact_set():
    """`filed_meeting`'s field, on the fast lane's slimmer report — the sentence for the human,
    the list for a caller, and the citation stated so nobody learns it from `git show`."""
    out = report.filed(page_path="wiki/notes/X.md", commit="abc123",
                       anchoring={"kind": "entity", "entities": ["Acme Corp"]}, links=[],
                       overlaps=[], findings=[],
                       source_pages=["sources/slack/x-thread.md"])
    assert out["source_pages"] == ["sources/slack/x-thread.md"]
    assert "filed verbatim at sources/slack/x-thread.md" in out["summary"]
    assert "`sources:`" in out["summary"]


# ── the anchor phrase names the SAME registry id the page's own `entity:` frontmatter was
# stamped with, id and display name together. ───────────────────────────────────────────────────
def test_filed_with_a_registry_names_both_the_display_name_and_the_backticked_id():
    out = report.filed(page_path="wiki/notes/X.md", commit="abc123",
                       anchoring={"kind": "entity", "entities": ["acme"]}, links=[],
                       overlaps=[], findings=[], registry=_registry())
    assert out["anchored_to"] == "Acme Corp (`acme`)"


def test_filed_with_a_registry_resolves_whatever_the_outcome_declared_id_name_or_alias():
    """The agent's outcome may have declared an id, a name or an alias — whichever it was, the
    phrase always names the CANONICAL id, the same one the page's frontmatter carries."""
    for declared in ("acme", "Acme Corp"):
        out = report.filed(page_path="wiki/notes/X.md", commit="abc123",
                           anchoring={"kind": "entity", "entities": [declared]}, links=[],
                           overlaps=[], findings=[], registry=_registry())
        assert out["anchored_to"] == "Acme Corp (`acme`)", declared


def test_filed_with_multiple_entities_and_a_registry_joins_id_name_pairs():
    reg = _registry()
    _register(reg, "globex", "Globex")
    out = report.filed(page_path="wiki/notes/X.md", commit="abc123",
                       anchoring={"kind": "entity", "entities": ["acme", "globex"]}, links=[],
                       overlaps=[], findings=[], registry=reg)
    assert out["anchored_to"] == "Acme Corp (`acme`), Globex (`globex`)"


def test_filed_with_two_spellings_of_the_same_entity_dedupes_to_one():
    """`["acme", "Acme Corp"]` — an id and a display name for the SAME entity — must read as ONE
    entity here, mirroring `gates.resolve_entity_ids`'s own dedup. This used to read "Acme Corp
    (`acme`), Acme Corp (`acme`)" beside a page whose (deduplicated) `entity:` frontmatter carried
    the id once — the exact vocabulary mismatch the anchor phrase exists to prevent."""
    out = report.filed(page_path="wiki/notes/X.md", commit="abc123",
                       anchoring={"kind": "entity", "entities": ["acme", "Acme Corp"]}, links=[],
                       overlaps=[], findings=[], registry=_registry())
    assert out["anchored_to"] == "Acme Corp (`acme`)"


def test_filed_company_scope_is_unaffected_by_a_registry_since_it_never_carries_an_id():
    out = report.filed(page_path="wiki/notes/X.md", commit="abc123",
                       anchoring={"kind": "company", "reason": "applies company-wide"},
                       links=[], overlaps=[], findings=[],
                       registry=_registry())
    assert out["anchored_to"] == "company-wide scope (applies company-wide)"


def test_filed_with_a_registry_falls_back_to_the_bare_id_when_it_cannot_resolve():
    """Defensive only (report.py's own docstring): the id `gate_anchoring` already verified should
    always resolve here too, but if it somehow does not, the safe failure is the bare id — never a
    crash, never an invented display name."""
    out = report.filed(page_path="wiki/notes/X.md", commit="abc123",
                       anchoring={"kind": "entity", "entities": ["unknown-id"]}, links=[],
                       overlaps=[], findings=[], registry=_registry())
    assert out["anchored_to"] == "`unknown-id`"


# ── the resolution is REPORTED, never silent (issue #77) ───────────────────────────────────────
# Which entity a capture is about is the agent's judgment now, not a suffix list's. An automatic
# decision nobody can see is exactly what this repo does not allow, so whenever the agent says WHY
# it pointed a capture somewhere, the person who submitted it reads that sentence beside the anchor.
def test_an_entity_anchors_stated_reason_reaches_the_submitter_beside_the_anchor():
    """The whole point of the change, at the one surface a human reads. The reason is the agent's
    prose and code neither writes it nor checks it — what code guarantees is that it is not
    DROPPED, and that the id it accompanies passed `gate_anchoring` to get here at all."""
    reason = ('resolved "Cofers, S.L." to acme: same company, legal-form suffix; three anchored '
              "pages match the billing context")
    out = report.filed(page_path="wiki/notes/X.md", commit="abc123",
                       anchoring={"kind": "entity", "entities": ["acme"], "reason": reason},
                       links=[], overlaps=[], findings=[], registry=_registry())

    assert out["anchor_reason"] == reason
    assert reason in out["summary"]
    assert report.RESOLUTION_PREFIX in out["summary"]
    assert reason in report.render_prose(out)


def test_the_reason_never_contaminates_the_anchor_identity_field():
    """`anchored_to` names WHICH entity and a read path branches on it (`slack.poller` renders it
    as the anchor line). A rationale glued onto it would make one field two facts, so the note gets
    its own key and its own clause."""
    out = report.filed(page_path="wiki/notes/X.md", commit="abc123",
                       anchoring={"kind": "entity", "entities": ["acme"],
                                  "reason": "same company, a legal form"},
                       links=[], overlaps=[], findings=[], registry=_registry())

    assert out["anchored_to"] == "Acme Corp (`acme`)"


def test_an_entity_anchor_with_no_stated_reason_reads_exactly_as_it_always_did():
    """The benign twin, and it is the common case: most captures name their entity plainly and
    there is nothing to explain. A report that grew an empty `Resolved:` clause on every filing
    would train a reader to skip the one line this change exists to make them read."""
    out = report.filed(page_path="wiki/notes/X.md", commit="abc123",
                       anchoring={"kind": "entity", "entities": ["acme"]},
                       links=[], overlaps=[], findings=[], registry=_registry())

    assert out["anchor_reason"] == ""
    assert report.RESOLUTION_PREFIX not in out["summary"]
    assert out["summary"].startswith(f"{schema.FILED} — wiki/notes/X.md@abc123, anchored to "
                                     "Acme Corp (`acme`). ")


def test_a_company_wide_reason_is_not_repeated_as_a_resolution_note():
    """Company-wide scope already carries its reason INSIDE the anchor phrase, where it justifies
    belonging to no entity and is REQUIRED by `gate_anchoring` rather than volunteered. Printing it
    a second time under a resolution heading would read as two different facts about one page."""
    out = report.filed(page_path="wiki/notes/X.md", commit="abc123",
                       anchoring={"kind": "company", "reason": "applies company-wide"},
                       links=[], overlaps=[], findings=[], registry=_registry())

    assert out["anchor_reason"] == ""
    assert out["summary"].count("applies company-wide") == 1


def test_a_hostile_reason_is_clamped_and_control_stripped_like_every_other_echoed_value():
    """The reason is agent prose derived from captured material, and it lands in a sentence a human
    reads. Same treatment as every other echoed value in this module — never a second escaping
    rule, and never a raw newline that could forge the report's own structure."""
    hostile = "same\x1b[31mcompany\nsecond line " + "x" * 900
    out = report.filed(page_path="wiki/notes/X.md", commit="abc123",
                       anchoring={"kind": "entity", "entities": ["acme"], "reason": hostile},
                       links=[], overlaps=[], findings=[], registry=_registry(),
                       agent_rationale=hostile)

    assert "\x1b" not in out["anchor_reason"] and "\n" not in out["anchor_reason"]
    # Compared against the sibling field rather than against a length: `agent_rationale` is the
    # module's existing prose-from-the-agent field, and asserting a number here would be a second
    # copy of a truncation rule (`text.clamp` marks the cut, so it is not simply the width).
    assert out["anchor_reason"] == out["agent_rationale"]


def test_a_meeting_reports_the_resolution_of_each_decisions_own_anchor():
    """A meeting anchors every decision INDEPENDENTLY, so a resolution note belongs per decision and
    not per capture — a reader has to be able to tell which page a judgment was about."""
    out = report.filed_meeting(
        source_pages=["sources/meetings/t.md"], meeting_page="wiki/meetings/m.md",
        decisions=[{"path": "wiki/decisions/d1.md",
                    "anchoring": {"kind": "entity", "entities": ["acme"],
                                  "reason": "the group form of the registered name"}},
                   {"path": "wiki/decisions/d2.md",
                    "anchoring": {"kind": "entity", "entities": ["acme"]}}],
        commit="cafefeed", registry=_registry())

    rows = out["filed_meeting"]["decisions"]
    assert rows[0]["anchor_reason"] == "the group form of the registered name"
    assert rows[1]["anchor_reason"] == ""
    assert "the group form of the registered name" in out["summary"]


def test_filed_with_company_scope_names_the_written_reason():
    out = report.filed(page_path="wiki/notes/X.md", commit="abc123",
                       anchoring={"kind": "company", "reason": "applies company-wide"},
                       links=[], overlaps=[], findings=[])
    assert "company-wide scope" in out["anchored_to"]
    assert "applies company-wide" in out["anchored_to"]


def test_filed_with_overlaps_reads_differently_from_a_plain_filed_and_names_both_sides():
    plain = report.filed(page_path="wiki/notes/X.md", commit="sha1",
                         anchoring={"kind": "entity", "entities": ["Acme Corp"]}, links=[],
                         overlaps=[], findings=[])
    overlapping = report.filed(page_path="wiki/notes/X.md", commit="sha1",
                               anchoring={"kind": "entity", "entities": ["Acme Corp"]}, links=[],
                               overlaps=[{"path": "wiki/notes/Y.md", "note": "same ground"}], findings=[])
    assert plain["summary"] != overlapping["summary"]
    assert "wiki/notes/Y.md" in overlapping["summary"]
    assert "nothing was deleted or rewritten" in overlapping["summary"]
    # the OVERLAP branch composes its own copy of the summary sentence (report.py's
    # `filed`, second `summary =`) — the same wording contract pinned literally here too, so a
    # future edit cannot fix the plain-filed branch and leave this one stale.
    assert "at the next index rebuild" in overlapping["summary"]
    assert "incremental upsert" in overlapping["summary"]
    assert "whichever lands first" in overlapping["summary"]
    assert "invisible to search_brain" not in overlapping["summary"]


def test_filed_retry_reads_as_neither_a_fresh_success_nor_a_penalty():
    out = report.filed_retry(original_id=42, page_path="wiki/notes/X.md", commit="sha1")
    assert out["status"] == schema.FILED
    assert "#42" in out["summary"]
    assert "retry" in out["summary"]
    assert out["retry_of"] == 42


def test_rejected_duplicate_points_at_the_existing_page_and_reads_differently_from_filed_overlap():
    out = report.rejected_duplicate(page_path="wiki/notes/X.md", as_of="2026-01-01")
    assert out["status"] == schema.REJECTED
    assert "wiki/notes/X.md" in out["summary"]
    assert "nothing new was created" in out["summary"]


def test_rejected_secret_never_carries_the_value_only_a_locator_and_rule():
    out = report.rejected_secret(line="4", rule_id="github-pat")
    assert out["status"] == schema.REJECTED
    assert "line 4" in out["summary"]
    assert "github-pat" in out["summary"]
    assert "your material" in out["summary"]
    assert "never redacted" not in out["summary"]     # not literally required, but never a value
    assert "Remove it and resubmit" in out["summary"]


def test_rejected_pii_names_the_kind_and_locator_never_the_value():
    out = report.rejected_pii(line="7", pattern_label="an IBAN")
    assert "an IBAN" in out["summary"]
    assert "line 7" in out["summary"]



def test_rejected_steering_never_echoes_the_planted_instruction():
    """Criterion 13's own rule at the rendering layer: the category is named, the payload text
    is never a parameter this function even accepts — there is no way to pass it through."""
    out = report.rejected_steering(path="ops/acl.json", category="write-outside-lane")
    assert "write-outside-lane" in out["summary"]
    assert "ops/acl.json" in out["summary"]


def test_triage_entity_names_the_unresolved_name_as_the_open_question():
    """INVERTED by the plural collapse. This used to call `triage_entity(name=...)` and the builder
    used to write `schema.SITUATION_NAME_KEY`. One shape now: a park writes the LIST whatever the
    count, and the singular key is retired as an output. Both halves are asserted — the new key
    present AND the old one absent — because an assertion on the new key alone would also pass a
    builder that wrote both, which is the duplication this change exists to remove."""
    out = report.triage_entity(names=["Nebula Systems"])
    assert out["status"] == schema.TRIAGE
    assert out[schema.SITUATION_NAMES_KEY] == ["Nebula Systems"]
    assert schema.SITUATION_NAME_KEY not in out
    assert "Nebula Systems" in out["summary"]
    assert "Nebula Systems" in out["open_question"]
    # The one-name SENTENCE survives the collapse: "your material named 1 things" is how a reader
    # learns nobody read what he sent. Nothing else in this file pins the singular prose.
    assert 'seems to be about "Nebula Systems"' in out["summary"]
    assert out["open_question"] == "which entity is Nebula Systems?"
    assert "named 1 things" not in out["summary"]
    # Ask-back exists (`report.needs_input`, `brain_reply`), so telling a submitter "there's no
    # way yet for the system to ask you a follow-up... that's not built yet" would be false — and
    # on the `asked=True` road (tested below) actively misleading, since their answer DID reach a
    # real question. This flavor (the default, `asked=False`) is reached without ever asking (a
    # governed veto), so it says exactly that: no question was due.
    assert "not built yet" not in out["summary"]
    assert "no question is coming about this one" in out["summary"]


def test_triage_entity_asked_reads_differently_once_the_one_question_is_already_spent():
    """The other of the two paths `triage_entity` is written to be true of (module docstring): a
    capture whose one ask-back question has already been used and still can't resolve. Distinct
    wording is load-bearing here, not decoration — a submitter who just replied must be told their
    answer was received, not that no question was ever coming."""
    out = report.triage_entity(names=["Nebula Systems"], asked=True)
    assert out["status"] == schema.TRIAGE
    assert out[schema.SITUATION_NAMES_KEY] == ["Nebula Systems"]
    assert schema.SITUATION_NAME_KEY not in out
    assert "Nebula Systems" in out["summary"]
    assert "you won't be asked again" in out["summary"]
    assert "no question is coming about this one" not in out["summary"]
    assert "not built yet" not in out["summary"]


CANDIDATES = [
    {"name": "Acme Corp", "aliases": ["Acme"]},
    {"name": "Globex Corp", "aliases": []},
]


# ── needs_input: the ask-back question itself ───────────────────────────────────────────────────
def test_needs_input_names_the_unresolved_name_and_states_the_reply_invocation_verbatim():
    """INVERTED by the plural collapse — `unresolved_name` was the ask-side twin of
    `SITUATION_NAME_KEY` and is retired as an output for the same reason. The absence of the old
    key is asserted beside the presence of the new one: an MCP client reading `brain_submissions`
    is the one genuinely outward-facing consumer of this payload, and a builder still emitting both
    would keep the debt alive while a new-key-only assertion stayed green.

    The one-name SENTENCE is unchanged, and is asserted here rather than described: the count still
    chooses the prose even though it no longer chooses the keys."""
    out = report.needs_input(submission_id=42, names=["Nebula Systems"], candidates=CANDIDATES,
                             total_candidates=len(CANDIDATES))
    assert out["status"] == schema.NEEDS_INPUT
    assert "Nebula Systems" in out["summary"]
    assert 'seems to be about "Nebula Systems"' in out["summary"]
    assert "names 1 things" not in out["summary"]
    assert out["open_question"] == "which entity is Nebula Systems?"
    assert out["unresolved_names"] == ["Nebula Systems"]
    assert "unresolved_name" not in out
    # rule 2: the STATED command is a promise. It has to be the byte-identical string the reply
    # channel actually accepts (`schema.reply_invocation`), never a paraphrase or a hand-rolled one.
    assert out["reply_invocation"] == schema.reply_invocation(42)
    assert out["reply_invocation"] in out["summary"]
    assert out["reply_invocation"] == 'brain_reply(submission_id=42, answer="<your answer>")'


def test_a_several_name_ask_stores_the_invocation_it_actually_wrote():
    """OLD BEHAVIOUR: the summary got `invocation.replace("<your answer>", "<your answer, covering
    all N>")` while `reply_invocation` stored the UNSUBSTITUTED string — two spellings of one
    sentence in one report, six lines apart in one function.

    `slack.poller._needs_input_prose` strips that suffix by exact match to turn MCP prose into a
    Slack card (its own docstring: `reply_invocation` "is stored beside the sentence for exactly
    this consumer"), so the strip silently never matched for n > 1 and a Slack submitter was shown
    the raw `brain_reply(...)` call — a tool they have no way to invoke from Slack.

    Asserted as a relationship rather than as a literal: what has to hold is that the field and the
    sentence are the same string, whatever the wording becomes.
    """
    out = report.needs_input(submission_id=42, names=["Nebula Systems", "Acme Capital"],
                             candidates=CANDIDATES, total_candidates=len(CANDIDATES))

    assert out["reply_invocation"] in out["summary"]
    assert out["summary"].endswith(f"\n\nReply with:\n  {out['reply_invocation']}")
    assert "covering all 2" in out["reply_invocation"], (
        "the stored command is the bare one again — the substituted line is what was written")
    assert out["unresolved_names"] == ["Nebula Systems", "Acme Capital"]


def test_a_one_name_ask_still_stores_the_plain_invocation_verbatim():
    """The benign twin, and the reason the change above is safe. For one name the two strings were
    already identical, so the fix must not have moved the singular road: the stored command is
    still byte-for-byte what `schema.reply_invocation` builds — the string the reply channel
    actually accepts, which `test_the_stated_reply_invocation_executed_verbatim...` runs for real.
    """
    out = report.needs_input(submission_id=42, names=["Nebula Systems"], candidates=CANDIDATES,
                             total_candidates=len(CANDIDATES))

    assert out["reply_invocation"] == schema.reply_invocation(42)
    assert out["summary"].endswith(f"\n\nReply with:\n  {out['reply_invocation']}")


def test_needs_input_lists_every_candidate_with_its_aliases():
    out = report.needs_input(submission_id=1, names=["Nebula"], candidates=CANDIDATES,
                             total_candidates=len(CANDIDATES))
    assert "Acme Corp (also known as: Acme)" in out["summary"]
    assert "Globex Corp (also known as: no other names on file)" in out["summary"]


def test_needs_input_states_both_outcomes_and_the_one_ask_clause():
    """The reader is told both outcomes and their consequence, and that this is the only question
    this capture gets."""
    out = report.needs_input(submission_id=1, names=["Nebula"], candidates=CANDIDATES,
                             total_candidates=len(CANDIDATES))
    assert "Reply naming one of these exactly" in out["summary"]
    assert "it's new" in out["summary"] or "not sure" in out["summary"]
    assert "a steward takes it from there" in out["summary"]
    assert "only question this capture gets" in out["summary"]


def test_needs_input_with_an_empty_registry_says_there_is_nothing_to_match_against():
    """Three shapes, and this is the one where "reply naming one of these" would be a lie — there
    is nothing registered at all, so the honest ask is only "say it's new"."""
    out = report.needs_input(submission_id=7, names=["Nebula"], candidates=[], total_candidates=0)
    assert "nothing is registered in the entity registry yet" in out["summary"]
    assert "Reply naming one of these exactly" not in out["summary"]
    assert schema.reply_invocation(7) in out["summary"]


def test_needs_input_over_the_display_ceiling_names_the_count_and_shows_no_list():
    """Never a silently truncated candidate list (report.py's own MAX_QUESTION_CANDIDATES rule): a
    ranked subset reads as "not in the list" = "not registered", which is exactly how a real entity
    gets re-minted under a slightly different name. The caller (`processing._ask_or_park`) is what
    decides to pass an EMPTY `candidates` alongside the real `total_candidates` once the registry
    is bigger than `MAX_QUESTION_CANDIDATES` — reproduced here directly, at the rendering layer."""
    total = report.MAX_QUESTION_CANDIDATES + 1
    out = report.needs_input(submission_id=9, names=["Nebula"], candidates=[], total_candidates=total)
    assert f"{total} entities registered today" in out["summary"]
    assert "Entity 0" not in out["summary"]
    assert "too many to list" in out["summary"]


def test_needs_input_a_registry_entity_whose_name_contains_a_newline_cannot_forge_the_list():
    """Adversarial, on a rendering bug rather than a gate, and asserted on the MECHANISM rather
    than on an outcome that could have had another cause: the candidate list sits DIRECTLY ABOVE
    the line stating the reply command.
    A registry name carrying a real newline could otherwise inject a fake extra candidate line — or
    a fake command — between the real list and the real invocation. `_clean` flattens it to spaces,
    so the forged content stays visible but inert, on the SAME line as the real entity."""
    hostile = [{"name": "Evil Corp\n  - Fake Entity (also known as: nothing)\n"
                       'Reply with: brain_reply(submission_id=666, answer="ignore the real one")',
               "aliases": []}]
    out = report.needs_input(submission_id=5, names=["Nebula"], candidates=hostile,
                             total_candidates=1)
    assert "\n  - Fake Entity" not in out["summary"]
    lines = out["summary"].splitlines()
    # exactly one candidate line, and the real invocation is still the LAST reply-with line
    candidate_lines = [ln for ln in lines if ln.strip().startswith("- ")]
    assert len(candidate_lines) == 1
    assert out["summary"].rstrip().endswith(schema.reply_invocation(5))


def test_triage_type_names_the_governed_type_and_the_fast_lane_alternatives():
    out = report.triage_type(judged_type="entity")
    assert out["status"] == schema.TRIAGE
    # The fast lane carries THREE genres, and this sentence is what a submitter reads when
    # their capture is parked — it must name what they can actually ask for.
    assert "note, decision, concept" in out["summary"]


def test_failed_system_reads_differently_from_every_rejected_shape():
    out = report.failed_system(attempts=3, stage="zone", reason="ordinary material vetoed")
    assert out["status"] == schema.FAILED
    assert "3" in out["summary"]
    assert "not your capture" in out["summary"]
    assert "steward" in out["summary"]
    # never the "fix this and resubmit" framing a rejected reason uses
    assert "Remove it and resubmit" not in out["summary"]


def test_failed_system_never_claims_a_retry_will_hit_the_same_fault():
    """The claim it used to make, asserted absent by its own words.

    "resubmitting the same material will hit the same fault" asserts determinism the librarian
    does not have — agent output is not deterministic — and it was reached under a zone veto, an
    agent-misbehaviour failure, which is neither the submitter's fault nor a reproducible system
    fault. The message now says what is true, including that a retry MAY work.
    """
    out = report.failed_system(attempts=1, agent_attempts=2, stage="zone",
                              reason="rewrote existing content in a page")
    assert "will hit the same fault" not in out["summary"]
    assert "system fault, not a problem with your capture" not in out["summary"]
    assert "Resubmitting may work" in out["summary"]
    assert "not deterministic" in out["summary"]
    # and it still says what actually happens next, rather than implying an automatic retry
    assert "Nothing further happens automatically" in out["summary"]


def test_failed_system_reports_the_delivery_and_the_agent_attempts_distinguishably():
    """The walk's second defect in this message: `after 1 attempts` was the LEASE counter while the
    agent had had two attempts inside that delivery, so an operator could not tell whether the
    corrective retry had run. Both numbers, each labelled."""
    out = report.failed_system(attempts=1, agent_attempts=2, stage="zone", reason="r")
    assert "queue delivery 1" in out["summary"]
    assert "2 agent attempts" in out["summary"]
    assert out["deliveries"] == 1 and out["agent_attempts"] == 2


def test_failed_system_gets_the_plural_right_for_a_single_agent_attempt():
    out = report.failed_system(attempts=2, agent_attempts=1, stage="git", reason="r")
    assert "1 agent attempt inside it" in out["summary"]
    assert "1 agent attempts" not in out["summary"]


def test_failed_system_omits_the_agent_counter_when_the_fault_landed_before_the_agent_ran():
    """A dead worktree or an unreachable git fails the item without the agent ever running.
    Reporting "0 agent attempts" would be noise; guessing at a number would be a lie."""
    out = report.failed_system(attempts=1, stage="WorktreeError", reason="r")
    assert "agent attempt" not in out["summary"]
    assert "queue delivery 1" in out["summary"]


def test_a_truncated_reason_stops_at_a_word_boundary():
    """The walk's `— it is th…`. A sentence cut mid-word reads as a rendering bug and costs the
    reader the one word that would have named the problem."""
    reason = ("rewrote existing content in wiki/decisions/Git as the Canonical Store.md: "
              "edits to a page that already exists may only ADD a back-link or a callout, and "
              "this one removed a line that the human who wrote the page had put there")
    out = report.failed_system(attempts=1, stage="zone", reason=reason)
    truncated = out["summary"].split("…")[0].rsplit(" ", 1)[-1]
    assert "…" in out["summary"]                       # it really was truncated
    assert reason.split()[0] in out["summary"]
    assert any(truncated == word for word in reason.split()), (
        f"the summary ends mid-word on {truncated!r}")


def test_a_single_long_token_is_still_truncated_rather_than_collapsing_to_nothing():
    """The benign twin of the boundary rule: a path or a rule id with no spaces in it must still be
    cut, not dropped, so the bound cannot be defeated by writing one long word."""
    out = report.failed_system(attempts=1, stage="zone", reason="x" * 400)
    assert "xxxx" in out["summary"] and "…" in out["summary"]






def test_injection_finding_names_only_the_category_never_a_payload_substring():
    sentence = report.injection_finding("reveal-credentials")
    assert "reveal-credentials" in sentence
    assert "not followed" in sentence


# ── nothing is silently omitted (module docstring's own rule) ──────────────────────────────────
def test_base_report_shape_is_shared_by_every_terminal_state_builder():
    builders_and_kwargs = [
        (report.filed, dict(page_path="p", commit="c", anchoring={}, links=[], overlaps=[], findings=[])),
        (report.rejected_duplicate, dict(page_path="p", as_of="2026-01-01")),
        (report.triage_entity, dict(names=["X"])),
        (report.failed_system, dict(attempts=1, stage="s", reason="r")),
    ]
    required_keys = {"status", "summary", "page_path", "commit", "anchored_to", "links_created",
                     "overlaps_flagged", "findings"}
    for builder, kwargs in builders_and_kwargs:
        out = builder(**kwargs)
        assert required_keys <= out.keys(), f"{builder.__name__} dropped a required field"



# ── render_prose: the CLI's rendering of the SAME fact set ──────────────────────────────────────
def test_render_prose_of_a_filed_report_includes_the_always_present_fields():
    out = report.filed(page_path="p", commit="c", anchoring={"kind": "company", "reason": "x"},
                       links=["[[Acme Corp]]"], overlaps=[], findings=[])
    prose = report.render_prose(out)
    assert prose.startswith(out["summary"])
    assert "links_created" in prose
    assert "overlaps_flagged" in prose


def test_render_prose_of_a_rejected_report_omits_the_filed_only_lines():
    out = report.rejected_duplicate(page_path="p", as_of="2026-01-01")
    prose = report.render_prose(out)
    assert "links_created" not in prose
    assert "overlaps_flagged" not in prose


def test_render_prose_lists_every_finding_with_a_bang_prefix():
    out = report.filed(page_path="p", commit="c", anchoring={"kind": "company", "reason": "x"},
                       links=[], overlaps=[],
                       findings=[report.injection_finding("declare-canonical")])
    prose = report.render_prose(out)
    assert "! finding: material attempted" in prose


def test_render_prose_of_a_filed_meeting_report_does_not_double_render_agent_rationale():
    # Findings cycle 2: `filed_meeting`'s summary already carries its own `agent_rationale` line
    # (and its own `links_created`/`overlaps_flagged`/`pages_edited` lines) —
    # `render_prose`'s `elif` fallback used to catch the meeting case (status FILED, excluded from
    # the primary branch) and append `agent_rationale` a second time.
    out = report.filed_meeting(
        source_pages=["sources/meetings/q3-sync-transcript.md"],
        meeting_page="wiki/meetings/2026-07-29-q3-sync.md", decisions=[],
        commit="cafefeed",
        agent_rationale="Filed the Borealis + Stigmergy sync as a source, meeting, and decisions.")
    prose = report.render_prose(out)
    assert prose.count("Filed the Borealis + Stigmergy sync") == 1
    # every other field the meeting summary already carries is likewise not appended a second time
    assert prose.count("links_created") == 1
    assert prose.count("overlaps_flagged") == 1
    assert prose.count("pages_edited") == 1


# ── echoed text is sanitized (the same seam capture.cli uses for untrusted terminal text) ───────
def test_echoed_page_path_with_control_characters_is_sanitized_in_the_summary():
    hostile_path = "wiki/notes/evil\x1b[31mred.md"
    out = report.rejected_duplicate(page_path=hostile_path, as_of="2026-01-01")
    assert "\x1b" not in out["summary"]


# ── filed_meeting: the page-SET report. End-to-end tests assert on `report["filed_meeting"]`
# fields; these exercise `report.filed_meeting` directly, at the unit level, against a
# company-wide reason and the zero-decisions case. ──────────────────────────────────────────────
def test_filed_meeting_names_every_page_path_and_every_decisions_own_anchor():
    out = report.filed_meeting(
        source_pages=["sources/meetings/q3-sync-transcript.md"],
        meeting_page="wiki/meetings/2026-07-29-q3-sync.md",
        decisions=[
            {"path": "wiki/decisions/q3-sync-decision-1.md",
             "anchoring": {"kind": "entity", "entities": ["acme"]}},
            {"path": "wiki/decisions/q3-sync-decision-2.md",
             "anchoring": {"kind": "company",
                          "reason": "applies to every customer, not one of them"}},
        ],
        commit="cafefeed", registry=_registry())
    assert out["status"] == schema.FILED
    assert out["page_path"] == "wiki/meetings/2026-07-29-q3-sync.md"   # the one door in
    rows = out["filed_meeting"]["decisions"]
    assert out["filed_meeting"]["source_pages"] == ["sources/meetings/q3-sync-transcript.md"]
    assert out["filed_meeting"]["meeting_page"] == "wiki/meetings/2026-07-29-q3-sync.md"
    assert len(rows) == 2
    assert rows[0]["path"] == "wiki/decisions/q3-sync-decision-1.md"
    assert rows[0]["anchored_to"] == "Acme Corp (`acme`)"
    assert rows[1]["path"] == "wiki/decisions/q3-sync-decision-2.md"
    # the company-wide reason is carried verbatim
    assert rows[1]["anchored_to"] == ("company-wide scope (applies to every customer, not one "
                                      "of them)")
    # the reason also has to survive into the rendered prose a human actually reads
    assert "applies to every customer, not one of them" in out["summary"]
    # `filed_meeting`'s `head` sentence composes its own copy of the wording contract
    # (report.py, ~line 355) rather than reusing `filed`'s — pinned literally here too.
    assert "at the next index rebuild" in out["summary"]
    assert "incremental upsert" in out["summary"]
    assert "whichever lands first" in out["summary"]
    assert "invisible to search_brain" not in out["summary"]


def test_filed_meeting_with_zero_decisions_says_so_rather_than_omitting_the_line():
    out = report.filed_meeting(source_pages=["sources/meetings/standup-transcript.md"],
                               meeting_page="wiki/meetings/2026-07-29-standup.md",
                               decisions=[], commit="deadbeef")
    assert "decision page(s)" in out["summary"]
    assert "none — nothing from this meeting was drafted as a decision" in out["summary"]
    assert out["filed_meeting"]["decisions"] == []


# ── `_reuse_lines` / `filed_meeting(reuse=...)` — the report half of distillation reuse,
# unit-tested directly. `test_meeting_processing_pg.py` proves the two shapes appear correctly at
# the END of a real processing pass (`test_the_reused_filing_says_so_in_the_report`,
# `test_a_genuine_re_distillation_diffs_the_two_outcomes`); this file's own charter is the
# rendering rules themselves, direct and unit-level — the same split every other `report.py`
# function in this file already gets.
def test_filed_meeting_with_no_reuse_argument_is_byte_for_byte_the_ordinary_report():
    """The docstring's own promise: "Absent entirely on an ordinary filing... no reader has to
    learn a new field for the common case." Proven as an equality, not a spot check — every key
    `filed_meeting` can produce, compared against itself with and without `reuse=`."""
    kwargs = dict(source_pages=["sources/meetings/q3-sync-transcript.md"],
                 meeting_page="wiki/meetings/2026-07-29-q3-sync.md",
                 decisions=[{"path": "wiki/decisions/q3-sync-decision-1.md",
                            "anchoring": {"kind": "entity", "entities": ["acme"]}}],
                 commit="cafefeed", registry=_registry())
    without_the_argument = report.filed_meeting(**kwargs)
    with_none = report.filed_meeting(reuse=None, **kwargs)
    with_empty = report.filed_meeting(reuse={}, **kwargs)
    assert without_the_argument == with_none == with_empty
    assert "distillation_reuse" not in without_the_argument


def test_reuse_lines_is_empty_for_no_reuse_and_for_an_empty_reuse_dict():
    assert report._reuse_lines({}) == []
    assert report._reuse_lines(None) == []


def test_reuse_lines_for_a_reused_filing_names_the_count_and_says_the_transcript_was_not_reread():
    lines = report._reuse_lines({"reused": True, "decisions": ["Ledgerly as source of truth",
                                                                "Make.com phased rollout"]})
    text = "\n".join(lines)
    assert "re-filed the distillation from the parked pass" in text
    assert "2 decision(s) preserved" in text
    assert "the transcript was not read again" in text


def test_reuse_lines_for_a_re_distillation_lists_dropped_before_added_and_names_it_a_warning():
    """The load-bearing ordering the module docstring insists on: "`dropped` is listed FIRST and
    named plainly, because a decision that disappeared between two passes is the finding." Checked
    as a POSITION, not merely as membership — a re-ordering that buried `dropped` after `added`
    would still pass a naive "both strings appear somewhere" assertion."""
    lines = report._reuse_lines({"reused": False,
                                 "dropped": ["ledgerly-as-the-single-source-of-truth"],
                                 "added": ["a-new-decision-nobody-asked-for"],
                                 "kept": ["two-track-approach"]})
    text = "\n".join(lines)
    assert "RE-DISTILLED" in text
    assert "1 decision(s) survived, 1 did not, 1 are new" in text
    assert text.index("DROPPED") < text.index("new (not in the parked pass)")
    assert "ledgerly-as-the-single-source-of-truth" in text
    assert "Read those before accepting this filing" in text   # the instruction, not just the fact


def test_reuse_lines_for_a_re_distillation_with_nothing_dropped_still_says_zero_plainly():
    """The benign-ish twin: a re-distillation that happened to keep every decision (the model ran
    again but agreed with itself) must not read like a loss — `DROPPED` never appears when nothing
    was in fact dropped, and the count says zero rather than a template silently rendering empty."""
    lines = report._reuse_lines({"reused": False, "dropped": [], "added": [],
                                 "kept": ["only-decision"]})
    text = "\n".join(lines)
    assert "RE-DISTILLED" in text
    assert "1 decision(s) survived, 0 did not, 0 are new" in text
    assert "DROPPED" not in text
    assert "new (not in the parked pass)" not in text


def test_filed_meeting_carries_distillation_reuse_as_a_fact_set_beside_the_prose():
    """`filed_meeting`'s own comment: the rendered lines are for the human, `distillation_reuse`
    is for a caller that wants to ASSERT on what changed without re-parsing the prose."""
    reuse = {"reused": False, "dropped": ["x"], "added": [], "kept": ["y"]}
    out = report.filed_meeting(source_pages=["sources/meetings/t.md"],
                               meeting_page="wiki/meetings/2026-07-29-t.md",
                               decisions=[{"path": "wiki/decisions/t-1.md",
                                          "anchoring": {"kind": "company", "reason": "x"}}],
                               commit="deadbeef", reuse=reuse)
    assert out["distillation_reuse"] == reuse
    assert "RE-DISTILLED" in out["summary"]


# ── SEVERAL unresolved names, through the SAME two builders ────────────────────────────────────
# INVERTED by the plural collapse: these used to call `needs_input_multi`/`triage_entity_multi`,
# the plural siblings. There are no siblings — `needs_input` and `triage_entity` serve any number
# of names and write one key shape. What the count still chooses is the SENTENCE, and that is what
# these cases and the one-name cases above split between them.
def test_needs_input_numbers_every_unresolved_name_and_asks_once():
    out = report.needs_input(submission_id=42, names=["Nebula Systems", "Quantum Labs"],
                             candidates=[{"name": "Acme Corp"}], total_candidates=1)
    assert out["status"] == schema.NEEDS_INPUT
    assert out["unresolved_names"] == ["Nebula Systems", "Quantum Labs"]
    assert "unresolved_name" not in out
    assert '1. "Nebula Systems"' in out["summary"]
    assert '2. "Quantum Labs"' in out["summary"]
    # ONE ask for both — the whole-meeting-parks clause names both by count, not by echoing them
    # a second time
    assert "all 2 at once" in out["summary"]


def test_triage_entity_names_every_still_unresolved_name_and_parks_the_whole_capture():
    out = report.triage_entity(names=["Nebula Systems", "Quantum Labs"], asked=True)
    assert out["status"] == schema.TRIAGE
    assert out[schema.SITUATION_NAMES_KEY] == ["Nebula Systems", "Quantum Labs"]
    assert schema.SITUATION_NAME_KEY not in out
    assert "Nebula Systems" in out["summary"] and "Quantum Labs" in out["summary"]
    assert "won't be asked again" in out["summary"]   # the asked=True tail, unchanged from asked=False


def test_the_two_park_builders_agree_with_render_prose():
    """Both builders go through `base_report` exactly like every other one — a regression here
    would mean an ask-back renders differently for a CLI reader than every other report this
    module produces. Asked of the ONE-name shape, which is the road the collapse re-pointed."""
    out = report.needs_input(submission_id=7, names=["Nebula Systems"], candidates=[],
                             total_candidates=0)
    prose = report.render_prose(out)
    assert prose.startswith(out["summary"])


# ── the builders serve BOTH flows, so the prose has to know which one it is talking to ─────────
# The several-names wording was written for the meeting flow and said so in every copy of the
# sentence. Issue #32 routes ORDINARY captures through it too, and an ordinary submitter told "the
# whole meeting parks" and "a meeting page can never link a decision that was never filed" is being
# told about consequences that do not exist for anything he sent — inside the ONE question his
# capture ever gets. Instructions a reader can see are not about him are instructions he stops
# reading.
def test_needs_input_for_an_ordinary_capture_never_mentions_a_meeting():
    """The ordinary side. `meeting=False` is also the DEFAULT, and that direction matters more than
    the flag: an un-threaded call site gets the ordinary copy, so a forgotten argument degrades to
    "generic", never to "claims to be a meeting"."""
    out = report.needs_input(submission_id=42, names=["Jack", "Acme Capital"],
                             candidates=[{"name": "Acme Corp"}], total_candidates=1,
                             meeting=False)

    assert "meeting" not in out["summary"]
    assert "decision that names it" not in out["summary"]
    assert "the whole capture parks for a steward" in out["summary"]
    # The default is the ordinary flow, asserted byte-for-byte rather than described.
    assert report.needs_input(submission_id=42, names=["Jack", "Acme Capital"],
                              candidates=[{"name": "Acme Corp"}],
                              total_candidates=1)["summary"] == out["summary"]


def test_needs_input_for_a_meeting_still_names_the_meeting_and_what_it_costs():
    """The specificity twin: the meeting flow must NOT lose the sentence that was written for it.
    A transcript becomes a whole page set, and one unplaced name parks all of it — telling that
    submitter only "the whole capture parks" would understate what is stuck by a page set."""
    out = report.needs_input(submission_id=42, names=["Jack", "Acme Capital"],
                             candidates=[{"name": "Acme Corp"}], total_candidates=1,
                             meeting=True)

    assert "the whole meeting parks for a steward" in out["summary"]
    assert "decision that names it" in out["summary"]
    assert "a meeting page can never link a decision that was never filed" in out["summary"]


def test_triage_entity_for_an_ordinary_capture_never_mentions_a_meeting():
    """The same split on the STEWARD-facing park — the report that says what a steward will do
    with the material. `meeting=False` by default here too."""
    out = report.triage_entity(names=["Jack", "Acme Capital"], meeting=False)

    assert "meeting" not in out["summary"]
    assert "place this capture where it actually belongs" in out["summary"]
    assert report.triage_entity(names=["Jack", "Acme Capital"])["summary"] == out["summary"]


def test_triage_entity_for_a_meeting_says_meeting():
    """Specificity twin for the park report: the flag has to actually change the noun, or the two
    tests above pass for a flag that is read and discarded."""
    out = report.triage_entity(names=["Jack", "Acme Capital"], meeting=True)

    assert "place this meeting where it actually belongs" in out["summary"]


def test_the_flow_flag_changes_the_prose_and_nothing_else():
    """The flag is copy, not structure. Every fact a consumer reads — the status, the per-name
    list, the open question, the report's own shape — is identical between the two flows, so
    `entities.cli` and the doorbell cannot start behaving differently because a submitter happened
    to send a transcript."""
    ordinary = report.triage_entity(names=["Jack", "Acme Capital"], asked=True)
    meeting = report.triage_entity(names=["Jack", "Acme Capital"], asked=True, meeting=True)

    assert {k: v for k, v in ordinary.items() if k != "summary"} == \
           {k: v for k, v in meeting.items() if k != "summary"}
    assert ordinary["summary"] != meeting["summary"]


# ── the flag has NO slot in the one-name sentence, and that is a property, not an accident ─────
# `meeting` reaches the several-names clause only (`ONE_ASK_CLAUSE_SEVERAL` / the `_parked_noun`
# in the park summary). The one-name sentence never named a flow and still does not — so the two
# flows produce a BYTE-IDENTICAL document for a one-name park. Asserted as an equality over the
# whole report rather than by reading the prose, because "the flag changes nothing here" is
# exactly the claim a spot check cannot make.
def test_a_one_name_ask_is_byte_identical_whichever_flow_it_came_from():
    kwargs = dict(submission_id=42, names=["Jack"], candidates=[{"name": "Acme Corp"}],
                  total_candidates=1)
    assert report.needs_input(**kwargs, meeting=True) == report.needs_input(**kwargs,
                                                                            meeting=False)


def test_a_one_name_park_is_byte_identical_whichever_flow_it_came_from():
    assert report.triage_entity(names=["Jack"], asked=True, meeting=True) == \
           report.triage_entity(names=["Jack"], asked=True, meeting=False)


# ── a blank beside a real name drops to ONE name, therefore to the one-name PROSE ──────────────
# `_named_only` filters blanks, and the count that chooses the sentence is the count AFTER the
# filter. A builder that decided the wording from the raw argument would tell a submitter his
# material "names 2 things" and then number one of them — the same class of defect as printing the
# blank itself, which `test_ordinary_multi_entity_park_unit.py` pins on the routing side.
def test_a_blank_beside_a_real_name_leaves_the_one_name_sentence_and_a_one_element_list():
    ask = report.needs_input(submission_id=42, names=["Jack", "   "], candidates=[],
                             total_candidates=0)
    park = report.triage_entity(names=["Jack", "   "])

    assert ask["unresolved_names"] == ["Jack"]
    assert "unresolved_name" not in ask
    assert 'seems to be about "Jack"' in ask["summary"]
    assert "names 2 things" not in ask["summary"]
    assert park[schema.SITUATION_NAMES_KEY] == ["Jack"]
    assert schema.SITUATION_NAME_KEY not in park
    assert 'seems to be about "Jack"' in park["summary"]
    assert "named 2 things" not in park["summary"]


# ── a name that survives whitespace-stripping but NOT identity-cleaning ────────────────────────
# The two input classes the collapse actually CHANGED, both toward correctness and neither covered
# on either side of the change. `triage_entity` filters with `_clean_identity`, which strips shell
# and filename metacharacters (`#` among them, `report._UNSAFE_IN_IDENTITY`) — so `"###"` is a
# non-empty string that survives `.strip()` and comes back EMPTY from the identity clean. Nothing
# in the suite fed a name of that shape to a park before now, on `main` or here.
#
# The mechanism, once, since both cases below rest on it: the old code decided the routing and the
# wording in two different places with two different filters. `processing._triage` counted names
# with a whitespace-only strip, and `report.triage_entity_multi` re-filtered with `_clean_identity`
# and worded the result from ITS own count. Two counts of one list, and every disagreement between
# them was user-visible. Collapsing the routing/wording split is what removed the disagreement, and
# these are what pin that it stays removed.
def test_an_identity_stripped_name_beside_a_real_one_leaves_the_one_name_sentence():
    """OLD BEHAVIOUR (`main`): the routing counted `["Jack", "###"]` as TWO names on a
    whitespace-only strip and sent it to `triage_entity_multi`, which re-filtered with
    `_clean_identity`, was left with one name, and rendered the PLURAL sentence over it —
    literally "Your material named 1 things the entity registry doesn't recognize — "Jack"".
    A message that says "1 things" is how a reader learns nobody read what he was sent, and it was
    reachable from any capture naming one real entity and one punctuation-only token.

    One count now, taken after the one filter, so the sentence and the key cannot disagree.
    """
    park = report.triage_entity(names=["Jack", "###"])

    assert park[schema.SITUATION_NAMES_KEY] == ["Jack"]
    assert schema.SITUATION_NAME_KEY not in park
    assert 'seems to be about "Jack"' in park["summary"]
    assert "named 1 things" not in park["summary"]
    assert "named 2 things" not in park["summary"]
    assert "###" not in park["summary"], (
        "the identity clean exists because this value is offered to a steward as a shell "
        "`--name`; echoing it back in the sentence would keep it in front of a human anyway")
    assert park["open_question"] == "which entity is Jack?"


def test_a_park_whose_only_name_is_identity_stripped_falls_back_to_the_shared_placeholder():
    """OLD BEHAVIOUR (`main`): `["###"]` was ONE name by the routing's count, so it took the
    singular builder, which had no fallback at all — `_clean_identity("###")` returned `""` and the
    park was written with `entity_name: ""`. Downstream that row was a park that named nothing and
    said nothing: `subjects_of` answered `[]`, `subject_of` answered `""`, `mint_name_prefill`
    answered `""`, and a steward's console displayed a blank subject for a real parked capture.

    One fallback now, the SHARED `schema.UNNAMED_ENTITY_PLACEHOLDER`, which is the value both mint
    surfaces refuse BY VALUE — so the row says what happened instead of going blank, and still
    cannot be minted by an unchanged click. The end-to-end consequence of that pairing is pinned in
    `tests/entities/test_situations.py`.
    """
    park = report.triage_entity(names=["###"])

    assert park[schema.SITUATION_NAMES_KEY] == [schema.UNNAMED_ENTITY_PLACEHOLDER]
    assert schema.SITUATION_NAME_KEY not in park
    assert park[schema.SITUATION_NAMES_KEY] != [""], (
        "an empty-string name is the old shape: a park that named nothing AND said nothing")
    assert f'seems to be about "{schema.UNNAMED_ENTITY_PLACEHOLDER}"' in park["summary"]
    assert park["open_question"] == f"which entity is {schema.UNNAMED_ENTITY_PLACEHOLDER}?"
    # the placeholder is never re-spelled locally — a local copy of the words at any of the three
    # sides silently unrefuses it at whichever end holds it (`capture/schema.py`'s own comment)
    assert schema.UNNAMED_ENTITY_PLACEHOLDER == "something unnamed"


def test_the_ask_and_the_park_clean_the_same_ragged_name_to_different_ends():
    """CHARACTERIZATION, and the reason the two cases above are about `triage_entity` alone: the
    ASK cleans with `_clean` (prose quoted back at the submitter, control characters flattened,
    nothing else removed) and the PARK cleans with `_clean_identity` (a value a steward is invited
    to paste into a shell). `"###"` therefore survives into the question and is dropped from the
    park — the SAME capture is a two-name ask and a one-name park.

    Pinned as CURRENT and defensible, not as desirable: the divergence is a deliberate consequence
    of the two fields having different jobs (`report.py`'s own note above `_UNSAFE_IN_IDENTITY`),
    and it is exactly the kind of thing a later "make the two builders share one filter" tidy-up
    would erase in one direction or the other without noticing it changed what a human is asked.
    """
    ask = report.needs_input(submission_id=42, names=["Jack", "###"], candidates=[],
                             total_candidates=0)
    park = report.triage_entity(names=["Jack", "###"])

    assert ask["unresolved_names"] == ["Jack", "###"]
    assert park[schema.SITUATION_NAMES_KEY] == ["Jack"]
    assert "names 2 things" in ask["summary"]
    assert "named 2 things" not in park["summary"]
