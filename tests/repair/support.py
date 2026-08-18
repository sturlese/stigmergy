"""Non-fixture test support for the repair suite: the Postgres seam with every schema this
package reads or writes, a real knowledge repo (bare remote + clone) with the proposer's own skill
in it, and the finding-seeding helpers every `test_*_pg.py` file needs.

**Real git, real Postgres, real gates, real gitleaks** — the double stands in for the model and
for nothing else. A faked diff would prove nothing about `gate_body_rewrite`, a faked gitleaks
nothing about the secrets veto, and both are exactly the properties this package exists to keep.

A plain module rather than a `conftest.py`, the same reasoning `tests/librarian/support.py` gives
for itself: fixtures are per-package pytest wiring, this is plain code any file can import.
"""
import json
import os

from stigmergy.capture import ops as capture_ops
from stigmergy.capture import schema as capture_schema
from stigmergy.gardener import checks as gardener_checks
from stigmergy.gardener import schema as gardener_schema
from stigmergy.gardener import store as gardener_store
from stigmergy.gardener import sweep as gardener_sweep
from stigmergy.repair import proposer
from stigmergy.repair.schema import ensure_repair_schema
from tests import testdb
from tests.librarian import support as librarian_support

# Pages the fixture knowledge repo (`tests/librarian/fixtures/repo/`) already carries. Named here
# so a test reads as the scenario it is rather than as a path soup, and so a fixture rename breaks
# in one place.
NOTE_A = "wiki/notes/Existing Note.md"
NOTE_B = "wiki/notes/Café Zürich Renewal.md"
DECISION = "wiki/decisions/a-decision-from-a-previous-meeting.md"

STEWARD = "steward@example.com"

# The repair-proposer skill, as a FIXTURE. The real one is versioned in the knowledge repo and
# read at run time (`proposer.SKILL_RELPATH`), which is the whole point of the design — so the
# suite carries its own, deliberately short, and never a frozen copy of the real one: what the
# code owes is the READ path and the refusal when it is absent, and pinning the brain repo's prose
# here would be pinning somebody else's editorial decisions.
FIXTURE_SKILL = """---
name: repair-proposer
description: fixture stand-in for the knowledge repo's own procedure
---

# repair-proposer (test fixture)

Propose the smallest additive repair for a finding, or nothing at all. Read the pages before you
propose anything about them. A finding is a hint, not a verdict.
"""


def connect_or_skip():
    conn = testdb.connect_or_skip("repair")
    capture_schema.ensure_capture_schema(conn)      # capture_queue, job_runs
    gardener_schema.ensure_gardener_schema(conn)    # gardener_findings — the proposer's input
    ensure_repair_schema(conn)                      # repair_proposals — this package's own table
    return conn


def clean(conn) -> None:
    """Empty every table this suite's own writes could have touched — test isolation only, the
    same posture every sibling suite takes."""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM repair_proposals")
        cur.execute("DELETE FROM gardener_findings")
        cur.execute("DELETE FROM capture_queue")
        cur.execute("DELETE FROM job_runs")


def build_repo(tmp_path, *, with_skill: bool = True):
    """A bare remote plus a clone of the fixture knowledge repo, with the proposer's skill
    committed into it. `with_skill=False` is the fixture for the named config refusal."""
    env = librarian_support.build_repo(str(tmp_path / "git"))
    if with_skill:
        write_skill(env.repo)
        librarian_support.commit_and_push(env.repo, "test: add the repair-proposer skill")
    return env


def write_skill(repo: str, text: str = FIXTURE_SKILL) -> str:
    path = proposer.skill_path(repo)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


# ── the proposer's input: a completed gardener run and its findings ───────────────────────────
def seed_gardener_run(conn, *, status: str = "ok") -> int:
    """A `job_runs` row for `job='gardener'` — what `store.latest_completed_run` reads. Seeded
    directly rather than by running a whole gardener pass, exactly as `tests.gardener.support`
    seeds its own."""
    return capture_ops.record_job_run(conn, gardener_schema.JOB_NAME, status=status, stats={})


def seed_finding(conn, run_id: int, *, check: str, subjects: list[str], detail: str = "",
                 severity: str = gardener_schema.SEVERITY_WARN) -> int:
    """One `gardener_findings` row, through the REAL assembler and the REAL writer — so a test
    exercises the same `subjects` round trip the gardener itself produces, never a hand-crafted
    row a marshalling bug could silently disagree with. Returns the finding's id."""
    finding = gardener_checks.build_finding(
        check=check, severity=severity, subject=", ".join(subjects), subjects=subjects,
        detail=detail or f"fixture finding about {', '.join(subjects)}",
        suggested_action="no command — this is a fixture",
        source=gardener_schema.SOURCE_MODEL, model_id="fixture-model")
    before = {row["id"] for row in gardener_store.findings_for_run(conn, run_id)}
    gardener_store.insert_findings(conn, run_id, [finding])
    after = gardener_store.findings_for_run(conn, run_id)
    return next(row["id"] for row in after if row["id"] not in before)


def seed_unlinked_mention(conn, run_id: int, pages=(NOTE_A, NOTE_B)) -> int:
    """The finding the offline double answers with ONE backlink op."""
    return seed_finding(conn, run_id, check=gardener_sweep.CHECK_MODEL_UNLINKED_MENTION,
                        subjects=list(pages))


def seed_contradiction(conn, run_id: int, pages=(NOTE_A, DECISION)) -> int:
    """The finding the offline double answers with the callout PAIR, one op per side."""
    return seed_finding(conn, run_id, check=gardener_sweep.CHECK_MODEL_CONTRADICTION,
                        subjects=list(pages))


def seed_empty_entity_body(conn, run_id: int, page: str = "") -> int:
    """The MODEL finding the same road answers: one entity page whose body is written and says
    nothing. `severity=info`, as `sweep.MODEL_CHECK_SEVERITY` emits it, and `source=model` with a
    model id, which `seed_finding` already sets — the two body-road findings differ in their check
    and in nothing else the proposer reads."""
    return seed_finding(conn, run_id, check=gardener_sweep.CHECK_MODEL_EMPTY_ENTITY_BODY,
                        subjects=[page or ENTITY_PAGE], severity=gardener_schema.SEVERITY_INFO)


def seed_placeholder_body(conn, run_id: int, page: str = "") -> int:
    """The gardener finding the `entity-body` road answers: one entity page, still its template.

    `severity=info`, as `check_entity_placeholder_bodies` emits it — the proposer does not read
    severity, but a fixture that says something the producer never says is a fixture that can go
    on agreeing with a road nothing takes any more."""
    return seed_finding(conn, run_id, check=gardener_checks.CHECK_ENTITY_PLACEHOLDER_BODY,
                        subjects=[page or ENTITY_PAGE], severity=gardener_schema.SEVERITY_INFO)


# ── the entity-body fixtures: a minted identity that says nothing about itself ────────────────
# The fixture repo's own `wiki/entities/Acme Corp.md` is deliberately NOT reused: it declares no
# `entity:` id, so the contract linter reports it as an entity page the registry does not register
# — a finding `gate_contract` would surface the moment a repair touched that file, vetoing every
# apply for a reason that has nothing to do with the repair. A page this kind can legitimately
# rewrite has to be registry-clean, so this seeds one.
ENTITY_STEM = "Meridian Partners"
ENTITY_ID = "meridian-partners"
ENTITY_PAGE = f"wiki/entities/{ENTITY_STEM}.md"

# `ops/templates/entity.md`'s own shape, angle markers included — what `stigmergy-entities create`
# copies verbatim into a committed page, and therefore what the gardener's
# `entity-placeholder-body` check actually finds in a real corpus.
PLACEHOLDER_BODY = f"""# {ENTITY_STEM}

## What / Who

<One clear paragraph: what this entity is and why it's in the brain.>

## Facts

- <fact, and the page it came from once one exists>
"""


def seed_entity(env, *, entity_id: str = ENTITY_ID, stem_name: str = ENTITY_STEM,
                role: str = "", body: str = PLACEHOLDER_BODY, anchored: int = 0,
                drop_updated: bool = False, push: bool = True) -> str:
    """A registered entity page in the checkout, plus `anchored` note pages declaring it.

    Registered as well as written, because the two have to agree: the contract linter's registry
    check is a whole-repo rule, and `gate_contract` reports it against any page the repair touches.
    `anchored` pages are what the proposer resolves a body draft FROM, so a test says how much
    evidence exists by counting rather than by writing pages by hand.
    """
    front = [
        "type: entity",
        f'title: "{stem_name}"',
        "status: developing",
        "entity_type: organization",
        f'role: "{role}"',
        "aliases: []",
        "created: 2026-01-01",
        *([] if drop_updated else ["updated: 2026-01-01"]),
        "tags: [entity, organization]",
        f'entity: ["{entity_id}"]',
        "related: []",
        "sources: []",
    ]
    path = os.path.join(env.repo, "wiki", "entities", f"{stem_name}.md")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("---\n" + "\n".join(front) + "\n---\n\n" + body)

    registry_path = os.path.join(env.repo, "ops", "entity-registry.json")
    with open(registry_path, encoding="utf-8") as f:
        registry = json.load(f)
    registry["entities"][entity_id] = {"name": stem_name, "type": "organization", "aliases": []}
    with open(registry_path, "w", encoding="utf-8") as f:
        json.dump(registry, f, ensure_ascii=False, indent=2)

    for n in range(anchored):
        write_anchored_note(env, f"Meridian Note {n + 1}", entity_id=entity_id, push=False)
    if push:
        librarian_support.commit_and_push(env.repo, f"test: mint {stem_name}")
    return f"wiki/entities/{stem_name}.md"


def write_anchored_note(env, title: str, *, entity_id: str = ENTITY_ID, body: str = "",
                        push: bool = True) -> str:
    """One `wiki/notes/` page anchored to an entity — the corpus the proposer drafts a body from,
    resolved through the page's own `entity:` frontmatter exactly as the index reads it."""
    front = ["type: note", f'title: "{title}"', "status: developing", "created: 2026-02-01",
             "updated: 2026-02-01", "tags: [note]", f'entity: ["{entity_id}"]', "related: []",
             "sources: []"]
    relpath = f"wiki/notes/{title}.md"
    path = os.path.join(env.repo, *relpath.split("/"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    text = body or (f"# {title}\n\n{title} records a renewal conversation with the broker, the "
                    f"volumes it covered and what was agreed about the next quarter.\n")
    with open(path, "w", encoding="utf-8") as f:
        f.write("---\n" + "\n".join(front) + "\n---\n\n" + text)
    if push:
        librarian_support.commit_and_push(env.repo, f"test: add {title}")
    return relpath


# ── the `delete` kind's fixtures: a page worth removing, and pages that mention it ─────────────
# Deliberately NO `entity:` declaration on either, exactly as the fixture repo's own hand-authored
# pages have none: an anchor would make every deletion test depend on the entity registry as well,
# and `gate_contract` would then surface a registry finding on any page a sweep touched.
DOOMED_STEM = "Superseded Renewal Memo"
DOOMED_PAGE = f"wiki/notes/{DOOMED_STEM}.md"


def write_note(env, title: str, *, related=(), body: str = "", push: bool = True) -> str:
    """One hand-authored `wiki/notes/` page with the `related:` list a test needs.

    The body is padded past the contract linter's thirty-line floor the same way the fixture repo's
    own pages are: a `size` warning is only a note, but a fixture that trips one teaches a reader
    to skim the gate output, which is where a real veto hides.
    """
    front = ["type: note", f'title: "{title}"', "status: developing", "created: 2026-02-01",
             "updated: 2026-02-01", "tags: [note]",
             f"related: {json.dumps([f'[[{name}]]' for name in related], ensure_ascii=False)}",
             "sources: []"]
    filler = "\n".join(f"- line {n} of the padding this page carries so the contract linter's "
                       f"thirty-line floor is met without a warning." for n in range(1, 26))
    relpath = f"wiki/notes/{title}.md"
    path = os.path.join(env.repo, *relpath.split("/"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    text = body or f"# {title}\n\n## What it says\n\n{filler}\n"
    with open(path, "w", encoding="utf-8") as f:
        f.write("---\n" + "\n".join(front) + "\n---\n\n" + text)
    if push:
        librarian_support.commit_and_push(env.repo, f"test: add {title}")
    return relpath


def write_source(env, title: str, *, content_hash: str, extracted_at: str = "2026-02-01T00:00:00Z",
                 body_link: str = "", push: bool = True) -> str:
    """One `sources/` page, shaped the way `page.stamp_source_fields` writes a real one — the
    provenance group the contract linter requires of that zone, and nothing an authored page has.

    `content_hash` is the whole point: two pages sharing one are the same captured document filed
    twice, which is the ONE deletion this system derives without asking a model.
    """
    front = ["type: source", f'title: "{title}"', "tags: [source]", "source_kind: upload",
             "status: developing", f'content_hash: "sha256:{content_hash}"',
             f'extracted_at: "{extracted_at}"', "tier: 1"]
    filler = "\n".join(f"- extracted line {n} of the document this page records."
                       for n in range(1, 31))
    if body_link:
        filler = f"- as [[{body_link}]] records, the volumes held.\n" + filler
    relpath = f"sources/{title}.md"
    path = os.path.join(env.repo, *relpath.split("/"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("---\n" + "\n".join(front) + "\n---\n\n" + f"# {title}\n\n{filler}\n")
    if push:
        librarian_support.commit_and_push(env.repo, f"test: add {title}")
    return relpath


def seed_deletion_corpus(env) -> dict[str, str]:
    """The shape every deletion test needs: one doomed page and three that mention it three
    different ways — a `related:` entry beside a surviving one, a body wikilink, and a page whose
    ONLY reference is a `related:` entry (so its scrub removes a line and adds none).

    Returns `{label: relpath}` so a test names the page by what it is FOR.
    """
    doomed = write_note(env, DOOMED_STEM, push=False)
    write_note(env, "Keeps A Link", related=[DOOMED_STEM, "Existing Note"], push=False)
    write_note(env, "Mentions It In Prose", push=False,
               body=f"# Mentions It In Prose\n\n## What it says\n\nThe broker agreed, as "
                    f"[[{DOOMED_STEM}]] records, and the volumes held.\n\n"
                    + "\n".join(f"- padding line {n} so the linter's floor is met."
                                for n in range(1, 26)) + "\n")
    write_note(env, "Only A Related Entry", related=[DOOMED_STEM], push=False)
    librarian_support.commit_and_push(env.repo, "test: seed the deletion corpus")
    return {"doomed": doomed,
            "keeps_a_link": "wiki/notes/Keeps A Link.md",
            "in_prose": "wiki/notes/Mentions It In Prose.md",
            "only_related": "wiki/notes/Only A Related Entry.md"}


# ── the `entity-alias` kind's fixtures: two identities that are one entity ────────────────────
# The fixture repo's `ops/entity-registry.json` registers `Acme Corp` under the id `acme`, which is
# NOT `slugify("Acme Corp")` — so that repo is permanently in registry drift as far as the frozen
# contract linter's `check_registry` is concerned. Every other kind gets away with it because
# `gate_contract` filters the linter's findings to the pages a diff TOUCHED and none of them touch
# the registry. A merge REGENERATES it, so the finding surfaces and vetoes the apply.
#
# `regenerate_registry` is therefore part of the fixture rather than part of the test: it makes the
# checkout's registry what `stigmergy-entities regenerate` would produce, which is the state every
# real knowledge repo is in (the knowledge repo's own CI goes red on drift).
SURVIVOR_STEM, SURVIVOR_ID = "Cofers", "cofers"
ABSORBED_STEM, ABSORBED_ID = "Cofers Holdings", "cofers-holdings"
SURVIVOR_PAGE = f"wiki/entities/{SURVIVOR_STEM}.md"
ABSORBED_PAGE = f"wiki/entities/{ABSORBED_STEM}.md"

# What an entity page says when somebody has written it — enough that the linter's size rule is
# satisfied and a reader can tell the two identities apart.
def entity_body(stem_name: str) -> str:
    return (f"# {stem_name}\n\n## What / Who\n\n{stem_name} is an organization this fixture corpus "
            f"records dealings with.\n\n## Facts\n\n"
            + "\n".join(f"- fact {n} about {stem_name}, recorded for the fixture."
                        for n in range(1, 26)) + "\n")


def write_entity_page(env, stem_name: str, entity_id: str, *, aliases=(),
                      push: bool = True) -> str:
    """One `wiki/entities/` page in the checkout — NOT registered here: `regenerate_registry`
    derives the registry from the pages, exactly as the mint door does."""
    front = ["type: entity", f'title: "{stem_name}"', "status: developing",
             "entity_type: organization", 'role: ""',
             f"aliases: {json.dumps(list(aliases), ensure_ascii=False)}",
             "created: 2026-01-01", "updated: 2026-01-01", "tags: [entity, organization]",
             f'entity: ["{entity_id}"]', "related: []", "sources: []"]
    relpath = f"wiki/entities/{stem_name}.md"
    path = os.path.join(env.repo, *relpath.split("/"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("---\n" + "\n".join(front) + "\n---\n\n" + entity_body(stem_name))
    if push:
        librarian_support.commit_and_push(env.repo, f"test: add {stem_name}")
    return relpath


def regenerate_registry(env, *, push: bool = True) -> None:
    """`ops/entity-registry.json`, derived from the entity pages by the ONE writer that derives it.
    Never hand-written here: a fixture registry a test typed would let a merge pass against a file
    the real generator would never produce."""
    from stigmergy.entities import generator

    generator.regenerate(env.repo)
    if push:
        librarian_support.commit_and_push(env.repo, "test: regenerate the entity registry")


def seed_duplicate_pair(env, *, absorbed_aliases=("Cofers Grupo",), anchored: int = 1,
                        push: bool = True) -> dict[str, str]:
    """The shape every merge test needs: two registered identities, `anchored` note pages anchored
    to the one that will be absorbed, one anchored to the survivor, and a derived registry.

    Returns `{label: relpath}` so a test names each page by what it is FOR.
    """
    write_entity_page(env, SURVIVOR_STEM, SURVIVOR_ID, push=False)
    write_entity_page(env, ABSORBED_STEM, ABSORBED_ID, aliases=absorbed_aliases, push=False)
    absorbed_notes = [write_anchored_note(env, f"Holdings Note {n + 1}", entity_id=ABSORBED_ID,
                                          push=False)
                      for n in range(anchored)]
    survivor_note = write_anchored_note(env, "Cofers Note", entity_id=SURVIVOR_ID, push=False)
    regenerate_registry(env, push=False)
    if push:
        librarian_support.commit_and_push(env.repo, "test: seed the duplicate identity pair")
    return {"survivor": SURVIVOR_PAGE, "absorbed": ABSORBED_PAGE,
            "survivor_note": survivor_note, **{f"absorbed_note_{n + 1}": p
                                               for n, p in enumerate(absorbed_notes)}}


def seed_duplicate_entity_finding(conn, run_id: int, pages=(SURVIVOR_PAGE, ABSORBED_PAGE)) -> int:
    """The MODEL finding the merge road answers: one pair of entity pages, both ids in `subjects`,
    `warn` — exactly as `sweep.MODEL_CHECK_SEVERITY` emits it."""
    return seed_finding(conn, run_id, check=gardener_sweep.CHECK_MODEL_DUPLICATE_ENTITY,
                        subjects=list(pages))


def stem(path: str) -> str:
    return path.rsplit("/", 1)[-1].removesuffix(".md")


def page_text(repo: str, path: str) -> str:
    with open(os.path.join(repo, path), encoding="utf-8") as f:
        return f.read()
