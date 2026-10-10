"""Layer views: every librarian and gardening write runs on a tree that looks like today's.

A view of layer `L` is a scratch directory holding `public` plus `L`: public pages as they are,
`L`'s own pages with their audience cleared, and each virtual path present in both merged into one
page. The existing writer runs there unchanged, so the librarian never sees a layer. Projection
then stores the result in `L` alone: `L` pages in full, and for a public page only the units the
public page does not already hold. The public page is never changed by another layer's write.
"""
from __future__ import annotations

import datetime as dt
import os
import shutil
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from stigmergy.index.corpus import split_frontmatter_checked
from stigmergy.kernel import layers
from stigmergy.kernel.normalize import resolution_key
from stigmergy.knowledge.pages import KNOWLEDGE_FOLDERS, KnowledgePage, parse_page, render_page

# A public write cannot see other layers, so it may claim a name a layer page already uses under
# another path. Those conflicts surface in that layer's view as these codes; they degrade links
# rather than block the layer.
CROSS_LAYER_CODES = frozenset({"alias-collision", "ambiguous-link"})
NARROWER = "the page has readers outside the capture's audience"


class LayerProjectionError(RuntimeError):
    pass


@dataclass
class LayerView:
    worktree: str
    layer: str
    root: str
    public: dict[str, KnowledgePage]
    own: dict[str, KnowledgePage]
    before: dict[str, bytes] = field(default_factory=dict)


def layer_names(worktree: str) -> list[str]:
    """Layers with a folder in the tree, public first."""
    folder = Path(worktree, layers.LAYER_ROOT)
    names = sorted(entry.name for entry in folder.iterdir() if entry.is_dir()) if folder.is_dir() else []
    return [layers.PUBLIC, *(name for name in names if layers.is_layer_name(name))]


def layer_root(worktree: str, layer: str) -> Path:
    return Path(worktree) if layer == layers.PUBLIC else Path(worktree, layers.LAYER_ROOT, layer)


def knowledge_pages(worktree: str, layer: str) -> dict[str, tuple[KnowledgePage, str]]:
    """`{virtual path: (page, text)}` for one layer's wiki pages."""
    base = layer_root(worktree, layer)
    pages = {}
    for folder in KNOWLEDGE_FOLDERS:
        directory = base.joinpath(*folder.split("/"))
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.md")):
            vpath = path.relative_to(base).as_posix()
            text = _read(worktree, path)
            pages[vpath] = (parse_page(vpath, text), text)
    return pages


def _read(worktree: str, path: Path) -> str:
    """A page's text, refused when a symlink anywhere on its path could pull in another layer's."""
    expected = os.path.join(os.path.realpath(worktree), os.path.relpath(path, worktree))
    if os.path.realpath(path) != expected or not path.is_file():
        raise LayerProjectionError("a knowledge path is not a regular file")
    return path.read_text(encoding="utf-8")


def build_view(worktree: str, layer: str, *, root: str | None = None) -> LayerView:
    """The layer's knowledge shaped as one repository at `root`, a new directory."""
    if root is None:
        root = tempfile.mkdtemp(prefix="stigmergy-layer-view-")
    else:
        os.makedirs(root)
    try:
        return _fill_view(worktree, layer, root)
    except BaseException:
        shutil.rmtree(root, ignore_errors=True)
        raise


def _fill_view(worktree: str, layer: str, root: str) -> LayerView:
    public = knowledge_pages(worktree, layers.PUBLIC)
    own = public if layer == layers.PUBLIC else knowledge_pages(worktree, layer)
    view = LayerView(
        worktree=worktree,
        layer=layer,
        root=root,
        public={path: page for path, (page, _text) in public.items()},
        own={path: page for path, (page, _text) in own.items()},
    )
    for vpath, (_page, text) in public.items():
        if layer != layers.PUBLIC and vpath in own:
            continue
        _write(root, vpath, text)
    if layer != layers.PUBLIC:
        for vpath, (page, _text) in own.items():
            base = view.public.get(vpath)
            _write(root, vpath, _open(page) if base is None else _merged(base, page))
    _copy_sources(worktree, layer_root(worktree, layers.PUBLIC), root)
    if layer != layers.PUBLIC:
        _copy_sources(worktree, layer_root(worktree, layer), root, clear_audience=True)
    view.before = snapshot(root)
    return view


def discard(view: LayerView) -> None:
    shutil.rmtree(view.root, ignore_errors=True)


def snapshot(root: str) -> dict[str, bytes]:
    result = {}
    for folder in KNOWLEDGE_FOLDERS:
        base = Path(root, *folder.split("/"))
        if base.is_dir():
            for path in sorted(base.glob("*.md")):
                result[path.relative_to(root).as_posix()] = path.read_bytes()
    return result


def project(view: LayerView, *, at: dt.date, strict: bool, skipped: list[str]) -> dict[str, str]:
    """Store the view's page changes in its layer; `{physical path: created|updated|deleted}`.

    Every write is computed before any file is touched, so a refused projection leaves the tree
    unchanged."""
    after = snapshot(view.root)
    writes: list[tuple[str, str | None]] = []
    notes: list[str] = []
    for vpath in sorted(set(view.before) | set(after)):
        old, new = view.before.get(vpath), after.get(vpath)
        if old == new:
            continue
        target = layers.physical_path(view.layer, vpath)
        base = None if view.layer == layers.PUBLIC else view.public.get(vpath)
        own = view.own.get(vpath)
        if new is None:
            if own is None:
                if strict:
                    raise LayerProjectionError("a layer write cannot delete a public page")
                notes.append(f"delete of {vpath} was not applied: {NARROWER}")
                continue
            writes.append((target, None))
            continue
        page = parse_page(vpath, new.decode("utf-8"))
        if view.layer == layers.PUBLIC:
            text = new.decode("utf-8")
        elif base is None:
            text = _with_audience(page, layers.acl_of_layer(view.layer))
        else:
            text = _layer_part(view, page, base, own, at=at, strict=strict, skipped=notes)
            if text is None:
                continue
        writes.append((target, text))
    changes: dict[str, str] = {}
    for target, text in writes:
        destination = Path(view.worktree, *target.split("/"))
        if text is None:
            destination.unlink()
            changes[target] = "deleted"
            continue
        existed = destination.exists()
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(text, encoding="utf-8")
        changes[target] = "updated" if existed else "created"
    skipped.extend(notes)
    return changes


def stores_whole(view: LayerView, vpath: str) -> bool:
    """Whether a change to the view's page at `vpath` is stored as that page itself: in a public view
    always, in a layer's view when the layer holds the page or the page is new. A change to a public
    page from a layer's view is stored only as units the public page lacks (`_layer_part`), so a
    frontmatter-only change such as an alias never reaches it."""
    return view.layer == layers.PUBLIC or vpath in view.own or vpath not in view.public


def _layer_part(view, page: KnowledgePage, base: KnowledgePage, own: KnowledgePage | None, *,
                at: dt.date, strict: bool, skipped: list[str]) -> str | None:
    kept = {unit.key for section in layers.parse_body(page.body).sections for unit in section.units}
    lost = [unit for section in layers.parse_body(base.body).sections for unit in section.units
            if unit.key not in kept]
    if lost and strict:
        raise LayerProjectionError("a layer write cannot change a public page")
    body = layers.split_from_public(page.body, base.body)
    if not body:
        skipped.append(f"update to {page.path} added nothing beyond the public page")
        return None
    public_names = {resolution_key(name) for name in (base.title, *base.aliases)}
    aliases = tuple(alias for alias in page.aliases if resolution_key(alias) not in public_names)
    base_sources = set(base.sources)
    sources = tuple(dict.fromkeys((*(own.sources if own else ()),
                                   *(source for source in page.sources if source not in base_sources))))
    return render_page(
        path=page.path,
        role=page.role,
        title=page.title,
        body=body,
        acl=layers.acl_of_layer(view.layer),
        sources=sources,
        status=page.status,
        page_id=own.page_id if own else f"page_{uuid.uuid4()}",
        created=own.created if own else at,
        updated=page.updated,
        entity_type=page.entity_type,
        aliases=aliases if page.role == "entity" else (),
    )


def _open(page: KnowledgePage) -> str:
    return _with_audience(page, None)


def _merged(base: KnowledgePage, own: KnowledgePage) -> str:
    title_key = resolution_key(base.title)
    aliases: dict[str, str] = {}
    for alias in (*base.aliases, *own.aliases):
        key = resolution_key(alias)
        if key != title_key:
            aliases.setdefault(key, alias)
    return render_page(
        path=base.path,
        role=base.role,
        title=base.title,
        body=layers.merge_for_writing(base.body, own.body, layer="view"),
        acl=None,
        sources=tuple(dict.fromkeys((*base.sources, *own.sources))),
        status=base.status,
        page_id=base.page_id,
        created=min(base.created, own.created),
        updated=max(base.updated, own.updated),
        entity_type=base.entity_type,
        aliases=tuple(aliases.values()) if base.role == "entity" else (),
    )


def _with_audience(page: KnowledgePage, acl) -> str:
    return render_page(
        path=page.path,
        role=page.role,
        title=page.title,
        body=page.body,
        acl=acl,
        sources=page.sources,
        status=page.status,
        page_id=page.page_id,
        created=page.created,
        updated=page.updated,
        entity_type=page.entity_type,
        aliases=page.aliases,
    )


def _copy_sources(worktree: str, base: Path, root: str, *, clear_audience: bool = False) -> None:
    folder = base / "sources"
    if not folder.is_dir():
        return
    for path in sorted(folder.rglob("*.md")):
        relative = path.relative_to(base).as_posix()
        text = _read(worktree, path)
        _write(root, relative, open_source_text(text) if clear_audience else text)


def open_source_text(text: str) -> str:
    """A source rendered as if it had been filed organization-wide."""
    metadata, body, malformed = split_frontmatter_checked(text)
    if malformed or not metadata:
        raise LayerProjectionError("source frontmatter is invalid")
    metadata.pop("acl", None)
    rendered = yaml.safe_dump(metadata, allow_unicode=True, sort_keys=False, width=1000).rstrip()
    return f"---\n{rendered}\n---\n{body if body.startswith(chr(10)) else chr(10) + body}"


def _write(root: str, relative: str, text: str) -> None:
    path = Path(root, *relative.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
