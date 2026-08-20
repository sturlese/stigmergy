"""`entities.situations` — which parked rows are an identity decision (pure, over a plain dict —
module docstring: "`classify(row)` takes a plain dict"). No database here; the three functions that
open one are exercised against real Postgres elsewhere:

- `list_pending_situations` and `get_situation` through `AdminService.entities_list`/
  `entities_show` in `tests/admin/test_service_pg.py`, and over HTTP on both entity routes in
  `tests/admin/test_routes_pg.py`.
- `require_situation` through `stigmergy-entities approve` against a real DSN in
  `tests/librarian/test_entity_full_circle_pg.py`, and its refusal through the console door in
  `tests/admin/test_service_pg.py`.

`entities.cli`'s own tests stub all three (that suite drives argument parsing and output). This
file is the pure-function layer underneath all of them.
"""
import pytest

from stigmergy.capture import schema
from stigmergy.entities import situations
from stigmergy.entities.errors import EntityError


def _row(*, status=schema.TRIAGE, report=None) -> dict:
    return {"status": status, "report": report or {}}


# ── classify: the current, coded vocabulary ───────────────────────────────────────────────────────
def test_classify_reads_the_declared_situation_code():
    row = _row(report={schema.SITUATION_KEY: schema.SITUATION_UNRESOLVED_ENTITY})
    assert situations.classify(row) == schema.SITUATION_UNRESOLVED_ENTITY


def test_classify_is_empty_for_a_row_not_parked_in_triage():
    row = _row(status=schema.QUEUED, report={schema.SITUATION_KEY:
                                             schema.SITUATION_UNRESOLVED_ENTITY})
    assert situations.classify(row) == ""


def test_classify_is_empty_for_an_unrecognized_situation_code():
    row = _row(report={schema.SITUATION_KEY: "some-future-situation-this-code-does-not-know"})
    assert situations.classify(row) == ""


def test_classify_handles_a_row_with_no_report_at_all():
    assert situations.classify({"status": schema.TRIAGE}) == ""
    assert situations.classify({}) == ""
    assert situations.classify(None) == ""


# ── classify: the legacy fallback (the shape that predates the coded vocabulary) ─────────────────
def test_classify_falls_back_to_the_legacy_which_entity_is_prefix():
    """The legacy hint shape predates `schema.SITUATION_KEY` — `report.triage_entity`'s prefix."""
    row = _row(report={"open_question": "which entity is this material about? Candidates: ..."})
    assert situations.classify(row) == schema.SITUATION_UNRESOLVED_ENTITY


def test_classify_falls_back_to_the_legacy_where_does_prefix():
    """The other legacy shape: `open_question: "where does a person page belong?"`."""
    row = _row(report={"open_question": "where does a person page belong?"})
    assert situations.classify(row) == schema.SITUATION_UNSUPPORTED_TYPE


def test_classify_prefers_the_coded_situation_over_the_legacy_prefix_when_both_are_present():
    row = _row(report={schema.SITUATION_KEY: schema.SITUATION_UNSUPPORTED_TYPE,
                      "open_question": "which entity is this about?"})
    assert situations.classify(row) == schema.SITUATION_UNSUPPORTED_TYPE


def test_classify_is_case_and_whitespace_tolerant_on_the_legacy_prefix():
    row = _row(report={"open_question": "  WHICH ENTITY is this about?"})
    assert situations.classify(row) == schema.SITUATION_UNRESOLVED_ENTITY


def test_classify_does_not_match_an_unrelated_question():
    row = _row(report={"open_question": "please clarify the date range"})
    assert situations.classify(row) == ""


# ── subject_of: the fact BESIDE the sentence ──────────────────────────────────────────────────────
def test_subject_of_unresolved_entity_is_the_name():
    row = _row(report={schema.SITUATION_KEY: schema.SITUATION_UNRESOLVED_ENTITY,
                      schema.SITUATION_NAME_KEY: "Acme Corp"})
    assert situations.subject_of(row) == "Acme Corp"


def test_subject_of_unsupported_type_is_the_judged_type():
    row = _row(report={schema.SITUATION_KEY: schema.SITUATION_UNSUPPORTED_TYPE,
                      schema.SITUATION_TYPE_KEY: "person"})
    assert situations.subject_of(row) == "person"


def test_subject_of_a_legacy_row_is_honestly_empty():
    """A legacy row carries no `entity_name`/`judged_type` at all — answered honestly rather than
    parsed back out of a sentence (module docstring)."""
    row = _row(report={"open_question": "where does a person page belong?"})
    assert situations.subject_of(row) == ""


# ── subjects_of: the per-name list, pinned as it BEHAVES ─────────────────────────────────────────
# CHARACTERIZATION. `subjects_of` had no direct test of its own — it was reachable only through the
# two API shapers that call it (`server.review._collect_open_items`, `admin.service._situation`),
# both of which exercise the two tidy cases and none of the ragged ones. It is the value the two
# mint doors decide a `Name` prefill from, so every ragged case below is a real string a steward
# can be handed for an irreversible mint.
#
# These tests record what the function DOES today, quirks included. They are not a statement that
# each answer is the desirable one: two of them (padding survives the plural path but not the
# singular one; `subject` and `subjects` disagree for an all-blank plural list) are arguably
# defects. They are here so that a restructuring which "obviously" normalizes has to notice it is
# changing behaviour and say so, rather than discover it in the knowledge repo.
def test_characterization_the_plural_list_is_returned_verbatim_but_the_singular_key_is_stripped():
    """The asymmetry a single consolidated decision would erase. `SITUATION_NAMES_KEY` entries are
    filtered on `.strip()` but returned UNSTRIPPED, so `"  Jack  "` survives with its padding all
    the way to the mint modal's `initial_value`; the singular `SITUATION_NAME_KEY` is stripped
    before it is wrapped. Same name, same steward, two different strings depending only on which
    key the librarian happened to write."""
    plural = _row(report={schema.SITUATION_KEY: schema.SITUATION_UNRESOLVED_ENTITY,
                          schema.SITUATION_NAMES_KEY: ["  Jack  "]})
    singular = _row(report={schema.SITUATION_KEY: schema.SITUATION_UNRESOLVED_ENTITY,
                            schema.SITUATION_NAME_KEY: "  Jack  "})

    assert situations.subjects_of(plural) == ["  Jack  "]
    assert situations.subjects_of(singular) == ["Jack"]


def test_characterization_two_identical_names_count_as_several_rather_than_one():
    """No de-duplication anywhere. A park that names the same unresolved entity twice is a
    TWO-name park: `subjects_of` returns both, `subject_of` joins them into `"Jack, Jack"`, and
    every consumer of the one-vs-several rule therefore takes the several branch — the mint modal
    blanks its `Name` field and lists "Jack" twice. A consolidation that de-duplicates would flip
    this row to a prefilled single name, which is a behaviour change and not a tidy-up."""
    row = _row(report={schema.SITUATION_KEY: schema.SITUATION_UNRESOLVED_ENTITY,
                       schema.SITUATION_NAMES_KEY: ["Jack", "Jack"]})

    assert situations.subjects_of(row) == ["Jack", "Jack"]
    assert situations.subject_of(row) == "Jack, Jack"


def test_characterization_a_blank_entry_beside_a_real_name_is_dropped():
    """The plural list is filtered on `.strip()`, so a list carrying one real name and one blank is
    a ONE-name park — the counterpart of `tests/slack/test_render.py`'s own blank-entry test, here
    at the layer that decides the count rather than at the layer that renders it."""
    row = _row(report={schema.SITUATION_KEY: schema.SITUATION_UNRESOLVED_ENTITY,
                       schema.SITUATION_NAMES_KEY: ["Jack", "   "]})

    assert situations.subjects_of(row) == ["Jack"]


def test_characterization_an_all_blank_plural_list_makes_subject_and_subjects_disagree():
    """The plural key is consulted when it is a non-empty LIST — before its entries are filtered —
    so `["  "]` takes the plural branch, empties itself, and never falls back to the singular key.
    `subject_of` reaches that same singular key through its own fallback and answers "Jack". The
    row therefore DISPLAYS a name it offers nothing to act on: the console shows "Jack" and the
    mint form is handed an empty list. Pinned because it is exactly the divergence a single
    decision function is likely to resolve silently, in either direction."""
    row = _row(report={schema.SITUATION_KEY: schema.SITUATION_UNRESOLVED_ENTITY,
                       schema.SITUATION_NAMES_KEY: ["  "],
                       schema.SITUATION_NAME_KEY: "Jack"})

    assert situations.subjects_of(row) == []
    assert situations.subject_of(row) == "Jack"


def test_characterization_a_plural_key_that_is_not_a_list_is_ignored_for_the_singular_one():
    """`isinstance(..., list)` guards the plural branch, so a malformed report whose `entity_names`
    is a bare string is not iterated character by character — it is ignored, and the singular key
    answers. Worth pinning: the alternative reading of the same data ("Otto" ignored, "Jack" split
    into four one-letter names) is a plausible refactoring accident, and every one of those letters
    would be offered as a mintable name."""
    row = _row(report={schema.SITUATION_KEY: schema.SITUATION_UNRESOLVED_ENTITY,
                       schema.SITUATION_NAMES_KEY: "Jack",
                       schema.SITUATION_NAME_KEY: "Otto"})

    assert situations.subjects_of(row) == ["Otto"]


def test_characterization_subjects_of_is_empty_for_every_row_that_is_not_an_unresolved_entity():
    """The guard the whole per-name list rests on: `unsupported-type` has a judged TYPE as its
    subject and no name to place, a non-triage row is not a situation at all, and a legacy row
    carries neither key. All three answer `[]` — never a one-element list holding the type, which
    a mint form would offer a steward as an entity called "person"."""
    unsupported = _row(report={schema.SITUATION_KEY: schema.SITUATION_UNSUPPORTED_TYPE,
                               schema.SITUATION_TYPE_KEY: "person"})
    not_parked = _row(status=schema.QUEUED,
                      report={schema.SITUATION_KEY: schema.SITUATION_UNRESOLVED_ENTITY,
                              schema.SITUATION_NAME_KEY: "Jack"})
    legacy = _row(report={"open_question": "which entity is this material about?"})

    assert situations.subjects_of(unsupported) == []
    assert situations.subjects_of(not_parked) == []
    assert situations.subjects_of(legacy) == []
    # the legacy row is still CLASSIFIED as an unresolved entity — it is the names it has none of
    assert situations.classify(legacy) == schema.SITUATION_UNRESOLVED_ENTITY


# ── the LEGACY ROW, written literally, never round-tripped ───────────────────────────────────────
# A park writes one shape now — `entity_names`, a list, whatever the count — and nothing anywhere
# writes `entity_name` any more. The read side keeps understanding it FOREVER, because rows parked
# before that change are never migrated: a reader that dropped the fallback would blank a live
# steward's queue on the day it shipped, and no test that goes through a builder could catch it,
# because no builder can produce the shape any more.
#
# So this fixture is a LITERAL: raw strings, no `schema.*` constants, no builder. Two properties
# fall out of that choice and neither is available any other way —
#
#   * it keeps testing the legacy shape after nothing writes it, where a round-trip version would
#     quietly start testing whatever the current builder emits while still carrying the old name;
#   * it pins the WIRE VALUES a live row actually carries. A rename of `SITUATION_NAME_KEY`'s
#     VALUE would leave every constant-based test in this file green and every pre-collapse row
#     unreadable; this one goes red, which is the correct answer.
#
# All three readers a steward's surfaces go through are asserted, because they are three different
# functions and only `subjects_of` has the fallback written in it: `subject_of` and
# `mint_name_prefill` inherit it, and an edit that gave either one its own name lookup would be
# invisible here otherwise.
LEGACY_ROW = {"status": "triage",
              "report": {"situation": "unresolved-entity", "entity_name": "Jack"}}


def test_a_row_written_before_the_plural_collapse_still_reads_correctly():
    assert situations.subjects_of(LEGACY_ROW) == ["Jack"]
    assert situations.subject_of(LEGACY_ROW) == "Jack"
    assert situations.mint_name_prefill(LEGACY_ROW) == "Jack"


def test_a_legacy_row_is_a_one_name_park_everywhere_the_count_is_read():
    """The same row through `_situation_view`, which is what both mint doors and the admin console
    are actually handed. A legacy row must be indistinguishable from a modern one-name park at that
    layer — otherwise the one-vs-several rule takes a different branch for a row whose only sin is
    its age, and a steward is offered a blank `Name` field for a park that names exactly one
    thing."""
    view = situations._situation_view(LEGACY_ROW)
    modern = situations._situation_view(
        {"status": "triage",
         "report": {"situation": "unresolved-entity", "entity_names": ["Jack"]}})

    assert view["situation"] == modern["situation"] == "unresolved-entity"
    assert view["subject"] == modern["subject"] == "Jack"
    assert view["subjects"] == modern["subjects"] == ["Jack"]
    assert view["mint_name_prefill"] == modern["mint_name_prefill"] == "Jack"


# ── the invariant the CLI's printed `--name` rests on ────────────────────────────────────────────
def test_when_subjects_of_is_empty_subject_of_is_the_raw_singular_name_and_never_a_join():
    """`entities.cli._cmd_show` prints its next commands from `row.get("subjects") or [subject]`,
    and every entry of that list is pasted into a `stigmergy-entities approve … --name <value>`
    line a human is invited to RUN. This pins what that fallback can actually reach.

    The fallback is taken ONLY when `subjects_of` answered `[]`, and `subject_of` builds its
    `", ".join(...)` display string ONLY when `subjects_of` answered something. The joined form —
    a compound string that is nobody's real name, and would mint one garbled entity where two were
    meant — is therefore structurally unreachable from that fallback: what reaches it is the row's
    raw singular `SITUATION_NAME_KEY`, verbatim. Nothing in either signature says so, so a later
    edit to `subject_of`'s branch order, or a fallback that starts consulting the plural key, would
    begin feeding a compound name into a printed command with no existing test noticing.

    Deliberately NOT asserted as `", " not in subject_of(row)`. That reads as "a printed name never
    contains a comma", which is a different and false claim: a librarian may legitimately park
    `"Acme, Inc."` under the singular key — and that row's `subjects_of` is NON-empty, so the
    fallback is not taken for it at all (pinned below). The invariant is that no JOIN ran.
    """
    fallback_shapes = {
        "no name key at all": {},
        "a blank singular key": {schema.SITUATION_NAME_KEY: "   "},
        "an all-blank plural list beside a real singular key": {
            schema.SITUATION_NAMES_KEY: ["  "], schema.SITUATION_NAME_KEY: "Jack"},
        "an all-blank plural list and no singular key": {schema.SITUATION_NAMES_KEY: [""]},
    }
    for label, report in fallback_shapes.items():
        row = _row(report={schema.SITUATION_KEY: schema.SITUATION_UNRESOLVED_ENTITY, **report})
        raw_singular = str(report.get(schema.SITUATION_NAME_KEY) or "")

        assert situations.subjects_of(row) == [], f"{label}: this row must take the CLI's fallback"
        assert situations.subject_of(row) == raw_singular, (
            f"{label}: the fallback value must be the singular key VERBATIM — anything else means "
            f"a name the row was never parked with is on its way into a printed --name")
        # `entities.cli._cmd_show`'s own expression, reproduced rather than imported (the CLI's
        # tests stub `get_situation`; this is the pure layer that decides what it is handed).
        printed = situations.subjects_of(row) or [situations.subject_of(row)]
        assert printed == [raw_singular], label

    several = _row(report={schema.SITUATION_KEY: schema.SITUATION_UNRESOLVED_ENTITY,
                           schema.SITUATION_NAMES_KEY: ["Jack", "Acme Capital"]})
    assert situations.subject_of(several) == "Jack, Acme Capital", "the join exists…"
    assert (situations.subjects_of(several) or [situations.subject_of(several)]) == [
        "Jack", "Acme Capital"], (
        "…and is never what the CLI prints: a row that HAS a joined display string has a non-empty "
        "`subjects_of`, so the fallback is skipped and each name is checked and printed on its own")

    comma_name = _row(report={schema.SITUATION_KEY: schema.SITUATION_UNRESOLVED_ENTITY,
                              schema.SITUATION_NAME_KEY: "Acme, Inc."})
    assert situations.subjects_of(comma_name) == ["Acme, Inc."], (
        "a comma in a real single name is DATA, not a join — it keeps `subjects_of` non-empty, so "
        "this row never reaches the fallback either")


# ── mint_name_prefill: the one-vs-several decision, at the ONE place that takes it ───────────────
# This is the home of a rule that used to be written three times — once in
# `slack.render.render_entity_mint_modal`, once in the admin console's `entityApproveFlow`, and
# nowhere provable in Python. Both mint doors now read what this function answers, so every case
# below is a case a steward can be handed for an irreversible mint, and the two surfaces cannot
# disagree about any of them by construction.
#
# Directly on the pure function: a dict in, a string out, no database and no test double. The
# equivalents that used to live as renderer-derives-it assertions in `tests/slack/test_render.py`
# (one name prefills, several do not, a blank entry does not count) are HERE now; that file pins
# what the renderer does with the decision it is handed, which is a different claim.
def test_a_park_naming_exactly_one_unresolved_entity_prefills_that_name():
    """The overwhelming majority of parks, and the case a prefill exists for: one name to mean, so
    the mint form may default to it."""
    row = _row(report={schema.SITUATION_KEY: schema.SITUATION_UNRESOLVED_ENTITY,
                       schema.SITUATION_NAMES_KEY: ["Globex Robotics"]})

    assert situations.mint_name_prefill(row) == "Globex Robotics"


def test_a_park_naming_two_unresolved_entities_prefills_nothing():
    """**The C-3 contract.** No single string is the right default for a two-name park: the joined
    display form (`subject_of`'s "Jack, Acme Capital") is neither name, and either name alone
    silently drops the other. `""` is the instruction to the surface — leave the field empty and
    list `subjects` — not an absence of an answer."""
    row = _row(report={schema.SITUATION_KEY: schema.SITUATION_UNRESOLVED_ENTITY,
                       schema.SITUATION_NAMES_KEY: ["Jack", "Acme Capital"]})

    assert situations.mint_name_prefill(row) == ""
    # and the names themselves are still there for the surface to list — an empty required field
    # with nothing beside it is a riddle
    assert situations.subjects_of(row) == ["Jack", "Acme Capital"]


def test_a_park_with_no_unresolved_names_at_all_prefills_nothing():
    """Zero names is not an error — a park whose plural list is empty (or whose keys are absent)
    has nothing to offer, and the modal still opens with a field a steward fills by hand."""
    empty_list = _row(report={schema.SITUATION_KEY: schema.SITUATION_UNRESOLVED_ENTITY,
                              schema.SITUATION_NAMES_KEY: []})
    no_keys = _row(report={schema.SITUATION_KEY: schema.SITUATION_UNRESOLVED_ENTITY})

    assert situations.mint_name_prefill(empty_list) == ""
    assert situations.mint_name_prefill(no_keys) == ""


def test_an_unsupported_type_park_never_prefills_the_judged_type():
    """The type is not a name. `unsupported-type`'s subject is "a page about one specific person",
    which is a JUDGEMENT about the material — a prefill carrying it would offer a steward an entity
    called "person" as the default of a form whose submission pushes a signed commit."""
    row = _row(report={schema.SITUATION_KEY: schema.SITUATION_UNSUPPORTED_TYPE,
                       schema.SITUATION_TYPE_KEY: "person"})

    assert situations.mint_name_prefill(row) == ""
    assert situations.subject_of(row) == "person", "sanity: the type IS this row's subject"


def test_a_legacy_row_carrying_only_the_singular_name_key_prefills_that_name():
    """The row shape that predates `SITUATION_NAMES_KEY`: one name under the singular key and no
    plural list beside it. `subjects_of`'s fallback makes it a one-element list, so it is a
    one-name park and it prefills — the migration must not blank the field for every row written
    before the plural key existed. Note the singular key is STRIPPED on the way out, unlike the
    plural one (pinned above)."""
    row = _row(report={schema.SITUATION_KEY: schema.SITUATION_UNRESOLVED_ENTITY,
                       schema.SITUATION_NAME_KEY: "  Jack  "})

    assert situations.mint_name_prefill(row) == "Jack"


def test_a_blank_entry_beside_a_real_name_still_leaves_a_one_name_park():
    """The blank is dropped before the count, so `["Jack", "   "]` is ONE name and prefills. This
    assertion used to live in `tests/slack/test_render.py`, where it measured the renderer's own
    filtering; it belongs here now, at the layer that decides."""
    row = _row(report={schema.SITUATION_KEY: schema.SITUATION_UNRESOLVED_ENTITY,
                       schema.SITUATION_NAMES_KEY: ["Jack", "   "]})

    assert situations.mint_name_prefill(row) == "Jack"


# ── the three quirk twins: CURRENT behaviour, deliberately preserved by the consolidation ───────
# Each of these is arguably a defect, and each was left exactly as it was: the consolidation's
# contract was "one place decides, and it decides what the two places decided before". They are
# pinned as CURRENT, not as DESIRABLE — changing any of them changes what gets minted, which needs
# a decision behind it (`tests/entities/test_situations.py`'s characterization block above pins the
# `subjects_of` half of each).
def test_current_behaviour_a_padded_single_name_is_prefilled_with_its_padding_intact():
    """The plural key's entries are filtered on `.strip()` and returned UNSTRIPPED, so the padding
    a park wrote rides into the mint form's default — and a default is what most stewards submit
    unchanged. Trimming here is very likely an improvement; it is also a change to what gets
    minted, so it is a decision and not a tidy-up."""
    row = _row(report={schema.SITUATION_KEY: schema.SITUATION_UNRESOLVED_ENTITY,
                       schema.SITUATION_NAMES_KEY: ["  Jack  "]})

    assert situations.mint_name_prefill(row) == "  Jack  "


def test_current_behaviour_the_same_name_twice_is_a_several_names_park():
    """Nothing de-duplicates, so a park naming the same unresolved entity twice takes the several
    branch: empty field, "Jack" listed twice. This is the SAFE direction of a bug — the value a
    steward would otherwise accept unchanged stays blank — and de-duplicating would flip the row
    to a silent prefill, which reaches the knowledge repo."""
    row = _row(report={schema.SITUATION_KEY: schema.SITUATION_UNRESOLVED_ENTITY,
                       schema.SITUATION_NAMES_KEY: ["Jack", "Jack"]})

    assert situations.mint_name_prefill(row) == ""


def test_current_behaviour_an_all_blank_plural_list_prefills_nothing_though_the_row_displays_a_name():
    """The plural key is consulted when it is a non-empty LIST, before its entries are filtered, so
    `["  "]` takes the plural branch, empties itself, and never falls back to the singular key —
    while `subject_of` reaches that same singular key through its own fallback and answers "Jack".
    The row therefore DISPLAYS a name whose mint form is handed nothing. Pinned because it is
    exactly the divergence a consolidation resolves silently, in either direction."""
    row = _row(report={schema.SITUATION_KEY: schema.SITUATION_UNRESOLVED_ENTITY,
                       schema.SITUATION_NAMES_KEY: ["  "],
                       schema.SITUATION_NAME_KEY: "Jack"})

    assert situations.mint_name_prefill(row) == ""
    assert situations.subject_of(row) == "Jack"


# ── the placeholder refusal: the ONE value that is a name syntactically and nobody's name ────────
# `schema.UNNAMED_ENTITY_PLACEHOLDER` is what `librarian.report`'s park builders write when a
# capture parked with nothing left to name. It is a perfectly ordinary-looking string, so a mint
# door that treated it as an ordinary name would offer it as the DEFAULT of a form whose submit
# button signs a commit — and a steward who accepts the default mints an entity called "something
# unnamed" that thereafter RESOLVES for every future capture containing that phrase. Self-
# propagating, and a registry entry is not undone by deleting a row.
#
# Refused by VALUE at both mint surfaces, against the one shared constant. The sensitivity cases
# come first; the specificity twins are below them, because this gate can bounce a real name.
def test_the_no_name_placeholder_is_never_offered_as_a_prefill():
    """The gate. A one-name park is exactly the shape that DOES prefill — this one is refused not
    for its shape but for its value, which is the only thing that distinguishes it."""
    row = _row(report={schema.SITUATION_KEY: schema.SITUATION_UNRESOLVED_ENTITY,
                       schema.SITUATION_NAMES_KEY: [schema.UNNAMED_ENTITY_PLACEHOLDER]})

    assert situations.subjects_of(row) == [schema.UNNAMED_ENTITY_PLACEHOLDER], (
        "sanity: the row really is a one-name park — the refusal is about the VALUE, and a row "
        "that had already lost its name would prove nothing")
    assert situations.mint_name_prefill(row) == ""


def test_the_placeholder_cannot_be_smuggled_past_the_refusal_with_padding():
    """The plural key's entries are returned UNSTRIPPED (pinned above), so `"  something unnamed  "`
    is what a prefill would otherwise carry — a different string from the constant, and a byte
    comparison against the constant alone would let it through into the form. The check strips
    before it compares. Note the padded value is still what a REAL name would be prefilled with
    (also pinned above): the strip is the comparison's, not the answer's."""
    row = _row(report={schema.SITUATION_KEY: schema.SITUATION_UNRESOLVED_ENTITY,
                       schema.SITUATION_NAMES_KEY: [f"  {schema.UNNAMED_ENTITY_PLACEHOLDER}  "]})

    assert situations.mint_name_prefill(row) == ""


def test_a_legacy_row_carrying_the_placeholder_is_refused_the_same_way():
    """The singular key reaches the same decision through `subjects_of`'s permanent fallback, so a
    row parked before the plural collapse cannot be the one door that still offers it."""
    row = _row(report={schema.SITUATION_KEY: schema.SITUATION_UNRESOLVED_ENTITY,
                       schema.SITUATION_NAME_KEY: schema.UNNAMED_ENTITY_PLACEHOLDER})

    assert situations.subjects_of(row) == [schema.UNNAMED_ENTITY_PLACEHOLDER]
    assert situations.mint_name_prefill(row) == ""


def test_the_two_mint_surfaces_refuse_the_placeholder_against_the_SAME_constant():
    """The rule is "refused by value at every mint door", and there are two doors: this one (the
    form default) and `entities.cli._suggestable` (the ready-to-run command). Asserted together,
    because a rule enforced at one door and not the other is the shape this consolidation exists to
    end — and both must be reading the shared constant, never a local copy of the words, or
    renaming the librarian's fallback silently unrefuses it at whichever end still holds the old
    spelling."""
    from stigmergy.entities import cli

    row = _row(report={schema.SITUATION_KEY: schema.SITUATION_UNRESOLVED_ENTITY,
                       schema.SITUATION_NAMES_KEY: [schema.UNNAMED_ENTITY_PLACEHOLDER]})

    assert situations.mint_name_prefill(row) == ""
    assert cli._suggestable(schema.UNNAMED_ENTITY_PLACEHOLDER) is False
    # the benign direction of the same pair, so this is not two absence checks agreeing
    assert cli._suggestable("Globex Robotics") is True


# ── the specificity twins: the gate must not bounce a real name ─────────────────────────────────
def test_a_real_name_that_merely_contains_the_placeholder_words_still_prefills():
    """The refusal is an EQUALITY, not a substring or a prefix test. "Something Unnamed Records" is
    a name a person could really register, and a containment check would blank the field for it —
    a defense that bounces real work, which is the failure mode a gate with no benign twin ships
    with. Case is part of the value too: the librarian writes exactly one spelling."""
    for real_name in ("Something Unnamed Records", "Unnamed", "Something Unnamed",
                      "SOMETHING UNNAMED"):
        row = _row(report={schema.SITUATION_KEY: schema.SITUATION_UNRESOLVED_ENTITY,
                           schema.SITUATION_NAMES_KEY: [real_name]})

        assert situations.mint_name_prefill(row) == real_name, real_name


def test_the_placeholder_is_still_listed_as_the_rows_subject_even_though_it_cannot_prefill():
    """What is refused is the DEFAULT, never the display. A park whose only name is the placeholder
    still has to say so on the console and in the queue listing — "this capture named nothing" is
    the fact a steward needs in order to act, and blanking the row as well would hide it."""
    row = _row(report={schema.SITUATION_KEY: schema.SITUATION_UNRESOLVED_ENTITY,
                       schema.SITUATION_NAMES_KEY: [schema.UNNAMED_ENTITY_PLACEHOLDER]})
    view = situations._situation_view(row)

    assert view["subject"] == schema.UNNAMED_ENTITY_PLACEHOLDER
    assert view["subjects"] == [schema.UNNAMED_ENTITY_PLACEHOLDER]
    assert view["mint_name_prefill"] == ""


# ── the route the plural collapse created, round-tripped through the real builder ────────────────
# The one place a round trip is the claim rather than a shortcut (contrast the LEGACY_ROW fixture
# above, which is a literal precisely so it cannot follow the builder). The collapse gave the
# placeholder a SECOND way onto a row: a park whose only declared name is stripped away by
# `report._clean_identity` (`"###"` — shell metacharacters, nothing left) now falls back to the
# placeholder where the retired singular builder wrote `entity_name: ""`.
#
# So the writer's new fallback and the reader's new refusal have to be tested TOGETHER: separately,
# each is green while a mint form offers `something unnamed` as its default for a real parked row.
def test_a_park_whose_only_name_was_stripped_away_reaches_the_reader_as_a_refused_prefill():
    """OLD BEHAVIOUR: the singular builder wrote `entity_name: ""` for this capture, so
    `subjects_of` answered `[]`, `subject_of` answered `""` and `mint_name_prefill` answered `""` —
    a blank subject on a real parked row, and the prefill was empty by ACCIDENT (there was no name)
    rather than by refusal. The row now says what happened, and the prefill is still empty — this
    time because the value is refused.

    The distinction is the whole point: the same `""` from a different cause, and the cause is what
    a future change can break."""
    from stigmergy.librarian import report as report_module

    row = _row(report=report_module.triage_entity(names=["###"]))

    assert situations.classify(row) == schema.SITUATION_UNRESOLVED_ENTITY
    assert situations.subjects_of(row) == [schema.UNNAMED_ENTITY_PLACEHOLDER], (
        "the writer's fallback: the row names the placeholder rather than going blank")
    assert situations.mint_name_prefill(row) == "", (
        "the reader's refusal: what the writer put there is exactly what no mint door may default "
        "to — a steward clicking Create unchanged would sign 'something unnamed' into the registry")


def test_the_same_park_with_a_surviving_name_still_prefills_it_end_to_end():
    """The benign twin of the round trip: an ordinary one-name park, built by the SAME builder,
    still prefills. Without this, the test above passes for a `mint_name_prefill` that has stopped
    prefilling anything at all."""
    from stigmergy.librarian import report as report_module

    row = _row(report=report_module.triage_entity(names=["Globex Robotics"]))

    assert situations.subjects_of(row) == ["Globex Robotics"]
    assert situations.mint_name_prefill(row) == "Globex Robotics"


# ── require_situation: the write guard (three distinct refusals) ─────────────────────────────────
class _FakeConn:
    """Not a mock of an interface — `require_situation`/`get_situation` take `conn` only to hand it
    to `queue.get_submission_trace`, which this stubs at the module level below instead. Kept for
    readability at the call site; carries no behaviour of its own."""


def test_require_situation_refuses_a_nonexistent_row(monkeypatch):
    monkeypatch.setattr(situations.queue, "get_submission_trace", lambda conn, sid: None)
    with pytest.raises(EntityError, match="does not exist"):
        situations.require_situation(_FakeConn(), 999, action="approve")


def test_require_situation_refuses_a_row_not_parked_in_triage(monkeypatch):
    monkeypatch.setattr(situations.queue, "get_submission_trace",
                       lambda conn, sid: {"status": schema.CLAIMED, "report": {}})
    with pytest.raises(EntityError, match="claimed"):
        situations.require_situation(_FakeConn(), 41, action="approve")


def test_require_situation_refuses_a_triage_row_that_is_not_an_identity_situation(monkeypatch):
    monkeypatch.setattr(situations.queue, "get_submission_trace",
                       lambda conn, sid: {"status": schema.TRIAGE, "report": {}})
    with pytest.raises(EntityError, match="not an entity situation"):
        situations.require_situation(_FakeConn(), 41, action="approve")


def test_require_situation_returns_the_row_when_it_really_is_a_pending_situation(monkeypatch):
    """The benign twin of the three refusals above."""
    monkeypatch.setattr(situations.queue, "get_submission_trace",
                       lambda conn, sid: {"status": schema.TRIAGE, "id": 41,
                                          "report": {schema.SITUATION_KEY:
                                                    schema.SITUATION_UNRESOLVED_ENTITY,
                                                    schema.SITUATION_NAME_KEY: "Acme"}})
    row = situations.require_situation(_FakeConn(), 41, action="approve")
    assert row["situation"] == schema.SITUATION_UNRESOLVED_ENTITY
    assert row["subject"] == "Acme"


# ── `is_mintable_name`: the one comparison every listing of a park's names shares ───────────────
def test_is_mintable_name_refuses_exactly_the_placeholder_stripped():
    """The per-name surfaces (the console's "Mint «X»" cards) ask THIS rather than re-deriving the
    comparison; `mint_name_prefill` asks it too, so the prefill and the per-name listing cannot
    disagree about the one value neither may offer."""
    assert situations.is_mintable_name(schema.UNNAMED_ENTITY_PLACEHOLDER) is False
    assert situations.is_mintable_name(f"  {schema.UNNAMED_ENTITY_PLACEHOLDER}\t") is False
    assert situations.is_mintable_name("Acme Corp") is True
    assert situations.is_mintable_name("Something Unnamed Records") is True, "by value, not substring"
    assert situations.is_mintable_name("") is True, (
        "emptiness is the gate's refusal (`birth._clean_name`), not this predicate's — a blank is "
        "not the placeholder")
