import pytest

from stigmergy.kernel.normalize import resolution_key


@pytest.mark.parametrize("spelling", [
    "Cofers",
    "COFERS",
    "  cofers  ",
    "Cofers.",
    "(Cofers)",
    'Cofers"',
    "Cofers/",
    "Côfers",
])
def test_resolution_key_folds_typographic_variants(spelling):
    assert resolution_key(spelling) == "cofers"


def test_empty_names_fold_to_nothing():
    for spelling in ("", "   ", "\n", "..."):
        assert resolution_key(spelling) == ""


@pytest.mark.parametrize("spelling", [
    "Cofers SL",
    "Cofers S.L.",
    "Cofers, S.L.",
    "Cofers Inc",
    "COFERS LTD",
    "Cofers Limited",
    "Cofers Corp",
    "Cofers Co",
    "Cofers GmbH",
    "Cofers B.V.",
    "Cofers S.L.U.",
    "Cofers Sociedad Limitada",
])
def test_legal_forms_are_not_identity_equivalence(spelling):
    assert resolution_key(spelling) != "cofers"


@pytest.mark.parametrize("spelling,expected", [
    ("Cofers Holdings", "cofers holdings"),
    ("Cofers Group", "cofers group"),
    ("Cofers España", "cofers espana"),
    ("Cofers (formerly Nubelo)", "cofers formerly nubelo"),
    ("Cofers Legal", "cofers legal"),
])
def test_qualifiers_remain_part_of_the_resolution_key(spelling, expected):
    assert resolution_key(spelling) == expected
