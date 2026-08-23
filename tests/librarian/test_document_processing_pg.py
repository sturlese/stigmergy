"""The capture-is-the-approval change: a `document` is the fast lane with the source attachment ON — text the client
already holds, filed as a synthesis page beside the verbatim `sources/documents/` part(s). What
this module proves, surface by surface:

- a document row files THROUGH THE REAL WORKER CYCLE as synthesis + `sources/documents/` part(s)
  in one commit — the attachment's second caller, riding `process_item` itself (no new flow), with
  the part stamped under the provenance group, `url:` carrying the submitter's `source_url`, and
  the producer's explicit `id:` on every part;
- a document that claims no provenance files with an empty `url:` — the claim is optional, and
  nothing is invented in its place;
- the text IS the dedup and scan surface: a document carrying a secret bounces WHOLE at the
  material scan, before any agent pass, exactly like every other kind;
- the agent is TOLD the source half is already handled, and an ordinary capture's prompt stays
  empty of that note;
- the outcome contract composes unchanged: a document about a name the registry does not know
  lands whole, in one commit.

Nothing here converts anything: there is no bytes blob, no extraction stage and no OCR — the
client extracted, and the worker reads text.
"""
import hashlib

from stigmergy.capture import schema
from stigmergy.librarian import worker
from tests import adversarial_payloads as payloads
from tests.librarian import support

DOC_TEXT = ("Acme renewal pricing\n"
            "The Acme Corp renewal closed on the terms agreed at the last sync.\n"
            "Captured from a shared document for the record.\n")
DOC_URL = "https://drive.google.com/file/d/TESTID123456/view"
# First line -> the double's title -> slugify + the document attachment's own suffix.
SOURCE_STEM = "acme-renewal-pricing-document"
SOURCE_PATH = f"sources/documents/{SOURCE_STEM}.md"


def _submit_and_process(conn, deps, text: str, **kw):
    item = support.submit_document(conn, deps, text, **kw)
    claimed, result = worker.process_next(conn, deps)
    assert claimed["id"] == item["id"]
    return claimed, result


# ── the benign twin: a document files synthesis + verbatim source part, one commit ──────────────
def test_a_document_capture_files_the_text_beside_the_synthesis(rig, clean_queue):
    env, deps = rig
    item, result = _submit_and_process(clean_queue, deps, DOC_TEXT)

    assert result.status == schema.FILED, result.report.get("summary")
    page_path, sha = result.result_ref.rsplit("@", 1)
    assert page_path.startswith("wiki/notes/")
    assert support.branch_sha(env.bare) == sha
    committed = set(support.changed_paths(env.bare, sha))
    assert {SOURCE_PATH, page_path} <= committed

    src = support.read_filed_page(env.bare, sha, SOURCE_PATH)
    assert "type: source" in src
    assert "source_kind: upload" in src
    assert f'url: "{DOC_URL}"' in src
    assert "tags: [source, document]" in src
    # The provenance group hashes the TEXT — what the gates verified and the reader sees, and the
    # same bytes the evidence key was built from.
    digest = hashlib.sha256(DOC_TEXT.encode("utf-8")).hexdigest()
    assert f'content_hash: "sha256:{digest}"' in src
    # The producer's explicit chain identity, stamped — a one-part chain's id IS the stem,
    # quoted (the `#p<n>` suffix only ever appears on continuation parts).
    assert f'id: "{SOURCE_STEM}"' in src
    assert "The Acme Corp renewal closed on the terms agreed at the last sync." in src

    syn = support.read_filed_page(env.bare, sha, page_path)
    assert f'sources: ["[[{SOURCE_STEM}]]"]' in syn
    assert result.report["source_pages"] == [SOURCE_PATH]


def test_a_document_that_claims_no_provenance_files_with_an_empty_url(rig, clean_queue):
    """`source_url` is the submitter's claim and claiming none is allowed: the part carries
    `url: ""` — the contract's spelling for "the door sent none" — and nothing is invented."""
    env, deps = rig
    _, result = _submit_and_process(clean_queue, deps, DOC_TEXT, source_url="")
    assert result.status == schema.FILED, result.report.get("summary")
    _, sha = result.result_ref.rsplit("@", 1)
    src = support.read_filed_page(env.bare, sha, SOURCE_PATH)
    assert 'url: ""' in src
    assert "source_kind: upload" in src


def test_the_text_is_the_dedup_and_scan_surface(rig, clean_queue):
    """`_pre_agent` runs over the material: a document carrying a secret bounces WHOLE at the
    material scan, before any agent pass — the same terms every other kind gets."""
    _, deps = rig
    poisoned = DOC_TEXT + f"\ntoken: {payloads.GITHUB_PAT}\n"
    _, result = _submit_and_process(clean_queue, deps, poisoned, title="poisoned")
    assert result.status == schema.REJECTED
    assert result.report["reason_code"] == "secret"


# ── the flow note: the agent is TOLD the attachment fact ────────────────────────────────────────
class _RecordingAgent:
    """Wraps the double and records what each pass was handed — the seam that proves the flow
    note reaches the agent on a document capture and stays absent on an ordinary one."""

    def __init__(self, inner):
        self.inner = inner
        # The declared port members, copied from what this wraps. Plain attribute
        # access with NO default: `processing._one_pass` refuses an agent that carries no
        # `structured_ordinary` rather than defaulting it, so a wrapper that swallowed the
        # declaration would silently change which shape of the ordinary flow runs behind it.
        self.structured_ordinary = inner.structured_ordinary
        self.wants_gathered = inner.wants_gathered
        self.flow_notes = []

    def run(self, **kwargs):
        self.flow_notes.append(kwargs.get("flow_note", ""))
        return self.inner.run(**kwargs)


def test_a_document_capture_tells_the_agent_the_source_half_is_handled(rig, clean_queue):
    """A document used to park as 'this reads like a source page' — genre rules that are right
    about a bare document and wrong about one whose source half code already owns. The fact is
    TOLD to the agent rather than left to be inferred; this pins that it actually reaches the
    agent, and that an ordinary capture's prompt stays empty of it."""
    import dataclasses
    env, base_deps = rig
    recording = dataclasses.replace(base_deps, agent=_RecordingAgent(base_deps.agent))
    _, result = _submit_and_process(clean_queue, recording, DOC_TEXT)
    assert result.status == schema.FILED
    assert len(recording.agent.flow_notes) == 1
    assert "SYSTEM NOTE" in recording.agent.flow_notes[0]
    assert "SYNTHESIS" in recording.agent.flow_notes[0]

    # The note is an INVARIANT, not a parameter: an ordinary capture archives too, and is told so.
    support.submit(clean_queue, recording, "An ordinary capture about the Acme renewal.")
    _, result = worker.process_next(clean_queue, recording)
    assert result.status == schema.FILED
    assert "SYNTHESIS" in recording.agent.flow_notes[-1]


# ── births and the outcome contract compose unchanged: it IS the fast lane ─────────────────────
def test_a_document_about_a_new_name_files_the_document_the_note_and_the_newborn_entity(
        rig, clean_queue):
    """A document about a name the registry does not know lands whole, in one commit: the source
    part, the synthesis anchored to the newborn entity, and the entity's own page."""
    env, deps = rig
    material = f"DOUBLE:propose=Umbrella Corp\n{DOC_TEXT}"
    item, result = _submit_and_process(clean_queue, deps, material)
    assert result.status == schema.FILED, result.report.get("summary")
    _, sha = result.result_ref.rsplit("@", 1)
    changed = support.changed_paths(env.bare, sha)
    assert any(p.startswith("sources/documents/") for p in changed)
    assert "wiki/entities/Umbrella Corp.md" in changed
    assert result.report["entities_born"][0]["id"] == "umbrella-corp"
