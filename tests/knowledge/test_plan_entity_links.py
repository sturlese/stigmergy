import pytest
from pydantic import ValidationError

from stigmergy.knowledge.plan import FilingPlan, PageMutation


def _mutation(*, entities: tuple[str, ...] | None) -> PageMutation:
    return PageMutation(
        action="create",
        role="note",
        title="Entity relationship",
        body="# Entity relationship\n\nDurable knowledge.",
        entities=entities,
        reason="The source establishes this durable relationship.",
    )


def test_entity_proposal_can_be_linked_by_its_canonical_name():
    plan = FilingPlan(
        summary="Recorded the organization relationship",
        entities=({"name": "Northstar Research", "entity_type": "organization"},),
        mutations=(_mutation(entities=("northstar research",)),),
    )

    assert plan.entities[0].name == "Northstar Research"


def test_entity_proposal_can_be_linked_by_a_unicode_normalized_alias():
    plan = FilingPlan(
        summary="Recorded the organization relationship",
        entities=(
            {
                "name": "Northstar Research",
                "entity_type": "organization",
                "aliases": ("ＮＯＲＴＨＳＴＡＲ　Ｒ",),
            },
        ),
        mutations=(_mutation(entities=("northstar r",)),),
    )

    assert plan.entities[0].aliases == ("ＮＯＲＴＨＳＴＡＲ　Ｒ",)


def test_unlinked_entity_proposal_is_rejected():
    with pytest.raises(ValidationError, match="must be linked"):
        FilingPlan(
            summary="Recorded a page without an entity relationship",
            entities=({"name": "Northstar Research", "entity_type": "organization"},),
            mutations=(_mutation(entities=()),),
        )


def test_mutation_may_reference_an_existing_entity_without_a_proposal():
    plan = FilingPlan(
        summary="Recorded a relationship with a known entity",
        mutations=(_mutation(entities=("ent_0b4e9439-b12d-48bf-a02e-4380065c70b9",)),),
    )

    assert plan.mutations[0].entities == ("ent_0b4e9439-b12d-48bf-a02e-4380065c70b9",)
