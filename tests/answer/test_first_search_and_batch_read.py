"""The server runs the first search before the model's first turn, and the agent reads several pages
in one tool call. Both keep the evidence ledger, the verifier, and ACL scoping unchanged. Needs
postgres (skips without it)."""
import asyncio
import types

import pytest

import stigmergy.answer.service as service_mod
from stigmergy.answer.synthesize import (
    READ_BATCH,
    AnswerOutput,
    Citation,
    SynthesisContext,
    read_batch,
)
from tests.answer.conftest import AnswerFixture, brain_service


@pytest.fixture()
def ask_service_scoped(answer_indexed):
    from stigmergy.answer.service import AnswerService
    conn, fx = answer_indexed
    return AnswerService(brain_service(conn, fx, "eng"))


@pytest.fixture()
def fx_hidden_path():
    return AnswerFixture.ACME_PAGE


class _Recording:
    """A synthesizer double that records the prompt and optionally reads pages in one batch."""

    def __init__(self, reads=(), answer=None):
        self.prompts = []
        self.reads = list(reads)
        self.answer = answer

    async def run(self, prompt, *, deps=None, usage_limits=None, message_history=None):
        self.prompts.append(prompt)
        if self.reads:
            read_batch(deps, self.reads)
        out = self.answer(deps) if self.answer else AnswerOutput(refused=True, confidence="low")
        usage = types.SimpleNamespace(requests=1, input_tokens=0, output_tokens=0,
                                      cache_read_tokens=0, details={})
        return types.SimpleNamespace(output=out, usage=usage, all_messages=lambda: [])


def test_the_first_message_carries_the_fenced_first_search(ask_service, monkeypatch):
    synth = _Recording()
    monkeypatch.setattr(service_mod, "build_synthesizer", lambda settings: synth)
    res = asyncio.run(ask_service.ask("what are the roadmap themes?"))
    prompt = synth.prompts[0]
    assert prompt.startswith("what are the roadmap themes?\n\nThe server already ran search()")
    assert "<<<UNTRUSTED-DATA" in prompt
    assert res["searched"] == ["what are the roadmap themes?"]
    assert res["surfaced"], "the first search's hits are recorded as surfaced evidence"


def test_a_page_read_in_a_batch_can_be_cited_and_verified(ask_service, monkeypatch):
    listing = ask_service.brain.search_text("roadmap themes", SynthesisContext(service=ask_service.brain))
    path = listing.split("- ", 1)[1].split("\n", 1)[0]
    body = ask_service.brain.get_page(path)["body"]
    quote = body.split(".")[0][:120].strip()

    def answer(deps):
        return AnswerOutput(answer_markdown="Roadmap themes are recorded.",
                            citations=[Citation(path=path, quote=quote)], confidence="medium")

    synth = _Recording(reads=[path, path], answer=answer)
    monkeypatch.setattr(service_mod, "build_synthesizer", lambda settings: synth)
    res = asyncio.run(ask_service.ask("what are the roadmap themes?"))
    assert res["refused"] is False
    assert res["verdict"]["verdict"] == "verified"


def test_a_batch_reads_at_most_the_cap_and_says_so(ask_service):
    ctx = SynthesisContext(service=ask_service.brain)
    paths = [f"wiki/notes/missing-{n}.md" for n in range(READ_BATCH + 2)]
    text = read_batch(ctx, paths)
    assert text.count("unknown page") == READ_BATCH
    assert f"only the first {READ_BATCH} paths were read" in text
    assert len(ctx.evidence) == READ_BATCH


def test_a_hidden_page_in_a_batch_reads_like_a_missing_one(ask_service_scoped, fx_hidden_path):
    ctx = SynthesisContext(service=ask_service_scoped.brain)
    text = read_batch(ctx, [fx_hidden_path, "wiki/notes/never-existed.md"])
    assert text.split("\n\n") == ["unknown page", "unknown page"]
    assert fx_hidden_path not in ctx.read_paths


def test_the_first_search_is_scoped_to_the_asker(ask_service_scoped, fx_hidden_path, monkeypatch):
    synth = _Recording()
    monkeypatch.setattr(service_mod, "build_synthesizer", lambda settings: synth)
    asyncio.run(ask_service_scoped.ask("total compensation payroll acme"))
    assert fx_hidden_path not in synth.prompts[0]


def test_a_refusal_after_a_wide_first_search_names_three_pages_and_counts_the_rest():
    from stigmergy.answer.service import run_facts_reason

    titles = [f"Page {n}" for n in range(32)]
    reason = run_facts_reason("no_match", "q?", ["q?"], titles)
    assert reason == 'searched "q?", surfaced Page 0, Page 1, Page 2 and 29 more — none of them answer that.'


def test_search_results_list_links_as_readable_paths(ask_service):
    listing = ask_service.brain.search_text("globex quarterly report")
    link_lines = [line for line in listing.splitlines() if line.strip().startswith("links:")]
    assert link_lines and all("wiki/" in line and ".md" in line for line in link_lines)
