"""Collection floors for the active adversarial release gate."""
import pathlib
import re

_TESTS_DIR = pathlib.Path(__file__).resolve().parent

_FLOORS = {"adversarial_cat1": 7, "adversarial_cat2": 6, "adversarial_cat7": 5}


def _count(pattern: str) -> int:
    rx = re.compile(rf"^def test_{pattern}_", re.M)
    return sum(len(rx.findall(p.read_text(encoding="utf-8")))
               for p in _TESTS_DIR.rglob("test_*.py"))


def test_every_armed_category_meets_its_collection_floor():
    counts = {cat: _count(cat) for cat in _FLOORS}
    below = {cat: (n, _FLOORS[cat]) for cat, n in counts.items() if n < _FLOORS[cat]}
    assert not below, (
        f"armed adversarial categories fell below their collection floor {below} — the release "
        f"gate collects by name, so a rename or deletion here silently disarms it")
