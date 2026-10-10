"""`ask` over the real MCP protocol: spawn the `stigmergy-server` console entry point and drive
the answering loop end-to-end with the fake synthesizer (ANSWER_LLM=fake, keyless).
The response must carry the answer, citations, confidence, the verdict object and the index's
built_at. Skips without postgres."""
import asyncio

from tests.server.conftest import call_json, mcp_session


def _run(coro):
    return asyncio.run(coro)


def test_ask_is_served_over_stdio_with_a_verdict(indexed, monkeypatch):
    _, fx = indexed
    monkeypatch.setenv("ANSWER_LLM", "fake")     # the spawned subprocess inherits this

    async def go():
        async with mcp_session(fx, fx.STEWARD) as session:
            names = {t.name for t in (await session.list_tools()).tools}
            assert "ask" in names                # mounted alongside the read tools
            res = await call_json(session, "ask", question="what is the arr-usd for initech?")
            return res

    res = _run(go())
    assert res["refused"] is False
    assert "512000" in res["answer_markdown"]
    assert res["citations"] and res["citations"][0]["path"] == fx.OPEN_PAGE
    assert res["confidence"]
    assert res["verdict"]["verdict"] == "verified"
    assert set(res["verdict"]) == {"verdict", "unverified_figures", "citation_problems"}
    assert res["built_at"]                       # index metadata rides along, as in search_brain
    # Operator telemetry never reaches the wire: `audit_summary` records the token counts inside
    # `call_async`, and the ask closure pops them before serializing (`mcp_server.py`) — this
    # asserts the pop a green suite would otherwise never miss.
    assert "usage" not in res


def test_ask_refuses_the_unanswerable_over_stdio(indexed, monkeypatch):
    _, fx = indexed
    monkeypatch.setenv("ANSWER_LLM", "fake")

    async def go():
        async with mcp_session(fx, fx.STEWARD) as session:
            return await call_json(session, "ask",
                                   question="what is our office plant watering policy?")

    res = _run(go())
    assert res["refused"] is True and res["answer_markdown"] == ""
    assert res["verdict"]["verdict"] == "verified"     # a clean refusal is verified behavior
    assert "research" not in res["reason"].lower() and "ingest" not in res["reason"].lower()


def test_same_question_two_identities_ask_get_indistinguishable_refusal(indexed, monkeypatch):
    """The canonical no-existence-leak property, over the REAL MCP protocol — same composition
    and pattern as
    test_mcp_harness.test_same_question_two_identities_get_different_realities, applied to `ask`.
    identity A (finance) gets the cited answer for the ACL'd Acme page; identity B (eng), asking the
    SAME question, gets a refusal that is byte-shape-identical — same fields, same wording
    TEMPLATE — to the refusal for a question that matches nothing in the brain at all. Only
    question-derived content may differ, so B cannot tell whether the page exists and is hidden or
    does not exist. tests/answer/test_existence_leak.py proves this at the service level (where the
    guarantee actually lives); this test proves the same property survives the stdio/JSON boundary."""
    _, fx = indexed
    monkeypatch.setenv("ANSWER_LLM", "fake")
    q = "what is the total-compensation for acme?"

    async def go():
        async with mcp_session(fx, fx.ANA) as a:     # finance: sees the acme page
            a_res = await call_json(a, "ask", question=q)
        async with mcp_session(fx, fx.ENG) as b:      # eng: the page exists but is out of scope
            hidden = await call_json(b, "ask", question=q)
            nothing = await call_json(b, "ask",
                                      question="what is the flux-capacitor rating for time-machine-9?")
        return a_res, hidden, nothing

    a_res, hidden, nothing = _run(go())
    # A sees it fully
    assert a_res["refused"] is False
    assert "750000" in a_res["answer_markdown"]
    assert a_res["citations"] and a_res["citations"][0]["path"] == fx.ACME_PAGE
    # B lives in a reality without it — every structural field matches the nothing-matches refusal
    for field in ("refused", "answer_markdown", "citations", "confidence", "suppressed"):
        assert hidden[field] == nothing[field], f"{field} differs — existence leak over MCP"
    assert hidden["refused"] is True and hidden["answer_markdown"] == ""
    assert hidden["verdict"] == nothing["verdict"]
    # the reason is composed server-side from what THIS run searched/surfaced —
    # the existence-leak property is that the case selection and surfaced TITLES never name the
    # acme page or its content, not that the wording is byte-identical (surfaced titles may
    # legitimately differ between two different questions over the same visible pages).
    assert hidden["refusal_case"] == nothing["refusal_case"] == "no_match"
    assert fx.ACME_PAGE not in hidden["reason"] and "acme" not in hidden["reason"].replace(q, "")
    assert "750000" not in hidden["reason"]
    assert not any("total-compensation" in title.lower() or "acme" in title.lower()
                  for title in hidden["surfaced"])
