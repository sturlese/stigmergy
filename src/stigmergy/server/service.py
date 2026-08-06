"""BrainService — the transport-agnostic serving core (the MCP adapter is a thin skin over it).

Four read primitives, each enforcing the page contract AND the client's ACL scope server-side:

- search():          contract-ranked hits (factors/score/arms attached), out-of-scope pages
                     dropped.
- read_page():       one page, trust signals first, body fenced UNTRUSTED-DATA; an out-of-scope
                     path returns the SAME "unknown page" shape as a nonexistent one (no leak).
- list_entities():   the ACL-scoped entity vocabulary.
- describe_entity(): everything anchored to one entity, layered and dated.

There is no `refresh()`: every call reads Postgres, so `stigmergy-index` rebuilds (by hand or cron)
refresh what the server serves without a restart. The service holds no copy of the index — only a
live connection, the query embedder, and the ACL scope.

The service layer is where both transports share their enforcement: every call to
`search`/`read_page` (and, via `call_async`, `ask` — one layer up in `stigmergy.answer`, invoked
from `mcp_server.py`'s tool closure) is wrapped by `_call`/`call_async` with a rate-limit check
and an audit-log write, attributed to `identity`. `rate_limiter` and `audit` are duck-typed
(`.check(identity, tool)` raising on refusal; `.write(**kwargs)`) rather than imported concrete
types, so a test can inject a fake of either without pulling in `stigmergy.server.ratelimit`/
`audit` — and both default to `None` (no enforcement), so a caller that builds a `BrainService`
directly (`tests/server/conftest.py::make_service`) keeps working unchanged.

The WRITE half rides the same seam: `submit()` and `submissions()` ride `_call` exactly as the
read primitives do, so rate limiting, the audit row and the error shaping come for free and there
is no second write path. Two things live here and nowhere else, because this is the only layer
that knows who the caller is:

- **attribution** — `submitted_by` is `self.identity`, the value the transport resolved
  (`--identity` over stdio, the token's email over HTTP). `stigmergy.capture` receives it as an
  argument and has no way to learn an identity from client input, which is the structural half of
  "attribution cannot be forged"; the explicit refusals in `capture.schema` are the loud half.
- **scope** — a submitter sees their own submissions, an unrestricted identity sees the whole
  queue. Decided once, from `self.audiences`, never from a client argument.

`reply()` rides the same seam — the ask-back channel's server half. It is the most
attacker-reachable write in this system and it resolves both of the questions this layer exists to
answer, in this order: WHO is calling (only the original submitter or a steward, and every other
case gets one identical sentence that confirms nothing — the no-existence-leak rule), and only
then whether the row is in a state that can be answered. See `_reply` for why exactly one of those
two refusals is allowed to be specific.

`evidence` is the third duck-typed seam beside `rate_limiter`/`audit` (same `None` default, same
reason): a test injects `capture.evidence.MemoryEvidenceStore()` and needs no bucket.
"""
import logging
import time
from datetime import date

from stigmergy import text as textutil
from stigmergy.capture import evidence as evidence_plane
from stigmergy.capture import queue
from stigmergy.capture import schema as capture_schema
from stigmergy.capture.errors import CaptureError, ReplyRejected
from stigmergy.capture.schema import ensure_capture_schema
from stigmergy.index import rank, search, store
from stigmergy.index.backends.embedder import embedder_for_model
from stigmergy.index.errors import EmptyIndexError
from stigmergy.server import entity_aliases, identity, review
from stigmergy.server.acl import visible
from stigmergy.server.audit import AuditWriter, ensure_audit_table
from stigmergy.server.errors import CapabilityUnavailableError, StartupError
from stigmergy.server.settings import Settings

# Re-exported for `stigmergy.slack.context`: the slack package may import only
# server/answer/review_kinds (`tests/test_architecture.py::test_slack_imports_only_server_and_
# answer`; `store.py` alone holds the one pinned `capture.schema` edge), so the door constant it
# hands back to `BrainService(door=...)` crosses through this layer rather than adding a second
# sideways edge. `capture.schema` stays the owner — this is the same one hop down that already
# carries `open_scoped_resources`.
SLACK_DOOR = capture_schema.SLACK_DOOR

log = logging.getLogger(__name__)

# ranked hits pulled from the index BEFORE ACL filtering + truncation: an out-of-scope page must
# not steal a slot from a visible one, so we rank a generous pool and filter down. The fused
# candidate set can reach ~2×CANDIDATE_POOL (both arms, disjoint), so this ceiling covers the
# whole ranked set — a heavily scoped client never loses a visible page ranked beyond it.
_CANDIDATE_HITS = 2 * rank.CANDIDATE_POOL
DEFAULT_MAX_RESULTS = 5
PAGE_EXCERPT = 6000

# How many `read_page` links/backlinks entries — and, sharing the same constant,
# `describe_entity` timeline members — are ever SHOWN. Never a silent cap: the truncation (or its
# absence) is always stated in an explicit note field (house wording, `views/skeleton.py`'s
# `render_backlinks`/`render_timeline` precedent).
NAV_CAP = 20

# The ceiling on any single user-controlled string argument (query/question/path —
# `mcp_server.py`'s `ask` closure applies the same constant to `question`) reachable over the
# public HTTP boundary. Rejected BEFORE the DB read, the embedder call, or the LLM call that
# argument would otherwise trigger — a pathological multi-MB string must fail fast and cheap, not
# after paying for a query/embedding/synthesis call or bloating an `audit_log` JSONB row. 8192 is
# generous for any real question/path/query (the longest fixture question is well under 200
# chars) while still bounding worst-case cost to a known constant.
MAX_ARG_CHARS = 8192

# How many submissions one `brain_submissions` call returns by default. Small on purpose —
# the tool answers "what happened to what I just sent", not "page through my history".
DEFAULT_SUBMISSION_LIMIT = 20

# The ceiling on how many hint key NAMES one audit row records (see `_audit_hint_keys`), and on
# how deep `_truncate_for_audit` will walk a nested value (see there). Both bound the audit row's
# SHAPE, where `MAX_ARG_CHARS` bounds each individual string inside it — a row can be enormous, or
# unserializable, without any single string in it being long.
MAX_AUDIT_HINT_KEYS = 32
MAX_AUDIT_DEPTH = 20

# The ONE sentence every identity failure of `brain_reply` answers with — a nonexistent id, a
# row belonging to somebody else, and a row belonging to somebody else that is not even waiting on
# an answer. Written once, as a constant, because the security property IS that the three are
# byte-identical: three sentences maintained separately drift, and the first one that drifts turns
# this tool into an oracle for whose captures exist (the same rule `read_page`'s "unknown page"
# response already enforces for the read side).
#
# Phrased from the CALLER's side ("waiting on a reply from you") rather than about the row, so it
# is true in all three cases without hinting which one it is.
NO_REPLY_WAITING = "no submission is waiting on a reply from you at that id"


def check_arg_length(name: str, value: str) -> None:
    """Fail-closed length guard, generic wording only (no input content, no internals):
    raises ValueError for the adapter to surface as a clean `{"error": ...}` payload.

    Raises a PLAIN `ValueError` on purpose, rather than a dedicated `ArgumentError` type:
    `tests/server/test_arg_length.py` asserts the audited `error_class` is exactly `"ValueError"`
    for this rejection, both at the `BrainService` level and through `ask`'s closure, so changing
    the raised TYPE would break the committed contract. Instead the raised instance carries a
    marker attribute, `is_arg_length_error`, so `mcp_server.py`'s `read_page`/`ask` closures can
    narrow their `ValueError` catch to ONLY this specific, known-safe message — never any other
    `ValueError` (a `pydantic_core.ValidationError`, e.g., which subclasses `ValueError` and could
    carry untrusted LLM output or internal field paths) — without needing a distinguishable class
    name. `search_brain` is unaffected: it already catches `ValueError` broadly for its own
    unknown-filter errors (also safe: they echo only the caller's own filter key plus a static
    allowed-column list — ADR 013), and keeps doing so."""
    if len(value) > MAX_ARG_CHARS:
        ex = ValueError(f"{name} too long (max {MAX_ARG_CHARS} characters)")
        ex.is_arg_length_error = True
        raise ex


def _truncate_for_audit(args: dict, depth: int = 0) -> dict:
    """Bound every string `args` carries to `MAX_ARG_CHARS` before it reaches `audit_log`:
    `check_arg_length` rejects an over-limit argument before the DB/embedder/LLM call it would
    trigger, but the REJECTION itself is still audited (an audit row is written even when the tool
    returns an error payload) — without this, a rejected several-MB `query` would still land as a
    several-MB JSONB row (audit_log has no size cap of its own). Recurses into dicts/lists (covers
    `filters`, a dict of user-controlled values); every other type (int/bool/None/…) passes
    through unchanged. A truncated value keeps a human-readable marker naming exactly how much was
    cut, so the audit trail stays honest about having clipped it.

    Dict KEYS are truncated too, not only values. `filters` is a dict whose keys are as
    client-controlled as its values — `search_brain` rejects an unknown filter NAME, but that
    rejection is itself audited, so a multi-MB key would have landed in the JSONB row unbounded
    through the very path the value cap was added to close. Two keys that share their first
    `MAX_ARG_CHARS` characters AND their length collapse into one entry; that is accepted
    deliberately, because the alternative is an unbounded audit row, and the marker keeps the
    collapse visible rather than silent.

    The RECURSION is bounded as well. `filters` and `hints` are client-controlled containers, and
    a small deeply-nested value (a few KB of `[[[[...`) would otherwise walk deeper than Python's
    recursion limit and raise `RecursionError` — inside `_call`'s `finally`, where it would
    replace the caller's real result or their real exception with an audit-shaping crash. Beyond
    `MAX_AUDIT_DEPTH` the value is replaced by a marker: no legitimate argument this server takes
    nests more than two levels, so the marker is itself a finding. It also keeps `Jsonb`'s own
    (recursive) serialization inside the same bound."""
    if depth > MAX_AUDIT_DEPTH:
        return "...[nested too deep]"
    if isinstance(args, str):
        if len(args) > MAX_ARG_CHARS:
            return args[:MAX_ARG_CHARS] + f"...[truncated {len(args) - MAX_ARG_CHARS} chars]"
        return args
    if isinstance(args, dict):
        return {_truncate_for_audit(k, depth + 1): _truncate_for_audit(v, depth + 1)
                for k, v in args.items()}
    if isinstance(args, list):
        return [_truncate_for_audit(v, depth + 1) for v in args]
    return args


def _audit_args(args: dict) -> dict:
    """`_truncate_for_audit` behind a fail-safe. The shaping runs in `_call`'s `finally`, OUTSIDE
    `AuditWriter.write`'s own try — so anything it raised would clobber the result (or the
    exception) the caller actually came for. The depth guard removes the known way to make it
    raise; this removes the unknown ones. A failure is logged loudly and the row still lands,
    carrying the tool's own marker instead of its arguments, because an audit row that says "this
    call happened and its arguments could not be shaped" is worth strictly more than no row."""
    try:
        return _truncate_for_audit(args)
    except Exception:  # noqa: BLE001 — audit shaping must never surface through the served call
        log.error("audit arg shaping failed; writing the row without arguments", exc_info=True)
        return {"args_unavailable": "audit shaping failed"}


def _result_for(summarize, value, outcome: str) -> dict | None:
    """`audit_log.result`: `None` unless a call site supplied `summarize` AND the call
    actually succeeded — an `error` outcome has no return value to summarize (`value` is still
    `None` from `_call`'s own initialization), and a tool with no `summarize` callback at all
    (every read/write tool except `search_brain`/`ask`) simply carries no per-tool summary, which
    is a `NULL` column, not an empty one.

    Failure-safe like `_audit_args`: `summarize` is CALLER-SUPPLIED code (a lambda in
    `BrainService.search`, `answer.service.audit_summary`) and must never be allowed to clobber the
    caller's real result or exception by raising inside this `finally` block."""
    if summarize is None or outcome != "ok":
        return None
    try:
        return summarize(value)
    except Exception:  # noqa: BLE001 — summarizing must never surface through the served call
        log.error("audit result summarizing failed; writing the row without a result",
                  exc_info=True)
        return None


# The UNTRUSTED-DATA fence around a page body (injection fencing) lives in `stigmergy.text`, which
# this module already imports as `textutil`. It used to be redefined here, byte-identically, under
# the reason "the librarian may not import the server" — a reason that answers a question nobody
# asked, since the shared home is neither. Two copies of an injection-fencing primitive kept in
# sync by hand is exactly what `stigmergy.text` was extracted to end. `neutralize_fence`/`fence` are
# re-exported below because the answer layer imports them from here, so the neutralization lives
# in exactly one place.
neutralize_fence = textutil.neutralize_fence
fence = textutil.fence


def _neutralize_entity_record(record: dict) -> dict:
    """A registry record (`{id, name, type, aliases}`) with every steward-authored string
    neutralized — `list_entities` and `describe_entity`'s entity layer share this ONE application,
    so a registry-derived string reaching either surface is protected exactly once, in exactly one
    place. `id` is a registry KEY (like a path, never free text) and passes through unchanged."""
    return {
        "id": record["id"],
        "name": neutralize_fence(record.get("name", "")),
        "type": neutralize_fence(record.get("type", "")),
        "aliases": [neutralize_fence(a) for a in record.get("aliases") or []],
    }


def _display_title(title: str) -> str:
    """Untrusted, page-derived title text -> a safe display string for a structured `{path,
    title}`-shaped field (`read_page`'s links/backlinks, `describe_entity`'s entity/view/timeline
    references). Never an empty string, and never the raw path standing in for a missing title
    (`answer/service.py::_titles_for`'s discipline: a page contract requires a title, so a missing
    one is a data problem, not a rendering one — reapplied here rather than imported, since
    `stigmergy.server` may not import `stigmergy.answer`)."""
    return neutralize_fence(title) if title else "(untitled)"


class BrainService:
    def __init__(self, settings: Settings, conn, embedder, audiences: set[str] | None, *,
                 identity: str | None = None, rate_limiter=None, audit=None, evidence=None,
                 door: str = ""):
        self.settings = settings
        self.conn = conn
        self.embedder = embedder
        # the client's ACL scope: every read path filters through it (None = unrestricted)
        self.audiences = audiences
        # the resolved caller (a name for stdio, an email for HTTP) attributed on every audit row
        # and keyed on every rate-limit bucket; None only where no enforcement is wired.
        self.identity = identity
        self.rate_limiter = rate_limiter
        self.audit = audit
        # which capture DOOR built this service. `""` for every client-facing construction
        # (stdio, HTTP — the MCP door); `capture_schema.SLACK_DOOR` only from
        # `slack.context.SlackContext.build_service`, which is server code. A fact about the
        # CALLER, told rather than inferred: `_submit` reads it to decide whether
        # `source_client`/`source_permalink` in `hints` are the Slack transport's own provenance
        # or a client's forgery of it (`capture_schema.reject_source_provenance_hints`).
        self.door = door
        # the content-addressed evidence store every capture is archived to. Duck-typed
        # (`.put(bytes) -> key`) like `rate_limiter`/`audit`, so a test injects
        # `capture.evidence.MemoryEvidenceStore()` without a bucket; None = this server has no
        # write path wired, and `submit` refuses cleanly instead of the READ tools failing to
        # start (a server whose bucket is unreachable must still serve reads).
        self.evidence = evidence

    # ── the service-layer wrapper (rate limit + audit), shared by every entry point ────────────
    def _call(self, tool: str, args: dict, fn, *, summarize=None):
        """`summarize`: an optional `(return_value) -> dict | None` callback, invoked only on a
        SUCCESSFUL call, whose result is written to `audit_log.result` — a per-tool outcome
        SUMMARY, never a transcript (see `_result_for`)."""
        start = time.monotonic()
        outcome, error_class, value = "ok", "", None
        try:
            if self.rate_limiter is not None:
                self.rate_limiter.check(self.identity, tool)
            value = fn()
            return value
        except Exception as ex:
            outcome, error_class = "error", ex.__class__.__name__
            raise
        finally:
            if self.audit is not None:
                self.audit.write(identity=self.identity, tool=tool, args=_audit_args(args),
                                 duration_ms=(time.monotonic() - start) * 1000,
                                 outcome=outcome, error_class=error_class,
                                 result=_result_for(summarize, value, outcome))

    async def call_async(self, tool: str, args: dict, coro_fn, *, summarize=None):
        """The same wrapper for an async entry point. Public: `ask` lives one layer ABOVE this
        service (`stigmergy.answer`), so it cannot be a method here (`service.py` must never import
        `stigmergy.answer` — `tests/test_architecture.py`); `mcp_server.py`'s `ask` tool closure
        calls this directly, which is also how the HTTP transport gets the same enforcement for
        free (both transports share that one closure). `summarize`: see
        `_call` above — `mention._run_ask` and `mcp_server.py`'s `ask` closure both pass
        `answer.service.audit_summary`, so Slack and every MCP transport share one definition of
        what `ask`'s outcome summary looks like."""
        start = time.monotonic()
        outcome, error_class, value = "ok", "", None
        try:
            if self.rate_limiter is not None:
                self.rate_limiter.check(self.identity, tool)
            value = await coro_fn()
            return value
        except Exception as ex:
            outcome, error_class = "error", ex.__class__.__name__
            raise
        finally:
            if self.audit is not None:
                self.audit.write(identity=self.identity, tool=tool, args=_audit_args(args),
                                 duration_ms=(time.monotonic() - start) * 1000,
                                 outcome=outcome, error_class=error_class,
                                 result=_result_for(summarize, value, outcome))

    # ── capability guard ─────────────────────────────────────────────────────
    def require_embedder(self) -> None:
        """Refuse, by name, a call that needs a query embedder this process does not have.

        The backend guard for the keyless split: **read access is not permission to search.** The
        write tools and `read_page` are unaffected and must not consult this; `search_brain` calls
        it and so does `ask` (from `mcp_server`'s tool closure, one layer up — `service.py` must
        never import `stigmergy.answer`), each before the expensive work an `.embed()` failure would
        otherwise sit behind: the DB read for one, the whole evidence-gathering agent for the other.

        `isinstance` rather than a duck-typed `getattr(self.embedder, "unavailable_reason", "")`,
        and that is not a style preference: `tests/server/test_arg_length.py` pins "length is
        checked before the embedder is touched" with a `Poison` double that raises on ANY
        attribute access, so an attribute probe here would touch the embedder before
        `check_arg_length` had run and break the very invariant that suite exists to pin. A type
        test reads nothing off the object. `UnavailableEmbedder` is
        this module's own sentinel, built in exactly one place (`_resolve_embedder`), so there is no
        third-party embedder for the duck typing to have served.
        """
        if isinstance(self.embedder, UnavailableEmbedder):
            raise CapabilityUnavailableError(self.embedder.unavailable_reason)

    # ── search ───────────────────────────────────────────────────────────────
    def search(self, query: str, filters: dict | None = None,
               max_results: int = DEFAULT_MAX_RESULTS, include_superseded: bool = True) -> dict:
        """Contract-ranked hits scoped to the client. Unknown filter names raise ValueError
        (the adapter surfaces it as a clean error). Superseded pages are demoted by default and
        stay reachable; `include_superseded=False` drops them entirely. Rate-limited + audited —
        see `_call`. `summarize`: `{"hits": count}`, the pilot report's one `search_brain` fact —
        never the query text or the hits themselves, which `args`/`_audit_args` already bound
        separately."""
        return self._call(
            "search_brain",
            {"query": query, "filters": filters, "max_results": max_results,
             "include_superseded": include_superseded},
            lambda: self._search(query, filters, max_results, include_superseded),
            summarize=lambda r: {"hits": r["count"]})

    def _search(self, query: str, filters: dict | None, max_results: int,
                include_superseded: bool) -> dict:
        check_arg_length("query", query)   # before the DB read AND the embedder call
        # `filters` VALUES are user-controlled too: parameterized, so no SQL injection risk, but
        # unbounded here would still mean unbounded query-planner work and an unbounded audit row
        # for every filter value, not just `query` itself.
        #
        # The KEY is user-controlled too, and an unbounded one used to reach `search.search_arms`
        # unchecked — `_filter_clause` echoes every unrecognized key VERBATIM into its own
        # ValueError message (`unknown filter column(s): [...]`), which `search_brain` then
        # returns to the caller, so a giant key was both wasted query-planner/set work AND a
        # reflection surface, never merely an oversized value. Checked with a FIXED name (never
        # `f"filters.{_k}"`) so a huge key is never itself embedded into an error message first.
        for _k, _v in (filters or {}).items():
            check_arg_length("filters key", str(_k))
            check_arg_length(f"filters.{_k}", str(_v))
        # AFTER the length checks and before the DB read: an over-limit argument is refused on its
        # own terms whether or not this process can search, and a keyless server must not do the DB
        # work for a query it cannot embed.
        self.require_embedder()
        # clamp the client-supplied count first: a negative or oversized value must never slice
        # open — max_results=-1 would otherwise drop the last hit AND report count=-1.
        max_results = max(1, min(int(max_results), _CANDIDATE_HITS))

        # Entity-first resolution lives HERE, in the service, rather than in the answer layer, so
        # EVERY client gets it — stdio, HTTP, Slack, `ask` — because how to query well belongs
        # BEHIND the API. Only when the caller passed NO explicit `entity` filter (key PRESENCE,
        # not truthiness — an explicit `filters={"entity": ""}` is still an explicit filter, just
        # one that matches nothing, `search._filter_clause`'s own documented contract).
        #
        # **It LAYERS on the ranking; it does not replace it** (ADR 022 D4, amended).
        # Resolution used to run a SCOPED search first and fall back to the unscoped one only on
        # ZERO hits, which meant a resolved entity with any hits at all eclipsed the blended
        # ranking entirely: a page that is genuinely company-wide (`entity: []` — a policy, a
        # process, a cross-cutting decision) became unreachable through EVERY query naming a
        # registered company, which is most real questions. Observed on staging, where the page
        # that actually answered the question ranked #2 on raw hybrid search and was absent from
        # `search_brain` altogether.
        #
        # What the resolution feeds instead is what it always also fed, and what was already
        # sufficient: `entity_hint` carries the resolved id into the rank-time entity boost and
        # hands the lexical arm the registry's other spellings. An anchored page still wins its
        # tie against an identical unanchored one — by SCORE, so an unanchored page that is
        # genuinely the better answer can still say so. A mis-resolution now costs a few rank
        # positions rather than a page's existence, and anchoring stops being retrieval-fatal.
        entity_id = None
        if not (filters and "entity" in filters):
            aliases = entity_aliases.load_aliases(self.settings.entity_registry_path)
            entity_id = entity_aliases.resolve_entity(aliases, query)
        return self._run_search(query, filters, max_results, include_superseded,
                                entity_hint=entity_id)

    def _expansion_terms(self, entity_id: str | None) -> tuple[str, ...]:
        """The registry's OTHER names for a resolved entity — canonical name + aliases — handed
        to the lexical arm as extra OR-lexemes, so a query naming an alias lexically matches pages
        naming the canonical form and vice versa. The vec arm embeds the raw query untouched.
        Registry-missing serves `()` (the loader's documented fail-open); registry-malformed
        raises, the standing posture."""
        if not entity_id:
            return ()
        record = entity_aliases.load_registry(self.settings.entity_registry_path).get(entity_id)
        if not record:
            return ()
        return tuple(t for t in (record.get("name") or "", *(record.get("aliases") or ())) if t)

    def _run_search(self, query: str, filters: dict | None, max_results: int,
                    include_superseded: bool, entity_hint: str | None = None) -> dict:
        """The search sequence itself (arms -> existence-scope -> shape -> truncate) — the ONE
        implementation both the plain call and the entity-hinted call in `_search` ride, so
        there are not two places that fetch/filter/shape a hit list. `entity_hint` is the id
        `_search` resolved, told rather than inferred: it feeds the rank-time entity boost and the
        lexical arm's alias expansion; None means neither fires."""
        result = search.search_arms(self.conn, query, embedder=self.embedder, k=_CANDIDATE_HITS,
                                    filters=filters, include_superseded=include_superseded,
                                    today=date.today(), entity_hint=entity_hint,
                                    fts_expansion=self._expansion_terms(entity_hint))
        visible_hits = [h for h in result["hits"] if visible(h.get("acl"), self.audiences)]
        meta = store.read_meta(self.conn) or {}
        hits = [_shape_hit(h) for h in visible_hits[:max_results]]
        return {
            "query": query,
            "built_at": meta.get("built_at"),
            "embedding_model": meta.get("model"),
            "count": len(hits),
            "hits": hits,
        }

    # ── page read (the shared base + its two semantic renderings) ─────────────
    def fetch_page_raw(self, path: str) -> dict | None:
        """One page's raw fields (body sanitized + excerpted, UNFENCED), ACL-scoped — or None when
        the path is nonexistent OR out of scope (existence itself is scoped). The single
        fetch+ACL+sanitize+excerpt base shared by `read_page` (which fences the body for the
        agent) and the answer layer's verifier evidence (which needs it unfenced), so the ACL read
        path is written once and reused, never re-implemented per consumer.

        `type`/`status`/`supersedes` come from columns `search.fetch_pages` already fetches, and
        the row's own raw `links` are resolved outbound paths, unshaped — `_read_page` turns these
        into the `{path, title}` navigation surface; `AnswerBrain.get_page`, this method's other
        caller, simply ignores the extra keys."""
        page = search.fetch_pages(self.conn, [path]).get(path)
        if page is None or not visible(page.get("acl"), self.audiences):
            return None
        return {
            "path": page["path"],
            "title": page.get("title", ""),
            # `pages_index.entity` is a list — the default follows the column's own type.
            "entity": page.get("entity") or [],
            "as_of": page.get("as_of", ""),
            "type": page.get("type", ""),
            "status": page.get("status", ""),
            "supersedes": page.get("supersedes", ""),
            "superseded_by": page.get("superseded_by", ""),
            "links": page.get("links") or [],
            "body": textutil.sanitize(page.get("body") or "")[:PAGE_EXCERPT],
        }

    def read_page(self, path: str) -> dict:
        """One page, trust signals first, body fenced as UNTRUSTED-DATA. An out-of-scope or
        nonexistent path returns the SAME shape — existence itself must not leak.
        Rate-limited + audited — see `_call`."""
        return self._call("read_page", {"path": path}, lambda: self._read_page(path))

    def _read_page(self, path: str) -> dict:
        check_arg_length("path", path)   # before the DB read
        page = self.fetch_page_raw(path)
        if page is None:
            return {"error": f"unknown page: {path}"}
        outbound_links, outbound_note = self._nav_section(
            self._outbound_rows(page["links"]), subject="linked from this page",
            empty="This page links to no other pages.")
        inbound_links, inbound_note = self._nav_section(
            self._inbound_rows(path), subject="link to this page",
            empty="No pages link to this page.")
        return {
            **page,   # `links` (the raw resolved-path list) is overwritten below by the shaped one
            "banner": ("SUPERSEDED — a newer version exists; prefer it"
                       if page["superseded_by"] else None),
            "links": outbound_links, "links_note": outbound_note,
            "backlinks": inbound_links, "backlinks_note": inbound_note,
            "body": fence(page["body"]),
        }

    def _outbound_rows(self, paths: list[str]) -> list[tuple[str, str, list | None]]:
        """`(path, title, acl)` for a page's own resolved outbound `links` — one batched fetch
        (`search.fetch_pages` already supports `WHERE path = ANY(...)`), in the row's own
        (path-sorted) order."""
        if not paths:
            return []
        rows = search.fetch_pages(self.conn, paths)
        return [(p, rows[p].get("title", ""), rows[p].get("acl")) for p in paths if p in rows]

    def _inbound_rows(self, path: str) -> list[tuple[str, str, list | None]]:
        """`(path, title, acl)` for every row whose OWN `links` contains `path` — the GIN
        containment lookup `pages_index_links_gin` exists for, never a scan. New
        `pages_index` readers must import `visible()`: this module already does."""
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT path, title, acl FROM pages_index"
                " WHERE links @> ARRAY[%s]::text[] AND path <> %s ORDER BY path",
                (path, path))
            return cur.fetchall()

    # ── the shared cap+note base (read_page's links/backlinks, describe_entity's timeline) —
    # ONE existence-scoping + truncation-note mechanism, never one per surface ──────────────────
    def _capped(self, rows: list[tuple]) -> tuple[list[tuple], int]:
        """Existence-scope `rows` (ACL is always the LAST element of each tuple — every caller's
        convention) and cap to `NAV_CAP`, returning `(shown rows, total VISIBLE count)`.
        `visible()` runs BEFORE the cap, so an out-of-scope row never steals a shown slot from a
        visible one and never hints at its own presence via a count discrepancy."""
        visible_rows = [r for r in rows if visible(r[-1], self.audiences)]
        return visible_rows[:NAV_CAP], len(visible_rows)

    @staticmethod
    def _cap_note(total: int, shown: int, *, subject: str, empty: str) -> str:
        """House wording (`views/skeleton.py`'s `render_backlinks`/`render_timeline`
        precedent) — no silent cap: the truncation, or its absence, is always stated."""
        if total == 0:
            return empty
        if total > shown:
            return f"{total} page(s) {subject} — showing the first {shown}, {total - shown} more not shown."
        return f"{total} page(s) {subject} — showing all {total}."

    def _nav_section(self, rows: list[tuple[str, str, list | None]], *,
                     subject: str, empty: str) -> tuple[list[dict], str]:
        """`(path, title, acl)` triples -> capped `{path, title}` entries + the truncation note.
        Titles never fall back to the raw path (`answer/service.py::_titles_for`'s discipline,
        reapplied here — `stigmergy.server` may not import `stigmergy.answer`)."""
        shown, total = self._capped(rows)
        items = [{"path": p, "title": _display_title(t)} for p, t, _acl in shown]
        return items, self._cap_note(total, len(items), subject=subject, empty=empty)

    # ── entity navigation ──────────────────────────────────────────────────────
    def list_entities(self) -> dict:
        """The ACL-scoped entity vocabulary: `scoped_entities()`'s id set, each enriched from the
        registry (`{id, name, aliases, type}`); an anchored id absent from the registry serves as
        `{id}` alone (honest — the gardener's business, not an error). Registry missing -> every
        id served that way (the loader's documented fail-open); registry malformed -> raises.
        Rate-limited + audited — see `_call`."""
        return self._call("list_entities", {}, lambda: self._list_entities())

    def _list_entities(self) -> dict:
        registry = entity_aliases.load_registry(self.settings.entity_registry_path)
        entities = []
        for eid in self.scoped_entities():
            record = registry.get(eid)
            entities.append(_neutralize_entity_record(record) if record else {"id": eid})
        return {"count": len(entities), "entities": entities}

    def describe_entity(self, entity: str) -> dict:
        """Everything anchored to one entity, layered and dated — never a flat list: registry
        metadata + its own page, its view reference and a dated-first timeline. `entity` accepts
        an id, a name or an alias, resolved through the SAME registry loader
        `list_entities`/entity-first search use, OR verbatim membership of the caller's own
        scoped-id set (ADR 022 D5 — an anchored-but-unregistered id resolves for an identity that
        can see it). An unknown entity and an out-of-scope one return the byte-identical absence
        shape, because entity existence itself is scoped — `scoped_entities()` is the one
        existence rule, same as `list_entities`. Rate-limited + audited — see `_call`."""
        return self._call("describe_entity", {"entity": entity},
                          lambda: self._describe_entity(entity))

    def _describe_entity(self, entity: str) -> dict:
        check_arg_length("entity", entity)   # before any DB read
        # Computed UNCONDITIONALLY, before resolution, and reused — not re-queried — by the
        # fallback below AND the absence gate two lines later. This closes a timing oracle: an
        # earlier version called `scoped_entities()` only inside the gate's `or`, so a
        # never-registered input short-circuited past the DB read a registered-but-out-of-scope
        # one still paid for — response latency itself told a caller which case applied.
        scoped = set(self.scoped_entities())
        aliases = entity_aliases.load_aliases(self.settings.entity_registry_path)
        # Registry alias/name/id match first (unchanged); falling back to EXACT raw-string
        # membership of `scoped` — the SAME existence rule the gate below already consults, not a
        # second resolver. Never normalized: a scoped id is an index fact (a `pages_index.entity`
        # element), not free text a person typed, so fuzzing the comparison could only risk a
        # false match. This is what lets an anchored-but-unregistered id (`list_entities` already
        # serves it honestly as `{"id": ...}`) resolve here too, closing the navigation loop.
        entity_id = entity_aliases.resolve_exact(aliases, entity) or (
            entity if entity in scoped else None)
        absence = {"error": f"unknown entity: {entity}"}
        if entity_id is None or entity_id not in scoped:
            return absence

        registry = entity_aliases.load_registry(self.settings.entity_registry_path)
        record = _neutralize_entity_record(
            registry.get(entity_id) or {"id": entity_id, "name": "", "type": "", "aliases": []})

        own_page = self._entity_own_page_row(entity_id)   # unscoped lookup — see its own docstring
        page_ref = None
        if own_page is not None and visible(own_page[2], self.audiences):
            page_ref = {"path": own_page[0], "title": _display_title(own_page[1])}

        # views/<id>.md is deterministic (`views.regenerate.view_relpath`'s own contract)
        # — computed here rather than imported, since `stigmergy.server` has no other reach into
        # `stigmergy.views` (a governed writer beside the API, not a layer of it) and this ONE
        # formula is simple and stable enough not to need one.
        view_path = f"views/{entity_id}.md"
        view_row = search.fetch_pages(self.conn, [view_path]).get(view_path)
        view_ref = None
        if view_row is not None and visible(view_row.get("acl"), self.audiences):
            view_ref = {
                "path": view_path,
                "title": _display_title(view_row.get("title") or ""),
                "generated_at": neutralize_fence(view_row.get("generated_at", "")),
            }

        excluded = [p for p in (own_page[0] if own_page else None, view_path) if p]
        timeline_items, timeline_note = self._timeline_section(
            self._entity_timeline_rows(entity_id, excluded))

        return {
            "entity": {"id": record["id"], "name": record["name"], "type": record["type"],
                      "aliases": record["aliases"], "page": page_ref},
            "view": view_ref,
            "timeline": timeline_items, "timeline_note": timeline_note,
        }

    def _entity_own_page_row(self, entity_id: str) -> tuple[str, str, list | None] | None:
        """`(path, title, acl)` for the entity's OWN self-anchored page (`type = 'entity'`,
        `entity_id = ANY(entity)` — entity pages self-anchor), UNSCOPED (no `visible()` here):
        callers need the path even when it turns out not to be visible, to exclude it from the
        timeline structurally regardless of this caller's own ACL scope.

        `ORDER BY path` before the `LIMIT 1`: without it, which row a multiply-self-anchored id
        returns is whatever Postgres's scan order happens to be — unspecified and not guaranteed
        stable across a rebuild. Path-sorted is the same determinism convention
        `load_pages`/`_inbound_rows`/every other multi-row query here already uses."""
        with self.conn.cursor() as cur:
            cur.execute("SELECT path, title, acl FROM pages_index"
                        " WHERE type = 'entity' AND %s = ANY(entity) ORDER BY path LIMIT 1",
                        (entity_id,))
            return cur.fetchone()

    def _entity_timeline_rows(self, entity_id: str,
                              excluded_paths: list[str]) -> list[tuple]:
        """`(path, title, type, status, as_of, acl)` for every page anchored to `entity_id`
        except `excluded_paths` (the entity's own page and its view) — dated entries first by
        `as_of` desc, undated after by path (`views.skeleton.timeline_order` semantics, expressed
        in SQL: `as_of = ''` sorts false-before-true, so dated rows lead)."""
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT path, title, type, status, as_of, acl FROM pages_index"
                " WHERE %s = ANY(entity) AND path <> ALL(%s)"
                " ORDER BY (as_of = ''), as_of DESC, path ASC",
                (entity_id, excluded_paths))
            return cur.fetchall()

    def _timeline_section(self, rows: list[tuple]) -> tuple[list[dict], str]:
        """`(path, title, type, status, as_of, acl)` rows -> capped timeline entries + the
        truncation note — the same cap+note base `_nav_section` rides (`_capped`/`_cap_note`,
        `NAV_CAP`: one shared constant rather than two that could drift apart), with the
        timeline's own richer item shape."""
        shown, total = self._capped(rows)
        items = [{"path": p, "title": _display_title(t), "type": neutralize_fence(ty),
                 "status": neutralize_fence(st), "as_of": neutralize_fence(ao)}
                for p, t, ty, st, ao, _acl in shown]
        return items, self._cap_note(total, len(items), subject="anchored to this entity",
                                     empty="No anchored pages.")

    # ── the write path (the fast lane's front half) ──────────────────────────
    def submit(self, kind: str, material: str, hints: dict | None = None,
               submitted_by: str | None = None, verification: str | None = None,
               acl=None, content_hash: str | None = None) -> dict:
        """Queue a capture, attributed to THIS service's resolved identity.

        The four fields after `hints` are TRAPS, not parameters. Each is computed by the server —
        attribution, the trust signal, the access-control labels and the content address — and
        each is accepted here only so it can be REFUSED explicitly. FastMCP builds its tool
        argument model with pydantic's default `extra="ignore"`, so a server-owned field in the
        arguments is dropped SILENTLY by the SDK unless the signature names it: a field that is
        not declared never reaches this method at all. Declaring exactly these four
        (`submitted_by`, `verification`, `acl`, `content_hash`) is what turns "quietly ignored"
        into an error for them.

        Fields NOT on this signature are still structurally safe — nothing anywhere reads client
        input into a server-computed column — but they are dropped rather than refused. That
        residual is documented in `docs/reference/capture.md`, deliberately, rather than papered
        over.

        Rate-limited + audited via `_call`, like every read tool — one seam, not a second path.
        The refusal happens inside `_call`, so it is rate-limited and audited like any other call,
        and it raises before any blob or row is written.
        """
        # Built ONCE and threaded through both consumers: the audit-arg builder (which records the
        # NAMES attempted) and the refusal. Two hand-maintained lists of the same four fields is
        # exactly how one of them ends up missing a field later.
        server_owned = {"submitted_by": submitted_by, "verification": verification,
                        "acl": acl, "content_hash": content_hash}
        return self._call("brain_submit",
                          _submit_audit_args(kind, material, hints, server_owned),
                          lambda: self._submit(kind, material, hints, server_owned))

    def _submit(self, kind: str, material: str, hints: dict | None,
                server_owned: dict) -> dict:
        capture_schema.reject_server_owned_arguments(server_owned)
        # Same seam, same shape, for the two source hints the fast lane trusts — refused for every
        # door but the Slack transport's own (`self.door`, see `__init__`).
        capture_schema.reject_source_provenance_hints(hints, door=self.door)
        # The drive pair, refused with NO door exception (ADR 028 D7) — the one legitimate
        # asserter (`stigmergy-drive`, an operator CLI) never passes through this service at all.
        capture_schema.reject_drive_provenance_hints(hints)
        # `brain_submit` is restricted to `("raw", "page")` explicitly, never to
        # `capture_schema.KINDS` (which also lists `"meeting"` for the DROP CLI's own direct call
        # to `queue.submit`). The runbook calls the drop CLI "the only door" onto the meeting
        # flow — that was a convention, not a control, until this check: `kind` is a MODEL-CHOSEN
        # MCP argument, and growing `KINDS` for the CLI silently made every value in it
        # acceptable here too.
        if kind not in capture_schema.MCP_SUBMIT_KINDS:
            raise CaptureError(
                f"kind {kind!r} is not submittable through brain_submit (allowed: "
                f"{', '.join(capture_schema.MCP_SUBMIT_KINDS)}) — a meeting transcript is dropped "
                f"through the `stigmergy-meeting drop` operator CLI, the only door onto that flow")
        if self.evidence is None:
            raise CaptureError("the capture queue is not available on this server")
        if not self.identity:
            # Fail-closed: an unattributable submission is worse than a refused one — the whole
            # fast-lane governance model rests on a named person standing behind a `developing`
            # page. Unreachable through either transport (both resolve an identity before serving).
            raise CaptureError("no resolved identity — a capture cannot be submitted unattributed")
        ack = queue.submit(self.conn, self.evidence, kind=kind, material=material, hints=hints,
                           submitted_by=self.identity)
        return {**ack, "message": _ack_message(ack)}

    def reply(self, submission_id: int, answer: str) -> dict:
        """Answer the librarian's one question about a `needs_input` capture.

        Rides `_call` like every other entry point, so the rate limit, the audit row and the error
        shaping come from the one seam rather than from a second write path.

        **The audit row records the answer's SIZE and HASH, never its text.** `_truncate_for_audit`
        would have bounded it, but bounding is not the question: content stays out of every log,
        `_submit_audit_args` already takes exactly this posture for the material itself, and a reply
        is free text a person typed — capable of carrying the same credential a capture can. The
        hash joins the audit row to the `reply` column for anyone who needs to prove what was said,
        which is what an audit trail owes and no more.
        """
        digest, size = capture_schema.material_digest(answer if isinstance(answer, str) else "")
        return self._call(
            "brain_reply",
            {"submission_id": submission_id, "answer_chars": len(answer or ""),
             "answer_bytes": size, "answer_sha256": digest if size else ""},
            lambda: self._reply(submission_id, answer))

    def _reply(self, submission_id: int, answer: str) -> dict:
        """Identity first, state second, and the two refusals are deliberately NOT alike.

        **Identity failures are generic and identical** — the id does not exist, it belongs to
        somebody else, or it belongs to somebody else and is not even `needs_input`. All three
        return one sentence, because any difference between them turns this tool into an oracle for
        "does submission 14 exist" and "whose is it" (the same no-existence-leak rule `read_page`
        already enforces for pages).

        **A state failure, for a caller who IS authorized, may be specific.** That caller can read
        the row through `brain_submissions` already, so naming its actual status leaks nothing —
        and the generic sentence would be actively wrong there: "no submission is waiting on a reply
        from you at that id" reads as "you are not allowed" to somebody looking at their own
        capture, which is exactly what a person replying twice out of habit, or replying after a
        steward already drained the row, will do.

        A steward (unrestricted identity, `audiences is None`) may reply on a submitter's behalf.
        The reply is attributed to the identity that actually made it — never to the submitter —
        because a governance record that quietly reattributed a steward's words would be worse than
        none. That is the same attribution rule every write channel here follows.
        """
        capture_schema.prepare_reply(answer)
        if not self.identity:
            # Fail-closed, same reasoning as `_submit`: an unattributable reply cannot be recorded
            # against anybody, and this row's whole point is that a named person answered it.
            raise ReplyRejected(NO_REPLY_WAITING)
        # Read UNSCOPED, then decide — deliberately, and it is the only way to tell the three
        # identity cases apart internally while telling nobody outside. A scoped read would collapse
        # "not yours" and "does not exist" here too, and then a steward could not reply on behalf.
        row = queue.get_submission_trace(self.conn, submission_id)
        unrestricted = self.audiences is None
        if row is None or not (unrestricted or row["submitted_by"] == self.identity):
            raise ReplyRejected(NO_REPLY_WAITING)
        if row["status"] != capture_schema.NEEDS_INPUT:
            raise ReplyRejected(
                f"capture {submission_id} isn't waiting on a reply — its status is "
                f"{row['status']!r}. There's nothing to answer; check brain_submissions for what "
                f"happened to it.")
        on_behalf = row["submitted_by"] != self.identity
        result = queue.record_reply(
            self.conn, submission_id, answer=answer, actor=self.identity,
            note=(f"replied on behalf of {row['submitted_by']}" if on_behalf else "replied"))
        return {**result, "on_behalf_of": row["submitted_by"] if on_behalf else "",
                "message": _reply_message(submission_id)}

    def submissions(self, limit: int = DEFAULT_SUBMISSION_LIMIT, status: str | None = None) -> dict:
        """The caller's own submissions with state, timestamps and result reference — or the whole
        queue for an unrestricted (steward) identity, with each row marked `mine`.

        The scope decision is made HERE, once, from the resolved audience scope — never from a
        client argument. There is no `submitted_by` filter on this tool at all, which is why a
        scoped identity has no way to ask for somebody else's rows.
        """
        return self._call("brain_submissions", {"limit": limit, "status": status},
                          lambda: self._submissions(limit, status))

    def _submissions(self, limit: int, status: str | None) -> dict:
        statuses = [status] if status else None
        unrestricted = self.audiences is None
        rows = (queue.list_all_submissions(self.conn, statuses=statuses, limit=limit)
                if unrestricted
                else queue.list_own_submissions(self.conn, self.identity, statuses=statuses,
                                                limit=limit))
        return {
            "identity": self.identity,
            "scope": "all" if unrestricted else "own",
            "count": len(rows),
            "submissions": [self._shape_submission(row) for row in rows],
        }

    def _shape_submission(self, row: dict) -> dict:
        """One listed submission, injection-safe AND confidentiality-safe on the way out.

        **Two different questions about the same field, and this used to answer only one.** The
        excerpt is material the submitter wrote — untrusted data like any page body, so it goes
        through the SAME fence, and every other free-text field (hint values, and the
        `error`/`question` note the librarian authors) is neutralized so it can never close that
        fence from in-band. That is injection protection, and it says nothing about whether the
        text should be here at all. It should not be, when the row is terminal *because* its
        material carried a secret or personal data: this surface was serving that value back in the
        same object as the refusal saying it had not — to the submitter on every call, and to any
        unrestricted (steward) identity for everybody's captures.

        The suppression is not made here. `capture.queue` decides it in the query, so the value
        never crosses the wire, and this method receives an empty `excerpt`, empty `hints` and a
        `withheld_reason` sentence — carried through unfenced and un-neutralized, deliberately: it
        is the server's own constant, and fencing it would tell a reader the submitter wrote it.
        """
        excerpt, note = row["excerpt"], row["error"]
        needs_input = row["status"] == capture_schema.NEEDS_INPUT
        return {
            "id": row["id"],
            "kind": row["kind"],
            "status": row["status"],
            "submitted_by": row["submitted_by"],
            "mine": row["submitted_by"] == self.identity,
            "attempts": row["attempts"],
            "created_at": row["created_at"],
            "claimed_at": row["claimed_at"],
            "finished_at": row["finished_at"],
            "result_ref": row["result_ref"],
            "question": neutralize_fence(note) if needs_input else "",
            # The invocation the question STATES, as a fact beside the sentence — the same doctrine
            # that puts `reason_code` beside a refusal. This surface is read either raw or through
            # the reader's own LLM session, and a paraphrase has no reason to preserve a literal
            # `brain_reply(...)` it did not write; the structured field is what does not depend on
            # that. Built from `capture.schema`, the one place that spells this call, so the prose
            # and the field cannot promise two different commands.
            "reply_hint": ({"tool": capture_schema.REPLY_TOOL, "submission_id": row["id"],
                            "invocation": capture_schema.reply_invocation(row["id"])}
                           if needs_input else None),
            # The journey, once the question has been answered: what was said, who is still being
            # waited on, and the row's own history of asks and dispositions. `reply` is the
            # submitter's own text, so it is neutralized like every other free-text field here —
            # and `_LIST_SELECT`, the query behind THIS surface, has already suppressed it entirely
            # on a row whose material may not be read back. Naming the query rather than the module
            # is the correction: the rule lived in that one SELECT and not in
            # `get_submission_trace`, which is the other read of the same column, and a comment
            # saying "capture.queue has suppressed it" is exactly what stops a reviewer noticing.
            # Both paths apply `_MATERIAL_WITHHELD` now, and both come from that one expression.
            "reply": neutralize_fence(row["reply"]),
            "waiting_on": row["waiting_on"],
            "events": _neutralize_report(row["events"]),
            "error": "" if needs_input else neutralize_fence(note),
            "blob_refs": row["blob_refs"],
            "content_sha256": row["content_sha256"],
            "bytes": row["bytes"],
            "payload_purged": row["payload_purged"],
            "withheld_reason": row["withheld_reason"],
            "hints": {k: neutralize_fence(v) for k, v in (row["hints"] or {}).items()},
            "flagged_hints": row["flagged_hints"],
            "excerpt": fence(excerpt) if excerpt else "",
            "report": _neutralize_report(row["report"]),
        }

    # ── the review lane ────────────────────────────────────────────────────────
    # `review_queue`/`review_decide` ride `_call` exactly like every tool above — the same seam
    # `submit` rides — so attribution, rate limiting and the audit row come for free, and no
    # second door is opened. The mechanics live in `stigmergy.server.review`: that module needs
    # `stigmergy.librarian` primitives `service.py` itself must never import
    # (`tests/test_architecture.py`'s `test_server_never_imports_the_librarian`), so it is a
    # sibling module with its own narrow, pinned exception rather than inline here.
    def review_queue(self, limit: int = 50) -> dict:
        """The unified inbox over entity proposals and parked captures —
        ACL/ownership-scoped to the caller, same posture as `submissions()`."""
        return self._call("review_queue", {"limit": limit},
                          lambda: review.review_queue(self, limit=limit))

    def review_decide(self, item_kind: str, item_id: str, verdict: str, notes: str = "", *,
                      name: str = "", entity_id: str = "", entity_type: str = "", aliases=None,
                      role: str = "", requeue: bool = False) -> dict:
        """Record a verdict on one review-queue item, attributed to THIS service's resolved
        identity. `reject` and every `parked-capture` verdict are Postgres only, literally —
        nothing on those paths touches git. Approving an `entity-proposal` mints through the
        governed door instead (ADR 030): `name`/`entity_type` are then required,
        `entity_id`/`aliases`/`role`/`requeue` optional — `review.review_decide`'s own docstring
        carries the full contract. `notes` is audited by length only; it is free text a steward
        typed and crosses `capture.dispositions.clean` (plus a secrets scan) before it reaches
        anything a submitter can read. The identity metadata is audited the same conservative way
        — lengths, a presence flag and the closed-vocabulary `entity_type`/`entity_id`, never the
        `name`/`role`/`aliases` text itself."""
        return self._call(
            "review_decide",
            {"item_kind": item_kind, "item_id": item_id, "verdict": verdict,
             "notes_chars": len(notes or ""), "name_chars": len(name or ""),
             "entity_id": entity_id or "", "entity_type": entity_type or "",
             "aliases_present": bool(aliases), "role_chars": len(role or ""),
             "requeue": bool(requeue)},
            lambda: review.review_decide(
                self, item_kind=item_kind, item_id=item_id, verdict=verdict, notes=notes,
                name=name, entity_id=entity_id, entity_type=entity_type, aliases=aliases,
                role=role, requeue=requeue))

    # ── scoped read helpers (reused by the answer layer) ──────────────────────
    def scoped_entities(self) -> list[str]:
        """Distinct entities on pages THIS client may see — existence is scoped too. The one place
        the entity discovery SQL lives: `AnswerBrain.known_entities` calls through to it rather
        than re-querying `pages_index`, so there is no second scoping rule to keep in step.

        `entity` is `text[]` — `unnest` gives one row per (id, acl) pair, the same shape this
        query returned when `entity` was one scalar per page, so the `visible()` scoping below
        needs no change."""
        with self.conn.cursor() as cur:
            cur.execute("SELECT DISTINCT unnest(entity), acl FROM pages_index"
                        " WHERE entity <> '{}'")
            return sorted({e for e, acl in cur.fetchall() if visible(acl, self.audiences)})


def _submit_audit_args(kind: str, material: str, hints: dict | None,
                       server_owned: dict) -> dict:
    """What `audit_log` records about a submit: sizes, a hash and hint KEYS — never the captured
    text: content is never logged.

    The hash is the same `sha256` the evidence key is built from (`capture.schema.material_digest`
    is the single definition), so the audit row joins to the archived object without ever carrying
    the material. `server_owned_args_present` lists the NAMES a caller tried to set and never the
    values: the attempt is the security signal, while the value is somebody's identity, ACL label
    or trust claim and does not belong in a log either.
    """
    digest, size = capture_schema.material_digest(material if isinstance(material, str) else "")
    return {
        "kind": kind,
        "material_bytes": size,
        "material_sha256": digest if size else "",
        "hint_keys": _audit_hint_keys(hints),
        "server_owned_args_present": sorted(k for k, v in server_owned.items() if v is not None),
    }


def _audit_hint_keys(hints) -> list[str]:
    """The hint key names, BOUNDED BY COUNT as well as by length.

    These are read off the raw client dict before validation, and the audit row is written
    whatever the outcome — so `hints` with 100k keys would land a multi-MB JSONB row on a call
    that was going to be rejected anyway (`_truncate_for_audit` caps each string, never the
    number of them). `HINT_KEYS` has four legal names; 32 is generous enough that a truncation
    marker here always means "this caller was doing something odd", which is itself the signal
    worth keeping. A non-dict `hints` contributes nothing rather than being iterated as a
    sequence."""
    keys = sorted(str(k) for k in (hints if isinstance(hints, dict) else {}))
    if len(keys) <= MAX_AUDIT_HINT_KEYS:
        return keys
    return keys[:MAX_AUDIT_HINT_KEYS] + [f"...[{len(keys) - MAX_AUDIT_HINT_KEYS} more keys]"]


def _reply_message(submission_id: int) -> str:
    """The reply acknowledgement's one line of prose, and it restates the one-ask budget on purpose.

    A bare `{"status": "queued"}` invites the reasonable-but-wrong belief "my answer worked, so it
    will be filed now". It will be TRIED again, which is a different promise — and this is the
    response the reader's eyes are actually on at the moment they might form that belief, which is
    why the rule is repeated here rather than left in the question they read an hour ago.
    """
    return (f"recorded — capture #{submission_id} is back in the queue to be looked at again. This "
            f"was the only question this capture gets: if it still can't be matched to a registered "
            f"entity, a steward takes it from there and you won't be asked a second time.")


def _ack_message(ack: dict) -> str:
    """The submit acknowledgement's one line of prose. It promises exactly what happened —
    queued and attributed — and nothing more: until the librarian files it, nothing is in
    the brain, and an ack that said "saved" would be a lie the user would plan around."""
    line = (f"queued as submission #{ack['id']} and attributed to {ack['submitted_by']}. "
            "The librarian files it; nothing is in the brain until it does — "
            "check with brain_submissions.")
    if ack.get("flagged_hints"):
        line += (f" Note: the material declares {', '.join(ack['flagged_hints'])} in its "
                 "frontmatter; recorded as a hint and ignored — those fields are the server's.")
    return line


def _neutralize_report(report, depth: int = 0):
    """The librarian's report, made safe to hand a reader.

    Every free-text field in it — the summary sentence, an overlap note, a page title inside a
    path — is DERIVED from captured material, so it is untrusted text on the way out exactly like
    the excerpt beside it. It is not `fence()`d: the report is a structured object a client reads
    field by field, not a blob pasted into a prompt, and fencing every leaf would make it
    unreadable. Neutralizing the fence token is the part that matters — it is what stops a value
    in here from closing the excerpt's fence from in-band.

    Depth-bounded with the same constant the audit shaper uses: the librarian builds this object,
    but it is stored as JSONB and read back, so nothing guarantees at THIS boundary that it is
    shallow. Beyond the bound the subtree is dropped rather than walked.
    """
    if depth > MAX_AUDIT_DEPTH:
        return None
    if isinstance(report, str):
        return neutralize_fence(report)
    if isinstance(report, dict):
        return {str(k): _neutralize_report(v, depth + 1) for k, v in report.items()}
    if isinstance(report, list):
        return [_neutralize_report(v, depth + 1) for v in report]
    return report


_fence = fence   # back-compat alias (tests/server/test_fence.py imports `_fence`)


def _shape_hit(h: dict) -> dict:
    """A search hit as JSON: path/title/snippet, contract flags, and the full ranking factors, so
    ranking is answerable. The body and raw acl labels never ship."""
    return {
        "path": h["path"],
        "title": h.get("title", ""),
        "type": h.get("type", ""),   # the agent's only way to spot "type: entity" pages from a
        #                               search listing, rather than only from read_page
        "snippet": h.get("snippet", ""),
        "score": h["score"],
        "arms": h["arms"],
        "factors": h["factors"],
        "entity": h.get("entity") or [],   # a list, not a scalar
        "as_of": h.get("as_of", ""),
        "superseded": bool(h.get("superseded_by")),
        "superseded_by": h.get("superseded_by", ""),
    }


class UnavailableEmbedder:
    """The "embedder" a keyless process gets: it holds the reason it could not be built and refuses.

    **This is what makes the write path independent of the read path.** Before it, a missing
    `OPENAI_API_KEY` was a `StartupError` and `stigmergy-server` did not start — so an expired
    embedding key, a spent quota or a provider outage took `brain_submit` down with
    `search_brain`, and capture is the one thing that must survive: it cannot depend on the read
    path's quota, key rotation or provider outage.

    Same duck type as a real embedder (`.embed(texts)`), so nothing downstream needs a null check,
    and it raises rather than returning a vector: a degraded embedder that returned SOMETHING would
    be the `--embedder fake` hazard wearing a different hat — a query embedded into a space the
    index was not built in returns unrelated results and reports success.

    `unavailable_reason` is the seam `BrainService.require_embedder` reads, so a tool can refuse
    BEFORE doing the expensive work (the DB read, the agent turns) that a `.embed()` call would sit
    behind.
    """

    def __init__(self, reason: str):
        self.unavailable_reason = reason

    def embed(self, texts):
        raise CapabilityUnavailableError(self.unavailable_reason)


def missing_embedder_reason(detail: str) -> str:
    """The one sentence every embedder-needing tool refuses with. Written once so `search_brain`
    and `ask` cannot describe the same absence differently.

    It names three things, and all three are load-bearing: WHICH capability is missing, that the
    write path is unaffected (so a person does not conclude the brain is down and stop capturing),
    and `detail` — the embedder's own message, which is where the "`--embedder fake` is not a
    substitute" correction lives.
    """
    return (f"unavailable: this server is running without a query embedder, so it cannot search. "
            f"{detail} Capture is unaffected — brain_submit, brain_submissions, read_page, "
            f"list_entities and describe_entity need no embedder and are working normally.")


def _resolve_embedder(settings: Settings, model: str):
    """The query embedder must embed in the same space the documents did. Default: the model the
    index was built with (index_meta).

    A missing OPENAI_API_KEY does not stop the server: it yields an `UnavailableEmbedder`, so the
    process starts, the write tools and `read_page` work, and the two tools that genuinely need an
    embedder refuse with a named capability. `StartupError` is kept for faults that really do mean
    "do not serve" — an unknown embedder name, which is a typo rather than an absent credential,
    and which would otherwise silently degrade a server the operator thought they had configured.
    """
    # An explicit-but-unrecognised name was SILENTLY IGNORED before: neither branch below matched,
    # so control fell through to "match the built index" and the server served happily with an
    # embedder the operator had not asked for. Harmless while `--embedder` was the only way in
    # (argparse `choices` filtered it) and not harmless now, because the fall-through is also the
    # degrade path — a typo would present as a missing key, which is a different problem with a
    # different fix. `argparse` still catches it first for CLI callers; this catches every other one.
    if settings.embedder not in (None, "", "fake", "openai"):
        raise StartupError(f"unknown embedder {settings.embedder!r} (use 'openai' or 'fake')")
    try:
        if settings.embedder == "fake":
            from stigmergy.index.backends.embedder import build_embedder
            return build_embedder("fake")
        if settings.embedder == "openai":
            from stigmergy.index.backends.embedder import build_embedder
            return build_embedder("openai", model)
        return embedder_for_model(model)          # match the built index (the usual path)
    except RuntimeError as ex:                     # e.g. OPENAI_API_KEY is not set
        log.warning("starting without a query embedder; search_brain and ask will refuse: %s", ex)
        return UnavailableEmbedder(missing_embedder_reason(str(ex)))


def open_scoped_resources(settings: Settings, conn=None):
    """`(conn, embedder)`, fail-closed on an empty index — the ONE place this three-step sequence
    (open the connection, read `index_meta`, resolve the query embedder against it) is written.

    Extracted so a THIRD transport (`stigmergy.slack`) can build its own per-identity
    `BrainService`s the same way `build_service` (stdio) and `transport_http.build_http_app`
    (HTTP) already do, without importing `stigmergy.index` itself — `stigmergy.slack`'s import list is
    pinned to exactly `{server, answer}`, and this is what keeps that literally true while every
    transport still shares one construction path rather than a third copy of it."""
    conn = conn or store.connect(settings.dsn)
    meta = store.read_meta(conn)
    if meta is None:
        raise EmptyIndexError(
            "the index is empty — run `stigmergy-index --rebuild --repo <dir>` before serving")
    embedder = _resolve_embedder(settings, meta["model"])
    return conn, embedder


def build_service(settings: Settings, conn=None) -> BrainService:
    """Wire a service from settings, fail-closed. Identity resolves FIRST (before any DB work),
    so an identity failure never touches Postgres and never starts the server open.

    Wires the service-layer AUDIT too — stdio's half of "both transports benefit", where HTTP's is
    `transport_http.build_http_app`, which also wires a `RateLimiter`. The audit table is ensured
    once here, so every stdio call is attributed the same as an HTTP one.

    Deliberately NO `RateLimiter` here: the 30/10 req/min budgets exist to protect the OpenAI
    spend behind a PUBLIC url — a single local operator over stdio already has unmediated
    Postgres/OpenAI access via other CLIs (`stigmergy-search`, `stigmergy-index`), so rate-limiting
    stdio would add friction without closing any new exposure (and
    `tests/server/test_rebuild_while_serving.py`'s rapid-fire stdio hammer test assumes exactly
    this)."""
    audiences_tuple = identity.resolve_audiences(settings.identities_path, settings.identity)
    conn, embedder = open_scoped_resources(settings, conn)
    audiences = set(audiences_tuple) if audiences_tuple is not None else None

    ensure_audit_table(conn)
    # the durable write-path tables, ensured beside the audit table — same idempotent-DDL
    # pattern, same startup, both transports. `store_from_env` does no I/O, so a server whose
    # bucket is unreachable still starts and still serves every read tool; the failure surfaces
    # on the submit that needs it, as a clean error.
    ensure_capture_schema(conn)
    # The review lane's one table (`review_decisions`), same idempotent-DDL pattern. Ensured
    # unconditionally, like the capture tables above: `review_queue`/`review_decide` work over
    # entity-proposal/parked-capture items on any server, configured knowledge repo or not.
    review.ensure_review_schema(conn)
    return BrainService(settings, conn, embedder, audiences, identity=settings.identity,
                        audit=AuditWriter(conn), evidence=evidence_plane.store_from_env())
