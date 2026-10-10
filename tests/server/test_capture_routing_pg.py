"""LAY-14..LAY-17: the layer of a capture that names none (spec audience-layers §3.6)."""
import hashlib
import uuid
from types import SimpleNamespace

import pytest

from stigmergy.capture import schema, uploads
from stigmergy.capture.errors import CaptureError
from stigmergy.capture.evidence import MemoryEvidenceStore
from stigmergy.kernel import llm
from stigmergy.server import routing
from stigmergy.server.identity import Principal
from stigmergy.server.service import SLACK_DOOR, BrainService

MULTI = Principal(subject="multi@example.com", display_name="Multi", groups=("finance", "eng"),
                  default_audience=("finance",))
SINGLE = Principal(subject="single@example.com", display_name="Single", groups=("finance",),
                   default_audience=("finance",))
READER = Principal(subject="reader@example.com", display_name="Reader", groups=(),
                   default_audience=("public",))
NO_DEFAULT = Principal(subject="nodefault@example.com", display_name="No default",
                       groups=("finance", "eng"), default_audience=None)


class Decider:
    """A scripted classifier: returns or raises `answer` and records what it was shown."""

    def __init__(self, answer):
        self.answer = answer
        self.calls = []

    def __call__(self, options, state):
        self.calls.append((dict(options), dict(state)))
        if isinstance(self.answer, BaseException):
            raise self.answer
        return dict(self.answer)


@pytest.fixture()
def described(indexed):
    conn, _fixture = indexed
    schema.ensure_capture_schema(conn)
    uploads.ensure_upload_schema(conn)
    with conn.cursor() as cursor:
        cursor.execute("UPDATE roster_groups SET description = 'Budgets, invoices, payroll.' "
                       "WHERE name = 'finance'")
        cursor.execute("UPDATE roster_groups SET description = 'Software, infrastructure, releases.' "
                       "WHERE name = 'eng'")
    yield conn
    with conn.cursor() as cursor:
        cursor.execute("UPDATE roster_groups SET description = '' WHERE name IN ('finance', 'eng')")


def service(conn, principal, *, door="", store=None):
    return BrainService(SimpleNamespace(), conn, None, set(principal.groups), identity=principal.subject,
                        evidence=store or MemoryEvidenceStore(), door=door, principal=principal)


def stored(conn, capture_id):
    with conn.cursor() as cursor:
        cursor.execute("SELECT acl, layer_decision, layer_probabilities FROM capture_queue WHERE id = %s",
                       (capture_id,))
        return cursor.fetchone()


NOT_CALLED = {"finance": 1.0, "eng": 1.0}
NONE = routing.NO_SINGLE_LAYER


def odds(**layers):
    """Scripted classifier probabilities: the named layers, and the rest as no single layer."""
    return {**layers, NONE: round(1 - sum(layers.values()), 4)}


# (case, principal, audience, door, classifier answer, layer, decision, classifier called, probabilities)
LAY_14 = [
    ("1 names a layer", MULTI, ["eng"], "", NOT_CALLED, "eng", "explicit", False, None),
    ("1 names public", MULTI, ["public"], "", NOT_CALLED, "public", "explicit", False, None),
    ("2 fixed by the channel", MULTI, ["eng"], SLACK_DOOR, NOT_CALLED, "eng", "channel", False, None),
    ("3 one candidate", SINGLE, None, "", NOT_CALLED, "finance", "default", False, None),
    ("3 no candidate", READER, None, "", NOT_CALLED, "public", "default", False, None),
    ("4 confident", MULTI, None, "", odds(finance=0.05, eng=0.9), "eng", "classified", True,
     odds(eng=0.9, finance=0.05)),
    ("4 exactly at the threshold", MULTI, None, "", odds(finance=0.2, eng=0.75), "eng", "classified",
     True, odds(eng=0.75, finance=0.2)),
    ("5 just below the threshold", MULTI, None, "", odds(finance=0.2501, eng=0.7499), "finance",
     "default", True, odds(eng=0.7499, finance=0.2501)),
    ("5 a tie", MULTI, None, "", odds(finance=0.5, eng=0.5), "finance", "default", True,
     odds(eng=0.5, finance=0.5)),
    ("5 no single layer fits", MULTI, None, "", odds(finance=0.03, eng=0.02), "finance", "default", True,
     odds(eng=0.02, finance=0.03)),
    ("5 timeout", MULTI, None, "", llm.DecisionFailed("timeout"), "finance", "default", True, None),
    ("5 API error", MULTI, None, "", llm.DecisionFailed("http_502"), "finance", "default", True, None),
    ("5 malformed answer", MULTI, None, "", {"finance": 0.9, NONE: 0.1}, "finance", "default", True, None),
    ("5 never public", MULTI, None, "", {"public": 1.0, "finance": 0.0, "eng": 0.0, NONE: 0.0},
     "finance", "default", True, None),
    ("5 never a layer the caller cannot name", MULTI, None, "",
     {"hr": 1.0, "finance": 0.0, "eng": 0.0, NONE: 0.0}, "finance", "default", True, None),
]


@pytest.mark.parametrize(
    "principal, audience, door, answer, layer, decision, called, probabilities",
    [case[1:] for case in LAY_14],
    ids=[case[0] for case in LAY_14],
)
def test_lay14_resolver_table(described, principal, audience, door, answer, layer, decision, called,
                              probabilities):
    decider = Decider(answer)
    with llm.decision_override(decider):
        receipt = service(described, principal, door=door).submit(
            text="Quarterly platform budget review.", title="Review", audience=audience)

    assert bool(decider.calls) is called
    assert receipt["layer"] == layer
    assert receipt["layer_decision"] == decision
    assert receipt.get("probabilities") == probabilities
    acl, stored_decision, stored_probabilities = stored(described, receipt["id"])
    assert acl == (None if layer == "public" else [layer])
    assert stored_decision == decision
    assert stored_probabilities == probabilities


def test_a_layer_the_caller_may_not_publish_to_is_refused_without_classifying(described):
    decider = Decider(NOT_CALLED)
    with llm.decision_override(decider), pytest.raises(CaptureError, match="not a member of"):
        service(described, SINGLE).submit(text="Engineering plan.", audience=["eng"])
    assert decider.calls == []


def test_without_a_default_layer_nothing_is_classified(described):
    decider = Decider({"finance": 1.0, "eng": 0.0})
    with llm.decision_override(decider), pytest.raises(CaptureError, match="no default layer"):
        service(described, NO_DEFAULT).submit(text="Where does this go?")
    assert decider.calls == []


def test_the_classifier_sees_only_the_callers_own_layers_and_the_capture_itself(described):
    decider = Decider(odds(finance=0.1, eng=0.8))
    text = "Release notes. " * 1000
    with llm.decision_override(decider):
        service(described, MULTI).submit(text=text, title="Release 4.2")

    [(options, state)] = decider.calls
    assert options == {"eng": "Software, infrastructure, releases.", "finance": "Budgets, invoices, payroll.",
                       NONE: routing.NO_SINGLE_LAYER_CRITERIA}
    assert state == {"title": "Release 4.2", "text": text[:routing.STATE_TEXT_CHARS]}


def test_a_layer_without_a_description_is_offered_by_its_name(indexed):
    conn, _fixture = indexed
    decider = Decider(odds(finance=0.1, eng=0.8))
    with llm.decision_override(decider):
        service(conn, MULTI).submit(text="Deploy checklist.")
    assert decider.calls[0][0] == {"eng": "eng", "finance": "finance", NONE: routing.NO_SINGLE_LAYER_CRITERIA}


def test_a_url_capture_is_classified_by_its_title_and_address(described, monkeypatch):
    from stigmergy.capture import fetch

    monkeypatch.setattr(fetch, "fetch_public", lambda url, **_: SimpleNamespace(
        data=b"<html><body>Pricing</body></html>", response_media_type="text/html",
        final_url=url))
    decider = Decider(odds(finance=0.9, eng=0.05))
    with llm.decision_override(decider):
        receipt = service(described, MULTI).submit(url="https://example.com/pricing", title="Pricing page")
    assert decider.calls[0][1] == {"title": "Pricing page", "locator": "https://example.com/pricing"}
    assert receipt["layer"] == "finance"


def _upload(bridge, store, data, media_type, name):
    upload = bridge.create_upload(idempotency_key=str(uuid.uuid4()), sha256=hashlib.sha256(data).hexdigest(),
                                  bytes=len(data), media_type=media_type, original_name=name)
    store.objects[uploads.staging_ref(upload["upload_id"])] = data
    return upload["upload_id"]


def test_a_bridge_text_file_is_classified_by_its_verified_text(described):
    store = MemoryEvidenceStore()
    bridge = service(described, MULTI, store=store)
    upload_id = _upload(bridge, store, b"# Incident review\nThe deploy pipeline failed twice.",
                        schema.MEDIA_MARKDOWN, "incident.md")
    decider = Decider(odds(finance=0.03, eng=0.95))
    with llm.decision_override(decider):
        receipt = bridge.finalize_upload_capture(upload_ids=[upload_id], idempotency_key=str(uuid.uuid4()),
                                                 title="Incident review")

    assert decider.calls[0][1] == {"title": "Incident review", "files": ["incident.md"],
                                   "text": "# Incident review\nThe deploy pipeline failed twice."}
    assert (receipt["layer"], receipt["layer_decision"]) == ("eng", "classified")
    assert receipt["probabilities"] == odds(eng=0.95, finance=0.03)


def test_a_bridge_file_that_needs_a_parser_is_classified_without_its_text(described):
    store = MemoryEvidenceStore()
    bridge = service(described, MULTI, store=store)
    upload_id = _upload(bridge, store, b"%PDF-1.4 opaque", schema.MEDIA_PDF, "q3-budget.pdf")
    decider = Decider(odds(finance=0.3, eng=0.3))
    with llm.decision_override(decider):
        receipt = bridge.finalize_upload_capture(upload_ids=[upload_id], idempotency_key=str(uuid.uuid4()))

    assert decider.calls[0][1] == {"files": ["q3-budget.pdf"]}
    assert (receipt["layer"], receipt["layer_decision"]) == ("finance", "default")


def test_bytes_that_do_not_match_their_digest_never_reach_the_classifier(described):
    store = MemoryEvidenceStore()
    bridge = service(described, MULTI, store=store)
    upload_id = _upload(bridge, store, b"declared text", schema.MEDIA_TEXT, "note.txt")
    store.objects[uploads.staging_ref(upload_id)] = b"swapped text!"
    decider = Decider(odds(finance=0.5, eng=0.5))
    with llm.decision_override(decider), pytest.raises(CaptureError, match="do not match"):
        bridge.finalize_upload_capture(upload_ids=[upload_id], idempotency_key=str(uuid.uuid4()))
    assert decider.calls == []


def test_a_capability_in_the_address_never_reaches_the_classifier(described):
    store = MemoryEvidenceStore()
    bridge = service(described, MULTI, store=store)
    upload_id = _upload(bridge, store, b"Shared notes.", schema.MEDIA_TEXT, "notes.txt")
    decider = Decider(odds(finance=0.1, eng=0.8))
    with llm.decision_override(decider):
        bridge.finalize_upload_capture(upload_ids=[upload_id], idempotency_key=str(uuid.uuid4()),
                                       locator="https://files.example.com/notes.txt?sig=secret")
    assert decider.calls[0][1]["locator"] == "https://files.example.com/notes.txt"


def test_a_door_that_offers_nothing_to_read_files_in_the_default_without_classifying(described):
    decider = Decider(odds(finance=0.0, eng=1.0))
    with llm.decision_override(decider):
        receipt = service(described, MULTI).submit_artifacts(
            artifact_values=((b"Thread.", schema.MEDIA_TEXT, None, None),),
            idempotency_key=str(uuid.uuid4()), audience=None)
    assert decider.calls == []
    assert (receipt["layer"], receipt["layer_decision"]) == ("finance", "default")


def test_a_layer_that_no_longer_exists_is_refused(described):
    ghost = Principal(subject="ghost@example.com", display_name="Ghost", groups=("finance", "eng", "gone"),
                      default_audience=("gone",))
    with pytest.raises(CaptureError, match="does not exist"):
        service(described, ghost).submit(text="Anything.")
    decider = Decider(odds(finance=0.05, eng=0.9))
    with llm.decision_override(decider):
        receipt = service(described, ghost).submit(text="Anything.", audience=["eng"])
    assert decider.calls == []
    assert receipt["layer"] == "eng"


def test_lay16_submissions_report_each_captures_layer_and_decision(described):
    caller = service(described, MULTI)
    with llm.decision_override(Decider(odds(finance=0.03, eng=0.95))):
        classified = caller.submit(text="Kubernetes upgrade plan.")
    named = caller.submit(text="Invoice batch.", audience=["finance"])

    rows = {row["id"]: row for row in caller.submissions(limit=200)["submissions"]}
    assert (rows[classified["id"]]["layer"], rows[classified["id"]]["layer_decision"]) == ("eng", "classified")
    assert (rows[named["id"]]["layer"], rows[named["id"]]["layer_decision"]) == ("finance", "explicit")


def test_lay17_without_a_key_routing_never_calls_the_api(described, monkeypatch):
    def no_network(*args, **kwargs):
        raise AssertionError("the keyless suite must never reach the decisions API")

    monkeypatch.setattr(llm.httpx, "Client", no_network)
    receipt = service(described, MULTI).submit(text="Anything at all.")
    assert (receipt["layer"], receipt["layer_decision"]) == ("finance", "default")
    assert "probabilities" not in receipt
