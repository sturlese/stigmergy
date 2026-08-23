"""The two count validators — `repair.settings._int_setting` and `gardener.settings.int_setting`
— are a DECLARED duplication (importing one would put a cross-package edge on the other for one
function), and a declared duplication needs a pin: the `maximum=` rule landed on the gardener's
copy first and reached this one late, with a comment promising parity that nothing enforced.
"""
import pytest

from stigmergy.gardener import settings as gardener_settings
from stigmergy.repair import settings as repair_settings
from stigmergy.repair.settings import RepairSettings
from stigmergy.server.errors import StartupError


@pytest.mark.parametrize("validator", [repair_settings._int_setting,
                                       gardener_settings.int_setting],
                         ids=["repair", "gardener"])
def test_both_count_validators_enforce_the_same_rules(validator, monkeypatch):
    """Behavioural parity, driven through BOTH copies with the same inputs, so the next rule
    cannot land on one copy only. (Exact signature equality is deliberately not asserted — the
    gardener's copy carries a `why` wording hook the repair one has no caller for.)"""
    monkeypatch.setenv("PARITY_TEST_KNOB", "5")
    assert validator("PARITY_TEST_KNOB", 3) == 5
    assert validator("PARITY_TEST_KNOB", 3, maximum=10) == 5

    monkeypatch.setenv("PARITY_TEST_KNOB", "11")
    with pytest.raises(StartupError, match="maximum of 10"):
        validator("PARITY_TEST_KNOB", 3, maximum=10)

    monkeypatch.setenv("PARITY_TEST_KNOB", "0")
    with pytest.raises(StartupError):
        validator("PARITY_TEST_KNOB", 3)

    monkeypatch.setenv("PARITY_TEST_KNOB", "not-a-number")
    with pytest.raises(StartupError):
        validator("PARITY_TEST_KNOB", 3)


def test_the_plan_ceiling_is_refused_when_it_is_not_a_positive_integer(monkeypatch):
    """The one count knob this package still has, driven through `from_env` rather than through the
    validator alone — a settings field wired to the wrong env name would pass the parity test above
    and fail nowhere else."""
    monkeypatch.setenv(repair_settings.MAX_PLAN_BYTES_ENV, "0")
    with pytest.raises(StartupError, match="positive integer"):
        RepairSettings.from_env()


def test_a_configured_plan_ceiling_is_accepted_the_benign_twin(monkeypatch):
    monkeypatch.setenv(repair_settings.MAX_PLAN_BYTES_ENV, "12345")
    assert RepairSettings.from_env().max_plan_bytes == 12345
