import pytest

from stigmergy.kernel.normalize import resolution_key


@pytest.mark.parametrize("spelling", [
    "Velnor",
    "VELNOR",
    "  velnor  ",
    "Velnor.",
    "(Velnor)",
    'Velnor"',
    "Velnor/",
    "Vélnor",
])
def test_resolution_key_folds_typographic_variants(spelling):
    assert resolution_key(spelling) == "velnor"


def test_empty_names_fold_to_nothing():
    for spelling in ("", "   ", "\n", "..."):
        assert resolution_key(spelling) == ""


@pytest.mark.parametrize("spelling", [
    "Velnor SL",
    "Velnor S.L.",
    "Velnor, S.L.",
    "Velnor Inc",
    "VELNOR LTD",
    "Velnor Limited",
    "Velnor Corp",
    "Velnor Co",
    "Velnor GmbH",
    "Velnor B.V.",
    "Velnor S.L.U.",
    "Velnor Sociedad Limitada",
])
def test_legal_forms_are_not_identity_equivalence(spelling):
    assert resolution_key(spelling) != "velnor"


@pytest.mark.parametrize("spelling,expected", [
    ("Velnor Holdings", "velnor holdings"),
    ("Velnor Group", "velnor group"),
    ("Velnor España", "velnor espana"),
    ("Velnor (formerly Patchwise)", "velnor formerly patchwise"),
    ("Velnor Legal", "velnor legal"),
])
def test_qualifiers_remain_part_of_the_resolution_key(spelling, expected):
    assert resolution_key(spelling) == expected
