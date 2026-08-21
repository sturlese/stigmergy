"""The `kind` strings the review inbox uses — `identity-proposal`, `alias-proposal`,
`repair-proposal` — and nothing else. At the package ROOT, beside `stigmergy.text`, so
`stigmergy.server.review`, the librarian's ledger read and the pure Block Kit consumers
(`stigmergy.slack.render`/`doorbell`) agree on these exact strings without the renderer importing
the server's whole import graph for a few literals. Nothing here imports anything from this
project; if it ever does, it has stopped being the bottom of the stack.

A kind in `ITEM_KINDS` is a kind `review_decide` accepts and the ledger records. It is NOT a
promise that every surface renders it: `repair-proposal` has no Block Kit card, and
`slack.doorbell` skips any kind it has no renderer for rather than guessing one. A kind is
reviewed in the console and over MCP until somebody writes it a card.
"""

# An identity the librarian created with `approved_by` empty, waiting on a steward's Approve,
# Merge or Decline; `item_id` is the entity's registry id. The librarian reads the ledger through
# this kind to refuse re-proposing what was declined, so EVERY door that declines an identity
# records its row under exactly this kind and that id.
KIND_IDENTITY_PROPOSAL = "identity-proposal"
# One spelling the librarian appended to a registered entity's `proposed_aliases:`, waiting on
# Approve or Decline; `item_id` is `<entity id>:<alias>`, split on the FIRST colon (an id is a
# slug and carries none).
KIND_ALIAS_PROPOSAL = "alias-proposal"
# The gardener's findings, turned into a concrete additive edit by `stigmergy.repair` and waiting
# on a steward. It has no submitter — nobody asked for it — which is why it appears in the
# MANAGEMENT read of the inbox only (`server.review._collect_open_items`).
KIND_REPAIR_PROPOSAL = "repair-proposal"
ITEM_KINDS = (KIND_IDENTITY_PROPOSAL, KIND_ALIAS_PROPOSAL, KIND_REPAIR_PROPOSAL)

# The kinds the ledger carries from before captures stopped parking: a mint approved from a parked
# capture (`item_id` = the capture's queue id) and a steward's disposition of a parked capture.
# Nothing writes them any more; the decisions FEED still labels the rows that exist.
LEGACY_KIND_ENTITY_PROPOSAL = "entity-proposal"
LEGACY_KIND_PARKED_CAPTURE = "parked-capture"
LEGACY_KINDS = (LEGACY_KIND_ENTITY_PROPOSAL, LEGACY_KIND_PARKED_CAPTURE)

# The entity types a mint may declare, for the entity-mint modal's `static_select`. The one true
# source is `entities.generator.ENTITY_TYPES`; this is a RESTATEMENT (this module may depend on
# nothing), pinned against drift by `tests/test_architecture.py::
# test_review_kinds_entity_types_matches_the_generators_closed_list`. `stigmergy.server.review`
# does not read this constant — it imports the generator's directly.
ENTITY_TYPES = ("person", "organization", "product", "tool", "repository", "place", "project")


def alias_item_id(entity_id: str, alias: str) -> str:
    """The one spelling of an alias proposal's `item_id`."""
    return f"{entity_id}:{alias}"


def split_alias_item_id(item_id: str) -> tuple[str, str]:
    """`(entity_id, alias)` — the inverse, splitting on the FIRST colon. `("", "")` when the id
    carries none, so a caller refuses rather than guesses."""
    head, sep, tail = str(item_id or "").partition(":")
    if not sep or not head.strip() or not tail.strip():
        return "", ""
    return head.strip(), tail.strip()
