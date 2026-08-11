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
from stigmergy.librarian import pricing, pydantic_backend, worker
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
# **One pruning finding falls out of this and belongs to the developer, not to a test:**
# `pydantic_backend.ADR` and `pydantic_backend.ORDINARY_ADR` now have NO reader in production. `ADR`
# was read only by the deleted refusal; `ORDINARY_ADR` was added by this milestone and is read by
# nothing at all. Two module constants naming documents that no message cites is the shape this
# repo prunes on sight — reported rather than deleted here, because production code is not the
# tester's to edit.


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


def test_the_skill_is_now_required_of_this_backend_too_and_not_only_of_the_sdk_one(rig,
                                                                                   monkeypatch):
    """ADR 033's one ADDITION to this pre-flight: the structured backend injects the SAME brief the
    exploring one does, so a base commit without it fails both identically — one loud line before
    the first claim rather than a `failed` row per capture.

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
    `SKILL_READING_BACKENDS` is `sdk` and `pydantic`, and the offline double reads no brief at all.
    Requiring one of a `double` worker would be a check that can only ever fail on something
    nothing was going to use — which is the same argument the credential check makes for itself."""
    env, deps = rig
    pathlib.Path(env.repo, *agent_module.SKILL_RELPATH.split("/")).unlink()
    support.commit_and_push(env.repo, "test: a base commit with no librarian skill")

    worker.startup_checks(deps.settings)                     # must not raise
    assert "double" not in agent_module.SKILL_READING_BACKENDS


# ── the LANDING ORDER, which is the real rule this refusal has depth for (ADR 033 D4) ──────────
# The brief lives in the knowledge repo and the platform lives here, so the milestone lands as two
# PRs. If the platform half arrives first, a structured worker injects a brief telling the model —
# in its own voice — to write its account to a file it holds no tool to write. On the MEETING flow
# that contradiction is named out loud and scoped (`pydantic_backend.OVERRIDE_NOTE`); on the
# ordinary flow after ADR 033 there is no override, because the brief is SUPPOSED to be the
# structured text. So an old brief is a silent, uncorrected contradiction — and the pages it
# produced would be scored as filing quality on the exact measurement M3's decision reads.
def _with_old_brief(env, extra: str = "") -> None:
    """Put a PRE-ADR-033 brief on the base commit: the same file, plus the sentence a tool-holding
    run's brief carries. The signal the check reads is the outcome FILE's own name appearing at
    all, and every brief that predates this milestone documents it."""
    path = pathlib.Path(env.repo, *agent_module.SKILL_RELPATH.split("/"))
    path.write_text(
        f"{path.read_text(encoding='utf-8')}\n\n## What you return\n\n"
        f"When you are done, write your account to `{agent_module.OUTCOME_FILENAME}` at the repo "
        f"root.{extra}\n", encoding="utf-8")
    support.commit_and_push(env.repo, "test: the pre-ADR-033 tool-holding brief")


def test_a_structured_worker_is_refused_a_brief_written_for_a_run_that_holds_tools(rig,
                                                                                   monkeypatch):
    """The refusal, and the whole of what it has to say: WHICH file the brief still names, WHY that
    is incompatible with this backend, and the DEPLOY ORDER that fixes it.

    An operator meeting this has one of two jobs — push the rewritten brief, or run the backend the
    old brief was written for — and a refusal that named the contradiction without naming the
    ordering would leave them guessing which repo to touch.
    """
    env, deps = rig
    monkeypatch.setenv(pydantic_backend.PROVIDER_KEY_ENV["openai"], FAKE_KEY)
    _with_old_brief(env)

    with pytest.raises(LibrarianConfigError) as exc_info:
        worker.startup_checks(_pydantic(deps))

    message = str(exc_info.value)
    assert agent_module.OUTCOME_FILENAME in message              # which file
    assert "holds" in message and "none" in message              # ...and why it cannot follow it
    assert "land BEFORE this worker runs" in message             # ...and the ordering
    assert pydantic_backend.ORDINARY_ADR in message              # ...and the decision behind it
    assert "STIGMERGY_LIBRARIAN_BACKEND=sdk" in message          # ...and the other way out


def test_the_document_that_refusal_cites_exists_and_decides_the_thing_it_claims(rig):
    """A refusal that cites a document is only as good as the document — this package's own
    standing rule, and the one the deleted meeting-only refusal used to carry. Opened from the repo
    root, so a renamed or unwritten ADR fails here rather than at the operator's 404."""
    adr = ROOT / pydantic_backend.ORDINARY_ADR
    assert adr.is_file(), f"the refusal cites {pydantic_backend.ORDINARY_ADR}, which does not exist"
    body = adr.read_text(encoding="utf-8")
    assert "D4" in body
    assert agent_module.SKILL_RELPATH in body, (
        "the ADR the refusal cites says nothing about the brief whose landing order it decides")


def test_the_SAME_old_brief_boots_an_sdk_worker_which_is_the_run_it_was_written_for(rig,
                                                                                    monkeypatch):
    """**The first benign twin, and the one that decides whether this check is safe to ship.** The
    old brief is not broken — it is CORRECT for a run that holds five tools and writes its own
    page, which is exactly what the `sdk` backend still is. A check keyed on the brief's text alone
    would ground every existing deployment on the day the platform landed."""
    env, deps = rig
    monkeypatch.setenv(agent_module.CREDENTIAL_ENV[0], FAKE_KEY)
    _with_old_brief(env)

    worker.startup_checks(dataclasses.replace(deps.settings, backend="sdk",
                                              model="claude-sonnet-5"))   # must not raise


def test_the_NEW_brief_boots_the_structured_backend_which_is_the_landed_state(rig, monkeypatch):
    """**The second benign twin: the state this milestone is FOR.** Driven against the brief this
    suite actually ships (`tests/librarian/fixtures/repo/`, the resynced drift-guard copy), so it
    is the real text rather than one written to pass — and the day the knowledge repo's brief
    reacquires an outcome-file mention, this goes red beside the drift test."""
    _, deps = rig
    monkeypatch.setenv(pydantic_backend.PROVIDER_KEY_ENV["openai"], FAKE_KEY)

    worker.startup_checks(_pydantic(deps))                       # must not raise


def test_the_check_reads_what_the_backend_DECLARES_and_not_which_backend_was_named(rig,
                                                                                   monkeypatch):
    """`build_agent(settings).structured_ordinary`, never `settings.backend == "pydantic"` — the
    same reason `processing._one_pass` reads the attribute. A fourth structured backend inherits
    this check by declaring the same thing, and a check keyed on the NAME would let it inject a
    contradictory brief on its first run.

    Asserted at the source, because the fourth backend does not exist yet and a test that waited
    for one would be a rule nobody knows about until it is too late to add cheaply.
    """
    import inspect

    source = inspect.getsource(worker._check_brief_matches_backend)

    assert "build_agent(settings).structured_ordinary" in source
    assert 'settings.backend ==' not in source, (
        "the brief check branches on the backend NAME — a fourth structured backend would inject "
        "a brief written for a tool-holding run on its first item")


# ── the three checks that were always about the BACKEND ────────────────────────────────────────
def test_the_backend_checks_still_run_from_the_check_function_itself(rig):
    """The entry point the cases below use, exercised once against a fully-configured value so a
    signature change here is one failure rather than a dozen."""
    _, deps = rig
    environ = {pydantic_backend.PROVIDER_KEY_ENV["openai"]: FAKE_KEY}

    worker._check_pydantic_backend(_pydantic(deps), environ=environ)


def test_a_bare_model_name_is_refused_because_pydantic_ai_would_pick_a_provider_nobody_chose(rig):
    """The provider-prefix rule, and the reason it is a refusal rather than a default: pydantic-ai
    reads a bare name as the OpenAI Responses API, so the `sdk` backend's own `claude-sonnet-5`
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


# ── the MIRROR: a prefixed id handed to the backend that cannot read one ───────────────────────
# The asymmetry was the defect. One backend refusing a bare id while the other silently accepted a
# prefixed one caught exactly half of one configuration mistake — an operator who set
# `STIGMERGY_LIBRARIAN_MODEL=openai:gpt-5.6-terra` and left `backend=sdk` reached the Claude Agent
# SDK, which has never heard of a provider prefix, and learned it from a failed run instead of a
# startup line. A spelling belongs to a backend; both are now refused by the backend they do not
# belong to.
def test_an_sdk_worker_is_refused_a_provider_prefixed_model(rig):
    """The mirror refusal, and it has to name the fix in the operator's own vocabulary: the bare
    spelling to use, and the backend the prefixed one belongs to."""
    _, deps = rig

    with pytest.raises(LibrarianConfigError) as exc_info:
        worker.startup_checks(dataclasses.replace(deps.settings, backend="sdk",
                                                  model=PRICED_MODEL))

    message = str(exc_info.value)
    assert PRICED_MODEL in message                       # what they set
    assert "'gpt-5.6-terra'" in message                  # ...and the bare spelling to use instead
    assert agent_module.PYDANTIC_BACKEND in message      # ...and whose spelling it actually is


def test_an_sdk_worker_with_the_bare_spelling_passes_that_check(rig, monkeypatch):
    """The benign twin, and the one that matters most: `claude-sonnet-5` is the DEFAULT and the
    shipped configuration. A mirror refusal that also bounced it would refuse every real `sdk`
    worker there is.

    **It used to stop on the missing librarian skill and it no longer does** — ADR 033 put the
    brief into this fixture knowledge repo, because the structured backend reads the same one and
    the suite needed it present. So the claim gets STRONGER rather than weaker: an `sdk` worker
    with a credential and the bare spelling now boots through the whole pre-flight, which is a
    stricter statement than "it was refused for some other reason".
    """
    _, deps = rig
    monkeypatch.setenv(agent_module.CREDENTIAL_ENV[0], FAKE_KEY)

    worker.startup_checks(dataclasses.replace(deps.settings, backend="sdk",
                                              model="claude-sonnet-5"))   # must not raise


# The specificity half of the twin above — an `sdk` worker with NO credential is still refused
# before it claims anything — is `test_startup_preflight.py`'s
# `test_startup_checks_refuses_an_sdk_run_with_no_credential`, which is that file's subject and
# which drives the real check against the package's own cleared environment. Not restated here: a
# second extraction of one refusal is how one of them quietly stops matching while the other stays
# green.


def test_the_double_is_never_refused_over_a_model_it_does_not_read(rig):
    """The double reads no model at all, so a refusal about that field would be a guard inventing
    work for an operator. Both spellings, and a nonsense one: all three boot."""
    _, deps = rig
    for spelling in (PRICED_MODEL, "claude-sonnet-5", "not-a-model-at-all"):
        worker.startup_checks(dataclasses.replace(deps.settings, backend="double",
                                                  model=spelling))


def test_the_prefixed_spelling_is_accepted_by_the_backend_it_belongs_to(rig):
    """The fourth cell of the table, closing it: prefixed + `pydantic` is the configuration this
    backend exists for, and its pre-flight passes it."""
    _, deps = rig
    worker._check_pydantic_backend(
        _pydantic(deps, model=PRICED_MODEL),
        environ={pydantic_backend.PROVIDER_KEY_ENV["openai"]: FAKE_KEY})


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
