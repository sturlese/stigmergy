"""Adversarial category 7 of the seven adversarial payload categories — ingestion robustness,
forged frontmatter: document content must never override server-computed metadata. Permanent, not
a one-off: every case here stays in the suite as the write path evolves.

Deterministic, keyless, model-independent — same posture as `tests/answer/test_adversarial_cat1.py`.
Named `test_adversarial_cat7_*` so a `-k adversarial_cat7` collection (mirroring the cat1
release-gate convention) finds every case.

**What the guarantee actually is**: a pre-drafted page's frontmatter is stored VERBATIM as
material, and any server-owned field it declares is recorded as a flagged hint and nothing else —
attribution and trust NEVER read this dict; `submitted_by` on the row is `BrainService.identity`
(the resolved caller), full stop. That is why the boundary cases below (a miss in the ANNOTATION)
are not a security bypass: there is no code path from `hints.declared_frontmatter`/`flagged` to
the `capture_queue` columns those fields are named after.
"""
import pytest

from stigmergy.capture import schema
from stigmergy.capture.errors import SubmissionRejected
from tests.adversarial_payloads import FORGED_PAGE


def test_adversarial_cat7_forged_frontmatter_is_recorded_never_trusted():
    submission = schema.prepare_submission("page", FORGED_PAGE)
    assert sorted(submission.hints["flagged"]) == ["acl", "content_hash", "submitted_by",
                                                    "verification"]
    assert submission.hints["declared_frontmatter"]["submitted_by"] == "ceo@example.com"
    # the material is archived AS-IS: flagging is annotation, never sanitization
    assert submission.payload["text"] == FORGED_PAGE


def test_adversarial_cat7_a_client_hint_cannot_smuggle_a_server_owned_field_either():
    """The two independent refusal paths named in schema.py's own module docstring: an argument
    AND a hint are both refused loudly; only frontmatter DECLARATION (never an argument/hint) is
    merely recorded."""
    with pytest.raises(SubmissionRejected, match="submitted_by"):
        schema.prepare_submission("raw", "innocuous material", hints={"submitted_by": "ceo@x"})


def test_adversarial_cat7_flagged_fields_never_reach_the_trusted_client_hints_bucket():
    submission = schema.prepare_submission("page", FORGED_PAGE)
    # `client` is the CALLER's own suggestions (type/path/entity/title) — the forged fields must
    # never leak into it, or a consumer reading `hints.client` blindly could be fooled.
    assert set(submission.hints["client"]) <= set(schema.HINT_KEYS)
    assert "submitted_by" not in submission.hints["client"]


def test_adversarial_cat7_no_code_path_from_declared_frontmatter_to_a_server_owned_column():
    """The structural half of the guarantee (ADR 014 §4): `Submission` carries no `submitted_by`
    field at all — `queue.submit` takes attribution as an ARGUMENT the service supplies. There is
    nothing on this dataclass a careless future caller could mistakenly read as attribution."""
    submission = schema.prepare_submission("page", FORGED_PAGE)
    assert not hasattr(submission, "submitted_by")
    assert not hasattr(submission, "acl")
    assert not hasattr(submission, "verification")


# ── a flagged risk: the scan is deliberately NOT YAML, so find the boundary ─────────────────────
def test_adversarial_cat7_boundary_a_quoted_top_level_key_evades_the_flat_scan():
    """A real YAML parser sees `submitted_by` as a top-level key here (quoted keys are valid
    YAML); the deliberately-shallow, non-recursive scanner (`_FM_LINE_RE` requires the key to
    start with a bare letter/underscore, never a quote) does NOT. This is the boundary the
    module's own docstring calls out ('an exotically-quoted... submitted_by: may go unflagged'),
    demonstrated concretely rather than asserted in prose.

    This is a MISS IN THE ANNOTATION, not a security bypass: `flagged` never feeds attribution or
    any other server-owned column, so a missed flag loses a note to the submitter/steward, never
    the structural property that keeps a client from setting a server-owned field."""
    material = '---\n"submitted_by": ceo@example.com\ntype: decision\n---\n\nbody\n'
    declared = schema.declared_frontmatter(material)
    assert "submitted_by" not in declared          # the scan never saw it as a key
    submission = schema.prepare_submission("page", material)
    assert "submitted_by" not in submission.hints["flagged"]
    assert submission.payload["text"] == material   # the material is still stored verbatim regardless


def test_adversarial_cat7_boundary_a_flow_mapping_nests_the_forged_key_out_of_flat_scope():
    """`submitted_by` here is only ever a NESTED key inside a YAML flow mapping (`meta: {...}`) —
    a real YAML parser can still reach it by walking the structure; the flat top-level scan
    records only the outer key `meta` and misses `submitted_by` entirely."""
    material = "---\nmeta: {submitted_by: ceo@example.com, type: decision}\n---\n\nbody\n"
    declared = schema.declared_frontmatter(material)
    assert "submitted_by" not in declared
    assert declared["meta"] == "{submitted_by: ceo@example.com, type: decision}"
    submission = schema.prepare_submission("page", material)
    assert submission.hints["flagged"] == []


def test_adversarial_cat7_a_multiline_plain_scalar_still_gets_flagged_though_the_value_is_partial():
    """Contrast case: a plain (unquoted) top-level `submitted_by:` that CONTINUES onto an indented
    second line (a folded plain scalar in real YAML) is NOT a miss — the scanner still sees the
    first line's `submitted_by: ceo` and flags it (the continuation line starts with whitespace
    and is correctly skipped as a nested/continuation line, never double-recorded as its own
    field). The recorded VALUE is truncated relative to full YAML semantics, but the ANNOTATION
    fires — which is the only thing the annotation is required to do."""
    material = "---\nsubmitted_by: ceo\n  @example.com\ntype: decision\n---\n\nbody\n"
    declared = schema.declared_frontmatter(material)
    assert declared["submitted_by"] == "ceo"        # partial, but present — flagged, not missed
    submission = schema.prepare_submission("page", material)
    assert "submitted_by" in submission.hints["flagged"]
