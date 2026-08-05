"""`audit_log` (ADR 013 §6): one row per tool call, both transports, written at the service
layer, so every call is attributable to a person. Real Postgres (`indexed` fixture) — DDL, the
writer's own swallow-on-failure guarantee, and `BrainService` actually producing a row end to end.
Skips without postgres, same posture as the rest of `tests/server/`."""
import logging

import pytest

from stigmergy.server.audit import AuditWriter, ensure_audit_table
from stigmergy.server.ratelimit import RateLimiter
from stigmergy.server.service import (
    MAX_ARG_CHARS,
    MAX_AUDIT_DEPTH,
    MAX_AUDIT_HINT_KEYS,
    _audit_hint_keys,
    _truncate_for_audit,
)
from tests.server.conftest import make_service


def _rows_for(conn, identity: str) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT identity, tool, args, duration_ms, outcome, error_class, result FROM audit_log"
            " WHERE identity = %s ORDER BY id", (identity,))
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, row, strict=True)) for row in cur.fetchall()]


@pytest.fixture()
def clean_audit_log(indexed):
    """`ensure_audit_table` is idempotent DDL (never drops), so the table survives across the
    whole session-scoped `indexed` fixture — each test clears it first so row-count assertions
    don't see other tests' rows."""
    conn, fx = indexed
    ensure_audit_table(conn)
    with conn.cursor() as cur:
        cur.execute("DELETE FROM audit_log")
    return conn, fx


def test_ensure_audit_table_is_idempotent(indexed):
    conn, _ = indexed
    ensure_audit_table(conn)
    ensure_audit_table(conn)   # a second call must not raise (CREATE TABLE IF NOT EXISTS)
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('audit_log')")
        assert cur.fetchone()[0] == "audit_log"


def test_write_inserts_a_queryable_row_with_every_expected_column(clean_audit_log):
    conn, _ = clean_audit_log
    AuditWriter(conn).write(identity="steward@example.com", tool="search_brain",
                            args={"query": "q", "max_results": 5}, duration_ms=12.5,
                            outcome="ok", error_class="")

    rows = _rows_for(conn, "steward@example.com")
    assert len(rows) == 1
    row = rows[0]
    assert row["tool"] == "search_brain"
    assert row["args"] == {"query": "q", "max_results": 5}   # full args JSON, not a summary
    assert row["duration_ms"] == pytest.approx(12.5)
    assert row["outcome"] == "ok"
    assert row["error_class"] == ""


def test_rows_are_queryable_per_identity(clean_audit_log):
    """Rows are queryable per identity — the golden-set harvest and the operator runbook both
    read the trail scoped to one email."""
    conn, _ = clean_audit_log
    writer = AuditWriter(conn)
    writer.write(identity="ana@example.com", tool="search_brain", args={}, duration_ms=1.0,
                outcome="ok", error_class="")
    writer.write(identity="eng@example.com", tool="read_page", args={}, duration_ms=1.0,
                outcome="ok", error_class="")

    assert [r["tool"] for r in _rows_for(conn, "ana@example.com")] == ["search_brain"]
    assert [r["tool"] for r in _rows_for(conn, "eng@example.com")] == ["read_page"]


def test_write_failure_is_logged_loudly_and_swallowed_not_raised(clean_audit_log, caplog):
    """An audit-write failure is loudly logged but does not fail the serving call — THIS is where
    that guarantee actually lives (not in `BrainService._call`, see
    `tests/server/test_service_layer_wrapping.py::test_audit_write_failure_is_not_this_layers_job_to_swallow`)."""
    conn, _ = clean_audit_log

    class BrokenConn:
        def cursor(self):
            raise RuntimeError("connection reset by peer")

    with caplog.at_level(logging.ERROR):
        AuditWriter(BrokenConn()).write(identity="steward@example.com", tool="search_brain", args={},
                                        duration_ms=1.0, outcome="ok", error_class="")   # must not raise

    assert any("audit write failed" in r.message for r in caplog.records)
    assert any(r.levelno == logging.ERROR for r in caplog.records)


# ── BrainService end to end: the audit row a real search/read_page/ask call produces ───────────
def test_brainservice_search_writes_an_ok_audit_row_with_full_args(clean_audit_log):
    conn, fx = clean_audit_log
    svc = make_service(fx, conn, fx.STEWARD, audit=AuditWriter(conn))
    svc.search("quarterly revenue", max_results=3)

    rows = _rows_for(conn, fx.STEWARD)
    assert len(rows) == 1
    assert rows[0]["tool"] == "search_brain"
    assert rows[0]["args"] == {"query": "quarterly revenue", "filters": None, "max_results": 3,
                               "include_superseded": True}
    assert rows[0]["outcome"] == "ok"


def test_search_brain_audit_row_carries_a_hit_count_result(clean_audit_log):
    """`search_brain`'s `audit_log.result` is `{"hits": count}` — a per-tool outcome summary the
    pilot report reads, never a transcript (the query text is `args`'s business, this is only the
    SHAPE of the outcome)."""
    conn, fx = clean_audit_log
    svc = make_service(fx, conn, fx.STEWARD, audit=AuditWriter(conn))
    out = svc.search("quarterly revenue", max_results=3)

    rows = _rows_for(conn, fx.STEWARD)
    assert rows[0]["result"] == {"hits": out["count"]}


def test_brainservice_writes_an_audit_row_even_when_the_call_errors(clean_audit_log):
    """An audit row is written even when the tool returns an error payload: an unknown filter
    raises ValueError at the service layer (the MCP adapter turns it into the JSON error payload
    one layer up); the audit row for the attempt must exist regardless."""
    conn, fx = clean_audit_log
    svc = make_service(fx, conn, fx.STEWARD, audit=AuditWriter(conn))
    with pytest.raises(ValueError):
        svc.search("x", filters={"body": "nope"})

    rows = _rows_for(conn, fx.STEWARD)
    assert len(rows) == 1
    assert rows[0]["outcome"] == "error"
    assert rows[0]["error_class"] == "ValueError"
    assert rows[0]["result"] is None      # nothing to summarize — the call never returned a value


def test_brainservice_writes_an_audit_row_on_a_rate_limit_refusal(clean_audit_log):
    conn, fx = clean_audit_log
    limiter = RateLimiter(overall_per_min=1)
    svc = make_service(fx, conn, fx.ANA, rate_limiter=limiter, audit=AuditWriter(conn))
    svc.search("acme payroll")                             # spends the one token
    from stigmergy.server.errors import RateLimitError
    with pytest.raises(RateLimitError):
        svc.read_page(fx.ACME_PAGE)                         # refused before it ever touches the index

    rows = _rows_for(conn, fx.ANA)
    assert [r["tool"] for r in rows] == ["search_brain", "read_page"]
    assert rows[1]["outcome"] == "error"
    assert rows[1]["error_class"] == "RateLimitError"


def test_brainservice_with_no_audit_wired_writes_nothing_and_still_serves(clean_audit_log):
    """`make_service` without `audit=` keeps `BrainService.audit` at its default `None`, so no row
    is written and the call still succeeds — the service-layer wrapper never assumes an audit
    writer is present."""
    conn, fx = clean_audit_log
    out = make_service(fx, conn, fx.STEWARD).search("quarterly revenue")
    assert out["hits"]
    assert _rows_for(conn, fx.STEWARD) == []


# ── a rejected oversized argument must land TRUNCATED in audit_log, never at full size — an 8+
# MB rejected query must not become an 8+ MB JSONB row. `_truncate_for_audit` is service.py's own
# concern; these tests prove its OUTPUT actually lands in the real audit_log row, end to end. ───
def test_oversized_query_audit_row_is_truncated_with_a_marker(clean_audit_log):
    conn, fx = clean_audit_log
    svc = make_service(fx, conn, fx.STEWARD, audit=AuditWriter(conn))
    overflow = 5000
    huge = "x" * (MAX_ARG_CHARS + overflow)
    with pytest.raises(ValueError):
        svc.search(huge)

    rows = _rows_for(conn, fx.STEWARD)
    assert len(rows) == 1
    stored_query = rows[0]["args"]["query"]
    assert stored_query.endswith(f"...[truncated {overflow} chars]")
    assert stored_query.startswith("x" * MAX_ARG_CHARS)          # the head survives, just clipped
    # bounded: nowhere close to the original ~13KB string reaching the JSONB row
    assert len(stored_query) < len(huge)
    assert len(stored_query) <= MAX_ARG_CHARS + len(f"...[truncated {overflow} chars]") + 1


def test_oversized_filters_value_in_the_audit_row_is_also_truncated(clean_audit_log):
    """`_truncate_for_audit` recurses into dicts, so the cap covers `filters` too — proven end to
    end against a REAL rejected `filters.entity` value, not just a synthetic dict handed straight
    to the truncation helper."""
    conn, fx = clean_audit_log
    svc = make_service(fx, conn, fx.STEWARD, audit=AuditWriter(conn))
    overflow = 100
    huge = "y" * (MAX_ARG_CHARS + overflow)
    with pytest.raises(ValueError, match="filters.entity too long"):
        svc.search("ok query", filters={"entity": huge})

    rows = _rows_for(conn, fx.STEWARD)
    assert len(rows) == 1
    stored_entity = rows[0]["args"]["filters"]["entity"]
    assert stored_entity.endswith(f"...[truncated {overflow} chars]")
    assert len(stored_entity) < len(huge)
    # the co-traveling, well-within-limit `query` string is untouched by the truncation pass
    assert rows[0]["args"]["query"] == "ok query"


# ── `filters` dict KEYS are as client-controlled as its values — an oversized KEY must never
# reach the JSONB row unbounded, whichever guard is the one that actually rejects the call. ─────
def test_oversized_filters_key_in_the_audit_row_is_also_truncated(clean_audit_log):
    conn, fx = clean_audit_log
    svc = make_service(fx, conn, fx.STEWARD, audit=AuditWriter(conn))
    overflow = 100
    huge_key = "z" * (MAX_ARG_CHARS + overflow)
    with pytest.raises(ValueError, match="too long"):
        # `check_arg_length` bounds filter KEYS too, before `search_arms` ever runs — so THIS is
        # the rejection that fires, ahead of `search.search_arms`'s own unknown-filter-name guard
        # `_filter_clause` would otherwise have raised second. Either way, it is the SAME `_call`
        # seam's `finally` block that shapes the audit row, which is what the protection asserted
        # below actually guards — unaffected by WHICH guard rejected the call first.
        svc.search("ok query", filters={huge_key: "irrelevant value"})

    rows = _rows_for(conn, fx.STEWARD)
    assert len(rows) == 1
    stored_filters = rows[0]["args"]["filters"]
    assert len(stored_filters) == 1
    (stored_key,) = stored_filters.keys()
    assert stored_key.endswith(f"...[truncated {overflow} chars]")
    assert stored_key.startswith("z" * MAX_ARG_CHARS)
    assert len(stored_key) < len(huge_key)


def test_a_normal_sized_call_audit_row_is_not_truncated(clean_audit_log):
    """Regression guard: `_truncate_for_audit` must be a complete no-op for ordinary-sized args —
    the exact full-args assertion `test_brainservice_search_writes_an_ok_audit_row_with_full_args`
    already makes above, repeated here with a `filters` dict too (the recursive case) to prove the
    dict-recursion branch itself doesn't mutate an already-compliant value."""
    conn, fx = clean_audit_log
    svc = make_service(fx, conn, fx.STEWARD, audit=AuditWriter(conn))
    svc.search("quarterly revenue", filters={"entity": "initech"})

    rows = _rows_for(conn, fx.STEWARD)
    assert rows[0]["args"] == {"query": "quarterly revenue", "filters": {"entity": "initech"},
                               "max_results": 5, "include_superseded": True}


# ── `_audit_hint_keys` caps by COUNT, not just by length ───────────────────────────────────────
# Pure unit tests — no Postgres needed, same posture as the length/key truncation above being
# testable directly against `_truncate_for_audit`.
def test_audit_hint_keys_returns_every_key_sorted_when_at_or_under_the_cap():
    hints = {f"k{i}": "v" for i in range(MAX_AUDIT_HINT_KEYS)}
    assert _audit_hint_keys(hints) == sorted(hints)


def test_audit_hint_keys_caps_at_max_and_appends_a_countable_marker():
    hints = {f"k{i}": "v" for i in range(MAX_AUDIT_HINT_KEYS + 8)}
    keys = _audit_hint_keys(hints)
    assert len(keys) == MAX_AUDIT_HINT_KEYS + 1              # the cap, plus the marker itself
    assert keys[:MAX_AUDIT_HINT_KEYS] == sorted(hints)[:MAX_AUDIT_HINT_KEYS]
    assert keys[-1] == "...[8 more keys]"


def test_audit_hint_keys_of_none_is_empty():
    assert _audit_hint_keys(None) == []


def test_audit_hint_keys_ignores_a_non_dict_hints_rather_than_iterating_it():
    """A list (or any non-dict) must contribute NOTHING rather than being iterated as a sequence
    of characters/elements — a caller who somehow got a list past the tool boundary must not have
    it silently reinterpreted as hint key names."""
    assert _audit_hint_keys(["type", "title", "entity"]) == []
    assert _audit_hint_keys("not-a-dict-either") == []


# ── `_truncate_for_audit`'s recursion-depth marker ─────────────────────────────────────────────
def test_truncate_for_audit_replaces_a_too_deeply_nested_value_with_a_marker():
    """Both the KEY and the VALUE at a given recursion level are run through
    `_truncate_for_audit` at the SAME depth (it is the one helper that truncates dict keys too) —
    so once the depth guard trips, the marker replaces both the key and the value at that level
    in the same step, one dict `{"...[nested too deep]": "...[nested too deep]"}`, rather than a
    key surviving one level deeper than its value."""
    nested: dict = {"x": "bottom"}
    for _ in range(MAX_AUDIT_DEPTH + 5):
        nested = {"x": nested}
    out = _truncate_for_audit(nested)

    # walk down by taking the (always exactly one) value at each level until the marker appears
    cursor = out
    marker_seen = False
    for _ in range(MAX_AUDIT_DEPTH + 6):
        if not isinstance(cursor, dict):
            marker_seen = cursor == "...[nested too deep]"
            break
        (key, value), = cursor.items()
        if key == "...[nested too deep]":
            marker_seen = True
            break
        cursor = value
    assert marker_seen, "the deeply-nested value was never replaced by the depth marker"


def test_truncate_for_audit_leaves_an_ordinary_shallow_value_untouched():
    shallow = {"filters": {"entity": "initech"}, "hints": {"title": "t"}}
    assert _truncate_for_audit(shallow) == shallow


def test_truncate_for_audit_depth_marker_keeps_jsonb_serialization_bounded(clean_audit_log):
    """The end-to-end proof the unit test above motivates: a deeply-nested `filters` value must
    not crash the real audit write (RecursionError / an unserializable Jsonb payload) — it lands
    as the marker, and the row is written successfully."""
    conn, fx = clean_audit_log
    svc = make_service(fx, conn, fx.STEWARD, audit=AuditWriter(conn))
    nested: dict = {"leaf": "x"}
    for _ in range(MAX_AUDIT_DEPTH + 10):
        nested = {"nested": nested}

    with pytest.raises(ValueError, match="unknown filter"):
        svc.search("q", filters={"bogus": nested})   # rejected for the unknown filter NAME anyway

    rows = _rows_for(conn, fx.STEWARD)
    assert len(rows) == 1   # the write succeeded — no RecursionError, no unbounded JSONB row
    assert "...[nested too deep]" in str(rows[0]["args"])


# ── ask's audit_log.result: the per-tool outcome summary, never a transcript ───────────────────
def test_ask_audit_row_carries_the_outcome_summary_never_the_question_or_answer(clean_audit_log):
    """The negative half, asserted end to end: write a row from an `ask` call whose question is a
    DISTINCTIVE string, then grep the stored `result` column for it and require zero matches —
    `result` may carry `{refused, suppressed, verdict, citations, retried}`, never the question
    text and never the answer's own prose.

    Reuses the module's own `indexed`/`fx` fixture (module-scoped, shared with every other test in
    this file) rather than rebuilding the index against a different corpus — `index.build.rebuild`
    drops and recreates `pages_index`, which would silently break every OTHER test in this file
    that runs after this one if this test built its own corpus on the same connection."""
    import asyncio

    from stigmergy.answer.service import AnswerService, audit_summary
    from stigmergy.server.settings import Settings

    conn, fx = clean_audit_log
    settings = Settings(identity="steward@example.com", identities_path=fx.identities_path,
                       llm="fake")
    from stigmergy.index.backends.embedder import build_embedder
    from stigmergy.server.service import BrainService
    svc = BrainService(settings, conn, build_embedder("fake"), audiences=None,
                       identity="steward@example.com", audit=AuditWriter(conn))

    question = "XYZZY-QUESTION-MARKER-9f3c what is the arr-usd for initech?"

    async def run():
        return await AnswerService(svc).ask(question)

    result = asyncio.run(svc.call_async("ask", {"question": question}, run,
                                        summarize=audit_summary))
    assert result["answer_markdown"]   # sanity: this really did answer something

    with conn.cursor() as cur:
        cur.execute("SELECT args, result FROM audit_log WHERE tool = 'ask' ORDER BY id DESC LIMIT 1")
        args, stored_result = cur.fetchone()

    assert stored_result is not None
    # A CLOSED key set, deliberately: this column's whole contract is that it carries an outcome
    # shape and nothing else, so a field added to `ask`'s response reaches the log only by being
    # named here on purpose. `first_verdict` was added that way — the first draft's verdict is the
    # only record of what a corrective retry was FOR.
    assert set(stored_result) == {"refused", "suppressed", "verdict", "first_verdict",
                                  "citations", "retried"}
    # …and it is subject to the same reduction as `verdict`: COUNTS, never the problem strings,
    # which embed up to 80 characters of the drafted citation quote.
    assert all(isinstance(v, int) for k, v in stored_result["first_verdict"].items()
               if k != "verdict"), stored_result["first_verdict"]
    result_text = str(stored_result)
    assert "XYZZY-QUESTION-MARKER-9f3c" not in result_text   # the question text never lands here
    assert "512000" not in result_text                        # nor the answer's own figure
    # `args` DOES carry the question (that is `_audit_args`'s own, separate, already-tested
    # contract) — this test's whole point is that `result` is a different, narrower column.
    assert "XYZZY-QUESTION-MARKER-9f3c" in str(args)


# ── the verdict travels as SHAPE, never drafted-answer text ────────────────────────────────────
def test_ask_audit_row_never_carries_a_citation_problems_drafted_quote(clean_audit_log, monkeypatch):
    """`verify_answer.check_citations` embeds up to 80 characters of the DRAFTED ANSWER's own
    citation quote into its problem string (`f"citation quote not found in {path}: {quote[:80]!r}"`),
    and `audit_summary` used to write `result["verdict"]` wholesale — landing that drafted text in
    `audit_log.result` on the routine partial/suppressed path, no attacker required. `verdict`
    must travel as SHAPE (`{"verdict": label, "unverified_figures": count, "citation_problems":
    count}`), never as the lists of problem STRINGS `verify()` computes for the answering loop's
    own internal use."""
    import asyncio
    import types

    import stigmergy.answer.service as service_mod
    from stigmergy.answer.service import AnswerService, audit_summary
    from stigmergy.answer.synthesize import AnswerOutput, Citation
    from stigmergy.server.settings import Settings

    conn, fx = clean_audit_log
    settings = Settings(identity="steward@example.com", identities_path=fx.identities_path,
                       llm="fake")
    from stigmergy.index.backends.embedder import build_embedder
    from stigmergy.server.service import BrainService
    svc = BrainService(settings, conn, build_embedder("fake"), audiences=None,
                       identity="steward@example.com", audit=AuditWriter(conn))

    fabricated_quote = "the drafted answer's own smuggled figure was FABRICATED-QUOTE-7f00e1"

    class Scripted:
        async def run(self, prompt, *, deps=None, usage_limits=None, message_history=None):
            deps.record(deps.service.page_text(fx.OPEN_PAGE, deps))
            out = AnswerOutput(
                answer_markdown="Initech had a fine quarter.",
                citations=[Citation(path=fx.OPEN_PAGE, quote=fabricated_quote)])  # not in the page
            usage = types.SimpleNamespace(input_tokens=0, output_tokens=0, cache_read_tokens=0,
                                          details={})
            return types.SimpleNamespace(output=out, usage=usage, all_messages=lambda: [])

    monkeypatch.setattr(service_mod, "build_synthesizer", lambda settings: Scripted())

    async def run():
        return await AnswerService(svc).ask("how did initech do?")

    result = asyncio.run(svc.call_async("ask", {"question": "how did initech do?"}, run,
                                        summarize=audit_summary))
    # sanity: this really did hit the citation-problem path the fix targets
    assert "citation quote not found" in " ".join(result["verdict"]["citation_problems"])

    with conn.cursor() as cur:
        cur.execute("SELECT result FROM audit_log WHERE tool = 'ask' ORDER BY id DESC LIMIT 1")
        stored_result = cur.fetchone()[0]

    assert stored_result is not None
    assert "FABRICATED-QUOTE-7f00e1" not in str(stored_result)
    assert isinstance(stored_result["verdict"], dict)
    assert stored_result["verdict"]["citation_problems"] == 1
    assert isinstance(stored_result["verdict"]["unverified_figures"], int)


# ── the verbatim-args exemption is STATED, not merely true by accident ─────────────────────────
def test_the_audit_docstring_states_the_verbatim_args_exemption():
    """`audit.py`'s own module docstring is where the exemption is stated: `ask.question` and
    `search_brain.query` are the two deliberately-verbatim `args` fields, and the docstring names
    them and says why. A doc-content assertion rather than a behavioral one on purpose — the
    VALUES themselves are already proven verbatim-and-bounded by every OTHER test in this file
    (`test_truncate_for_audit_*`); this one protects the fact that the exemption is DOCUMENTED,
    which a future edit could silently drop without breaking any of those."""
    from stigmergy.server import audit

    doc = (audit.__doc__ or "").lower()
    assert "exemption" in doc
    assert "verbatim" in doc
    assert "ask" in doc and "question" in doc
    assert "search_brain" in doc and "query" in doc


