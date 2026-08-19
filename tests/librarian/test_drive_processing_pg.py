"""ADR 028: the drive flow — conversion at the worker, then the fast lane with the source
attachment ON. What this module proves, surface by surface:

- a drive row files THROUGH THE REAL WORKER CYCLE as synthesis + `sources/drive/` part(s) in one
  commit — the attachment's second caller, riding `process_item` itself (D1: no new flow), with
  the part stamped under the provenance group, `url:` carrying the Drive link (the binary stays
  in Drive), and — D6 — the producer's explicit `id:` on every part;
- the conversion seam refuses honestly, as a NAMED `failed` stage, for each of its causes: a
  drive row with no bytes blob, a converter that raises, an extraction with no text, an
  extraction over the material cap — never an exception loop, never a submitter-blaming report;
- the vision fallback is CODE-decided and bounded (D4): rich text never pays a vision call, thin
  text falls back exactly once when the capability exists, and degrades to the honest refusal or
  the thin text (by the `DRIVE_MIN_TEXT_CHARS` line) when it does not;
- the extracted-text path is hermetic (a `.md` document exercises the `text` hand); the REAL
  `pdftotext` binary is exercised once over a real one-page PDF, with the require-or-fail-in-CI
  posture `require_gitleaks` set, because a conversion suite that silently skips is exactly the
  class of test that rots unnoticed.
"""
import hashlib
import shutil

import pytest

from stigmergy.capture import schema
from stigmergy.kernel import converters
from stigmergy.librarian import processing, worker
from tests import adversarial_payloads as payloads
from tests import testdb
from tests.librarian import support

DOC_TEXT = ("Acme renewal pricing\n"
            "The Acme Corp renewal closed on the terms agreed at the last sync.\n"
            "Captured from a Drive document for the record.\n")
DOC_URL = "https://drive.google.com/file/d/TESTID123456/view"
# First line -> the double's title -> slugify + the drive attachment's own suffix.
SOURCE_STEM = "acme-renewal-pricing-document"
SOURCE_PATH = f"sources/drive/{SOURCE_STEM}.md"


def _drop_and_process(conn, deps, document_bytes: bytes, **kw):
    item = support.submit_drive(conn, deps, document_bytes, **kw)
    claimed, result = worker.process_next(conn, deps)
    assert claimed["id"] == item["id"]
    return claimed, result


# ── the benign twin: a drive drop files synthesis + verbatim source part, one commit ────────────
def test_a_drive_capture_files_the_document_beside_the_synthesis(rig, clean_queue):
    env, deps = rig
    item, result = _drop_and_process(clean_queue, deps, DOC_TEXT.encode("utf-8"))

    assert result.status == schema.FILED
    page_path, sha = result.result_ref.rsplit("@", 1)
    assert page_path.startswith("wiki/notes/")
    assert support.branch_sha(env.bare) == sha
    committed = set(support.changed_paths(env.bare, sha))
    assert {SOURCE_PATH, page_path} <= committed

    src = support.read_filed_page(env.bare, sha, SOURCE_PATH)
    assert "type: source" in src
    assert "source_kind: google-drive" in src
    assert f'url: "{DOC_URL}"' in src
    assert "tags: [source, drive-document]" in src
    # The provenance group hashes the EXTRACTED TEXT (what the gates verified and the reader
    # sees); the original bytes' own hash lives in the manifest and the evidence key.
    digest = hashlib.sha256(DOC_TEXT.encode("utf-8")).hexdigest()
    assert f'content_hash: "sha256:{digest}"' in src
    # D6: the producer's explicit chain identity, stamped — a one-part chain's id IS the
    # stem, quoted (the `#p<n>` suffix only ever appears on continuation parts).
    assert f'id: "{SOURCE_STEM}"' in src
    assert "The Acme Corp renewal closed on the terms agreed at the last sync." in src

    syn = support.read_filed_page(env.bare, sha, page_path)
    assert f'sources: ["[[{SOURCE_STEM}]]"]' in syn
    assert result.report["source_pages"] == [SOURCE_PATH]


def test_the_extracted_text_is_the_dedup_and_scan_surface(rig, clean_queue):
    """`_pre_agent` runs over the EXTRACTED text: a document carrying a secret bounces WHOLE at
    the material scan, before any agent pass — the same terms every other flow gets."""
    _, deps = rig
    poisoned = (DOC_TEXT + f"\ntoken: {payloads.GITHUB_PAT}\n").encode("utf-8")
    _, result = _drop_and_process(clean_queue, deps, poisoned, drive_name="poisoned.md")
    assert result.status == schema.REJECTED
    assert result.report["reason_code"] == "secret"


# ── the conversion seam: every refusal is a NAMED failed stage, honest on the wire ──────────────
def test_a_drive_row_with_no_bytes_blob_fails_at_the_conversion_stage(rig, clean_queue):
    _, deps = rig
    _, result = _drop_and_process(clean_queue, deps, DOC_TEXT.encode("utf-8"),
                                  with_bytes_blob=False)
    assert result.status == schema.FAILED
    assert result.report["stage"] == "conversion"
    assert "re-drop" in result.report["summary"]


def test_a_converter_that_raises_fails_at_the_conversion_stage(rig, clean_queue, monkeypatch):
    _, deps = rig

    def boom(path, method):
        raise RuntimeError("pdftotext rc=1: /private/tmp/leaky-path and a stack of detail")

    monkeypatch.setattr(converters, "extract", boom)
    _, result = _drop_and_process(clean_queue, deps, b"%PDF-1.4 not really a pdf",
                                  drive_name="deck.pdf")
    assert result.status == schema.FAILED
    assert result.report["stage"] == "conversion"
    # The wire sentence names the file and the method — never the exception's own text (R5).
    assert "deck.pdf" in result.report["summary"]
    assert "leaky-path" not in result.report["summary"]


def test_an_extraction_with_no_text_fails_honestly(rig, clean_queue, monkeypatch):
    _, deps = rig
    monkeypatch.setattr(converters, "extract", lambda path, method: {"method": "pdf", "text": " \f "})
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    _, result = _drop_and_process(clean_queue, deps, b"%PDF-1.4 scanned", drive_name="scan.pdf")
    assert result.status == schema.FAILED
    assert result.report["stage"] == "conversion"
    assert "scan.pdf" in result.report["summary"]


def test_an_extraction_over_the_material_cap_fails_honestly(rig, clean_queue, monkeypatch):
    _, deps = rig
    huge = "x" * (schema.MAX_MATERIAL_BYTES + 10)
    monkeypatch.setattr(converters, "extract", lambda path, method: {"method": "text", "text": huge})
    _, result = _drop_and_process(clean_queue, deps, b"whatever", drive_name="big.md")
    assert result.status == schema.FAILED
    assert result.report["stage"] == "conversion"
    assert f"{schema.MAX_MATERIAL_BYTES:,}" in result.report["summary"]


# ── the vision fallback: code-decided, bounded, degrades honestly (D4) ──────────────────────────
def _fake_pdf_path(tmp_path) -> str:
    path = tmp_path / "doc.pdf"
    path.write_bytes(b"%PDF-1.4 stand-in bytes")
    return str(path)


def test_rich_text_never_pays_a_vision_call(tmp_path, monkeypatch):
    def no_vision(path):
        raise AssertionError("vision_extract must not be called for a rich text layer")

    monkeypatch.setattr(converters, "vision_extract", no_vision)
    rich = ("line of real prose, repeated enough to clear the per-page bar. " * 20) + "\f" + \
           ("second page prose, also comfortably over the threshold here. " * 20)
    out = processing._with_vision_fallback(_fake_pdf_path(tmp_path), "pdf", rich, "deck.pdf")
    assert out == rich


def test_thin_text_with_no_key_refuses_below_the_floor(tmp_path, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(processing._ConversionRefused) as ex:
        processing._with_vision_fallback(_fake_pdf_path(tmp_path), "pdf", "x\f y", "deck.pdf")
    assert "scanned PDF" in str(ex.value)
    assert "GEMINI_API_KEY" in str(ex.value)


def test_a_prefixed_vision_model_missing_its_key_refuses_naming_that_key(tmp_path, monkeypatch):
    """A KNOWN provider prefix with no key is the misconfiguration "requeue" can never fix, so
    it must read as unconfigured with the provider's OWN variable named — not as configured
    (rasterize, fail at the provider, tell the submitter to requeue), and not with advice to set
    GEMINI_API_KEY, which could never fix it either. OLD BEHAVIOUR: `vision_configured()`
    returned True for any prefixed model, so this capture burned a rasterization and got a
    requeue loop."""
    monkeypatch.setenv("VISION_MODEL", "openrouter:qwen/qwen3-vl-8b-instruct")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    called = []
    monkeypatch.setattr(converters, "vision_extract",
                        lambda *a, **k: called.append(1))

    with pytest.raises(processing._ConversionRefused) as ex:
        processing._with_vision_fallback(_fake_pdf_path(tmp_path), "pdf", "x\f y", "deck.pdf")

    assert "OPENROUTER_API_KEY" in str(ex.value)
    assert "GEMINI_API_KEY" not in str(ex.value)
    assert called == []                       # never rasterized, never paid


def test_thin_but_present_text_without_key_proceeds(tmp_path, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    thin = "a real sentence of some length that clears the absolute floor easily"
    out = processing._with_vision_fallback(_fake_pdf_path(tmp_path), "pdf", thin, "deck.pdf")
    assert out == thin


def test_thin_text_with_key_takes_the_ocr_and_keeps_the_longer(tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    ocr_text = "the full transcription of the scanned deck, page by page, faithfully " * 5
    monkeypatch.setattr(converters, "vision_extract",
                        lambda path: {"method": "vision", "text": ocr_text, "model": "gemini-test"})
    out = processing._with_vision_fallback(_fake_pdf_path(tmp_path), "pdf", "x\f y", "deck.pdf")
    assert out == ocr_text.strip()


def test_a_failing_vision_call_degrades_to_the_floor_rule(tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")

    def boom(path):
        raise RuntimeError("quota")

    monkeypatch.setattr(converters, "vision_extract", boom)
    with pytest.raises(processing._ConversionRefused):
        processing._with_vision_fallback(_fake_pdf_path(tmp_path), "pdf", "x\f y", "deck.pdf")
    thin = "a real sentence of some length that clears the absolute floor easily"
    assert processing._with_vision_fallback(_fake_pdf_path(tmp_path), "pdf", thin,
                                            "deck.pdf") == thin


def test_non_pdf_methods_never_consider_vision(tmp_path, monkeypatch):
    def no_vision(path):
        raise AssertionError("vision_extract is a PDF fallback only")

    monkeypatch.setattr(converters, "vision_extract", no_vision)
    assert processing._with_vision_fallback(_fake_pdf_path(tmp_path), "sheet", "x", "d.xlsx") == "x"


# ── the real pdftotext hand, once, over a real one-page PDF ─────────────────────────────────────
# A minimal but genuine PDF: one page, two text-drawing operations. Enough for pdftotext to find
# a text layer — which is exactly what the drive flow's primary hand must prove it can read. The
# two lines put the extraction comfortably over `DRIVE_MIN_TEXT_CHARS`, so the e2e below takes
# the thin-but-present road (no vision configured in this suite) rather than the refusal.
_PDF_LINE_1 = "Acme renewal closed in June with the pricing floor agreed at the quarterly sync"
_PDF_LINE_2 = "The renewal terms apply company-wide from July onward per the signed order form"


def _tiny_pdf() -> bytes:
    """A minimal but SPEC-VALID one-page PDF, with a real xref table and trailer. macOS's
    Homebrew poppler forgave the first version's missing xref ("Couldn't find trailer
    dictionary"); Ubuntu's — the CI runner's, and the deployed worker's — refuses it, which is
    exactly the strictness this fixture must survive, so offsets are computed, never typed."""
    stream = (f"BT /F1 12 Tf 72 720 Td ({_PDF_LINE_1}) Tj ET\n"
              f"BT /F1 12 Tf 72 700 Td ({_PDF_LINE_2}) Tj ET\n").encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
         b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>"),
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"endstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for n, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (n, body)
    xref_at = len(out)
    out += b"xref\n0 %d\n" % (len(objects) + 1)
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += (b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n"
            % (len(objects) + 1, xref_at))
    return bytes(out)


def _require_pdftotext():
    if shutil.which("pdftotext"):
        return
    if testdb.required():
        pytest.fail("$STIGMERGY_TEST_DSN is set (CI mode) but pdftotext is not on PATH — refusing "
                    "to skip the drive conversion test silently. Install poppler-utils BEFORE "
                    "the test step (see .github/workflows/ci.yml).")
    pytest.skip("pdftotext not on PATH (brew install poppler) — the drive flow's text-layer hand")


def test_the_real_pdftotext_hand_reads_a_real_pdf(tmp_path):
    _require_pdftotext()
    path = tmp_path / "tiny.pdf"
    path.write_bytes(_tiny_pdf())
    out = converters.extract(str(path), "pdf")
    assert _PDF_LINE_1 in out["text"]
    assert _PDF_LINE_2 in out["text"]


def test_a_real_pdf_drive_capture_files_end_to_end(rig, clean_queue):
    """The full chain with the REAL primary hand: pdf bytes → pdftotext → the fast lane →
    synthesis + `sources/drive/` part. The extracted text's first line is the double's title, so
    the stems here are this PDF's own."""
    _require_pdftotext()
    env, deps = rig
    _, result = _drop_and_process(clean_queue, deps, _tiny_pdf(), drive_name="acme-renewal.pdf")
    assert result.status == schema.FILED
    _, sha = result.result_ref.rsplit("@", 1)
    sources = [p for p in support.changed_paths(env.bare, sha) if p.startswith("sources/drive/")]
    assert len(sources) == 1
    src = support.read_filed_page(env.bare, sha, sources[0])
    assert _PDF_LINE_1 in src
    assert "source_kind: google-drive" in src


# ── the flow note: the agent is TOLD the attachment fact (D7 as amended) ────────────────────────
class _RecordingAgent:
    """Wraps the double and records what each pass was handed — the seam that proves the flow
    note reaches the agent on a drive capture and stays absent on an ordinary one."""

    def __init__(self, inner):
        self.inner = inner
        # The declared port member, copied from what this wraps (ADR 033). Plain attribute
        # access with NO default: `processing._one_pass` refuses an agent that carries no
        # `structured_ordinary` rather than defaulting it, so a wrapper that swallowed the
        # declaration would silently change which shape of the ordinary flow runs behind it.
        # Reading it here means a wrapper around a non-conforming backend fails at
        # CONSTRUCTION, in the test that built it, instead of one queue delivery at a time.
        self.structured_ordinary = inner.structured_ordinary
        self.wants_gathered = inner.wants_gathered
        self.flow_notes = []

    def run(self, **kwargs):
        self.flow_notes.append(kwargs.get("flow_note", ""))
        return self.inner.run(**kwargs)


def test_a_drive_capture_tells_the_agent_the_source_half_is_handled(rig, clean_queue):
    """A drive capture used to park as 'this reads like a source page' — genre rules that are
    right about a bare document and wrong about one whose source half code already owns. The fact
    is TOLD to the agent rather than left to be inferred; this pins that it actually reaches the
    agent, and that an ordinary capture's prompt stays empty of it."""
    import dataclasses
    env, base_deps = rig
    recording = dataclasses.replace(base_deps, agent=_RecordingAgent(base_deps.agent))
    _, result = _drop_and_process(clean_queue, recording, DOC_TEXT.encode("utf-8"))
    assert result.status == schema.FILED
    assert len(recording.agent.flow_notes) == 1
    assert "SYSTEM NOTE" in recording.agent.flow_notes[0]
    assert "SYNTHESIS" in recording.agent.flow_notes[0]

    support.submit(clean_queue, recording, "An ordinary capture about the Acme renewal.")
    _, result = worker.process_next(clean_queue, recording)
    assert result.status == schema.FILED
    assert recording.agent.flow_notes[-1] == ""


# ── parks and the outcome contract compose unchanged (D1: it IS the fast lane) ──────────────────
def test_a_parked_drive_capture_writes_nothing_and_reconverts_on_requeue(rig, clean_queue):
    """ADR 028 D8: "the SAME capture resumes, reusing its stored distillation" is the document
    flow's property, not the fast lane's — a parked drive capture re-runs the agent AND the
    conversion on its next delivery, and files the same set, anchored, once the name resolves.
    The park itself must leave no source part behind."""
    env, deps = rig
    before = support.branch_sha(env.bare)
    material = f"DOUBLE:triage-entity=Umbrella Corp\n{DOC_TEXT}"
    item, result = _drop_and_process(clean_queue, deps, material.encode("utf-8"))
    assert result.status == schema.NEEDS_INPUT
    assert support.branch_sha(env.bare) == before
    assert not any(p.startswith("sources/drive/")
                   for p in support.all_ever_committed_paths(env.bare))
