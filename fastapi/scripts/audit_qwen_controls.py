#!/usr/bin/env python3
"""
AUDITORÍA DE LOS CONTROLES DE QWEN (Opción B, Félix 2026-10-21).

Pregunta: los tres juicios de Qwen que aún pueden bloquear una entrada
(`reentry_ok`, `quality`, `regime`) ¿tienen skill medible contra la BASE del
mercado, o son ruido que a veces bloquea?

Método (el mismo de T-31.10, sin inventar una vara nueva):
- muestra: ciclos `ai_predict_cycle` con Qwen OK (no skipped/degraded) y una
  entrada PROPUESTA (`ppo_direction` BUY/SELL) en la ventana de días;
- resultado: dirección realizada a +24 h REALES con `canonical_direction`
  (terminal = última vela H1 cerrada con time <= t0+24h, t0 convertido de
  reloj broker a UTC con el desfase medido);
- acierto del control: la dirección propuesta coincide con la realizada;
- base: la mejor predicción CONSTANTE de la MISMA muestra
  (`skill_vs_base`), no el 50 %;
- comparación de dos grupos (bloqueado vs permitido): diferencia en pp,
  IC 95 % (Wald) y p-valor de dos proporciones;
- retorno secundario: pips firmados a +24 h SIN costes (es una medida de
  señal, no de PnL; no se resta un spread inventado).
- n < 30 en un grupo = «insuficiente», no se declara skill ni se revoca.

Uso:
    DATABASE_URL=... INTERNAL_TOKEN=... HMAC_SECRET=... \
    MT5_HTTP_URL=http://... PYTHONPATH=. \
    python scripts/audit_qwen_controls.py --days 30 --symbols EURUSD,USDCHF
"""
import argparse
import asyncio
import bisect
import datetime as dt
import json
import math
import statistics
import sys
import time

import asyncpg
import httpx

from services.measurement import canonical_direction, skill_vs_base

MIN_N = 30
HORIZON_H = 24


def _pip(symbol: str) -> float:
    return 0.0001 if symbol.upper().endswith("USD") or symbol.upper().startswith("EUR") else 0.0001


def _outcome(bar_t: list, bar_c: list, t0: float, now: float) -> dict | None:
    """Dirección + cambio bruto a +24 h, con los MISMOS índices canónicos."""
    if not bar_t or bar_t[-1] < t0 + HORIZON_H * 3600:
        return None
    i0 = bisect.bisect_right(bar_t, t0) - 1
    i1 = bisect.bisect_right(bar_t, t0 + HORIZON_H * 3600) - 1
    if i0 < 0 or i1 >= len(bar_c) or i1 <= i0:
        return None
    if bar_t[i1] + 3600 > now:
        return None
    change = bar_c[i1] - bar_c[i0]
    if abs(change) <= 0.00005:
        return None
    d = 1 if change > 0 else -1
    # cordura: la dirección local debe coincidir con la canónica
    assert canonical_direction(bar_t, bar_c, t0, now) == d
    return {"dir": d, "change": change}


def _group(rows: list[dict]) -> dict:
    n = len(rows)
    if n == 0:
        return {"n": 0, "note": "sin muestra"}
    hits = sum(1 for r in rows if r["hit"])
    dirs = [r["realized"] for r in rows]
    sk = skill_vs_base(hits, n, dirs)
    pips = [r["signed_pips"] for r in rows]
    mean = statistics.mean(pips) if pips else None
    sd = statistics.stdev(pips) if len(pips) > 1 else 0.0
    ci = (1.96 * sd / math.sqrt(n)) if n > 1 else None
    return {
        "n": n,
        "hits": hits,
        "acc": sk.get("acc"),
        "base": sk.get("base"),
        "skill_pp": sk.get("skill_pp"),
        "wilson_lo": sk.get("wilson_lo"),
        "skill_lo_pp": sk.get("skill_lo_pp"),
        "granted": sk.get("granted"),
        "avg_signed_pips": round(mean, 2) if mean is not None else None,
        "avg_signed_pips_ci95": (
            [round(mean - ci, 2), round(mean + ci, 2)] if ci is not None else None
        ),
        "insufficient": n < MIN_N,
    }


def _wilson_bounds(hits: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Intervalo de Wilson (bilateral) para una proporción."""
    p = hits / n
    den = 1 + z * z / n
    centre = p + z * z / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (
        max(0.0, (centre - margin) / den),
        min(1.0, (centre + margin) / den),
    )


def _two_prop(a: dict, b: dict) -> dict | None:
    n1, n2 = a.get("n", 0), b.get("n", 0)
    if not n1 or not n2:
        return None
    h1, h2 = a["hits"], b["hits"]
    p1, p2 = h1 / n1, h2 / n2
    pooled = (h1 + h2) / (n1 + n2)
    se = math.sqrt(pooled * (1 - pooled) * (1 / n1 + 1 / n2))
    if se == 0:
        return None
    z = (p1 - p2) / se
    pval = math.erfc(abs(z) / math.sqrt(2))
    diff = p1 - p2
    # 2026-10-21: IC de Newcombe (híbrido score), no Wald. El Wald se
    # degenera con celdas 0 y n<30 (quality<5: n=1 ⇒ IC [−29,5, −16,4]
    # con p=0,586; eff<0,35: n=19 ⇒ IC que excluía 0 con p=0,052).
    # Newcombe es consistente con el test y robusto en esos casos.
    l1, u1 = _wilson_bounds(h1, n1)
    l2, u2 = _wilson_bounds(h2, n2)
    lo = diff - math.sqrt((p1 - l1) ** 2 + (u2 - p2) ** 2)
    hi = diff + math.sqrt((u1 - p1) ** 2 + (p2 - l2) ** 2)
    return {
        "diff_pp": round(diff * 100, 2),
        "z": round(z, 3),
        "p_value": round(pval, 6),
        "ci95_pp": [round(lo * 100, 2), round(hi * 100, 2)],
        "ci_method": "newcombe_hybrid_score",
    }


async def _rows(conn, days: int, symbols: list[str]) -> list[dict]:
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)
    return await conn.fetch(
        """
        SELECT (data->'data'->>'mt5_time')::float AS mt5_time,
               data->'data'->>'symbol'          AS symbol,
               data->'data'->>'ppo_direction'   AS direction,
               (data->'data'->>'quality_score')::float AS quality,
               data->'data'->>'regime'          AS regime,
               data->'data'->>'reentry_ok'      AS reentry_ok,
               (data->'data'->>'effective_prob')::float AS effective_prob
        FROM audit_log
        WHERE event='ai_predict_cycle'
          AND created_at >= $1
          AND data->'data'->>'symbol' = ANY($2::text[])
          AND data->'data'->>'qwen_skipped' = 'false'
          AND data->'data'->>'qwen_degraded' = 'false'
          AND data->'data'->>'ppo_direction' IN ('BUY','SELL')
        ORDER BY created_at
        """,
        since, symbols,
    )


async def _measure_symbol(conn, symbol: str, days: int,
                          market_port=None) -> dict:
    rows = await _rows(conn, days, [symbol])
    if market_port is None:
        from core.container import build_registry_from_settings
        market_port = build_registry_from_settings().get(symbol).market
    raw, offset_h = await market_port.get_candles_with_offset(symbol, "H1", 2000)
    now = time.time()
    closed = [c for c in raw if float(c["time"]) + 3600 <= now]
    pairs = sorted((float(c["time"]), float(c["close"])) for c in closed)
    bar_t = [p[0] for p in pairs]
    bar_c = [p[1] for p in pairs]

    evaluated: list[dict] = []
    for r in rows:
        if r["mt5_time"] is None:
            continue
        t0 = float(r["mt5_time"]) - float(offset_h) * 3600.0
        out = _outcome(bar_t, bar_c, t0, now)
        if out is None:
            continue
        direction = 1 if r["direction"] == "BUY" else -1
        evaluated.append({
            "direction": direction,
            "realized": out["dir"],
            "hit": out["dir"] == direction,
            "signed_pips": direction * out["change"] / _pip(symbol),
            "quality": r["quality"],
            "regime": r["regime"],
            "reentry_ok": r["reentry_ok"],
            "effective_prob": r["effective_prob"],
        })

    # reentry_ok: SÓLO se evalúa dentro de la ventana de re-entrada
    reentry_rows = [r for r in evaluated if r["reentry_ok"] in ("true", "false")]
    blocked = _group([r for r in reentry_rows if r["reentry_ok"] == "false"])
    allowed = _group([r for r in reentry_rows if r["reentry_ok"] == "true"])

    # quality: el veto vivo es < 5; el score se compara también por buckets
    q_lt5 = _group([r for r in evaluated if r["quality"] is not None and r["quality"] < 5])
    q_ge5 = _group([r for r in evaluated if r["quality"] is not None and r["quality"] >= 5])
    buckets = {}
    for lo, hi in [(5, 7), (7, 8), (8, 9), (9, 11)]:
        g = _group([r for r in evaluated if r["quality"] is not None and lo <= r["quality"] < hi])
        buckets[f"{lo}-{hi - 1}"] = g

    # regime: RANGING vs no-RANGING; y la condición EXACTA del veto
    ranging = _group([r for r in evaluated if r["regime"] == "RANGING"])
    not_ranging = _group([r for r in evaluated if r["regime"] not in (None, "RANGING")])
    veto_cond = _group([
        r for r in evaluated
        if r["regime"] == "RANGING"
        and ((r["quality"] is not None and r["quality"] < 5)
             or (r["effective_prob"] is not None and r["effective_prob"] < 0.35))
    ])
    not_veto_cond = _group([
        r for r in evaluated
        if not (r["regime"] == "RANGING"
                and ((r["quality"] is not None and r["quality"] < 5)
                     or (r["effective_prob"] is not None and r["effective_prob"] < 0.35)))
    ])

    # effective_prob: el segundo componente del veto RANGING (eff < 0.35)
    eff_lt = _group([r for r in evaluated if r["effective_prob"] is not None and r["effective_prob"] < 0.35])
    eff_ge = _group([r for r in evaluated if r["effective_prob"] is not None and r["effective_prob"] >= 0.35])

    return {
        "symbol": symbol,
        "candles": {
            "n": len(pairs),
            "from": dt.datetime.fromtimestamp(bar_t[0], dt.timezone.utc).isoformat() if bar_t else None,
            "to": dt.datetime.fromtimestamp(bar_t[-1], dt.timezone.utc).isoformat() if bar_t else None,
            "offset_h": offset_h,
        },
        "evaluated": len(evaluated),
        "direction_counts": {
            "BUY": sum(1 for r in evaluated if r["direction"] == 1),
            "SELL": sum(1 for r in evaluated if r["direction"] == -1),
            "realized_up": sum(1 for r in evaluated if r["realized"] == 1),
            "realized_down": sum(1 for r in evaluated if r["realized"] == -1),
        },
        "reentry_ok": {
            "blocked_false": blocked,
            "allowed_true": allowed,
            "blocked_minus_allowed": _two_prop(blocked, allowed),
        },
        "quality": {
            "lt5": q_lt5,
            "ge5": q_ge5,
            "lt5_minus_ge5": _two_prop(q_lt5, q_ge5),
            "buckets": buckets,
        },
        "regime": {
            "ranging": ranging,
            "not_ranging": not_ranging,
            "ranging_minus_not": _two_prop(ranging, not_ranging),
            "veto_condition": veto_cond,
            "not_veto_condition": not_veto_cond,
            "veto_minus_not": _two_prop(veto_cond, not_veto_cond),
        },
        "effective_prob": {
            "lt035": eff_lt,
            "ge035": eff_ge,
            "lt035_minus_ge035": _two_prop(eff_lt, eff_ge),
        },
    }


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--symbols", default="EURUSD,USDCHF")
    ap.add_argument("--dsn", default=None)
    args = ap.parse_args()
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]

    import os
    dsn = args.dsn or os.environ.get("DATABASE_URL")
    if not dsn:
        print("Falta DATABASE_URL (o --dsn)", file=sys.stderr)
        return 2

    conn = await asyncpg.connect(dsn)
    try:
        out = {
            "measured_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "days": args.days,
            "method": "canonical_direction + skill_vs_base; n<30 = insuficiente",
            "symbols": {},
        }
        for sym in symbols:
            out["symbols"][sym] = await _measure_symbol(conn, sym, args.days)
        print(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    finally:
        await conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
