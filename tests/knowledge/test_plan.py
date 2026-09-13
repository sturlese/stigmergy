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


def test_create_requires_an_explicit_complete_entity_set():
    with pytest.raises(ValidationError, match="explicit entities"):
        PageMutation(**_create())
    with pytest.raises(ValidationError, match="explicit entities"):
        PageMutation(**_create(entities=None))

    assert PageMutation(**_create(entities=())).entities == ()


def test_entity_reference_null_and_empty_have_distinct_mutation_semantics():
    update = PageMutation(
        action="update",
        path="wiki/concepts/Harness Engineering.md",
        body="# Harness Engineering\n\nUpdated knowledge.",
        reason="The source updates the concept.",
    )
    cleared = update.model_copy(update={"entities": ()})
    deleted = PageMutation(
        action="delete",
        path="wiki/concepts/Harness Engineering.md",
        reason="The page is redundant.",
    )

    assert update.entities is None
    assert cleared.entities == ()
    assert deleted.entities is None
    with pytest.raises(ValidationError, match="delete accepts only a path and reason"):
        PageMutation(
            action="delete",
            path="wiki/concepts/Harness Engineering.md",
            entities=(),
            reason="The page is redundant.",
        )


def test_native_output_schema_describes_filing_and_entity_link_intent():
    schema = FilingPlan.model_json_schema()
    mutation = schema["$defs"]["PageMutation"]["properties"]

    assert "Plain-English" in schema["properties"]["summary"]["description"]
    assert "Explicit complete entity anchor set" in mutation["entities"]["description"]
    assert "tolerated but ignored" in mutation["role"]["description"]
    assert "entities" in schema["$defs"]["PageMutation"]["required"]
