"""The code vetoes: each gate checks one property of the agent's diff and passes or refuses.

A `veto` refuses the commit; a `note` changes nothing. The agent gets ONE corrective retry with
the veto findings handed back; a `repairable=False` veto skips it.

Gates read git's STRUCTURED output (`--raw -z`, per-path blob reads), never a rendered diff: page
content can be spelled to look exactly like diff metadata. Every message names a path, a line
number, a rule id or a category — never the offending value.
"""
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import unicodedata
from dataclasses import dataclass, field

import yaml

from stigmergy import text as textutil
from stigmergy.capture import schema as capture_schema
from stigmergy.librarian import gitcmd
from stigmergy.librarian import page as page_policy
from stigmergy.librarian.errors import LibrarianConfigError

log = logging.getLogger(__name__)

# Whitelist, not a blocklist: a zone added tomorrow is out of bounds by default.
ALLOWED_WRITE_PREFIXES = tuple(f"{folder}/" for folder in page_policy.FOLDER_BY_TYPE.values())

SEVERITY_VETO = "veto"
SEVERITY_NOTE = "note"

# The report renders the category, never the planted text: a payload reproduced is a payload
# delivered twice.
INJECTION_CATEGORIES = ("declare-canonical", "write-outside-lane", "reveal-credentials")


@dataclass(frozen=True)
class Finding:
    """One thing a gate observed: `message` for a HUMAN, `brief` for the agent's corrective
    retry (falling back to `message`). `repairable` defaults to `True` — a wasted retry is
    recoverable, one withheld from a fixable finding is not. `values` carries the VERBATIM
    identifiers for identity comparison; `locator` is a presentation transform, never parsed
    back."""
    gate: str
    code: str
    message: str
    severity: str = SEVERITY_VETO
    locator: str = ""
    brief: str = ""
    repairable: bool = True
    values: tuple[str, ...] = ()


@dataclass
class GateContext:
    """Everything the gates read, assembled once per attempt by `processing.py`. `entries` is the
    authoritative view of the diff — status AND mode. `changes` is a derived property, never a
    second field: a drift between the two lets a page replaced by a symlink slip past the
    body-rewrite gate."""
    worktree: str
    entries: list                      # [gitcmd.DiffEntry, ...] from gitcmd.diff_entries
    added: list                        # [(path, lineno, text), ...] from gitcmd.added_lines
    material: str                      # the captured material, from the evidence store
    outcome: object                    # the agent's own account of what it did (agent.Outcome)
    registry: object                   # kernel.registry.Registry
    linter_path: str = ""
    gitleaks_bin: str = "gitleaks"
    stamped: dict = field(default_factory=dict)   # the server-owned values `_stamp` wrote
    findings: list = field(default_factory=list)

    # A gate is TOLD these caller-scoped facts; it never infers one. Widening happens on the
    # CONTEXT, never on the module constant, which is what keeps an ordinary capture claiming
    # `type: meeting` parked rather than filed.
    write_prefixes: tuple = ALLOWED_WRITE_PREFIXES
    creatable_types: frozenset = field(default_factory=lambda: frozenset(page_policy.FAST_LANE_TYPES))
    # `{folder (no trailing slash): type}`, consulted BEFORE the global `page.type_for_folder`,
    # never instead of it.
    extra_folder_types: dict = field(default_factory=dict)
    # Per-page declarations; empty falls back to the single `ctx.outcome` fields.
    page_declared: dict = field(default_factory=dict)
    # `{new page path: {field: value}}`; falls back to `ctx.stamped`.
    stamped_by_path: dict = field(default_factory=dict)
    # Paths where the provenance fields are LEGITIMATE, server-stamped ones.
    provenance_pages: frozenset = field(default_factory=frozenset)
    # `False` for a caller granting no edit mechanism at all: a status-`M` entry from such a flow
    # is never legitimate, additive or not, because nothing in it could have produced one
    # legitimately. **No caller passes `False` today** — the meeting flow, which did, gained the
    # fast lane's declared-edit mechanism (ADR-038) and now grants it too. The field and its check
    # stay because the property is a CALLER's to declare, not a fact about which flows exist; the
    # branch is exercised by `test_gates_unit.py`'s explicit contexts, which is where its red proof
    # lives now that no production flow reaches it.
    edits_allowed: bool = True

    @property
    def changes(self) -> list[tuple[str, str]]:
        return [(entry.status, entry.path) for entry in self.entries]

    def in_lane(self) -> list:
        """Entries under THIS RUN's `write_prefixes`. Content gates scope themselves through
        here; placement gates look at everything, which is why out-of-lane writes are caught."""
        return [e for e in self.entries if e.path.startswith(self.write_prefixes)]

    def in_lane_writes(self) -> list:
        """In-lane adds and modifies — the surface a secret or PII pattern can reach."""
        return [e for e in self.in_lane() if e.status in ("A", "M")]

    def new_pages(self) -> list[str]:
        return [e.path for e in self.entries if e.status == "A"]

    def in_lane_new_pages(self) -> list[str]:
        """Pages this capture CREATED in the lane — the only ones whose whole content is its
        own doing."""
        return [e.path for e in self.in_lane() if e.status == "A"]

    def in_lane_modified_pages(self) -> list[str]:
        return [e.path for e in self.in_lane() if e.status == "M"]

    def touched_pages(self) -> list[str]:
        return [e.path for e in self.entries]


# ── zone and path: writes stay in the lane ────────────────────────────────────────────────────
# The code `processing._uncreatable_type` routes on: the message is this module's to reword, the
# code is its contract.
TYPE_NOT_CREATABLE = "type-not-creatable"


def gate_zone(ctx: GateContext) -> list[Finding]:
    """Vetoes writes outside the whitelisted folders, deletions, non-additive edits, edits where
    the caller grants no edit mechanism, and pages of an uncreatable type. Reads what the agent
    DID, not what it said: an agent reporting `decision` while writing into `wiki/entities/` is
    caught by the path."""
    out = []
    for entry in ctx.entries:
        status, path = entry.status, entry.path
        if status == "D":
            out.append(Finding("zone", "deletion",
                               f"deleted {path}: the librarian never deletes a file",
                               locator=path))
            continue
        # Refused BY NAME rather than falling through: a typechange (`T`, a page replaced by a
        # symlink) is outside every status the content gates read.
        if status not in ("A", "M"):
            out.append(Finding("zone", "unsupported-change",
                               f"changed {path} in a way the fast lane does not file "
                               f"(git status {status!r}): only adding a page and additively "
                               f"editing one are allowed",
                               locator=path))
            continue
        if not path.startswith(ctx.write_prefixes):
            out.append(Finding("zone", "outside-lane",
                               f"wrote {path}, which is outside the fast lane's folders "
                               f"({', '.join(ctx.write_prefixes)})",
                               locator=path))
            continue
        # An executable bit, a symlink or a gitlink under a page's name is not a page, whatever
        # the path says.
        if entry.new_mode not in ("", gitcmd.REGULAR_FILE_MODE):
            out.append(Finding("zone", "not-a-regular-file",
                               f"wrote {path} with file mode {entry.new_mode}: a page is an "
                               f"ordinary file ({gitcmd.REGULAR_FILE_MODE})",
                               locator=path))
            continue
        # A path under an allowed prefix is not automatically a page: a `.gitattributes` carrying
        # `* -diff` blinds the content gates for every capture filed into that folder afterwards.
        basename = path.rsplit("/", 1)[-1]
        if basename.startswith(".") or not basename.endswith(".md"):
            out.append(Finding("zone", "not-a-page",
                               f"wrote {path}, which is not a page: the fast lane writes "
                               f"`.md` files and never a dotfile",
                               locator=path))
            continue
        # Asked of the STEM, exactly as `processing._write_ordinary_page` asks it: a longer
        # string here would let a title pass the writer and be vetoed as a librarian fault.
        unnameable = page_policy.unnameable_reason(basename.removesuffix(".md"))
        if unnameable:
            out.append(Finding("zone", "unnameable-page",
                               f"wrote {path}, which cannot be filed under that name: "
                               f"{unnameable}",
                               locator=path))
            continue
        # CATCH-ALL, THEREFORE LAST: every more specific diagnosis names something wrong IN
        # ADDITION to being a modification and gets first say. `repairable=False` for EVIDENCE
        # PRESERVATION — `processing.preserve_refused_diff` runs only on the terminal path, so a
        # repairable finding lets the retry's reset erase the trace of an unexplained write.
        # The CODE keeps the name of the flow that motivated it: it is what a preserved refused
        # diff's `# refused by:` header already says on deployed stacks, and renaming it would
        # orphan those artifacts for nothing. The MESSAGE states the rule instead of that flow.
        if status == "M" and not ctx.edits_allowed:
            # The anti-blame clause comes FIRST, before the explanation: `report.failed_system`
            # clamps `reason` at 200 characters and the path can be 95 of them, so a clause after
            # the explanation is truncated out of the report an operator actually reads. Bound
            # measured and pinned in `test_gates_unit.py`.
            out.append(Finding("zone", "meeting-edit-refused",
                               f"modified {path}: a worker defect or worktree interference, not "
                               f"the material — this flow grants no edit mechanism at all; the "
                               f"refused diff is preserved for inspection",
                               locator=path, repairable=False))
            continue
        if status == "A":
            out.extend(_check_created_type(ctx, path))
    return out


def _check_created_type(ctx: GateContext, path: str) -> list[Finding]:
    """The type half of the zone gate for one created page. The folder decides the type, and a
    missing `page_type` must not skip the comparison: silence is not an outcome. `unknown-folder`
    and `type-not-creatable` are defensive, firing only if the two derived views of
    `page.PAGE_TYPES` disagree."""
    implied = _type_for_path(ctx, path)
    if not implied:
        return [Finding("zone", "unknown-folder",
                        f"created {path}, whose folder implies no page type the fast lane files",
                        locator=path)]
    # Asked of `ctx.creatable_types`, never of the global table directly.
    if implied not in ctx.creatable_types:
        reason = page_policy.classify_page_type(implied).reason or "it is not a fast-lane type"
        return [Finding("zone", TYPE_NOT_CREATABLE,
                        f"{path}: the fast lane cannot create a {implied!r} page: {reason}",
                        locator=path)]

    declared = str((ctx.page_declared.get(path) or {}).get("page_type", "")
                   or getattr(ctx.outcome, "page_type", "") or "").lower()
    if not declared:
        return [Finding("zone", "undeclared-type",
                        f"created {path} without declaring a page type: every filed page states "
                        f"its own type, so the folder it landed in can be checked against it",
                        locator=path)]
    if declared != implied:
        return [Finding("zone", "type-folder-mismatch",
                        f"filed {path} as a {declared!r} page, but that folder holds "
                        f"{implied!r} pages",
                        locator=path)]
    return []


def _type_for_path(ctx: GateContext, path: str) -> str:
    """The type a created page's folder implies: `extra_folder_types` before the global
    `page.type_for_folder`, never instead of it."""
    folder = path.rsplit("/", 1)[0] if "/" in path else ""
    if folder in ctx.extra_folder_types:
        return ctx.extra_folder_types[folder]
    return page_policy.type_for_folder(path)


def _base_text(worktree: str, path: str) -> str | None:
    """One modified page as it stands at the base commit, or `None`. Structured output, not a
    rendering: nothing in a page's content can change what this returns."""
    try:
        proc = gitcmd.run("show", f"HEAD:{path}", cwd=worktree, check=False)
    except (UnicodeDecodeError, ValueError):
        return None         # a base blob that is not text: `unreadable-edit`, not an exception
    return proc.stdout if proc.returncode == 0 else None


def _appended_callout_only(base_body: list[str], new_body: list[str]) -> bool:
    """Is `new_body` `base_body` plus nothing but appended callout lines? Every base line must
    survive in place, and every line beyond it be blank or a `>` quote — the only shape
    `page.with_callout` emits."""
    if new_body[:len(base_body)] != base_body:
        return False
    return all(not line.strip() or line.startswith(">") for line in new_body[len(base_body):])


def _related_growth_ok(base_text: str, new_text: str) -> bool:
    """Did the page's `related:` field change only by GAINING links? The one field an additive
    edit may rewrite, and it needs a semantic rather than a byte rule, since a flow list means
    adding a link replaces the line. Identical bytes pass; otherwise the link set must be a
    STRICT superset, so a drop, reorder, reformat or unparseable value is refused."""
    base_block, base_links = page_policy.related_declaration(base_text)
    new_block, new_links = page_policy.related_declaration(new_text)
    if base_block == new_block:
        return True
    if base_links is None or new_links is None:
        return False        # an unprovable before/after must never read as "nothing was lost"
    return set(base_links) < set(new_links)


def gate_body_rewrite(ctx: GateContext) -> list[Finding]:
    """An existing page may gain `related:` links and callouts — never a rewritten body. Proven
    against the base BLOB, never a rendered diff: classifying diff lines by prefix is defeatable
    from page content. An unestablishable "before" is refused rather than assumed additive.

    Every finding is `repairable=False`: a modified page comes from `edits.apply_declared` or
    from nothing, so a corrective brief would tell the agent to repair somebody else's action.
    """
    out = []
    for path in sorted({e.path for e in ctx.entries if e.status == "M"}):
        base_text = _base_text(ctx.worktree, path)
        if base_text is None:
            out.append(Finding("zone", "unreadable-edit",
                               f"the version of {path} this capture started from could not be read "
                               f"out of the base commit, so an additive edit cannot be "
                               f"distinguished from a rewrite; refusing rather than assuming",
                               locator=path, repairable=False))
            continue
        try:
            with open(os.path.join(ctx.worktree, path), encoding="utf-8") as f:
                new_text = f.read()
        except (OSError, UnicodeDecodeError):
            out.append(Finding("zone", "unreadable-edit",
                               f"the modified page {path} could not be read back as text, so what "
                               f"changed in it cannot be established; refusing rather than "
                               f"assuming",
                               locator=path, repairable=False))
            continue

        # Parse before the LINE-span comparisons trust it: an indented line after a flow-style
        # `related:` list is absorbed as a continuation rather than read as a new field.
        new_front_block, _ = page_policy.split_frontmatter(new_text)
        try:
            parsed_front = yaml.safe_load(new_front_block) if new_front_block.strip() else {}
            if parsed_front is not None and not isinstance(parsed_front, dict):
                raise yaml.YAMLError("frontmatter does not parse to a mapping")
        except yaml.YAMLError:
            message = (f"{path}: the frontmatter this edit would commit is not valid YAML, so "
                      f"what this page would actually declare cannot be established — refusing "
                      f"before comparing it against the base version")
            out.append(Finding("zone", "unparseable", message, locator=path, repairable=False))
            continue

        if not _appended_callout_only(page_policy.body_lines(base_text),
                                      page_policy.body_lines(new_text)):
            out.append(Finding("zone", "body-rewrite",
                               f"rewrote existing content in {path}: edits to a page that already "
                               f"exists may only ADD a back-link or a callout",
                               locator=path, repairable=False))
            continue

        base_front, new_front = (page_policy.frontmatter_lines(base_text),
                                 page_policy.frontmatter_lines(new_text))
        base_block, _ = page_policy.related_declaration(base_text)
        new_block, _ = page_policy.related_declaration(new_text)
        base_rest = _without(base_front, base_block)
        new_rest = _without(new_front, new_block)

        if base_rest != new_rest:
            out.append(Finding("zone", "body-rewrite",
                               f"rewrote existing frontmatter in {path}: edits to a page that "
                               f"already exists may only ADD a back-link or a callout",
                               locator=path, repairable=False))
            continue
        if not _related_growth_ok(base_text, new_text):
            out.append(Finding("zone", "body-rewrite",
                               f"changed the `related:` field of {path} without adding to it: an "
                               f"edit to a page that already exists may only GROW its link list",
                               locator=path, repairable=False))
    return out


def _without(lines: list[str], block: list[str]) -> list[str]:
    """`lines` with the first contiguous occurrence of `block` removed; an empty block removes
    nothing."""
    if not block:
        return list(lines)
    for index in range(len(lines) - len(block) + 1):
        if lines[index:index + len(block)] == block:
            return lines[:index] + lines[index + len(block):]
    return list(lines)


# ── a page is text: the precondition every content gate depends on ────────────────────────────
def gate_binary_page(ctx: GateContext) -> list[Finding]:
    """Every page this capture wrote must be readable UTF-8 text with no NUL bytes. Runs BEFORE
    the content gates: one NUL byte makes `git diff` emit no content lines for the blob, silently
    disabling the secrets, PII and body-rewrite gates at once. The message names the PATH only,
    never an offset into content just judged unsafe."""
    out = []
    for entry in ctx.in_lane_writes():
        full = os.path.join(ctx.worktree, entry.path)
        try:
            with open(full, "rb") as f:
                data = f.read()
        except OSError:
            out.append(Finding("binary", "unreadable-page",
                               f"{entry.path} could not be read back after it was written",
                               locator=entry.path))
            continue
        if b"\x00" in data:
            out.append(Finding("binary", "binary-page",
                               f"{entry.path} contains a NUL byte: a page is UTF-8 text, and a "
                               f"page git treats as binary cannot be gate-checked at all",
                               locator=entry.path))
            continue
        try:
            data.decode("utf-8")
        except UnicodeDecodeError:
            out.append(Finding("binary", "binary-page",
                               f"{entry.path} is not valid UTF-8: a page is UTF-8 text, and a "
                               f"page git treats as binary cannot be gate-checked at all",
                               locator=entry.path))
    return out


# ── secrets: gitleaks, the independent ground truth ───────────────────────────────────────────
def ensure_scanner(gitleaks_bin: str) -> None:
    """Startup check. A missing scanner is a CONFIG error, not a per-item failure: without it the
    secrets gate would silently pass everything."""
    try:
        proc = subprocess.run([gitleaks_bin, "version"], capture_output=True, text=True)
    except (OSError, ValueError) as ex:
        raise LibrarianConfigError(
            f"the secret scanner {gitleaks_bin!r} is not runnable ({ex.__class__.__name__}) — "
            f"install gitleaks or set $STIGMERGY_GITLEAKS_BIN. The librarian will not run without "
            f"it: a secrets gate that silently passes is worse than no gate") from ex
    if proc.returncode != 0:
        raise LibrarianConfigError(
            f"the secret scanner {gitleaks_bin!r} exited {proc.returncode} on `version`")


def _gitleaks_dir(scratch: str, gitleaks_bin: str) -> list[dict]:
    """Run gitleaks over one directory and return its raw hits. `--redact`: a finding carries the
    rule id and line, never the matched value. `--exit-code 0` because hits are read from the
    JSON report, so a non-zero exit stays distinguishable from the binary crashing."""
    proc = subprocess.run(
        [gitleaks_bin, "dir", scratch, "--no-banner", "--redact", "--exit-code", "0",
         "--report-format", "json", "--report-path", "-"],
        capture_output=True, text=True)
    if proc.returncode != 0:
        raise LibrarianConfigError(
            f"the secret scanner failed to run (rc={proc.returncode}); refusing to file without "
            f"a working secrets gate")
    try:
        return json.loads(proc.stdout or "[]") or []
    except json.JSONDecodeError:
        raise LibrarianConfigError("the secret scanner returned unparseable output") from None


# gitleaks matches WITHIN a line, and text extraction hard-wraps long tokens, so a credential in
# a dropped PDF arrives already split and matches no rule. Every surface is scanned twice: as
# written, and with adjacent PAIRS of lines rejoined — never the whole document glued into one,
# which would hand entropy rules a kilometre of text and bounce real work.
_REJOINED_SUBDIR = ".stigmergy-rejoined"


def _rejoined(text: str) -> str:
    lines = (text or "").split("\n")
    return "\n".join(a.rstrip() + b.lstrip() for a, b in zip(lines, lines[1:], strict=False))


def _write_rejoined(scratch: str, rel: str, text: str) -> None:
    destination = os.path.join(scratch, _REJOINED_SUBDIR, rel)
    os.makedirs(os.path.dirname(destination), exist_ok=True)
    with open(destination, "w", encoding="utf-8") as f:
        f.write(_rejoined(text))


def _hit_surface(scratch: str, hit) -> tuple[str, bool]:
    """One hit's path relative to the scanned surface, and whether it came from a rejoined copy.
    Also what keeps the scratch directory out of a message a person reads."""
    relative = os.path.relpath(str(hit.get("File", "")), scratch)
    parts = relative.split(os.sep)
    if parts[0] == _REJOINED_SUBDIR:
        return os.sep.join(parts[1:]), True
    return relative, False


def _secret_findings(scratch: str, hits, label_for) -> list[Finding]:
    # Both copies find a secret on ONE line, and the real copy carries the author's own line
    # number, so the rejoined copy only speaks where the real one was silent.
    on_one_line = {(_hit_surface(scratch, h)[0], h.get("RuleID", "unknown"))
                   for h in hits if not _hit_surface(scratch, h)[1]}
    out = []
    for hit in hits:
        relative, rejoined = _hit_surface(scratch, hit)
        label, rule = label_for(relative), hit.get("RuleID", "unknown")
        if rejoined and (relative, rule) in on_one_line:
            continue
        # `values` carries (line, rule) structurally. An empty line means "visible only once
        # adjacent lines were rejoined" — a fact about the finding, not a missing value.
        if rejoined:
            out.append(Finding("secrets", "secret",
                              f"a likely secret was matched in {label}, split across a line "
                              f"break (rule: {rule})", locator=label, values=("", rule)))
        else:
            line = str(hit.get("StartLine", "?"))
            out.append(Finding("secrets", "secret",
                              f"a likely secret was matched near line {line} of {label} "
                              f"(rule: {rule})", locator=f"{label}:{line}", values=(line, rule)))
    return out


def scan_secrets(text: str, *, gitleaks_bin: str, label: str) -> list[Finding]:
    """Run gitleaks over one blob of text. `label` names the surface for the refusal message."""
    with tempfile.TemporaryDirectory(prefix="stigmergy-gitleaks-") as scratch:
        # A file, not stdin: `gitleaks stdin` reports no line numbers, and the message exists
        # to tell a person WHERE to look.
        with open(os.path.join(scratch, "capture.md"), "w", encoding="utf-8") as f:
            f.write(text or "")
        _write_rejoined(scratch, "capture.md", text or "")
        hits = _gitleaks_dir(scratch, gitleaks_bin)
        return _secret_findings(scratch, hits, lambda _rel: label)


def scan_worktree_files(worktree: str, rel_paths, *, gitleaks_bin: str) -> list[Finding]:
    """gitleaks over COPIES OF THE FILES THEMSELVES, not a rendered diff: these are the bytes
    that would be committed. Paths are reproduced in the scratch directory so a hit maps back to
    the real page."""
    rel_paths = [p for p in rel_paths if p]
    if not rel_paths:
        return []
    with tempfile.TemporaryDirectory(prefix="stigmergy-gitleaks-disk-") as scratch:
        for rel in rel_paths:
            destination = os.path.join(scratch, rel)
            os.makedirs(os.path.dirname(destination), exist_ok=True)
            try:
                shutil.copyfile(os.path.join(worktree, rel), destination)
            except OSError:
                continue        # `gate_binary_page` already vetoed anything unreadable
            try:
                with open(destination, encoding="utf-8") as f:
                    _write_rejoined(scratch, rel, f.read())
            except (OSError, UnicodeDecodeError):
                continue
        hits = _gitleaks_dir(scratch, gitleaks_bin)
        return _secret_findings(scratch, hits, lambda rel: rel or "the drafted page")


def gate_secrets(ctx: GateContext) -> list[Finding]:
    """gitleaks over two surfaces: new pages whole off disk, and edits as the diff's added lines
    — a secret already in the repo is not this capture's doing. An empty added-lines list is a
    VETO whenever the diff claims an in-lane edit, since anything that empties the diff would
    otherwise turn this gate off silently; `repairable=False`, because the gate could not run."""
    new_pages = ctx.in_lane_new_pages()
    out = list(scan_worktree_files(ctx.worktree, new_pages, gitleaks_bin=ctx.gitleaks_bin))

    # New pages excluded: the on-disk pass above covered every byte with a better locator.
    edited = set(ctx.in_lane_modified_pages())
    added = "\n".join(text for path, _, text in ctx.added if path in edited)
    if added.strip():
        out += scan_secrets(added, gitleaks_bin=ctx.gitleaks_bin, label="the drafted page")
    elif edited:
        paths = ", ".join(sorted(edited))
        out.append(Finding("secrets", "unscanned-diff",
                           f"the diff produced no readable added lines for {paths}; refusing "
                           f"rather than passing unscanned",
                           repairable=False))
    return out


# ── PII: four high-value patterns, deliberately short ─────────────────────────────────────────
# Emails and personal names deliberately do NOT bounce: they are the normal tissue of a company
# brain and `submitted_by` is literally an email.
_PII_PATTERNS = (
    ("a private key", re.compile(r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----")),
    ("an IBAN", re.compile(r"\b[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]{4}){3,7}\b")),
    ("a DNI/NIE number", re.compile(r"\b(?:[0-9]{8}[A-HJ-NP-TV-Z]|[XYZ][0-9]{7}[A-HJ-NP-TV-Z])\b")),
)
_CARD_RE = re.compile(r"\b(?:\d[ -]?){13,19}\b")


def _luhn(digits: str) -> bool:
    """Luhn checksum. A 16-digit number that FAILS it is not a card — an order id, a phone
    number, a hash prefix — and must not bounce someone's work."""
    total, alternate = 0, False
    for char in reversed(digits):
        value = int(char)
        if alternate:
            value *= 2
            if value > 9:
                value -= 9
        total += value
        alternate = not alternate
    return total % 10 == 0


def scan_pii(lines) -> list[Finding]:
    """The four patterns over `[(path, lineno, text), ...]`. Names the KIND and a locator,
    never the value."""
    out = []
    for path, lineno, text in lines:
        for label, pattern in _PII_PATTERNS:
            if pattern.search(text):
                out.append(Finding("pii", "pii",
                                   f"what looks like {label} near line {lineno}",
                                   locator=f"{path}:{lineno}"))
        for match in _CARD_RE.finditer(text):
            digits = re.sub(r"\D", "", match.group(0))
            if 13 <= len(digits) <= 19 and _luhn(digits):
                out.append(Finding("pii", "pii",
                                   f"what looks like a card number near line {lineno}",
                                   locator=f"{path}:{lineno}"))
    return out


def gate_pii(ctx: GateContext) -> list[Finding]:
    """The four patterns over the same two surfaces, and the same empty-input veto, as
    `gate_secrets`."""
    out = []
    for path in ctx.in_lane_new_pages():
        try:
            with open(os.path.join(ctx.worktree, path), encoding="utf-8") as f:
                lines = f.read().splitlines()
        except (OSError, UnicodeDecodeError):
            continue            # `gate_binary_page` already vetoed it
        out += scan_pii([(path, n, text) for n, text in enumerate(lines, start=1)])

    edited = set(ctx.in_lane_modified_pages())
    added = [(path, n, text) for path, n, text in ctx.added if path in edited]
    if added:
        out += scan_pii(added)
    elif edited:
        paths = ", ".join(sorted(edited))
        out.append(Finding("pii", "unscanned-diff",
                           f"the diff produced no readable added lines for {paths}; refusing "
                           f"rather than passing unscanned",
                           repairable=False))
    return _dedupe(out)


def _dedupe(findings) -> list[Finding]:
    """Drop exact duplicates: the scanning surfaces overlap by design, and a refusal shows one
    line per real problem, not one per surface that noticed it."""
    seen, out = set(), []
    for finding in findings:
        key = (finding.gate, finding.code, finding.message)
        if key not in seen:
            seen.add(key)
            out.append(finding)
    return out


# Matched on the check id, never the message: the message is the linter's to reword.
ORPHAN_CHECK = "orphans"
DEAD_LINKS_CHECK = "dead_links"
FRONTMATTER_CHECK = "frontmatter"

# The one place the linter's dead-link message shape is read back by anything but a human.
_DEAD_LINK_TARGET_RE = re.compile(r"dead link: \[\[(.+)\]\]\s*$")

# A `FRONTMATTER_CHECK` message diagnoses the block but never says WHOSE field it is, so the
# brief states the ownership split rather than leaving the one retry to rediscover it.
FRONTMATTER_FACTS = (
    "The worker stamps `status`, `as_of`, `submitted_by`, `entity` and `acl` after your draft — "
    "do not add them yourself. Every other required field — `type`, `title`, `created`, "
    "`updated`, `tags` — must already be in the page file's frontmatter block, exactly as "
    "`ops/templates/<type>.md` declares."
)


def dead_link_target(finding: "Finding") -> str:
    """The wikilink TARGET a `("contract", "dead_links")` finding names — `locator` is the FILE
    the dead link lives on, not what it points at. `""` when the message does not match the
    linter's shape."""
    m = _DEAD_LINK_TARGET_RE.search(finding.message or "")
    return m.group(1).strip() if m else ""


def gate_contract(ctx: GateContext) -> list[Finding]:
    """`stigmergy_lint.py` over the worktree, filtered to the files this capture touched — the
    knowledge repo's own gate, so the librarian is held to a human PR's standard. Only `error`
    vetoes; warnings become notes. `orphans` on a page this capture just created is dropped: it
    fires on every filed page by construction. On a pre-existing page it is real and stays."""
    if not ctx.linter_path or not os.path.exists(ctx.linter_path):
        raise LibrarianConfigError(
            f"the contract linter is missing at {ctx.linter_path!r} — it is the knowledge "
            f"repo's own gate and the librarian will not file without it")
    proc = subprocess.run(
        ["python3", ctx.linter_path, "--repo", ctx.worktree, "--json"],
        capture_output=True, text=True,
        # An EXPLICIT environment: a script out of the repo the librarian CURATES must not
        # inherit the App private key or the queue DSN. This prevents secrets being HANDED to
        # it; it cannot prevent a same-uid process TAKING them.
        env=gitcmd.base_env())
    if proc.returncode == 2 or not proc.stdout.strip():
        raise LibrarianConfigError(
            f"the contract linter could not scan the worktree (rc={proc.returncode})")
    try:
        report = json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise LibrarianConfigError("the contract linter returned unparseable output") from None

    touched = set(ctx.touched_pages())
    born_here = set(ctx.in_lane_new_pages())
    out = []
    for finding in report.get("findings", []):
        if finding.get("file") not in touched:
            continue
        if finding.get("check") == ORPHAN_CHECK and finding.get("file") in born_here:
            continue
        severity = SEVERITY_VETO if finding.get("severity") == "error" else SEVERITY_NOTE
        message = f"{finding.get('file')}: {finding.get('message')}"
        brief = f"{message}\n{FRONTMATTER_FACTS}" if finding.get("check") == FRONTMATTER_CHECK else ""
        out.append(Finding("contract", finding.get("check", "contract"), message,
                           severity=severity, locator=str(finding.get("file", "")), brief=brief))
    return out


# ── anchoring: nothing is filed ownerless ─────────────────────────────────────────────────────
# The code `processing._refuse` routes on: the message is this module's to reword, the code is
# its contract.
ANCHORING_UNRESOLVED = "unresolved"


def _declared_entities(anchoring: dict) -> list[str]:
    """The raw `anchoring.entities` list, defensively: `ctx.outcome` comes off whatever backend
    produced it, so `parse_outcome`'s normalization is not assumed."""
    raw = anchoring.get("entities") if isinstance(anchoring, dict) else None
    if raw is None:
        return []
    return [str(v) for v in raw] if isinstance(raw, (list, tuple)) else [str(raw)]


def resolve_entity_ids(anchoring: dict, registry) -> tuple[list[str], list[str]]:
    """`(ids, unresolved)` for one anchoring outcome; `([], [])` for company-wide scope. A PAIR,
    because `ids` alone folds two states onto `[]`: company-wide (the RIGHT `[]`) and "declared
    an entity anchor but nothing resolved" (the WRONG `[]`, which must never reach a committed
    page). Raises on a falsy `registry`, deliberately uncaught, for the same reason."""
    anchoring = anchoring if isinstance(anchoring, dict) else {}
    if str(anchoring.get("kind", "")).lower() != "entity":
        return [], []
    ids, unresolved = [], []
    for raw in _declared_entities(anchoring):
        cid = registry.canonical_id(raw)
        if cid:
            ids.append(cid)
        else:
            unresolved.append(raw)
    return list(dict.fromkeys(ids)), unresolved


def gate_anchoring(ctx: GateContext) -> list[Finding]:
    """Every filed page declares an anchoring outcome, and the declaration is CHECKED: `entity`
    values must resolve through the registry read at the base commit, `company` scope needs a
    written reason (only "non-empty string" is checkable, never that it is TRUE). `unresolved`
    always carries a `locator`, so an empty `entities` list reaches the steward park rather than
    a system fault. Per-page when `ctx.page_declared` is populated."""
    new_pages = ctx.in_lane_new_pages()
    if not new_pages:
        return []

    if ctx.page_declared:
        return _per_page_anchoring(ctx, new_pages)
    return _anchoring_outcome_findings(ctx, getattr(ctx.outcome, "anchoring", None) or {})


def _anchoring_outcome_findings(ctx: GateContext, anchoring: dict, *,
                                path: str = "") -> list[Finding]:
    """The check over ONE declared anchoring outcome, wherever it was declared — the whole rule,
    in one place, so the per-page road and the per-capture road cannot start answering differently.

    A `path` names the page whose declaration this is: it prefixes the message and becomes the
    `locator` for the two findings that have nothing else to point at. The unresolved finding
    locates the NAME on both roads, because that is the name a parked report tells a steward to
    register; its `values` carry every declared name VERBATIM, so `processing._unanchorable` can
    match a companion dead link against any of them, not only the one picked for display.
    """
    prefix = f"{path}: " if path else ""
    kind = str(anchoring.get("kind", "")).lower()
    if kind == "company":
        if not str(anchoring.get("reason", "")).strip():
            return [Finding("anchoring", "no-reason",
                            f"{prefix}declared company-wide scope with no written reason: a page "
                            f"that belongs to no entity must say why in a sentence",
                            locator=path)]
        return []
    if kind != "entity":
        # The per-page sentence says "every FILED page" where the per-capture one says "every
        # page". Carried, not unified: no test pins either spelling, so collapsing them here would
        # silently reword what a submitter reads on one of the two roads.
        every = "filed page" if path else "page"
        return [Finding("anchoring", "undeclared",
                        f"{prefix}filed a page without declaring an anchoring outcome: every "
                        f"{every} names the entity it belongs to, or declares company-wide scope "
                        f"with a reason",
                        locator=path)]
    declared = _declared_entities(anchoring)
    # `_ids` is unused, but one function must decide what "resolves" means for write and veto.
    _ids, unresolved = resolve_entity_ids(anchoring, ctx.registry)
    if unresolved or not declared:
        return [Finding("anchoring", ANCHORING_UNRESOLVED,
                        f"{prefix}{_unresolved_message(declared, unresolved)}",
                        locator=_unresolved_name(declared, unresolved),
                        brief=anchoring_brief(ctx, declared),
                        values=tuple(unresolved or declared))]
    return []


def _per_page_anchoring(ctx: GateContext, new_pages: list[str]) -> list[Finding]:
    """One anchoring veto per new page declaring an anchoring outcome in `ctx.page_declared`. A
    page with no `"anchoring"` key is not asked at all: a provenance record has nothing to
    anchor, and the caller puts the key only on pages that need one."""
    out = []
    for path in sorted(new_pages):
        declared_for_page = ctx.page_declared.get(path) or {}
        if "anchoring" not in declared_for_page:
            continue
        out += _anchoring_outcome_findings(ctx, declared_for_page.get("anchoring") or {},
                                           path=path)
    return out


def _unresolved_message(declared: list[str], unresolved: list[str]) -> str:
    """Names EVERY unresolved id, not just the first, so one repair pass fixes all of them."""
    if not declared:
        return ('declared an entity anchor, but "anchoring.entities" names no entity at all')
    ids = unresolved or declared
    quoted = [f'"{_one_line(i, MAX_BRIEF_NAME_LEN)}"' for i in ids[:MAX_BRIEF_ITEMS]]
    phrase = quoted[0] if len(quoted) == 1 else ", ".join(quoted[:-1]) + f" and {quoted[-1]}"
    rest = len(ids) - len(quoted)
    if rest > 0:
        phrase += f", and {rest} more"
    noun = "anchor" if len(ids) == 1 else "anchors"
    verb = "does" if len(ids) == 1 else "do"
    return (f"declared entity {noun} {phrase} {verb} not resolve in the entity registry read at "
            f"the base commit")


# ── the corrective brief: written for an agent that must repair ────────────────────────────────
# Echoed names are clamped and control-stripped: a brief goes into the next pass's PROMPT, where
# a name carrying newlines could forge the brief's own structure.
MAX_BRIEF_ITEMS = 12
MAX_BRIEF_NAME_LEN = 80

# Past this size the brief gives a count instead of the names: a TRUNCATED candidate list is
# worse than none, because "not in the list" reads as proof re-anchoring is unavailable.
MAX_BRIEF_REGISTRY_NAMES = 40


def _one_line(text: str, limit: int) -> str:
    """One bounded, single-line, control-character-free rendering of an untrusted name. A DISPLAY
    transform, never an identity key: truncation makes distinct names render alike and NFD-vs-NFC
    spellings render apart, so identity comparisons use `normalize_identifier`. No
    fence-neutralization: that step belongs to the fence's own callers."""
    return textutil.one_line(text, limit)


def normalize_identifier(text: str) -> str:
    """The identity key: NFC + casefold + whitespace-collapse, because the two producers compared
    (a declared anchoring value, a wikilink target) need not agree on incidental spacing. No
    clamp: an NFD-spelled accented name must match its NFC twin.
    """
    return " ".join(unicodedata.normalize("NFC", text or "").split()).casefold()


def _listed_names(names, *, limit: int) -> str:
    """`a, b, c` — bounded, and saying so when it stopped early rather than trailing off."""
    names = [_one_line(name, MAX_BRIEF_NAME_LEN) for name in names]
    shown = names[:limit]
    rest = len(names) - len(shown)
    return ", ".join(shown) + (f", and {rest} more" if rest > 0 else "")


def registry_candidates(registry) -> list[dict]:
    """Every entity the registry would resolve. THE one reading of "which entities exist" outside
    `kernel.registry`, shared by `anchoring_brief` and `report.needs_input`: a candidate list
    differing from the one the gate asks is worse than none. Aliases are included so a name
    registered under another spelling does not invite "it's new" and mint a duplicate entity."""
    entities = getattr(registry, "entities", None) or {}
    out = []
    for entity in entities.values():
        if not isinstance(entity, dict) or not entity.get("name"):
            continue
        aliases = entity.get("aliases")
        out.append({"name": str(entity["name"]),
                    "aliases": [str(a) for a in (aliases if isinstance(aliases, list) else [])]})
    return sorted(out, key=lambda e: e["name"])


def _registry_id_names(registry) -> list[tuple[str, str]]:
    """`(id, name)` for every entity, sorted by id: the brief asks the agent to declare an ID, so
    the ids themselves are what it has to show."""
    entities = getattr(registry, "entities", None) or {}
    out = [(str(cid), str(e["name"])) for cid, e in entities.items()
          if isinstance(e, dict) and e.get("name")]
    return sorted(out)


def _unresolved_name(declared: list[str], unresolved: list[str]) -> str:
    """WHICH name could not be resolved — the one a parked report tells a steward to register.
    Never `""`: `processing._unanchorable` needs a truthy locator so "nothing here anchors"
    reaches the steward park rather than a system fault. The placeholder is one shared spelling
    for "nothing was named", so `entities.cli._suggestable` can refuse it as a `--name`."""
    for name in [*unresolved, *declared]:
        cleaned = _one_line(name, MAX_BRIEF_NAME_LEN)
        if cleaned:
            return cleaned
    return capture_schema.UNNAMED_ENTITY_PLACEHOLDER


def anchoring_brief(ctx: GateContext, declared: list[str]) -> str:
    """What an agent whose entity anchor did not resolve is told, so it can repair it. Built from
    the OUTCOME's declared `anchoring.entities` and the registry, never from the page, which this
    gate does not read. It names registry CONTENTS rather than a path (an ephemeral worktree may
    hold no such file) and closes by ruling out rewriting the page, which cannot work here."""
    id_names = _registry_id_names(ctx.registry)
    if not id_names:
        registry_line = ("the registry loaded for this run holds NO entities at all, so outcome "
                         "1 below is unavailable — no id can resolve on this pass")
    elif len(id_names) <= MAX_BRIEF_REGISTRY_NAMES:
        # Into the NEXT pass's prompt, where a newline in a name forges the brief's structure.
        rendered = ", ".join(f"{_one_line(cid, MAX_BRIEF_NAME_LEN)} — {_one_line(name, MAX_BRIEF_NAME_LEN)}"
                             for cid, name in id_names[:MAX_BRIEF_REGISTRY_NAMES])
        registry_line = (f"the registry loaded for this run resolves exactly these "
                         f"{len(id_names)} ids (id — name): {rendered}")
    else:
        registry_line = (f"the registry loaded for this run resolves {len(id_names)} ids, too "
                         f"many to list here; none of them is what this outcome declared")
    declared_line = (_listed_names(declared, limit=MAX_BRIEF_ITEMS)
                     if declared else
                     'nothing — "anchoring.entities" is empty')

    lines = [
        'no entity anchor resolved. The gate read THIS OUTCOME\'S declared "anchoring.entities" '
        'list and asked the entity registry (loaded at the base commit — this list, not the file '
        'in the checkout, is what the gate asks) to resolve each one; none resolved.',
        f"  declared — {declared_line}",
        f"  registry — {registry_line}",
        "Three outcomes satisfy this gate. Pick the one that is TRUE of the material, not the one "
        "that files:",
    ]
    if id_names:
        lines.append(
            '  1. ANCHOR — declare a registry id from the list above, spelled exactly as it is '
            'listed there (an id, never a display name or alias): "anchoring": {"kind": "entity", '
            '"entities": ["<that id>"]}. This is a change to the OUTCOME only — nothing on the '
            'page itself is checked or needs to change.')
    else:
        lines.append(
            "  1. ANCHOR — unavailable on this pass: the registry is empty, so no id resolves.")
    lines += [
        '  2. COMPANY-WIDE — if the material genuinely belongs to no single entity, change only '
        'the outcome: "anchoring": {"kind": "company", "reason": "<one sentence saying why it '
        'belongs to no entity>"}. The reason is required; an empty string is not one.',
        '  3. PARK — if the material really is about a specific entity nobody has registered yet, '
        'do NOT file it: "decision": "triage", "triage": {"kind": "unresolved-entity", "name": '
        '"<the name>"}, and write no page. Nothing is committed and a steward registers the '
        'entity. This is a correct outcome, not a failure.',
        "Adding a wikilink, rewriting the body, or renaming the page does not change this gate's "
        "answer: it reads the declared \"entities\" list against the registry named above, and "
        "only that registry.",
    ]
    return "\n".join(lines)


# ── server-owned frontmatter: the post-condition, checked by a real YAML parser ────────────────
# Never present on a filed page. `owner` matters most: it says who ANSWERS for a page, and a
# captured document must not assign that to a third party.
FORBIDDEN_PAGE_KEYS = ("owner", "id", "content_hash", "tier", "extracted_at")

# Legitimate ONLY on `ctx.provenance_pages`, a fact the caller declares and no gate infers.
# Named once so the exemption above and the duplicate backstop read the SAME set.
PROVENANCE_PAGE_KEYS = ("content_hash", "tier", "extracted_at", "id")

# Refused OUTRIGHT before any YAML parsing: no legitimate place in this repo's page dialect.
_BOM = "\ufeff"  # U+FEFF, the UTF-8 BOM
_EXPLICIT_KEY_RE = re.compile(r"^\?\s")

# A merge key also escapes `page.duplicate_top_level_keys`, which cannot see a key contributed by
# the aliased node one level below `root.value`.
_MERGE_KEY_RE = re.compile(r"^<<\s*:")

# Enumerating confusable spellings (homoglyphs, small-caps, zero-width joiners, combining marks)
# does not converge — none folds to an ASCII `SERVER_OWNED_KEYS` name, so each survives
# `page.stamp_server_fields`' strip — so the control inverts into a whitelist: a top-level key
# must look like a plain lowercase ASCII identifier or the page is refused.
_ALLOWED_KEY_RE = re.compile(r"^[a-z_][a-z0-9_.-]*$")


def gate_frontmatter(ctx: GateContext) -> list[Finding]:
    """The stamped frontmatter is what a real YAML PARSER says it is, not what our line editor
    thinks: `page.stamp_server_fields` rewrites lines rather than round-tripping YAML, so a check
    on the OUTPUT cannot be defeated by a new spelling of the input.

    A duplicate declaration of a server-owned key is refused outright: PyYAML takes the LAST
    occurrence and the server's line is appended last, so the value comparison alone cannot prove
    the capture's own declaration was removed. Runs on MODIFIED pages too, since
    `gate_body_rewrite` judges the SHAPE of a change, never whether a VALUE is legitimate.
    """
    out = []
    for path in ctx.in_lane_new_pages():
        expected = dict(ctx.stamped_by_path.get(path) or ctx.stamped or {})
        out.extend(_frontmatter_findings(ctx, path, expected=expected))
    for path in ctx.in_lane_modified_pages():
        out.extend(_frontmatter_findings(ctx, path, expected={}))
    return out


def _frontmatter_findings(ctx: GateContext, path: str, *, expected: dict) -> list[Finding]:
    """The structural checks `gate_frontmatter` runs per page. The new-page and modified-page
    loops differ only in `expected`, the stamped values a parsed value must equal."""
    try:
        with open(os.path.join(ctx.worktree, path), encoding="utf-8") as f:
            front, _ = page_policy.split_frontmatter(f.read())
    except (OSError, UnicodeDecodeError):
        return []            # `gate_binary_page` already vetoed it

    out = []
    if _BOM in front:
        out.append(Finding("frontmatter", "forged-field",
                           f"{path}: its frontmatter contains a Unicode BOM (U+FEFF), which "
                           f"has no legitimate place inside a frontmatter block — refused "
                           f"outright",
                           locator=path))
    if any(_EXPLICIT_KEY_RE.match(line) for line in front.splitlines()
          if not line[:1].isspace()):
        out.append(Finding("frontmatter", "forged-field",
                           f"{path}: its frontmatter uses YAML explicit-key syntax (`? key`), "
                           f"which is not part of this repo's page dialect — refused outright",
                           locator=path))
    if any(_MERGE_KEY_RE.match(line) for line in front.splitlines()
          if not line[:1].isspace()):
        out.append(Finding("frontmatter", "forged-field",
                           f"{path}: its frontmatter uses a YAML merge key (`<<:`), which is "
                           f"not part of this repo's page dialect — refused outright",
                           locator=path))

    # The provenance group too: a duplicate is a forged line beside the server's stamped one.
    dup_keys = page_policy.duplicate_top_level_keys(front) & (
        set(page_policy.SERVER_OWNED_KEYS) | set(PROVENANCE_PAGE_KEYS))
    for key in sorted(dup_keys):
        out.append(Finding("frontmatter", "forged-field",
                           f"{path}: it declares {key!r} more than once — a server-owned "
                           f"field must appear exactly once, and a duplicate can hide a "
                           f"capture's own attempt behind whichever occurrence a YAML parser "
                           f"happens to read last",
                           locator=path))
    try:
        parsed = yaml.safe_load(front) if front.strip() else {}
    except yaml.YAMLError:
        out.append(Finding("frontmatter", "unparseable",
                           f"{path}: its frontmatter is not valid YAML, so what the page "
                           f"actually declares cannot be established",
                           locator=path))
        return out
    if parsed is None:
        parsed = {}
    if not isinstance(parsed, dict):
        out.append(Finding("frontmatter", "unparseable",
                           f"{path}: its frontmatter does not parse to a mapping",
                           locator=path))
        return out

    # A key that is not even a string (a bare `123:`) cannot match and is refused the same way.
    for key in parsed:
        if not isinstance(key, str) or not _ALLOWED_KEY_RE.match(key):
            out.append(Finding("frontmatter", "forged-field",
                               f"{path}: it declares the top-level key {key!r}, which is not "
                               f"a plain lowercase identifier (`^[a-z_][a-z0-9_.-]*$`) — "
                               f"refused outright, whatever it resembles",
                               locator=path))

    # `normalize_key` on both sides: `Owner:` and its homoglyph twins parse to a DIFFERENT dict
    # key than `owner`, so a raw `in` test against `parsed` would miss them.
    normalized_parsed_keys = {page_policy.normalize_key(k) for k in parsed if isinstance(k, str)}
    # Only READ, never recomputed: the caller declares which pages are source pages.
    provenance_ok = path in ctx.provenance_pages
    for key in FORBIDDEN_PAGE_KEYS:
        if key in PROVENANCE_PAGE_KEYS and provenance_ok:
            continue
        if page_policy.normalize_key(key) not in normalized_parsed_keys:
            continue
        out.append(Finding("frontmatter", "forbidden-field",
                           f"{path}: the filed page still declares {key!r}, which the "
                           f"fast lane never sets and a capture may never assert",
                           locator=path))
    for key, expected_value in expected.items():
        if expected_value is None:
            if key in parsed:
                out.append(Finding("frontmatter", "forged-field",
                                   f"{path}: it declares {key!r}, which the server resolved "
                                   f"to nothing and the page must therefore omit",
                                   locator=path))
            continue
        # Text on both sides: `as_of: 2026-07-26` parses to a `date`, and the question is what
        # the page says, not which Python type PyYAML chose for it.
        if _as_text(parsed.get(key)) != _as_text(expected_value):
            out.append(Finding("frontmatter", "forged-field",
                               f"{path}: its {key!r} is not the value the server stamped — "
                               f"that field is computed by the server and a capture's own "
                               f"declaration of it is ignored",
                               locator=path))
    return out


def _as_text(value) -> str:
    """One comparable spelling for a frontmatter value, whatever YAML made of it."""
    if isinstance(value, (list, tuple)):
        return ",".join(str(v) for v in value)
    return str(value)


# ── orchestration ─────────────────────────────────────────────────────────────────────────────
# Every gate runs every time: there is one corrective retry, and a gate hiding a second problem
# would waste it. `gate_binary_page`'s position IS load-bearing — it establishes that pages are
# text at all, the precondition the text-reading gates assume and one NUL byte turns off.
ALL_GATES = (gate_zone, gate_binary_page, gate_body_rewrite, gate_secrets, gate_pii,
             gate_frontmatter, gate_contract, gate_anchoring)


def run_gates(ctx: GateContext, gates=ALL_GATES) -> list[Finding]:
    """Run every gate over one attempt's diff and return every finding, vetoes first."""
    findings = []
    for gate in gates:
        findings.extend(gate(ctx))
    findings.sort(key=lambda f: (f.severity != SEVERITY_VETO, f.gate, f.code))
    return findings


def vetoes(findings) -> list[Finding]:
    return [f for f in findings if f.severity == SEVERITY_VETO]


def unrepairable(findings) -> list[Finding]:
    """The vetoes that name no repair the agent can perform — empty when the retry is worth
    taking. ANY unrepairable veto stops the retry: it exists to reach a pass with NO vetoes, and
    one that cannot clear makes that unreachable. Whether `deletion` / `unsupported-change` /
    `not-a-regular-file` belong here too is an OPEN QUESTION, flagged so silence is not an
    answer."""
    return [f for f in vetoes(findings) if not f.repairable]


def corrective_brief(findings, *, reset: bool = True) -> str:
    """What the agent is told on its one corrective retry: the vetoes, as instructions to REPAIR.
    A brief owes three things a message does not: what the gate EXAMINED, which outcomes are
    AVAILABLE, and the SMALLEST edit reaching one. The preamble deliberately does not say "write
    the page again", since parking is a valid repair.

    `reset`: the preamble is a CLAIM about what just happened, so it must be true. `False` for a
    caller with no worktree to reset, where "nothing you wrote is still on disk" would be a lie.
    """
    lines = [_brief_item(f) for f in vetoes(findings)]
    preamble = ("The gates refused this draft, and the worktree has been reset to the commit it "
               "branched from — nothing you wrote is still on disk. " if reset else
               "The gates refused this proposal; nothing was committed or pushed. ")
    return (preamble
           + "Every point below has to be resolved. Each one says what would satisfy it; where a "
             "point names more than one acceptable outcome, pick the one that is true of the "
             "material.\n"
           + "\n".join(lines))


def _brief_item(finding: Finding) -> str:
    """One veto as a list item, continuations indented so a multi-line brief stays one point."""
    first, *rest = (finding.brief or finding.message).splitlines() or [""]
    return "\n".join([f"- [{finding.gate}] {first}",
                      *(f"  {line}" if line else "" for line in rest)])
