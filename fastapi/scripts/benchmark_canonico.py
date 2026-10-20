#!/usr/bin/env python3
"""
L2 · EL BENCHMARK (la vara) — medición CANÓNICA (services/measurement).

Mide los predictores triviales con la MISMA definición que usa la autoridad
del veto y que usará el ledger:

    base_i = última vela con time <= t0
    term_i = PRIMERA vela con time >= t0 + 24 h   (determinista)
    evaluable ⇔ el terminal existe y está CERRADA

Se evalúa en CADA hora de la serie (miles de muestras ⇒ estable) y también
en los instantes reales de los ciclos del sistema (la muestra de las fuentes).

Uso:
    python3 benchmark_canonico.py [días] [--ciclos]
"""
import asyncio
import bisect
import datetime as dt
import os
import sys

import httpx

sys.path.insert(0, "/app")
from services.measurement import (
    benchmark_predictions,
    canonical_direction,
    skill_of,
    wilson_lower_bound,
)

MT5 = os.environ.get("MT5_HTTP_URL", "http://host.docker.internal:8080").rstrip("/")
SYMBOL = "EURUSD"


async def candles(count=1200):
    async with httpx.AsyncClient(timeout=60.0) as c:
        r = await c.get(
            f"{MT5}/api/v1/market/candles/latest",
            params={"symbol_name": SYMBOL, "timeframe": "H1", "count": count},
        )
        r.raise_for_status()
        data = r.json()
    raw = data if isinstance(data, list) else data.get("candles", [])

    def ts(x):
        if isinstance(x, (int, float)):
            return float(x)
        return dt.datetime.fromisoformat(str(x).replace("Z", "+00:00")).timestamp()

    return sorted((ts(x["time"]), float(x["close"])) for x in raw)


async def main():
    dias = 30
    only_cycles = False
    for a in sys.argv[1:]:
        if a == "--ciclos":
            only_cycles = True
        elif a.isdigit():
            dias = int(a)

    pairs = await candles()
    bt = [p[0] for p in pairs]
    bc = [p[1] for p in pairs]
    now = dt.datetime.now(dt.timezone.utc).timestamp()
    print(f"serie: {len(bt)} velas H1  "
          f"{dt.datetime.fromtimestamp(bt[0], dt.timezone.utc):%Y-%m-%d %H:%M} → "
          f"{dt.datetime.fromtimestamp(bt[-1], dt.timezone.utc):%Y-%m-%d %H:%M}")

    # ── 1) los triviales, evaluados en cada hora de la serie ──────────────
    since = now - dias * 86400
    res: dict = {}
    n_eval = 0
    for i in range(0, len(bt)):
        t0 = bt[i]
        if t0 < since:
            continue
        d = canonical_direction(bt, bc, t0, now)
        if d is None:
            continue
        n_eval += 1
        up = d > 0
        for name, pred in benchmark_predictions(bt, bc, t0, now).items():
            res.setdefault(name, [0, 0])
            res[name][0] += 1 if ((pred > 0) == up) else 0
            res[name][1] += 1

    print()
    print("=" * 74)
    print(f"BENCHMARK (canónico, +24 h) — {dias} días, evaluado cada hora "
          f"({n_eval} instantes evaluables)")
    print("=" * 74)
    for name in ("sma200", "mom24", "barra_previa"):
        if name not in res:
            continue
        h, n = res[name]
        lo = wilson_lower_bound(h, n)
        sk = skill_of(h, n)
        print(f"  {name:14s}: {h:5d}/{n:5d} = {h/n*100:5.1f} %   "
              f"CI95% inf={lo*100:5.1f} %   "
              f"{'★ DISTINGUE DEL AZAR' if sk['granted'] else 'no distingue del azar'}")

    # ── 2) en los instantes REALES de los ciclos del sistema ─────────────
    if not only_cycles:
        from db.connection import init_pool, get_pool
        await init_pool()
        pool = get_pool()
        rows = await pool.fetch(
            """
            SELECT (data->'data'->>'mt5_time')::float AS t,
                   data->'data'->>'llm_bias' AS b,
                   (data->'data'->>'llm_bias_prob')::float AS p,
                   data->'data'->>'ppo_direction' AS ppo
            FROM audit_log WHERE event='ai_predict_cycle'
              AND data->'data'->>'symbol'=$1
              AND data->'data'->>'llm_bias' IN ('BULLISH','BEARISH')
              AND COALESCE(data->'data'->>'qwen_skipped','false')='false'
              AND COALESCE(data->'data'->>'qwen_degraded','false')='false'
              AND (data->'data'->>'mt5_time')::float IS NOT NULL
            ORDER BY created_at
            """, SYMBOL)
        # el mt5_time viene en el reloj del BROKER: se alinea con la serie
        # (la vela más nueva = floor(hora real) + offset ⇒ el desfase se deduce)
        from services.market_time import detect_utc_offset_hours
        newest = dt.datetime.fromtimestamp(bt[-1], dt.timezone.utc)
        offset_h = detect_utc_offset_hours(newest, dt.datetime.now(dt.timezone.utc))

        res2: dict = {}
        for r in rows:
            t0 = float(r["t"]) - offset_h * 3600
            d = canonical_direction(bt, bc, t0, now)
            if d is None:
                continue
            up = d > 0
            if r["ppo"] in ("BUY", "SELL"):
                res2.setdefault("PPO", [0, 0])
                res2["PPO"][0] += 1 if ((r["ppo"] == "BUY") == up) else 0
                res2["PPO"][1] += 1
            if r["p"] is not None:
                hit = (r["b"] == "BULLISH" and up) or (r["b"] == "BEARISH" and not up)
                res2.setdefault("LLM todas", [0, 0])
                res2["LLM todas"][0] += hit; res2["LLM todas"][1] += 1
                if abs(float(r["p"]) - 0.5) >= 0.2:
                    res2.setdefault("LLM alta conf", [0, 0])
                    res2["LLM alta conf"][0] += hit; res2["LLM alta conf"][1] += 1
        print()
        print("=" * 74)
        print("EN LOS CICLOS REALES DEL SISTEMA (muestra madura, +24 h)")
        print("=" * 74)
        for name in ("PPO", "LLM alta conf", "LLM todas"):
            if name not in res2:
                print(f"  {name:14s}: sin muestra madura (n=0) — la skill NO es medible aún")
                continue
            h, n = res2[name]
            lo = wilson_lower_bound(h, n)
            sk = skill_of(h, n)
            print(f"  {name:14s}: {h:5d}/{n:5d} = {h/n*100:5.1f} %   "
                  f"CI95% inf={(lo*100 if lo is not None else 0):5.1f} %   "
                  f"{'★' if sk['granted'] else 'no distingue del azar'}")


if __name__ == "__main__":
    asyncio.run(main())
