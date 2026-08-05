"""The evidence ledger records what the tools RETURNED, never what the model ASKED.

`verify_answer` traces every figure in an answer against `SynthesisContext.evidence_text()`. That
corpus is the whole basis of the product's central claim — "any figure that cannot be traced back
is withheld" — so what is allowed into it is a security property, not a rendering detail.

The three absence paths used to echo their own argument back (`f"no results for: {query}"`,
`f"unknown page: {path}"`, `f"unknown entity: {entity}"`), and the tool wrappers in
`synthesize.py` record whatever the renderer returned. A figure inside a model-chosen query was
therefore "traced" by construction: the model asks for it, the search misses, the echo lands in
evidence, and the verifier confirms the number against the model's own question.

Pure and keyless — the seams are faked at exactly the boundary the renderers call.
"""
from stigmergy.answer.brain import AnswerBrain
from stigmergy.answer.numbers import unverified_figures
from stigmergy.answer.synthesize import SynthesisContext


class _FakeService:
    """Stands in for `BrainService` at the three seams the renderers call. Every lookup misses,
    which is the whole point: the absence path is the one that used to echo."""

    def __init__(self, *, hits=None, page=None, entity=None):
        self._hits = hits if hits is not None else []
        self._page = page
        self._entity = entity

    def search(self, query, filters=None, max_results=None):
        return {"hits": self._hits}

    def read_page(self, path):
        return self._page if self._page is not None else {"error": f"unknown page: {path}"}

    def describe_entity(self, entity):
        return self._entity if self._entity is not None else {"error": f"unknown entity: {entity}"}


# ── the argument must not survive into the ledger ─────────────────────────────────────────────
def test_a_missed_search_records_nothing_the_model_chose():
    """OLD BEHAVIOUR: `return f"no results for: {query}"`. The query is the model's own text, and
    `synthesize.py`'s tool wrapper records the renderer's return value verbatim into evidence."""
    ctx = SynthesisContext(service=None)
    brain = AnswerBrain(_FakeService())

    text = brain.search_text("acme ARR was 42.7M confirmed", ctx)
    ctx.record(text)

    assert "42.7M" not in ctx.evidence_text()
    assert "acme" not in ctx.evidence_text().lower()


def test_an_unknown_page_records_nothing_the_model_chose():
    ctx = SynthesisContext(service=None)
    brain = AnswerBrain(_FakeService())

    ctx.record(brain.page_text("wiki/notes/invented-8100000-eur.md", ctx))

    assert "8100000" not in ctx.evidence_text()
    assert "invented" not in ctx.evidence_text()


def test_an_unknown_entity_records_nothing_the_model_chose():
    ctx = SynthesisContext(service=None)
    brain = AnswerBrain(_FakeService())

    ctx.record(brain.entity_text("acme-holdings-with-99pct-margin", ctx))

    assert "99" not in ctx.evidence_text()
    assert "acme" not in ctx.evidence_text().lower()


def test_a_figure_cannot_be_laundered_through_a_query_the_model_invented():
    """The defect, stated as the property it broke. The agent asks a question containing a number,
    the search misses, and the verifier used to accept that number as traced to evidence."""
    ctx = SynthesisContext(service=None)
    brain = AnswerBrain(_FakeService())
    ctx.record(brain.search_text("what confirms ARR of 42.7M", ctx))

    untraced = unverified_figures("ARR is $42.7M.", ctx.evidence_text())

    assert untraced == ["42.7M"], (
        "a figure the model put in its own failed query is being treated as traced evidence")


# ── the absence shapes stay uniform, which is a second property ───────────────────────────────
def test_every_absence_is_byte_identical_whatever_was_asked_for():
    """A side effect worth pinning: with the argument gone, the absence string can no longer differ
    between "this page does not exist" and "you may not see this page", because it no longer
    differs between any two inputs at all."""
    brain = AnswerBrain(_FakeService())
    assert brain.page_text("wiki/a.md") == brain.page_text("wiki/secret/payroll.md")
    assert brain.entity_text("ghost") == brain.entity_text("vault-corp")
    assert brain.search_text("one thing") == brain.search_text("another thing entirely")


# ── benign twins: a real result still lands in evidence, whole ────────────────────────────────
def test_a_search_that_hits_still_records_what_came_back():
    ctx = SynthesisContext(service=None)
    hits = [{"path": "wiki/kpi.md", "title": "KPI metrics", "snippet": "ARR reached 512000 usd"}]
    brain = AnswerBrain(_FakeService(hits=hits))

    ctx.record(brain.search_text("initech ARR", ctx))

    evidence = ctx.evidence_text()
    assert "512000" in evidence and "wiki/kpi.md" in evidence
    assert unverified_figures("ARR reached 512000 usd.", evidence) == []
    assert ctx.read_paths == {"wiki/kpi.md"}


def test_a_page_that_exists_still_records_its_body():
    ctx = SynthesisContext(service=None)
    page = {"path": "wiki/kpi.md", "title": "KPI metrics", "type": "report", "status": "",
            "entity": ["initech"], "as_of": "2026-01", "supersedes": None, "superseded_by": None,
            "body": "ARR reached 512000 usd this quarter.",
            "links": [], "backlinks": [], "links_note": "", "backlinks_note": ""}
    brain = AnswerBrain(_FakeService(page=page))

    ctx.record(brain.page_text("wiki/kpi.md", ctx))

    assert "512000" in ctx.evidence_text()
    assert ctx.read_paths == {"wiki/kpi.md"}


def test_the_lookup_itself_is_still_a_recorded_fact():
    """`note_query` is the CORRECT channel for "what the model asked" — separate from evidence,
    and the refusal composer only ever quotes it when it is a substring of the asker's own
    question. Closing the evidence echo must not close this."""
    ctx = SynthesisContext(service=None)
    brain = AnswerBrain(_FakeService())

    brain.search_text("acme ARR", ctx)
    brain.entity_text("ghost-corp", ctx)

    assert ctx.searched == ["acme ARR", "ghost-corp"]
