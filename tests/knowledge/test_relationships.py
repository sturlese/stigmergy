import datetime as dt

from stigmergy.entities.model import EntityRecord, new_name_claim
from stigmergy.knowledge.lint import CorpusPage, _editorial_violations
from stigmergy.knowledge.relationships import has_entity_relationship_evidence

SOURCE = "sources/2026/09/00000000-0000-4000-8000-000000000001.md"
ENTITY_ID = "ent_11111111-1111-4111-8111-111111111111"
PAGE = "wiki/concepts/Harness Engineering.md"


def _record():
    at = dt.datetime(2026, 9, 13, tzinfo=dt.UTC)
    return EntityRecord(
        entity_id=ENTITY_ID,
        entity_type="organization",
        created_at=at,
        updated_at=at,
        claims=(
            new_name_claim(
                "Claude Code",
                kind="preferred",
                acl=None,
                source=SOURCE,
                actor="marc",
                introduced_at=at,
            ),
        ),
    )


def _page(body: str) -> CorpusPage:
    return CorpusPage(
        path=PAGE,
        page_type="concept",
        title="Harness Engineering",
        acl=None,
        entities=(ENTITY_ID,),
        sources=(SOURCE,),
        text=(
            "---\nid: page_harness_engineering\ntype: concept\ntitle: Harness Engineering\n"
            f"sources:\n- {SOURCE}\nentity:\n- {ENTITY_ID}\n---\n{body}"
        ),
    )


def test_entity_relationship_evidence_requires_attribution_in_the_same_paragraph():
    cited = f"**Claude\u202fCode** is a coding-agent environment. (Source: `{SOURCE}`)"

    assert has_entity_relationship_evidence(cited, "Claude Code", (SOURCE,)) is True
    assert has_entity_relationship_evidence(
        f"**Claude Code** is a coding-agent environment.\n\n(Source: `{SOURCE}`)",
        "Claude Code",
        (SOURCE,),
    ) is False
    assert has_entity_relationship_evidence(
        f"**Claude Code** is a coding-agent environment.\n\nUnrelated analysis.\n\n(Source: `{SOURCE}`)",
        "Claude Code",
        (SOURCE,),
    ) is False
    assert has_entity_relationship_evidence(
        "Claude Code is a coding-agent environment. (source: `" + SOURCE + "`)",
        "Claude Code",
        (SOURCE,),
    ) is False


def test_editorial_lint_uses_the_shared_entity_relationship_predicate():
    page = _page(
        f"# Harness Engineering\n\nClaude Code supports the implementation. "
        f"(Source: `{SOURCE}`)"
    )

    valid = _editorial_violations({PAGE: page}, {ENTITY_ID: _record()}, frozenset({PAGE}))
    invalid = _editorial_violations(
        {
            PAGE: _page(
                f"# Harness Engineering\n\nClaude Code supports the implementation.\n\n"
                f"(Source: `{SOURCE}`)"
            )
        },
        {ENTITY_ID: _record()},
        frozenset({PAGE}),
    )

    assert not any(item.code == "entity-without-relationship" for item in valid)
    assert any(item.code == "entity-without-relationship" for item in invalid)
