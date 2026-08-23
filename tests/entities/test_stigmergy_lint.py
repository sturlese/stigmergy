"""The knowledge repo's own CI gate over the registry-consistency rule and the
quoted-frontmatter-key fix — exercised against the FROZEN copy this platform depends on
(`gates.gate_contract` runs exactly this script, materialized from the base commit).

`tests/librarian/fixtures/repo/.claude/tools/stigmergy_lint.py` is byte-identical to the knowledge
repo's linter (`test_frozen_linter.py` is what watches that). That repo carries its OWN
rule-by-rule unit test suite for this script (`.claude/tools/test_stigmergy_lint.py`, run read-only
below and never written to), which is the authority on the rule's exhaustive behaviour; this file
is the platform's OWN, independent regression on the two properties that matter here, run against
the artifact THIS repo actually ships and depends on rather than trusting that the frozen copy and
the real script never drift apart in a way the byte-identity check would not itself explain.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

LINTER = (Path(__file__).resolve().parents[1] / "librarian" / "fixtures" / "repo" / ".claude"
         / "tools" / "stigmergy_lint.py")

GOOD_BODY = "\n".join([
    "# Sample Entity", "",
    "## What / Who", "",
    "A paragraph long enough to keep the size rule quiet, describing what this entity is",
    "and why it belongs in the brain at all, in plain and ordinary sentences that a person",
    "would actually write about a real organization or person worth remembering here.", "",
    "## Facts", "",
    "- founded in a year nobody disputes (Source: [[Somewhere]])", "",
    "## Connections", "",
    "- [[Somewhere]] — an ordinary relationship", "",
])


def _run(repo: Path, *extra: str) -> dict:
    proc = subprocess.run([sys.executable, str(LINTER), "--repo", str(repo), "--json", *extra],
                          capture_output=True, text=True)
    return {"rc": proc.returncode, **json.loads(proc.stdout)}


def _entity_page(root: Path, name: str, *, entity_type="organization", aliases=(),
                 extra_frontmatter="") -> None:
    d = root / "wiki" / "entities"
    d.mkdir(parents=True, exist_ok=True)
    listed = "[" + ", ".join(f'"{a}"' for a in aliases) + "]"
    front = (f'type: entity\ntitle: "{name}"\nentity_type: {entity_type}\nstatus: developing\n'
            f'created: 2026-07-01\nupdated: 2026-07-01\ntags: [entity]\naliases: {listed}\n'
            f'related: []\nsources: []\n{extra_frontmatter}')
    (d / f"{name}.md").write_text(f"---\n{front}---\n\n{GOOD_BODY}", encoding="utf-8")


def _registry(root: Path, entities: dict) -> None:
    (root / "ops").mkdir(parents=True, exist_ok=True)
    (root / "ops" / "entity-registry.json").write_text(
        json.dumps({"entities": entities}), encoding="utf-8")


def test_frozen_linter_is_green_on_a_registry_consistent_repo(tmp_path):
    """"Green on the current repo", reproduced from scratch rather than only relying on the real
    repo being present on this machine."""
    _entity_page(tmp_path, "Acme Corp", aliases=["Acme"])
    _registry(tmp_path, {"acme-corp": {"name": "Acme Corp", "type": "organization",
                                       "aliases": ["Acme"]}})
    result = _run(tmp_path)
    # scoped to the REGISTRY check only — the fixture body's own wikilink to a page that does not
    # exist in this minimal repo is a `dead_links` finding, irrelevant to what this test verifies
    registry_findings = [f for f in result["findings"] if f["check"] == "registry"]
    assert registry_findings == []


def test_frozen_linter_goes_red_on_aliases_that_diverge_and_says_whose_the_fix_is(tmp_path):
    """The registry-consistency rule's own reproduction: a page's declared aliases and the
    registry's disagree.

    OLD BEHAVIOUR: the message ended in a runnable command, `stigmergy-entities regenerate`.
    The capture-is-the-approval change deleted that command, so the message names WHOSE the fix is
    instead — and it names the
    right person: the worker regenerates the registry in a commit that touches the identity zone,
    but it REFUSES to write an identity while the two sides disagree, so the pass that would heal
    the drift is the pass the drift prevents. A message containing a command is an executable
    promise; so is a message promising something will fix itself."""
    _entity_page(tmp_path, "Acme Corp", aliases=["Acme", "Acme Corporation"])
    _registry(tmp_path, {"acme-corp": {"name": "Acme Corp", "type": "organization",
                                       "aliases": ["Acme"]}})    # missing "Acme Corporation"
    result = _run(tmp_path)
    registry_findings = [f for f in result["findings"] if f["check"] == "registry"]
    assert registry_findings, "the linter must go RED on diverging aliases"
    assert any("declares alias" in f["message"] for f in registry_findings)
    assert all("an operator puts the pages and the registry back in step" in f["message"]
               for f in registry_findings), registry_findings
    assert result["summary"]["errors"] >= 1


def test_frozen_linter_is_strict_mode_exits_non_zero_on_the_same_drift(tmp_path):
    _entity_page(tmp_path, "Acme Corp", aliases=["Acme Corporation"])
    _registry(tmp_path, {"acme-corp": {"name": "Acme Corp", "type": "organization",
                                       "aliases": ["Acme"]}})
    result = _run(tmp_path, "--strict")
    assert result["rc"] == 1


# ══════════════════════════════════════════════════════════════════════════════════════════════
# The quoted-frontmatter-key fix: quoted keys must parse identically to unquoted ones, for EVERY
# rule — reproduced with a quoted `owner` on a page that legitimately declares one.
# ══════════════════════════════════════════════════════════════════════════════════════════════
def test_a_quoted_owner_satisfies_the_canonical_requires_owner_rule(tmp_path):
    """The exact reproduction: `"owner": "someone@else.com"`, quoted, on a `status: canonical`
    page. Before the fix, the parser's key regex could not see a
    quoted key at all (`^([A-Za-z_][\\w-]*):`), so this page's OWN declared owner was invisible to
    the "canonical requires an owner" rule and it was reported as ownerless — a FALSE finding on a
    page that legitimately declared one, purely because of how it was quoted. "Is caught" means:
    the value is now seen, so the rule reports the truth about the page rather than a punctuation
    artifact.
    """
    d = tmp_path / "wiki" / "notes"
    d.mkdir(parents=True)
    front = ('type: note\ntitle: "Sample Decision"\nstatus: canonical\n'
            '"owner": "someone@else.com"\ncreated: 2026-07-01\nupdated: 2026-07-01\n'
            'tags: [note]\nrelated: []\nsources: []\n')
    (d / "Sample Decision.md").write_text(f"---\n{front}---\n\n{GOOD_BODY}", encoding="utf-8")
    result = _run(tmp_path)
    assert not any("requires an `owner`" in f["message"] for f in result["findings"]), (
        "a quoted owner key must satisfy the canonical-requires-owner rule exactly like an "
        "unquoted one — the finding means the quoted key is still invisible to this rule")


def test_the_benign_twin_a_canonical_page_is_now_refused_by_its_status(tmp_path):
    """The benign twin: the fix must not have made the rule blind in the OTHER direction.

    WHICH rule catches this page changed, not whether it is caught. The old finding was
    "a canonical page requires an `owner`" — a rule that presumed `canonical` was a legal state
    to be in badly. It is not a legal state at all any more, so the page is refused one step
    earlier, on the status itself. Strictly stronger: an owner cannot rescue it."""
    d = tmp_path / "wiki" / "notes"
    d.mkdir(parents=True)
    front = ('type: note\ntitle: "No Owner"\nstatus: canonical\ncreated: 2026-07-01\n'
            'updated: 2026-07-01\ntags: [note]\nrelated: []\nsources: []\n')
    (d / "No Owner.md").write_text(f"---\n{front}---\n\n{GOOD_BODY}", encoding="utf-8")
    result = _run(tmp_path)
    assert any("invalid status" in f["message"] for f in result["findings"])
    assert not any("requires an `owner`" in f["message"] for f in result["findings"])


def test_a_quoted_trust_field_still_parses_the_same_as_unquoted_on_an_authored_page(tmp_path):
    """The parser fix (quoted keys read like unquoted ones) still holds, proven the same way this
    file already proves `owner` above — but the "authored page carries machine-only field" rule
    (the `submitted_by`-keyed zone-ownership check) is gone entirely: legitimacy of a trust field
    is the knowledge repo's CI author check's job, over commit history this stateless scan cannot
    read. So the assertion is not "the finding fires" but "no finding fires about it at all" —
    quoted or not."""
    d = tmp_path / "wiki" / "concepts"
    d.mkdir(parents=True)
    front = ('type: concept\ntitle: "Ordinary Concept"\nstatus: developing\n'
            '"verification": "verified"\ncreated: 2026-07-01\nupdated: 2026-07-01\n'
            'tags: [concept]\nrelated: []\nsources: []\n')
    (d / "Ordinary Concept.md").write_text(f"---\n{front}---\n\n{GOOD_BODY}", encoding="utf-8")
    result = _run(tmp_path)
    assert not any("machine-only field" in f["message"] for f in result["findings"])


# ══════════════════════════════════════════════════════════════════════════════════════════════
# Read-only verification against the real repo's OWN rule-by-rule test suite (never writes to it)
# ══════════════════════════════════════════════════════════════════════════════════════════════
REAL_REPO = os.environ.get("STIGMERGY_REPO", "../stigmergy-brain")


def test_the_real_repos_own_linter_test_suite_passes_read_only():
    """Runs `python3 -m unittest test_stigmergy_lint` from the real repo's `.claude/tools/` — its
    own rule-by-rule cases, including the registry-consistency and quoted-key ones this file
    reproduces independently above. Entirely sandboxed (every case builds its own
    `tempfile.TemporaryDirectory`, per that file's own `setUp`) — never touches the real repo's
    tracked content. Skips cleanly where the repo is not present on this machine.
    """
    tools_dir = Path(REAL_REPO) / ".claude" / "tools"
    if not (tools_dir / "test_stigmergy_lint.py").exists():
        pytest.skip(f"no knowledge repo at {REAL_REPO} on this machine — the read-only "
                    f"verification is skipped where it does not exist")
    proc = subprocess.run([sys.executable, "-m", "unittest", "test_stigmergy_lint", "-v"],
                          cwd=str(tools_dir), capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


def test_the_real_repo_is_green_on_the_registry_rule_read_only():
    if not (Path(REAL_REPO) / "wiki" / "entities").is_dir():
        pytest.skip(f"no knowledge repo at {REAL_REPO} on this machine")
    result = _run(Path(REAL_REPO))
    registry_findings = [f for f in result["findings"] if f["check"] == "registry"]
    assert registry_findings == [], registry_findings
