#!/usr/bin/env python3
"""Diagnóstico de expectancia negativa (Fase 3, plan 2026-09-30) — SOLO LECTURA.

Responde a la pregunta que el gate solo enmarca: ¿DÓnde se pierde? Descompone
las operaciones del bundle ACTIVO (contrato 20,24, sin memoria) en el MISMO
dominio que evaluó el gate (mismo snapshot, mismos folds, mismo env_cfg,
mismas stats VecNormalize) en:

  1. Mezcla de cierres (sl / tp / agent) × (bruto de mercado, fricción, neto).
  2. Ficción total = spread completo + 2 comisiones por round-trip.
  3. Frecuencia real de trading vs la métrica `openings` del gate
     (intención del policy con umbral 0.33 — ¿cuadra con aperturas reales?).
  4. Reaperturas en la ventana anti-churn (≤2 velas) del candidato Fase 3.
  5. Ventana 2024-09-02→2026-10-01 (mismo tramo que el libro REAL, 147
     trades, +306.81 €) → comparación sim-vs-real sobre idénticos datos.

No escribe nada en el repo ni en la DB. Salida: informe en stdout + JSON en
DIAG_JSON (default /tmp/opencode/diagnostico_expectancia.json).
"""
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

# Mismo patrón que tests/conftest.py: Settings se instancia al importar.
os.environ.setdefault("database_url", "postgresql://test:test@localhost/test")
os.environ.setdefault("internal_token", "test-token")
os.environ.setdefault("hmac_secret", "test-hmac")
os.environ.setdefault("mt5_http_url", "http://localhost:8000")
os.environ.setdefault(
    "PPO_OUTPUT_DIR", str(Path(__file__).resolve().parents[1] / "fastapi" / "ml")
)
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "fastapi"))

import numpy as np
import pandas as pd

from ml.trading_env_v2 import CHURN_BARS, ForexTradingEnvV2
from services.auto_retrain import (  # noqa: E402
    RetrainConfig,
    _load_obs_rms,
    default_model_path,
)
from services.walk_forward import build_walk_forward_split  # noqa: E402
from stable_baselines3 import PPO
from stable_baselines3.common.vec_env import DummyVecEnv, VecNormalize

REPO = Path(__file__).resolve().parents[1]
SNAPSHOT = (
    REPO / "fastapi" / "ml" / "snapshots" /
    "20211206T170000Z_20261001T140000Z__a0f03c076d8f__retrain.parquet"
)
LIVE_FROM = "2024-09-02"   # primera salida real (trade_outcomes)
JSON_OUT = os.environ.get("DIAG_JSON", "/tmp/opencode/diagnostico_expectancia.json")
SPREAD = 1.5 * 0.0001       # spread_pips=1.5 del env_cfg del gate
COMM = RetrainConfig.commission  # commission_per_lot=0.0001 (ver hallazgo)


def make_env_cfg(df: pd.DataFrame) -> dict:
    """Réplica verbatim de auto_retrain._do_retrain_core env_cfg (L777-804)."""
    cfg = RetrainConfig
    from config import settings
    return dict(
        df=df,
        window_size=cfg.retrain_window_size,
        initial_balance=cfg.initial_balance,
        lot_size=100000.0,
        max_lot=settings.max_volume,
        risk_pct=settings.ppo_risk_pct,
        pip_size=0.0001,
        spread_pips=1.5,
        commission_per_lot=cfg.commission,
        max_episode_steps=1000,
        max_holding_steps=120,
        reward_scale=100.0,
        random_reset=False,
        real_outcomes=[],
        real_outcome_weight=cfg.real_outcome_weight,
        trailing_enabled=True,
        trailing_percent=0.25,
        trailing_min_pips=15.0,
        trailing_atr_mult=2.0,
        min_stop_dist_pips=1.0,
        memory_channel=True,  # el evaluador lo pisa según el ancho del modelo
    )


def evaluate(policy, segment_df, rms, env_cfg_template):
    """_evaluate_model + instrumentación por trade. Devuelve (métricas, records)."""
    cfg = dict(env_cfg_template)
    cfg["df"] = segment_df
    width = tuple(policy.observation_space.shape)[-1]
    if width == 24:
        cfg["memory_channel"] = False
    elif width == 25:
        cfg["memory_channel"] = True
    else:
        raise RuntimeError(f"contrato desconocido: {width}")

    dummy = DummyVecEnv([lambda: ForexTradingEnvV2(**cfg)])
    if rms is not None:
        env = VecNormalize(dummy, training=False, norm_reward=False, clip_obs=10.0)
        env.obs_rms = rms
    else:
        env = dummy
    # envgym subyacente: VecNormalize → DummyVecEnv.envs → ForexTradingEnvV2
    raw = env.venv if isinstance(env, VecNormalize) else env
    if isinstance(raw, DummyVecEnv):
        raw = raw.envs[0]

    obs = env.reset()
    records: list[dict] = []
    metric_openings = 0
    actual_opens = 0
    prev_pos = 0
    prev_equity = cfg["initial_balance"]
    total_reward = 0.0
    open_step_idx = None
    open_t = None
    churn_opens = 0
    done = False
    step = 0
    while not done:
        # Estado PRE-paso (antes del auto-reset de DummyVecEnv al `done`):
        # la vela consumida es current_step EN este paso (se incrementa al
        # final, L647) y sl/tp/volumen siguen siendo los de la posición abierta.
        pre_pos = raw.position
        pre_sl, pre_tp = raw.sl_price, raw.tp_price
        pre_vol = raw.volume
        pre_ever = raw.ever_closed
        pre_bars = raw.bars_since_close
        pre_pnl_n = len(raw.trade_pnls)
        pre_candle_idx = raw.current_step

        action, _ = policy.predict(obs, deterministic=True)
        flat = np.asarray(action, dtype=np.float32).reshape(-1)
        target = 1 if flat[0] > 0.33 else (-1 if flat[0] < -0.33 else 0)
        if target != 0 and prev_pos == 0:
            metric_openings += 1          # métrica 'openings' del gate

        obs, reward, done_arr, infos = env.step(flat.reshape(1, -1))
        info = infos[0]
        total_reward += float(reward[0])
        step += 1

        # info['position'/'trade_pnls'] es el snapshot del paso ANTES del
        # auto-reset (al `done` DummyVecEnv reinicia el env subyacente:
        # position=0, trade_pnls=[] — raw.* daría cierres fantasma con la
        # lista vacía y el índice fuera de rango).
        pos_now = int(info.get("position", 0))
        pnls = list(info.get("trade_pnls") or [])
        closed = len(pnls) > pre_pnl_n
        opened = (pre_pos == 0 and pos_now != 0) or (closed and pos_now != 0)

        if closed:                                              # cierre
            candle = raw.df.iloc[pre_candle_idx]
            if pre_pos == 1:
                reason = "sl" if candle["low"] <= pre_sl else (
                    "tp" if candle["high"] >= pre_tp else "agent")
            else:
                reason = "sl" if candle["high"] >= pre_sl else (
                    "tp" if candle["low"] <= pre_tp else "agent")
            realized = float(pnls[pre_pnl_n])
            spread_cost = pre_vol * 100000.0 * SPREAD
            open_comm = pre_vol * COMM
            close_comm = pre_vol * COMM
            market_gross = realized + close_comm + spread_cost
            records.append({
                "reason": reason,
                "market_gross": market_gross,
                "friction": spread_cost + open_comm + close_comm,
                "net": realized - open_comm,
                "realized_close": realized,
                "vol": pre_vol,
                "bars_held": (step - open_step_idx) if open_step_idx else None,
                "pnl3": realized >= 3.0,   # regla 3 € sobre el cierre
                # trazas temporales para el cruce sim-vs-real (mismo tramo)
                "t_open": str(open_t) if open_t is not None else None,
                "t_close": str(raw.df.iloc[pre_candle_idx]["time"]),
            })
            open_step_idx = None
            open_t = None

        if opened:                                              # apertura real
            # Va DESPUÉS del cierre: en reversión (mismo paso) el cierre usa
            # open_step_idx/open_t de la posición ANTERIOR y ésta los pisa
            # para la nueva.
            actual_opens += 1
            bars_at_open = 0 if closed else pre_bars + 1
            if (closed or pre_ever) and bars_at_open <= CHURN_BARS:
                churn_opens += 1
            open_step_idx = step
            open_t = raw.df.iloc[pre_candle_idx]["time"]

        prev_pos = int(info.get("position", 0))
        prev_equity = float(info.get("equity", prev_equity))
        done = bool(done_arr[0])

    return {
        "steps": step,
        "reward": total_reward,
        "final_equity": prev_equity,
        "trades": len(records),
        "actual_opens": actual_opens,
        "metric_openings": metric_openings,
        "churn_opens": churn_opens,
        "balance": float(info.get("balance", cfg["initial_balance"])),
    }, records


def summarize(tag, metrics, records):
    by = defaultdict(lambda: {"n": 0, "market": 0.0, "friction": 0.0, "net": 0.0})
    for r in records:
        b = by[r["reason"]]
        b["n"] += 1
        b["market"] += r["market_gross"]
        b["friction"] += r["friction"]
        b["net"] += r["net"]
    tot_market = sum(b["market"] for b in by.values())
    tot_fric = sum(b["friction"] for b in by.values())
    tot_net = sum(b["net"] for b in by.values())
    n = len(records)
    sub3 = sum(1 for r in records if 0 < r["realized_close"] < 3.0)
    wr3 = sum(1 for r in records if r["pnl3"]) / n if n else 0.0
    print(f"\n=== {tag} ===")
    print(f"steps={metrics['steps']} trades={n} opens_reales={metrics['actual_opens']} "
          f"metric_openings={metrics['metric_openings']} churn_opens={metrics['churn_opens']} "
          f"reward={metrics['reward']:.2f}")
    print(f"{'reason':>7} {'n':>4} {'mercado':>10} {'friccion':>9} {'neto':>10} {'neto/tr':>8}")
    for reason in ("sl", "tp", "agent"):
        b = by.get(reason)
        if not b:
            continue
        print(f"{reason:>7} {b['n']:>4} {b['market']:>10.2f} {b['friction']:>9.2f} "
              f"{b['net']:>10.2f} {(b['net']/b['n']):>8.2f}")
    print(f"{'TOTAL':>7} {n:>4} {tot_market:>10.2f} {tot_fric:>9.2f} {tot_net:>10.2f} "
          f"{(tot_net/n if n else 0):>8.2f}   WR(3€)={wr3:.0%} sub3€={sub3}")
    return {
        "tag": tag, **metrics, "tot_market": tot_market,
        "tot_friction": tot_fric, "tot_net": tot_net,
        "by_reason": {k: dict(v) for k, v in by.items()},
        "wr3": wr3, "sub3_wins": sub3,
    }


def main():
    df = pd.read_parquet(SNAPSHOT).sort_values("time").reset_index(drop=True)
    assert "rsi14" in df.columns, f"snapshot sin features: {list(df.columns)[:8]}"
    print(f"snapshot: {len(df)} barras  {df['time'].iloc[0]} → {df['time'].iloc[-1]}")

    model_path = default_model_path("EURUSD")
    model = PPO.load(model_path)
    rms = _load_obs_rms(model_path)
    print(f"bundle activo: {model_path} obs={model.observation_space.shape}")
    env_tpl = make_env_cfg(df)

    reports = []
    split = build_walk_forward_split(
        len(df),
        window_size=RetrainConfig.retrain_window_size,
        n_folds=RetrainConfig.walk_forward_folds,
        train_frac=RetrainConfig.walk_forward_train_frac,
        holdout_frac=RetrainConfig.walk_forward_holdout_frac,
        min_fold_bars=RetrainConfig.walk_forward_min_fold_bars,
    )
    # Espejo de evaluate_walk_forward: segmento = context_start:end (evalúa
    # los primeros 1000 pasos por max_episode_steps, igual que el gate).
    for i, sl in enumerate(split.folds):
        m, rec = evaluate(model, df.iloc[sl.context_start:sl.end], rms, env_tpl)
        reports.append(summarize(f"fold{i} (gate)", m, rec))
    m, rec = evaluate(model, df.iloc[split.holdout.context_start:split.holdout.end],
                      rms, env_tpl)
    reports.append(summarize("holdout (congelado)", m, rec))

    # Ventana REAL: mismas fechas que el libro vivo (147 trades, +306.81 €).
    live = df[df["time"] >= LIVE_FROM].reset_index(drop=True)
    print(f"\nventana real: {len(live)} barras {LIVE_FROM} → {live['time'].iloc[-1]}")
    for start in range(0, len(live), 1000):
        seg = live.iloc[max(0, start - 20):start + 1000]
        if len(seg) <= 21:
            break
        m, rec = evaluate(model, seg, rms, env_tpl)
        reports.append(summarize(f"real-window [{start}:{start + 1000}]", m, rec))

    with open(JSON_OUT, "w") as fh:
        json.dump(reports, fh, indent=1, default=float)
    print(f"\nJSON → {JSON_OUT}")


if __name__ == "__main__":
    main()
