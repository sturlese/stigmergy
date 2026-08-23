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
    _register(reg, "acme-corp", "Acme Corp")
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
    """The verbatim archive, on the ONE report every capture gets — the sentence for the human, the
    list for a caller, and the citation stated so nobody learns it from `git show`.

    OLD BEHAVIOUR: `source_pages` was `report.filed_meeting`'s field and `filed` carried a slimmer
    copy of it for the fast lane. That builder is deleted: every kind of material takes the same
    road, so `filed` is where the archive path is stated and there is no second shape to compare.
    """
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
                       anchoring={"kind": "entity", "entities": ["acme-corp"]}, links=[],
                       overlaps=[], findings=[], registry=_registry())
    assert out["anchored_to"] == "Acme Corp (`acme-corp`)"


def test_filed_with_a_registry_resolves_whatever_the_outcome_declared_id_name_or_alias():
    """The agent's outcome may have declared an id, a name or an alias — whichever it was, the
    phrase always names the CANONICAL id, the same one the page's frontmatter carries."""
    for declared in ("acme-corp", "Acme Corp"):
        out = report.filed(page_path="wiki/notes/X.md", commit="abc123",
                           anchoring={"kind": "entity", "entities": [declared]}, links=[],
                           overlaps=[], findings=[], registry=_registry())
        assert out["anchored_to"] == "Acme Corp (`acme-corp`)", declared


def test_filed_with_multiple_entities_and_a_registry_joins_id_name_pairs():
    reg = _registry()
    _register(reg, "globex", "Globex")
    out = report.filed(page_path="wiki/notes/X.md", commit="abc123",
                       anchoring={"kind": "entity", "entities": ["acme-corp", "globex"]}, links=[],
                       overlaps=[], findings=[], registry=reg)
    assert out["anchored_to"] == "Acme Corp (`acme-corp`), Globex (`globex`)"


def test_filed_with_two_spellings_of_the_same_entity_dedupes_to_one():
    """`["acme-corp", "Acme Corp"]` — an id and a display name for the SAME entity — must read as ONE
    entity here, mirroring `gates.resolve_entity_ids`'s own dedup. This used to read "Acme Corp
    (`acme-corp`), Acme Corp (`acme-corp`)" beside a page whose (deduplicated) `entity:` frontmatter carried
    the id once — the exact vocabulary mismatch the anchor phrase exists to prevent."""
    out = report.filed(page_path="wiki/notes/X.md", commit="abc123",
                       anchoring={"kind": "entity", "entities": ["acme-corp", "Acme Corp"]}, links=[],
                       overlaps=[], findings=[], registry=_registry())
    assert out["anchored_to"] == "Acme Corp (`acme-corp`)"


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
    reason = ('resolved "Cofers, S.L." to acme-corp: same company, legal-form suffix; three anchored '
              "pages match the billing context")
    out = report.filed(page_path="wiki/notes/X.md", commit="abc123",
                       anchoring={"kind": "entity", "entities": ["acme-corp"], "reason": reason},
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
                       anchoring={"kind": "entity", "entities": ["acme-corp"],
                                  "reason": "same company, a legal form"},
                       links=[], overlaps=[], findings=[], registry=_registry())

    assert out["anchored_to"] == "Acme Corp (`acme-corp`)"


def test_an_entity_anchor_with_no_stated_reason_reads_exactly_as_it_always_did():
    """The benign twin, and it is the common case: most captures name their entity plainly and
    there is nothing to explain. A report that grew an empty `Resolved:` clause on every filing
    would train a reader to skip the one line this change exists to make them read."""
    out = report.filed(page_path="wiki/notes/X.md", commit="abc123",
                       anchoring={"kind": "entity", "entities": ["acme-corp"]},
                       links=[], overlaps=[], findings=[], registry=_registry())

    assert out["anchor_reason"] == ""
    assert report.RESOLUTION_PREFIX not in out["summary"]
    assert out["summary"].startswith(f"{schema.FILED} — wiki/notes/X.md@abc123, anchored to "
                                     "Acme Corp (`acme-corp`). ")


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
                       anchoring={"kind": "entity", "entities": ["acme-corp"], "reason": hostile},
                       links=[], overlaps=[], findings=[], registry=_registry(),
                       agent_rationale=hostile)

    assert "\x1b" not in out["anchor_reason"] and "\n" not in out["anchor_reason"]
    # Compared against the sibling field rather than against a length: `agent_rationale` is the
    # module's existing prose-from-the-agent field, and asserting a number here would be a second
    # copy of a truncation rule (`text.clamp` marks the cut, so it is not simply the width).
    assert out["anchor_reason"] == out["agent_rationale"]


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


def test_failed_system_reads_differently_from_every_rejected_shape():
    out = report.failed_system(attempts=3, stage="zone", reason="ordinary material vetoed")
    assert out["status"] == schema.FAILED
    assert "3" in out["summary"]
    assert "not your capture" in out["summary"]
    assert "operator" in out["summary"]
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
    reason = ("rewrote existing content in wiki/notes/Git as the Canonical Store.md: "
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


# The `filed_meeting` branch of `render_prose` is GONE, and with it the test that pinned it.
# Nothing writes that key and nothing reads it any more: the separate meeting road, its report
# builder and the branch that kept `render_prose` from double-rendering its field block were
# removed together. An old row in `capture_queue` still carries the key and now renders the
# ordinary field block beside its stored summary — visibly redundant, never wrong, and the
# alternative was keeping a branch alive for rows nobody files any more.



# ── echoed text is sanitized (the same seam capture.cli uses for untrusted terminal text) ───────
def test_echoed_page_path_with_control_characters_is_sanitized_in_the_summary():
    hostile_path = "wiki/notes/evil\x1b[31mred.md"
    out = report.rejected_duplicate(page_path=hostile_path, as_of="2026-01-01")
    assert "\x1b" not in out["summary"]


# ── DELETED with `report.filed_meeting`, the builder for a page SET filed down its own road ────
# `test_filed_meeting_names_every_page_path_and_every_decisions_own_anchor`,
# `test_filed_meeting_with_zero_decisions_says_so_rather_than_omitting_the_line`,
# `test_filed_meeting_names_its_births_in_the_rendered_block_and_the_fact_set` and
# `test_a_meeting_reports_the_resolution_of_each_decisions_own_anchor` all called that builder, and
# every sentence they pinned was composed inside it. There is one road now, so there is one
# builder: what they measured survives here on `filed` — the anchor phrase and the resolution note
# (the `Resolved:` tests above), the births clause (the births section below), the searchability
# wording (pinned literally on `filed`'s plain and overlap branches, which are the two copies of it
# left in `report.py`), and the page SET itself, which is `filed(pages_filed=...)` now and is
# pinned end to end in `tests/librarian/test_structured_processing_pg.py`.
#
# What died with the builder and has NO home: a report that states a DIFFERENT anchor per filed
# page. `filed` takes one `anchoring` for the whole capture, so the per-decision anchor rows and
# their per-decision resolution notes are not expressible in the report layer at all — the pages
# still carry their own `entity:` frontmatter, and that is where the per-page anchor is asserted
# now (`test_structured_processing_pg.py`, reading the committed pages back out of git).
#
# `render_prose`'s branch on a STORED `filed_meeting` key is a different property and it is alive:
# see `test_render_prose_of_a_stored_filed_meeting_row_does_not_double_render_its_field_block`.


# ── births: the page landed AND the capture introduced these identities ──────────────────────
def test_filed_with_a_born_entity_says_so_beside_the_anchor_and_carries_the_fact_set():
    """OLD BEHAVIOUR: the sentence said the identity was "created unconfirmed" and that a steward
    still "confirms, merges or declines" it. the capture is the approval, so the submitter
    is told what their capture INTRODUCED and who confirmed it — them — with nothing left pending.
    "filed" alone would still hide that the registry grew."""
    reg = _registry()
    _register(reg, "scircle", "Scircle")
    out = report.filed(page_path="wiki/notes/S.md", commit="abc123",
                       anchoring={"kind": "entity", "entities": ["Scircle"]}, links=[],
                       overlaps=[], findings=[], registry=reg,
                       entities_born=[{"id": "scircle", "name": "Scircle",
                                       "type": "organization", "confirmed_by": "marc"}],
                       aliases_added=[{"entity": "acme-corp", "alias": "Acme Corporation"}])
    assert out["anchored_to"] == "Scircle (`scircle`)"
    assert "It introduces 1 new entity: Scircle (`scircle`)" in out["summary"]
    assert "the identity is confirmed by you" in out["summary"]
    assert "unconfirmed" not in out["summary"]
    assert 'It teaches the registry 1 new spelling: "Acme Corporation" for `acme-corp`' in out["summary"]
    assert out["entities_born"] == [{"id": "scircle", "name": "Scircle", "type": "organization",
                                     "confirmed_by": "marc"}]
    assert out["aliases_added"] == [{"entity": "acme-corp", "alias": "Acme Corporation"}]
    prose = report.render_prose(out)
    assert "entities_born Scircle (scircle)" in prose
    assert "aliases_added Acme Corporation -> acme-corp" in prose


def test_an_ordinary_filing_carries_empty_birth_lists_and_an_unchanged_sentence():
    """The benign twin: nothing born, nothing added — the common case reads as it always did,
    and the two keys are present (empty) so a reader never has to `.get()` them."""
    out = report.filed(page_path="wiki/notes/X.md", commit="abc123",
                       anchoring={"kind": "entity", "entities": ["Acme Corp"]}, links=[],
                       overlaps=[], findings=[], registry=_registry())
    assert out["entities_born"] == [] and out["aliases_added"] == []
    assert "introduces" not in out["summary"]
    assert "entities_born" not in report.render_prose(out)


def test_a_born_name_is_cleaned_through_the_identity_cleaner_never_echoed_raw():
    out = report.filed(page_path="wiki/notes/X.md", commit="abc123",
                       anchoring={"kind": "company", "reason": "r"}, links=[], overlaps=[],
                       findings=[],
                       entities_born=[{"id": "x", "name": "Evil\x1b[31m `$(rm)`",
                                       "type": "organization"}])
    assert "\x1b" not in out["summary"] and "$(" not in out["summary"]
