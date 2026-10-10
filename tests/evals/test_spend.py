"""`evals/spend.py`: the evaluation spending cap holds across runs and within one run."""
import pytest

from evals import spend


def _budget(tmp_path, max_usd, *, reader=lambda: None, cap=2.0):
    return spend.Budget("run", max_usd, ledger=tmp_path / "spend.ndjson", total_cap=cap,
                        usage_reader=reader)


def test_every_input_token_is_priced_at_the_full_input_rate():
    assert spend.cost_usd("openrouter:deepseek/deepseek-v4.1-flash", 1_000_000, 1_000_000) == \
        pytest.approx(0.20 + 0.80)
    assert spend.cost_usd("openai/text-embedding-3-large", 1_000_000, 0) == pytest.approx(0.13)


def test_a_run_is_refused_when_the_ledger_plus_its_cap_passes_the_total(tmp_path):
    first = _budget(tmp_path, 1.5)
    first.charge_usd(1.2, model="deepseek/deepseek-v4.1-flash", note="x")
    with pytest.raises(spend.BudgetExceeded):
        _budget(tmp_path, 0.9)
    assert _budget(tmp_path, 0.8).max_usd == 0.8


def test_a_run_is_exhausted_once_its_charges_reach_its_cap(tmp_path):
    budget = _budget(tmp_path, 0.01)
    budget.check()
    budget.charge("deepseek/deepseek-v4.1-flash", 0, 20_000)
    assert budget.exhausted
    with pytest.raises(spend.BudgetExceeded):
        budget.check()


def test_a_hedged_request_is_charged_twice(tmp_path):
    budget = _budget(tmp_path, 1.0)
    single = budget.charge("deepseek/deepseek-v4.1-flash", 1000, 1000)
    double = budget.charge("deepseek/deepseek-v4.1-flash", 1000, 1000, multiplier=2)
    assert double == pytest.approx(2 * single)


def test_the_provider_counter_blocks_a_run_whose_cap_would_pass_the_total(tmp_path):
    readings = iter([10.0, 11.95, 11.95])
    _budget(tmp_path, 0.1, reader=lambda: next(readings))
    with pytest.raises(spend.BudgetExceeded):
        _budget(tmp_path, 0.1, reader=lambda: next(readings))
    assert _budget(tmp_path, 0.05, reader=lambda: next(readings)).max_usd == 0.05


def test_a_model_without_a_price_is_refused_before_any_call(tmp_path):
    with pytest.raises(spend.BudgetExceeded, match="no price"):
        spend.Budget("run", 0.1, ledger=tmp_path / "spend.ndjson", usage_reader=lambda: None,
                     models=("openrouter:some/unpriced-model",))


def test_the_metered_embedder_charges_before_it_delegates(tmp_path):
    class Inner:
        model = "openai/text-embedding-3-large"

        def embed(self, texts, **_):
            return [[0.0] for _ in texts]

    budget = _budget(tmp_path, 1.0)
    metered = spend.MeteredEmbedder(Inner(), budget)
    assert metered.embed(["a" * 3000]) == [[0.0]]
    assert metered.model == "openai/text-embedding-3-large"
    assert budget.calls == 1 and budget.spent > 0


def test_the_provider_counter_stops_a_run_its_own_estimates_would_let_continue(tmp_path):
    reading = {"usd": 5.0}
    budget = _budget(tmp_path, 0.1, reader=lambda: reading["usd"])
    reading["usd"] = 5.02
    budget.check()
    reading["usd"] = 5.2
    with pytest.raises(spend.BudgetExceeded, match="provider"):
        budget.check()
