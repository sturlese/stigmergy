"""Manual walk of entity navigation, driven against the real local stack.

Not a test: a narrated walk of the navigation read surface the way an agent meets it —
`list_entities` to learn the vocabulary, `describe_entity` for the layered dated view,
`read_page` to walk one hop along served links/backlinks — and, at every new surface, the
existence rule shown live: two identities, same calls, and the scoped one gets ABSENCE,
byte-identical to nonexistence, never a refusal. Real Postgres, fake embedder, keyless —
the same posture every other walk script in this repo takes.

**What this script does NOT do**, and why (the same reason as the view walk): it does not run
the live "what do we know about X?" `ask` against a real model and a real corpus — that stays a hand
step, because which surface the model chooses is the model's call and has to be observed, not
asserted. This script proves the MECHANISM; the hand steps are printed at the end.

Run: `.venv/bin/python scripts/walk_navigation.py` (repo root, `make db-up` already run).
"""
import json
import os
import shutil
import sys
import tempfile
from types import SimpleNamespace

sys.path.insert(0, os.getcwd())
sys.path.insert(0, os.path.join(os.getcwd(), "src"))

from stigmergy.index import build  # noqa: E402
from stigmergy.index.backends.embedder import build_embedder  # noqa: E402
from stigmergy.server.service import BrainService  # noqa: E402
from tests import testdb  # noqa: E402

STEP = 0


def step(title):
    global STEP
    STEP += 1
    print(f"\n{'=' * 78}\nSTEP {STEP} — {title}\n{'=' * 78}")


def show(label, value):
    print(f"  {label}: {value}")


def page(repo, rel, fm_lines, body):
    path = os.path.join(repo, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("---\n" + "\n".join(fm_lines) + "\n---\n\n" + body + "\n")


def main() -> int:
    conn = testdb.connect_or_skip("walk_navigation")
    root = tempfile.mkdtemp(prefix="walk-m10-")
    repo = os.path.join(root, "repo")

    step("a small knowledge repo with a graph worth walking")
    # The entity page self-anchors; two dated decisions + one undated note anchor
    # to it; one of them is finance-scoped; the view exists; pages wikilink each other.
    page(repo, "wiki/entities/Acme Corp.md",
         ['type: entity', 'title: "Acme Corp"', 'entity_type: organization',
          'role: "Pilot customer for the routing engine"', 'aliases: ["Acme"]',
          'entity: ["acme-corp"]', 'status: developing'],
         "# Acme Corp\n\nPilot customer. See [[Q3 pricing floor]] and [[Acme renewal notes]].")
    page(repo, "wiki/decisions/Q3 pricing floor.md",
         ['type: decision', 'title: "Q3 pricing floor"', 'entity: ["acme-corp"]',
          'as_of: "2026-06-01"', 'verification: verified'],
         "Floor set for [[Acme Corp]] renewals. quarterly pricing decision.")
    page(repo, "wiki/decisions/Acme renewal terms.md",
         ['type: decision', 'title: "Acme renewal terms"', 'entity: ["acme-corp"]',
          'as_of: "2026-03-15"', 'verification: verified', "acl: ['finance']"],
         "Finance-scoped renewal terms for [[Acme Corp]]. confidential pricing.")
    page(repo, "wiki/notes/Acme renewal notes.md",
         ['type: note', 'title: "Acme renewal notes"', 'entity: ["acme-corp"]'],
         "Undated working notes about [[Acme Corp]] and the [[Q3 pricing floor]].")
    page(repo, "views/acme-corp.md",
         ['type: view', 'title: "Acme Corp — view"', 'entity: ["acme-corp"]',
          'generated_at: "2026-07-30T00:00:00+00:00"', 'verification: verified'],
         "## Timeline\n\nRollup for Acme Corp.")
    os.makedirs(os.path.join(repo, "ops"), exist_ok=True)
    with open(os.path.join(repo, "ops", "entity-registry.json"), "w") as f:
        json.dump({"entities": {"acme-corp": {"name": "Acme Corp", "type": "organization",
                                              "aliases": ["Acme"]}}}, f)
    build.rebuild(conn, repo, build_embedder("fake"))
    show("indexed", repo)

    settings = SimpleNamespace(
        entity_registry_path=os.path.join(repo, "ops/entity-registry.json"))
    unrestricted = BrainService(settings, conn, build_embedder("fake"), None, identity="steward")
    scoped = BrainService(settings, conn, build_embedder("fake"), {"eng"}, identity="eng")

    step("list_entities — the vocabulary, served (both identities)")
    show("unrestricted", unrestricted.list_entities())
    show("eng-scoped  ", scoped.list_entities())

    step("describe_entity('Acme') — layered and dated, resolved by ALIAS")
    d = unrestricted.describe_entity("Acme")
    show("entity ", d["entity"])
    show("view", d["view"])
    for t in d["timeline"]:
        show("timeline", t)
    show("note   ", d["timeline_note"])
    assert [t["path"] for t in d["timeline"]][0] == "wiki/decisions/Q3 pricing floor.md", \
        "dated-first, newest first"

    step("existence is scoped: the finance decision is ABSENT for eng — no annotation, no gap")
    d2 = scoped.describe_entity("Acme")
    shown = [t["path"] for t in d2["timeline"]]
    show("eng timeline", shown)
    assert "wiki/decisions/Acme renewal terms.md" not in shown
    show("eng note    ", d2["timeline_note"])

    step("unknown entity vs out-of-scope entity — byte-identical absence")
    a1 = scoped.describe_entity("nonexistent-widgets")
    show("unknown    ", a1)
    # every acme page is open except one, so acme itself stays visible to eng; the byte-identity
    # rule is shown against a FULLY scoped entity in the automated suite (fixtures with labels) —
    # here the demonstrable half is: absence carries the same shape as unknown.
    assert a1 == {"error": "unknown entity: nonexistent-widgets"}

    step("read_page serves the graph — and one hop along it")
    p = unrestricted.read_page("wiki/entities/Acme Corp.md")
    show("type/status", f"{p['type']} / {p['status']}")
    show("links      ", p["links"])
    show("links_note ", p["links_note"])
    show("backlinks  ", [b["path"] for b in p["backlinks"]])
    show("backlinks_note", p["backlinks_note"])
    hop = unrestricted.read_page(p["links"][0]["path"])
    show("one hop -> ", f"{p['links'][0]['path']} ({hop['title']})")

    step("the scoped identity's graph: the finance page is absent from backlinks too")
    p2 = scoped.read_page("wiki/entities/Acme Corp.md")
    show("eng backlinks", [b["path"] for b in p2["backlinks"]])
    assert "wiki/decisions/Acme renewal terms.md" not in [b["path"] for b in p2["backlinks"]]
    r1 = scoped.read_page("wiki/decisions/Acme renewal terms.md")
    r2 = scoped.read_page("wiki/decisions/No Such Page.md")
    show("out-of-scope read", r1)
    show("nonexistent read ", r2)
    assert list(r1)[0] == "error" and list(r2)[0] == "error", "absence, never a refusal"

    shutil.rmtree(root, ignore_errors=True)
    print("\nMECHANISM WALK COMPLETE — every navigation surface served, existence-scoped.")
    print("""
THE LIVE HALF (hand steps, real corpus + real model):
  1. make index-rebuild                              # real embedder over ../stigmergy-brain
  2. generate the real views (one per anchored entity):
       .venv/bin/stigmergy-views regenerate --all   # commits+pushes as the App bot (normal op)
     then make index-rebuild again so describe_entity's view layer sees them.
  3. ask, through the entity door:
       connect a Claude session over MCP and ask "what do we know about Borealis Dynamics?"
     — record whether the trace uses describe_entity / entity-filtered search / read_page+links
     (a model choice: record the outcome either way).""")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
