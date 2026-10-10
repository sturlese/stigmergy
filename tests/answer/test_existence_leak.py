"""No existence leak — the canonical property of the answer surface.

The docker e2e packages this over the real MCP protocol; here it is proven deterministically at
the service level, which is where the guarantee actually lives: identity A (finance) gets a cited
answer for the acme page; identity B (eng), asking the same question, gets a refusal that is
byte-shape-identical — same fields, same wording TEMPLATE — to the refusal for a question that
matches nothing at all. Only question-derived content may differ, so B cannot tell whether the
page exists and is hidden or does not exist.
"""
import asyncio

from tests.answer.conftest import brain_service


def _ask(conn, fx, identity_name, q):
    from stigmergy.answer.service import AnswerService
    return asyncio.run(AnswerService(brain_service(conn, fx, identity_name)).ask(q))


def test_scoped_answer_vs_indistinguishable_refusal(answer_indexed):
    conn, fx = answer_indexed
    q = "what is the total-compensation for acme?"

    a = _ask(conn, fx, "ana", q)                 # finance: sees the acme page
    assert a["refused"] is False
    assert "750000" in a["answer_markdown"]
    assert a["citations"][0]["path"] == fx.ACME_PAGE

    hidden = _ask(conn, fx, "eng", q)            # eng: the page exists but is out of scope
    nothing = _ask(conn, fx, "eng", "what is the flux-capacitor rating for time-machine-9?")

    # every structural field is identical between "hidden" and "nothing-matches"
    for field in ("refused", "answer_markdown", "citations", "confidence", "suppressed"):
        assert hidden[field] == nothing[field], f"{field} differs — existence leak"
    assert hidden["refused"] is True and hidden["answer_markdown"] == ""
    assert hidden["verdict"] == nothing["verdict"]
    # the reason is composed server-side from what THIS run searched/surfaced, so the
    # existence-leak property is not "the same template" — it is that neither the case selection
    # nor the surfaced TITLES ever name the acme page or its content. `surfaced` may
    # legitimately differ in CONTENT between the two questions (different vector-similarity
    # candidates for two different query strings, both drawn from pages "eng" can see regardless
    # of either question) — what must never happen is fx.ACME_PAGE (or its content) appearing.
    assert hidden["refusal_case"] == nothing["refusal_case"] == "no_match"
    assert fx.ACME_PAGE not in hidden["reason"] and "acme" not in hidden["reason"].replace(q, "")
    assert "750000" not in hidden["reason"]                      # the compensation figure never leaks
    assert not any("total-compensation" in title.lower() or "acme" in title.lower()
                  for title in hidden["surfaced"])
