"""The `kind` string constants the review inbox uses — `entity-proposal`, `parked-capture` —
and nothing else.

They live at the ROOT of the package, beside `stigmergy.text`, for the identical reason: both
`stigmergy.server.review` (the inbox's implementation) and `stigmergy.slack.render`/
`stigmergy.slack.doorbell` (the Block Kit renderer and the doorbell that consume it) need to agree
on these exact strings, and a module below BOTH of them is what lets each import it without either
reaching sideways into the other's package.

**Why it exists rather than living in the server.** `stigmergy.slack.render` is meant to be a pure
Block Kit function, fully testable with none of the server's dependencies mattering. Importing
these constants from the server directly dragged the render module's OWN import graph through
`stigmergy.librarian.*`, `stigmergy.entities.*`, `stigmergy.index.*`, `subprocess` and PyYAML — the
whole world, for a few string literals. Restating the literals on the server side instead would
need a drift-guard test to stay honest; ONE definition needs no guard.

Nothing here imports anything from this project. That is the property that makes it safe to depend
on from everywhere; if this module ever needs a stigmergy import, it has stopped being the bottom of
the stack.
"""

KIND_ENTITY_PROPOSAL = "entity-proposal"
KIND_PARKED_CAPTURE = "parked-capture"
ITEM_KINDS = (KIND_ENTITY_PROPOSAL, KIND_PARKED_CAPTURE)

# The six entity types a mint may declare (ADR 030 D5) — `stigmergy.slack.render` needs this closed
# list to build the entity-mint modal's `static_select` options without importing
# `stigmergy.entities` at all (the same "a pure Block Kit renderer must agree with the server
# without importing it" argument this module's own docstring makes for the two `KIND_*` strings
# above). The one true source is `entities.generator.ENTITY_TYPES`; this is a RESTATEMENT, not an
# import (this module may depend on nothing — see the module docstring), pinned against drifting
# from it by `tests/test_architecture.py::
# test_review_kinds_entity_types_matches_the_generators_closed_list`. Unlike `KIND_*` above,
# `stigmergy.server.review` does not read this constant — it still imports `ENTITY_TYPES` directly
# from `entities.generator`, so there is no risk of the SERVER's own copy drifting; only this
# restatement can, which is exactly what the drift test holds to.
ENTITY_TYPES = ("person", "organization", "product", "tool", "repository", "place")
