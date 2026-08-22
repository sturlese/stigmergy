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
from stigmergy.capture import schema
from stigmergy.librarian import worker
from tests.librarian import support

SCOPED = ["leadership"]


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

    pages = [p for p in support.paths_in_commit(env.repo, sha) if p.endswith(".md")]
    assert len(pages) >= 3, f"a meeting set is a source, a meeting page and its decisions: {pages}"
    for page_path in pages:
        page = support.read_filed_page(env.repo, sha, page_path)
        assert _acl_line(page) == 'acl: ["leadership"]', f"{page_path}\n{page}"
