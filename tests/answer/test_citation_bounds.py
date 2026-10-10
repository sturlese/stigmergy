"""A quote or citation list past its bound is cut to it, never a reason to discard the answer: the
evidence-only completion has no request left to repair one, and a long verbatim quote lost the
whole recovered answer (a budget refusal on multi-source questions)."""
import asyncio
import types

from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel

from stigmergy.answer.synthesize import (
    MAX_CITATIONS,
    MAX_QUOTE_CHARS,
    AnswerOutput,
    Citation,
    build_evidence_synthesizer,
)
from stigmergy.answer.verify_answer import check_citations
from stigmergy.kernel.llm import ANSWER_MODEL, model_override

PAGE = "wiki/notes/architecture.md"
SENTENCE = ("Three systems do the work and share one managed database: a web app on one host, a remote "
            "MCP server for agent clients on the same host, and a worker on a container platform that "
            "runs the long jobs and writes their results back.")


def test_a_quote_past_its_bound_is_cut_to_a_verbatim_prefix():
    assert len(SENTENCE) > MAX_QUOTE_CHARS
    citation = Citation(path=PAGE, quote=SENTENCE)

    assert citation.quote == SENTENCE[:MAX_QUOTE_CHARS]
    assert check_citations([citation], lambda path: {"title": "Architecture", "body": SENTENCE},
                           {PAGE}) == []


def test_citations_past_their_bound_keep_the_first_ones():
    out = AnswerOutput(answer_markdown="a", citations=[{"path": f"wiki/notes/{n}.md", "quote": "q"}
                                                       for n in range(MAX_CITATIONS + 5)])
    assert [c.path for c in out.citations] == [f"wiki/notes/{n}.md" for n in range(MAX_CITATIONS)]


def test_the_evidence_only_completion_keeps_an_answer_whose_quote_runs_long():
    def complete(_messages, _info):
        return ModelResponse(parts=[TextPart(
            '{"answer_markdown":"Three systems share one database.",'
            f'"citations":[{{"path":"{PAGE}","quote":"{SENTENCE}"}}],"confidence":"medium","refused":false}}')])

    settings = types.SimpleNamespace(llm="openrouter", model=ANSWER_MODEL)
    with model_override(FunctionModel(complete)):
        result = asyncio.run(build_evidence_synthesizer(settings).run("How is it built?", evidence=SENTENCE))

    assert result.output.refused is False
    assert result.output.citations[0].quote == SENTENCE[:MAX_QUOTE_CHARS]
