"""Runtime configuration for the librarian worker — constructed once at the entry point.

Same ground rule as every other stigmergy package (`server.settings`, `kernel.settings`): frozen
dataclass, every default written once on the class, and **no module reads the environment at
import time** — `from_args` is the single place flags and env fallbacks are resolved. Precedence
is CLI flag → env var → class default, expressed as `args.x or os.environ.get("ENV", cls.x)`,
mirroring `server.settings.Settings.from_args`.

**Everything tunable is here**: model, max turns, max tool calls, wall-clock timeout, poll
interval, visibility timeout. Model IDs are configuration, never constants in the call site —
models get deprecated, and a hardcoded id is a landmine.

**The visibility timeout is DERIVED from this worker's own bounds**, and that is a correction.

It used to be `capture.queue.DEFAULT_VISIBILITY_TIMEOUT_S` verbatim, on the reasoning that
`stigmergy-queue reclaim` and this worker sweep the same column and must not drift — which is the
right doctrine and was the wrong number. 300s was chosen for a HUMAN-scale claim (somebody running
`stigmergy-queue claim` at a terminal) and inherited for an AGENT-scale one: two agent attempts at
300s each, plus two gitleaks runs, two whole-repo linter runs, a commit and a retrying push. The
lease was therefore less than half the worst-case item, so the queue could redeliver a row this
worker was still processing — and because the commit and the push happen before `finish` is
attempted, both workers would file the same capture.

So the number is now computed from the numbers it actually depends on, in one place, and
`worker.startup_checks` REFUSES to run if configuration drops it back below that. The
anti-drift intent is preserved where it belongs: nothing here is a second magic constant, and
`cli.py` says what the number means for this command.
"""
import os
import tempfile
from dataclasses import dataclass

from stigmergy.capture import queue
from stigmergy.librarian.errors import LibrarianConfigError

# A Sonnet-class model is the default for routine filing: the work is structuring prose and
# resolving links against a small graph, not deep synthesis. Overridable per deployment; never
# read anywhere but `from_args`.
#
# **PROVIDER-PREFIXED, and that is a correction the retirement forced rather than a model change.**
# It was the bare `claude-sonnet-5` — the spelling the retired Claude-Code backend handed to its own
# SDK. The one real backend left resolves model strings through pydantic-ai, where a bare name means
# the OpenAI Responses API, so `worker._check_pydantic_backend` refuses one: the bare default would
# have made the SHIPPED default unbootable for every worker that did not override it, which is the
# one value that must never need overriding. Same model, same provider, spelled for the backend that
# reads it — and priced under this id in `pricing.py`, which the same pre-flight requires.
DEFAULT_MODEL = "anthropic:claude-sonnet-5"

# Per-item bounds. They cap ONE runaway run; nothing here caps a day of them.
#
# **`max_turns` is LIVE again (ADR 034), under a new mechanism and the same meaning.** It bounded
# the retired harness's conversational loop; it now bounds the pydantic-ai ordinary run's, as
# `UsageLimits(request_limit=...)` — how many times the model may go round with its tools before
# the worker stops paying for one capture. The number is unchanged at 30 deliberately: it is the
# bound this system already ran a tool-using filing agent under, so an operator who tuned it then
# does not have to re-derive it now, and a milestone that both restored iteration and moved its
# ceiling would have made the golden's two arms incomparable for two reasons at once.
#
# It is deliberately NOT reused by the meeting flow, which makes ONE call and derives its own
# ceiling from `OUTPUT_RETRIES`: borrowing this number there would license thirty full requests for
# a flow that must make one.
#
# **`max_tool_calls` stays DEPRECATED and read by nothing.** It was the tool-call ceiling this
# worker counted in a `PostToolUse` hook because that harness had none; pydantic-ai accumulates
# `RunUsage.tool_calls` itself and bounds the loop that makes them by REQUESTS, so a second
# hand-maintained ceiling would be a second answer to one question — and this repo adds a bound
# when a defect asks for one, not for symmetry. (If one ever does, `UsageLimits` takes a
# `tool_calls_limit` and it is a one-line change rather than a counting harness.) It is kept
# parsed rather than dropped because it is a CONFIGURATION surface — `.env`, `fly.toml`, a
# documented table — and silently ignoring a value an operator set is the failure this module's own
# `from_args` docstring refuses on principle. Removing it is a separate change with its own
# consumer inventory; see the CHANGELOG entry.
#
# **Both are `int()`-parsed, and a malformed one raises a bare `ValueError` out of `from_args`
# rather than a named `LibrarianConfigError`.** Pre-existing, and left alone deliberately:
# `resolved_timeout_s` below shows what a NAMED refusal costs to write, and this is stated here, in
# `.env.example` and in the reference table so nobody reads "parsed" as "refused the way the others
# are".
#
# `timeout_s` is the per-item WALL CLOCK the backend wraps its whole run in — a different bound
# from `max_turns` and not a substitute for it (thirty fast requests can fit inside five minutes,
# and one hanging provider fills it with nothing). The visibility lease below is derived from it.
DEFAULT_MAX_TURNS = 30
DEFAULT_MAX_TOOL_CALLS = 120
DEFAULT_TIMEOUT_S = 300

# The first pass plus exactly one corrective retry. Lives here rather than in `processing`
# because it is a per-item BUDGET, and because the lease below is computed from it — two numbers
# that must agree should not sit in two modules.
MAX_AGENT_ATTEMPTS = 2

# What one item costs BESIDES its agent attempts: two gitleaks runs (material, then diff), two
# whole-repo contract-linter runs, the worktree add/remove, the commit, a push that retries up to
# `gitcmd.PUSH_ATTEMPTS` times with a rebase and a doubling backoff in between (~5s of waiting at the
# current budget, so the push is a rounding error inside this number rather than the bulk of it) —
# and, for a backend that declares `wants_gathered`, up to two GATHERS (ADR 033): one per agent
# pass, each a `corpus.load_pages` walk of the checkout plus one tokenization of every page
# (`gather.load_corpus`) and a second directory walk for the wikilink vocabulary.
#
# **An ITERATING run (ADR 034) parses the corpus at most once MORE per pass**, not once per tool
# call: `pydantic_backend.FilingToolbox` caches the parse for the life of one run, so a model that
# searches twenty times pays one walk, and one that searches never pays none. That bound is the
# whole reason the cache exists.
#
# The gather is the only term here that scales with the SIZE OF THE KNOWLEDGE REPO rather than with
# one capture, which is what makes it worth naming separately: everything else above is bounded by
# the diff. At the corpus this ships against it is well under a second; see
# `minimum_visibility_timeout_s` for where that assumption is recorded and when to re-measure it.
GATE_BUDGET_S = 120

# Headroom on top of the worst case. Being wrong in this direction costs a slower recovery from a
# genuine crash; being wrong in the other direction files a capture twice.
VISIBILITY_HEADROOM_S = 180

# ── the gatherer's two dials (ADR 033) ────────────────────────────────────────────────────────
# What the ordinary flow hands the model BEFORE it looks for itself (`librarian/gather.py`). Both
# trade prompt cost against recall, which is why they are configuration and not constants: the
# number that is right for a 40-page brain is not the number that is right for a 4,000-page one,
# and finding out is a measurement somebody runs against a deployment rather than a value this file
# can know.
#
# Twelve candidates at twenty lines each is roughly a page's worth of excerpt — enough for the
# overlap-versus-duplicate judgment the brief asks for, and small enough that the gathered context
# stays a fraction of the captured material it sits beside. Read by `processing._one_pass` for a
# backend that declares `wants_gathered`, and by the `search_pages` tool for its own result size
# (ADR 034) — one pair of dials for both, so the pages a model finds by searching are shown to it
# exactly as the pages it was handed are. The offline double consults neither: it writes its own
# page from a directive.
DEFAULT_GATHER_TOP_K = 12
DEFAULT_GATHER_EXCERPT_LINES = 20

DEFAULT_POLL_INTERVAL_S = 3.0

# The retry-collapse window (`dedup`'s level 1): identical content from the same submitter
# inside this many seconds is a RETRY of one capture, not a second one. Ten minutes is generous
# enough to cover a person re-running a failed-looking submit and short enough that deliberately
# re-filing the same material tomorrow is still a new capture.
DEFAULT_DEDUP_WINDOW_S = 600

REPO_ENV = "STIGMERGY_REPO"
REPO_DEFAULT = "../stigmergy-brain"

# Where the deployed worker clones the knowledge repo FROM. Absent on a laptop, where `repo` is
# already a checkout a human maintains; set on the container, where there is no checkout until
# `librarian.bootstrap` makes one. Slash-separated and git-shaped (`https://github.com/<slug>.git`
# on staging, `git://git-remote:9418/stigmergy.git` in the composition).
REPO_URL_ENV = "STIGMERGY_LIBRARIAN_REPO_URL"

# "This process is the DEPLOYED worker, so a base that did not come from the remote is a fault."
# Exported by `bootstrap.worker_env` — the one place in the system that knows the process is
# containerized — and read here, so the knowledge stays where it is true instead of being guessed
# from the presence of some other variable.
#
# It exists because `bootstrap.verify_checkout_at_base` refuses a non-remote base at STARTUP and
# `gitcmd.base_ref` answers a failed fetch with a warning and the local branch on EVERY item after
# that. A token that expires after boot therefore turns the deployed worker into exactly what the
# startup check exists to refuse. `processing.process_item` asks this question per item; a laptop,
# where a local base is the correct answer, never sets it — a guard must not refuse the machine
# it was written for.
REQUIRE_REMOTE_BASE_ENV = "STIGMERGY_LIBRARIAN_REQUIRE_REMOTE_BASE"
TIMEOUT_ENV = "STIGMERGY_LIBRARIAN_TIMEOUT_S"

# What counts as "yes" in an environment variable. One spelling for the one boolean this package
# reads.
_TRUTHY = ("1", "true", "yes")

# The three repo-sourced inputs, as paths RELATIVE TO THE REPO ROOT — which is the form git reads
# take (`<commit>:<relpath>`) and the form a checkout path is built from. One spelling each, so
# `base_inputs`' reads at `base.sha` and `Settings`' checkout paths below can never disagree about
# where a file lives. Slash-separated on purpose: these are git paths first.
ACL_RELPATH = "ops/acl.json"
REGISTRY_RELPATH = "ops/entity-registry.json"
LINTER_RELPATH = ".claude/tools/stigmergy_lint.py"
# The doorbell's scope->steward-emails map, same posture as `identities.json` — read at the base
# commit like the three inputs above (`base_inputs.load_stewards`), never the working tree.
STEWARDS_RELPATH = "ops/stewards.json"


def _in_repo(repo: str, relpath: str) -> str:
    """A repo-relative git path as a filesystem path inside `repo`."""
    return os.path.join(repo, *relpath.split("/"))


# Where a refused diff is preserved for diagnosis (`processing.preserve_refused_diff`). Under the
# system temp dir by default and deliberately NOT under `gitcmd.WORKTREE_PREFIX`: startup reaping
# deletes worktree directories under that root, so a diagnostics directory named like a worktree
# would be swept away by the next run that needed to read it.
#
# That is now belt AND braces rather than the only thing standing between the reap and an arbitrary
# temp directory. `gitcmd.reapable` matches the FULL worktree name shape — prefix, this repo's key,
# a creating pid, a uuid — so an unrelated directory that merely starts with the prefix is no longer
# swept either. The name still stays out of the way, because a rule that depends on one directory
# being named differently from another is a rule one rename breaks.
REFUSED_DIFF_DIRNAME = "stigmergy-refused-diffs"
REFUSED_DIFF_ROOT_ENV = "STIGMERGY_LIBRARIAN_REFUSED_DIFF_DIR"


def refused_diff_dir(root: str = "") -> str:
    """The directory refused diffs are written to. `""` resolves to the system temp dir, resolved
    at use time rather than at import (no module here reads the environment at import time)."""
    return root or os.path.join(tempfile.gettempdir(), REFUSED_DIFF_DIRNAME)


def minimum_visibility_timeout_s(*, timeout_s: int = DEFAULT_TIMEOUT_S) -> int:
    """The smallest lease that outlives one item's worst case. `worker.startup_checks` refuses
    anything at or below this, and the default below is this plus headroom — one arithmetic, read
    by the default, by the refusal and by the test that asserts the relationship.

    **The GATHER is assumed to fit inside `VISIBILITY_HEADROOM_S` rather than being a term here**,
    and that is an assumption with a scale attached to it (ADR 033). It is the one per-item cost
    that grows with the SIZE OF THE KNOWLEDGE REPO — two walks of the checkout and one tokenization
    of every page, per agent pass — where every other term above is bounded by one capture. At a
    few hundred pages it is well under a second against a 180s headroom, so folding it into
    `GATE_BUDGET_S` would be precision nobody has.
    **Re-measure it at roughly 5,000 pages**, or sooner if a `stigmergy-librarian status` p95 starts
    tracking corpus growth rather than model latency: past that, the honest fix is a term in
    `GATE_BUDGET_S` computed from the corpus, not a bigger headroom. A lease that is too short
    files a capture twice, which is why this assumption is written down rather than left implicit.
    """
    return MAX_AGENT_ATTEMPTS * int(timeout_s) + GATE_BUDGET_S


DEFAULT_VISIBILITY_TIMEOUT_S = minimum_visibility_timeout_s() + VISIBILITY_HEADROOM_S


def resolved_timeout_s() -> int:
    """The per-item agent budget THIS environment resolves — `$STIGMERGY_LIBRARIAN_TIMEOUT_S` or the
    class default, read at call time. The ONE place that reads this variable, so the budget and the
    lease derived from it cannot be resolved two different ways.

    **It refuses a value it cannot use, rather than propagating it.** `check_domains` validates
    `poll_interval_s` and `max_attempts` and never this one, and `startup_checks`' lease rule is
    RELATIVE (`visibility <= minimum(timeout)`), which a negative pair satisfies — so a worker
    booted happily on `-1000`. That was survivable while the number stayed inside the worker; it
    stopped being survivable when the admin console started deriving its Reclaim horizon from the
    same variable, because a negative horizon inverts `make_interval` and sweeps rows claimed in
    the FUTURE. Malformed is refused for the same reason: `.env.example` ships the line commented
    out, so uncommenting it without a value yields `""`, not absence."""
    raw = os.environ.get(TIMEOUT_ENV)
    if raw is None:
        return DEFAULT_TIMEOUT_S
    try:
        value = int(raw)
    except ValueError:
        raise LibrarianConfigError(
            f"${TIMEOUT_ENV} must be a whole number of seconds, not {raw!r} — the per-item agent "
            f"budget, and what this worker's lease is derived from") from None
    if value <= 0:
        raise LibrarianConfigError(
            f"${TIMEOUT_ENV} must be positive, not {value} — it is a per-item wall clock, and the "
            f"visibility lease derived from it would sweep claims that have not happened yet")
    return value


def resolved_visibility_timeout_s(*, timeout_s: int | None = None) -> int:
    """The lease the worker in THIS environment actually holds: the same derivation
    `Settings.from_args` performs, resolved from `$STIGMERGY_LIBRARIAN_TIMEOUT_S` at CALL time —
    never cached, never read at import (the module's standing rule). Exists so a reader that is
    not the worker — the admin console's lease meter and its Reclaim default — can state the
    deployed worker's real number instead of the class default: `fly.toml`'s `[env]` is app-wide,
    so the env this reads IS the env the worker resolved (the meter once read 900s
    while the deployed worker held 1500s)."""
    budget = resolved_timeout_s() if timeout_s is None else timeout_s
    return minimum_visibility_timeout_s(timeout_s=budget) + VISIBILITY_HEADROOM_S


@dataclass(frozen=True)
class Settings:
    """The librarian's runtime configuration. Pure data — no I/O, no clock, no environment."""

    repo: str = REPO_DEFAULT            # the knowledge-repo checkout the worktrees branch from
    branch: str = "main"                # the branch the fast lane commits to, directly
    dsn: str | None = None              # Postgres DSN (None -> store.dsn())
    # Deployed only, set by `bootstrap.worker_env`: refuse an item whose base did not come from the
    # remote, rather than filing it against this container's own stale clone. Default False so a
    # laptop — where an offline run against the local branch is the intended behaviour — is
    # unchanged. There is no CLI flag: this is a fact about the environment the process runs in,
    # not a preference an operator expresses per invocation.
    require_remote_base: bool = False

    # the agent
    # 'pydantic' | 'double' — CI and the suite stay on the double. Both serve BOTH flows and both
    # write their own ordinary page through the same confinement rule; `pydantic` reaches that page
    # by iterating over the checkout with five tools from a gathered seed (ADR 034), the double by
    # following a directive with no model at all. `agent.BACKENDS` is the tuple, and
    # `agent.ensure_known_backend` is what refuses anything else.
    backend: str = "double"
    model: str = DEFAULT_MODEL
    max_turns: int = DEFAULT_MAX_TURNS          # the ordinary run's request ceiling; see above
    max_tool_calls: int = DEFAULT_MAX_TOOL_CALLS  # DEPRECATED — no backend reads it; see above
    timeout_s: int = DEFAULT_TIMEOUT_S

    # the gatherer (a backend that declares `wants_gathered` — and its `search_pages` tool)
    gather_top_k: int = DEFAULT_GATHER_TOP_K
    gather_excerpt_lines: int = DEFAULT_GATHER_EXCERPT_LINES

    # the loop
    poll_interval_s: float = DEFAULT_POLL_INTERVAL_S
    visibility_timeout_s: int = DEFAULT_VISIBILITY_TIMEOUT_S
    max_attempts: int = queue.DEFAULT_MAX_ATTEMPTS
    dedup_window_s: int = DEFAULT_DEDUP_WINDOW_S

    # the gates
    gitleaks_bin: str = "gitleaks"      # resolved on PATH; existence checked ONCE at startup
    worktree_root: str = ""             # "" -> a per-run temp dir under the system temp
    refused_diff_root: str = ""         # "" -> <tempdir>/stigmergy-refused-diffs

    @classmethod
    def from_args(cls, args) -> "Settings":
        """Build settings from parsed CLI args with env fallbacks. The ONLY place this package
        reads `os.environ`.

        **Absence is `None`, never falsiness** — and that is a correction. `flag` used to read
        `getattr(args, name, None) or default`, which silently discarded every explicitly passed
        ZERO: `--visibility-timeout 0` resolved to the 900s default and the run then reported the
        default in its own interrupt message, so an operator who asked for something specific was
        told a different number and had no way to see that their flag had been dropped. A flag the
        operator typed must either take effect or be refused out loud; being ignored in silence is
        the one outcome that teaches them the tool lies. `0` now reaches
        `worker.startup_checks`, which refuses it loudly with the arithmetic.

        The argparse defaults are `None` for exactly this reason (`cli.build_parser`): the parser
        no longer pre-fills the class default, so "absent" and "explicitly zero" are distinct here
        rather than indistinguishable.
        """
        def flag(name, default):
            value = getattr(args, name, None)
            return default if value is None else value

        # The visibility timeout DERIVES from the RESOLVED agent timeout, not from the class
        # default. The module docstring says "derived from this worker's own bounds", and the
        # static `DEFAULT_VISIBILITY_TIMEOUT_S` breaks that the moment `timeout_s` is raised
        # through its env var: the deployed worker (`stigmergy-librarian-boot`, no CLI flags) would
        # then REFUSE to boot on its own startup arithmetic with no way to pass the matching
        # visibility. A figure-dense document that needs two 300s agent timeouts is enough to trip
        # it — the budget is tuned in fly.toml, and this line is what makes that tune bootable. An
        # explicit `--visibility-timeout` still wins, and the default case is unchanged.
        #
        # Both halves are module functions rather than inline arithmetic because a SECOND reader
        # exists: the admin console states this same lease on its meter and its Reclaim default,
        # and it read the class default instead until this was derived — one derivation, two callers.
        # ONE resolution, threaded into the derivation — not two independent env reads. The lease
        # must be derived from THIS settings object's own budget; re-reading the environment for
        # the second half would make that a coincidence rather than an invariant.
        agent_timeout_s = resolved_timeout_s()
        derived_visibility_s = resolved_visibility_timeout_s(timeout_s=agent_timeout_s)

        settings = cls(
            repo=flag("repo", os.environ.get(REPO_ENV) or cls.repo),
            branch=flag("branch", os.environ.get("STIGMERGY_LIBRARIAN_BRANCH", cls.branch)),
            dsn=flag("dsn", None),
            require_remote_base=os.environ.get(REQUIRE_REMOTE_BASE_ENV, "").strip().lower()
            in _TRUTHY,
            backend=str(flag("backend",
                             os.environ.get("STIGMERGY_LIBRARIAN_BACKEND", cls.backend))).lower(),
            # STRIPPED, once, here. A model id is compared, prefix-parsed and priced by three
            # different checks that each strip on their own, and then handed to a framework that
            # does not: `STIGMERGY_LIBRARIAN_MODEL=openai:gpt-5.6-terra ` (a trailing space out of
            # an env file, or a newline out of a shell export) passes every pre-flight and fails
            # inside the provider client. Normalizing at the ONE place this package reads the
            # environment is what makes the pre-flights and the run agree about the same string.
            model=os.environ.get("STIGMERGY_LIBRARIAN_MODEL", cls.model).strip(),
            max_turns=int(os.environ.get("STIGMERGY_LIBRARIAN_MAX_TURNS", cls.max_turns)),
            max_tool_calls=int(os.environ.get("STIGMERGY_LIBRARIAN_MAX_TOOL_CALLS",
                                              cls.max_tool_calls)),
            timeout_s=agent_timeout_s,
            gather_top_k=int(os.environ.get("STIGMERGY_LIBRARIAN_GATHER_TOP_K",
                                            cls.gather_top_k)),
            gather_excerpt_lines=int(os.environ.get("STIGMERGY_LIBRARIAN_GATHER_EXCERPT_LINES",
                                                    cls.gather_excerpt_lines)),
            poll_interval_s=float(flag("poll_interval", cls.poll_interval_s)),
            visibility_timeout_s=int(flag("visibility_timeout", derived_visibility_s)),
            max_attempts=int(flag("max_attempts", cls.max_attempts)),
            dedup_window_s=int(os.environ.get("STIGMERGY_LIBRARIAN_DEDUP_WINDOW_S",
                                              cls.dedup_window_s)),
            gitleaks_bin=os.environ.get("STIGMERGY_GITLEAKS_BIN", cls.gitleaks_bin),
            worktree_root=os.environ.get("STIGMERGY_LIBRARIAN_WORKTREE_ROOT", cls.worktree_root),
            refused_diff_root=os.environ.get(REFUSED_DIFF_ROOT_ENV, cls.refused_diff_root),
        )
        settings.check_domains()
        return settings

    def check_domains(self) -> None:
        """Refuse values that are inside the type and outside the meaning.

        The counterpart to the `is None` fix above: now that an explicit `0` survives resolution, it
        has to be *answered*, and two of these numbers have no sane zero. A `--poll-interval 0` is a
        busy loop hammering Postgres, and a `--max-attempts 0` means every delivery is already
        exhausted, so the sweep fails each item on its first claim and the queue drains itself into
        `failed`. Neither is a plausible request and both are silent in their damage.

        Deliberately NOT `__post_init__`: the domain check belongs to the RESOLUTION step, and a
        frozen dataclass that validates in its constructor cannot be used to build the deliberately
        out-of-domain case a test needs in order to prove the refusal fires. The lease-versus-item
        arithmetic stays in `worker.startup_checks` — it is a relationship between this
        configuration and one item's worst case, not a property of a single field.
        """
        if float(self.poll_interval_s) <= 0:
            raise LibrarianConfigError(
                f"poll_interval_s is {self.poll_interval_s}, which would poll the queue in a tight "
                f"loop with no pause; pass a positive --poll-interval (default "
                f"{DEFAULT_POLL_INTERVAL_S})")
        if int(self.max_attempts) < 1:
            raise LibrarianConfigError(
                f"max_attempts is {self.max_attempts}, so every delivery would already be "
                f"exhausted and the queue would fail each item on its first claim; pass "
                f"--max-attempts 1 or more (default {queue.DEFAULT_MAX_ATTEMPTS})")

    # ── the three repo-sourced inputs: where they live IN A CHECKOUT ──────────────────────────
    # These are locations, not reads. The fast lane opens none of them: it reads all three at
    # `base.sha` through `librarian.base_inputs`, which is where that argument is written down.
    #
    # What these properties used to be was an ASYMMETRY — all three resolved against the local
    # working tree of `self.repo` while the diff they judge comes from a worktree built at `base`,
    # so an uncommitted local edit changed a run's behaviour without changing the commit being
    # filed against. The registry is the output of a governed steward flow, and a working-tree
    # read is a read AROUND that gate: an uncommitted edit could anchor captures to an entity
    # nobody approved.
    #
    # The properties stay, because "where does this file live in a checkout" is still a real
    # question with real callers: the steward tooling edits `ops/entity-registry.json` in its own
    # clone, and an operator-facing message about a missing file is about a path a human can open.
    # They are derived from the RELPATHs the base reads use, so the two answers cannot drift.
    @property
    def acl_path(self) -> str:
        return _in_repo(self.repo, ACL_RELPATH)

    @property
    def registry_path(self) -> str:
        return _in_repo(self.repo, REGISTRY_RELPATH)

    @property
    def linter_path(self) -> str:
        return _in_repo(self.repo, LINTER_RELPATH)
