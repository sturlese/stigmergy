"""The audience vocabulary: what a label list MEANS when two of them meet.

Two questions live here, and neither of them is "who is asking" — that one is
`stigmergy.server.acl.visible()`, the one enforcement point, and it stays there.

  * `flows_into(content_acl, page_acl)` — may this CONTENT be written into, or rendered onto, a
    page carrying that label? Asked wherever text crosses from one governed page to another.
  * `view_acl(member_acls)` — a view's own label, the INTERSECTION of its members'. On its way
    out with ADR 045 D5: a view carries no label at all once its members are filtered to open.

Where a label comes from is not this module's business either, and used to be: `resolve_acl` and
its four matchers mapped a path to a label list from an ordered config file, of which two matchers
(`unit`, `entity_kind`) had no input anywhere in the system and one (`path_contains`) no user.
[ADR 045](../../../docs/decisions/045-audience-from-the-door.md) D2 replaced the whole of it with
a decision made at the door and carried on the capture's own queue row, so there is no resolution
step left to own.

**One dialect, everywhere** (ADR 045 D9): `None` is open, `[]` is nobody. The librarian's old
adapter translated a resolved `[]` back to `None` — "empty means open" — which made the one
spelling `ops/acl.json` used to restrict mean its opposite once stamped. Nothing here collapses
one into the other, and `page.stamp_server_fields` writes `acl: []` rather than omitting the line.
"""


def flows_into(content_acl: list[str] | None, page_acl: list[str] | None) -> bool:
    """May content labelled `content_acl` be written into a page labelled `page_acl`?

    Named for the direction it guards, because the two arguments are NOT interchangeable and a
    reversed call reads perfectly. Truth table:

      * open content (`None`) flows anywhere — it is already readable by everyone;
      * nothing labelled flows into an OPEN page: every reader of that page would inherit material
        its author restricted them from;
      * otherwise every group of the PAGE must be a group of the CONTENT (`set(page) <=
        set(content)`) — everyone who can read the page can already read the source.

    It is deliberately not `visible()`, and the difference is the whole point: `visible()` asks
    whether ONE PERSON may read something, and a person needs to share only one label. This asks
    whether an AUDIENCE may, so it needs containment — sharing one label with a page's audience
    would leak to the rest of it.

    The four callers are every seam where a model reads one governed page while writing another
    (the filing port, the meeting distiller's corpus context, the repair proposer, the view
    synthesis), plus the write-lane check in `gate_zone` and the view's own member and backlink
    feeds. Its default posture is fail-closed: called with `page_acl=None` — an open page, the
    widest audience there is — it admits open content only.

    This was `visible_to_view`, and shipped for the view backlink feed alone (ADR 021 D4). The
    question it answers was never view-shaped: ADR 045 D3 asks it of every model input, because
    the page linking upward to something its readers cannot see was written by an agent that had
    searched the corpus unrestricted.
    """
    if content_acl is None:
        return True
    if page_acl is None:
        return False
    return set(page_acl) <= set(content_acl)


def view_acl(member_acls: list) -> list[str] | None:
    """A view's audience: the INTERSECTION of its members' — a rollup must never widen access to
    what it summarizes. Members without a label don't restrict; all-`None` -> `None` (open). An
    empty intersection is restrictive by construction, never silently open.

    **Retired by [ADR 045](../../../docs/decisions/045-audience-from-the-door.md) D5**, in the
    phase after this one: the intersection never widens, correctly, but it COLLAPSES — one
    leadership-only note anchored to a popular entity makes that entity's view vanish for
    everyone else. It is replaced by an open view whose members are filtered with `flows_into`.
    """
    sets = [set(a) for a in member_acls if a is not None]
    if not sets:
        return None
    out = set.intersection(*sets)
    return sorted(out)
