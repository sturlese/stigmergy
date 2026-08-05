"""The fast lane's source ATTACHMENT — a Slack 🧠 capture files a
`sources/slack/` page (the thread, verbatim, code-written) beside the one synthesis page the
agent files, and the synthesis cites it in `sources:`. A parameter on the ordinary flow, never a
third one: same worker cycle, same gates, same one-question budget.

What this module proves, surface by surface:

- the whole set files in ONE commit through the real worker cycle, with the source page stamped
  under the provenance group (`content_hash`/`extracted_at`/`tier`) and the synthesis under the
  fast-lane group — the benign twin every gate must not fire on;
- the MECHANISM fired, not merely an outcome that could have had another cause: the citation is
  on the filed page, the
  report and the commit body both name the source part(s), and the card-facing report carries
  `source_pages`;
- the parameter's OFF position is byte-identical: an ordinary capture files exactly as before,
  with no `sources/` page and no `source_pages` key (the benign twin's other half);
- the anchoring question is STILL ASKED in per-page mode, proven by watching the check go red:
  populating
  `page_declared` for the source pages switches `gate_anchoring` per-page, and an unearned
  anchor claim must keep parking exactly as `test_processing_pg`'s original proves for the
  single-outcome mode;
- a long thread splits into cross-linked parts through the SAME writer the meeting flow uses
  (`_build_source_parts`, the one writer both flows share);
- a recaptured thread whose title lands on an existing source stem is refused, never silently
  overwritten (`open_for_new`'s O_EXCL discipline, checked before the first byte).

The trigger (`hints.client.source_client == "slack"`) is trustworthy here BECAUSE
`tests/server/test_service_capture.py` proves the client seam refuses it from every door but the
Slack transport's own — this module submits through `queue.submit` directly, which is exactly
what the Slack door does after that seam.
"""
import dataclasses
import hashlib
import json
import os

from stigmergy.capture import queue, schema
from stigmergy.librarian import gitcmd, processing, worker
from tests.librarian import support

# First line -> the double's title ("Acme renewal pricing") -> slugify -> the stem CODE derives.
THREAD_MATERIAL = (
    "Acme renewal pricing\n"
    "The Acme Corp renewal closed on the terms agreed at the last sync.\n"
    "Thread captured for the record by the brain reaction.\n")
SOURCE_STEM = "acme-renewal-pricing-thread"
SOURCE_PATH = f"sources/slack/{SOURCE_STEM}.md"

PERMALINK = "https://example.slack.com/archives/C1/p1722600000100"
SLACK_HINTS = {
    "source_client": "slack",
    "source_permalink": PERMALINK,
    "source_channel_id": "C1",
    "source_channel_name": "dealflow",
    "source_thread_ts": "1722600000.100",
}


def _file(conn, deps, material, **kw):
    support.submit(conn, deps, material, **kw)
    return worker.process_next(conn, deps)


# ── the benign twin: a 🧠 capture files source + synthesis, one commit, no findings ─────────────
def test_a_slack_capture_files_the_thread_verbatim_beside_the_synthesis(rig, clean_queue):
    env, deps = rig
    item, result = _file(clean_queue, deps, THREAD_MATERIAL, hints=SLACK_HINTS)

    assert result.status == schema.FILED
    page_path, sha = result.result_ref.rsplit("@", 1)
    # `result_ref` names the SYNTHESIS — `sources/` sorts before `wiki/`, so this is the proof
    # `_file` picks by exclusion rather than alphabetically.
    assert page_path.startswith("wiki/notes/")
    assert support.branch_sha(env.bare) == sha

    committed = set(support.changed_paths(env.bare, sha))
    assert {SOURCE_PATH, page_path} <= committed

    # The source page: the material VERBATIM, stamped under the provenance group.
    src = support.read_filed_page(env.bare, sha, SOURCE_PATH)
    assert "type: source" in src
    assert "source_kind: slack" in src
    assert f'url: "{PERMALINK}"' in src
    assert "tags: [source, slack-thread]" in src
    digest = hashlib.sha256(THREAD_MATERIAL.encode("utf-8")).hexdigest()
    assert f'content_hash: "sha256:{digest}"' in src
    assert "tier: 1" in src
    # ADR 028 D6: every source part carries its producer-computed `id:` — the Slack
    # caller proves the stamp fires for ALL callers of the shared writer, not only drive's.
    assert f'id: "{SOURCE_STEM}"' in src
    assert f"submitted_by: {support.DEFAULT_SUBMITTER}" in src
    assert "The Acme Corp renewal closed on the terms agreed at the last sync." in src

    # The synthesis cites its verbatim source — code wrote the citation, and the stamped page
    # carries it — the mechanism, asserted on the committed artifact.
    syn = support.read_filed_page(env.bare, sha, page_path)
    assert f'sources: ["[[{SOURCE_STEM}]]"]' in syn

    # Every reporting surface knows both pages exist.
    assert result.report["source_pages"] == [SOURCE_PATH]
    assert SOURCE_PATH in result.report["summary"]
    body = gitcmd.run("log", "-1", "--format=%b", sha, cwd=env.bare).stdout
    assert "1 source page(s)" in body

    row = queue.get_submission_trace(clean_queue, item["id"])
    assert row["report"]["source_pages"] == [SOURCE_PATH]


def test_a_slack_capture_survives_every_gate_with_only_the_thin_page_note(rig, clean_queue):
    """The attachment is the benign twin of the multi-page veto — the same diff shape
    (`sources/` page + `wiki/` page in one run) that `_cross_check_outcome` must veto for an
    AGENT must sail through when code wrote the extra pages and declared them.

    The ONE admitted finding is the linter's thin-page WARN on the source page: a three-message
    thread genuinely is 5 body lines, the linter says so at note severity, and the capture files
    anyway — the same honest warn the meeting flow's 6-line tail part earns, because the splitter
    honours its maximum and ignores its minimum by design. Anything else here is a regression."""
    _, deps = rig
    _, result = _file(clean_queue, deps, THREAD_MATERIAL, hints=SLACK_HINTS)
    assert result.status == schema.FILED
    assert all("thin page" in finding for finding in result.findings)


# ── the OFF position: an ordinary capture is byte-identical to before the parameter ─────────────
def test_an_ordinary_capture_files_no_source_page_and_reports_none(rig, clean_queue):
    env, deps = rig
    _, result = _file(clean_queue, deps, THREAD_MATERIAL)     # same material, no slack hints

    assert result.status == schema.FILED
    _, sha = result.result_ref.rsplit("@", 1)
    assert "source_pages" not in result.report
    assert not any(p.startswith("sources/") for p in support.changed_paths(env.bare, sha))


def test_an_mcp_style_capture_with_only_untrusted_source_hints_stays_ordinary(rig, clean_queue):
    """The trigger is `source_client` alone — the five untrusted source hints (channel, ts,
    participants) may arrive from anywhere and must not switch the attachment on."""
    env, deps = rig
    hints = {k: v for k, v in SLACK_HINTS.items()
             if k not in ("source_client", "source_permalink")}
    _, result = _file(clean_queue, deps, THREAD_MATERIAL, hints=hints)
    assert result.status == schema.FILED
    _, sha = result.result_ref.rsplit("@", 1)
    assert "source_pages" not in result.report
    assert not any(p.startswith("sources/") for p in support.changed_paths(env.bare, sha))


# ── rule 9, the red proof: per-page mode did not lose the anchoring question ────────────────────
class _UnresolvableAnchorAgent:
    """`test_processing_pg._UnresolvableAnchorAgent`'s shape, replicated rather than imported
    across test modules: the double's DECLARED anchoring is rewritten to a name the registry does
    not know, page untouched. With the attachment ON this claim travels `ctx.page_declared`
    (per-page mode) instead of the single-outcome check — the park must be identical."""

    def __init__(self, inner):
        self.inner = inner

    def run(self, **kwargs):
        run = self.inner.run(**kwargs)
        if run.outcome is not None and run.outcome.decision == "file":
            run.outcome = dataclasses.replace(
                run.outcome,
                anchoring={"kind": "entity", "reason": "", "entities": ["Ghost Company Inc"]},
                links_created=("Ghost Company Inc",))
        return run


def test_an_unearned_anchor_claim_still_parks_with_the_attachment_on(rig, clean_queue):
    env, base_deps = rig
    before = support.branch_sha(env.bare)
    deps = dataclasses.replace(base_deps, agent=_UnresolvableAnchorAgent(base_deps.agent))

    item, result = _file(clean_queue, deps, THREAD_MATERIAL, hints=SLACK_HINTS)

    assert result.status == schema.TRIAGE
    assert result.result_ref == ""
    assert "Ghost Company Inc" in result.report["open_question"]
    assert support.branch_sha(env.bare) == before        # nothing committed — no source page,
    assert json.dumps(result.report).count(schema.FAILED) == 0     # no synthesis, no partial set


def test_a_company_wide_slack_capture_files_with_the_citation_and_empty_entity(rig, clean_queue):
    """The per-page road's other half: `kind: company` with a written reason files, `entity: []`
    on the synthesis, and the citation is merged beside the double's own drafted `sources: []`."""
    env, deps = rig
    _, result = _file(clean_queue, deps, f"DOUBLE:company\n{THREAD_MATERIAL}", hints=SLACK_HINTS)
    assert result.status == schema.FILED
    page_path, sha = result.result_ref.rsplit("@", 1)
    syn = support.read_filed_page(env.bare, sha, page_path)
    assert "entity: []" in syn
    assert "-thread]]" in syn                       # cited, whatever the derived title slug


# ── a parked capture writes nothing — the attachment must not leak a page before triage ─────────
def test_a_parked_slack_capture_leaves_no_source_page_behind(rig, clean_queue):
    env, deps = rig
    before = support.branch_sha(env.bare)
    item, result = _file(clean_queue, deps,
                         f"DOUBLE:triage-entity=Umbrella Corp\n{THREAD_MATERIAL}",
                         hints=SLACK_HINTS)
    assert result.status == schema.NEEDS_INPUT
    assert support.branch_sha(env.bare) == before
    assert SOURCE_PATH not in support.all_ever_committed_paths(env.bare)


# ── the corrective retry composes with the attachment: one set, filed once ──────────────────────
def test_a_shape_refusal_on_pass_one_still_files_exactly_one_source_set(rig, clean_queue):
    """The retry loop composes with the attachment: pass 1's refusal costs nothing of the set,
    and pass 2 runs the whole attachment path against its own outcome — exactly one source part
    in the final commit, no duplicate, no leftover (`open_for_new` would crash on one)."""
    env, deps = rig
    _, result = _file(clean_queue, deps, f"DOUBLE:bad-shape-once\n{THREAD_MATERIAL}",
                      hints=SLACK_HINTS)
    assert result.status == schema.FILED
    _, sha = result.result_ref.rsplit("@", 1)
    sources = [p for p in support.changed_paths(env.bare, sha) if p.startswith("sources/")]
    assert sources == [SOURCE_PATH]


# ── a recaptured thread: the stem exists, and the capture is refused, never overwritten ─────────
def test_a_source_stem_that_already_exists_refuses_rather_than_overwrites(rig, clean_queue):
    env, deps = rig
    existing = ("---\ntype: source\ntitle: \"Acme renewal pricing — thread\"\n"
                "source_kind: slack\ncontent_hash: \"sha256:0\"\ntier: 1\n"
                "tags: [source, slack-thread]\nrelated: []\nsources: []\n---\n\n# Old capture\n")
    full = os.path.join(env.repo, SOURCE_PATH)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w", encoding="utf-8") as f:
        f.write(existing)
    support.commit_and_push(env.repo, "test: a previous capture of the same thread")

    before = support.branch_sha(env.bare)
    item, result = _file(clean_queue, deps, THREAD_MATERIAL, hints=SLACK_HINTS)

    assert result.status == schema.FAILED
    assert "already exist" in result.report["summary"]
    assert support.branch_sha(env.bare) == before


# ── a long thread splits into cross-linked parts through the shared writer ──────────────────────
def test_a_long_thread_splits_into_slack_parts_through_the_shared_writer():
    """The shared writer, exercised with the fast lane's own parameters — no repo, no PG:
    `_build_source_parts` is the ONE writer both flows share, and this is the proof the slack
    frontmatter (kind, tags, url) lands on EVERY part while the cross-link chain keeps the
    meeting flow's exact shape. (An end-to-end split cannot be driven through the offline double:
    it copies the material verbatim into the synthesis body, so any material long enough to split
    the source page also trips the linter's 150-line cap on the synthesis — a limit of the double,
    not of the flow; the e2e above proves the single-part set through the same code path.)"""
    filler = "\n".join(f"message line about the acme renewal, entry {chr(97 + n % 26)}"
                       for n in range(200))
    parts = processing._build_source_parts(
        SOURCE_STEM, "Acme renewal pricing — thread", f"Acme renewal pricing\n{filler}\n",
        source_kind="slack", tags=("source", "slack-thread"), url=PERMALINK)

    assert [stem for stem, _pid, _text in parts] == [SOURCE_STEM, f"{SOURCE_STEM}-p2"]
    # ADR 028 D6: the producer computes each part's EXPLICIT chain identity — the
    # filename stem carries `-p<n>` (a wikilink target must be a filename), the identity carries
    # the historical `#p<n>` sub-identity convention, and part 1's identity IS the bare stem.
    assert [pid for _stem, pid, _text in parts] == [SOURCE_STEM, f"{SOURCE_STEM}#p2"]
    part1, part2 = parts[0][2], parts[1][2]
    for text in (part1, part2):
        assert "source_kind: slack" in text
        assert "tags: [source, slack-thread]" in text
        assert f'url: "{PERMALINK}"' in text
    assert f"Continues in [[{SOURCE_STEM}-p2]]." in part1
    assert f"Continued from [[{SOURCE_STEM}]]." in part2
