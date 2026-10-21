#!/usr/bin/env python3
"""
MEDICIÓN — protección del time-stop para cierres <3 €.

Implementa el contrato congelado:
    fastapi/ops/2026-10-21-pre-registro-time-stop-3eur.md

No acepta parámetros que cambien la muestra, el horizonte, el supuesto
SL-primero ni el umbral. Si se cambia algo, se escribe una enmienda fechada
en el pre-registro y se vuelve a medir.

Sólo LEE: `audit_log`, `orders`, `trade_outcomes` y velas H1 del MCP.
No abre, no cierra y no modifica ninguna posición.

Uso (dentro del contenedor, que tiene /app/.env):
    python scripts/measure_time_stop_protection.py
"""
import asyncio
import datetime as dt
import json
import math
import os
import random
import statistics

import asyncpg

from config import settings
from services.mt5_client import get_account, get_candles_with_offset

MIN_N = 30
HORIZON_BARS = 48
PIP = 0.0001
BOOTSTRAP = 10_000
SEED = 42
OUT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "ops",
    "2026-10-21-time-stop-3eur-measure.json",
)

STOPS_SQL = """
WITH stops AS (
  SELECT f.created_at AS filled_at,
         f.data->>'symbol' AS symbol,
         (f.data->>'order_db_id')::bigint AS order_id
  FROM audit_log f
  JOIN LATERAL (
    SELECT c.created_at, c.data
    FROM audit_log c
    WHERE c.event = 'ai_predict_cycle'
      AND c.data->'data'->>'symbol' = f.data->>'symbol'
      AND c.created_at <= f.created_at
      AND c.created_at >= f.created_at - INTERVAL '10 seconds'
    ORDER BY c.created_at DESC
    LIMIT 1
  ) c ON true
  WHERE f.event = 'order_filled'
    AND c.data->'data'->>'time_stop' = 'true'
    AND c.data->'data'->>'decision' = 'CLOSE'
)
SELECT s.filled_at,
       s.symbol,
       s.order_id,
       o.type,
       o.entry::float8   AS entry,
       o.sl::float8      AS sl,
       o.tp::float8      AS tp,
       o.volume::float8  AS volume,
       o.created_at      AS order_created_at
FROM stops s
JOIN orders o ON o.id = s.order_id
ORDER BY s.filled_at
"""

OUTCOME_SQL = """
SELECT id,
       entry_time,
       exit_time,
       pnl::float8      AS pnl,
       pnl_pct::float8  AS pnl_pct,
       exit_reason
FROM trade_outcomes
WHERE symbol = $1
  AND direction = $2
  AND abs(EXTRACT(EPOCH FROM (entry_time - $3))) < 120
ORDER BY abs(EXTRACT(EPOCH FROM (entry_time - $3)))
LIMIT 1
"""


def _bootstrap_ci(xs: list[float]) -> list[float] | None:
    if len(xs) < 2:
        return None
    rng = random.Random(SEED)
    means = []
    n = len(xs)
    for _ in range(BOOTSTRAP):
        means.append(sum(xs[rng.randrange(n)] for _ in range(n)) / n)
    means.sort()
    return [round(means[int(0.025 * BOOTSTRAP)], 4),
            round(means[int(0.975 * BOOTSTRAP)], 4)]


def _max_dd(pnls: list[float]) -> float:
    """Max drawdown de la curva de PnL acumulado (EUR)."""
    peak = cum = 0.0
    dd = 0.0
    for p in pnls:
        cum += p
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
    return dd


def _counterfactual(order: dict, outcome: dict, bars: list[dict],
                    all_bars: list[dict], filled_at: dt.datetime) -> dict:
    """Mantiene la posición hasta TP/SL o 48 H1; sólo velas cerradas."""
    direction = "LONG" if order["type"] == "BUY" else "SHORT"
    entry = float(order["entry"])
    sl = float(order["sl"])
    tp = float(order["tp"])
    actual = float(outcome["pnl"])
    pnl_pct = float(outcome["pnl_pct"])

    # Precio de salida real a partir del retorno de precio (pnl_pct en %).
    if direction == "LONG":
        exit_actual = entry * (1.0 + pnl_pct / 100.0)
    else:
        exit_actual = entry * (1.0 - pnl_pct / 100.0)
    move = abs(exit_actual - entry)
    if move <= 0:
        return {"status": "invalid", "reason": "precio de salida no reconstruible"}
    eur_per_pip = abs(actual) / (move / PIP)

    start = math.ceil(filled_at.timestamp() / 3600.0) * 3600
    future = [b for b in bars if b["time"] >= start][:HORIZON_BARS]
    if not future:
        last = float(all_bars[-1]["close"]) if all_bars else exit_actual
        mtm = ((last - entry) if direction == "LONG" else (entry - last)) / PIP * eur_per_pip
        return {
            "status": "censored",
            "reason": "sin velas H1 cerradas después del cierre",
            "bars_available": 0,
            "mtm_pnl_eur": round(mtm, 2),
            "mtm_price": last,
            "eur_per_pip": round(eur_per_pip, 4),
        }

    exit_price = None
    exit_reason = None
    for b in future:
        if direction == "LONG":
            if b["low"] <= sl:          # SL primero (conservador)
                exit_price, exit_reason = sl, "sl"
                break
            if b["high"] >= tp:
                exit_price, exit_reason = tp, "tp"
                break
        else:
            if b["high"] >= sl:         # SL primero (conservador)
                exit_price, exit_reason = sl, "sl"
                break
            if b["low"] <= tp:
                exit_price, exit_reason = tp, "tp"
                break

    if exit_price is None:
        if len(future) >= HORIZON_BARS:
            exit_price, exit_reason = float(future[-1]["close"]), "horizon"
        else:
            last = float(all_bars[-1]["close"]) if all_bars else exit_actual
            mtm = ((last - entry) if direction == "LONG" else (entry - last)) / PIP * eur_per_pip
            return {
                "status": "censored",
                "reason": "TP/SL no tocados; horizonte no alcanzado",
                "bars_available": len(future),
                "mtm_pnl_eur": round(mtm, 2),
                "mtm_price": last,
                "eur_per_pip": round(eur_per_pip, 4),
            }

    cf = ((exit_price - entry) if direction == "LONG" else (entry - exit_price)) / PIP * eur_per_pip
    return {
        "status": "uncensored",
        "exit_reason": exit_reason,
        "exit_price": round(exit_price, 5),
        "cf_pnl_eur": round(cf, 2),
        "eur_per_pip": round(eur_per_pip, 4),
        "bars_used": len(future),
    }


async def main() -> int:
    conn = await asyncpg.connect(settings.database_url)
    try:
        stops = await conn.fetch(STOPS_SQL)
    finally:
        await conn.close()

    candle_cache: dict[str, tuple[list[dict], list[dict], float]] = {}
    units: list[dict] = []

    for s in stops:
        symbol = s["symbol"]
        if symbol not in candle_cache:
            raw, offset_h = await get_candles_with_offset(symbol, "H1", 1000)
            now = dt.datetime.now(dt.timezone.utc).timestamp()
            all_bars = sorted(raw, key=lambda c: c["time"])
            closed = [c for c in all_bars if c["time"] + 3600 <= now]
            candle_cache[symbol] = (closed, all_bars, float(offset_h))
        bars, all_bars, offset_h = candle_cache[symbol]

        order_created = s["order_created_at"]
        target_entry = order_created + dt.timedelta(hours=offset_h)
        direction = "LONG" if s["type"] == "BUY" else "SHORT"

        conn = await asyncpg.connect(settings.database_url)
        try:
            outcome = await conn.fetchrow(
                OUTCOME_SQL, symbol, direction, target_entry
            )
        finally:
            await conn.close()

        unit = {
            "symbol": symbol,
            "order_id": int(s["order_id"]),
            "filled_at": s["filled_at"].isoformat(),
            "type": s["type"],
            "entry": float(s["entry"]),
            "sl": float(s["sl"]),
            "tp": float(s["tp"]),
            "volume": float(s["volume"]),
            "actual_pnl_eur": round(float(outcome["pnl"]), 2) if outcome else None,
            "outcome_id": int(outcome["id"]) if outcome else None,
        }
        if not outcome:
            unit["note"] = "sin trade_outcomes emparejado (revisar reloj/entry_time)"
            units.append(unit)
            continue

        unit["actual_pnl_eur"] = round(float(outcome["pnl"]), 2)
        if not (0.0 < unit["actual_pnl_eur"] < 3.0):
            unit["note"] = "fuera de (0,3); no elegible para la protección"
            units.append(unit)
            continue

        cf = _counterfactual(s, outcome, bars, all_bars, s["filled_at"])
        unit["counterfactual"] = cf
        units.append(unit)

    eligible = [u for u in units if u.get("counterfactual") is not None]
    uncensored = [
        u for u in eligible
        if u["counterfactual"].get("status") == "uncensored"
    ]
    deltas = [
        u["counterfactual"]["cf_pnl_eur"] - u["actual_pnl_eur"]
        for u in uncensored
    ]
    ci = _bootstrap_ci(deltas)
    actual_seq = [u["actual_pnl_eur"] for u in eligible]
    cf_seq = [
        u["counterfactual"].get("cf_pnl_eur", u["counterfactual"].get("mtm_pnl_eur", 0.0))
        for u in eligible
    ]

    try:
        acct = await get_account()
        equity = float(acct.get("equity", 0.0)) or None
    except Exception:  # noqa: BLE001
        equity = None

    ctrl_dd = _max_dd(actual_seq) if actual_seq else None
    cf_dd = _max_dd(cf_seq) if cf_seq else None
    ctrl_dd_pct = round(ctrl_dd / equity * 100, 2) if ctrl_dd is not None and equity else None
    cf_dd_pct = round(cf_dd / equity * 100, 2) if cf_dd is not None and equity else None

    n_unc = len(uncensored)
    if n_unc < MIN_N:
        decision = "not_implemented_insufficient_sample"
        reason = f"n no censurado={n_unc} < {MIN_N}: no hay decisión"
    elif ci is not None and ci[0] > 0 and cf_dd_pct is not None and ctrl_dd_pct is not None \
            and cf_dd_pct <= ctrl_dd_pct + 2.0:
        decision = "implement"
        reason = "IC 95 % de Δ > 0 y DD dentro de +2 pp"
    else:
        decision = "not_implemented_no_improvement"
        reason = "IC de Δ incluye 0 o el DD empeora >2 pp"

    result = {
        "study": "time_stop_protection_3eur",
        "prereg": "fastapi/ops/2026-10-21-pre-registro-time-stop-3eur.md",
        "measured_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "method": {
            "population": "cierres ejecutados por time_stop (order_filled + ciclo time_stop=true)",
            "eligibility": "0 < pnl_real < 3 EUR",
            "intervention": "no cerrar; TP/SL o 48 H1; SL primero si ambos en la vela",
            "horizon_bars": HORIZON_BARS,
            "min_n": MIN_N,
            "bootstrap": BOOTSTRAP,
            "seed": SEED,
        },
        "n_stops_executed": len(stops),
        "n_eligible_0_3": len(eligible),
        "n_uncensored": n_unc,
        "decision": decision,
        "reason": reason,
        "aggregate": {
            "delta_mean_eur": round(statistics.mean(deltas), 4) if deltas else None,
            "delta_ci95_eur": ci,
            "actual_wins_3eur": sum(1 for p in actual_seq if p >= 3.0),
            "cf_wins_3eur": sum(
                1 for u in uncensored if u["counterfactual"]["cf_pnl_eur"] >= 3.0
            ),
            "control_max_dd_eur": round(ctrl_dd, 2) if ctrl_dd is not None else None,
            "cf_max_dd_eur": round(cf_dd, 2) if cf_dd is not None else None,
            "control_max_dd_pct": ctrl_dd_pct,
            "cf_max_dd_pct": cf_dd_pct,
            "equity_eur": equity,
        },
        "units": units,
    }

    with open(OUT_PATH, "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=2, sort_keys=True)

    print(json.dumps({
        "n_stops_executed": result["n_stops_executed"],
        "n_eligible_0_3": result["n_eligible_0_3"],
        "n_uncensored": result["n_uncensored"],
        "decision": result["decision"],
        "reason": result["reason"],
        "aggregate": result["aggregate"],
        "out": OUT_PATH,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
