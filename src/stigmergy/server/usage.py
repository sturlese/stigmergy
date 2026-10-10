"""Model spend per tenant: the tokens of each filing and each answer, with model and operation.

Recording never fails the work it records. OCR, the layer and alias classifier, and embeddings
are not recorded yet; the master console labels its figure an estimate.
"""
from __future__ import annotations

import logging

from stigmergy.capture.schema import startup_ddl_lock
from stigmergy.kernel import tenancy

log = logging.getLogger(__name__)

# Above what the privacy-compatible hosts charged in measured runs, so estimates err high.
PRICES_USD_PER_MTOK = {
    "deepseek/deepseek-v4.1-flash": (0.20, 0.80),
}
OPERATIONS = ("filing", "answer")

_DDL = f"""
CREATE TABLE IF NOT EXISTS model_usage (
    id BIGSERIAL PRIMARY KEY,
    {tenancy.TENANT_COLUMN},
    at TIMESTAMPTZ NOT NULL DEFAULT now(),
    operation TEXT NOT NULL CHECK (operation IN ({", ".join(f"'{o}'" for o in OPERATIONS)})),
    model TEXT NOT NULL,
    requests INTEGER NOT NULL DEFAULT 0,
    input_tokens BIGINT NOT NULL DEFAULT 0,
    output_tokens BIGINT NOT NULL DEFAULT 0,
    cache_read_tokens BIGINT NOT NULL DEFAULT 0
)
"""


def ensure_schema(conn) -> None:
    with startup_ddl_lock(conn) as cur:
        cur.execute(_DDL)
        cur.execute("CREATE INDEX IF NOT EXISTS model_usage_at_idx ON model_usage (tenant_id, at)")
        tenancy.protect(cur, "model_usage")


def _model_key(model: str) -> str:
    return model.split(":", 1)[1] if model.startswith("openrouter:") else model


def estimate_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    price_in, price_out = PRICES_USD_PER_MTOK.get(_model_key(model), (0.0, 0.0))
    return round((input_tokens * price_in + output_tokens * price_out) / 1_000_000, 6)


def record(conn, *, operation: str, model: str, requests: int = 0, input_tokens: int = 0,
           output_tokens: int = 0, cache_read_tokens: int = 0) -> None:
    """Append one row for the connection's tenant; bookkeeping never fails a filing or answer."""
    if not (requests or input_tokens or output_tokens):
        return
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('model_usage') IS NOT NULL")
            if not cur.fetchone()[0]:
                return
            cur.execute("INSERT INTO model_usage (operation, model, requests, input_tokens, "
                        "output_tokens, cache_read_tokens) VALUES (%s, %s, %s, %s, %s, %s)",
                        (operation, model, int(requests), int(input_tokens), int(output_tokens),
                         int(cache_read_tokens)))
    except Exception as error:  # noqa: BLE001
        log.error("model usage write failed (%s)", error.__class__.__name__)


def record_filing(conn, report: dict | None, model: str) -> None:
    """The tokens every model phase of one filing spent, from its capture report."""
    phases = ((report or {}).get("model_telemetry") or {}).values()
    totals = {key: sum(int((phase or {}).get(key) or 0) for phase in phases)
              for key in ("requests", "input_tokens", "output_tokens", "cache_read_tokens")}
    record(conn, operation="filing", model=model, **totals)


def record_answer(conn, usage: dict | None, model: str) -> None:
    usage = usage or {}
    record(conn, operation="answer", model=model, requests=usage.get("requests", 0),
           input_tokens=usage.get("input_tokens", 0), output_tokens=usage.get("output_tokens", 0),
           cache_read_tokens=usage.get("cache_read_tokens", 0))


def spend_by_tenant(conn, *, days: int = 7) -> dict[str, dict]:
    """`{tenant id: {input_tokens, output_tokens, usd}}` over the last `days`, every tenant."""
    with tenancy.platform_scope(conn), conn.cursor() as cur:
        cur.execute("SELECT to_regclass('model_usage') IS NOT NULL")
        if not cur.fetchone()[0]:
            return {}
        cur.execute("SELECT tenant_id, model, sum(input_tokens), sum(output_tokens) FROM model_usage "
                    "WHERE at > now() - make_interval(days => %s) GROUP BY tenant_id, model",
                    (int(days),))
        rows = cur.fetchall()
    spend: dict[str, dict] = {}
    for tenant, model, input_tokens, output_tokens in rows:
        entry = spend.setdefault(str(tenant), {"input_tokens": 0, "output_tokens": 0, "usd": 0.0})
        entry["input_tokens"] += int(input_tokens or 0)
        entry["output_tokens"] += int(output_tokens or 0)
        entry["usd"] = round(entry["usd"] + estimate_usd(model, int(input_tokens or 0),
                                                         int(output_tokens or 0)), 6)
    return spend
