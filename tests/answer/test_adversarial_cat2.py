"""ACL non-disclosure across answer surfaces."""
import asyncio

from stigmergy.answer import brain as brain_mod
from stigmergy.answer.brain import AnswerBrain
from stigmergy.answer.service import AnswerService
from tests.answer.conftest import ACME_ID, brain_service


def _brain(conn, fx, identity):
    return AnswerBrain(brain_service(conn, fx, identity))


def test_adversarial_cat2_search_never_lists_an_out_of_scope_page(answer_indexed):
    conn, fx = answer_indexed
    listing = _brain(conn, fx, "eng").search_text("acme payroll total compensation")
    assert fx.ACME_PAGE not in listing
    assert "Acme payroll summary" not in listing


def test_adversarial_cat2_the_benign_twin_in_scope_still_sees_it(answer_indexed):
    """Leak tests without a benign twin can pass by breaking search entirely."""
    conn, fx = answer_indexed
    listing = _brain(conn, fx, "ana").search_text("acme payroll total compensation")
    assert fx.ACME_PAGE in listing


def test_adversarial_cat2_read_by_exact_path_is_absence_not_denial(answer_indexed):
    """Knowing the path must not beat the scope: an out-of-scope read serves the SAME shape an
    unknown page serves — absence, never 'forbidden' (a denial confirms existence)."""
    conn, fx = answer_indexed
    eng = _brain(conn, fx, "eng")
    # The absence string no longer echoes the path at all (it used to, and the evidence ledger
    # recorded it), which makes the two cases identical by construction rather than by matching
    # interpolations.
    assert eng.page_text(fx.ACME_PAGE) == eng.page_text("wiki/notes/no-such-page.md")
    assert eng.page_text(fx.ACME_PAGE) == brain_mod.UNKNOWN_PAGE
    assert eng.get_page(fx.ACME_PAGE) is None


def test_adversarial_cat2_entity_surfaces_hide_out_of_scope_existence(answer_indexed):
    conn, fx = answer_indexed
    eng = _brain(conn, fx, "eng")
    described = eng.service.describe_entity("Acme")
    ghost = eng.service.describe_entity("no-such-entity")
    assert described == ghost
    assert described["found"] is False
    assert ACME_ID not in eng.known_entities()
    assert ACME_ID in _brain(conn, fx, "ana").known_entities()


def test_adversarial_cat2_a_refusal_never_names_what_the_asker_cannot_see(answer_indexed):
    """The composed refusal (`searched`/`surfaced`) is built from ACL-scoped tool returns, so an
    out-of-scope title can never be echoed into the explanation an attacker reads."""
    conn, fx = answer_indexed
    svc = AnswerService(brain_service(conn, fx, "eng"))
    res = asyncio.run(svc.ask("what is the total payroll compensation at acme?"))
    assert res["refused"] is True
    flat = " ".join([res["reason"], *res["surfaced"], *res["searched"]])
    assert "Acme payroll summary" not in flat
    assert fx.ACME_PAGE not in flat


def test_adversarial_cat2_entity_filtered_search_does_not_confirm_existence(answer_indexed):
    """The filter probe: `filters={'entity': 'acme'}` from an identity that cannot see acme's
    only page returns the empty shape a nonexistent entity returns — a hit COUNT of zero for
    both, never an error that distinguishes them."""
    conn, fx = answer_indexed
    svc = brain_service(conn, fx, "eng")
    scoped = svc.search("payroll", filters={"entity": ACME_ID})
    ghost = svc.search("payroll", filters={"entity": "no-such-entity"})
    assert scoped["count"] == 0
    assert scoped["count"] == ghost["count"]
    assert [h["path"] for h in scoped["hits"]] == []
