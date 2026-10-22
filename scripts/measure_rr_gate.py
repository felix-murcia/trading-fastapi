"""
H-N (2026-10-22): ¿debe el contrato rechazar SL ancho o R:R neto bajo?

POR QUÉ SE MIDE
---------------
`evaluate_trade` verifica I1 (riesgo ≤ tope), I2 (riesgo ≥ Safe-4) e I3
(TP neto ≥ Safe-4), pero NO la RELACIÓN entre TP y SL: un SL de 45 p con un
TP de 40 p (R:R neto 0,84) pasa si el lote cabe. La pregunta es de dinero:
¿filtrar esas entradas mejora la esperanza por operación o sólo reduce el
número de operaciones?

MÉTODO (sin lookahead)
----------------------
· H1 real, 30.000 velas por par (EURUSD, USDCHF), 2021-12 → 2026-10.
· Niveles confirmados con `candidates()` (swings i ≤ t−2, ventana 120,
  significancia 0,75×ATR) — la misma geometría que la variante B desplegada.
· SL: `production_sl` (suelo/techo más cercano + buffer del EA) y
  `effective_sl_pips` — exactamente lo que usa /predict.
· Lote/TP: se llama a `evaluate_trade` por señal (misma función de producción,
  no una copia): lote por riesgo con suelo Safe-4 y TP = primer nivel que paga
  ≥ 4 € NETOS. Sin candidato que pague ⇒ la señal no existe (como en vivo).
· Conversión €: `pip_value_eur_per_lot` con el EURUSD contemporáneo (no el pip
  en divisa de cotización: ese fue el error de +13,5 % de la orden 80).
· Resultado: barreras SL/TP en las 12 velas siguientes (time-stop al cierre).
  R = (tp − coste)/sl si toca TP; −1 si toca SL; (movimiento − coste)/sl en
  time-stop. Coste = 2 p (el mismo `EXEC_COST_PIPS` del contrato).
· E[R] es la esperanza POR OPERACIÓN con riesgo fijo: es el criterio correcto
  porque el sistema ya elige una sola posición por par y descarta el resto.

LECTURA
-------
Un filtro sólo se acepta si (a) el tramo rechazado tiene E[R] < 0, (b) mejora
E[R] de las conservadas, (c) conserva una fracción útil de señales y (d) el
signo se mantiene por año (estabilidad entre regímenes). El script imprime
todo eso; la decisión se registra en el informe, no aquí.
"""
import json
import os
import sys

import numpy as np
import pandas as pd

os.environ.setdefault("database_url", "postgresql://x:x@localhost/x")
os.environ.setdefault("internal_token", "t")
os.environ.setdefault("hmac_secret", "s")
os.environ.setdefault("mt5_http_url", "http://x")
sys.path.insert(0, "/home/felix/Public/n8n/fastapi")
sys.path.insert(0, "/home/felix/Public/n8n/scripts")

import compare_geometry_variants as base  # noqa: E402

from core.rules import SPREAD_BUFFER_PIPS, size_ppo_position  # noqa: E402
from ml.trading_env_v2 import (  # noqa: E402
    effective_sl_pips,
    engineer_market_features,
    production_sl,
)
from services.trade_contract import evaluate_trade, pip_value_eur_per_lot  # noqa: E402

PIP = 0.0001
H = 12
COST = SPREAD_BUFFER_PIPS
VOL_NORM = base.VOL_NORM
EQUITY, RISK_PCT = base.EQUITY, base.RISK_PCT
MIN_LOT, MAX_LOT, LOCK = 0.01, 0.50, 4.0
SYMBOLS = (
    ("EURUSD", "/tmp/opencode/eurusd_30k.json"),
    ("USDCHF", "/tmp/opencode/usdchf_30k.json"),
)


def simulate_r(df, idx, sl, tp, side):
    """R por señal: TP primero, SL primero, o time-stop al cierre de H velas."""
    h = df["high"].to_numpy(float)
    l = df["low"].to_numpy(float)
    c = df["close"].to_numpy(float)
    fut = idx[:, None] + np.arange(1, H + 1)[None, :]
    entry = c[idx]
    if side == 1:
        hit_sl = l[fut] <= (entry - sl * PIP)[:, None]
        hit_tp = h[fut] >= (entry + tp * PIP)[:, None]
    else:
        hit_sl = h[fut] >= (entry + sl * PIP)[:, None]
        hit_tp = l[fut] <= (entry - tp * PIP)[:, None]
    has_sl, has_tp = hit_sl.any(1), hit_tp.any(1)
    first_sl = np.where(has_sl, hit_sl.argmax(1), H + 1)
    first_tp = np.where(has_tp, hit_tp.argmax(1), H + 1)
    tp_wins = first_tp < first_sl          # empate en la misma vela ⇒ SL
    sl_wins = has_sl & ~tp_wins
    r_tp = (tp - COST) / sl
    r_sl = -np.ones_like(sl)
    move = (c[fut[:, -1]] - entry) * side / PIP
    r_to = (move - COST) / sl
    r = np.where(tp_wins, r_tp, np.where(sl_wins, r_sl, r_to))
    return r, tp_wins, sl_wins, (~has_sl & ~has_tp)


def build(sym, path, eurusd_map, tp_mode="levels"):
    rows = sorted(json.load(open(path)), key=lambda x: x["time"])
    df = engineer_market_features(pd.DataFrame(rows))
    t = pd.to_datetime(df["time"], utc=True)
    # TP: candidatos con la banda CORREGIDA (ATR en precio, suelo 5 pips).
    # SL: floor_below/ceiling_above tal como los consume /predict.
    fl, ce = base.candidates(df["close"], df["high"], df["low"],
                             df["atr"] * df["close"],
                             base.NEAREST_LEVEL_ATR_MULT)
    close = df["close"].to_numpy(float)
    atr_p = df["atr"].to_numpy(float) * close / PIP
    floor_col = df["floor_below"].to_numpy(float)
    ceil_col = df["ceiling_above"].to_numpy(float)
    n = len(df)
    per_side = {1: [], -1: []}
    for i in range(n - H):
        px = close[i]
        f0 = float(floor_col[i]) if np.isfinite(floor_col[i]) else None
        c0 = float(ceil_col[i]) if np.isfinite(ceil_col[i]) else None
        for side, levels in ((1, ce[i]), (-1, fl[i])):
            sl_geom = production_sl(
                float(df["atr"].iat[i]), PIP, direction=side,
                floor_below=f0, ceiling_above=c0, current_price=px,
            )
            sl = effective_sl_pips(sl_geom)
            if tp_mode == "2xsl":
                dists = [2.0 * sl]    # lo que sirve HOY (bug de unidades)
            else:
                dists = []
                for x in levels:
                    if not np.isfinite(x):
                        continue
                    d = abs(float(x) - px) / PIP
                    if d >= 5.0:      # MIN_LEVEL_PIPS (misma regla que el TP)
                        dists.append(d)
                if not dists:
                    dists = [2.0 * sl]  # fallback declarado del serve
            eurusd_ref = None if sym == "EURUSD" else eurusd_map.get(t.iat[i])
            if sym != "EURUSD" and eurusd_ref is None:
                continue
            ppl = pip_value_eur_per_lot(sym, px, eurusd_price=eurusd_ref)
            head = size_ppo_position(
                volume_norm=VOL_NORM, sl_pips=sl, equity=EQUITY,
                pip_value=ppl, risk_pct=RISK_PCT,
                max_lot=MAX_LOT, min_lot=MIN_LOT,
            )
            c = evaluate_trade(
                symbol=sym, price=px, equity=EQUITY, sl_pips=sl,
                head_lots=head, tp_candidates_pips=dists,
                risk_pct=RISK_PCT, min_lot=MIN_LOT, max_lot=MAX_LOT,
                min_lock_eur=LOCK, eurusd_price=eurusd_ref,
            )
            if not c.ok:
                continue
            per_side[side].append((i, c.sl_pips, c.tp_pips, c.lots, atr_p[i]))
    chunks = []
    for side in (1, -1):
        arr = np.asarray(per_side[side], dtype=float)
        if len(arr) == 0:
            continue
        idx = arr[:, 0].astype(int)
        sl, tp, lot, atr = arr[:, 1], arr[:, 2], arr[:, 3], arr[:, 4]
        r, tw, sw, tow = simulate_r(df, idx, sl, tp, side)
        chunks.append({
            "idx": idx, "side": np.full(len(idx), side), "sl": sl, "tp": tp,
            "lot": lot, "atr_p": atr, "r": r, "tp_w": tw, "sl_w": sw, "to_w": tow,
        })
    out = {k: np.concatenate([c[k] for c in chunks]) for k in chunks[0]}
    out["year"] = t.iloc[out["idx"]].dt.year.to_numpy()
    out["month"] = t.iloc[out["idx"]].dt.strftime("%Y-%m").to_numpy()
    out["rr"] = (out["tp"] - COST) / out["sl"]
    out["sl_atr"] = out["sl"] / np.maximum(out["atr_p"], 1e-9)
    return out, t


def summarize(r, tp_w, sl_w):
    n = len(r)
    if n == 0:
        return "n=0"
    return (f"n={n:5d} · E[R] {r.mean():+.3f} · total {r.sum():+8.1f} R · "
            f"P(TP) {tp_w.mean() * 100:3.0f}% · P(SL) {sl_w.mean() * 100:3.0f}%")


def monthly_t(r, month):
    s = pd.Series(r).groupby(month).agg(["mean", "size"])
    s = s[s["size"] >= 5]
    if len(s) < 6:
        return float("nan"), len(s)
    return float(s["mean"].mean() / (s["mean"].std(ddof=1) / np.sqrt(len(s)))), len(s)


def table(d, tag):
    print(f"  {tag}")
    print(f"    {summarize(d['r'], d['tp_w'], d['sl_w'])}")


def filters(d, tag):
    print(f"  Filtros sobre {tag}:")
    print("    regla                conserva   E[R]     ΔE[R]   total R   t(mes)")
    base_r = d["r"]
    for kind, vals in (
        ("R:R neto ≥", (0.75, 1.00, 1.25, 1.50)),
        ("SL/ATR ≤", (1.50, 2.00, 2.25, 2.50, 3.00)),
    ):
        for v in vals:
            keep = d["rr"] >= v if kind.startswith("R:R") else d["sl_atr"] <= v
            if keep.sum() == 0:
                continue
            t, m = monthly_t(d["r"][keep], d["month"][keep])
            print(f"    {kind} {v:4.2f}        {keep.mean() * 100:6.1f}%  "
                  f"{d['r'][keep].mean():+7.3f}  {d['r'][keep].mean() - base_r.mean():+7.3f}  "
                  f"{d['r'][keep].sum():+8.1f}  {t:+5.1f} ({m}m)")
    # combinación de los dos gates con mejor pinta (se imprime por completitud)
    for rr_min, sl_max in ((1.00, 2.00), (1.00, 2.50), (1.25, 2.50)):
        keep = (d["rr"] >= rr_min) & (d["sl_atr"] <= sl_max)
        if keep.sum() == 0:
            continue
        t, m = monthly_t(d["r"][keep], d["month"][keep])
        print(f"    R:R≥{rr_min:.2f} & SL/ATR≤{sl_max:.2f}  {keep.mean() * 100:6.1f}%  "
              f"{d['r'][keep].mean():+7.3f}  {d['r'][keep].mean() - base_r.mean():+7.3f}  "
              f"{d['r'][keep].sum():+8.1f}  {t:+5.1f} ({m}m)")


def buckets(d, key, cuts, label):
    print(f"  Por {label}:")
    print("    tramo              n     E[R]    P(TP)   total R")
    edges = [-np.inf, *cuts, np.inf]
    for lo, hi in zip(edges[:-1], edges[1:]):
        m = (d[key] >= lo) & (d[key] < hi) if np.isfinite(hi) else (d[key] >= lo)
        if not m.any():
            continue
        print(f"    [{lo:5.2f},{hi:5.2f})  {m.sum():5d}  {d['r'][m].mean():+7.3f}  "
              f"{d['tp_w'][m].mean() * 100:5.0f}%  {d['r'][m].sum():+8.1f}")


def main():
    eurusd = sorted(json.load(open(SYMBOLS[0][1])), key=lambda x: x["time"])
    eurusd_map = {pd.Timestamp(r["time"]): float(r["close"]) for r in eurusd}
    pooled = []
    for sym, path in SYMBOLS:
        d, t = build(sym, path, eurusd_map, "levels")
        d_old, _ = build(sym, path, eurusd_map, "2xsl")
        print("=" * 118)
        print(f"=== {sym} · geometría: niveles corregidos vs 2×SL (lo que sirve hoy) ===")
        table(d_old, "2×SL (actual)")
        table(d, "niveles B (corregido)")
        print(f"=== {sym} · {len(t):,} velas H1 · {t.iloc[0]:%Y-%m-%d} → {t.iloc[-1]:%Y-%m-%d} "
              f"· equity {EQUITY:.2f} € · riesgo {EQUITY * RISK_PCT:.2f} € ===")
        print("  Señales factibles (contrato I1/I2/I3):")
        print(f"    SL med {np.median(d['sl']):5.1f} p · TP med {np.median(d['tp']):5.1f} p · "
              f"R:R neto med {np.median(d['rr']):4.2f} · SL/ATR med {np.median(d['sl_atr']):4.2f} · "
              f"R:R<1 {np.mean(d['rr'] < 1) * 100:4.1f}% · SL>2×ATR {np.mean(d['sl_atr'] > 2) * 100:4.1f}%")
        buckets(d, "rr", (0.75, 1.00, 1.25, 1.50, 2.00), "R:R neto")
        buckets(d, "sl_atr", (1.25, 1.75, 2.25, 3.00), "SL/ATR")
        filters(d, "todas")
        print("  Por año (señales conservadas):")
        for year in sorted(set(d["year"])):
            m = d["year"] == year
            print(f"    {year}: {summarize(d['r'][m], d['tp_w'][m], d['sl_w'][m])}")
        pooled.append(d)
    print("=" * 118)
    d = {k: np.concatenate([x[k] for x in pooled]) for k in pooled[0]}
    print("=== POOL EURUSD+USDCHF ===")
    table(d, "baseline")
    filters(d, "pool")
    print("  Por año (pool):")
    for year in sorted(set(d["year"])):
        m = d["year"] == year
        print(f"    {year}: {summarize(d['r'][m], d['tp_w'][m], d['sl_w'][m])}")


if __name__ == "__main__":
    main()
