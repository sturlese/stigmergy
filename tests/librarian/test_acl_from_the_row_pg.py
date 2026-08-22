"""The audience a filed page carries is the DOOR's decision, off this capture's own queue row.

The label used to be resolved from the page's OWN PATH in the knowledge repo, through
`ops/acl.json`, at the item's base commit — so the only lever for restricting anything was a
sub-directory under `wiki/`, in a layout where the folder is the page's TYPE. ADR 045 D1/D2
replaced the derivation whole: a human decides at the door, the decision lands on
`capture_queue.acl`, and the worker stamps THAT on every page the capture writes.

This is the through-the-filing-path proof, on real git and the real gates: what the row says is
what the committed page carries, for every page in the set — including the ones that used to carry
no audience at all by contract, which is what §4 case 5 leaked.
"""
import json
import pathlib

from stigmergy.capture import schema
from stigmergy.librarian import dedup, worker
from tests.librarian import support

SCOPED = ["leadership"]
# A page the fixture repo already carries, with no `acl:` — the open page a scoped
# capture must not be allowed to append to.
EXISTING_OPEN_PAGE = "wiki/notes/Existing Note.md"


def _material(label: str) -> str:
    """Distinct content per call — `dedup.find_already_filed` matches on content hash ACROSS every
    submitter, so two items with byte-identical material would collide as a duplicate regardless
    of who submitted them, which is not the property this file tests."""
    return f"A memo ({label}) about the Acme Corp partnership renewal timeline.\n"


def _file_one(conn, deps, *, label: str, acl=None):
    support.submit(conn, deps, _material(label), submitted_by=f"{label}@stigmergy.test", acl=acl)
    return worker.process_next(conn, deps)


def _acl_line(page: str) -> str:
    return next((ln for ln in page.splitlines() if ln.startswith("acl:")), "(none)")


def test_a_capture_queued_at_an_audience_files_a_page_carrying_it(rig, clean_queue):
    env, deps = rig
    _item, result = _file_one(clean_queue, deps, label="scoped", acl=SCOPED)
    assert result.status == schema.FILED, result.report.get("summary")
    path, sha = result.result_ref.rsplit("@", 1)
    page = support.read_filed_page(env.repo, sha, path)
    assert _acl_line(page) == 'acl: ["leadership"]', page


def test_the_benign_twin_a_capture_with_no_audience_files_an_open_page(rig, clean_queue):
    """The specificity half. A rule that only ever restricts has been measured for sensitivity and
    never for specificity, and this one runs on every ordinary capture anybody makes: omitting
    `audience` must leave the page with no `acl:` line at all, which is the contract's spelling of
    open."""
    env, deps = rig
    _item, result = _file_one(clean_queue, deps, label="open")
    assert result.status == schema.FILED, result.report.get("summary")
    path, sha = result.result_ref.rsplit("@", 1)
    page = support.read_filed_page(env.repo, sha, path)
    assert _acl_line(page) == "(none)", page


def test_two_captures_in_a_row_are_stamped_independently_of_each_other(rig, clean_queue):
    """One worker, one `Deps`, no restart: the label is per ROW, so a scoped capture must not
    leak its audience onto the next open one, and an open one must not widen a scoped one. The old
    machinery cached a config for the process's lifetime, which is the failure this shape looks
    for from the other side."""
    env, deps = rig
    _i1, r1 = _file_one(clean_queue, deps, label="first", acl=SCOPED)
    _i2, r2 = _file_one(clean_queue, deps, label="second")
    _i3, r3 = _file_one(clean_queue, deps, label="third", acl=SCOPED)

    lines = []
    for result in (r1, r2, r3):
        assert result.status == schema.FILED, result.report.get("summary")
        path, sha = result.result_ref.rsplit("@", 1)
        lines.append(_acl_line(support.read_filed_page(env.repo, sha, path)))
    assert lines == ['acl: ["leadership"]', "(none)", 'acl: ["leadership"]'], lines


def test_the_verbatim_source_page_carries_the_same_audience_as_its_synthesis(rig, clean_queue):
    """**The leak the old contract had.** A `sources/` page carried no `acl:` "by contract" — it
    was treated as being about nothing — while the page distilled FROM it carried one. So the
    restricted material was readable verbatim by everyone, at a path the synthesis cites.

    Under ADR 045 D2 a source is the ORIGIN of the label, not an exception to it: one capture, one
    audience, every page it writes."""
    env, deps = rig
    support.submit_document(clean_queue, deps, "Board pack: renewal terms for Acme Corp.\n",
                            title="Board pack", acl=SCOPED)
    _item, result = worker.process_next(clean_queue, deps)
    assert result.status == schema.FILED, result.report.get("summary")
    path, sha = result.result_ref.rsplit("@", 1)

    written = support.paths_in_commit(env.repo, sha)
    sources = [p for p in written if p.startswith("sources/")]
    assert sources, f"the document lane filed no source page: {written}"
    for page_path in [path, *sources]:
        page = support.read_filed_page(env.repo, sha, page_path)
        assert _acl_line(page) == 'acl: ["leadership"]', f"{page_path}\n{page}"


def test_every_page_of_a_meeting_set_carries_the_captures_audience(rig, clean_queue):
    """§4 case 5, closed. The meeting page LISTS its decision pages by title, and was stamped
    `acl=None` unconditionally — so a restricted meeting published the titles of every decision it
    produced, to everyone, on a page that named them all in one place."""
    env, deps = rig
    support.submit_meeting(
        clean_queue, deps,
        "Marc: we renew Acme Corp for another year.\nAna: agreed, at the new rate.\n",
        title="Renewals", acl=SCOPED)
    _item, result = worker.process_next(clean_queue, deps)
    assert result.status == schema.FILED, result.report.get("summary")
    _path, sha = result.result_ref.rsplit("@", 1)

    # `wiki/entities/` is excluded BY DESIGN, not by accident: an entity page is the brain's
    # shared vocabulary and carries no audience at all (ADR 045 D6). Asserting "every page" would
    # demand the wrong behaviour the day a meeting births one.
    pages = [p for p in support.paths_in_commit(env.repo, sha)
             if p.endswith(".md") and not p.startswith("wiki/entities/")]
    assert len(pages) >= 3, f"a meeting set is a source, a meeting page and its decisions: {pages}"
    for page_path in pages:
        page = support.read_filed_page(env.repo, sha, page_path)
        assert _acl_line(page) == 'acl: ["leadership"]', f"{page_path}\n{page}"


# ── the write lane: material may only be ADDED to a page its readers could already read ───────
def test_a_scoped_capture_may_not_append_to_an_open_page(rig, clean_queue):
    """ADR 045 D3's other half. The input scope stops a model READING out of scope; this stops the
    edit it declares landing out of scope — and edits are not only a model's doing (the deletion
    sweep and the repair loop write here too), so the check belongs at the gate.

    `gate_zone` judges what the diff DID: a `[leadership]` capture appending a back-link sentence
    to an open note would put restricted material in front of readers it was restricted from."""
    env, deps = rig
    support.submit(clean_queue, deps,
                   f"DOUBLE:backlink={EXISTING_OPEN_PAGE}\n{_material('append')}",
                   submitted_by="scoped@stigmergy.test", acl=SCOPED)
    before = support.branch_sha(env.bare)
    _item, result = worker.process_next(clean_queue, deps)
    # The status is the double's: it declares the same edit again on the corrective pass, so the
    # veto fires twice and the row ends terminal-but-not-filed. What this test pins is the
    # PROPERTY — the edit was refused and nothing reached the remote — not which terminal state a
    # test double's second identical answer produces.
    assert result.status != schema.FILED, result.report.get("summary")
    assert support.branch_sha(env.bare) == before, "a refused edit must commit nothing at all"
    # WHICH gate refused, read off the preserved diff's own header rather than inferred from the
    # terminal status — `report["stage"]` says only "zone", and this run must fail for THIS
    # reason and not for any other zone veto.
    refused = pathlib.Path(result.diagnostics_path).read_text(encoding="utf-8")
    assert "zone/edit-outside-audience" in refused, refused[:400]


def test_the_benign_twin_an_OPEN_capture_may_append_to_an_open_page(rig, clean_queue):
    """The specificity half: this gate runs on every declared edit anybody makes, and open into
    open is the case that must sail through untouched."""
    env, deps = rig
    support.submit(clean_queue, deps,
                   f"DOUBLE:backlink={EXISTING_OPEN_PAGE}\n{_material('append-open')}",
                   submitted_by="open@stigmergy.test")
    _item, result = worker.process_next(clean_queue, deps)
    assert result.status == schema.FILED, result.report.get("summary")


# ── dedup is scoped, in both directions ───────────────────────────────────────────────────────
# Asked of `dedup` directly, against real rows: the offline double names a page after the
# material's first line, so two captures with the SAME digest would collide on the page name
# before dedup's answer could be observed through a filing. The property is about the query.

def _queued(conn, *, digest: str, acl, submitted_by: str, status: str, result_ref: str = "") -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO capture_queue (kind, payload, submitted_by, status, acl, result_ref) "
            "VALUES ('raw', %s::jsonb, %s, %s, %s, %s) RETURNING id",
            (json.dumps({"sha256": digest, "text": "x"}), submitted_by, status, acl, result_ref))
        return cur.fetchone()[0]


def test_the_same_material_at_a_DIFFERENT_audience_is_not_a_duplicate(rig, clean_queue):
    """Two failures in one shape. Collapsing them would silently DISCARD the restriction — the
    submitter is told "filed" and pointed at the OPEN page — and it would make the duplicate
    refusal an existence oracle, since that refusal names a page path and a date for a page the
    caller may not read."""
    digest = "d" * 64
    _queued(clean_queue, digest=digest, acl=None, submitted_by="first@x", status=schema.FILED,
            result_ref="wiki/notes/Open.md@abc")
    mine = _queued(clean_queue, digest=digest, acl=SCOPED, submitted_by="second@x",
                   status=schema.QUEUED)
    item = {"id": mine, "kind": "raw", "payload": {"sha256": digest}, "acl": SCOPED,
            "submitted_by": "second@x"}
    assert dedup.find_already_filed(clean_queue, item) is None


def test_the_benign_twin_the_same_material_at_the_SAME_audience_still_collapses(rig, clean_queue):
    """Dedup is not turned off — it is keyed on one more fact. NULL matches NULL through
    `IS NOT DISTINCT FROM`; a plain `=` would make every open capture stop deduplicating at all,
    which is almost all of them."""
    digest = "e" * 64
    first = _queued(clean_queue, digest=digest, acl=None, submitted_by="first@x",
                    status=schema.FILED, result_ref="wiki/notes/Open.md@abc")
    mine = _queued(clean_queue, digest=digest, acl=None, submitted_by="second@x",
                   status=schema.QUEUED)
    item = {"id": mine, "kind": "raw", "payload": {"sha256": digest}, "acl": None,
            "submitted_by": "second@x"}
    match = dedup.find_already_filed(clean_queue, item)
    assert match is not None and match.submission_id == first


def test_a_scoped_capture_still_collapses_against_its_own_audience(rig, clean_queue):
    """The other half of the twin: scoping dedup must not mean scoped captures never dedup."""
    digest = "f" * 64
    first = _queued(clean_queue, digest=digest, acl=SCOPED, submitted_by="first@x",
                    status=schema.FILED, result_ref="wiki/notes/Board.md@abc")
    mine = _queued(clean_queue, digest=digest, acl=SCOPED, submitted_by="second@x",
                   status=schema.QUEUED)
    item = {"id": mine, "kind": "raw", "payload": {"sha256": digest}, "acl": SCOPED,
            "submitted_by": "second@x"}
    match = dedup.find_already_filed(clean_queue, item)
    assert match is not None and match.submission_id == first


# ── D6: an entity is the shared vocabulary, born open, kept open ──────────────────────────────
def _entity_page(env, sha, paths):
    entity_paths = [p for p in paths if p.startswith("wiki/entities/")]
    assert entity_paths, f"no entity page in the commit: {paths}"
    return entity_paths[0], support.read_filed_page(env.repo, sha, entity_paths[0])


def test_an_entity_born_from_restricted_material_carries_no_audience(rig, clean_queue):
    """A wiki's vocabulary is shared by definition. A corpus where the same customer exists under
    three spellings because one of them was hidden is the failure this system was built to
    prevent, so an entity page never carries an audience — not even one born from material that
    does."""
    env, deps = rig
    support.submit(clean_queue, deps, f"DOUBLE:propose=Zenith Freight\n{_material('birth')}",
                   submitted_by="scoped@stigmergy.test", acl=SCOPED)
    _item, result = worker.process_next(clean_queue, deps)
    assert result.status == schema.FILED, result.report.get("summary")
    _path, sha = result.result_ref.rsplit("@", 1)

    entity_path, page = _entity_page(env, sha, support.paths_in_commit(env.repo, sha))
    assert _acl_line(page) == "(none)", f"{entity_path}\n{page}"


def test_a_restricted_birth_writes_identity_and_What_Who_and_nothing_else(rig, clean_queue):
    """The facts and connections belong to the restricted material and stay on the page this
    capture filed. The one sentence that DOES cross is deliberate and is the smallest thing that
    can: ADR 042 D3 refuses an entity page with no What / Who at all, so the alternative is no
    identity — and the same name introduced again next week as a second entity."""
    env, deps = rig
    support.submit(clean_queue, deps, f"DOUBLE:propose=Kestrel Haulage\n{_material('spine')}",
                   submitted_by="scoped@stigmergy.test", acl=SCOPED)
    _item, result = worker.process_next(clean_queue, deps)
    assert result.status == schema.FILED, result.report.get("summary")
    _path, sha = result.result_ref.rsplit("@", 1)

    _entity_path, page = _entity_page(env, sha, support.paths_in_commit(env.repo, sha))
    assert "Kestrel Haulage is an entity the captured material names" in page, page
    assert "Named in the capture filed as" not in page, page   # a declared FACT
    assert "the note that introduced it" not in page, page     # a declared CONNECTION


def test_the_benign_twin_an_OPEN_birth_still_writes_its_facts(rig, clean_queue):
    """The specificity half, and the one that carries ADR 042: an open capture's entity page is
    RICH — that is what 042 exists for, and D6 must not quietly undo it for everybody."""
    env, deps = rig
    support.submit(clean_queue, deps, f"DOUBLE:propose=Marlowe Rail\n{_material('open-birth')}",
                   submitted_by="open@stigmergy.test")
    _item, result = worker.process_next(clean_queue, deps)
    assert result.status == schema.FILED, result.report.get("summary")
    _path, sha = result.result_ref.rsplit("@", 1)

    _entity_path, page = _entity_page(env, sha, support.paths_in_commit(env.repo, sha))
    assert "Named in the capture filed as" in page, page
    assert "the note that introduced it" in page, page


def test_what_was_withheld_is_reported_to_the_person_who_captured(rig, clean_queue):
    """Counted, never quoted — the sentence must not put back what it is telling you was kept off
    an open page. Said at all because the person who captured is the only one who can decide
    whether the fact belongs in the open, and they can only decide it if they are told."""
    env, deps = rig
    support.submit(clean_queue, deps, f"DOUBLE:propose=Ashford Logistics\n{_material('told')}",
                   submitted_by="scoped@stigmergy.test", acl=SCOPED)
    _item, result = worker.process_next(clean_queue, deps)
    assert result.status == schema.FILED, result.report.get("summary")

    summary = result.report["summary"]
    assert "stayed OFF that entity's page" in summary, summary
    assert result.report["entities_withheld"], result.report
    assert "Named in the capture filed as" not in summary, "the report quoted the withheld fact"


def test_the_spine_of_a_registered_entity_is_not_written_from_restricted_material(
        rig, clean_queue):
    """§4 case 3. `entity_updates` append to a page that is open to everyone, so a restricted
    capture's are dropped whole — its own page carries the facts, and the entity keeps its
    anchor."""
    env, deps = rig
    before = support.read_filed_page(env.repo, "main", "wiki/entities/Acme Corp.md")
    support.submit(clean_queue, deps, f"DOUBLE:update=acme-corp\n{_material('spine-update')}",
                   submitted_by="scoped@stigmergy.test", acl=SCOPED)
    _item, result = worker.process_next(clean_queue, deps)
    assert result.status == schema.FILED, result.report.get("summary")
    _path, sha = result.result_ref.rsplit("@", 1)

    assert support.read_filed_page(env.repo, sha, "wiki/entities/Acme Corp.md") == before
    assert result.report["entities_withheld"], result.report


def test_the_benign_twin_an_OPEN_capture_still_grows_the_spine(rig, clean_queue):
    env, deps = rig
    before = support.read_filed_page(env.repo, "main", "wiki/entities/Acme Corp.md")
    support.submit(clean_queue, deps, f"DOUBLE:update=acme-corp\n{_material('open-update')}",
                   submitted_by="open@stigmergy.test")
    _item, result = worker.process_next(clean_queue, deps)
    assert result.status == schema.FILED, result.report.get("summary")
    _path, sha = result.result_ref.rsplit("@", 1)

    after = support.read_filed_page(env.repo, sha, "wiki/entities/Acme Corp.md")
    assert after != before
    assert result.report["entities_updated"], result.report
