"""The `sdk` backend's retirement, from the one angle that outlives the code: **the configuration
value survives the deletion, and the first worker to boot on the new image is configured for a
backend that is not there.**

A deployment carries `STIGMERGY_LIBRARIAN_BACKEND` in `fly.toml`'s `[env]` or in a gitignored
`.env`, and neither is updated by a `git pull`. So the retirement's real contract is not "the code
is gone" — that is trivially true and untestable — it is what a stale deployment is TOLD, and
whether everything that message promises is real. `agent.RETIRED_BACKENDS` is where the sentence
lives and `agent.ensure_known_backend` is the one place either refusal is worded.

**Every promise in that message is executed here**, because a refusal that sends an operator
mid-incident to a command, a document or a model id that does not work burns their trust in the
next message too:

* the two `fly` commands it prints are the runbook's own Rollback section, verbatim;
* the model id it offers as the replacement — `anthropic:claude-sonnet-5` — clears every refusal
  BELOW this one, which is the rule `worker.py` records where `_usable_example` used to be: a
  refusal whose own example fails the refusal below it is worse than one with no example;
* and its central claim — "changing only the backend swaps this refusal for that one" — is driven
  as a real two-step upgrade rather than read.

**Both roads are exercised**, because they are two callers of one function and a retirement is
exactly the change that would have updated one of them: `ensure_known_backend` as the pure function
it is, and `worker.startup_checks` as the integration road a stale deployment actually takes before
a single item is claimed.

Keyless throughout: every refusal here is a pure function of a string, a mapping and a git repo.
"""
import dataclasses
import pathlib
import re

import pytest

from stigmergy.librarian import agent as agent_module
from stigmergy.librarian import config, pricing, pydantic_backend, worker
from stigmergy.librarian.errors import LibrarianConfigError

ROOT = pathlib.Path(__file__).resolve().parents[2]
RUNBOOK = ROOT / "docs" / "reference" / "operator-runbook.md"

# The value a stale deployment carries. Spelled out here rather than read off `RETIRED_BACKENDS`,
# and that is the point of this file: the table could lose its entry and every derived assertion
# would keep passing while the deployment it exists for got "invalid librarian backend 'sdk'" — the
# typo message for somebody who did not mistype anything.
RETIRED_VALUE = "sdk"

FAKE_KEY = "sk-fixture-not-a-real-key"
ANTHROPIC_KEY_ENV = pydantic_backend.PROVIDER_KEY_ENV["anthropic"]


def _refusal(backend: str) -> str:
    with pytest.raises(LibrarianConfigError) as exc_info:
        agent_module.ensure_known_backend(backend)
    return str(exc_info.value)


# ── the refusal itself ─────────────────────────────────────────────────────────────────────────
def test_the_retired_value_is_still_in_the_table_the_refusal_is_read_from():
    """First, because everything below derives from it. A retirement whose VALUE is forgotten is a
    retirement that only helped the people who did not need it."""
    assert RETIRED_VALUE in agent_module.RETIRED_BACKENDS
    assert RETIRED_VALUE not in agent_module.BACKENDS


def test_a_retired_backend_is_refused_by_name_and_not_as_a_typo():
    """**THE message.** An operator did not mistype anything — their configuration aged past the
    code — so the refusal has to say what happened, what replaces it, how to get running again
    while they make the edit. Each of those is one assertion, in that order."""
    message = _refusal(RETIRED_VALUE)

    assert "RETIRED" in message                                     # what happened
    assert "not a typo" in message                                  # ...and what it is NOT
    assert agent_module.PYDANTIC_BACKEND in message                 # what replaces it
    assert "anthropic:claude-sonnet-5" in message                   # ...spelled for that backend
    assert "fly releases" in message                                # how to get running again
    assert "fly deploy --image" in message


def test_the_refusal_names_the_two_places_a_stale_value_actually_lives():
    """The whole reason this refusal exists rather than a deletion note in a CHANGELOG: the value
    is in a file `git pull` does not touch. An operator who is told "retired" and not WHERE to
    change it goes looking through the code."""
    message = _refusal(RETIRED_VALUE)
    assert "fly.toml" in message
    assert ".env" in message


def test_the_refusal_says_the_upgrade_is_two_edits_and_names_both():
    """The half that is easy to miss, and the reason the message is a paragraph rather than a line:
    the backend and the model id are one edit each, and doing only the first lands on a DIFFERENT
    refusal. Saying so in advance is what stops that being discovered twice."""
    message = _refusal(RETIRED_VALUE)
    assert "TWO edits" in message
    assert f"STIGMERGY_LIBRARIAN_BACKEND={agent_module.PYDANTIC_BACKEND}" in message
    assert "STIGMERGY_LIBRARIAN_MODEL" in message


def test_every_retired_value_refuses_with_a_message_that_points_at_a_live_backend():
    """Derived, so a SECOND retirement inherits the shape rather than the next person rediscovering
    it: whatever a retired entry says, it has to name a backend that still exists — a refusal that
    retires a value without naming the road forward is a dead end with extra words."""
    for value, sentence in agent_module.RETIRED_BACKENDS.items():
        message = _refusal(value)
        assert message == sentence, f"{value!r} is refused with something other than its own entry"
        assert any(live in message for live in agent_module.BACKENDS), (
            f"the refusal for {value!r} names no surviving backend")


# ── the SPECIFICITY half: two mistakes, two sentences ──────────────────────────────────────────
def test_an_unknown_backend_is_refused_without_the_word_retired():
    """The twin that decides whether the retirement message is worth having at all. "Retired" is a
    claim about history: told to somebody who typed `pydnatic`, it sends them looking for a
    migration that does not concern them. The two refusals are different sentences for different
    mistakes, and this is what keeps them different."""
    message = _refusal("nonsense")

    assert "retired" not in message.lower()
    assert "nonsense" in message                                  # what they set
    for live in agent_module.BACKENDS:                            # ...and what they may set
        assert live in message


def test_a_live_backend_is_not_refused_at_all():
    """The other specificity half, and the cheapest possible regression net: every shipped value
    passes the gate every worker and every `build_agent` call goes through."""
    for live in agent_module.BACKENDS:
        agent_module.ensure_known_backend(live)                   # must not raise


# ── the integration road: what a stale DEPLOYMENT meets, before a single item is claimed ───────
def test_a_stale_deployment_meets_the_retirement_refusal_at_startup(rig):
    """`worker.startup_checks` is the road, and it is asserted to carry the SAME sentence rather
    than merely to fail: the two callers of `ensure_known_backend` used to hold two copies of one
    message, and a retirement is exactly the change that would have updated one of them."""
    _, deps = rig

    with pytest.raises(LibrarianConfigError) as exc_info:
        worker.startup_checks(dataclasses.replace(deps.settings, backend=RETIRED_VALUE))

    assert str(exc_info.value) == agent_module.RETIRED_BACKENDS[RETIRED_VALUE]


def test_the_retirement_is_refused_before_anything_expensive_is_touched(rig, monkeypatch):
    """It is the FIRST check for a reason: a stale worker must not spend a fetch, a scanner probe
    or a blob read discovering that its backend does not exist. Proven by making every step after
    it explode — if the refusal is still the retirement's own sentence, nothing below it ran."""
    _, deps = rig

    def boom(*_a, **_k):
        raise AssertionError("startup_checks got past the backend check on a retired value")

    monkeypatch.setattr(worker.gates, "ensure_scanner", boom)
    monkeypatch.setattr(worker.gitcmd, "base_ref", boom)

    with pytest.raises(LibrarianConfigError) as exc_info:
        worker.startup_checks(dataclasses.replace(deps.settings, backend=RETIRED_VALUE))

    assert "RETIRED" in str(exc_info.value)


def test_the_dispatch_refuses_the_retired_value_too_for_the_caller_that_skipped_the_preflight(rig):
    """The eval rig and any script build an agent without running `startup_checks` at all. A typo
    must never fall through to the paid path, and a RETIRED value must say so in those words on
    that road as well."""
    _, deps = rig

    with pytest.raises(LibrarianConfigError) as exc_info:
        agent_module.build_agent(dataclasses.replace(deps.settings, backend=RETIRED_VALUE))

    assert str(exc_info.value) == agent_module.RETIRED_BACKENDS[RETIRED_VALUE]


# ── the EXECUTABLE PROMISES: every artifact the message sends somebody to ──────────────────────
def test_the_two_fly_commands_the_refusal_prints_are_the_runbooks_own_rollback():
    """The one operation the message asks an operator to perform mid-incident, and the reason it is
    quoted rather than paraphrased: it is lifted from the runbook's Rollback section, so the
    message and the procedure cannot come to disagree about how a deployment is rolled back.

    The commands are extracted from the MESSAGE and looked for in the runbook — that direction, so
    a message that starts printing a command the runbook does not document fails here rather than
    the reverse.
    """
    message = agent_module.RETIRED_BACKENDS[RETIRED_VALUE]
    commands = [c for c in re.findall(r"`([^`]+)`", message) if c.startswith("fly ")]
    assert commands, "the refusal no longer prints a fly command — update this test with what it does"

    assert RUNBOOK.exists()
    runbook = RUNBOOK.read_text(encoding="utf-8")
    section = runbook[runbook.index("### Rollback"):][:800]
    for command in commands:
        # `fly deploy --image <that image ref>` is printed with the operator's own placeholder; the
        # runbook carries the same line. Compare the invocation, not the placeholder's spelling.
        invocation = command.split("<")[0].strip()
        assert invocation in section, (
            f"the retirement refusal tells an operator to run `{command}`, and the runbook's "
            f"Rollback section does not document `{invocation}`")


def test_the_runbook_section_the_refusal_names_is_the_one_that_exists():
    """The message says "(docs/reference/operator-runbook.md, Rollback)". A section name is a
    promise as much as a command is — an operator searching a 900-line runbook for a heading that
    is not there is exactly the dead end this rule exists to prevent."""
    message = agent_module.RETIRED_BACKENDS[RETIRED_VALUE]
    assert "Rollback" in message
    assert "### Rollback" in RUNBOOK.read_text(encoding="utf-8")


# ── the replacement it offers has to BOOT: the shipped default, priced and provider-prefixed ───
# `worker.py` records the rule where `_usable_example` used to be: a refusal whose own example
# fails the refusal below it is worse than one with no example. This refusal offers exactly one
# model id, and it is the SHIPPED DEFAULT — so the id, the default and the price table are one
# claim, checked as one.
def test_the_model_the_refusal_offers_is_the_shipped_default():
    """The default moved with the retirement (`claude-sonnet-5` → `anthropic:claude-sonnet-5`) and
    this is where the two are tied together: an operator who takes the refusal's advice and an
    operator who sets nothing at all must end up on the same model."""
    assert config.DEFAULT_MODEL in agent_module.RETIRED_BACKENDS[RETIRED_VALUE]


def test_the_shipped_default_is_provider_prefixed_because_a_bare_one_is_unbootable():
    """The correction the retirement forced. pydantic-ai reads a BARE name as the OpenAI Responses
    API, so `worker._check_pydantic_backend` refuses one — a bare default would make the shipped
    default unbootable for every worker that did not override it, which is the one value that must
    never need overriding."""
    assert pydantic_backend.provider_of(config.DEFAULT_MODEL), (
        f"config.DEFAULT_MODEL is {config.DEFAULT_MODEL!r}, which names no provider — the shipped "
        f"default is refused by the backend's own pre-flight")


def test_the_shipped_default_is_priced_so_a_default_run_can_report_what_it_cost():
    """The second half of bootable, and it is a separate refusal in production: an unpriced id is
    refused by `pricing.require_priced` before the run, because `$0.00` for work that costs money
    reads as free."""
    assert config.DEFAULT_MODEL in pricing.PRICES
    pricing.require_priced(config.DEFAULT_MODEL)                  # must not raise


def test_a_worker_on_the_shipped_default_clears_the_backend_preflight(rig):
    """**The benign twin the whole retirement rests on.** Everything above says the refusal points
    somewhere; this says the place it points to WORKS. The environment is injected rather than
    exported, because the check is a pure function of a mapping and a model string."""
    _, deps = rig

    worker._check_pydantic_backend(
        dataclasses.replace(deps.settings, backend=agent_module.PYDANTIC_BACKEND,
                            model=config.DEFAULT_MODEL),
        environ={ANTHROPIC_KEY_ENV: FAKE_KEY})                    # must not raise


def test_a_worker_on_the_shipped_default_boots_through_the_WHOLE_preflight(rig, monkeypatch):
    """The same claim on the fullest keyless road there is: `startup_checks`, against a real git
    repo, a real scanner and the fixture knowledge repo's own brief at the base commit. The
    injected-mapping test above proves the unit; this proves the operator's actual first command.
    """
    _, deps = rig
    monkeypatch.setenv(ANTHROPIC_KEY_ENV, FAKE_KEY)

    resolved = worker.startup_checks(
        dataclasses.replace(deps.settings, backend=agent_module.PYDANTIC_BACKEND,
                            model=config.DEFAULT_MODEL))

    assert resolved["base"].ref                                   # it got all the way to the ref


def test_the_offline_double_still_boots_end_to_end(rig):
    """The other benign twin, and the one CI depends on: the suite, the container e2e and every
    walk script run this backend. A retirement that broke the double would be discovered by
    everything at once — which is precisely why it is asserted deliberately, once, here."""
    _, deps = rig
    assert deps.settings.backend == "double"

    resolved = worker.startup_checks(deps.settings)               # must not raise

    assert resolved["repo"] == deps.settings.repo


# ── the TWO-EDIT path: changing only the backend lands on the second refusal, as promised ──────
def test_changing_only_the_backend_swaps_this_refusal_for_the_model_one(rig):
    """**The message's own central claim, executed.** A stale deployment carries `backend=sdk` AND
    the bare `claude-sonnet-5` the retired backend spelled its model with. Change the first value
    only — the obvious reading of "the replacement is pydantic" — and the run must land on the
    provider-prefix refusal, not on a run that files this brain's captures through a provider
    nobody chose."""
    _, deps = rig
    stale = dataclasses.replace(deps.settings, backend=RETIRED_VALUE, model="claude-sonnet-5")

    assert "A bare id is refused by the check below this one" in _refusal(stale.backend)

    with pytest.raises(LibrarianConfigError) as exc_info:
        worker.startup_checks(dataclasses.replace(stale,
                                                  backend=agent_module.PYDANTIC_BACKEND))

    message = str(exc_info.value)
    assert "claude-sonnet-5" in message                           # what they left behind
    assert "anthropic:claude-sonnet-5" in message, (
        "the second refusal does not spell out the prefixed id — an operator who has just been "
        "told the model needs a prefix is now being asked to guess which one")
    assert "retired" in message.lower(), (
        "the second refusal does not connect itself to the first — this is the message a "
        "half-finished upgrade lands on, and it has to say so")


def test_the_two_refusals_are_not_the_same_sentence(rig):
    """They are two steps of one upgrade and they must read as two: an operator who gets the same
    paragraph twice cannot tell whether their first edit took effect."""
    _, deps = rig

    with pytest.raises(LibrarianConfigError) as exc_info:
        worker.startup_checks(dataclasses.replace(deps.settings,
                                                  backend=agent_module.PYDANTIC_BACKEND,
                                                  model="claude-sonnet-5"))

    assert str(exc_info.value) != agent_module.RETIRED_BACKENDS[RETIRED_VALUE]


def test_the_finished_upgrade_boots(rig, monkeypatch):
    """The end of the road: both edits made, the provider key exported. This is the state the whole
    message is steering an operator towards, and nothing else in this file asserts they arrive."""
    _, deps = rig
    monkeypatch.setenv(ANTHROPIC_KEY_ENV, FAKE_KEY)

    worker.startup_checks(dataclasses.replace(deps.settings,
                                              backend=agent_module.PYDANTIC_BACKEND,
                                              model="anthropic:claude-sonnet-5"))
