"""The `entity-body` op: the one repair that REPLACES text, and the narrow shape that makes it
judgeable.

Every other op in this loop is additive, and that is what let the nine gates judge it unchanged.
This one is not, so it buys its safety somewhere else — by being **structurally unable to touch
anything but one page's prose**:

  · the page is in the entity zone and declares `type: entity`, both checked;
  · everything down to and including the page's own `# Title` survives BYTE FOR BYTE — the
    frontmatter block, the template's comment, the H1 — so a body draft is not
    also approving a change to `entity:`, `acl:` or `status:`;
  · exactly two frontmatter lines may differ, and only in place: `updated:` (the apply date) and
    `role:` (only when the page declares an EMPTY one — a role somebody wrote is a statement of
    identity, and replacing it is not a body draft);
  · the draft carries no frontmatter fence and no H1 of its own, so it cannot re-open either.

`gates.gate_body_rewrite` proves the same properties AGAIN, against the produced diff, for a path
the apply named in `GateContext.body_rewrite_allowed`. That redundancy is the design: this module
is what a caller INTENDED, the gate is what the diff actually says.

The validator runs at both ends, exactly as `librarian.edits.validate` does and for the same
reason: propose time proves a draft is storable against the checkout the model read, apply time
proves it still applies to a clone that may be hours newer. Neither trusts the other.
"""
import datetime
import os
import re

import yaml

from stigmergy.librarian import edits, gates
from stigmergy.librarian import page as page_policy
from stigmergy.repair import deletion, schema
from stigmergy.repair.errors import RepairError

# The op name IS the proposal kind: one vocabulary word for one shape (`schema.py`).
OP_KIND = schema.KIND_ENTITY_BODY

# The entity zone, spelled here rather than imported. `stigmergy.entities.generator` owns the same
# string (`ENTITIES_RELDIR`), and importing that package would give the repair loop a path into the
# governed birth door for one constant — the duplication is DECLARED, not discovered, the same
# posture `remote.GITLEAKS_BIN_ENV` takes one module over. Entity birth stays identity-only;
# this reaches the same folder to write CONTENT and nothing else.
ENTITY_ZONE_PREFIX = "wiki/entities/"

# What `deletion.page_refusal` — the ONE confinement predicate this package has — says on this
# kind's behalf. The checks are shared because they are a security predicate; the sentences are not,
# because they are read afterwards and have to name what a BODY DRAFT did to the page.
# `require_readable` is left off deliberately: the read below is a separate finding
# (`unreadable-target`), since a page that exists but cannot be decoded is a different problem from
# one that is not there.
_NOT_A_PAGE_WHY = "declared a body draft for {path}, which is not a page"
_OUTSIDE_WORKTREE_WHY = "declared a body draft for {path}, which resolves outside the worktree"
_SYMLINK_WHY = "declared a body draft for {path}, which is a symlink and not a page"
_MISSING_WHY = "declared a body draft for {path}, which does not exist in the repo"

# What one draft may be. Constants rather than env-tunable settings, deliberately: the real ceiling
# is the KNOWLEDGE repo's contract linter, which vetoes any page whose body exceeds 150 lines
# (`gate_contract` surfaces it as a veto), so an operator raising these could only produce
# proposals the gates then refuse. The line bound leaves the head — the template's comment block,
# the H1, the blank line under it — inside the linter's ceiling without this module having to
# reproduce another repo's counting rule.
MAX_BODY_BYTES = 6_000
MAX_BODY_LINES = 110
# A role is ONE sentence of identity on a page a person reads, and it becomes a frontmatter line.
MAX_ROLE_CHARS = 200

# The page's own title line. `#` followed by whitespace and nothing else — `## Facts` is a section
# a draft may legitimately open with, and forbidding it would forbid every real body.
_H1_RE = re.compile(r"^#\s")

# `today` becomes a frontmatter LINE, so anything but a plain date is a line-oriented write into
# somebody's page (`entities.birth._clean_today`'s reasoning, one package over).
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# The wikilink shape, hand-mirrored from the knowledge repo's contract linter rather than imported
# from `index.corpus` — this package talks to the linter through FILES, and the dead-link question
# below has to be asked exactly as the linter will ask it or a draft passes here and vetoes there.
_WIKILINK_RE = re.compile(r"!?\[\[([^\[\]]+?)\]\]")

_FRONTMATTER_FENCE = "---"


def _finding(code: str, message: str, locator: str = "") -> gates.Finding:
    return gates.Finding("entity-body", code, message, locator=locator)


class _Parsed:
    """One entity page's structure: where its frontmatter ends and where its own H1 sits.

    Whole-file LINE indices, never a re-derived split, because the writer's promise is about
    BYTES: it rebuilds the file from `lines`, so anything it did not deliberately change is
    literally the same string it read.
    """

    __slots__ = ("lines", "front_end", "h1")

    def __init__(self, lines: list[str], front_end: int, h1: int):
        self.lines, self.front_end, self.h1 = lines, front_end, h1

    @property
    def front(self) -> list[str]:
        return self.lines[1:self.front_end]


def parse(text: str) -> tuple[_Parsed | None, str]:
    """`(parsed, reason_code)` — `("", …)` never: a page this cannot read is REFUSED by name.

    Deliberately strict about the opening fence (`---` alone on the first line): a BOM or a CRLF
    file is a page shape this writer does not know, and guessing at one is how a rewrite lands
    somewhere nobody predicted.
    """
    lines = (text or "").split("\n")
    if not lines or lines[0] != _FRONTMATTER_FENCE:
        return None, "no-frontmatter"
    front_end = next((i for i in range(1, len(lines)) if lines[i] == _FRONTMATTER_FENCE), -1)
    if front_end < 0:
        return None, "no-frontmatter"
    h1 = next((i for i in range(front_end + 1, len(lines)) if _H1_RE.match(lines[i])), -1)
    if h1 < 0:
        return None, "no-h1"
    return _Parsed(lines, front_end, h1), ""


# What each `parse` refusal says. A dict rather than a conditional at the raise site: both reasons
# are page SHAPES this writer does not know, and each one's sentence has to say what the shape is
# and why guessing at it is worse than refusing.
_UNPARSEABLE_PAGE = {
    "no-frontmatter": (
        "{path} opens with no `---` frontmatter block this can read, so there is no boundary "
        "between what the page declares and what it says — refusing rather than guessing where "
        "the body starts"),
    "no-h1": (
        "{path} has no `# Title` line, so there is no point to cut the body at — and inventing a "
        "title is the identity decision the birth fold reserves for the capture that made it"),
}


def _declared_type(front_lines: list[str]) -> tuple[str, bool]:
    """`(type, parsed_ok)` — the page's declared type through a real YAML parser, since `type` is
    a value question and the line editors answer shape questions."""
    try:
        parsed = yaml.safe_load("\n".join(front_lines)) if front_lines else {}
    except yaml.YAMLError:
        return "", False
    if not isinstance(parsed, dict):
        return "", False
    return str(parsed.get("type") or "").strip().lower(), True


def _is_empty_scalar(raw: str) -> bool:
    """Is this frontmatter value an EMPTY string? An unparseable value answers False — "I could
    not tell what is there" must never read as "there is nothing there"."""
    try:
        parsed = yaml.safe_load(raw)
    except yaml.YAMLError:
        return False
    return parsed is None or (isinstance(parsed, str) and not parsed.strip())


def validate(worktree: str, ops, *, link_names: set[str] | None = None) -> list[gates.Finding]:
    """Every reason this draft could not be applied to `worktree`, or `[]`.

    `link_names` is the resolvable page-name set; scanned lazily from the worktree when omitted.
    The dead-link question is asked HERE rather than left to the gates because the knowledge
    repo's own contract linter treats an unresolvable `[[wikilink]]` as an ERROR, and
    `gate_contract` turns that into a veto — so a draft citing a page that does not exist would be
    a repair that reads as valid and that code can never apply.
    """
    ops = list(ops or ())
    if len(ops) != 1:
        return [_finding("one-op",
                         f"an {OP_KIND} proposal carries exactly one draft for one page, not "
                         f"{len(ops)}: a body draft is one page's prose, and one page's only")]
    op = ops[0]
    path = str(op.get("path", ""))
    body = str(op.get("body_markdown", ""))
    role = str(op.get("role", ""))
    out: list[gates.Finding] = []

    if str(op.get(schema.OP_KIND_KEY, "")) != OP_KIND:
        return [_finding("unknown-kind",
                         f"declared a {op.get(schema.OP_KIND_KEY)!r} op in an {OP_KIND} proposal",
                         path)]
    if not path.startswith(ENTITY_ZONE_PREFIX):
        return [_finding("outside-lane",
                         f"declared a body draft for {path}, which is not an entity page — this "
                         f"kind rewrites nothing outside {ENTITY_ZONE_PREFIX}", path)]
    code, sentence = deletion.page_refusal(
        worktree, path, not_a_page_why=_NOT_A_PAGE_WHY,
        outside_worktree_why=_OUTSIDE_WORKTREE_WHY, symlink_why=_SYMLINK_WHY,
        missing_why=_MISSING_WHY)
    if code:
        return [_finding(code, sentence, path)]
    full = os.path.join(worktree, path)
    try:
        with open(full, encoding="utf-8") as f:
            text = f.read()
    except (OSError, UnicodeDecodeError):
        return [_finding("unreadable-target",
                         f"{path} could not be read as text, so what a draft would replace cannot "
                         f"be established", path)]

    parsed, reason = parse(text)
    if parsed is None:
        return [_finding(reason, _UNPARSEABLE_PAGE[reason].format(path=path), path)]
    page_type, parsed_ok = _declared_type(parsed.front)
    if not parsed_ok:
        out.append(_finding("unparseable-frontmatter",
                            f"{path}: its frontmatter is not valid YAML, so what the page declares "
                            f"cannot be established", path))
    elif page_type != page_policy.ENTITY_PAGE_TYPE:
        out.append(_finding("not-an-entity-page",
                            f"{path} declares type {page_type!r}: the entity zone is a folder and "
                            f"the type is a declaration, and this kind needs both", path))
    if page_policy.top_level_key_line(parsed.front, "updated")[0] < 0:
        out.append(_finding("no-updated-line",
                            f"{path} declares no `updated:` line — this kind rewrites that line in "
                            f"place and never appends one, which would move frontmatter the "
                            f"reader never saw", path))

    out += _role_findings(parsed, role, path=path)
    out += _body_findings(worktree, body, path=path, link_names=link_names)
    return out


def _role_findings(parsed: _Parsed, role: str, *, path: str) -> list[gates.Finding]:
    if not role:
        return []                      # the ordinary proposal: a body and nothing else
    if len(role) > MAX_ROLE_CHARS:
        return [_finding("role-too-long",
                         f"the role drafted for {path} is {len(role)} characters (max "
                         f"{MAX_ROLE_CHARS}): a role is one sentence of identity", path)]
    if role != " ".join(role.split()) or "\n" in role or "\r" in role:
        return [_finding("role-not-one-line",
                         f"the role drafted for {path} is not a single line of text — it becomes "
                         f"ONE frontmatter line, and a break in it forges the next field", path)]
    index, raw = page_policy.top_level_key_line(parsed.front, "role")
    if index < 0:
        return [_finding("no-role-line",
                         f"{path} declares no `role:` line, and this kind rewrites lines in place "
                         f"rather than adding fields", path)]
    if not _is_empty_scalar(raw):
        return [_finding("role-already-set",
                         f"{path} already declares a role: a role somebody wrote is a statement of "
                         f"identity, and replacing it is not a body draft", path)]
    return []


def _body_findings(worktree: str, body: str, *, path: str,
                   link_names: set[str] | None) -> list[gates.Finding]:
    out = []
    if not body.strip():
        return [_finding("empty-body",
                         f"the draft for {path} is empty: a page with no body is the placeholder "
                         f"this repair exists to answer", path)]
    size = len(body.encode("utf-8"))
    if size > MAX_BODY_BYTES:
        out.append(_finding("body-too-long",
                            f"the draft for {path} is {size} bytes (max {MAX_BODY_BYTES})", path))
    lines = body.strip("\n").split("\n")
    if len(lines) > MAX_BODY_LINES:
        out.append(_finding("body-too-many-lines",
                            f"the draft for {path} is {len(lines)} lines (max {MAX_BODY_LINES}) — "
                            f"the knowledge repo's contract linter refuses a page body over its "
                            f"own ceiling, so a longer draft could never be applied", path))
    if any(line.strip() == _FRONTMATTER_FENCE for line in lines):
        out.append(_finding("body-frontmatter-fence",
                            f"the draft for {path} contains a `---` line: a body that can re-open "
                            f"a frontmatter block is a body that can declare fields", path))
    if any(_H1_RE.match(line) for line in lines):
        out.append(_finding("body-h1",
                            f"the draft for {path} contains an H1 line: the page's own `# Title` "
                            f"survives this rewrite, and a second one is a second title", path))
    dead = _dead_links(worktree, lines, link_names)
    if dead:
        out.append(_finding("dead-link",
                            f"the draft for {path} links {', '.join(f'[[{d}]]' for d in dead)}, "
                            f"which resolve to no page in the graph", path))
    return out


def _dead_links(worktree: str, lines, link_names: set[str] | None) -> list[str]:
    """Wikilink targets in the draft that resolve to nothing — the contract linter's own question,
    asked its own way: by bare basename, alias and anchor stripped."""
    targets = []
    for line in lines:
        for match in _WIKILINK_RE.finditer(line):
            target = match.group(1).split("|", 1)[0].split("#", 1)[0].strip()
            stem = target.rsplit("/", 1)[-1].removesuffix(".md")
            if stem:
                targets.append(stem)
    if not targets:
        return []
    resolvable = edits.page_names(worktree) if link_names is None else link_names
    return sorted({t for t in targets if t not in resolvable})


def rewritten(text: str, *, body_markdown: str, role: str, updated: str) -> str:
    """One page's new bytes: everything through its own H1, then the draft. Pure — no filesystem,
    so the property this kind rests on can be asserted about a string.

    Call only after `validate` returned nothing; every shape it asks about is assumed here.
    """
    parsed, _reason = parse(text)
    lines = list(parsed.lines)
    front_offset = 1                       # `front` is `lines[1:front_end]`
    index, _raw = page_policy.top_level_key_line(parsed.front, "updated")
    lines[front_offset + index] = f"updated: {updated}"
    if role:
        role_index, _raw = page_policy.top_level_key_line(parsed.front, "role")
        lines[front_offset + role_index] = f"role: {page_policy.yaml_scalar(role)}"
    head = "\n".join(lines[:parsed.h1 + 1])
    return f"{head}\n\n{body_markdown.strip(chr(10))}\n"


def apply_declared(worktree: str, ops, *, today: str = "") -> tuple[list[str], list[gates.Finding]]:
    """Validate then write, all-or-nothing — `edits.apply_declared`'s posture, and here the reason
    is sharper: a half-applied rewrite is a page with its body gone.

    `today` is the injectable clock (`processing.Deps.today`'s own convention); the apply date is
    what lands in `updated:`.
    """
    findings = validate(worktree, ops)
    if findings:
        return [], findings
    op = list(ops)[0]
    path = str(op["path"])
    full = os.path.join(worktree, path)
    with open(full, encoding="utf-8") as f:
        before = f.read()
    after = rewritten(before, body_markdown=str(op.get("body_markdown", "")),
                      role=str(op.get("role", "")), updated=_apply_date(today))
    if after == before:
        return [], []
    with page_policy.open_for_rewrite(full) as f:
        f.write(after)
    return [path], []


def _apply_date(today: str) -> str:
    value = str(today or "").strip() or datetime.date.today().isoformat()
    if not _DATE_RE.match(value):
        raise RepairError(
            f"{value!r} is not a date this can write into a page's `updated:` field — that value "
            f"becomes one frontmatter line, and anything else is a line-oriented write into the "
            f"page")
    return value
