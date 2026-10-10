import pytest
from pydantic import ValidationError

from stigmergy.knowledge.plan import FilingPlan, PageMutation


def _create(**changes):
    values = {
        "action": "create",
        "role": "concept",
        "title": "Harness Engineering",
        "body": "# Harness Engineering\n\nDurable knowledge.",
        "reason": "The source establishes durable knowledge.",
    }
    values.update(changes)
    return values


def _entity(**changes):
    values = _create(
        role="entity",
        title="Northstar Research",
        body="# Northstar Research\n\nA research organization.",
        entity_type="organization",
    )
    values.update(changes)
    return values


def test_creating_an_entity_requires_its_entity_type():
    with pytest.raises(ValidationError, match="requires its entity type"):
        PageMutation(**_entity(entity_type=None))

    mutation = PageMutation(**_entity())

    assert mutation.role == "entity"
    assert mutation.entity_type == "organization"
    assert mutation.aliases is None


def test_entity_aliases_are_optional_verbatim_names_on_create():
    assert PageMutation(**_entity(aliases=("Northstar", "NSR"))).aliases == ("Northstar", "NSR")
    assert PageMutation(**_entity(aliases=())).aliases == ()


@pytest.mark.parametrize("field", [{"entity_type": "organization"}, {"aliases": ("Alias",)}])
def test_notes_and_concepts_never_carry_entity_fields(field):
    with pytest.raises(ValidationError, match="only entity pages carry"):
        PageMutation(**_create(**field))
    with pytest.raises(ValidationError, match="only entity pages carry"):
        PageMutation(**_create(role="note", **field))


def test_alias_null_and_empty_have_distinct_update_semantics():
    values = {
        "action": "update",
        "path": "wiki/entities/Northstar Research.md",
        "body": "# Northstar Research\n\nUpdated knowledge.",
        "reason": "The source updates the entity.",
    }

    assert PageMutation(**values).aliases is None
    assert PageMutation(**values, aliases=()).aliases == ()
    assert PageMutation(**values, aliases=("NSR",)).aliases == ("NSR",)


def test_delete_accepts_only_a_path_and_reason():
    deleted = PageMutation(
        action="delete",
        path="wiki/entities/Northstar Research.md",
        reason="The page is redundant.",
    )

    assert deleted.entity_type is None and deleted.aliases is None
    for field in ({"aliases": ()}, {"entity_type": "organization"}, {"body": "# X"}):
        with pytest.raises(ValidationError, match="delete accepts only a path and reason"):
            PageMutation(
                action="delete",
                path="wiki/entities/Northstar Research.md",
                reason="The page is redundant.",
                **field,
            )


@pytest.mark.parametrize(
    "aliases",
    [
        ("Acme", "ACME"),
        ("ＮＯＲＴＨＳＴＡＲ　Ｒ", "northstar r"),
        ("!!!",),
        ("Northstar", "…"),
    ],
)
def test_aliases_must_be_unique_after_normalization_and_searchable(aliases):
    with pytest.raises(ValidationError, match="unique searchable names"):
        PageMutation(**_entity(aliases=aliases))


def test_unicode_variant_aliases_are_accepted_verbatim():
    mutation = PageMutation(**_entity(aliases=("ＮＯＲＴＨＳＴＡＲ　Ｒ", "Pedro García")))

    assert mutation.aliases == ("ＮＯＲＴＨＳＴＡＲ　Ｒ", "Pedro García")


def test_filing_plan_has_no_entity_proposal_channel():
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        FilingPlan(summary="Recorded an organization", entities=())
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        PageMutation(**_create(entities=()))


def test_native_output_schema_describes_filing_and_entity_intent():
    schema = FilingPlan.model_json_schema()
    mutation = schema["$defs"]["PageMutation"]["properties"]

    assert "Plain-English" in schema["properties"]["summary"]["description"]
    assert "Required when creating an entity page" in mutation["entity_type"]["description"]
    assert "Update null preserves existing aliases" in mutation["aliases"]["description"]
    assert "tolerated but ignored" in mutation["role"]["description"]
    assert "entities" not in mutation
    assert "entities" not in schema["properties"]
    assert schema["$defs"]["PageMutation"]["required"] == ["action", "reason"]


def test_a_plan_holds_a_note_a_concept_and_every_entity_of_a_busy_meeting():
    mutations = [
        {"action": "create", "role": "note", "title": "Meeting", "body": "# Meeting\n\nBody.", "reason": "note"},
        *(
            {
                "action": "create",
                "role": "entity",
                "title": f"Person {index}",
                "entity_type": "person",
                "body": f"# Person {index}\n\nBody.",
                "reason": "entity",
            }
            for index in range(20)
        ),
    ]

    plan = FilingPlan.model_validate({"summary": "A busy meeting.", "mutations": mutations})

    assert len(plan.mutations) == 21


def test_ledger_text_over_its_length_is_accepted_whole_not_a_rejected_plan():
    from stigmergy.knowledge.plan import RepairMutation, RepairPlan

    plan = FilingPlan(summary="s" * 1500, mutations=(PageMutation(
        action="delete", path="wiki/notes/Old.md", reason="r" * 1500),))
    repair = RepairPlan(summary="s" * 1500, mutations=(RepairMutation(
        path="wiki/notes/Old.md", body="# Old", reason="r" * 1500),))

    assert len(plan.summary) == len(plan.mutations[0].reason) == 1500
    assert len(repair.summary) == len(repair.mutations[0].reason) == 1500


def test_the_schema_the_librarian_receives_is_byte_identical():
    import hashlib
    import json

    from stigmergy.knowledge.plan import RepairPlan

    def fingerprint(model):
        return hashlib.sha256(json.dumps(model.model_json_schema()).encode()).hexdigest()

    assert fingerprint(FilingPlan) == "b47cfe7730691643f0280f83e61d90e230b281c8de59b240b67393e85f190fff"
    assert fingerprint(RepairPlan) == "01a7f4410a59cba1ae2f061143f5f901e123c35bf8135bfacf21245024864060"
