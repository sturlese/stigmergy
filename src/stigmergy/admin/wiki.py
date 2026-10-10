"""The backoffice wiki: ACL-scoped page reads rendered to sanitized HTML on the server.

Every read goes through `BrainService` with the signed-in reader's audiences, so the wiki is
audited, rate-limited, and scoped exactly like MCP. Markdown is rendered with raw HTML and images
disabled, only `https` links survive as external links, and the result is sanitized against a
fixed allowlist before it leaves the server.
"""
from __future__ import annotations

import html
import urllib.parse
from pathlib import PurePosixPath

import nh3
from markdown_it import MarkdownIt

from stigmergy.index import health as index_health
from stigmergy.index.corpus import WIKILINK_RE
from stigmergy.kernel.layers import MARKER_RE
from stigmergy.server.audit import AuditWriter
from stigmergy.server.service import BrainService

SEARCH_RESULTS = 20
_ROUTE = "#/wiki/"
_TAGS = {
    "a", "blockquote", "br", "code", "del", "em", "h1", "h2", "h3", "h4", "h5", "h6", "hr",
    "li", "ol", "p", "pre", "s", "span", "strong", "table", "tbody", "td", "th", "thead", "tr",
    "ul",
}
_ATTRIBUTES = {"a": {"href", "class", "title"}, "span": {"class"}, "ol": {"start"}}


class WikiNotFound(Exception):
    """A hidden page and a missing page are the same absence."""


def page_href(path: str) -> str:
    return _ROUTE + urllib.parse.quote(path, safe="/")


def _wikilink_rule(state, silent: bool) -> bool:
    if not state.src.startswith("[[", state.pos) and not state.src.startswith("![[", state.pos):
        return False
    match = WIKILINK_RE.match(state.src, state.pos)
    if match is None:
        return False
    if not silent:
        target, _, label = match.group(1).partition("|")
        token = state.push("wikilink", "", 0)
        token.meta = {"target": target.split("#")[0].strip(), "label": (label or target).strip()}
    state.pos = match.end()
    return True


def _layer_rule(state, silent: bool) -> bool:
    """A fused page marks each unit a restricted layer contributed as `[layer:<name>] `."""
    if not state.src.startswith("[layer:", state.pos):
        return False
    match = MARKER_RE.match(state.src, state.pos)
    if match is None:
        return False
    if not silent:
        token = state.push("layer_tag", "", 0)
        token.meta = {"layer": match.group("layer")}
    state.pos = match.end()
    return True


def _render_layer_tag(self, tokens, idx, _options, _env) -> str:
    layer = html.escape(tokens[idx].meta["layer"])
    return f'<span class="layer-tag">{layer}</span> '


def _safe_links(state) -> None:
    """Keep absolute https links; every other Markdown link renders as its plain text."""
    for block in state.tokens:
        stack = []
        for token in block.children or ():
            if token.type == "link_open":
                href = str(token.attrGet("href") or "")
                allowed = href.startswith("https://")
                stack.append(allowed)
                if allowed:
                    token.attrs = {"href": href, "class": "external"}
                else:
                    token.tag, token.attrs = "span", {}
            elif token.type == "link_close" and stack and not stack.pop():
                token.tag = "span"


def _render_wikilink(self, tokens, idx, _options, env) -> str:
    meta = tokens[idx].meta
    target = meta["target"].lower()
    if target.endswith(".md"):
        target = target[:-3]
    path = env["links"].get(target)
    label = html.escape(meta["label"] or target)
    if path is None:
        return f'<span class="wikilink-missing">{label}</span>'
    return f'<a class="wikilink" href="{html.escape(page_href(path))}">{label}</a>'


def _render_code_inline(self, tokens, idx, _options, env) -> str:
    content = tokens[idx].content
    path = content.strip()
    if path not in env["cited"]:
        return f"<code>{html.escape(content)}</code>"
    _zone, year, month, name = path.split("/")
    label = f"source {year}-{month} · {name[:8]}"
    return (f'<a class="citation" href="{html.escape(page_href(path))}" '
            f'title="{html.escape(path)}">{html.escape(label)}</a>')


def _markdown() -> MarkdownIt:
    md = MarkdownIt("commonmark", {"html": False, "linkify": False, "typographer": False})
    md.enable(["table", "strikethrough"])
    md.disable(["image", "html_inline", "html_block"])
    md.inline.ruler.before("link", "wikilink", _wikilink_rule)
    md.inline.ruler.before("link", "layer_tag", _layer_rule)
    md.core.ruler.push("safe_links", _safe_links)
    md.add_render_rule("wikilink", _render_wikilink)
    md.add_render_rule("layer_tag", _render_layer_tag)
    md.add_render_rule("code_inline", _render_code_inline)
    return md


_MD = _markdown()


def render(body: str, *, links: list[dict], cited: list[dict]) -> str:
    """Render one page body; only visible link targets and cited sources become internal links."""
    by_stem: dict[str, str] = {}
    for link in links:
        by_stem.setdefault(PurePosixPath(link["path"]).stem.lower(), link["path"])
    env = {"links": by_stem, "cited": {item["path"] for item in cited}}
    return nh3.clean(
        _MD.render(body, env),
        tags=_TAGS,
        attributes=_ATTRIBUTES,
        url_schemes={"https"},
        link_rel="noopener noreferrer",
        strip_comments=True,
    )


class WikiService:
    def __init__(self, conn, *, principal, server_settings, embedder=None, rate_limiter=None):
        audiences = principal.audiences
        self._conn = conn
        self._brain = BrainService(
            server_settings,
            conn,
            embedder,
            set(audiences) if audiences is not None else None,
            identity=principal.subject,
            rate_limiter=rate_limiter,
            audit=AuditWriter(conn),
            principal=principal,
        )

    def pages(self) -> dict:
        return self._brain.browse_pages()

    def graph(self) -> dict:
        return self._brain.browse_graph()

    def page(self, path: str) -> dict:
        page = self._brain.browse_page(path)
        if page is None:
            raise WikiNotFound("not found")
        body = page.pop("body")
        page["html"] = render(body, links=page["links"], cited=page["cited"])
        return page

    def search(self, query: str) -> dict:
        result = self._brain.search(query, None, SEARCH_RESULTS)
        return {"query": query, "count": result["count"], "hits": [
            {key: hit[key] for key in ("path", "title", "type", "summary", "snippet", "updated", "layers")}
            for hit in result["hits"]
        ]}

    def status(self) -> dict:
        # When the index last moved would tell a reader that a layer they cannot see was written.
        return {"dirty": bool(index_health.read(self._conn)["dirty"])}
