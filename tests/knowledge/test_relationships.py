from stigmergy.knowledge.lint import CorpusPage, _editorial_violations
from stigmergy.knowledge.relationships import source_attributions

SOURCE = "sources/2026/09/00000000-0000-4000-8000-000000000001.md"
OTHER_SOURCE = "sources/2026/09/00000000-0000-4000-8000-000000000002.md"
PAGE = "wiki/concepts/Harness Engineering.md"


def _page(body: str, *, sources=(SOURCE,), page_type="concept", title="Harness Engineering") -> CorpusPage:
    return CorpusPage(
        path=PAGE,
        page_type=page_type,
        title=title,
        acl=None,
        sources=sources,
        text=(
            f"---\nid: page_harness_engineering\ntype: {page_type}\ntitle: {title}\n"
            f"sources:\n" + "".join(f"- {source}\n" for source in sources) + f"---\n{body}"
        ),
    )


def test_source_attributions_collects_every_canonical_local_citation():
    body = (
        f"**Claude Code** is a coding-agent environment. (Source: `{SOURCE}`)\n\n"
        f"- A durable fact. (Source: `{OTHER_SOURCE}`)\n"
        f"- A repeated citation. (Source: `{SOURCE}`)"
    )

    assert source_attributions(body) == frozenset({SOURCE, OTHER_SOURCE})


def test_source_attributions_ignores_noncanonical_citations():
    assert source_attributions("") == frozenset()
    assert source_attributions(f"Claude Code is an environment. (source: `{SOURCE}`)") == frozenset()
    assert source_attributions(f"(Source: {SOURCE})") == frozenset()
    assert source_attributions("(Source: `https://example.com/report`)") == frozenset()
    assert source_attributions("(Source: `wiki/notes/Other.md`)") == frozenset()
    assert source_attributions("(Source: `sources/2026/9/short.md`)") == frozenset()


def test_editorial_lint_requires_only_local_attribution_not_relationship_prose():
    linked = _page(
        f"# Harness Engineering\n\n[[Claude Code]] supports the implementation of harness engineering. "
        f"(Source: `{SOURCE}`)\n\n## Connections\n\n- [[Agent Harness]] - the system this practice designs."
    )
    detached = _page(
        f"# Harness Engineering\n\n[[Claude Code]] supports the implementation of harness engineering.\n\n"
        f"(Source: `{OTHER_SOURCE}`)"
    )

    assert _editorial_violations({PAGE: linked}, frozenset({PAGE})) == []
    codes = sorted(item.code for item in _editorial_violations({PAGE: detached}, frozenset({PAGE})))
    assert codes == ["invalid-local-source-reference", "missing-local-source-attribution"]
    assert not any("relationship" in code for code in codes)


def test_editorial_lint_applies_the_same_minimums_to_entity_pages():
    page = _page("# Claude Code\n\nTODO", page_type="entity", title="Claude Code")

    assert sorted(item.code for item in _editorial_violations({PAGE: page}, frozenset({PAGE}))) == [
        "missing-local-source-attribution",
        "placeholder-knowledge-body",
    ]


def test_editorial_lint_ignores_pages_outside_the_candidate_write():
    page = _page("# Harness Engineering\n\nTODO")

    assert _editorial_violations({PAGE: page}, frozenset({"wiki/notes/Elsewhere.md"})) == []
