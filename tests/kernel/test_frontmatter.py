"""`kernel.frontmatter.split_frontmatter` — the tolerant read half of the page contract.

The twin of `index.corpus.split_frontmatter`. Two parsers over one file format is a standing
drift risk, so what one of them learns has to reach the other; this file exists because a lesson
learned in `corpus` had not.
"""
from stigmergy.kernel import frontmatter


def test_a_well_formed_page_splits_into_frontmatter_and_body():
    fm, body = frontmatter.split_frontmatter("---\ntitle: Globex\n---\n\nbody text\n")
    assert fm == {"title": "Globex"}
    assert "body text" in body


def test_a_crlf_page_is_not_a_malformed_one():
    """OLD BEHAVIOUR: `{}` — a well-formed page read as having no frontmatter at all.

    The block regex anchored on bare `\\n`, so `---\\r\\n` matched nothing on a checkout written
    on Windows or normalized by a `.gitattributes` rule. `index.corpus` already carries this fix
    and the reason for it; this parser is its twin and had not.

    It is not cosmetic here either: `entities.generator` reads entity pages through this function,
    so such a page produced `{}` and the generator refused it with "declares no `title`, so it
    names no entity" — blocking `stigmergy-entities regenerate` and, through `mint_via_clone`, the
    governed mint door `review_decide`'s approve reaches.
    """
    fm, body = frontmatter.split_frontmatter("---\r\ntitle: Globex\r\n---\r\n\r\nbody text\r\n")

    assert fm == {"title": "Globex"}
    assert "body text" in body


def test_a_page_with_no_frontmatter_is_all_body():
    fm, body = frontmatter.split_frontmatter("just a body, no frontmatter\n")
    assert fm == {}
    assert body == "just a body, no frontmatter\n"


def test_unparseable_frontmatter_degrades_to_body_only_rather_than_raising():
    """The tolerance this parser promises: a YAML error is `{}` and the whole text as body, never
    an exception. (A caller that makes an ACCESS decision needs more than that — see
    `index.corpus.split_frontmatter_checked`.)"""
    fm, body = frontmatter.split_frontmatter("---\ntitle: x: [unclosed\n---\nfindable body\n")
    assert fm == {}
    assert "findable body" in body


def test_a_frontmatter_block_that_is_a_list_is_no_frontmatter_at_all():
    """OLD BEHAVIOUR: `(['a', 'b'], 'body\\n')` — a LIST handed back where the signature, the
    docstring and every caller promise a dict.

    Valid YAML that is not a mapping parsed cleanly and travelled on, so the type error surfaced
    one frame later at the caller's first `.get`: `evals/run_filing._page_type` crashed on it, and
    `entities.generator` only survived because it re-checked the type by hand. The tolerance this
    parser promises is about the RESULT TYPE too — a block that is not a mapping declares no
    fields, which is the same observation as having no frontmatter.
    """
    fm, body = frontmatter.split_frontmatter("---\n- a\n- b\n---\nbody\n")

    assert fm == {}
    assert body == "body\n"


def test_a_frontmatter_block_that_is_a_scalar_is_no_frontmatter_at_all():
    """OLD BEHAVIOUR: `(42, 'body\\n')` — an int where the contract promises a dict.

    Same defect as the list case, and the shape a truncated or mis-fenced page reaches this
    parser as. `index.corpus.split_frontmatter_checked` — the declared stricter twin — already
    guarded against it; this one did not, and two parsers over one file format only stay honest
    if what one learns reaches the other.
    """
    fm, body = frontmatter.split_frontmatter("---\n42\n---\nbody\n")

    assert fm == {}
    assert body == "body\n"
