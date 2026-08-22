"""The one thing a person still writes to the knowledge repo from the serving process: a page
removal, decided and applied in the same call (ADR 043 D2).

Everything else that used to live here is gone with the waiting (ADR 044): an identity a capture
introduces is born confirmed by whoever captured it, and a repair is derived and applied by the
worker without anybody being asked. What is left is the deletion — a judgment only a person can
make, made at the command they gave it — plus `commission_registration`, which queues a capture
and touches no git at all.

The deletion goes through the SAME `repair.apply` door the worker's repairs go through, and lands
in the same ledger under the same three outcomes. What differs is one field: `actor` names the
person, which puts their name in the commit's `Approved-by:` trailer where a worker-derived repair
carries a `Repair:` line instead.

`delete_and_record` takes NO authorization argument: authorization is per-surface, so each door
decides who may before it calls in — the MCP tool by requiring an unrestricted identity, the
console by sitting behind its operator token.
"""
import logging

from stigmergy.capture import queue
from stigmergy.capture import schema as capture_schema
from stigmergy.capture.errors import CaptureError
from stigmergy.entities.generator import ENTITY_TYPES, canonical_id_for
from stigmergy.repair import schema as repair_schema
from stigmergy.text import one_line

log = logging.getLogger(__name__)

# The repair ledger's DDL, re-exported for the startup pass in `service.build_service`: this module
# is where the table is written, so this is where a caller asks for it.
ensure_repair_schema = repair_schema.ensure_repair_schema


class ReviewError(CaptureError):
    """A refusal a caller may read — the vocabulary both sequences here raise in."""


# What a deletion may be, at every door — spelled in `capture.schema`, where the seam that
# enforces it lives, and re-exported here because the doors read them for their own copy.
MAX_DELETED_PAGES = capture_schema.MAX_DELETED_PAGES
DELETE_REASON_CHARS = capture_schema.DELETE_REASON_CHARS

DELETE_NEEDS_A_REASON = (
    "a removal needs a reason: what makes these pages stale. It is what `git log` carries "
    "afterwards and the only thing a later reader will have — nothing was queued")


def queue_deletion(conn, evidence, *, paths, why: str, actor: str, source: str) -> dict:
    """QUEUE a removal — the sequence both doors share, and the only thing either of them does.

    The worker performs it (ADR 044 D3): it holds the checkout and the credential, and this process
    holds neither. What lands here is a durable `delete` row with the person's name on it, and what
    they get back is a queue acknowledgement rather than a commit.

    It carries NO authorization: authorization is per-surface, and each door decides who may before
    it calls in — the MCP tool by requiring an unrestricted identity (a removal touches every page
    that refers to the ones named, so "may this caller see the whole corpus" is the only question
    answerable before a tree is read), the console by sitting behind its operator token. The CALLER
    SET is closed and pinned in `tests/test_architecture.py`.
    """
    if not str(actor or "").strip():
        raise ReviewError("a removal cannot be queued unattributed — nothing was queued")
    if evidence is None:
        raise ReviewError("the capture queue is not available on this server, so nothing can be "
                          "removed — it needs an evidence store configured")
    reason = one_line(capture_schema.clean_note(why), DELETE_REASON_CHARS)
    if not reason:
        raise ReviewError(DELETE_NEEDS_A_REASON)
    listed = "\n".join(str(p).strip() for p in (paths or ()) if str(p).strip())
    ack = queue.submit(conn, evidence, kind=capture_schema.DELETE, material=reason,
                       hints={"delete_paths": listed, "delete_source": source},
                       submitted_by=actor)
    return {**ack, "message": (
        f"queued #{ack['id']} — the librarian performs the removal: the pages go, every page that "
        f"referred to them is rewritten, the nine gates judge the result, and one commit lands "
        f"with an `Approved-by: {actor}` trailer. The per-page diffs are on the capture, which is "
        f"the reading nobody gives that prose before it lands.")}


def commission_registration(conn, evidence, *, name: str, entity_type: str, aliases: list[str],
                            about: str, actor: str, source: str) -> dict:
    """A person introducing an entity nobody has captured about yet — the console's Register door.

    There is no deterministic birth: what they know about the entity (`about`) is queued as a
    capture carrying the registration, and the librarian writes the entity's page from it and from
    what the brain already holds, anchors the note to it, and births the identity CONFIRMED by
    `actor` (ADR 042, ADR 044 D1). Nothing here touches git.

    It carries no authorization of its own: the console decides under its operator token before
    calling it, and `brain_submit` accepts the same `register_*` hints from any door — a
    registration pins what the librarian would otherwise infer, and pins nothing else.
    """
    clean_name = " ".join(str(name or "").split())
    clean_about = str(about or "").strip()
    if not clean_name or not clean_about:
        raise ReviewError(
            "registering an entity needs its name and what it is: the librarian writes the page "
            "from what you say and from what the brain already holds, and a page with nothing said "
            "about the entity is not written at all")
    if entity_type not in ENTITY_TYPES:
        raise ReviewError(f"entity_type {entity_type!r} is not one of {', '.join(ENTITY_TYPES)}")
    if evidence is None:
        raise ReviewError("the capture queue is not available on this server, so nothing can be "
                           "registered — it needs an evidence store configured")
    hints = capture_schema.registration_hints(name=clean_name, entity_type=entity_type,
                                              aliases=aliases, source=source)
    ack = queue.submit(conn, evidence, kind=capture_schema.RAW, material=clean_about, hints=hints,
                       submitted_by=actor)
    return {**ack, "entity_id": canonical_id_for(clean_name), "name": clean_name,
            "message": (f"commissioned as capture #{ack['id']}: the librarian writes the page of "
                        f"{clean_name} from what you said and what the brain already holds, anchors "
                        f"the note to it, and the entity is born confirmed by {actor}. It appears "
                        f"in Entities when the capture files.")}
