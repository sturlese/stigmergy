"""The `kind` strings the review inbox uses — `entity-proposal`, `parked-capture`,
`repair-proposal` — and nothing else. At the package ROOT, beside `stigmergy.text`, so
`stigmergy.server.review` and the pure Block Kit consumers (`stigmergy.slack.render`/`doorbell`)
agree on these exact strings without the renderer importing the server's whole import graph for a
few literals. Nothing here imports anything from this project; if it ever does, it has stopped
being the bottom of the stack.

A kind in this tuple is a kind `review_decide` accepts and the ledger records. It is NOT a promise
that every surface renders it: `repair-proposal` has no Block Kit card, and `slack.doorbell` skips
any kind it has no renderer for rather than guessing one (its `_EVENT_NAMES` is the list of kinds
that ring). A kind is reviewed in the console and over MCP until somebody writes it a card.
"""

KIND_ENTITY_PROPOSAL = "entity-proposal"
KIND_PARKED_CAPTURE = "parked-capture"
# An identity the librarian created with `approved_by` empty, waiting on a steward's Approve,
# Merge or Decline; `item_id` is the entity's registry id. `alias-proposal` is one spelling the
# librarian appended to a registered entity's `proposed_aliases:`; `item_id` is `<id>:<alias>`.
# The librarian reads the ledger through these kinds to refuse re-proposing what was declined.
KIND_IDENTITY_PROPOSAL = "identity-proposal"
KIND_ALIAS_PROPOSAL = "alias-proposal"
# The gardener's findings, turned into a concrete additive edit by `stigmergy.repair` and waiting
# on a steward. Unlike the other two it has no submitter — nobody asked for it — which is why it
# appears in the MANAGEMENT read of the inbox only (`server.review._collect_open_items`).
KIND_REPAIR_PROPOSAL = "repair-proposal"
ITEM_KINDS = (KIND_ENTITY_PROPOSAL, KIND_PARKED_CAPTURE, KIND_REPAIR_PROPOSAL)

# The entity types a mint may declare, for the entity-mint modal's `static_select`. The one true
# source is `entities.generator.ENTITY_TYPES`; this is a RESTATEMENT (this module may depend on
# nothing), pinned against drift by `tests/test_architecture.py::
# test_review_kinds_entity_types_matches_the_generators_closed_list`. `stigmergy.server.review`
# does not read this constant — it imports the generator's directly.
ENTITY_TYPES = ("person", "organization", "product", "tool", "repository", "place", "project")
