"""`librarian.config.Settings`: precedence (CLI flag -> env var -> class default, module
docstring), and the computed paths derived from `repo` (`acl_path`, `registry_path`,
`linter_path`) that every fixture in this suite relies on implicitly.
"""
import pytest

from stigmergy.librarian import config
from stigmergy.librarian.errors import LibrarianConfigError


class _Args:
    """A stand-in for `argparse.Namespace` — only the attributes `Settings.from_args` reads."""

    def __init__(self, **kwargs):
        for key, value in kwargs.items():
            setattr(self, key, value)


def _args(**overrides):
    base = dict(repo=None, branch=None, dsn=None, backend=None, poll_interval=None,
               visibility_timeout=None, max_attempts=None)
    base.update(overrides)
    return _Args(**base)


def test_defaults_with_no_flags_and_no_env(monkeypatch):
    for var in ("STIGMERGY_REPO", "STIGMERGY_LIBRARIAN_BRANCH", "STIGMERGY_LIBRARIAN_BACKEND",
               "STIGMERGY_LIBRARIAN_MODEL", "STIGMERGY_LIBRARIAN_MAX_TURNS",
               "STIGMERGY_LIBRARIAN_MAX_TOOL_CALLS", "STIGMERGY_LIBRARIAN_TIMEOUT_S",
               "STIGMERGY_LIBRARIAN_DEDUP_WINDOW_S", "STIGMERGY_GITLEAKS_BIN",
               "STIGMERGY_LIBRARIAN_WORKTREE_ROOT"):
        monkeypatch.delenv(var, raising=False)

    settings = config.Settings.from_args(_args())

    assert settings.repo == config.REPO_DEFAULT
    assert settings.branch == "main"
    assert settings.backend == "double"
    assert settings.model == config.DEFAULT_MODEL
    assert settings.max_turns == config.DEFAULT_MAX_TURNS
    assert settings.gitleaks_bin == "gitleaks"
    assert settings.worktree_root == ""


def test_a_cli_flag_beats_the_env_var(monkeypatch):
    monkeypatch.setenv("STIGMERGY_REPO", "/env/repo")
    settings = config.Settings.from_args(_args(repo="/flag/repo"))
    assert settings.repo == "/flag/repo"


def test_the_env_var_beats_the_class_default(monkeypatch):
    monkeypatch.setenv("STIGMERGY_REPO", "/env/repo")
    settings = config.Settings.from_args(_args())
    assert settings.repo == "/env/repo"


def test_backend_is_lowercased_regardless_of_source(monkeypatch):
    monkeypatch.delenv("STIGMERGY_LIBRARIAN_BACKEND", raising=False)
    settings = config.Settings.from_args(_args(backend="DOUBLE"))
    assert settings.backend == "double"


@pytest.mark.parametrize("raw", ["openai:gpt-5.6-terra ", " openai:gpt-5.6-terra",
                                 "openai:gpt-5.6-terra\n", "\topenai:gpt-5.6-terra\t"])
def test_the_model_is_stripped_at_the_one_place_the_environment_is_read(monkeypatch, raw):
    """**A trailing space out of an env file, or a newline out of a shell export, used to pass every
    pre-flight and fail inside the provider client.**

    The id is compared, prefix-parsed and priced by three different checks that each strip on their
    own — so `provider_of`, `require_priced` and the backend-spelling mirror all said yes — and then
    the unstripped string was handed to a framework that does not strip. Normalizing HERE, at the
    single place this package reads the environment, is what makes the pre-flights and the run agree
    about the same string.
    """
    monkeypatch.setenv("STIGMERGY_LIBRARIAN_MODEL", raw)
    assert config.Settings.from_args(_args()).model == "openai:gpt-5.6-terra"


def test_a_stripped_model_is_the_one_the_pricing_and_prefix_checks_then_agree_about(monkeypatch):
    """The consequence, spelled out rather than left to inference: after the strip, the SAME string
    satisfies the provider-prefix rule and the price table. That agreement is the whole point of
    normalizing at one place instead of at three."""
    from stigmergy.librarian import pricing, pydantic_backend

    monkeypatch.setenv("STIGMERGY_LIBRARIAN_MODEL", " openai:gpt-5.6-terra\n")
    monkeypatch.delenv(pricing.PRICING_ENV, raising=False)

    model = config.Settings.from_args(_args()).model

    assert pydantic_backend.provider_of(model) == "openai"
    assert pricing.require_priced(model)
    assert model in pricing.priced_models()


def test_an_unset_model_is_still_the_class_default_after_the_strip(monkeypatch):
    """The benign twin: normalizing must not turn "nothing configured" into something else. The
    default has no whitespace to lose and comes back unchanged."""
    monkeypatch.delenv("STIGMERGY_LIBRARIAN_MODEL", raising=False)
    assert config.Settings.from_args(_args()).model == config.DEFAULT_MODEL


def test_numeric_env_vars_are_coerced_to_their_declared_types(monkeypatch):
    monkeypatch.setenv("STIGMERGY_LIBRARIAN_MAX_TURNS", "7")
    monkeypatch.setenv("STIGMERGY_LIBRARIAN_TIMEOUT_S", "42")
    monkeypatch.setenv("STIGMERGY_LIBRARIAN_DEDUP_WINDOW_S", "99")
    settings = config.Settings.from_args(_args())
    assert settings.max_turns == 7
    assert isinstance(settings.max_turns, int)
    assert settings.timeout_s == 42
    assert settings.dedup_window_s == 99


def test_visibility_timeout_outlives_one_items_worst_case_rather_than_inheriting_the_queues():
    """This used to pin `visibility_timeout_s == capture_queue.DEFAULT_VISIBILITY_TIMEOUT_S`, on
    the doctrine "the queue's own default, not a second number invented here". The doctrine is
    right and that number was wrong: 300s was chosen
    for a human-scale claim, while one librarian item is two agent attempts at `timeout_s` each
    plus the gates, the commit and a retrying push. The lease was less than half the worst-case
    item, so the queue could redeliver a row this worker was still processing — and since the
    commit and push happen before `finish`, both workers filed it (reproduced end to end).

    The anti-drift intent is kept where it belongs: the number is DERIVED, not invented, so what
    is pinned here is the relationship rather than a literal. `max_attempts` still comes from the
    queue — that one genuinely is the same question for both tools."""
    from stigmergy.capture import queue as capture_queue
    settings = config.Settings.from_args(_args())
    assert settings.visibility_timeout_s > config.minimum_visibility_timeout_s(
        timeout_s=settings.timeout_s)
    assert settings.visibility_timeout_s == (
        config.MAX_AGENT_ATTEMPTS * settings.timeout_s
        + config.GATE_BUDGET_S + config.VISIBILITY_HEADROOM_S)
    assert settings.max_attempts == capture_queue.DEFAULT_MAX_ATTEMPTS


def test_a_raised_agent_timeout_raises_the_derived_visibility_with_it(monkeypatch):
    """ADR 028: the deployed worker (`stigmergy-librarian-boot`, no CLI flags) must stay
    BOOTABLE when the agent budget is tuned through its env var — the derivation the module
    docstring promises is real, not a static constant. A measured 600s tune in `fly.toml` against
    a 900s static visibility default was a startup refusal nothing deployed could answer."""
    monkeypatch.setenv("STIGMERGY_LIBRARIAN_TIMEOUT_S", "600")
    settings = config.Settings.from_args(_args())
    assert settings.timeout_s == 600
    assert settings.visibility_timeout_s == (
        config.MAX_AGENT_ATTEMPTS * 600 + config.GATE_BUDGET_S + config.VISIBILITY_HEADROOM_S)


# ══════════════════════════════════════════════════════════════════════════════════════════════
# `resolved_visibility_timeout_s`. A caller with no `argparse.Namespace` to hand
# `Settings.from_args` (the admin console's Worker tab: `meta()`, `worker_status()`,
# `_in_flight()`'s three verdicts, and `queue_reclaim()`'s default horizon — all four in
# `stigmergy.admin.service`) still needs the SAME derived lease the deployed worker actually holds,
# not `DEFAULT_VISIBILITY_TIMEOUT_S` — the CLASS default, frozen once at whatever moment this
# module happens to be imported, that never moves with `$STIGMERGY_LIBRARIAN_TIMEOUT_S`. This is the
# ONE shared resolution function every such caller now agrees on, read at CALL time like every
# other env fallback in this module (module docstring: "no module reads the environment at import
# time... `from_args` is the single place" — this function is the second place, for callers that
# have no `args` to give it).
# ══════════════════════════════════════════════════════════════════════════════════════════════
def test_resolved_visibility_timeout_s_is_the_class_default_with_no_env_var(monkeypatch):
    monkeypatch.delenv("STIGMERGY_LIBRARIAN_TIMEOUT_S", raising=False)
    assert config.resolved_visibility_timeout_s() == 900
    assert config.resolved_visibility_timeout_s() == config.DEFAULT_VISIBILITY_TIMEOUT_S


def test_resolved_visibility_timeout_s_derives_1500_from_stagings_own_env_var(monkeypatch):
    """The exact number at stake: staging sets `STIGMERGY_LIBRARIAN_TIMEOUT_S=600`
    (`fly.toml`), which derives a 1500s lease — 2 agent attempts * 600s + 120s gate budget + 180s
    headroom, the SAME arithmetic `test_a_raised_agent_timeout_raises_the_derived_visibility_
    with_it` above pins on `Settings.from_args`."""
    monkeypatch.setenv("STIGMERGY_LIBRARIAN_TIMEOUT_S", "600")
    assert config.resolved_visibility_timeout_s() == 1500
    assert config.resolved_visibility_timeout_s() == (
        config.MAX_AGENT_ATTEMPTS * 600 + config.GATE_BUDGET_S + config.VISIBILITY_HEADROOM_S)


def test_resolved_visibility_timeout_s_reads_the_env_at_call_time_not_a_value_cached_at_import(
        monkeypatch):
    """`config` was imported at the TOP of this file, long before this test body runs — so calling
    the function once with the env var absent, then again after `monkeypatch.setenv`, with no
    re-import in between, is what proves the read happens at CALL time rather than being computed
    once (at import, or memoized on a first call) and handed back unchanged forever after. The
    third call, after `delenv`, closes the loop: a cached value would not go back down."""
    monkeypatch.delenv("STIGMERGY_LIBRARIAN_TIMEOUT_S", raising=False)
    assert config.resolved_visibility_timeout_s() == 900

    monkeypatch.setenv("STIGMERGY_LIBRARIAN_TIMEOUT_S", "600")
    assert config.resolved_visibility_timeout_s() == 1500

    monkeypatch.delenv("STIGMERGY_LIBRARIAN_TIMEOUT_S", raising=False)
    assert config.resolved_visibility_timeout_s() == 900, (
        "went back down once the env var was removed again — a cached/memoized value would not")


def test_resolved_visibility_timeout_s_agrees_with_settings_from_args_exactly(monkeypatch):
    """The anti-drift contract the issue names explicitly: ONE shared resolution function, not a
    second formula that happens to match today and silently diverges the next time somebody tunes
    `VISIBILITY_HEADROOM_S` or `GATE_BUDGET_S` in only one of the two places. Proven at both the
    class default and the staging env var, against a REAL `Settings.from_args` call rather than a
    hand-copied arithmetic expression that could drift from it unnoticed."""
    monkeypatch.delenv("STIGMERGY_LIBRARIAN_TIMEOUT_S", raising=False)
    assert config.resolved_visibility_timeout_s() == \
        config.Settings.from_args(_args()).visibility_timeout_s

    monkeypatch.setenv("STIGMERGY_LIBRARIAN_TIMEOUT_S", "600")
    assert config.resolved_visibility_timeout_s() == \
        config.Settings.from_args(_args()).visibility_timeout_s


def test_cli_flags_for_the_loop_tunables_are_honored(monkeypatch):
    settings = config.Settings.from_args(
        _args(poll_interval=2.5, visibility_timeout=60, max_attempts=5))
    assert settings.poll_interval_s == 2.5
    assert settings.visibility_timeout_s == 60
    assert settings.max_attempts == 5


# ── an explicitly passed value must not be discarded in silence ──────────────────────────────────
# The carry-over defect. `flag` read `getattr(args, name, None) or default`, so every explicit ZERO
# was falsy and silently replaced: `--visibility-timeout 0` resolved to 900 and the run then quoted
# 900 back at the operator who had asked for something else. A flag the operator typed must either
# take effect or be refused out loud — being ignored in silence is the one outcome that teaches them
# the tool lies.
def test_an_explicit_zero_visibility_timeout_survives_resolution(monkeypatch):
    settings = config.Settings.from_args(_args(visibility_timeout=0))
    assert settings.visibility_timeout_s == 0
    assert settings.visibility_timeout_s != config.DEFAULT_VISIBILITY_TIMEOUT_S


def test_absence_is_still_the_default_which_is_the_other_half(monkeypatch):
    """`None` is what "absent" means now, and it must still resolve to the default — otherwise the
    fix would have replaced a silent substitution with a broken default."""
    monkeypatch.delenv("STIGMERGY_LIBRARIAN_TIMEOUT_S", raising=False)
    settings = config.Settings.from_args(_args(visibility_timeout=None))
    assert settings.visibility_timeout_s == config.DEFAULT_VISIBILITY_TIMEOUT_S


def test_the_env_var_is_still_consulted_between_the_flag_and_the_default(monkeypatch):
    """Precedence is unchanged: CLI flag -> env var -> class default. Proven on `repo`, the one
    tunable that has all three sources."""
    monkeypatch.setenv("STIGMERGY_REPO", "/env/repo")
    assert config.Settings.from_args(_args()).repo == "/env/repo"
    assert config.Settings.from_args(_args(repo="/flag/repo")).repo == "/flag/repo"


def test_a_zero_poll_interval_is_refused_rather_than_defaulted_or_accepted():
    """Now that zero survives, it has to be ANSWERED. A zero poll interval claims in a tight loop
    with no pause, which is silent in its damage — so the refusal is the point."""
    with pytest.raises(LibrarianConfigError) as exc_info:
        config.Settings.from_args(_args(poll_interval=0))
    message = str(exc_info.value)
    assert "poll_interval_s is 0" in message and "tight loop" in message
    assert str(config.DEFAULT_POLL_INTERVAL_S) in message


def test_a_negative_poll_interval_is_refused_too():
    with pytest.raises(LibrarianConfigError, match="poll_interval_s is -1"):
        config.Settings.from_args(_args(poll_interval=-1))


def test_zero_max_attempts_is_refused_because_every_delivery_would_start_exhausted():
    """`max_attempts=0` means the sweep fails each row on its first claim, so the queue drains itself
    into `failed` — a configuration that destroys captures rather than processing them."""
    with pytest.raises(LibrarianConfigError) as exc_info:
        config.Settings.from_args(_args(max_attempts=0))
    assert "max_attempts is 0" in str(exc_info.value)


def test_an_ordinary_explicit_value_still_takes_effect(monkeypatch):
    """The benign twin: the domain checks must not have turned "honor the flag" into "refuse the
    flag"."""
    settings = config.Settings.from_args(
        _args(poll_interval=2.5, visibility_timeout=1200, max_attempts=1))
    assert (settings.poll_interval_s, settings.visibility_timeout_s, settings.max_attempts) == \
        (2.5, 1200, 1)


def test_check_domains_is_not_run_by_the_plain_constructor():
    """Deliberate, and the reason it is a method rather than `__post_init__`: the domain check belongs
    to RESOLUTION, and a frozen dataclass that validated in its constructor could not be used to
    build the out-of-domain case a test needs in order to prove the refusal fires."""
    settings = config.Settings(poll_interval_s=0)         # must not raise
    with pytest.raises(LibrarianConfigError):
        settings.check_domains()


def test_settings_never_reads_the_environment_outside_from_args(monkeypatch):
    """Module docstring: "no module reads the environment at import time... `from_args` is the
    single place." Constructing `Settings` directly (bypassing `from_args`) must depend on
    nothing in the environment — proven by setting a value that WOULD change the outcome through
    `from_args` and confirming the plain constructor ignores it entirely."""
    monkeypatch.setenv("STIGMERGY_REPO", "/should/be/ignored")
    settings = config.Settings()
    assert settings.repo == config.REPO_DEFAULT


# ── computed paths every fixture in this suite depends on ──────────────────────────────────────
def test_acl_registry_and_linter_paths_are_derived_from_repo():
    settings = config.Settings(repo="/some/repo")
    assert settings.acl_path == "/some/repo/ops/acl.json"
    assert settings.registry_path == "/some/repo/ops/entity-registry.json"
    assert settings.linter_path == "/some/repo/.claude/tools/stigmergy_lint.py"


# ══════════════════════════════════════════════════════════════════════════════════════════════
# `require_remote_base` — the flag ONLY `bootstrap.worker_env` sets, read here, nowhere else, so
# a laptop (which never sets it) keeps the DEFAULT "local base is fine" behaviour, and the tests
# below cover the default an operator gets as well as every explicit spelling.
# ══════════════════════════════════════════════════════════════════════════════════════════════
def test_require_remote_base_defaults_to_false_with_no_env_var(monkeypatch):
    monkeypatch.delenv(config.REQUIRE_REMOTE_BASE_ENV, raising=False)
    assert config.Settings.from_args(_args()).require_remote_base is False


@pytest.mark.parametrize("value", ["1", "true", "True", "TRUE", "yes", "YES"])
def test_require_remote_base_is_true_for_every_truthy_spelling(monkeypatch, value):
    monkeypatch.setenv(config.REQUIRE_REMOTE_BASE_ENV, value)
    assert config.Settings.from_args(_args()).require_remote_base is True


@pytest.mark.parametrize("value", ["0", "false", "False", "no", "", "  ", "2"])
def test_require_remote_base_is_false_for_everything_else(monkeypatch, value):
    monkeypatch.setenv(config.REQUIRE_REMOTE_BASE_ENV, value)
    assert config.Settings.from_args(_args()).require_remote_base is False


def test_require_remote_base_has_no_cli_flag_it_is_a_fact_about_the_environment(monkeypatch):
    """Module comment: "There is no CLI flag: this is a fact about the environment the process
    runs in, not a preference an operator expresses per invocation." `_args()` (this file's own
    `argparse.Namespace` stand-in) carries no `require_remote_base` attribute at all — proving the
    env var is consulted independently of anything `from_args` reads off `args`."""
    monkeypatch.setenv(config.REQUIRE_REMOTE_BASE_ENV, "1")
    args = _args()
    assert not hasattr(args, "require_remote_base")
    assert config.Settings.from_args(args).require_remote_base is True


# ── a budget this worker cannot use is REFUSED, never propagated ───────────────────────────────
# `check_domains` validates `poll_interval_s` and `max_attempts` and never `timeout_s`, and
# `startup_checks`' lease rule is RELATIVE (`visibility <= minimum(timeout)`) — which a negative
# pair satisfies. So a worker booted happily on `-1000`. Survivable while the number stayed
# inside the worker; not survivable once the admin console derived its Reclaim horizon from the
# same variable, because a negative horizon inverts `make_interval` and sweeps claims that have
# not happened yet.
def test_a_non_positive_agent_budget_is_refused_by_name(monkeypatch):
    monkeypatch.setenv(config.TIMEOUT_ENV, "-1000")
    with pytest.raises(LibrarianConfigError, match=config.TIMEOUT_ENV):
        config.resolved_timeout_s()
    with pytest.raises(LibrarianConfigError):
        config.resolved_visibility_timeout_s()


def test_a_malformed_agent_budget_is_refused_by_name(monkeypatch):
    """`.env.example` ships this line commented out, so uncommenting it without a value yields
    `""` — absence and empty string are different things and only one of them is a default."""
    for raw in ("", "abc", "600.0"):
        monkeypatch.setenv(config.TIMEOUT_ENV, raw)
        with pytest.raises(LibrarianConfigError, match=config.TIMEOUT_ENV):
            config.resolved_timeout_s()


def test_a_positive_budget_is_the_benign_twin(monkeypatch):
    """The refusals above measure sensitivity; this measures specificity — an ordinary tune still
    resolves, and the lease still derives from it."""
    monkeypatch.setenv(config.TIMEOUT_ENV, "600")
    assert config.resolved_timeout_s() == 600
    assert config.resolved_visibility_timeout_s() == 1500


def test_the_lease_derives_from_this_settings_objects_own_budget(monkeypatch):
    """The invariant, stated as one: `from_args` resolves the budget ONCE and threads it into the
    derivation, so the two halves cannot come from two different reads of the environment."""
    monkeypatch.setenv(config.TIMEOUT_ENV, "450")
    settings = config.Settings.from_args(_args())
    assert settings.visibility_timeout_s == config.minimum_visibility_timeout_s(
        timeout_s=settings.timeout_s) + config.VISIBILITY_HEADROOM_S
