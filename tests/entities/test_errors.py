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
    EntityError,
    PushRaceError,
)


def test_every_domain_error_is_an_entity_error():
    for cls in (CollisionError, CloneStateError, PushRaceError, CapabilityUnavailableError):
        assert issubclass(cls, EntityError)


def test_entity_error_itself_is_a_runtime_error_never_system_exit():
    """`cli.main` catches `(EntityError, CaptureError, LibrarianError)` and prints one line — it
    must never need to catch `SystemExit`, which `pytest.raises`/`except` clauses upstream (and a
    bare `except Exception`) would not otherwise stop from propagating past a library boundary."""
    assert issubclass(EntityError, RuntimeError)
    assert not issubclass(EntityError, SystemExit)


def test_each_subclass_carries_the_message_it_is_raised_with():
    for cls in (EntityError, CollisionError, CloneStateError, PushRaceError,
               CapabilityUnavailableError):
        ex = cls("a specific, operator-facing sentence")
        assert str(ex) == "a specific, operator-facing sentence"
