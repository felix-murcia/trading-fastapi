"""
VARIANTE "EL TECHO MÁS CERCANO QUE PAGA" (2026-10-21) — medición.

Situación medida en vivo: con el TP en el techo más cercano, el valor en € del
TP varía mucho (1,78 € … 8,12 €). Cuando no llega al mínimo de clasificación
(3 €) el gate de recompensa DESCARTA la entrada — pero el SL (el suelo más
cercano) sigue siendo válido: el siguiente techo más lejano pagaría.

Variante: el TP = el techo confirmado más cercano cuyo valor en € (al lote
elegido) sea ≥ MIN_WIN_EUR; si no hay ninguno ⇒ no hay trade (el gate lo
bloquea igual).

Mismo motor, misma muestra (29 981 velas H1 por par), dos ventanas
disjuntas, horizonte de 12 velas, prioridad SL intra-vela, timeout al mark.
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
    nearest_significant_levels,
)
from core.rules import SPREAD_BUFFER_PIPS, size_ppo_position
from services.position_sizing import pip_value_per_lot  # noqa: E402

H = 12
EQUITY, RISK_PCT = 481.17, 0.02
SAFE_LOCK_EUR, MIN_WIN_EUR = 4.0, 3.0
WIN = (("TREN", "2021-12-01", "2025-01-01"), ("TEST", "2025-01-01", "2027-01-01"))


def candidate_ceilings(close, high, low, atr, mult, window=120, max_c=4):
    """Techos confirmados por encima del precio, ordenados de más cerca a más lejos."""
    n = len(close)
    cv = close.to_numpy(float); hv = high.to_numpy(float); lv = low.to_numpy(float)
    av = atr.to_numpy(float)

    def _shift(a, k):
        o = np.full(n, np.nan)
        if k > 0:
            o[k:] = a[:-k]
        else:
            o[:k] = a[-k:]
        return o

    sh = (hv > _shift(hv, 1)) & (hv > _shift(hv, 2)) & (hv > _shift(hv, -1)) & (hv > _shift(hv, -2))
    hi_idx = np.where(sh)[0]
    out = np.full((n, max_c), np.nan)
    for t in range(n):
        a = av[t]
        if not np.isfinite(a) or a <= 0:
            continue
        band = a * mult
        cand = hi_idx[(hi_idx <= t - 2) & (hi_idx >= t - 2 - window)]
        if not len(cand):
            continue
        vals = np.sort(hv[cand][(hv[cand] > cv[t]) & ((hv[cand] - cv[t]) >= band)])
        if len(vals):
            out[t, :min(len(vals), max_c)] = vals[:max_c]
    return out


def sim(df, sl_pips, tp_pips, idx):
    h = df["high"].to_numpy(); l = df["low"].to_numpy(); c = df["close"].to_numpy()
    fut = idx[:, None] + np.arange(1, H + 1)[None, :]
    lows, highs, close_h = l[fut], h[fut], c[fut]
    entry = c[idx]
    tp = sl = to = 0
    pnl = 0.0
    bars = 0
    for d in (1, -1):
        if d == 1:
            sl_t = lows <= (entry - sl_pips * 0.0001)[:, None]
            tp_t = highs >= (entry + tp_pips * 0.0001)[:, None]
        else:
            sl_t = highs >= (entry + sl_pips * 0.0001)[:, None]
            tp_t = lows <= (entry - tp_pips * 0.0001)[:, None]
        has_sl, has_tp = sl_t.any(1), tp_t.any(1)
        fs = np.where(has_sl, sl_t.argmax(1), H + 1)
        ft = np.where(has_tp, tp_t.argmax(1), H + 1)
        sl_w = fs <= ft
        res = has_sl | has_tp
        exit_px = np.where(has_tp & ~sl_w, entry + d * tp_pips * 0.0001,
                           np.where(has_sl, entry - d * sl_pips * 0.0001, close_h[:, -1]))
        pnl_r = np.where(has_tp & ~sl_w, tp_pips / sl_pips,
                         np.where(has_sl, -1.0,
                                  (exit_px - entry) * d / (sl_pips * 0.0001)))
        tp += int((has_tp & ~sl_w).sum()); sl += int(has_sl.sum()); to += int((~res).sum())
        pnl += pnl_r.sum()
        bars += np.where(sl_w, fs, ft).clip(max=H).sum()
    n = tp + sl + to
    return {"p_tp": tp / n, "p_sl": sl / n, "p_to": to / n, "e_r": pnl / n, "horas": bars / n}


def main():
    for sym, path, price in (("EURUSD", "/tmp/opencode/eurusd_30k.json", 1.12391),
                             ("USDCHF", "/tmp/opencode/usdchf_30k.json", 0.82476)):
        rows = sorted(json.load(open(path)), key=lambda r: r["time"])
        df = engineer_market_features(pd.DataFrame(rows))
        t = pd.to_datetime(df["time"], utc=True)
        c = df["close"].to_numpy(); atr = df["atr"].to_numpy()
        floor = df["floor_below"].to_numpy()
        cands = candidate_ceilings(df["close"], df["high"], df["low"], df["atr"],
                                    NEAREST_LEVEL_ATR_MULT)
        ppv = pip_value_per_lot(sym, price)
        print("=" * 100)
        print(f"=== {sym} ===")
        for name, lo, hi in WIN:
            m = (t >= lo) & (t < hi) & np.isfinite(atr) & (atr > 0) & (floor > 0)
            idx = np.where(m)[0]
            idx = idx[(idx + H) < len(df)]
            if not len(idx):
                continue
            entry = c[idx]
            fl = floor[idx]
            # SL: suelo más cercano + buffer del EA
            sl_pips = (entry - (fl - SPREAD_BUFFER_PIPS * 0.0001)) / 0.0001
            lot = np.array([size_ppo_position(volume_norm=0.65, sl_pips=s, equity=EQUITY,
                                              pip_value=ppv, risk_pct=RISK_PCT,
                                              max_lot=0.5, min_lot=0.01)
                            for s in sl_pips])
            # variante A: el techo más cercano (implementado hoy)
            tpA = (cands[idx, 0] - entry) / 0.0001
            eurA = lot * tpA * ppv / price
            okA = np.isfinite(tpA) & (eurA >= MIN_WIN_EUR)
            # variante B: el techo más cercano que PAGA (≥ 3 €); si no hay, no hay trade
            tpB = np.full(len(idx), np.nan)
            for j in range(len(idx)):
                for k in range(cands.shape[1]):
                    cc = cands[idx[j], k]
                    if not np.isfinite(cc):
                        break
                    d = (cc - entry[j]) / 0.0001
                    if lot[j] * d * ppv / price >= MIN_WIN_EUR:
                        tpB[j] = d
                        break
            okB = np.isfinite(tpB)
            out = []
            for tag, tp, ok in (("A techo más cercano", tpA, okA), ("B techo que paga", tpB, okB)):
                if not ok.any():
                    out.append(f"{tag}: sin trades")
                    continue
                ii = idx[ok]; ss = sl_pips[ok]; tt = tp[ok]
                r = sim(df, ss, tt, ii)
                out.append(
                    f"{tag}: n={len(ii):5d} ({ok.mean()*100:4.1f}% de las señales) "
                    f"TP {np.median(tt):5.1f}p R:R {np.median(tt)/np.median(ss):4.2f} | "
                    f"P(TP) {r['p_tp']*100:3.0f}% P(SL) {r['p_sl']*100:3.0f}% "
                    f"E[R] {r['e_r']:+.3f} E[€] {r['e_r']*EQUITY*RISK_PCT:+.3f} "
                    f"t {r['horas']:4.1f}h")
            print(f"  {name:5s}")
            for o in out:
                print("     " + o)


if __name__ == "__main__":
    main()
