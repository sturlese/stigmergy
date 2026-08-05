"""views.render — assembling the skeleton sections and the synthesis into one page.

The page carries no `verification:` verdict, because nothing computes one: a view's figures are
not machine-checked, and `SYNTHESIS_CAPTION` states that on the page itself rather than letting
an absent verdict read as a passing one. `entity:` is a LIST (`entity: [<id>]`), matching every
other page type's frontmatter and the parity rule `index.corpus.entity_list` depends on.
"""
import datetime
import hashlib

from stigmergy.kernel.acl import view_acl
from stigmergy.kernel.page import _yaml
from stigmergy.views.skeleton import Member

# The one road to a withheld synthesis (`synthesis.write_synthesis`'s `UsageLimitExceeded`
# catch): the agent's run exceeded its request/tool-call budget before a draft existed. State the
# fact, never the implication — this block claims a budget, not a verdict, because no verdict is
# computed.
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
          timeline_md: str, backlinks_md: str, synthesis_body: str, shipped: bool,
          now: datetime.datetime | None = None) -> str:
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
         # An honest "unchanged" no-op needs SOME persisted signal of the member set the last
         # regeneration produced, and for a derived page that signal belongs on the page itself,
         # git-versioned — never in a side-channel state file that can drift out of step with the
         # page it describes. Machine-only; the contract linter has no unknown-key rejection, so
         # this field needs no linter change.
         f'member_hash: "{member_hash}"']
    acl = view_acl([m.acl for m in members])
    if acl is not None:
        # The INTERSECTION of the members' audiences — a rollup never widens access.
        # `view_acl` returns `None` for an open corpus (nothing to add) and a
        # (possibly empty) sorted list otherwise; an empty list IS a legal, meaningful value
        # ("restrictive by construction, never silently open" — view_acl's own docstring), so
        # it is still rendered as `acl: []`, never omitted.
        fm.append("acl: [" + ", ".join(_yaml(a) for a in acl) + "]")
    fm.append("---")
    return "\n".join(fm) + "\n\n" + body
