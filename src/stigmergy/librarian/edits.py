"""Edits to pages that already exist: **declared by the agent, performed by code**.

The agent writes only NEW files. When it wants a reciprocal `related:` link or an
overlap/contradiction callout on a page that already exists, it names it in the outcome and this
module performs the edit.

**Why the split.** The agent used to make the additive edits itself, with a gate behind it
refusing anything that was not a pure addition. On the first real `--backend sdk` run the agent
rewrote the body of an existing human-authored page; the gate refused the whole capture,
correctly; and the corrective retry, handed that finding, did it again. Two for two — and the
mechanism explains why: for a model, "insert a line into this file and change nothing else" is a
strictly harder and less reliable operation than "say which link you want". Cross-linking is the
librarian's whole value — the agent is handed the graph so it *integrates* rather than merely
places — so a mechanism that fails at linking fails at the job. Naming the edit removes the
failure class instead of defending against it.

**This is a smaller security surface, not a larger one.** The agent's write confinement became "a
NEW `.md` page in one of the creatable folders" with no modifications at all (`agent.confined_write`),
which is a strictly shorter allow-list than before. Code's own edits are not exempt from anything:
they land in the same diff, and every gate — `gate_body_rewrite` above all — judges them. They
pass because they are additive by construction (`page.with_related_link`, `page.with_callout`),
which is a property of the code, provable by reading it, rather than of a model's good behaviour.

**Validated before anything is written.** A declaration is untrusted input like the rest of the
outcome: `agent.parse_outcome` bounds its SHAPE (the vocabulary of kinds, the path pattern, the
text lengths), and `validate` here answers the questions that need the real graph — the target
exists, it is one of the creatable folders, it resolves INSIDE the worktree, it is not a page this
capture just created, and the declared link resolves to a page that exists. If ANY declaration is
bad, nothing is applied and the findings go back to the agent as its one corrective retry: a
half-applied set of edits is a worktree nobody can reason about.

**Path questions go through `page.py`, never through `==`.** "Is this the page we just created"
and "does this resolve inside the worktree" are the same two questions `agent.confined_write`
asks, and answering them here with a second implementation is how the first of them came to be
wrong in two places at once (an exact string test against a case- and normalization-insensitive
filesystem). `page.path_key` and `page.is_inside` are that shared seam.
"""
import logging
import os

from stigmergy.librarian import gates
from stigmergy.librarian import page as page_policy

log = logging.getLogger(__name__)

# The vocabulary lives in `page.py` — the placement table every other type question reads — so the
# outcome boundary and the applier cannot disagree about which kinds exist.
EDIT_KINDS = page_policy.EDIT_KINDS
NOTE_REQUIRED_KINDS = page_policy.NOTE_REQUIRED_KINDS


def _finding(code: str, message: str, locator: str = "") -> gates.Finding:
    return gates.Finding("edits", code, message, locator=locator)


def page_names(worktree: str) -> set[str]:
    """Every page basename (without `.md`) in the worktree's `wiki/` tree.

    A wikilink resolves by bare basename, which is what the contract linter does too — so this is
    the same question "does `[[X]]` resolve" is answered with there, asked early enough that the
    refusal names the DECLARATION rather than surfacing later as a dead link on somebody's page.
    """
    names = set()
    root = os.path.join(worktree, "wiki")
    for _parent, _dirs, files in os.walk(root):
        for name in files:
            if name.endswith(".md") and not name.startswith("."):
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
        # `path_key`, not `==`: the filesystem this runs on resolves case and Unicode
        # normalization, so `wiki/notes/target.md` and the `Target.md` this capture just
        # created are ONE file, and an exact string test answered "a different page" for it. Same
        # helper `agent.confined_write` asks the same question with, so the two cannot drift.
        if page_policy.path_key(path) in page_policy.path_keys(created):
            out.append(_finding("own-page",
                                f"declared an edit to {path}, which this capture just created: "
                                f"write the link into the new page instead of declaring an edit "
                                f"to it", path))
            continue
        full = os.path.join(worktree, path)
        # Containment, resolved rather than inferred from the string's shape. Everything above is a
        # shape check; a symlinked DIRECTORY component (a `wiki/playbooks` link merged into the
        # repo) satisfies every one of them and would be written through, outside the worktree, as
        # the worker. `confined_write` resolves-and-contains for the agent's writes; code's own
        # writes get the same treatment rather than relying on the folder-name equality above.
        if not page_policy.is_inside(worktree, path):
            out.append(_finding("outside-worktree",
                                f"declared an edit to {path}, which resolves outside the "
                                f"worktree — no page there belongs to this capture", path))
            continue
        if os.path.islink(full):
            # `open(p, "w")` follows a symlink, so a page that is really a link somewhere else
            # would be edited THROUGH it, as the worker. `apply` opens with `O_NOFOLLOW` as well;
            # this is the half that produces a readable refusal instead of an OSError.
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
