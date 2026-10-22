"""
DECISIÓN: ¿qué variante da MÁS BENEFICIO? (2026-10-22) — v2 CORREGIDA.

Correcciones sobre v1 (que Lawnumbers ling:
  1) el ATR del df es RELATIVO (TR/close): para compararlo con pips hay que
     multiplicarlo por el precio (ATR en pips = atr_rel × price / pip);
  2) la geometría es ESPECÍFICA de la dirección: para un LONG el SL va al
     suelo y el TP al techo; para un SHORT, al revés (v1 usaba las mismas
     distancias en los dos lados ⇒ medía una geometría que no existe);
  3) la tasa de señales es la que PASA el gate de recompensa (€ al TP ≥ 3 €),
     no "hay SL y TP" (el fallback siempre los da).

Variantes:
  A (actual)  TP = techo más cercano; si falta un nivel ⇒ fallback ATR COMPLETO.
  B           TP = techo más cercano QUE PAGA (≥ MIN_WIN_EUR al lote elegido).
  C           FALLBACK POR LADO: cada lado usa su nivel si existe; si no, ATR.
  D           B + C.

Criterio: E[€]/trade = riesgo_€ × [E[R]_geometría + e/(SL en ATR)], donde e es
el movimiento favorable esperado del EDGE (en ATR) — el edge vale menos en R si
el stop es más ancho (el lote baja). Se evalúa e ∈ {0, 0,5, 1,0} ATR.
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

from ml.trading_env_v2 import (  # noqa: E402
    NEAREST_LEVEL_ATR_MULT,
    engineer_market_features,
)
from core.rules import SPREAD_BUFFER_PIPS, size_ppo_position
from services.position_sizing import pip_value_per_lot  # noqa: E402

H = 12
EQUITY, RISK_PCT = 489.49, 0.02
SAFE_LOCK_EUR, MIN_WIN_EUR = 4.0, 3.0
WIN = (("TREN", "2021-12-01", "2025-01-01"), ("TEST", "2025-01-01", "2027-01-01"))
EDGE_ATR = (0.0, 0.5, 1.0)
VOL_NORM = 0.65
MAX_C = 6


def _shift(a, k):
    n = len(a)
    o = np.full(n, np.nan)
    if k > 0:
        o[k:] = a[:-k]
    else:
        o[:k] = a[-k:]
    return o


def candidates(close, high, low, atr, mult, window=120):
    """Techos (arriba, de más cerca a más lejos) y suelos (abajo, idem)."""
    n = len(close)
    cv = close.to_numpy(float); hv = high.to_numpy(float); lv = low.to_numpy(float)
    av = atr.to_numpy(float)
    sh = (hv > _shift(hv, 1)) & (hv > _shift(hv, 2)) & (hv > _shift(hv, -1)) & (hv > _shift(hv, -2))
    sl = (lv < _shift(lv, 1)) & (lv < _shift(lv, 2)) & (lv < _shift(lv, -1)) & (lv < _shift(lv, -2))
    hi_i, lo_i = np.where(sh)[0], np.where(sl)[0]
    ceil = np.full((n, MAX_C), np.nan)
    floor = np.full((n, MAX_C), np.nan)
    for t in range(n):
        a = av[t]
        if not np.isfinite(a) or a <= 0:
            continue
        band = a * mult
        ci = hi_i[(hi_i <= t - 2) & (hi_i >= t - 2 - window)]
        if len(ci):
            v = np.sort(hv[ci][(hv[ci] > cv[t]) & ((hv[ci] - cv[t]) >= band)])
            if len(v):
                ceil[t, :min(len(v), MAX_C)] = v[:min(len(v), MAX_C)]
        li = lo_i[(lo_i <= t - 2) & (lo_i >= t - 2 - window)]
        if len(li):
            v = np.sort(lv[li][(lv[li] < cv[t]) & ((cv[t] - lv[li]) >= band)])[::-1]
            if len(v):
                floor[t, :min(len(v), MAX_C)] = v[:min(len(v), MAX_C)]
    return floor, ceil


def lot_for(sl_pips, price, ppv):
    """Lote con el suelo de protección Safe-4 (el EA necesita ≥ 4 € de riesgo)."""
    l = size_ppo_position(volume_norm=VOL_NORM, sl_pips=sl_pips, equity=EQUITY,
                          pip_value=ppv, risk_pct=RISK_PCT, max_lot=0.5, min_lot=0.01)
    if l <= 0:
        return 0.0
    eur_pip = ppv / price
    need = SAFE_LOCK_EUR / (sl_pips * eur_pip)
    lock = np.ceil(need / 0.01 - 1e-9) * 0.01
    if lock > l and (lock * sl_pips * eur_pip) <= EQUITY * RISK_PCT:
        l = lock
    return float(l)


def geometry(df, fl, ce, variant, price, ppv):
    """(sl_pips, tp_pips, lot) por (barra, dirección) — LONG idx 0, SHORT idx 1."""
    n = len(df)
    atr_p = df["atr"].to_numpy() * df["close"].to_numpy() / 0.0001   # ATR en pips
    c = df["close"].to_numpy()
    sl = np.full((n, 2), np.nan)
    tp = np.full((n, 2), np.nan)
    lot = np.zeros((n, 2))
    atr_sl = np.clip(atr_p * 1.5, 10, 50)
    for i in range(n):
        entry = c[i]
        floors = fl[i]; ceils = ce[i]
        floors = floors[np.isfinite(floors)]
        ceils = ceils[np.isfinite(ceils)]
        for d, side in ((0, 1), (1, -1)):    # 0 = LONG, 1 = SHORT
            # SL: LONG → suelo; SHORT → techo
            stop_levels = floors if side == 1 else ceils
            target_levels = ceils if side == 1 else floors
            if len(stop_levels):
                lvl = stop_levels[0]
                px = lvl - SPREAD_BUFFER_PIPS * 0.0001 if side == 1 else lvl + SPREAD_BUFFER_PIPS * 0.0001
                sl_p = abs(entry - px) / 0.0001
            else:
                sl_p = np.nan
            # TP: LONG → techo; SHORT → suelo
            tp_p = np.nan
            if len(target_levels):
                if variant in ("A", "C"):
                    tp_p = abs(target_levels[0] - entry) / 0.0001
                else:
                    l_tmp = lot_for(sl_p if np.isfinite(sl_p) else atr_sl[i], price, ppv)
                    for lvl in target_levels:
                        d_p = abs(lvl - entry) / 0.0001
                        if l_tmp * d_p * ppv / price >= MIN_WIN_EUR:
                            tp_p = d_p
                            break
            if variant in ("A", "B"):
                if not (np.isfinite(sl_p) and np.isfinite(tp_p)):
                    sl_p = atr_sl[i]; tp_p = 2 * sl_p
            else:
                if not np.isfinite(sl_p):
                    sl_p = atr_sl[i]
                if not np.isfinite(tp_p):
                    tp_p = 2 * sl_p
            sl[i, d] = np.clip(sl_p, 10, 50)
            tp[i, d] = np.clip(tp_p, 10, 200)
            lot[i, d] = lot_for(sl[i, d], price, ppv)
    return sl, tp, lot, atr_p


def simulate(df, sl_p, tp_p, idx, direction):
    h = df["high"].to_numpy(); l = df["low"].to_numpy(); c = df["close"].to_numpy()
    fut = idx[:, None] + np.arange(1, H + 1)[None, :]
    lows, highs, close_h = l[fut], h[fut], c[fut]
    entry = c[idx]
    tp = sl = to = 0
    pnl = 0.0
    bars = 0
    if direction == 1:
        sl_t = lows <= (entry - sl_p * 0.0001)[:, None]
        tp_t = highs >= (entry + tp_p * 0.0001)[:, None]
    else:
        sl_t = highs >= (entry + sl_p * 0.0001)[:, None]
        tp_t = lows <= (entry - tp_p * 0.0001)[:, None]
    has_sl, has_tp = sl_t.any(1), tp_t.any(1)
    fs = np.where(has_sl, sl_t.argmax(1), H + 1)
    ft = np.where(has_tp, tp_t.argmax(1), H + 1)
    sl_w = fs <= ft
    res = has_sl | has_tp
    exit_px = np.where(
        has_tp & ~sl_w,
        entry + direction * tp_p * 0.0001,
        np.where(has_sl, entry - direction * sl_p * 0.0001, close_h[:, -1]),
    )
    pnl_r = np.where(has_tp & ~sl_w, tp_p / sl_p,
                     np.where(has_sl, -1.0,
                              (exit_px - entry) * direction / (sl_p * 0.0001)))
    tp = int((has_tp & ~sl_w).sum()); sl = int(has_sl.sum()); to = int((~res).sum())
    pnl = float(pnl_r.sum())
    bars = float(np.where(sl_w, fs, ft).clip(max=H).sum())
    n = tp + sl + to
    return {"p_tp": tp / n, "p_sl": sl / n, "e_r": pnl / n, "horas": bars / n}


def main():
    risk = EQUITY * RISK_PCT
    for sym, path, price in (("EURUSD", "/tmp/opencode/eurusd_30k.json", 1.12391),
                             ("USDCHF", "/tmp/opencode/usdchf_30k.json", 0.82476)):
        rows = sorted(json.load(open(path)), key=lambda r: r["time"])
        df = engineer_market_features(pd.DataFrame(rows))
        t = pd.to_datetime(df["time"], utc=True)
        fl, ce = candidates(df["close"], df["high"], df["low"], df["atr"],
                            NEAREST_LEVEL_ATR_MULT)
        ppv = pip_value_per_lot(sym, price)
        print("=" * 112)
        print(f"=== {sym} · riesgo {risk:.2f} € · MIN_WIN {MIN_WIN_EUR} € ===")
        for name, lo, hi in WIN:
            idx_all = np.where((t >= lo) & (t < hi))[0]
            idx_all = idx_all[(idx_all + H) < len(df)]
            print(f"  {name}:")
            for variant in ("A", "B", "C", "D"):
                sl, tp, lot, atr_p = geometry(df, fl, ce, variant, price, ppv)
                agg = {"tp": 0, "sl": 0, "n": 0, "pnl": 0.0, "bars": 0, "sig": 0, "tot": 0}
                sl_med, tp_med, e_atr_med = [], [], []
                for d in (1, -1):
                    ok = np.isfinite(sl[:, 0 if d == 1 else 1])
                    idx = idx_all[ok[idx_all]]
                    s = sl[idx, 0 if d == 1 else 1]
                    t_ = tp[idx, 0 if d == 1 else 1]
                    l_ = lot[idx, 0 if d == 1 else 1]
                    eur_tp = l_ * t_ * ppv / price
                    passes = eur_tp >= MIN_WIN_EUR
                    agg["tot"] += len(idx)
                    agg["sig"] += int(passes.sum())
                    if not passes.any():
                        continue
                    r = simulate(df, s[passes], t_[passes], idx[passes], d)
                    agg["tp"] += r["p_tp"] * passes.sum()
                    agg["sl"] += r["p_sl"] * passes.sum()
                    agg["n"] += passes.sum()
                    agg["pnl"] += r["e_r"] * passes.sum()
                    agg["bars"] += r["horas"] * passes.sum()
                    sl_med.append(np.median(s[passes])); tp_med.append(np.median(t_[passes]))
                    e_atr_med.append(np.median(s[passes]) / np.median(atr_p[idx[passes]]))
                if not agg["n"]:
                    continue
                e_r = agg["pnl"] / agg["n"]
                sl_atr = float(np.mean(e_atr_med))
                sig = agg["sig"] / max(agg["tot"], 1)
                parts = []
                for e in EDGE_ATR:
                    parts.append(f"e={e:.1f}:E[€]={(e_r + e / sl_atr) * risk:+.2f}")
                print(f"    {variant}: señales {sig*100:5.1f}% | SL {np.mean(sl_med):5.1f}p "
                      f"TP {np.mean(tp_med):5.1f}p R:R {np.mean(tp_med)/np.mean(sl_med):4.2f} | "
                      f"P(TP) {agg['tp']/agg['n']*100:3.0f}% P(SL) {agg['sl']/agg['n']*100:3.0f}% "
                      f"E[R] {e_r:+.3f} t {agg['bars']/agg['n']:4.1f}h | " + " | ".join(parts))


if __name__ == "__main__":
    main()
