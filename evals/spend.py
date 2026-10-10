"""Spend control for paid evaluation runs: one price table, one ledger, one hard cap.

Every paid runner opens a `Budget` with its own sub-cap. The ledger in `evals/out/spend.ndjson`
accumulates every charged call across runs, so no run can start when the ledger total plus its
sub-cap would pass `TOTAL_CAP_USD`. Charges price every input token at the full input rate, a
hedged request twice, and a call without usage at a worst case. When an OpenRouter key is
available, the provider's own usage counter is authoritative: a run stops once the counter has
moved by its cap, and no run starts when the counter's movement since the baseline row plus its cap
would pass the total. Other traffic on the same key only makes both guards stricter.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import fcntl
import json
import os
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
LEDGER = ROOT / "evals" / "out" / "spend.ndjson"
TOTAL_CAP_USD = 3.00
# Prices above what the privacy-compatible hosts OpenRouter routes to by throughput charged in
# measured runs (about 0.16 and 0.65); estimates err high. The provider counter is the truth.
PRICES_USD_PER_MTOK = {
    "deepseek/deepseek-v4.1-flash": (0.20, 0.80),
    "openai/text-embedding-3-large": (0.13, 0.0),
}
UNKNOWN_ASK_USD = 0.02
_CHARS_PER_TOKEN = 3
_KEY_URL = "https://openrouter.ai/api/v1/key"


class BudgetExceeded(RuntimeError):
    """The run reached its sub-cap, or the total cap would be passed."""


def _model_key(model: str) -> str:
    return model.removeprefix("openrouter:")


def cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    input_price, output_price = PRICES_USD_PER_MTOK[_model_key(model)]
    return (max(0, input_tokens) * input_price + max(0, output_tokens) * output_price) / 1_000_000


def _rows(ledger: Path) -> list[dict]:
    if not ledger.exists():
        return []
    return [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines() if line.strip()]


def ledger_total(ledger: Path = LEDGER) -> float:
    return round(sum(float(row.get("usd", 0.0)) for row in _rows(ledger)), 6)


@contextlib.contextmanager
def _locked(ledger: Path):
    """Serialize ledger reads and writes across concurrent runs."""
    ledger.parent.mkdir(parents=True, exist_ok=True)
    with open(ledger.with_suffix(".lock"), "a", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _append(ledger: Path, row: dict) -> None:
    row = {"ts": dt.datetime.now(dt.UTC).isoformat(), **row}
    with _locked(ledger), ledger.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, sort_keys=True) + "\n")


def provider_usage(api_key: str | None = None, *, client: httpx.Client | None = None) -> float | None:
    """The key's lifetime spend reported by OpenRouter, or ``None`` when it cannot be read."""
    key = api_key if api_key is not None else os.environ.get("OPENROUTER_API_KEY", "")
    if not key:
        return None
    try:
        response = (client or httpx).get(_KEY_URL, headers={"Authorization": f"Bearer {key}"}, timeout=20)
        response.raise_for_status()
        return float(response.json()["data"]["usage"])
    except (httpx.HTTPError, KeyError, TypeError, ValueError):
        return None


def provider_baseline(ledger: Path = LEDGER) -> float | None:
    for row in _rows(ledger):
        if row.get("kind") == "baseline":
            return float(row["provider_usage"])
    return None


def record_baseline(usage: float, ledger: Path = LEDGER) -> None:
    if provider_baseline(ledger) is None:
        _append(ledger, {"kind": "baseline", "provider_usage": usage, "usd": 0.0})


class Budget:
    """A run's spending guard. `check()` before a paid call; `charge()` after it."""

    def __init__(self, run: str, max_usd: float, *, ledger: Path = LEDGER,
                 total_cap: float = TOTAL_CAP_USD, usage_reader=provider_usage,
                 models: tuple[str, ...] = ()) -> None:
        if max_usd <= 0:
            raise BudgetExceeded("--max-usd must be positive")
        unpriced = [m for m in models if _model_key(m) not in PRICES_USD_PER_MTOK]
        if unpriced:
            raise BudgetExceeded(f"{run}: no price for {unpriced}; add it before any paid call")
        self.run = run
        self.max_usd = max_usd
        self.ledger = ledger
        self.total_cap = total_cap
        self.spent = 0.0
        self.calls = 0
        self._usage_reader = usage_reader
        with _locked(ledger):
            before = ledger_total(ledger)
        if before + max_usd > total_cap + 1e-9:
            raise BudgetExceeded(
                f"{run}: ledger total {before:.4f} USD plus this run's cap {max_usd:.2f} USD "
                f"exceeds the {total_cap:.2f} USD evaluation cap")
        self.provider_at_start = usage_reader()
        baseline = provider_baseline(ledger)
        if baseline is None and self.provider_at_start is not None:
            record_baseline(self.provider_at_start, ledger)
            baseline = self.provider_at_start
        if (baseline is not None and self.provider_at_start is not None
                and self.provider_at_start - baseline + max_usd > total_cap + 1e-9):
            raise BudgetExceeded(
                f"{run}: OpenRouter reports {self.provider_at_start - baseline:.4f} USD spent since "
                f"the baseline; this run's cap would pass the {total_cap:.2f} USD evaluation cap")

    def provider_spent(self) -> float | None:
        """What OpenRouter reports this run has spent so far, other traffic on the key included."""
        now = self._usage_reader()
        if now is None or self.provider_at_start is None:
            return None
        return now - self.provider_at_start

    @property
    def exhausted(self) -> bool:
        if self.spent >= self.max_usd:
            return True
        provider = self.provider_spent()
        return provider is not None and provider >= self.max_usd

    def check(self) -> None:
        if self.exhausted:
            raise BudgetExceeded(
                f"{self.run}: reached its {self.max_usd:.2f} USD cap "
                f"(estimated {self.spent:.4f} USD, provider {self.provider_spent()})")

    def charge(self, model: str, input_tokens: int, output_tokens: int, *, note: str = "",
               multiplier: int = 1) -> float:
        usd = cost_usd(model, input_tokens, output_tokens) * multiplier
        return self._record(usd, model=_model_key(model), input_tokens=input_tokens,
                            output_tokens=output_tokens, note=note)

    def charge_usd(self, usd: float, *, model: str, note: str) -> float:
        return self._record(usd, model=_model_key(model), note=note)

    def charge_text(self, model: str, texts: list[str], *, note: str = "") -> float:
        tokens = sum(len(text) for text in texts) // _CHARS_PER_TOKEN + len(texts)
        return self.charge(model, tokens, 0, note=note)

    def _record(self, usd: float, **fields) -> float:
        self.spent += usd
        self.calls += 1
        _append(self.ledger, {"kind": "charge", "run": self.run, "usd": round(usd, 8), **fields})
        return usd

    def summary(self) -> dict:
        end = self._usage_reader()
        delta = (end - self.provider_at_start
                 if end is not None and self.provider_at_start is not None else None)
        return {"run": self.run, "calls": self.calls, "charged_usd": round(self.spent, 6),
                "cap_usd": self.max_usd, "complete": not self.exhausted,
                "provider_delta_usd": None if delta is None else round(delta, 6),
                "ledger_total_usd": ledger_total(self.ledger)}


class MeteredEmbedder:
    """Charge every embedding call to a budget before delegating to the real embedder."""

    def __init__(self, inner, budget: Budget) -> None:
        self._inner = inner
        self._budget = budget

    def embed(self, texts, *args, **kwargs):
        self._budget.check()
        self._budget.charge_text(self._inner.model, list(texts), note="embed")
        return self._inner.embed(texts, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._inner, name)


def add_budget_arguments(parser) -> None:
    parser.add_argument("--max-usd", type=float, default=None,
                        help="hard spending cap for this run; required for paid backends")
    parser.add_argument("--live", action="store_true",
                        help="acknowledge that this run calls OpenRouter and costs money")


def require_budget(parser, args, run: str, *, paid: bool, expected: str,
                   models: tuple[str, ...] = ()) -> Budget | None:
    """Refuse a paid run without an explicit cap and acknowledgement; print the expected cost."""
    if not paid:
        return None
    if args.max_usd is None or not args.live:
        parser.error(f"a paid run needs --live and --max-usd (expected cost: {expected})")
    print(f"{run}: expected cost {expected}; cap {args.max_usd:.2f} USD; "
          f"ledger so far {ledger_total():.4f} USD of {TOTAL_CAP_USD:.2f}")
    try:
        return Budget(run, args.max_usd, models=models)
    except BudgetExceeded as error:
        parser.exit(2, f"{error}\n")
