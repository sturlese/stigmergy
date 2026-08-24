import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / "src/stigmergy/admin/static/assets/change-view.js"


def test_change_disclosures_keep_source_collapsed_and_exact_patch_intact():
    script = f"""
import {{ pathDiffDisclosure, exactPatchDisclosure }} from {json.dumps(MODULE.as_uri())};
const h = (tag, attrs = {{}}, ...children) => ({{ tag, attrs, children, open: false }});
const source = pathDiffDisclosure(h, "source", h("div", {{}}, "source patch"));
const note = pathDiffDisclosure(h, "note", h("div", {{}}, "note patch"));
const patch = "diff --git a/wiki/notes/A.md b/wiki/notes/A.md\\n+current\\n";
const exact = exactPatchDisclosure(h, patch);
process.stdout.write(JSON.stringify({{
  sourceOpen: source.open,
  sourceSummary: source.children[0].children[0],
  noteOpen: note.open,
  noteSummary: note.children[0].children[0],
  exactOpen: exact.open,
  exactSummary: exact.children[0].children[0],
  exactPatch: exact.children[1].children[0],
}}));
"""
    result = subprocess.run(
        ["node", "--input-type=module", "--eval", script],
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(result.stdout) == {
        "sourceOpen": False,
        "sourceSummary": "Show archived source diff",
        "noteOpen": True,
        "noteSummary": "Show line changes",
        "exactOpen": False,
        "exactSummary": "Exact Git patch",
        "exactPatch": "diff --git a/wiki/notes/A.md b/wiki/notes/A.md\n+current\n",
    }
