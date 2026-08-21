"""`entities.errors` — the domain error hierarchy `entities.cli.main` maps to one clean stderr
line (module docstring). Thin, but not zero: the hierarchy IS the contract every other test in
this package relies on (`pytest.raises(CollisionError)`, `pytest.raises(CloneStateError)` ...),
and a rename or a dropped base class would silently widen or narrow what every one of those
`except` clauses actually catches.
"""
from stigmergy.entities.errors import (
    CapabilityUnavailableError,
    CloneStateError,
    CollisionError,
    CollisionRaceError,
    EntityError,
    PushRaceError,
    TemplateMissingError,
)

# The four arms of `entities.remote.decide_via_clone`'s post-clone ladder (issue #57).
MAPPED_AT_THE_SERVER_DOOR = (CloneStateError, TemplateMissingError, CollisionRaceError,
                             PushRaceError)


def test_every_domain_error_is_an_entity_error():
    for cls in (CollisionError, CollisionRaceError, CloneStateError, PushRaceError,
                CapabilityUnavailableError, TemplateMissingError):
        assert issubclass(cls, EntityError)


def test_no_mapped_refusal_type_is_a_subclass_of_another():
    """`entities.remote`'s post-clone ladder is an ORDERED `except` chain over these four, and an
    ordered chain is order-INDEPENDENT only while they really are siblings. Make one a subclass of
    another and the arms silently start shadowing each other — every existing test stays green,
    because each type is still caught by SOME arm, and a steward starts being told to commit a
    template when the mint lost a push race."""
    for cls in MAPPED_AT_THE_SERVER_DOOR:
        for other in MAPPED_AT_THE_SERVER_DOOR:
            assert cls is other or not issubclass(cls, other), (
                f"{cls.__name__} became a subclass of {other.__name__} — the refusal ladder in "
                f"`entities.remote.decide_via_clone` now depends on which arm is written first")


def test_the_collision_race_is_a_collision_and_the_plain_verdict_is_not_a_race():
    """The one subclass relationship this hierarchy DOES want, in both directions.

    `CollisionRaceError` must stay a `CollisionError` — every `except CollisionError` and every
    `pytest.raises(CollisionError)` in the CLI half still has to catch the post-rebase re-ask, and
    a steward at a terminal reads the same verdict either way. And the plain verdict must NOT
    become a race: `entities.remote` maps the race and passes the verdict through, so a widening
    here would turn "this identity already exists" into "approve again" at the server door."""
    assert issubclass(CollisionRaceError, CollisionError)
    assert not issubclass(CollisionError, CollisionRaceError)


def test_entity_error_itself_is_a_runtime_error_never_system_exit():
    """`cli.main` catches `(EntityError, CaptureError, LibrarianError)` and prints one line — it
    must never need to catch `SystemExit`, which `pytest.raises`/`except` clauses upstream (and a
    bare `except Exception`) would not otherwise stop from propagating past a library boundary."""
    assert issubclass(EntityError, RuntimeError)
    assert not issubclass(EntityError, SystemExit)


def test_each_subclass_carries_the_message_it_is_raised_with():
    for cls in (EntityError, CollisionError, CollisionRaceError, CloneStateError, PushRaceError,
               CapabilityUnavailableError, TemplateMissingError):
        ex = cls("a specific, operator-facing sentence")
        assert str(ex) == "a specific, operator-facing sentence"
