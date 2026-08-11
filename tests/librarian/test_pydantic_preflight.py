"""What a WORKER on the `pydantic` backend is still refused for — and what it is no longer refused
for at all.

**The refusal this file was written around is gone (ADR 033 D5).** M1's `worker.startup_checks`
refused `backend="pydantic"` outright, because config that half-works is the failure this repo
refuses on principle: a worker's queue carries ordinary captures too, and a backend serving the
meeting flow only would have drained `raw` and `drive` rows into `failed` one delivery at a time
while looking configured. That backend serves both flows now, so there is nothing left to refuse —
and the `meeting_only` escape that softened the refusal for the eval rig has nothing left to
soften. The parameter is gone from `_check_pydantic_backend` and from `startup_checks`, and every
call below spells the checks the way production spells them.

What SURVIVES is everything that was always about the BACKEND rather than about a flow it could not
run, and each of the three is a pre-flight an operator meets before a single item is claimed:

* a provider-prefixed model id (pydantic-ai reads a bare name as the OpenAI Responses API);
* a configured price (an unpriced id reports `$0.00` for work that costs money);
* the provider's own key.

They are exercised through `_check_pydantic_backend` with an INJECTED environment mapping — the
check is a pure function of a mapping and a model string, exactly like `agent.credential_status`,
and asserting it against a process whose environment has been mutated around it would prove less
and flake more.

**And the LIFTED state is asserted as its own case**, because a removal nothing pins is a removal
that can be quietly re-imposed: a fully-configured `pydantic` worker now boots through the whole
pre-flight, which is the invocation M1 exited on.
"""
import dataclasses
import json
import logging
import pathlib
import re

import pytest

from stigmergy.librarian import agent as agent_module
from stigmergy.librarian import config, pricing, pydantic_backend, worker
from stigmergy.librarian.errors import LibrarianConfigError
from stigmergy.librarian.pydantic_backend import PydanticMeetingAgent
from tests.librarian import support

ROOT = pathlib.Path(__file__).resolve().parents[2]

PRICED_MODEL = "openai:gpt-5.6-terra"
FAKE_KEY = "sk-fixture-not-a-real-key"


def _pydantic(deps, **overrides):
    """`deps.settings` switched to the backend under test, with a provider-prefixed model unless a
    case is about the model itself."""
    return dataclasses.replace(deps.settings, backend=agent_module.PYDANTIC_BACKEND,
                               **{"model": PRICED_MODEL, **overrides})


# ── the refusal that is GONE, and the four tests that went with it ─────────────────────────────
# **DELETED here, with the refusal they spent (ADR 033 D5):**
#
#   `test_a_worker_is_refused_the_meeting_only_backend_before_it_claims_anything` — the refusal
#       itself: the backend id, the word "meeting", the two `STIGMERGY_LIBRARIAN_BACKEND=` values
#       it recommended and the "tests only" warning attached to the double.
#   `test_the_refusal_points_at_a_decision_record_that_exists` — the ADR the refusal cited.
#   `test_the_worker_refusal_offers_a_model_this_environment_can_actually_price` — the derived
#       `--model` example, which `worker._usable_example` composed and which no longer exists.
#   `test_the_meeting_only_rig_passes_the_preflight_the_worker_is_refused_by` — the `meeting_only`
#       escape hatch, which has nothing left to soften.
#
# Their subject was a limitation, not a rule, and the limitation is what lifted. The RULES they
# carried survive where they still bite: "a refusal that cites a document is only as good as the
# document" and "a message containing a command is an executable promise" are enforced in
# `test_operator_surface.py`, which is this package's declared home for both and which keeps the
# credential refusal's `make librarian-walk` and the push-identity refusal's runbook honest.
#
# **The pruning finding this note used to carry is CLOSED, and half of it was wrong when written —
# which is why the correction stays rather than the paragraph simply being deleted.** It read:
# `pydantic_backend.ADR` and `pydantic_backend.ORDINARY_ADR` now have NO reader in production.
#
#   * `ADR` — correct, and actioned: it was cited by exactly one message (the refusal above), and
#     it is gone, with its own tombstone at the top of `pydantic_backend.py`.
#   * `ORDINARY_ADR` — **not correct AT THE TIME.** `worker._check_brief_matches_backend` cited it
#     by name, and acting on the finding as written would have deleted a constant a live refusal
#     read. It became true one milestone later, when ADR 034 retired that check — and the constant
#     went WITH its reader, in the same change, which is the difference between pruning and
#     guessing.
#
# The lesson is the one this file's own doctrine already states about refusals: check the call
# sites before calling something dead. A pruning note is a fix instruction, and a wrong one costs
# more than none.


# ── the lifted state, which is what M2 actually changed ────────────────────────────────────────
def test_a_fully_configured_pydantic_worker_now_boots_through_the_whole_preflight(rig, monkeypatch):
    """**The milestone, at the seam that used to stop it.** `startup_checks` — not
    `_check_pydantic_backend` — because the refusal that died lived in the caller: it fired before
    the lease check, before the scanner and before any git read, so an operator never reached the
    checks below it. A worker whose model is provider-prefixed and priced and whose provider key is
    exported now walks the whole pre-flight and claims items.

    Everything else about this rig is production's: the fixture knowledge repo carries the librarian
    skill at its base commit (`agent.SKILL_READING_BACKENDS` covers this backend since ADR 033), and
    the push identity check returns early because the origin is a local bare path.
    """
    _, deps = rig
    monkeypatch.setenv(pydantic_backend.PROVIDER_KEY_ENV["openai"], FAKE_KEY)

    worker.startup_checks(_pydantic(deps))                   # must not raise

    # ...and the sentence that used to stop it here is not in the codebase any more
    assert "serves the MEETING flow only" not in (ROOT / "src" / "stigmergy" / "librarian"
                                                  / "worker.py").read_text(encoding="utf-8")


def test_that_worker_is_still_refused_when_its_provider_key_is_missing(rig, monkeypatch):
    """The lifted state's own specificity half. "The backend is allowed now" must not read as "the
    backend is unchecked now": the three checks that were always about the backend still fire from
    the same place, and the key is the one an operator most often has not exported."""
    _, deps = rig
    monkeypatch.delenv(pydantic_backend.PROVIDER_KEY_ENV["openai"], raising=False)

    with pytest.raises(LibrarianConfigError) as exc_info:
        worker.startup_checks(_pydantic(deps))

    assert pydantic_backend.PROVIDER_KEY_ENV["openai"] in str(exc_info.value)


# ── M3: the iteration ceiling is refused BY NAME below the usable minimum, not silently clamped ──
@pytest.mark.parametrize("bad", [1, 0, -5], ids=["one", "zero", "negative"])
def test_a_max_turns_below_two_is_refused_by_name_before_the_first_claim(rig, monkeypatch, bad):
    """**M3, at the startup seam.** The ordinary run maps `max_turns` to
    `UsageLimits(request_limit=…)`, and a tool-using pass needs at least TWO requests — one to call a
    tool, one to write its account — so a ceiling below 2 fails every ordinary capture at full cost
    the moment the model reaches for a tool. The backend no longer clamps it silently (that would
    rewrite an operator's number, the failure `resolved_timeout_s` refuses on principle); instead
    `startup_checks` refuses it here, before a single item is claimed, and the message names the
    variable and the default so the fix is one edit.

    The provider key is present so the ONLY thing wrong is `max_turns` — otherwise this would refuse
    on the key check that runs first (`_check_pydantic_backend`), and prove nothing about the ceiling.
    """
    _, deps = rig
    monkeypatch.setenv(pydantic_backend.PROVIDER_KEY_ENV["openai"], FAKE_KEY)

    with pytest.raises(LibrarianConfigError) as exc_info:
        worker.startup_checks(_pydantic(deps, max_turns=bad))

    message = str(exc_info.value)
    assert str(bad) in message and "max_turns" in message
    assert "$STIGMERGY_LIBRARIAN_MAX_TURNS" in message
    assert str(config.DEFAULT_MAX_TURNS) in message, "the refusal does not name the usable default"


@pytest.mark.parametrize("ok", [2, "default"], ids=["floor", "default"])
def test_a_usable_max_turns_boots_which_is_the_refusals_benign_twin(rig, monkeypatch, ok):
    """**The M3 twin, at the boundary rather than far from it.** A refusal that fires at `< 2` must
    let `2` — the exact floor — and the shipped default through, or it is a guard nobody can satisfy.
    Both boot the whole pre-flight, which is the invocation a real worker makes.

    `2` is the floor the message tells the operator to raise to, and the default is what a worker
    that set nothing runs on; a guard that refused either would be refusing a working deployment.
    """
    _, deps = rig
    monkeypatch.setenv(pydantic_backend.PROVIDER_KEY_ENV["openai"], FAKE_KEY)
    overrides = {} if ok == "default" else {"max_turns": ok}

    worker.startup_checks(_pydantic(deps, **overrides))          # must not raise

    assert config.DEFAULT_MAX_TURNS >= 2, (
        "the shipped default is below the floor the refusal enforces — the twin would be a lie")


def test_the_double_is_never_refused_over_a_max_turns_it_does_not_read(rig):
    """M3's specificity half, and the one that could refuse a working deployment: the meeting flow
    and the offline double do not read `max_turns` at all (the double runs no model loop), so a
    `max_turns` below 2 must not refuse a `double` worker. The ceiling is the ordinary pydantic run's
    alone."""
    _, deps = rig
    worker.startup_checks(dataclasses.replace(deps.settings, backend="double", max_turns=1))


def test_the_skill_is_required_of_the_backend_that_injects_it(rig,
                                                              monkeypatch):
    """ADR 033's one ADDITION to this pre-flight: a backend that injects the brief is proven to
    have one at the base commit — one loud line before the first claim rather than a `failed`
    row per capture. It was added when a SECOND backend started injecting the same brief the
    first one did; it is now the check for the only one that does.

    Driven by deleting the skill from the base COMMIT (`support.commit_and_push`), never from the
    working tree: `startup_checks` reads at the base ref, and a test that unlinked the file on disk
    would assert nothing about what a run sees.
    """
    env, deps = rig
    monkeypatch.setenv(pydantic_backend.PROVIDER_KEY_ENV["openai"], FAKE_KEY)
    pathlib.Path(env.repo, *agent_module.SKILL_RELPATH.split("/")).unlink()
    support.commit_and_push(env.repo, "test: a base commit with no librarian skill")

    with pytest.raises(LibrarianConfigError) as exc_info:
        worker.startup_checks(_pydantic(deps))

    assert "librarian skill" in str(exc_info.value)


def test_the_double_is_not_asked_for_a_skill_it_never_reads(rig):
    """The addition's specificity half, and it is the one that could refuse a working deployment:
    `SKILL_READING_BACKENDS` names every backend that INJECTS the brief, and the offline double
    reads none at all. Requiring one of a `double` worker would be a check that can only ever
    fail on something nothing was going to use."""
    env, deps = rig
    pathlib.Path(env.repo, *agent_module.SKILL_RELPATH.split("/")).unlink()
    support.commit_and_push(env.repo, "test: a base commit with no librarian skill")

    worker.startup_checks(deps.settings)                     # must not raise
    assert "double" not in agent_module.SKILL_READING_BACKENDS


# ── RETIRED with `worker._check_brief_matches_backend` (ADR 034) ───────────────────────────────
# Three tests went with that check, plus the `_with_old_brief` helper that staged its input:
#
#   `test_a_structured_worker_is_refused_a_brief_written_for_a_run_that_holds_tools` — the refusal
#       itself: which file the brief still named, why a tool-less backend could not follow it, the
#       deploy ORDER that fixed it, and the absence of a `sdk` escape hatch.
#   `test_the_document_that_refusal_cites_exists_and_decides_the_thing_it_claims` — the ADR that
#       refusal cited (`pydantic_backend.ORDINARY_ADR`, retired with its only reader).
#   `test_the_check_reads_what_the_backend_DECLARES_and_not_which_backend_was_named` — that the
#       check branched on the declaration rather than on the backend's name.
#
# **They go because their SUBJECT is gone, not because they became inconvenient.** The check was
# keyed on `structured_ordinary`, and the shipped ordinary backend now declares `False` — so all
# three would have passed forever while testing a branch no worker takes, which is the
# permanently-green shape this repo calls worse than no test. The rule each one carried survives
# where it is still enforced: "a refusal that cites a document is only as good as the document" and
# "a message containing a command is an executable promise" are `test_operator_surface.py`'s, and
# "read what the backend DECLARES, never which one was named" is pinned on the branch that still
# reads a declaration (`test_structured_processing_pg.py`'s own
# `test_the_declaration_is_what_selects_the_shape_and_not_the_backends_class`).
#
# What no longer has a home is a pre-flight against a FUTURE structured backend meeting a
# tool-describing brief. That is a test to write WITH that backend, against the brief as it is
# then — see the tombstone in `worker.py`.


def test_the_shipped_brief_boots_the_real_backend_which_is_the_landed_state(rig, monkeypatch):
    """**The benign twin that outlived the refusal, and it is the one worth keeping.** Driven
    against the brief this suite actually ships (`tests/librarian/fixtures/repo/`, the resynced
    drift-guard copy), so it is the real text rather than one written to pass: a fully configured
    worker on the real backend walks the whole pre-flight, brief included, and claims items."""
    _, deps = rig
    monkeypatch.setenv(pydantic_backend.PROVIDER_KEY_ENV["openai"], FAKE_KEY)

    worker.startup_checks(_pydantic(deps))                       # must not raise


# ── the three checks that were always about the BACKEND ────────────────────────────────────────
def test_the_backend_checks_still_run_from_the_check_function_itself(rig):
    """The entry point the cases below use, exercised once against a fully-configured value so a
    signature change here is one failure rather than a dozen."""
    _, deps = rig
    environ = {pydantic_backend.PROVIDER_KEY_ENV["openai"]: FAKE_KEY}

    worker._check_pydantic_backend(_pydantic(deps), environ=environ)


def test_a_bare_model_name_is_refused_because_pydantic_ai_would_pick_a_provider_nobody_chose(rig):
    """The provider-prefix rule, and the reason it is a refusal rather than a default: pydantic-ai
    reads a bare name as the OpenAI Responses API, so the retired backend's own `claude-sonnet-5`
    would file this brain's meetings through a provider the operator never named — silently, and
    correctly enough that nothing downstream would notice."""
    _, deps = rig
    with pytest.raises(LibrarianConfigError) as exc_info:
        worker._check_pydantic_backend(_pydantic(deps, model="claude-sonnet-5"), environ={})

    message = str(exc_info.value)
    assert "claude-sonnet-5" in message
    assert "provider" in message
    # and it lists what this environment can price, so the fix is a paste rather than a search
    for known in pricing.PRICES:
        assert known in message


def test_an_unpriced_model_is_refused_before_the_run_rather_than_reported_as_zero(rig):
    """The pricing pre-flight, at the point that makes it worth having: an unpriced id would report
    `$0.00` for work that costs money, and `$0.00` reads as free. The refusal is `pricing`'s own, so
    it carries the id, the variable and the table's `AS_OF` date."""
    _, deps = rig
    with pytest.raises(LibrarianConfigError) as exc_info:
        worker._check_pydantic_backend(_pydantic(deps, model="openai:gpt-9"), environ={})
    message = str(exc_info.value)
    assert "openai:gpt-9" in message
    assert pricing.PRICING_ENV in message and pricing.AS_OF in message


@pytest.mark.parametrize("provider, key_env", sorted(pydantic_backend.PROVIDER_KEY_ENV.items()))
def test_each_known_provider_family_is_refused_without_its_own_key(rig, provider, key_env):
    """One case per family, parametrized off the production table so a fourth provider is covered
    the day it is added. The message has to name the VARIABLE — "authentication failed" three
    hundred seconds into a run is the outcome this pre-flight exists to replace."""
    _, deps = rig
    model = next(m for m in pricing.PRICES if m.startswith(f"{provider}:"))

    with pytest.raises(LibrarianConfigError) as exc_info:
        worker._check_pydantic_backend(_pydantic(deps, model=model), environ={})

    message = str(exc_info.value)
    assert key_env in message
    assert model in message


@pytest.mark.parametrize("provider, key_env", sorted(pydantic_backend.PROVIDER_KEY_ENV.items()))
def test_each_known_provider_family_passes_once_its_key_is_present(rig, provider, key_env):
    """The benign twin, one per family: a pre-flight that can only refuse is a pre-flight nobody
    can satisfy, and this one stands between the operator and every measurement of the backend."""
    _, deps = rig
    model = next(m for m in pricing.PRICES if m.startswith(f"{provider}:"))

    worker._check_pydantic_backend(_pydantic(deps, model=model),
                                   environ={key_env: FAKE_KEY})


def test_an_empty_key_is_not_a_key(rig):
    """An exported-but-empty variable is the shape a half-written env file produces, and it must not
    read as configured."""
    _, deps = rig
    with pytest.raises(LibrarianConfigError, match=re.escape("OPENAI_API_KEY")):
        worker._check_pydantic_backend(
            _pydantic(deps),
            environ={pydantic_backend.PROVIDER_KEY_ENV["openai"]: ""})


def test_an_unlisted_provider_prefix_warns_and_proceeds_rather_than_refusing(rig, caplog,
                                                                             monkeypatch):
    """pydantic-ai supports providers this table has not heard of, and a pre-flight that refused
    every unlisted one would make the adapter provider-specific — a guard whose specificity failure
    blocks a legitimate configuration.

    Reachable ONLY through a priced id, which is the honest way in: the price check runs first, so
    an unlisted provider must be given a price before the key preflight is even consulted. That is
    the same door `$STIGMERGY_LIBRARIAN_PRICING` exists to open.

    WARNING and not INFO is load-bearing: nothing in this package configures logging, so
    `logging.lastResort` prints WARNING and above and drops INFO entirely — an advisory nobody sees
    is not an advisory. Same argument as `_check_agent_credential`'s own ambient line.
    """
    _, deps = rig
    exotic = "mistral:mistral-large"
    monkeypatch.setenv(pricing.PRICING_ENV, json.dumps({exotic: [1.0, 1.0, 3.0]}))

    with caplog.at_level(logging.WARNING, logger="stigmergy.librarian.worker"):
        worker._check_pydantic_backend(_pydantic(deps, model=exotic), environ={})

    assert len(caplog.records) == 1, "the advisory is one line, not a paragraph of them"
    assert caplog.records[0].levelno == logging.WARNING
    assert "mistral:" in caplog.records[0].getMessage()


def test_a_listed_provider_with_its_key_says_nothing_at_all(rig, caplog):
    """The advisory's own specificity half: a fully configured run must not be told it is relying
    on anything, or the line stops meaning what it says."""
    _, deps = rig
    with caplog.at_level(logging.WARNING, logger="stigmergy.librarian.worker"):
        worker._check_pydantic_backend(
            _pydantic(deps),
            environ={pydantic_backend.PROVIDER_KEY_ENV["openai"]: FAKE_KEY})
    assert caplog.records == []


def test_the_key_preflight_reads_the_process_environment_when_nothing_is_injected(rig,
                                                                                  monkeypatch):
    """The injectable mapping is for the tests; production passes nothing and reads `os.environ`.
    Proven once, here, so the default is not a branch only tests take."""
    _, deps = rig
    monkeypatch.setenv(pydantic_backend.PROVIDER_KEY_ENV["openai"], FAKE_KEY)
    worker._check_pydantic_backend(_pydantic(deps))   # must not raise

    monkeypatch.delenv(pydantic_backend.PROVIDER_KEY_ENV["openai"], raising=False)
    with pytest.raises(LibrarianConfigError):
        worker._check_pydantic_backend(_pydantic(deps))


# ── RETIRED with the `sdk` backend: the model-spelling MIRROR ─────────────────────────────────
# Three tests went with `worker._check_model_spelling_for`, and the comment block that framed them:
#
#   `test_an_sdk_worker_is_refused_a_provider_prefixed_model`
#   `test_an_sdk_worker_with_the_bare_spelling_passes_that_check`   (its benign twin)
#   `test_the_SAME_old_brief_boots_an_sdk_worker_which_is_the_run_it_was_written_for`  (above)
#
# The first pair existed because the asymmetry was the defect: one backend refused a bare id while
# the other silently accepted a prefixed one, so exactly HALF of one configuration mistake was
# caught, and an operator who set `openai:gpt-5.6-terra` on the wrong backend learned it from a
# failed run. With one backend there is one spelling and no mirror to hold up.
#
# **The surviving half is not weaker, it is the whole of it**: a bare id is refused by
# `_check_pydantic_backend`, tested by
# `test_a_bare_model_name_is_refused_because_pydantic_ai_would_pick_a_provider_nobody_chose` above,
# and its message now names the retirement explicitly — a deployment that changed the backend and
# not the model lands exactly there. The RULE outlives the mirror and is recorded in `worker.py`
# where the helper used to be: a model spelling belongs to a backend, and a backend must refuse the
# spelling that is not its own.
#
# **A FOURTH test went and should not have: `test_the_prefixed_spelling_is_accepted_by_the_backend_
# it_belongs_to`.** It closed the fourth cell of the spelling table (prefixed + `pydantic` boots).
# It is not restored because it is now a strict subset of
# `test_each_known_provider_family_passes_once_its_key_is_present` above, which drives the same
# call for EVERY provider family off the production table rather than for one hand-picked id — a
# second, narrower copy of a covered property is the kind of duplicate that goes stale first. The
# two below WERE restored: they are live properties about surfaces that never had an `sdk` half.


def test_the_double_is_never_refused_over_a_model_it_does_not_read(rig):
    """The double reads no model at all, so a refusal about that field would be a guard inventing
    work for an operator. Both spellings, and a nonsense one: all three boot."""
    _, deps = rig
    for spelling in (PRICED_MODEL, "claude-sonnet-5", "not-a-model-at-all"):
        worker.startup_checks(dataclasses.replace(deps.settings, backend="double",
                                                  model=spelling))


# ── the preflight table covers what the price table prices ─────────────────────────────────────
def test_every_provider_the_price_table_names_has_a_key_preflight():
    """A derived assertion, because the two tables are edited at different times for different
    reasons. A model priced under a prefix `PROVIDER_KEY_ENV` has never heard of gets no key
    pre-flight at all — it drops to the advisory and the run fails unauthenticated three hundred
    seconds later, which is precisely the outcome the pre-flight exists to replace."""
    priced_providers = {pydantic_backend.provider_of(model) for model in pricing.PRICES}
    priced_providers.discard("")

    missing = sorted(priced_providers - set(pydantic_backend.PROVIDER_KEY_ENV))

    assert not missing, (
        f"librarian/pricing.PRICES prices {missing} and pydantic_backend.PROVIDER_KEY_ENV has no "
        f"key for them — a model this repo ships a price for must not fall through to the "
        f"unknown-provider advisory")


# ── the two tables that must AGREE about the deployed worker's stripped key ────────────────────
def test_the_only_provider_key_the_deployed_worker_strips_is_the_read_paths_own():
    """**The coupling nothing else states, between two tables edited for unrelated reasons.**

    `bootstrap.READ_PATH_ONLY_ENV` is what `stigmergy-librarian-boot` deletes from the DEPLOYED
    worker's environment before exec'ing the loop, so the write path cannot reach the read path's
    embedder key. `pydantic_backend.PROVIDER_KEY_ENV` is what the filing backend AUTHENTICATES
    with. Where they intersect, an operator can configure a model whose key the container strips
    on purpose — the pre-flight then refuses on the deployed worker and passes on a laptop, and no
    export can fix it there.

    That intersection is exactly one entry today (OpenAI's), it is deliberate, and the refusal says
    so. This pins the SIZE of it: a second provider joining `READ_PATH_ONLY_ENV`, or a new
    read-path key that happens to be a provider key, would silently make another model family
    unusable in the container with nothing to say why.
    """
    from stigmergy.librarian import bootstrap

    stripped = set(bootstrap.READ_PATH_ONLY_ENV) & set(pydantic_backend.PROVIDER_KEY_ENV.values())

    assert stripped == {pydantic_backend.PROVIDER_KEY_ENV["openai"]}, (
        f"bootstrap.READ_PATH_ONLY_ENV and pydantic_backend.PROVIDER_KEY_ENV now intersect at "
        f"{sorted(stripped)}. Every variable in that intersection names a provider the DEPLOYED "
        f"worker cannot authenticate as, whatever the operator exports. If this is intended, "
        f"update this test AND `worker._check_pydantic_backend`'s missing-key refusal, which names "
        f"the dead end for exactly these variables")


def test_every_provider_the_preflight_knows_is_one_a_priced_model_could_use():
    """The pruning half. A key pre-flight for a provider nothing can be configured to use is a
    branch nothing exercises and a name nobody maintains — and the advisory path already handles a
    provider this table has not heard of, so an entry earns its place by being reachable."""
    priced_providers = {pydantic_backend.provider_of(model) for model in pricing.PRICES}
    assert set(pydantic_backend.PROVIDER_KEY_ENV) <= priced_providers, (
        "PROVIDER_KEY_ENV names a provider no priced model uses — either price one or drop the "
        "entry; the unlisted-provider advisory already covers the general case")


def test_provider_of_reads_the_prefix_and_answers_empty_for_a_bare_name():
    """The one-line helper the refusal above branches on, pinned on its own so the branch's meaning
    does not depend on reading the caller."""
    assert pydantic_backend.provider_of("openai:gpt-5.6-terra") == "openai"
    assert pydantic_backend.provider_of("google-gla:gemini-3.6-flash") == "google-gla"
    assert pydantic_backend.provider_of("  anthropic:claude-sonnet-5  ") == "anthropic"
    assert pydantic_backend.provider_of("claude-sonnet-5") == ""
    assert pydantic_backend.provider_of("") == ""
    assert pydantic_backend.provider_of(None) == ""


# ── the ordinary flow on this backend ──────────────────────────────────────────────────────────
# **DELETED here (ADR 033):** `test_the_ordinary_flow_refuses_honestly_and_names_the_document` and
# `test_that_refusal_is_unreachable_through_a_worker_which_is_why_it_may_be_terse`. Both were about
# `PydanticFilingAgent.run` being a REFUSAL — that it raised `AgentError` rather than
# `NotImplementedError` (so `processing` turned it into a `failed` row with a sentence instead of a
# traceback), that it cited the ADR, and that no worker could reach it because `startup_checks`
# refused the backend first. `run` is a real structured model call now; there is no refusal left to
# be honest about and no unreachability left to prove.
#
# Where the replacement coverage lives, so this is a move rather than a loss:
#
#   * the ENVELOPE — a real `run` through pydantic-ai's `TestModel` returns a parsed outcome and a
#     positive `cost_usd`, and its faults still carry `run_cost_usd` —
#     `test_filing_port_conformance.py`;
#   * the FLOW — a structured filing walked end to end over a real queue, real git and real gates,
#     including the confinement cases the refusal used to make moot —
#     `test_structured_processing_pg.py`.
def test_the_backend_object_still_refuses_to_be_built_around_a_model_nobody_prices(rig):
    """The one construction-time refusal that outlived the flow refusal, kept here because this
    file is where an operator's configuration mistakes are pinned: a backend that cannot say what a
    run cost must not be constructible, and `worker.startup_checks` is the loud road to the same
    answer rather than the only one."""
    _, deps = rig

    with pytest.raises(LibrarianConfigError) as exc_info:
        PydanticMeetingAgent(_pydantic(deps, model="openai:gpt-9"))

    message = str(exc_info.value)
    assert "openai:gpt-9" in message
    assert pricing.PRICING_ENV in message and pricing.AS_OF in message
