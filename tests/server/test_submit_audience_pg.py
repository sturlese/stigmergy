import json

import pytest

from stigmergy.capture.errors import CaptureError
from stigmergy.capture.evidence import MemoryEvidenceStore
from tests.server.conftest import Fixture, make_service

STEWARD, ANA, ENG = Fixture.STEWARD, Fixture.ANA, Fixture.ENG


def service(indexed, subject):
    conn, fixture = indexed
    return make_service(fixture, conn, subject, evidence=MemoryEvidenceStore())


def stored_acl(indexed, submission_id):
    conn, _fixture = indexed
    with conn.cursor() as cursor:
        cursor.execute("SELECT acl FROM capture_queue WHERE id = %s", (submission_id,))
        return cursor.fetchone()[0]


def test_scoped_principal_can_capture_for_a_group_they_hold(indexed):
    receipt = service(indexed, ANA).submit(text="Payroll rates for Q3.", audience=["finance"])
    assert stored_acl(indexed, receipt["id"]) == ["finance"]


def test_scoped_principal_cannot_capture_for_a_group_they_cannot_read(indexed):
    with pytest.raises(CaptureError, match="could not read afterwards"):
        service(indexed, ANA).submit(text="Engineering plan.", audience=["eng"])


def test_scoped_principal_must_hold_every_requested_audience(indexed):
    with pytest.raises(CaptureError, match="could not read afterwards"):
        service(indexed, ANA).submit(
            text="Cross-functional plan.",
            audience=["finance", "eng"],
        )


def test_refused_audience_queues_nothing(indexed):
    conn, _fixture = indexed
    with conn.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM capture_queue")
        before = cursor.fetchone()[0]

    with pytest.raises(CaptureError):
        service(indexed, ANA).submit(text="Do not queue.", audience=["eng"])

    with conn.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM capture_queue")
        assert cursor.fetchone()[0] == before


def test_master_can_choose_any_existing_audience(indexed):
    receipt = service(indexed, STEWARD).submit(text="Engineering note.", audience=["eng"])
    assert stored_acl(indexed, receipt["id"]) == ["eng"]


def test_audience_labels_have_one_sorted_persisted_spelling(indexed):
    first = service(indexed, STEWARD).submit(
        text="First ordering.", audience=["eng", "finance"]
    )
    second = service(indexed, STEWARD).submit(
        text="Second ordering.", audience=["finance", "eng"]
    )
    assert stored_acl(indexed, first["id"]) == ["eng", "finance"]
    assert stored_acl(indexed, second["id"]) == ["eng", "finance"]


def test_omitted_audience_uses_the_principals_configured_default(indexed):
    finance = service(indexed, ANA).submit(text="Uses Ana's default.")
    organization_wide = service(indexed, STEWARD).submit(text="Uses the master's default.")

    assert stored_acl(indexed, finance["id"]) == ["finance"]
    assert stored_acl(indexed, organization_wide["id"]) is None


def test_empty_or_scalar_audience_is_refused(indexed):
    with pytest.raises(CaptureError, match="not a request"):
        service(indexed, ENG).submit(text="Ambiguous.", audience=[])
    with pytest.raises(CaptureError, match=r'\["finance"\]'):
        service(indexed, ANA).submit(text="Wrong shape.", audience="finance")


@pytest.mark.parametrize("audience", [["all"], ["fin\nance"], ["*"]])
def test_invalid_group_names_are_refused_at_the_capture_boundary(indexed, audience):
    with pytest.raises(CaptureError):
        service(indexed, STEWARD).submit(text="Invalid audience.", audience=audience)


def test_unknown_group_is_refused_even_for_the_master(indexed):
    with pytest.raises(CaptureError, match="readable by nobody") as error:
        service(indexed, STEWARD).submit(text="Typo.", audience=["finanace"])
    assert "finanace" in str(error.value)
    assert "finance" not in str(error.value).replace("finanace", "")


def test_service_has_no_legacy_acl_or_capture_kind_parameters(indexed):
    with pytest.raises(TypeError):
        service(indexed, STEWARD).submit(text="No ACL override.", acl=["finance"])
    with pytest.raises(TypeError):
        service(indexed, STEWARD).submit(kind="raw", material="Old contract")


def test_scoped_identity_lists_only_its_own_capture_metadata(indexed):
    eng_receipt = service(indexed, ENG).submit(text="Engineering-only material.")
    ana = service(indexed, ANA)
    ana_receipt = ana.submit(text="Finance-only material.")

    result = ana.submissions(limit=200)

    assert result["scope"] == "own"
    assert all(item["submitted_by"] == ANA for item in result["submissions"])
    assert any(item["id"] == ana_receipt["id"] for item in result["submissions"])
    assert all(item["id"] != eng_receipt["id"] for item in result["submissions"])
    assert "Finance-only material" not in json.dumps(result)
