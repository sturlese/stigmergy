"""Navigation rendered to the answer agent stays ACL-scoped."""
import os
from pathlib import Path

import pytest

from stigmergy.answer import brain as brain_mod
from stigmergy.answer.brain import AnswerBrain
from stigmergy.index import build
from stigmergy.index.backends.embedder import build_embedder
from stigmergy.server.service import BrainService
from stigmergy.server.settings import Settings
from tests.index.support import write_controls
from tests.server.conftest import connect_or_skip, write_page

HUB_PAGE = "wiki/concepts/nav-text-hub.md"
OPEN_SPOKE = "wiki/notes/nav-text-spoke-open.md"
RESTRICTED_SPOKE = "wiki/notes/nav-text-spoke-restricted.md"


class _NavTextFixture:
    def __init__(self, root: str):
        self.repo = os.path.join(root, "repo")
        write_page(self.repo, HUB_PAGE,
                  {"status": "evergreen", "title": "Nav Text Hub"},
                  "Hub links to [[nav-text-spoke-open]] and [[nav-text-spoke-restricted]].")
        write_page(self.repo, OPEN_SPOKE,
                  {"title": "Nav Text Spoke Open"},
                  "Open spoke links back to [[nav-text-hub]].")
        write_page(self.repo, RESTRICTED_SPOKE,
                  {"title": "Nav Text Spoke Restricted", "acl": ["finance"]},
                  "Restricted spoke also links to [[nav-text-hub]].")
        write_controls(Path(self.repo))


@pytest.fixture(scope="module")
def nav_text_indexed(tmp_path_factory):
    fx = _NavTextFixture(str(tmp_path_factory.mktemp("page-text-nav")))
    conn = connect_or_skip()
    build.rebuild(conn, fx.repo, build_embedder("fake"))
    yield conn, fx
    conn.close()


def _brain(conn, fx, audiences=None) -> AnswerBrain:
    settings = Settings(llm="fake")
    service = BrainService(settings, conn, build_embedder("fake"), audiences=audiences)
    return AnswerBrain(service)


# ── the twin test: page_text carries EXACTLY the links/backlinks the service returned ───────────
def test_page_text_renders_the_links_and_backlinks_the_service_returned_one_hidden(
        nav_text_indexed):
    conn, fx = nav_text_indexed
    unrestricted = _brain(conn, fx, audiences=None)
    scoped = _brain(conn, fx, audiences={"eng"})   # never "finance" -> RESTRICTED_SPOKE is hidden

    full_page = unrestricted.service.read_page(HUB_PAGE)
    hidden_page = scoped.service.read_page(HUB_PAGE)
    # sanity: the service really does hide the restricted link for the scoped identity
    assert {e["path"] for e in full_page["links"]} == {OPEN_SPOKE, RESTRICTED_SPOKE}
    assert {e["path"] for e in hidden_page["links"]} == {OPEN_SPOKE}

    full_text = unrestricted.page_text(HUB_PAGE)
    hidden_text = scoped.page_text(HUB_PAGE)

    # the rendered text carries exactly the {path, title} entries the service returned, for both
    # links and backlinks, never re-derived and never double-fenced
    for entry in full_page["links"]:
        assert f"{entry['path']} — {entry['title']}" in full_text
    for entry in full_page["backlinks"]:
        assert f"{entry['path']} — {entry['title']}" in full_text
    assert full_page["links_note"] in full_text
    assert full_page["backlinks_note"] in full_text

    for entry in hidden_page["links"]:
        assert f"{entry['path']} — {entry['title']}" in hidden_text
    assert hidden_page["links_note"] in hidden_text
    # the hidden link is absent from the rendering in every form — no path, no annotation
    assert RESTRICTED_SPOKE not in hidden_text
    assert hidden_page["links_note"] != full_page["links_note"]   # the counts really differ


def test_page_text_renders_type_and_status(nav_text_indexed):
    conn, fx = nav_text_indexed
    brain = _brain(conn, fx)
    text = brain.page_text(HUB_PAGE)
    assert "type: concept" in text
    assert "status: evergreen" in text


def test_page_text_head_extension_sits_outside_the_fence_and_before_the_body(nav_text_indexed):
    """The navigation head is structured, agent-trusted data — it must never land INSIDE the
    UNTRUSTED-DATA fence the page's own body is wrapped in, and it must never be fenced a second
    time itself (the service already fenced the body once; `page_text` must not re-wrap)."""
    conn, fx = nav_text_indexed
    brain = _brain(conn, fx)
    text = brain.page_text(HUB_PAGE)
    assert text.count("<<<UNTRUSTED-DATA") == 1
    assert text.count("UNTRUSTED-DATA;end>>>") == 1
    fence_start = text.index("<<<UNTRUSTED-DATA")
    assert text.index("type: concept") < fence_start
    assert text.index("status: evergreen") < fence_start
    assert text.index("links:") < fence_start
    assert text.index("backlinks:") < fence_start


def test_page_text_of_an_unknown_page_is_unchanged(nav_text_indexed):
    conn, fx = nav_text_indexed
    brain = _brain(conn, fx)
    assert brain.page_text("wiki/does-not-exist.md") == brain_mod.UNKNOWN_PAGE


def test_search_text_renders_type_per_hit(nav_text_indexed):
    conn, fx = nav_text_indexed
    brain = _brain(conn, fx)
    listing = brain.search_text("Nav Text Hub")
    assert "Nav Text Hub (concept" in listing
