"""The one layer a capture files in and how it was decided (spec audience-layers §3.6)."""

from __future__ import annotations

import functools
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from stigmergy.capture import schema
from stigmergy.capture.provenance import without_capability
from stigmergy.kernel import layers, llm
from stigmergy.server import identity

log = logging.getLogger(__name__)

THRESHOLD = 0.75
STATE_TEXT_CHARS = 8000
# A choice among layers alone always names one; this option lets the classifier say none fits.
# The underscore keeps it out of the layer namespace.
NO_SINGLE_LAYER = "no_single_layer"
NO_SINGLE_LAYER_CRITERIA = (
    "None of these layers clearly fits the capture, or it fits more than one of them equally well."
)
INSTRUCTIONS = (
    "Which layer of the team wiki should this capture be filed in? Each option is a layer; its "
    "description says what belongs there."
)


@dataclass(frozen=True)
class Routed:
    acl: tuple[str, ...] | None
    decision: schema.LayerDecision


def route(
    principal: identity.Principal,
    requested,
    *,
    descriptions: Callable[[], Mapping[str, str]],
    state: Callable[[], dict],
    fixed_by_door: bool = False,
) -> Routed:
    """A named layer wins, and a door that names it (a Slack channel) fixes it. Otherwise the
    caller's default, unless the classifier gives one of the caller's own layers at least
    `THRESHOLD`. The classifier's options are only existing layers the caller could name, never
    `public`, plus `NO_SINGLE_LAYER`; any failure of the classifier is the default."""
    known = functools.cache(descriptions)
    if requested is not None:
        decision = schema.LayerDecision(decision=schema.CHANNEL if fixed_by_door else schema.EXPLICIT)
        routed = Routed(identity.resolve_capture_layer(principal, requested), decision)
    else:
        routed = _unnamed(principal, known, state)
    if routed.acl is not None and routed.acl[0] not in known():
        raise identity.LayerRefused("that layer does not exist")
    return routed


def _unnamed(principal: identity.Principal, known, state) -> Routed:
    default = identity.resolve_capture_layer(principal, None)
    unsure = Routed(default, schema.LayerDecision(decision=schema.DEFAULT))
    candidates = principal.publishable_layers - {layers.PUBLIC}
    if len(candidates) < 2:
        return unsure
    choices = {layer: known()[layer] or layer for layer in sorted(candidates) if layer in known()}
    if len(choices) < 2:
        return unsure
    snapshot = state()
    if not snapshot:
        return unsure
    options = {**choices, NO_SINGLE_LAYER: NO_SINGLE_LAYER_CRITERIA}
    try:
        answer = llm.choose(options, snapshot, instructions=INSTRUCTIONS)
        if set(answer) != set(options):
            raise llm.DecisionFailed("invalid_response")
        probabilities = {option: float(answer[option]) for option in options}
    except Exception as error:  # noqa: BLE001 — any classifier failure files in the default
        reason = error.reason if isinstance(error, llm.DecisionFailed) else error.__class__.__name__
        log.warning("capture layer classification failed", extra={"error_class": reason})
        return unsure
    best = max(choices, key=probabilities.__getitem__)
    if probabilities[best] >= THRESHOLD:
        return Routed(layers.acl_of_layer(best),
                      schema.LayerDecision(decision=schema.CLASSIFIED, probabilities=probabilities))
    return Routed(default, schema.LayerDecision(decision=schema.DEFAULT, probabilities=probabilities))


def capture_state(
    *,
    title: str | None = None,
    files=(),
    locator: str | None = None,
    text: str = "",
) -> dict:
    """What the classifier reads: the capture's own title, file names, address without capability
    parameters, and the leading text that is readable without a parser."""
    state = {}
    if title:
        state["title"] = title
    if files:
        state["files"] = list(files)
    if locator:
        try:
            state["locator"] = without_capability(locator)
        except ValueError:
            pass
    if text:
        state["text"] = text[:STATE_TEXT_CHARS]
    return state


def receipt_fields(row: dict) -> dict:
    """The layer a queued capture files in and how it was decided, as callers receive it."""
    fields = {"layer": layers.layer_of_acl(row["acl"]), "layer_decision": row["layer_decision"]}
    if row.get("layer_probabilities") is not None:
        fields["probabilities"] = row["layer_probabilities"]
    return fields
