"""The adversarial release gate's collection floor.

`make adversarial` and `evals/run_gates.py` collect the armed categories by NAME
(`-k "adversarial_cat1 or adversarial_cat2 or adversarial_cat7"`). A `-k` expression fails
OPEN: rename the tests and the gate green-lights an empty run. This keyless test pins the
floor — at least as many named cases per armed category as existed the day the gate armed —
so silent de-collection turns red in CI, where the gate itself cannot run (CI is keyless for
the golden halves; the adversarial half runs here too, but the FLOOR must hold regardless).

Cat 5 (secrets/PII) is not an armed release-gate category — `gates.gate_pii` itself stays — so
its count is not pinned here; cat 6 went with the loop it belonged to, and would need a fresh
design if that were ever rebuilt.
"""
import pathlib
import re

_TESTS_DIR = pathlib.Path(__file__).resolve().parent

# The counts the day the gate armed: cat1 = 4 (answer) + 10 (librarian) · cat2 = 6 (answer) ·
# cat7 = 7 (capture) + 5 (librarian). Floors, not exact counts — growth never turns this red.
_FLOORS = {"adversarial_cat1": 14, "adversarial_cat2": 6, "adversarial_cat7": 12}


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
