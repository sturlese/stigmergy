"""`entities.errors` — the hierarchy IS the contract every other test in this package relies on
(`pytest.raises(CollisionError)`, `pytest.raises(EntityError)`), and it is the contract
`librarian.identity` relies on too: it catches the two classes SEPARATELY, turning one into a
finding about a declared field and the other into a finding naming the entity to anchor to
instead. A rename, a dropped base class or a merge of the two would silently change which finding
a capture gets, with every existing test still green.
"""
from stigmergy.entities.errors import CollisionError, EntityError


def test_a_collision_is_an_entity_error_and_the_base_is_not_a_collision():
    """The one subclass relationship this hierarchy has, asserted in BOTH directions.

    `except EntityError` must keep catching the collision verdict — one handler covers every
    refusal. And `EntityError` must not become a collision: `librarian.identity`'s collision arm
    writes "anchor to that entity instead", which is wrong prose for a malformed name.
    """
    assert issubclass(CollisionError, EntityError)
    assert not issubclass(EntityError, CollisionError)


def test_entity_error_itself_is_a_runtime_error_never_system_exit():
    """These are raised inside the worker's filing pass, which catches by type and turns the
    sentence into a gate finding. A `SystemExit` would propagate past every `except` clause in
    that pass and kill the process mid-capture instead."""
    assert issubclass(EntityError, RuntimeError)
    assert not issubclass(EntityError, SystemExit)


def test_each_class_carries_the_message_it_is_raised_with():
    for cls in (EntityError, CollisionError):
        ex = cls("a specific, person-facing sentence")
        assert str(ex) == "a specific, person-facing sentence"
