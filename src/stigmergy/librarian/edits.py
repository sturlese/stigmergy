"""Edits to pages that already exist: **declared by the agent, performed by code**.

The agent writes only NEW files; a reciprocal `related:` link or an overlap/contradiction
callout on an existing page is named in the outcome and this module performs it. The split
exists because a model asked to "insert a line and change nothing else" rewrote a human page's
body on both of its passes — naming the edit removes the failure class instead of defending
against it, and it is a smaller surface: `agent.confined_write` admits no modification at all,
while code's own edits land in the same diff and every gate — `gate_body_rewrite` above all —
still judges them, additive by construction (`page.with_related_link`, `page.with_callout`).

A declaration is untrusted input: `agent.parse_outcome` bounds its SHAPE, and `validate` here
answers the questions that need the real graph. If ANY declaration is bad, nothing is applied —
a half-applied set of edits is a worktree nobody can reason about. Path questions go through
`page.path_key` / `page.is_inside`, never `==`.
"""
import logging
import os

from stigmergy.index import corpus
from stigmergy.librarian import gates
from stigmergy.librarian import page as page_policy

log = logging.getLogger(__name__)

# The vocabulary lives in `page.py` — the placement table every other type question reads — so
# the outcome boundary and the applier cannot disagree about which kinds exist.
EDIT_KINDS = page_policy.EDIT_KINDS
NOTE_REQUIRED_KINDS = page_policy.NOTE_REQUIRED_KINDS


def _finding(code: str, message: str, locator: str = "") -> gates.Finding:
    return gates.Finding("edits", code, message, locator=locator)


def page_names(worktree: str, *, confined: bool = False) -> set[str]:
    """Every page basename (without `.md`) in EVERY content zone of the worktree.

    A wikilink resolves by bare basename, exactly as the contract linter resolves it — which is
    why this walks ALL the zones the linter's own `by_name` index is built from
    (`CONTENT_ROOTS`): a declared link to a transcript, a source or a view must not be refused
    as dead about a page that plainly exists.

    `confined` drops any page that does not resolve inside the worktree, or whose leaf is a
    symlink — the same filter `gather._confined` applies — and it is a PARAMETER because the two
    callers need different answers: `validate` (the default) is answering "would this link be
    dead?", and the answer must be the linter's, whose index has no containment notion (the
    WRITE road refuses symlinks separately). The `confined=True` caller used to be `gather`,
    whose answer became a list of names in a model's prompt; since a model's reads were scoped
    to what its page could cite, that vocabulary
    comes off the SCOPED corpus instead (`gather.Corpus.link_names`), because containment says
    nothing about audience and a name is the whole of what a link leaks.
    """
    names = set()
    for zone in corpus.ZONES:
        for parent, _dirs, files in os.walk(os.path.join(worktree, zone)):
            for name in files:
                if not name.endswith(".md") or name.startswith("."):
                    continue
                full = os.path.join(parent, name)
                if confined and (os.path.islink(full)
                                 or not page_policy.is_inside(worktree, full)):
                    continue
                names.add(name[: -len(".md")])
    return names


def validate(worktree: str, declared, *, new_pages) -> list[gates.Finding]:
    """Check every declared edit against the real worktree. Returns findings; `[]` means apply."""
    out = []
    resolvable = None                       # scanned lazily: most captures declare no edits
    created = set(new_pages or ())
    for edit in declared or ():
        path = str(edit.get("path", ""))
        kind = str(edit.get("kind", ""))
        link = str(edit.get("link", ""))
        note = str(edit.get("note", ""))

        if kind not in EDIT_KINDS:
            out.append(_finding("unknown-kind",
                                f"declared a {kind!r} edit to {path}: an existing page may only "
                                f"gain one of {', '.join(EDIT_KINDS)}", path))
            continue
        if not page_policy.type_for_folder(path):
            out.append(_finding("outside-lane",
                                f"declared an edit to {path}, which is not in one of the fast "
                                f"lane's folders — no other page may be touched at all", path))
            continue
        basename = path.rsplit("/", 1)[-1]
        if basename.startswith(".") or not basename.endswith(".md"):
            out.append(_finding("not-a-page",
                                f"declared an edit to {path}, which is not a page", path))
            continue
        # `path_key`, not `==`: on this filesystem `wiki/notes/target.md` and the `Target.md`
        # this capture just created are ONE file. Same helper `agent.confined_write` asks the
        # same question with, so the two cannot drift.
        if page_policy.path_key(path) in page_policy.path_keys(created):
            out.append(_finding("own-page",
                                f"declared an edit to {path}, which this capture just created: "
                                f"write the link into the new page instead of declaring an edit "
                                f"to it", path))
            continue
        full = os.path.join(worktree, path)
        # Containment, RESOLVED rather than inferred from the string's shape: everything above
        # is a shape check, and a symlinked DIRECTORY component satisfies every one of them and
        # would be written through, outside the worktree, as the worker.
        if not page_policy.is_inside(worktree, path):
            out.append(_finding("outside-worktree",
                                f"declared an edit to {path}, which resolves outside the "
                                f"worktree — no page there belongs to this capture", path))
            continue
        if os.path.islink(full):
            # `open(p, "w")` follows a symlink, so a page that is really a link would be edited
            # THROUGH it. `apply` opens with `O_NOFOLLOW` as well; this is the half that
            # produces a readable refusal instead of an OSError.
            out.append(_finding("symlinked-target",
                                f"declared an edit to {path}, which is a symlink and not a page",
                                path))
            continue
        if not os.path.isfile(full):
            out.append(_finding("missing-target",
                                f"declared an edit to {path}, which does not exist in the repo",
                                path))
            continue
        if not link:
            out.append(_finding("no-link",
                                f"declared an edit to {path} naming no page to link", path))
            continue
        if resolvable is None:
            resolvable = page_names(worktree)
        if link not in resolvable:
            out.append(_finding("dead-link",
                                f"declared an edit to {path} linking [[{link}]], which resolves "
                                f"to no page in the graph", path))
            continue
        if kind in NOTE_REQUIRED_KINDS and not note.strip():
            out.append(_finding("no-note",
                                f"declared a {kind} callout on {path} with no sentence saying "
                                f"what it overlaps or contradicts", path))
    return out


def apply(worktree: str, declared) -> list[str]:
    """Perform every declared edit. Returns the paths that actually changed.

    Call only after `validate` returned nothing. Each target is read, transformed in memory and
    written back once, so a page is never left half-edited by an exception between two writes.
    """
    changed = []
    for edit in declared or ():
        path = str(edit.get("path", ""))
        kind = str(edit.get("kind", ""))
        full = os.path.join(worktree, path)
        with open(full, encoding="utf-8") as f:
            before = f.read()

        after, _ = page_policy.with_related_link(before, str(edit.get("link", "")))
        if kind in page_policy.CALLOUT_STYLES:
            after = page_policy.with_callout(after, kind=kind, name=str(edit.get("link", "")),
                                             note=str(edit.get("note", "")))
        if after == before:
            log.info("declared %s edit to %s changed nothing (the link was already there)",
                     kind, path)
            continue
        with page_policy.open_for_rewrite(full) as f:
            f.write(after)
        changed.append(path)
    return changed


def apply_declared(worktree: str, declared, *, new_pages) -> tuple[list[str], list[gates.Finding]]:
    """Validate then apply, all-or-nothing. Returns `(edited paths, findings)`."""
    findings = validate(worktree, declared, new_pages=new_pages)
    if findings:
        return [], findings
    return apply(worktree, declared), []
