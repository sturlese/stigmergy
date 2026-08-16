"""The `kind` strings the review inbox uses — `entity-proposal`, `parked-capture` — and nothing
else. At the package ROOT, beside `stigmergy.text`, so `stigmergy.server.review` and the pure
Block Kit consumers (`stigmergy.slack.render`/`doorbell`) agree on these exact strings without the
renderer importing the server's whole import graph for a few literals. Nothing here imports
anything from this project; if it ever does, it has stopped being the bottom of the stack.
"""

KIND_ENTITY_PROPOSAL = "entity-proposal"
KIND_PARKED_CAPTURE = "parked-capture"
ITEM_KINDS = (KIND_ENTITY_PROPOSAL, KIND_PARKED_CAPTURE)

# The entity types a mint may declare, for the entity-mint modal's `static_select`. The one true
# source is `entities.generator.ENTITY_TYPES`; this is a RESTATEMENT (this module may depend on
# nothing), pinned against drift by `tests/test_architecture.py::
# test_review_kinds_entity_types_matches_the_generators_closed_list`. `stigmergy.server.review`
# does not read this constant — it imports the generator's directly.
ENTITY_TYPES = ("person", "organization", "product", "tool", "repository", "place", "project")
