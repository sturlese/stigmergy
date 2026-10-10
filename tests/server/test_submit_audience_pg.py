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


def test_scoped_principal_cannot_capture_in_a_layer_they_are_not_a_member_of(indexed):
    with pytest.raises(CaptureError, match="not a member of"):
        service(indexed, ANA).submit(text="Engineering plan.", audience=["eng"])


def test_a_capture_files_in_exactly_one_layer(indexed):
    with pytest.raises(CaptureError, match="exactly one layer"):
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


def test_reading_everything_does_not_grant_publishing_where_not_a_member(indexed):
    with pytest.raises(CaptureError, match="not a member of"):
        service(indexed, STEWARD).submit(text="Engineering note.", audience=["eng"])


def test_every_identity_may_name_public(indexed):
    receipt = service(indexed, ANA).submit(text="Open note.", audience=["public"])
    assert stored_acl(indexed, receipt["id"]) is None


def test_omitted_audience_uses_the_principals_default_layer(indexed):
    finance = service(indexed, ANA).submit(text="Uses Ana's default.")
    assert stored_acl(indexed, finance["id"]) == ["finance"]


def test_without_a_default_layer_a_capture_must_name_one(indexed):
    conn, _fixture = indexed
    with conn.cursor() as cursor:
        cursor.execute("UPDATE roster_principals SET default_audience = NULL WHERE subject = %s", (STEWARD,))
    try:
        with pytest.raises(CaptureError, match="no default layer"):
            service(indexed, STEWARD).submit(text="Where does this go?")
    finally:
        with conn.cursor() as cursor:
            cursor.execute("UPDATE roster_principals SET default_audience = '{public}' WHERE subject = %s",
                           (STEWARD,))


def test_empty_or_scalar_audience_is_refused(indexed):
    with pytest.raises(CaptureError, match="names no layer"):
        service(indexed, ENG).submit(text="Ambiguous.", audience=[])
    with pytest.raises(CaptureError, match=r'\["finance"\]'):
        service(indexed, ANA).submit(text="Wrong shape.", audience="finance")


@pytest.mark.parametrize("audience", [["all"], ["fin\nance"], ["*"], ["brain-admins"]])
def test_invalid_layer_names_are_refused_at_the_capture_boundary(indexed, audience):
    with pytest.raises(CaptureError):
        service(indexed, STEWARD).submit(text="Invalid audience.", audience=audience)


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
