"""The audience vocabulary: what a label list MEANS when two of them meet.

One question lives here, and it is not "who is asking" — that one is
`stigmergy.server.acl.visible()`, the one enforcement point, and it stays there.

  * `flows_into(content_acl, page_acl)` — may this CONTENT be written into, or rendered onto, a
    page carrying that label? Asked wherever text crosses from one governed page to another.

`view_acl` lived here too — a view's own label, the INTERSECTION of its members'. It never
widened access, correctly, and it COLLAPSED: one leadership-only note anchored to a popular entity
made that entity's view vanish for everyone else. It was replaced with an open view whose
members are filtered with `flows_into`, so there is no second label to compute.

Where a label comes from is not this module's business either, and used to be: `resolve_acl` and
its four matchers mapped a path to a label list from an ordered config file, of which two matchers
(`unit`, `entity_kind`) had no input anywhere in the system and one (`path_contains`) no user.
The whole of it was replaced by a decision made at the door and carried on the capture's own
queue row, so there is no resolution
step left to own.

**One dialect, everywhere**: `None` is open, `[]` is nobody. The librarian's old
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

    Its callers are every seam where a model reads one governed page while writing another
    (the filing port's corpus context, the removal sweep), plus the write-lane check in
    `gate_zone`. Its default posture is fail-closed: called with `page_acl=None` — an open page,
    the widest audience there is — it admits open content only.

    This was `visible_to_view`, and shipped for one feed of a page type that no longer exists. The
    question it answers was never that feed's: it is asked of every model input, because
    the page linking upward to something its readers cannot see was written by an agent that had
    searched the corpus unrestricted.
    """
    if content_acl is None:
        return True
    if page_acl is None:
        return False
    return set(page_acl) <= set(content_acl)

