#!/usr/bin/env python3
"""
MEDICIÓN GBPUSD — implementa el pre-registro
`fastapi/ops/2026-10-21-pre-registro-gbpusd.md` (2026-10-21).

No acepta parámetros que cambien las reglas, la muestra, el coste ni la
división descubrimiento/validación: eso es el contrato. Si se cambia algo,
se escribe una enmienda fechada en el pre-registro y se vuelve a medir.

Uso:
    DATABASE_URL=... INTERNAL_TOKEN=... HMAC_SECRET=... \
    MT5_HTTP_URL=http://... PYTHONPATH=. \
    python scripts/measure_gbpusd.py
"""
import asyncio
import bisect
import datetime as dt
import json
import math
import statistics
import time

from services.measurement import canonical_direction, skill_vs_base, wilson_lower_bound

SYMBOL = "GBPUSD"
COUNT = 1999
STEP = 24            # ventanas NO solapadas
HORIZON_H = 24
COST_PIPS = 1.5      # ida y vuelta, conservador
Z_BONFERRONI = 2.128  # una cola 98,33 % (0,05 / 3 reglas)
MIN_N = 30
MAX_SKILL_DRIFT_PP = 10.0


def _pip(symbol: str) -> float:
    return 0.0001


def _wilson_skill(hits: int, n: int, base: float) -> dict:
    lo = wilson_lower_bound(hits, n, z=Z_BONFERRONI) if n else None
    return {
        "n": n,
        "hits": hits,
        "acc": round(hits / n, 4) if n else None,
        "base": round(base, 4) if base is not None else None,
        "skill_pp": round((hits / n - base) * 100, 2) if n and base is not None else None,
        "wilson_lo_98_33": round(lo, 4) if lo is not None else None,
        "skill_lo_pp": round((lo - base) * 100, 2) if lo is not None and base is not None else None,
        "insufficient": n < MIN_N,
    }


def _stats(rows: list[dict]) -> dict:
    n = len(rows)
    if not n:
        return {"n": 0, "note": "sin señales"}
    hits = sum(1 for r in rows if r["hit"])
    dirs = [r["realized"] for r in rows]
    sk = skill_vs_base(hits, n, dirs)
    base = sk.get("base")
    out = _wilson_skill(hits, n, base)
    out["skill_vs_base"] = sk
    pips = [r["net_pips"] for r in rows]
    mean = statistics.mean(pips)
    sd = statistics.stdev(pips) if n > 1 else 0.0
    ci = 1.96 * sd / math.sqrt(n) if n > 1 else None
    out["net_pips_mean"] = round(mean, 2)
    out["net_pips_ci95"] = [round(mean - ci, 2), round(mean + ci, 2)] if ci is not None else None
    out["gross_pips_mean"] = round(statistics.mean(r["gross_pips"] for r in rows), 2)
    out["long"] = sum(1 for r in rows if r["dir"] == 1)
    out["short"] = sum(1 for r in rows if r["dir"] == -1)
    return out


def _rule_mom24(bars: list[dict], i: int) -> int | None:
    c, prev = bars[i]["close"], bars[i - 24]["close"]
    if c == prev:
        return None
    return 1 if c > prev else -1


def _rule_rev24(bars: list[dict], i: int) -> int | None:
    c, prev = bars[i]["close"], bars[i - 24]["close"]
    if c == prev:
        return None
    return -1 if c > prev else 1


def _rule_brk24(bars: list[dict], i: int) -> int | None:
    window = bars[i - 24:i]
    hi = max(b["high"] for b in window)
    lo = min(b["low"] for b in window)
    c = bars[i]["close"]
    if c > hi:
        return 1
    if c < lo:
        return -1
    return None


RULES = {
    "MOM24": _rule_mom24,
    "REV24": _rule_rev24,
    "BRK24": _rule_brk24,
}


async def main(market_port=None) -> int:
    if market_port is None:
        from core.container import build_registry_from_settings
        market_port = build_registry_from_settings().get(SYMBOL).market
    raw, offset_h = await market_port.get_candles_with_offset(SYMBOL, "H1", COUNT)
    now = time.time()
    pairs = sorted(
        (float(c["time"]), float(c["open"]), float(c["high"]), float(c["low"]), float(c["close"]))
        for c in raw
        if float(c["time"]) + 3600 <= now
    )
    bars = [
        {"time": t, "open": o, "high": h, "low": l, "close": c}
        for (t, o, h, l, c) in pairs
    ]
    bar_t = [b["time"] for b in bars]
    bar_c = [b["close"] for b in bars]

    # índice de decisión: i (cierre de la vela i), cada 24 velas.
    # t0 = apertura de i+1 − 1 s ⇒ la base canónica es la vela i (sin mirar
    # el cierre de i+1, que aún no existía al decidir).
    samples_by_rule: dict[str, list[dict]] = {name: [] for name in RULES}
    for i in range(24, len(bars) - 24, STEP):
        t0 = bar_t[i + 1] - 1.0
        realized = canonical_direction(bar_t, bar_c, t0, now, horizon_h=HORIZON_H)
        if realized is None:
            continue
        # base y terminal canónicos: último cierre <= t0 y <= t0+24h
        # (se recalculan aquí sólo para el retorno; la dirección es la canónica)
        i0 = bisect.bisect_right(bar_t, t0) - 1
        i1 = bisect.bisect_right(bar_t, t0 + HORIZON_H * 3600) - 1
        if i0 < 0 or i1 >= len(bar_c) or i1 <= i0:
            continue
        change = bar_c[i1] - bar_c[i0]
        for name, fn in RULES.items():
            d = fn(bars, i)
            if d is None:
                continue
            gross = d * change / _pip(SYMBOL)
            samples_by_rule[name].append({
                "t0": t0,
                "dir": d,
                "realized": realized,
                "hit": realized == d,
                "gross_pips": gross,
                "net_pips": gross - COST_PIPS,
                "i": i,
            })

    out = {
        "measured_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "symbol": SYMBOL,
        "pre_registration": "fastapi/ops/2026-10-21-pre-registro-gbpusd.md",
        "data": {
            "candles": len(bars),
            "from": dt.datetime.fromtimestamp(bar_t[0], dt.timezone.utc).isoformat() if bar_t else None,
            "to": dt.datetime.fromtimestamp(bar_t[-1], dt.timezone.utc).isoformat() if bar_t else None,
            "offset_h": offset_h,
            "step": STEP,
            "cost_pips": COST_PIPS,
            "z_bonferroni": Z_BONFERRONI,
        },
        "rules": {},
    }
    decisions = []
    for name, rows in samples_by_rule.items():
        rows.sort(key=lambda r: r["t0"])
        n = len(rows)
        cut = int(n * 0.7)
        disc, val = rows[:cut], rows[cut:]
        d = _stats(disc)
        v = _stats(val)
        # criterios congelados
        pass_disc = (
            d.get("n", 0) >= MIN_N
            and (d.get("skill_pp") or 0) > 0
            and (d.get("skill_lo_pp") or -999) > 0
        )
        pass_val = (
            v.get("n", 0) >= MIN_N
            and (v.get("skill_pp") or 0) > 0
            and (v.get("net_pips_mean") or 0) > 0
        )
        drift = None
        if d.get("skill_pp") is not None and v.get("skill_pp") is not None:
            drift = round(abs(v["skill_pp"] - d["skill_pp"]), 2)
        stable = drift is not None and drift <= MAX_SKILL_DRIFT_PP
        passed = bool(pass_disc and pass_val and stable)
        out["rules"][name] = {
            "samples": n,
            "split": {"discovery": len(disc), "validation": len(val)},
            "discovery": d,
            "validation": v,
            "drift_pp": drift,
            "pass_discovery": pass_disc,
            "pass_validation": pass_val,
            "stable": stable,
            "passed": passed,
        }
        decisions.append((name, passed))

    passed_rules = [name for name, ok in decisions if ok]
    out["decision"] = {
        "passed_rules": passed_rules,
        "gbpusd": "SHADOW" if passed_rules else "DORMIDO",
        "note": (
            "Si pasa: sombra 30 días, sin capital; canario sólo tras repetir skill."
            if passed_rules else
            "Ninguna regla cumple el contrato: no se activa, no se prueba una cuarta en la misma muestra."
        ),
    }
    print(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
