"""The `pydantic` backend is refused for a WORKER, and every sentence of that refusal is checked by
running it.

The refusal exists because config that half-works is the failure this repo refuses on principle: a
worker's queue carries ordinary captures too, and a backend that serves the meeting flow only would
drain `raw` and `drive` rows into `failed` one delivery at a time while looking configured. So
`worker.startup_checks` refuses it before a single item is claimed — and hands the operator four
things: the two backends that DO serve every flow, the rig that can measure this one, the exact
command line to run it with, and a document.

**Each of those four is an executable promise, and this file spends them.** The two named backends
are put through the same pre-flight point. The command line is parsed by the eval runner's own
`main`, with the measurement itself stubbed out at the seam where dollars start. The document is
opened. A refusal that names a command nobody has run is a refusal that costs somebody a day, and
this repo has already paid that once (`test_startup_preflight.py`'s own four-detour docstring).

The three checks BELOW the refusal are the meeting-only rig's, reached with `meeting_only=True`:
a provider-prefixed model, a configured price, and the provider's own key. They are exercised
through `_check_pydantic_backend` with an INJECTED environment mapping — the check is a pure
function of a mapping and a model string, exactly like `agent.credential_status`, and asserting it
against a process whose environment has been mutated around it would prove less and flake more.
"""
import dataclasses
import json
import logging
import pathlib
import re

import pytest

from stigmergy.librarian import agent as agent_module
from stigmergy.librarian import pricing, pydantic_backend, worker
from stigmergy.librarian.errors import AgentError, LibrarianConfigError
from stigmergy.librarian.pydantic_backend import PydanticMeetingAgent

ROOT = pathlib.Path(__file__).resolve().parents[2]

PRICED_MODEL = "openai:gpt-5.6-terra"
FAKE_KEY = "sk-fixture-not-a-real-key"


def _pydantic(deps, **overrides):
    """`deps.settings` switched to the backend under test, with a provider-prefixed model unless a
    case is about the model itself."""
    return dataclasses.replace(deps.settings, backend=agent_module.PYDANTIC_BACKEND,
                               **{"model": PRICED_MODEL, **overrides})


# ── the worker refusal ─────────────────────────────────────────────────────────────────────────
def test_a_worker_is_refused_the_meeting_only_backend_before_it_claims_anything(rig):
    """The refusal itself, and the reason it gives: a worker's queue carries every kind, and this
    backend serves one. It fires from `startup_checks` — before the lease check, before the
    scanner, before any git read — so an operator who exported the wrong value learns it in one
    line rather than from a column of `failed` rows."""
    _, deps = rig

    with pytest.raises(LibrarianConfigError) as exc_info:
        worker.startup_checks(_pydantic(deps))

    message = str(exc_info.value)
    assert agent_module.PYDANTIC_BACKEND in message
    assert "meeting" in message.lower()
    # the two that DO serve every flow, spelled as the variable an operator actually exports
    assert "STIGMERGY_LIBRARIAN_BACKEND=sdk" in message
    assert "STIGMERGY_LIBRARIAN_BACKEND=double" in message
    # ...and the double is named with its warning attached, never offered as a bare workaround:
    # it fabricates pages, and a knowledge repo is what would receive them.
    assert "tests only" in message


def test_the_refusal_points_at_a_decision_record_that_exists(rig):
    """A refusal that cites a document is only as good as the document. Opened from the repo root,
    so a renamed or unwritten ADR fails here rather than at the operator's 404."""
    _, deps = rig
    with pytest.raises(LibrarianConfigError) as exc_info:
        worker.startup_checks(_pydantic(deps))

    assert pydantic_backend.ADR in str(exc_info.value)
    adr = ROOT / pydantic_backend.ADR
    assert adr.is_file(), f"the refusal cites {pydantic_backend.ADR}, which does not exist"
    body = adr.read_text(encoding="utf-8")
    assert agent_module.PYDANTIC_BACKEND in body
    assert pricing.PRICING_ENV in body, (
        "the ADR the refusal cites says nothing about the pricing seam it decided")


def test_the_two_backends_the_refusal_names_are_the_ones_that_actually_serve_a_worker(rig,
                                                                                      monkeypatch):
    """**The benign twin of the refusal, and it is the half that could break a working deployment.**
    A guard that refused one backend is worth nothing if it also refuses the two it recommends.

    `double` boots all the way through — it is what the whole suite runs on. `sdk` is taken as far
    as this fixture can honestly take it: with a credential exported it passes the backend check and
    the lease, and stops at the librarian skill, which this fixture knowledge repo deliberately does
    not carry (it carries the meeting brief only). That the refusal it meets is about the SKILL and
    not about the backend is exactly the claim — `sdk` is not a backend a worker is refused for
    being.
    """
    _, deps = rig

    assert deps.settings.backend == "double"
    worker.startup_checks(deps.settings)                     # must not raise

    monkeypatch.setenv(agent_module.CREDENTIAL_ENV[0], FAKE_KEY)
    with pytest.raises(LibrarianConfigError) as exc_info:
        worker.startup_checks(dataclasses.replace(deps.settings, backend="sdk"))
    message = str(exc_info.value)
    assert "librarian skill" in message
    assert agent_module.PYDANTIC_BACKEND not in message, (
        "an `sdk` worker was refused with the meeting-only backend's own sentence")


def test_the_worker_refusal_offers_a_model_this_environment_can_actually_price(rig):
    """The `--model` in the printed command is DERIVED, not hardcoded: an operator whose model is
    already provider-prefixed sees their own, and one whose model is the `sdk` backend's bare
    spelling sees an id this environment can price. Either way, pasting it must not walk them into
    the next refusal down."""
    _, deps = rig

    # their own, when it is already provider-prefixed
    with pytest.raises(LibrarianConfigError) as exc_info:
        worker.startup_checks(_pydantic(deps, model=PRICED_MODEL))
    assert f"--model {PRICED_MODEL}" in str(exc_info.value)

    # ...and a priced substitute when it is the bare `sdk` spelling, which pydantic-ai would resolve
    # to a provider nobody chose
    with pytest.raises(LibrarianConfigError) as exc_info:
        worker.startup_checks(_pydantic(deps, model="claude-sonnet-5"))
    offered = re.search(r"--model (\S+)\. See ", str(exc_info.value))
    assert offered, "the refusal printed no --model to paste"
    assert pricing.require_priced(offered.group(1))          # the offer is priced, not a guess
    assert pydantic_backend.provider_of(offered.group(1)), (
        "the refusal offered a bare model id, which is the very next thing it refuses")


# **The command line the refusal prints is spent in `test_operator_surface.py`**, which is this
# package's declared home for "a message containing a command is an executable promise" — beside the
# make target `_check_agent_credential` names and the runbook `_check_push_identity` names. It is
# driven there through `run_filing.build_parser()`, a seam that did not exist when this file was
# written and that removed the need to monkeypatch `sys.argv` and stub the measurement. Kept as one
# home rather than two: three near-identical extractions of the same sentence is how one of them
# quietly stops matching and nobody notices, because the other two are green.
#
# What stays HERE is the shape of the message itself (above) — the two backends it names, the
# document it cites, and the `--model` example it derives.


# ── the three checks the meeting-only rig still owes ───────────────────────────────────────────
def test_the_meeting_only_rig_passes_the_preflight_the_worker_is_refused_by(rig):
    """The other half of the same guard: `meeting_only=True` is the rig's claim that it hands the
    agent meeting rows and nothing else, and the pre-flight then validates the REST of the
    configuration instead of refusing outright. A `meeting_only` that also refused would leave the
    backend unmeasurable, which is the milestone's whole point."""
    _, deps = rig
    environ = {pydantic_backend.PROVIDER_KEY_ENV["openai"]: FAKE_KEY}

    worker._check_pydantic_backend(_pydantic(deps), meeting_only=True, environ=environ)


def test_a_bare_model_name_is_refused_because_pydantic_ai_would_pick_a_provider_nobody_chose(rig):
    """The provider-prefix rule, and the reason it is a refusal rather than a default: pydantic-ai
    reads a bare name as the OpenAI Responses API, so the `sdk` backend's own `claude-sonnet-5`
    would file this brain's meetings through a provider the operator never named — silently, and
    correctly enough that nothing downstream would notice."""
    _, deps = rig
    with pytest.raises(LibrarianConfigError) as exc_info:
        worker._check_pydantic_backend(_pydantic(deps, model="claude-sonnet-5"),
                                       meeting_only=True, environ={})

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
        worker._check_pydantic_backend(_pydantic(deps, model="openai:gpt-9"),
                                       meeting_only=True, environ={})
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
        worker._check_pydantic_backend(_pydantic(deps, model=model), meeting_only=True, environ={})

    message = str(exc_info.value)
    assert key_env in message
    assert model in message


@pytest.mark.parametrize("provider, key_env", sorted(pydantic_backend.PROVIDER_KEY_ENV.items()))
def test_each_known_provider_family_passes_once_its_key_is_present(rig, provider, key_env):
    """The benign twin, one per family: a pre-flight that can only refuse is a pre-flight nobody
    can satisfy, and this one stands between the operator and every measurement of the backend."""
    _, deps = rig
    model = next(m for m in pricing.PRICES if m.startswith(f"{provider}:"))

    worker._check_pydantic_backend(_pydantic(deps, model=model), meeting_only=True,
                                   environ={key_env: FAKE_KEY})


def test_an_empty_key_is_not_a_key(rig):
    """An exported-but-empty variable is the shape a half-written env file produces, and it must not
    read as configured."""
    _, deps = rig
    with pytest.raises(LibrarianConfigError, match=re.escape("OPENAI_API_KEY")):
        worker._check_pydantic_backend(
            _pydantic(deps), meeting_only=True,
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
        worker._check_pydantic_backend(_pydantic(deps, model=exotic), meeting_only=True,
                                       environ={})

    assert len(caplog.records) == 1, "the advisory is one line, not a paragraph of them"
    assert caplog.records[0].levelno == logging.WARNING
    assert "mistral:" in caplog.records[0].getMessage()


def test_a_listed_provider_with_its_key_says_nothing_at_all(rig, caplog):
    """The advisory's own specificity half: a fully configured run must not be told it is relying
    on anything, or the line stops meaning what it says."""
    _, deps = rig
    with caplog.at_level(logging.WARNING, logger="stigmergy.librarian.worker"):
        worker._check_pydantic_backend(
            _pydantic(deps), meeting_only=True,
            environ={pydantic_backend.PROVIDER_KEY_ENV["openai"]: FAKE_KEY})
    assert caplog.records == []


def test_the_key_preflight_reads_the_process_environment_when_nothing_is_injected(rig,
                                                                                  monkeypatch):
    """The injectable mapping is for the tests; production passes nothing and reads `os.environ`.
    Proven once, here, so the default is not a branch only tests take."""
    _, deps = rig
    monkeypatch.setenv(pydantic_backend.PROVIDER_KEY_ENV["openai"], FAKE_KEY)
    worker._check_pydantic_backend(_pydantic(deps), meeting_only=True)   # must not raise

    monkeypatch.delenv(pydantic_backend.PROVIDER_KEY_ENV["openai"], raising=False)
    with pytest.raises(LibrarianConfigError):
        worker._check_pydantic_backend(_pydantic(deps), meeting_only=True)


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
    worker there is. Taken as far as this fixture goes — it stops on the librarian skill, which this
    fixture knowledge repo deliberately does not carry, and not on the model spelling."""
    _, deps = rig
    monkeypatch.setenv(agent_module.CREDENTIAL_ENV[0], FAKE_KEY)

    with pytest.raises(LibrarianConfigError) as exc_info:
        worker.startup_checks(dataclasses.replace(deps.settings, backend="sdk",
                                                  model="claude-sonnet-5"))

    assert "librarian skill" in str(exc_info.value)
    assert "spelling" not in str(exc_info.value)


def test_the_double_is_never_refused_over_a_model_it_does_not_read(rig):
    """The double reads no model at all, so a refusal about that field would be a guard inventing
    work for an operator. Both spellings, and a nonsense one: all three boot."""
    _, deps = rig
    for spelling in (PRICED_MODEL, "claude-sonnet-5", "not-a-model-at-all"):
        worker.startup_checks(dataclasses.replace(deps.settings, backend="double",
                                                  model=spelling))


def test_the_prefixed_spelling_is_accepted_by_the_backend_it_belongs_to(rig):
    """The fourth cell of the table, closing it: prefixed + `pydantic` is the configuration the
    milestone exists for, and the meeting-only rig's pre-flight passes it."""
    _, deps = rig
    worker._check_pydantic_backend(
        _pydantic(deps, model=PRICED_MODEL), meeting_only=True,
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


# ── the ordinary flow on a meeting-only backend ────────────────────────────────────────────────
def test_the_ordinary_flow_refuses_honestly_and_names_the_document(rig):
    """`run` exists because the port requires it. It must refuse as an `AgentError` — the family
    `processing` already turns into a `failed` row with a sentence on it — and never as a
    `NotImplementedError`, which would surface at an operator as a traceback for a configuration
    that was refused at startup anyway."""
    _, deps = rig
    backend = PydanticMeetingAgent(_pydantic(deps))

    with pytest.raises(AgentError) as exc_info:
        backend.run(worktree="/tmp/nonexistent", material="a note", hints={},
                    submitted_by="a@b.test")

    message = str(exc_info.value)
    assert pydantic_backend.ADR in message
    assert "'sdk'" in message and "'double'" in message
    assert not isinstance(exc_info.value, NotImplementedError)


def test_that_refusal_is_unreachable_through_a_worker_which_is_why_it_may_be_terse(rig):
    """The claim the refusal above makes about itself — "a state no worker can reach" — checked
    rather than asserted in prose: the only way to a worker's `run` is `startup_checks` passing,
    and it does not."""
    _, deps = rig
    with pytest.raises(LibrarianConfigError):
        worker.startup_checks(_pydantic(deps), meeting_only=False)
