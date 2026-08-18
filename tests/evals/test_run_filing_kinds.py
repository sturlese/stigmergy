"""`--kinds`: a subset is a DIFFERENT measurement, and every part of the instrument has to say so.

The filing golden was one set, scored whole, against pinned denominators. ADR 032 gave it a subset
mode so a backend that served the meeting flow only could be measured at all — and a subset is where
a score series can quietly start lying: three phases and twelve phases both render a table with the
same column headings, and a row in `history.ndjson` read two years later has nothing but its own
fields to say which one it was.

**ADR 033 lifted the RESTRICTION and kept the flag.** `_require_measurable_subset` — which refused
`--backend pydantic` anything but `--kinds meeting` — is gone with the limitation it described:
that backend serves every kind the golden set carries now. The four tests that pinned the refusal
went with it (see the code comment that replaced the function in `evals/run_filing.py`, which keeps
the RULE: a run that cannot measure a capture is refused before the queue is touched, never scored
as a failure). What is asserted in its place is the lifted state itself — the structured backend
parses and measures the WHOLE set, which is the invocation M1 refused.

So every seam the subset touches is pinned here, on both sides:

* the SELECTION refuses a kind the set does not contain, by name, rather than scoring nothing;
* the NARROWING takes both halves of the golden set with it, never the manifest alone;
* the DENOMINATOR check stops holding a subset against the whole set's pin — and still refuses a
  subset that would score nothing at all, and still refuses drift when the whole set IS being run;
* every backend argparse accepts is one dispatch knows, and vice versa;
* and `kinds` reaches the report, the caption a human screenshots, and the history row.

Pure: no Postgres, no git, no model. `_run` — the half that spends money — is never called here,
which is exactly the split `test_filing_scorer.py`'s own banner describes.
"""
import json

import pytest

from evals import eval_history, run_filing
from stigmergy.capture import schema

MEETING_IDS = ("F08-meeting-two-decisions", "F09-meeting-parks")

# How many captures the shipped set carries, DERIVED from the manifest rather than retyped: this
# number is not a claim these tests make about the set, it is the set's own size, and a literal here
# is one more place a grown golden set has to be chased to (issue #77 grew it by four).
SHIPPED_CAPTURES = len(json.loads(
    run_filing.FIXTURE.joinpath("captures", "manifest.json").read_text(encoding="utf-8"))["captures"])


@pytest.fixture(scope="module")
def golden():
    """The real manifest and expectations on disk — the set an operator actually subsets."""
    manifest = json.loads(run_filing.FIXTURE.joinpath("captures", "manifest.json")
                          .read_text(encoding="utf-8"))
    expectations = json.loads(run_filing.FIXTURE.joinpath("expected", "expectations.json")
                              .read_text(encoding="utf-8"))
    return manifest, expectations


# ── selecting the kinds ────────────────────────────────────────────────────────────────────────
def test_no_kinds_at_all_means_the_whole_set_and_not_an_empty_one(golden):
    """`None`, not `[]`. The difference is the whole design: `None` says "do not narrow" and takes
    the pinned-denominator path, while an empty list would narrow to nothing and take the subset
    path — a run that measures zero captures and prints a table."""
    manifest, _ = golden
    assert run_filing._select_kinds(manifest, "") is None
    assert run_filing._select_kinds(manifest, "   ") is None
    assert run_filing._select_kinds(manifest, None) is None


def test_an_unknown_kind_is_refused_by_name_rather_than_scoring_nothing(golden):
    """A typo that produced an empty run would print a table of zero denominators, which reads
    exactly like a backend that files nothing — the most expensive way to be wrong about an
    instrument. The refusal has to name the typo AND what the set actually carries."""
    manifest, _ = golden
    with pytest.raises(SystemExit) as exc_info:
        run_filing._select_kinds(manifest, "meetings")          # the plural, which is the typo

    message = str(exc_info.value)
    assert "meetings" in message
    assert schema.MEETING in message and "raw" in message


def test_a_known_kind_resolves_sorted_and_deduplicated(golden):
    """Its benign twin, and the normalisation: whitespace, repetition and order are the operator's,
    and the value that reaches the report must not be."""
    manifest, _ = golden
    assert run_filing._select_kinds(manifest, schema.MEETING) == [schema.MEETING]
    assert run_filing._select_kinds(manifest, " meeting , raw ,meeting ") == ["meeting", "raw"]


def test_one_unknown_kind_among_valid_ones_is_still_refused(golden):
    """A partially-valid list must not silently score its valid half — that is the shape in which a
    subset silently becomes a different subset."""
    manifest, _ = golden
    with pytest.raises(SystemExit, match="drive"):
        run_filing._select_kinds(manifest, "meeting,drive")


def test_the_manifests_own_kinds_are_what_the_selection_is_judged_against(golden):
    """The available list is derived from the file on disk, never a constant — a kind added to the
    golden set becomes selectable in the same commit, and one removed stops being."""
    manifest, _ = golden
    assert run_filing._manifest_kinds(manifest) == sorted(
        {capture["kind"] for capture in manifest["captures"]})


# ── narrowing both halves ──────────────────────────────────────────────────────────────────────
def test_the_subset_narrows_the_expectations_as_well_as_the_captures(golden):
    """**Both halves, or neither.** `_check_set`'s first refusal exists because the manifest and
    the expectations can disagree about what a capture is; a subset that narrowed only the manifest
    would manufacture that disagreement on every run."""
    manifest, expectations = golden

    sub_manifest, sub_expectations = run_filing._subset(manifest, expectations, [schema.MEETING])

    assert [c["id"] for c in sub_manifest["captures"]] == list(MEETING_IDS)
    assert [e["id"] for e in sub_expectations["expectations"]] == list(MEETING_IDS)
    assert all(c["kind"] == schema.MEETING for c in sub_manifest["captures"])


def test_the_subset_keeps_every_other_key_of_both_files(golden):
    """A subset is the same two documents with fewer entries — anything else in them (a version, a
    note, a frozen-at stamp) belongs to the set, not to the captures, and dropping it would make a
    subset run's provenance narrower than the run it came from."""
    manifest, expectations = golden
    sub_manifest, sub_expectations = run_filing._subset(manifest, expectations, [schema.MEETING])

    assert set(sub_manifest) == set(manifest)
    assert set(sub_expectations) == set(expectations)
    for key in set(manifest) - {"captures"}:
        assert sub_manifest[key] == manifest[key]


def test_the_expectations_are_narrowed_by_the_manifests_ids_and_not_by_a_kind_of_their_own(golden):
    """The yardstick file records no kind at all, and inventing one there would be a second place
    for the two halves to disagree. Asserted as a property of the FILE: if an expectation ever
    grows a `kind`, this goes red and the decision gets made deliberately."""
    _, expectations = golden
    assert not [e for e in expectations["expectations"] if "kind" in e]


# ── the denominator check, in both modes ───────────────────────────────────────────────────────
def test_the_whole_set_still_has_to_match_the_pin(golden):
    """The check the subset mode must not have weakened: run whole, the set still owes the pinned
    denominators, because that is what makes two full-set scores comparable per run."""
    manifest, expectations = golden
    run_filing._check_set(manifest, expectations)              # must not raise
    run_filing._check_set(manifest, expectations, whole_set=True)


def test_drift_in_the_whole_set_is_still_refused_and_names_the_facet(golden):
    """The sabotage twin for the pin itself. A capture removed from the shipped set changes a
    denominator, and a score recorded after it is not comparable to one recorded before — the
    refusal has to say which facet moved and in which direction."""
    manifest, expectations = golden
    dropped = {**expectations,
               "expectations": [e for e in expectations["expectations"]
                                if e["id"] != "F01-plain-note-known-entity"]}
    thinned = {**manifest,
               "captures": [c for c in manifest["captures"]
                            if c["id"] != "F01-plain-note-known-entity"]}

    with pytest.raises(SystemExit) as exc_info:
        run_filing._check_set(thinned, dropped, whole_set=True)

    message = str(exc_info.value)
    assert "EXPECTED_DENOMINATORS" in message
    assert "status" in message


def test_a_subset_is_not_held_against_the_whole_sets_pin(golden):
    """The point of the flag: the pin describes the shipped set and only it, so holding a subset
    against it would fail every subset by construction — the meeting-only measurement this
    milestone exists for could never run."""
    manifest, expectations = golden
    sub_manifest, sub_expectations = run_filing._subset(manifest, expectations, [schema.MEETING])

    run_filing._check_set(sub_manifest, sub_expectations, whole_set=False)   # must not raise

    with pytest.raises(SystemExit, match="EXPECTED_DENOMINATORS"):
        run_filing._check_set(sub_manifest, sub_expectations, whole_set=True)


def test_a_subset_that_would_score_nothing_is_still_refused(golden):
    """What a subset owes INSTEAD of the pin: it has to score something. A filter whose captures
    name no facet produces a table of empty denominators, which is the same misreadable output the
    unknown-kind refusal exists to prevent, arrived at by a different road."""
    manifest, expectations = golden
    facetless = {**expectations,
                 "expectations": [{"id": capture_id, "expect": {}} for capture_id in MEETING_IDS]}
    sub_manifest, _ = run_filing._subset(manifest, expectations, [schema.MEETING])

    with pytest.raises(SystemExit) as exc_info:
        run_filing._check_set(sub_manifest, facetless, whole_set=False)
    assert "scores no facet" in str(exc_info.value)


def test_a_subset_still_owes_the_three_checks_that_are_true_of_any_set(golden):
    """`whole_set=False` relaxes the fourth refusal and NOTHING else. An inconsistent subset — a
    capture with no expectation — must still be refused, or the flag would be a way to run a broken
    set rather than a smaller one."""
    manifest, expectations = golden
    sub_manifest, sub_expectations = run_filing._subset(manifest, expectations, [schema.MEETING])
    orphaned = {**sub_expectations,
                "expectations": [e for e in sub_expectations["expectations"]
                                 if e["id"] != MEETING_IDS[0]]}

    with pytest.raises(SystemExit, match="inconsistent"):
        run_filing._check_set(sub_manifest, orphaned, whole_set=False)


# ── the backend/subset pairing, after the restriction was lifted ───────────────────────────────
# **DELETED here, with the restriction they pinned (ADR 033):**
# `test_the_meeting_only_backend_refuses_a_subset_it_could_not_measure`,
# `test_the_meeting_only_backend_refuses_the_whole_set_too`,
# `test_the_meeting_only_subset_is_accepted_and_declares_itself_meeting_only` and
# `test_every_other_backend_may_measure_any_subset_and_is_never_meeting_only`. All four drove
# `run_filing._require_measurable_subset`, which no longer exists: `--backend pydantic` served the
# meeting flow only in M1, so a run that also submitted ordinary captures scored a column of
# refusals and called it a backend result. That backend serves both flows now, so there is no
# unmeasurable pairing left for the rig to refuse. Their rule — a run that cannot measure a capture
# is refused BEFORE the queue is touched, never scored as a failure — is recorded in
# `evals/run_filing.py` where the function was, for the next backend/subset pairing somebody adds.
#
# What follows is the lifted state, which nothing asserted before because it was refused.
def test_the_runner_carries_no_backend_subset_refusal_at_all_any_more():
    """The removal, asserted directly rather than only by the absence of four tests.

    A helper that came back — under this name or another — would silently re-impose a limitation
    the milestone lifted, and the four tests that would have caught it are gone. So the name is
    pinned as ABSENT, which is the cheapest thing that notices.
    """
    assert not hasattr(run_filing, "_require_measurable_subset")


def test_the_backend_the_runner_names_is_the_structured_one_the_librarian_dispatches():
    """The two modules spell the backend id independently — the runner names it at its own module
    scope so `--help` costs nothing but the standard library. Two spellings of one id is a defect
    that would surface as a run nobody can ask for, so they are compared here."""
    from stigmergy.librarian import agent as agent_module

    assert run_filing.REAL_BACKEND == agent_module.PYDANTIC_BACKEND
    assert run_filing.REAL_BACKEND in agent_module.BACKENDS


# ── the parser, as a value ─────────────────────────────────────────────────────────────────────
# `build_parser()` exists so a flag an operator is promised — by a refusal, by a doc, by the
# runner's own module docstring — can be checked without driving a measurement. Everything that is
# purely about ARGUMENTS is asserted through it directly; the `parse` fixture below stays for the
# cases that are about what `main` does with those arguments afterwards.
def test_the_parser_is_a_value_and_accepts_every_flag_the_docs_promise():
    args = run_filing.build_parser().parse_args(
        ["--backend", run_filing.REAL_BACKEND, "--kinds", schema.MEETING,
         "--model", "openai:gpt-5.6-terra", "--report", "/tmp/x.json"])

    assert args.backend == run_filing.REAL_BACKEND
    assert args.kinds == schema.MEETING
    assert args.model == "openai:gpt-5.6-terra"
    assert args.report == "/tmp/x.json"


def test_the_parsers_defaults_are_the_whole_shipped_set_on_the_real_backend():
    """The invocation with no flags at all, which is what `make filing-golden` runs: the real
    backend, the frozen fixture, no subset.

    The DEFAULT moved when the `sdk` backend retired, and the assertion is written against the
    runner's own constant rather than a literal so the two cannot drift again: what this pins is
    that a bare `make filing-golden` measures the REAL backend and never the double, which is the
    property, not which real backend it happens to be."""
    args = run_filing.build_parser().parse_args([])

    assert args.backend == run_filing.REAL_BACKEND
    assert args.backend != "double", (
        "a bare `make filing-golden` would score the offline double and append it to the series")
    assert args.kinds == ""
    assert args.model is None
    assert args.manifest.endswith("manifest.json")


@pytest.mark.parametrize("name", ["double", run_filing.REAL_BACKEND])
def test_every_backend_dispatch_knows_is_a_choice_the_parser_accepts(name):
    """The `--backend` choices and `agent.BACKENDS` must not drift: a backend argparse accepts and
    dispatch does not is a run that dies after the fixture repo is built, and one dispatch knows and
    argparse rejects is a measurement nobody can ask for."""
    from stigmergy.librarian import agent as agent_module

    assert name in agent_module.BACKENDS
    assert run_filing.build_parser().parse_args(["--backend", name]).backend == name


def test_the_parser_rejects_a_backend_nothing_dispatches(capsys):
    """Exit 2 is argparse's own usage code, which is what a shell script checking `$?` will see."""
    with pytest.raises(SystemExit) as exc_info:
        run_filing.build_parser().parse_args(["--backend", "pydanitc"])
    assert exc_info.value.code == 2


@pytest.fixture()
def parse(monkeypatch):
    """Drive `main` through the real parser and stop at the seam where a measurement starts costing
    money.

    This is for what happens AFTER parsing — the `--kinds` resolution against the manifest on disk,
    the whole-set decision and the denominator check. `_run` itself needs Postgres, git, gitleaks
    and (for a real backend) a key, which is exactly why it is the boundary rather than a
    convenience. Argument shapes alone are asserted through `build_parser()` above, with no stub at
    all.

    **The stub's signature is `_run`'s own**, deliberately: `meeting_only` went with the refusal
    (ADR 033), and a stub that kept accepting `**kwargs` would swallow a keyword `main` stopped
    passing — or one it started passing — without a single test noticing.
    """
    def _parse(*argv):
        captured = {}

        def _record(args, manifest, expectations, *, kinds=None):
            captured.update(backend=args.backend, model=args.model, report=args.report,
                            kinds=kinds, ids=[c["id"] for c in manifest["captures"]])
            return 0

        monkeypatch.setattr(run_filing, "_run", _record)
        monkeypatch.setattr("sys.argv", ["run_filing.py", *argv])
        assert run_filing.main() == 0
        assert captured, "the command line parsed but never reached the measurement"
        return captured

    return _parse


@pytest.mark.parametrize("backend", ["double", run_filing.REAL_BACKEND])
def test_every_backend_parses_and_measures_the_whole_set_by_default(parse, backend):
    """The default invocation, and the milestone in one assertion: no `--kinds`, the whole set,
    on EVERY backend.

    The structured one is the row that changed. `--backend pydantic` with no subset was the exact
    invocation M1 exited on before the queue was touched; it now reaches the measurement carrying
    every capture in the set, which is what "the backend serves both flows" means at the rig's seam.

    The `sdk` arm went with that backend. Nothing about this test needed rewriting for it — the
    parametrize list is one entry shorter and every assertion is untouched, which is what a rig
    written against the port rather than against a class buys.
    """
    captured = parse("--backend", backend)

    assert captured["backend"] == backend
    assert len(captured["ids"]) == SHIPPED_CAPTURES
    assert captured["kinds"] == ["meeting", "raw"], (
        "a whole-set run must still record which kinds it measured, or its row is the ambiguous "
        "one every later row is compared against")


def test_the_structured_backend_may_still_be_given_a_subset_as_an_ablation(parse):
    """`--kinds` survived the restriction that made it necessary, and it has to: a subset somebody
    CHOSE is a different thing from a limitation they worked around, and the meeting-only row
    already recorded in `history.ndjson` must stay reproducible."""
    captured = parse("--backend", run_filing.REAL_BACKEND, "--kinds", schema.MEETING,
                     "--model", "openai:gpt-5.6-terra")

    assert captured["kinds"] == [schema.MEETING]
    assert captured["ids"] == list(MEETING_IDS)
    assert captured["model"] == "openai:gpt-5.6-terra"


def test_spelling_out_every_kind_the_set_carries_is_the_WHOLE_set_and_still_owes_the_pin(parse):
    """**`--kinds meeting,raw` is not a subset, and the runner must not treat it as one.**

    Reading the flag's PRESENCE instead of what it selected would let an operator skip the
    denominator drift check — the one check that makes two full-set scores comparable per run — by
    spelling out the set they were already going to measure. The selection is compared against the
    manifest's own kinds, so this invocation takes the pinned-denominator road.
    """
    captured = parse("--backend", "double", "--kinds", "meeting,raw")

    assert len(captured["ids"]) == SHIPPED_CAPTURES, "spelling out every kind narrowed the set"
    assert captured["kinds"] == ["meeting", "raw"]


def test_a_genuine_subset_that_would_fail_the_pin_still_runs(parse):
    """The other side of the same decision, and the reason it cannot simply always check the pin: a
    real subset produces different denominators by construction, and holding it against the shipped
    set's would make the meeting-only measurement impossible."""
    captured = parse("--backend", "double", "--kinds", schema.MEETING)
    assert captured["ids"] == list(MEETING_IDS)


def test_a_spelled_out_whole_set_that_has_drifted_is_still_refused(monkeypatch, golden, tmp_path):
    """The sabotage twin for the rule above: if `--kinds meeting,raw` silently took the subset road,
    a drifted golden set would score without complaint. Driven through `main` over a manifest with
    one capture removed — the drift refusal has to fire even though a `--kinds` flag was passed."""
    manifest, expectations = golden
    thinned = {**manifest, "captures": [c for c in manifest["captures"]
                                        if c["id"] != "F01-plain-note-known-entity"]}
    dropped = {**expectations, "expectations": [e for e in expectations["expectations"]
                                                if e["id"] != "F01-plain-note-known-entity"]}
    (tmp_path / "manifest.json").write_text(json.dumps(thinned), encoding="utf-8")
    (tmp_path / "expectations.json").write_text(json.dumps(dropped), encoding="utf-8")

    monkeypatch.setattr(run_filing, "_run", lambda *a, **kw: 0)
    monkeypatch.setattr("sys.argv", [
        "run_filing.py", "--backend", "double", "--kinds", "meeting,raw",
        "--manifest", str(tmp_path / "manifest.json"),
        "--expectations", str(tmp_path / "expectations.json")])

    with pytest.raises(SystemExit, match="EXPECTED_DENOMINATORS"):
        run_filing.main()


# ── the kinds ride into the report, the caption and the row ────────────────────────────────────
def _phase(capture_id: str, status: str = schema.FILED) -> dict:
    return run_filing._phase(capture_id, "only", {"status": status},
                             {"status": status, "attempts": 1, "cost_usd": 0.5})


def test_the_report_records_which_kinds_the_run_measured():
    """A per-facet score is only comparable against another score over the SAME set. `kinds` is
    what makes that recoverable from the artifact rather than from whoever remembers the command."""
    report = run_filing.aggregate([_phase("F08-meeting-two-decisions")], backend="pydantic",
                                  model="openai:gpt-5.6-terra", wall_s=3.0,
                                  kinds=[schema.MEETING])
    assert report["kinds"] == [schema.MEETING]


def test_the_kinds_key_is_present_and_sorted_even_when_a_caller_names_none():
    """Always present, never conditional: a row missing the key is ambiguously old, and a row
    carrying `[]` is visibly a run that named no subset. Sorted for the same reason every other
    recorded list here is — two reports of the same run must diff to nothing."""
    assert run_filing.aggregate([], backend="sdk", model="m", wall_s=0.0)["kinds"] == []
    assert run_filing.aggregate([], backend="sdk", model="m", wall_s=0.0,
                                kinds=["raw", "meeting"])["kinds"] == ["meeting", "raw"]


def test_the_caption_names_the_kinds_a_subset_measured():
    """The table is the thing somebody screenshots. A three-phase score quoted as the shipped set's
    is the one failure a series read years later cannot recover from, so the caption carries the
    subset beside the backend and the model."""
    caption = run_filing.render(
        run_filing.aggregate([_phase("F08-meeting-two-decisions")], backend="pydantic",
                             model="openai:gpt-5.6-terra", wall_s=3.0,
                             kinds=[schema.MEETING])).splitlines()[0]

    assert schema.MEETING in caption
    assert "pydantic" in caption and "openai:gpt-5.6-terra" in caption


def test_a_whole_set_caption_is_unchanged_and_claims_no_subset():
    """The benign twin: every caption ever rendered before `--kinds` existed must still read the
    same way, or the flag rewrote the history it was added to protect."""
    caption = run_filing.render(
        run_filing.aggregate([_phase("F01-plain-note-known-entity")], backend="sdk",
                             model="claude-sonnet-5", wall_s=3.0)).splitlines()[0]

    assert caption == "# golden filing — backend `sdk` · model `claude-sonnet-5`"


def test_the_history_row_carries_the_kinds_it_measured():
    """The row is the one artifact of a paid run that outlives the terminal, and this is the field
    that keeps a subset score from being read as a full-set one. Written as JSON here because that
    is how it is written for real — a Counter or a set in this dict would raise at the append site,
    on the run that could least afford it."""
    report = run_filing.aggregate([_phase("F08-meeting-two-decisions")], backend="pydantic",
                                  model="openai:gpt-5.6-terra", wall_s=3.0,
                                  kinds=[schema.MEETING])

    metrics = run_filing._history_metrics(report, report["phases"], {"corpus": "x"})

    assert metrics["kinds"] == [schema.MEETING]
    assert json.loads(json.dumps(metrics, sort_keys=True)) == metrics


def test_a_row_written_by_a_caller_that_names_no_kinds_still_carries_the_key():
    """The pre-`--kinds` shape, which every existing caller still has: the key is always there, so
    an older row is visibly older rather than ambiguously a subset."""
    report = run_filing.aggregate([_phase("F01-plain-note-known-entity")], backend="sdk",
                                  model="claude-sonnet-5", wall_s=1.0)
    assert run_filing._history_metrics(report, report["phases"], {"corpus": "x"})["kinds"] == []


def test_the_series_reader_can_still_read_a_row_that_predates_the_field(tmp_path):
    """`history.ndjson` is append-only and years long, so the field's absence in old rows must be
    survivable — a reader that required it would make every recorded filing score unreadable."""
    path = tmp_path / "history.ndjson"
    eval_history.append_run(suite="filing", git_sha="old", path=path,
                            metrics={"backend": "sdk", "model": "claude-sonnet-5"})
    eval_history.append_run(suite="filing", git_sha="new", path=path,
                            metrics={"backend": "pydantic", "model": "openai:gpt-5.6-terra",
                                     "kinds": [schema.MEETING]})

    rows = eval_history.read_history(path)           # the real reader, over both shapes
    assert len(rows) == 2
    assert "kinds" not in rows[0]
    assert rows[1]["kinds"] == [schema.MEETING]
