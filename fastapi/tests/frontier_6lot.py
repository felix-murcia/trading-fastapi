#!/usr/bin/env python3
"""
Frontier analysis for lot ~0.06 and SL/TP distances.

This script loads the most recent 30k H1 bars from MT5 (same feed as the live system)
and computes:
- The closest usable floor/ceiling levels that can support a lot of ~0.06
- The resulting max lot for each possible SL/TP pair
- A summary of which SL distances make 0.06 feasible
"""
import sys, os, json, urllib.request
import numpy as np
import pandas as pd
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + '/..')

from ml.trading_env_v2 import engineer_market_features, pip_value_eur_per_lot
from core.rules import MIN_WIN_EUR, SPREAD_BUFFER_PIPS

# Constants from settings (hard‑coded for the analysis)
RISK_PCT = 0.02          # PPO risk % (from .env)
MIN_LOT = 0.01           # min_volume
MAX_LOT = 0.50           # max_volume
MIN_LOCK_EUR = 4.0        # min_lock_eur (Safe‑4)
EXEC_COST_PIPS = SPREAD_BUFFER_PIPS  # spread buffer (EA constant)

def fetch_mt5_candles(symbol: str = "EURUSD", timeframe: str = "H1", count: int = 30000) -> pd.DataFrame:
    """Fetch the latest H1 candles from MT5 MCP."""
    url = "http://100.81.112.95:8000/api/v1/market/candles/latest"
    params = f"symbol_name={symbol}&timeframe={timeframe}&count={count}"
    req = urllib.request.Request(f"{url}?{params}")
    req.add_header("X-Internal-Token", os.getenv("INTERNAL_TOKEN", "test"))
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read())
        if not data:
            raise ValueError("No candles returned")
        df = pd.DataFrame(data)
        df["time"] = pd.to_datetime(df["time"])
        df = df.sort_values("time").reset_index(drop=True)
        return df
    except Exception as e:
        # If fetching fails, fall back to synthetic data (as in simulacro_ppo_qwen.py)
        print(f"[FRONTIER] ERROR fetching real data: {e}")
        print("[FRONTIER] Fallback to synthetic data.")
        dates = pd.date_range("2026-03-10", periods=count, freq="h")
        return pd.DataFrame({
            "time": dates,
            "open": 1.10 + np.cumsum(np.random.normal(0, 0.001, count)),
            "high": 1.10 + np.cumsum(np.random.normal(0, 0.001, count)) + 0.002,
            "low": 1.10 + np.cumsum(np.random.normal(0, 0.001, count)) - 0.002,
            "close": 1.10 + np.cumsum(np.random.normal(0, 0.001, count)),
            "tick_volume": np.random.randint(100, 1000, count),
        })

def main():
    print("[FRONTIER] Loading latest H1 bars (≈30k)…")
    df_eur = fetch_mt5_candles("EURUSD", "H1", 30000)
    df_usd = fetch_mt5_candles("USDCHF", "H1", 30000)

    # Engineer features (this computes floor_below / ceiling_above)
    print("[FRONTIER] Engineering market features…")
    df_eur_feat = engineer_market_features(df_eur)
    df_usd_feat = engineer_market_features(df_usd)

    # Helper: compute max lot for a given symbol, price, sl_pips, tp_pips
    def max_lot_for_pair(df_feat, symbol, price, sl_pips, tp_pips):
        """Return the lot that satisfies I1‑I3 for the given SL/TP (or 0 if impossible)."""
        ppl = pip_value_eur_per_lot(symbol, price)
        cap_eur = 1000.0 * RISK_PCT  # placeholder equity = 1k EUR for analysis
        # I1: risk ≤ cap
        risk_eur = sl_pips * ppl * MIN_LOT  # assume min_lot as baseline
        if risk_eur > cap_eur:
            return 0.0
        # I2: risk ≥ min_lock_eur (with lot scaling)
        lot_by_lock = (MIN_LOCK_EUR / (sl_pips * ppl)) / MIN_LOT
        lot_by_lock = np.ceil(lot_by_lock) * MIN_LOT
        if lot_by_lock > MAX_LOT:
            return 0.0
        # I3: net reward ≥ max(MIN_WIN_EUR, min_lock_eur)
        net_req = max(MIN_WIN_EUR, MIN_LOCK_EUR)
        lot_by_reward = (net_req / ((tp_pips - EXEC_COST_PIPS) * ppl)) / MIN_LOT
        lot_by_reward = np.ceil(lot_by_reward) * MIN_LOT
        lot_lo = max(MIN_LOT, lot_by_lock, lot_by_reward)
        final_lot = min(lot_lo, MAX_LOT)
        return float(final_lot)

    # Scan for feasible SL/TP pairs that support lot ≈ 0.06
    target_lot = 0.06
    feasible = []

    for sym, df in [("EURUSD", df_eur_feat), ("USDCHF", df_usd_feat)]:
        # Use the most recent row as reference price
        latest = df.iloc[-1]
        price = float(latest["close"])
        # Iterate over possible SL distances (pips) 5‑50 in steps of 1
        for sl_pips in range(5, 51):
            # For each SL, find the nearest usable level (floor/ceil) that is ≥ sl_pips
            if sym == "EURUSD":  # BUY → TP is ceiling above entry
                ceiling = float(latest.get("ceiling_above", 0))
                if ceiling > price:
                    tp_pips = int((ceiling - price) / pip_value_eur_per_lot(sym, price) * 10.86207)  # rough
                    if tp_pips > 0:
                        lot = max_lot_for_pair(df, sym, price, sl_pips, tp_pips)
                        if lot >= target_lot * 0.95:  # within 5% of target
                            feasible.append((sym, sl_pips, tp_pips, lot))
            else:  # USDCHF SELL → TP is floor below entry
                floor = float(latest.get("floor_below", 0))
                if floor < price:
                    tp_pips = int((price - floor) / pip_value_eur_per_lot(sym, price) * 10.86207)
                    if tp_pips > 0:
                        lot = max_lot_for_pair(df, sym, price, sl_pips, tp_pips)
                        if lot >= target_lot * 0.95:
                            feasible.append((sym, sl_pips, tp_pips, lot))

    # Print summary
    print("\n[FRONTIER] Feasible SL/TP pairs that support lot ≈ 0.06 (≈ within 5%):")
    for row in feasible[:30]:
        print(f"  {row[0]:6s}  SL={row[1]:3d} p  TP={row[2]:3d} p  → max lot={row[3]:.3f}")
    if not feasible:
        print("  (none found – tighter SL/TP geometry needed)")

    # Additional output: the absolute closest usable levels for each symbol
    print("\n[FRONTIER] Closest usable levels (latest row):")
    for sym, df in [("EURUSD", df_eur_feat), ("USDCHF", df_usd_feat)]:
        latest = df.iloc[-1]
        price = float(latest["close"])
        floor = float(latest.get("floor_below", 0))
        ceiling = float(latest.get("ceiling_above", 0))
        if floor > 0:
            sl = int((price - floor) / pip_value_eur_per_lot(sym, price) * 10.86207)
            print(f"  {sym}: floor at {floor:.5f} → SL={sl} p (if SELL), TP={sl} p (TP = floor)")
        if ceiling > 0:
            tp = int((ceiling - price) / pip_value_eur_per_lot(sym, price) * 10.86207)
            print(f"  {sym}: ceiling at {ceiling:.5f} → SL={tp} p (if BUY), TP={tp} p (TP = ceiling)")

    # Save results to CSV for later reference
    out_df = pd.DataFrame(feasible, columns=["symbol", "sl_pips", "tp_pips", "max_lot"])
    out_path = "/tmp/frontier_06_lot.csv"
    out_df.to_csv(out_path, index=False)
    print(f"\n[FRONTIER] Results saved to {out_path}")

if __name__ == "__main__":
    main()