"""views.render — assembling the skeleton sections and the synthesis into one page.

The page carries no `verification:` verdict because nothing computes one — `SYNTHESIS_CAPTION`
says so on the page itself. `entity:` is a LIST, the parity rule `index.corpus.entity_list`
depends on.
"""
import datetime
import hashlib

from stigmergy.kernel.page import _yaml
from stigmergy.views.skeleton import Member

# The withheld block claims a budget, never a verdict — no verdict is computed.
WITHHELD_BLOCK = """**Withheld — the automatic summary ran out of budget before it could be finished.**

An agent began drafting a summary of this entity from the pages above, but the run exceeded its
request/tool-call budget before a final draft was ready. Nothing below this line was written.

Nothing above this line depended on that summary succeeding: the timeline and backlinks above come
straight from this entity's own pages, computed the same way whether or not a summary ever ships,
and are current as of {generated_date}.

This is not an error and this page is not broken — the summary will be attempted again
automatically the next time one of this entity's pages changes."""

SYNTHESIS_CAPTION = ("*Written by an agent from the pages above. Like every page in this brain, "
                     "its figures are not machine-verified — the pages it was written from are "
                     "linked above, and they are the check.*")


def render_synthesis(synthesis_body: str, shipped: bool, generated_date: str) -> str:
    if not shipped:
        return WITHHELD_BLOCK.format(generated_date=generated_date)
    return f"{SYNTHESIS_CAPTION}\n\n{synthesis_body}"


def render(entity_id: str, entity_title: str, members: list[Member], *, member_hash: str,
          backlink_hash: str, timeline_md: str, backlinks_md: str, synthesis_body: str,
          shipped: bool, now: datetime.datetime | None = None) -> str:
    """The full view page: frontmatter + body. Returns the rendered text (not yet written to
    disk — `views.writer` owns that)."""
    now = now or datetime.datetime.now(datetime.UTC)
    generated_at = now.isoformat()
    generated_date = generated_at[:10]

    body = (
        f"# {entity_title} — view\n\n"
        f"*Regenerated {generated_date} from {len(members)} anchored page(s).*\n\n"
        f"## Timeline\n\n{timeline_md}\n\n"
        f"## Backlinks\n\n{backlinks_md}\n\n"
        f"## Synthesis\n\n{render_synthesis(synthesis_body, shipped, generated_date)}\n"
    )
    body_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()

    fm = ["---", "type: view", f"title: {_yaml(entity_title + ' — view')}",
         f"entity: [{_yaml(entity_id)}]", "tags: [view]", "tier: 3",
         f'content_hash: "sha256:{body_hash}"', f'generated_at: "{generated_at}"',
         f"members: {len(members)}",
         # The persisted staleness signals, on the derived page itself and git-versioned — never
         # in a side-channel state file that can drift from the page it describes. TWO fields,
         # one per feed the page renders: `member_hash` would start lying if the backlinks were
         # folded into it, and a comparison that reads a missing `backlink_hash:` as a match is
         # exactly the silence #85 was filed for — so `backlink_hash` is REQUIRED here, and a
         # view written without one reads as stale until it has been regenerated once.
         f'member_hash: "{member_hash}"', f'backlink_hash: "{backlink_hash}"']
    # NO `acl:` line, ever (ADR 045 D5). A view is the OPEN rollup: `skeleton.members_of` admits
    # only members that may be rendered onto an open page, and `backlinks_of` gates the
    # non-member feed the same way, so there is nothing on this page to restrict. The label it
    # used to carry was the intersection of its members' — which never widened access, correctly,
    # and collapsed the page to nobody the moment two members disagreed.
    fm.append("---")
    return "\n".join(fm) + "\n\n" + body
