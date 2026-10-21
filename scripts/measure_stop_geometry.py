"""
MEDICIÓN DE LA GEOMETRÍA DE STOPS (2026-10-21) — reproducible.

Contesta: ¿qué distancia de SL/TP es coherente con el horizonte de 12 velas
H1 (TIME_STOP_BARS) y con el money model del sistema (riesgo fraccional)?

Uso:
    python scripts/measure_stop_geometry.py [--pairs EURUSD,USDCHF] [--window 30000]

Método (sin lookahead):
  · el mismo motor de features del sistema (`engineer_market_features`) ⇒
    los niveles (suelo/techo más cercanos SIGNIFICATIVOS) son los que usa
    producción, no una reimplementación;
  · swings CONFIRMADOS (un swing de la vela i se conoce en i+2);
  · simulación de 12 velas con prioridad SL intra-vela (convención del env);
  · dos ventanas DISJUNTAS (TREN 2021-12→2024-12, TEST 2025-01→2026-10) para
    que el óptimo no sea un artefacto de la muestra;
  · E[€] con el modelo real: lot = riesgo_€ / (distancia × pip_value) ⇒ la
    Reward en euros = riesgo_€ × (TP/SL) en TP, −riesgo_€ en SL y el MARK
    real en el timeout (el timeout NO es 0: la banda es asimétrica).

Salida: distancias, P(TP)/P(SL)/P(timeout), E[€], tiempo medio, lote y si
el trailing "Safe-4" del EA (riesgo ≥ 4 €) queda VIVO.
"""
import argparse
import json
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "fastapi"))
os.environ.setdefault("database_url", "postgresql://x:x@localhost/x")
os.environ.setdefault("internal_token", "t")
os.environ.setdefault("hmac_secret", "s")
os.environ.setdefault("mt5_http_url", "http://x")

from ml.trading_env_v2 import (  # noqa: E402
    effective_sl_pips,
    engineer_market_features,
    production_stops,
)
from services.position_sizing import pip_value_per_lot, size_ppo_position  # noqa: E402

H = 12               # TIME_STOP_BARS (velas H1)
EQUITY = 491.64
RISK_PCT = 0.015
SAFE_LOCK_EUR = 4.0
MIN_WIN_EUR = 3.0
WINDOWS = (("TREN", "2021-12-01", "2025-01-01"), ("TEST", "2025-01-01", "2027-01-01"))
OLD_GEOM = (1.5, 3.0)   # geometría anterior: SL = 1,5×ATR, TP = 2×SL


def load(path):
    rows = sorted(json.load(open(path)), key=lambda r: r["time"])
    return pd.DataFrame(rows)


def simulate(df, direction, sl_pips_arr, tp_pips_arr, idx):
    h = df["high"].to_numpy(); l = df["low"].to_numpy(); c = df["close"].to_numpy()
    fut = idx[:, None] + np.arange(1, H + 1)[None, :]
    lows, highs, close_h = l[fut], h[fut], c[fut]
    entry = c[idx]
    tp = sl = to = 0
    pnl = 0.0
    bars = 0
    for d in (1, -1):
        if d == 1:
            sl_t = lows <= (entry - sl_pips_arr * 0.0001)[:, None]
            tp_t = highs >= (entry + tp_pips_arr * 0.0001)[:, None]
        else:
            sl_t = highs >= (entry + sl_pips_arr * 0.0001)[:, None]
            tp_t = lows <= (entry - tp_pips_arr * 0.0001)[:, None]
        has_sl, has_tp = sl_t.any(1), tp_t.any(1)
        fs = np.where(has_sl, sl_t.argmax(1), H + 1)
        ft = np.where(has_tp, tp_t.argmax(1), H + 1)
        sl_w = fs <= ft
        res = has_sl | has_tp
        exit_px = np.where(
            has_tp & ~sl_w, entry + d * tp_pips_arr * 0.0001,
            np.where(has_sl, entry - d * sl_pips_arr * 0.0001, close_h[:, -1]),
        )
        pnl_r = np.where(has_tp & ~sl_w, tp_pips_arr / sl_pips_arr,
                         np.where(has_sl, -1.0,
                                  (exit_px - entry) * d / (sl_pips_arr * 0.0001)))
        tp += int((has_tp & ~sl_w).sum()); sl += int(has_sl.sum()); to += int((~res).sum())
        pnl += pnl_r.sum()
        bars += np.where(sl_w, fs, ft).clip(max=H).sum()
    n = tp + sl + to
    return {"p_tp": tp / n, "p_sl": sl / n, "p_to": to / n, "e_r": pnl / n,
            "horas": bars / n, "n": n}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--eurusd", default="/tmp/opencode/eurusd_30k.json")
    ap.add_argument("--usdchf", default="/tmp/opencode/usdchf_30k.json")
    args = ap.parse_args()
    for sym, path, price in (("EURUSD", args.eurusd, 1.12391),
                             ("USDCHF", args.usdchf, 0.82476)):
        df = load(path)
        feats = engineer_market_features(df)
        t = pd.to_datetime(feats["time"], utc=True)
        atr = feats["atr"].to_numpy()
        floor = feats["floor_below"].to_numpy()
        ceil = feats["ceiling_above"].to_numpy()
        usable = (floor > 0) & (ceil > 0) & np.isfinite(atr) & (atr > 0)
        print("=" * 96)
        print(f"=== {sym} · {len(feats)} velas H1 · niveles utilizables "
              f"{usable.mean()*100:.1f}% ===")
        ppv = pip_value_per_lot(sym, price)
        risk = EQUITY * RISK_PCT
        for name, lo, hi in WINDOWS:
            m = (t >= lo) & (t < hi) & usable
            idx = np.where(m)[0]
            idx = idx[(idx + H) < len(feats)]
            if len(idx) == 0:
                continue
            entry = feats["close"].to_numpy()[idx]
            # geometría NUEVA (suelo/techo más cercano, con el buffer del EA)
            sl_new, tp_new = [], []
            for i, e in zip(idx, entry):
                s, tp_ = production_stops(
                    atr[i], 0.0001, direction=1, floor_below=float(floor[i]) or None,
                    ceiling_above=float(ceil[i]) or None, current_price=float(e),
                )
                sl_new.append(effective_sl_pips(s)); tp_new.append(tp_)
            sl_new = np.array(sl_new); tp_new = np.array(tp_new)
            # geometría VIEJA (SL = 1,5×ATR, TP = 2×SL) con el mismo buffer
            sl_old = np.array([effective_sl_pips(max(10.0, min(50.0, atr[i] / 0.0001 * 1.5)))
                               for i in idx])
            tp_old = sl_old * 2.0
            for tag, (s_arr, t_arr) in (("VIEJA 1.5xATR/2x", (sl_old, tp_old)),
                                        ("NUEVA suelo/techo", (sl_new, tp_new))):
                r = simulate(feats, 1, s_arr, t_arr, idx)
                lot = size_ppo_position(volume_norm=0.65, sl_pips=float(np.median(s_arr)),
                                        equity=EQUITY, pip_value=ppv, risk_pct=RISK_PCT,
                                        max_lot=0.5, min_lot=0.01)
                eur_risk = lot * float(np.median(s_arr)) * ppv / price
                eur_tp = lot * float(np.median(t_arr)) * ppv / price
                print(f"  {name:5s} {tag:20s}: SL {np.median(s_arr):5.1f}p "
                      f"TP {np.median(t_arr):5.1f}p R:R {np.median(t_arr)/np.median(s_arr):4.2f} | "
                      f"P(TP) {r['p_tp']*100:3.0f}% P(SL) {r['p_sl']*100:3.0f}% "
                      f"P(to) {r['p_to']*100:3.0f}% E[R] {r['e_r']:+.3f} "
                      f"E[€] {r['e_r']*risk:+.3f} | t {r['horas']:4.1f}h | "
                      f"lote {lot:.2f} riesgo {eur_risk:5.2f}€ TP {eur_tp:5.2f}€ "
                      f"Safe-4 {'VIVO' if eur_risk >= SAFE_LOCK_EUR else 'APAGADO'} "
                      f"regla3€ {'OK' if eur_tp >= MIN_WIN_EUR else 'NO'}")


if __name__ == "__main__":
    main()
