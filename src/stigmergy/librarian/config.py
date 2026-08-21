"""Runtime configuration for the librarian worker — constructed once at the entry point.

Frozen dataclass, every default written once on the class, and **no module reads the environment
at import time**: `from_args` is the single place flags and env fallbacks are resolved,
precedence CLI flag -> env var -> class default. The visibility timeout is DERIVED from this
worker's per-item bounds, and a lease shorter than one item's worst case lets the queue redeliver
a row mid-commit, filing the same capture twice.
"""
import os
import tempfile
from dataclasses import dataclass

from stigmergy.capture import queue
from stigmergy.librarian.errors import LibrarianConfigError

# The default filing model. PROVIDER-PREFIXED (pydantic-ai reads a bare name as an OpenAI model)
# and priced under this exact spelling in `pricing.py`.
DEFAULT_MODEL = "anthropic:claude-sonnet-5"

# Anthropic prompt-cache TTL for the ORDINARY run: `'off' | '5m' | '1h'`. No effect on a
# non-Anthropic model, nor on the one-call meeting flow, which would pay a WRITE for no read.
DEFAULT_PROMPT_CACHE = "5m"

# Per-item bounds. They cap ONE runaway run; nothing here caps a day of them.
# `max_turns` — the ordinary run's request ceiling; the meeting flow makes one call and does not
# read it. `max_tool_calls` — DEPRECATED, read by nothing, still parsed because silently ignoring
# a value an operator set is refused on principle. `timeout_s` — the per-item WALL CLOCK, not a
# substitute for `max_turns`, and what the visibility lease below is derived from.
DEFAULT_MAX_TURNS = 30
DEFAULT_MAX_TOOL_CALLS = 120
DEFAULT_TIMEOUT_S = 300

# The first pass plus exactly one corrective retry. Here and not in `processing` because the
# lease below is computed from it, and two numbers that must agree belong in one module.
MAX_AGENT_ATTEMPTS = 2

# What one item costs BESIDES its agent attempts: the gitleaks and linter runs, the worktree,
# the commit, the push retries, and up to two gathers for a `wants_gathered` backend.
GATE_BUDGET_S = 120

# Headroom on top of the worst case. Being wrong in this direction costs a slower recovery from
# a genuine crash; being wrong in the other direction files a capture twice.
VISIBILITY_HEADROOM_S = 180

# ── the gatherer's two dials ──────────────────────────────────────────────────────────────────
# What the ordinary flow hands the model before it looks for itself; both trade prompt cost
# against recall. Read by both the gather and the `search_pages` tool, so searched pages are
# shown exactly as handed ones are.
DEFAULT_GATHER_TOP_K = 12
DEFAULT_GATHER_EXCERPT_LINES = 20

DEFAULT_POLL_INTERVAL_S = 3.0

# ── the periodic view sweep ───────────────────────────────────────────────────────────────────
# The worker's one maintenance pass: it converges `views/` to the corpus from STATE, so a view is
# never stale whatever door wrote the page. It runs on the idle branch (the queue is empty) but on
# its OWN clock — an empty queue polls every few seconds, and the pass costs a corpus parse.
VIEW_SWEEP_INTERVAL_ENV = "STIGMERGY_LIBRARIAN_VIEW_SWEEP_INTERVAL_S"
DEFAULT_VIEW_SWEEP_INTERVAL_S = 900.0
# `0` is a real setting, not a broken one: it turns the pass off, leaving the post-meeting hook and
# `stigmergy-views regenerate` as the only roads — what this deployment had before the pass existed.
VIEW_SWEEP_OFF = 0.0

# How many entities ONE pass may regenerate or remove. #69's lesson, applied to a second unattended
# loop: N changed entities are N model calls, and until this existed nothing bounded them. The
# surplus is not lost — the pass recomputes its population from state, so whatever it defers is
# still divergent when the next one starts.
VIEW_SWEEP_CEILING_ENV = "STIGMERGY_LIBRARIAN_VIEW_SWEEP_CEILING"
DEFAULT_VIEW_SWEEP_CEILING = 10

# The retry-collapse window (`dedup`'s level 1): identical content from the same submitter
# inside this many seconds is a RETRY of one capture, not a second one.
DEFAULT_DEDUP_WINDOW_S = 600

REPO_ENV = "STIGMERGY_REPO"
REPO_DEFAULT = "../stigmergy-brain"

# Where the deployed worker clones the knowledge repo FROM: absent on a laptop, which already
# has a checkout; set on the container, where `librarian.bootstrap` makes one.
REPO_URL_ENV = "STIGMERGY_LIBRARIAN_REPO_URL"

# "This process is the DEPLOYED worker, so a base that did not come from the remote is a fault."
# Enforced per item, not only at startup: `gitcmd.base_ref` answers a failed fetch with the local
# branch, so a token expiring after boot would otherwise go unnoticed.
REQUIRE_REMOTE_BASE_ENV = "STIGMERGY_LIBRARIAN_REQUIRE_REMOTE_BASE"
TIMEOUT_ENV = "STIGMERGY_LIBRARIAN_TIMEOUT_S"
PROMPT_CACHE_ENV = "STIGMERGY_LIBRARIAN_PROMPT_CACHE"
# The TTLs Anthropic's cache fields accept; `pydantic_backend` reads these, not a copy.
PROMPT_CACHE_TTLS = ("5m", "1h")
# The only three spellings `resolved_prompt_cache` accepts, in the order its refusal lists them.
PROMPT_CACHE_VALUES = ("off", *PROMPT_CACHE_TTLS)

# What counts as "yes" in an environment variable.
_TRUTHY = ("1", "true", "yes")

# The three repo-sourced inputs, RELATIVE TO THE REPO ROOT — one spelling each, so the reads at
# `base.sha` and the checkout paths below can never disagree.
ACL_RELPATH = "ops/acl.json"
REGISTRY_RELPATH = "ops/entity-registry.json"
LINTER_RELPATH = ".claude/tools/stigmergy_lint.py"
# The doorbell's scope->steward-emails map, read at the base commit like the three above.
STEWARDS_RELPATH = "ops/stewards.json"


def _in_repo(repo: str, relpath: str) -> str:
    """A repo-relative git path as a filesystem path inside `repo`."""
    return os.path.join(repo, *relpath.split("/"))


# ── which checkout a `--repo` means, and whether it is one ─────────────────────────────────────
# Every operator CLI that takes a `--repo` resolves it the same way and against the same
# constants — one precedence and one opinion about what a checkout is, instead of a copy per CLI.
# TWO copies remain in this package, both deliberate and neither an operator CLI: `bootstrap.main`
# resolves the same precedence inline because it runs BEFORE a checkout exists (it is the thing
# that makes one, so the predicate below has nothing to be true about yet), and `Settings.from_args`
# resolves it as one field among many for the worker, which is handed a path it does not have to
# absolutize — `gitcmd` runs every command with `cwd=repo`. Making `Settings` absolutize is a
# behaviour change for anyone passing a relative `--repo`, so it is a separate change, not this one.
def repo_path(explicit: str = "") -> str:
    """WHICH knowledge-repo checkout a command was pointed at, as an absolute path: an explicit
    `--repo`, else `$STIGMERGY_REPO`, else the default. Answers WHERE only — a command that has to
    WRITE to the checkout pairs this with `is_repo_checkout` below and writes its own refusal when
    the predicate is false (each CLI owns its sentence; only the judgement is shared)."""
    return os.path.abspath(explicit or os.environ.get(REPO_ENV) or REPO_DEFAULT)


def is_repo_checkout(path: str) -> bool:
    """The ONE predicate for "is this a git checkout" — what a command that commits to the repo
    asks before it writes anything.

    `.git` is a DIRECTORY in an ordinary clone but a FILE (a `gitdir:` pointer) in a
    `git worktree add` checkout, so `isdir` alone is the bug: it refuses a genuine worktree. That
    was a real disagreement, not a hypothetical — `stigmergy-entities` refused a worktree
    `stigmergy-views` accepted, for the same directory.

    Bare `exists` is the OTHER bug: it accepts a stray file named `.git` — a leftover, a note, a
    binary — and the caller then commits with the steward's own identity into a directory git does
    not manage. So the FILE case has to look like what git writes: a `gitdir:` pointer. Unreadable
    or binary answers `False`, never raises.

    A PREDICATE, deliberately, rather than a resolver that raises: a caller in `entities` may not
    interpolate a foreign exception's text into its refusal (`tests/test_architecture.py`'s
    `test_an_entities_refusal_never_splices_a_caught_exceptions_text`, because `server.review`
    echoes those refusals to a steward verbatim). Each CLI therefore writes its own sentence
    around the path it already holds, and only the JUDGEMENT is shared.
    """
    dot_git = os.path.join(path, ".git")
    if os.path.isdir(dot_git):
        return True
    try:
        with open(dot_git, "rb") as f:
            head = f.read(64)
    except OSError:
        return False
    return head.startswith(b"gitdir:")


# Where a refused diff is preserved for diagnosis. Deliberately NOT under
# `gitcmd.WORKTREE_PREFIX`: startup reaping would sweep it before anyone read it.
REFUSED_DIFF_DIRNAME = "stigmergy-refused-diffs"
REFUSED_DIFF_ROOT_ENV = "STIGMERGY_LIBRARIAN_REFUSED_DIFF_DIR"


def refused_diff_dir(root: str = "") -> str:
    """The directory refused diffs are written to. `""` resolves to the system temp dir at use
    time, never at import."""
    return root or os.path.join(tempfile.gettempdir(), REFUSED_DIFF_DIRNAME)


def minimum_visibility_timeout_s(*, timeout_s: int = DEFAULT_TIMEOUT_S) -> int:
    """The smallest lease that outlives one item's worst case. `worker.startup_checks` refuses
    anything at or below this; the default is this plus headroom — one arithmetic, three readers.

    The GATHER is assumed to fit inside `VISIBILITY_HEADROOM_S` rather than being a term here: it
    is the one per-item cost that grows with the size of the knowledge repo. Re-measure at roughly
    5,000 pages, and past that add a corpus-derived term to `GATE_BUDGET_S`. Nothing converts
    before the first agent pass any more: a document arrives as text (ADR 044 D4).
    """
    return MAX_AGENT_ATTEMPTS * int(timeout_s) + GATE_BUDGET_S


DEFAULT_VISIBILITY_TIMEOUT_S = minimum_visibility_timeout_s() + VISIBILITY_HEADROOM_S


def resolved_timeout_s() -> int:
    """The per-item agent budget THIS environment resolves, read at call time — the ONE place
    that reads this variable, so the budget and the lease derived from it cannot disagree.

    An unusable value is refused, never propagated: the admin console derives its Reclaim horizon
    from the same variable, and a negative one inverts `make_interval`."""
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
    """The lease the worker in THIS environment actually holds: `Settings.from_args`' own
    derivation, resolved at CALL time, so a reader that is not the worker (the admin console's
    lease meter and Reclaim default) states the deployed number, not the class default."""
    budget = resolved_timeout_s() if timeout_s is None else timeout_s
    return minimum_visibility_timeout_s(timeout_s=budget) + VISIBILITY_HEADROOM_S


def resolved_prompt_cache() -> str:
    """`$STIGMERGY_LIBRARIAN_PROMPT_CACHE` or the class default, read at call time — the ONE
    place this variable is consulted. An unrecognized spelling fails the boot rather than
    reaching the backend as an off switch nobody chose."""
    raw = os.environ.get(PROMPT_CACHE_ENV)
    if raw is None:
        return DEFAULT_PROMPT_CACHE
    value = raw.strip()
    if value not in PROMPT_CACHE_VALUES:
        raise LibrarianConfigError(
            f"${PROMPT_CACHE_ENV} must be one of {', '.join(PROMPT_CACHE_VALUES)}, not {raw!r} — "
            f"the ordinary filing run's Anthropic prompt-cache TTL ('off' is the escape hatch)")
    return value


@dataclass(frozen=True)
class Settings:
    """The librarian's runtime configuration. Pure data — no I/O, no clock, no environment."""

    repo: str = REPO_DEFAULT            # the knowledge-repo checkout the worktrees branch from
    branch: str = "main"                # the branch the fast lane commits to, directly
    dsn: str | None = None              # Postgres DSN (None -> store.dsn())
    # Deployed only: refuse an item whose base did not come from the remote. Default False so a
    # laptop's offline run is unchanged, and no CLI flag — a fact about the environment.
    require_remote_base: bool = False

    # the agent
    # 'pydantic' | 'double' — CI and the suite stay on the double; both serve BOTH flows.
    backend: str = "double"
    model: str = DEFAULT_MODEL
    max_turns: int = DEFAULT_MAX_TURNS          # the ordinary run's request ceiling; see above
    max_tool_calls: int = DEFAULT_MAX_TOOL_CALLS  # DEPRECATED — no backend reads it; see above
    timeout_s: int = DEFAULT_TIMEOUT_S
    prompt_cache: str = DEFAULT_PROMPT_CACHE    # 'off' | '5m' | '1h' — the ordinary run's cache TTL

    # the gatherer (a backend that declares `wants_gathered` — and its `search_pages` tool)
    gather_top_k: int = DEFAULT_GATHER_TOP_K
    gather_excerpt_lines: int = DEFAULT_GATHER_EXCERPT_LINES

    # the loop
    poll_interval_s: float = DEFAULT_POLL_INTERVAL_S
    visibility_timeout_s: int = DEFAULT_VISIBILITY_TIMEOUT_S
    max_attempts: int = queue.DEFAULT_MAX_ATTEMPTS
    dedup_window_s: int = DEFAULT_DEDUP_WINDOW_S

    # the periodic view sweep (see the constants above)
    view_sweep_interval_s: float = DEFAULT_VIEW_SWEEP_INTERVAL_S
    view_sweep_ceiling: int = DEFAULT_VIEW_SWEEP_CEILING

    # the gates
    gitleaks_bin: str = "gitleaks"      # resolved on PATH; existence checked ONCE at startup
    worktree_root: str = ""             # "" -> a per-run temp dir under the system temp
    refused_diff_root: str = ""         # "" -> <tempdir>/stigmergy-refused-diffs

    @classmethod
    def from_args(cls, args) -> "Settings":
        """Build settings from parsed CLI args with env fallbacks. The ONLY place this package
        reads `os.environ`.

        **Absence is `None`, never falsiness**: an `or default` resolution silently discards an
        explicit zero, so "absent" and "explicitly zero" stay distinct and a `0` reaches the
        refusals that name it.
        """
        def flag(name, default):
            value = getattr(args, name, None)
            return default if value is None else value

        # DERIVES from the RESOLVED agent timeout, not the class default, so a budget raised
        # through its env var stays bootable with no CLI flags; `--visibility-timeout` still wins.
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
            # STRIPPED, once, here: a trailing space out of an env file must not pass three
            # pre-flights that each strip on their own and then fail inside the provider client.
            model=os.environ.get("STIGMERGY_LIBRARIAN_MODEL", cls.model).strip(),
            max_turns=int(os.environ.get("STIGMERGY_LIBRARIAN_MAX_TURNS", cls.max_turns)),
            max_tool_calls=int(os.environ.get("STIGMERGY_LIBRARIAN_MAX_TOOL_CALLS",
                                              cls.max_tool_calls)),
            timeout_s=agent_timeout_s,
            prompt_cache=resolved_prompt_cache(),
            gather_top_k=int(os.environ.get("STIGMERGY_LIBRARIAN_GATHER_TOP_K",
                                            cls.gather_top_k)),
            gather_excerpt_lines=int(os.environ.get("STIGMERGY_LIBRARIAN_GATHER_EXCERPT_LINES",
                                                    cls.gather_excerpt_lines)),
            poll_interval_s=float(flag("poll_interval", cls.poll_interval_s)),
            visibility_timeout_s=int(flag("visibility_timeout", derived_visibility_s)),
            max_attempts=int(flag("max_attempts", cls.max_attempts)),
            dedup_window_s=int(os.environ.get("STIGMERGY_LIBRARIAN_DEDUP_WINDOW_S",
                                              cls.dedup_window_s)),
            view_sweep_interval_s=float(os.environ.get(VIEW_SWEEP_INTERVAL_ENV,
                                                       cls.view_sweep_interval_s)),
            view_sweep_ceiling=int(os.environ.get(VIEW_SWEEP_CEILING_ENV,
                                                  cls.view_sweep_ceiling)),
            gitleaks_bin=os.environ.get("STIGMERGY_GITLEAKS_BIN", cls.gitleaks_bin),
            worktree_root=os.environ.get("STIGMERGY_LIBRARIAN_WORKTREE_ROOT", cls.worktree_root),
            refused_diff_root=os.environ.get(REFUSED_DIFF_ROOT_ENV, cls.refused_diff_root),
        )
        settings.check_domains()
        return settings

    def check_domains(self) -> None:
        """Refuse values inside the type and outside the meaning: `--poll-interval 0` is a busy
        loop hammering Postgres, and `--max-attempts 0` drains the queue into `failed` on first
        claim. Both are silent in their damage.

        Deliberately NOT `__post_init__`: a frozen dataclass validating in its constructor cannot
        build the out-of-domain case a test needs to prove the refusal fires. The lease-versus-item
        arithmetic stays in `worker.startup_checks`, being a relation and not a field's domain.
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
        # A NEGATIVE interval is the out-of-domain one, not zero: zero is the documented off
        # switch, while a negative value would make every idle tick "due" and turn a corpus parse
        # plus a fresh worktree into the poll loop.
        if float(self.view_sweep_interval_s) < VIEW_SWEEP_OFF:
            raise LibrarianConfigError(
                f"view_sweep_interval_s is {self.view_sweep_interval_s}, which would run the "
                f"periodic view sweep on every idle poll — a corpus parse and a fresh worktree "
                f"every few seconds. Set ${VIEW_SWEEP_INTERVAL_ENV} to a positive number of "
                f"seconds (default {DEFAULT_VIEW_SWEEP_INTERVAL_S}), or to "
                f"{VIEW_SWEEP_OFF:g} to turn the sweep off")
        # The damage the branch above describes is reachable from INSIDE its domain: the pass is
        # due at most once per idle poll, so any interval below the poll interval means every idle
        # tick, which is the "corpus parse and a fresh worktree every few seconds" already named
        # there. `0.9` for `900` is the typo that gets there, and it is not a fault the value's
        # sign can catch.
        if VIEW_SWEEP_OFF < float(self.view_sweep_interval_s) < float(self.poll_interval_s):
            raise LibrarianConfigError(
                f"view_sweep_interval_s is {self.view_sweep_interval_s}, which is below the "
                f"{self.poll_interval_s}s poll interval — the sweep is due at most once per idle "
                f"poll, so anything under that runs it on EVERY one: a corpus parse and a fresh "
                f"worktree every few seconds. Set ${VIEW_SWEEP_INTERVAL_ENV} to at least "
                f"{self.poll_interval_s:g} (default {DEFAULT_VIEW_SWEEP_INTERVAL_S}), or to "
                f"{VIEW_SWEEP_OFF:g} to turn the sweep off")
        if int(self.view_sweep_ceiling) < 1:
            raise LibrarianConfigError(
                f"view_sweep_ceiling is {self.view_sweep_ceiling}, so every pass would defer "
                f"every entity and no view would ever be regenerated — a silently useless loop. "
                f"Set ${VIEW_SWEEP_CEILING_ENV} to 1 or more (default "
                f"{DEFAULT_VIEW_SWEEP_CEILING}), or turn the sweep off with "
                f"${VIEW_SWEEP_INTERVAL_ENV}={VIEW_SWEEP_OFF:g}")

    # ── the three repo-sourced inputs: where they live IN A CHECKOUT ──────────────────────────
    # Locations, not reads — the fast lane opens none of them, reading all three at `base.sha`.
    # These exist for the steward tooling and operator messages, off the same RELPATHs.
    @property
    def acl_path(self) -> str:
        return _in_repo(self.repo, ACL_RELPATH)

    @property
    def registry_path(self) -> str:
        return _in_repo(self.repo, REGISTRY_RELPATH)

    @property
    def linter_path(self) -> str:
        return _in_repo(self.repo, LINTER_RELPATH)
