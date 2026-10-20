"""
E1b · ¿Es TRADABLE el edge de la ESTRUCTURA? (evaluación, sin entrenar)

La vara canónica dice: la estructura (SMA200 + pendiente) acierta el 62,6 % de
las direcciones a +24 h (CI 58,0 %) — el edge trivial más fuerte medido, y el
momentum está INVERTIDO (42,1 %).

Pero una exactitud direccional NO es una estrategia: hay que medirla CON la
mecánica de ejecución real (spread 0,17 pips, SL/TP, sizing, la regla de 3 €,
el ancla anti-churn) y en OOS (walk-forward con el split congelado).

Este script evalúa la regla como POLÍTICA en el mismo env y con el mismo
walk-forward que el retrain ⇒ sus números son comparables con los de un
candidato entrenado (mismo gate, misma expectativa, mismo DD).
"""
import asyncio
import datetime as dt
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, "/app")
from services import auto_retrain as ar
from services.walk_forward import build_walk_forward_split, evaluate_walk_forward


class StructurePolicy:
    """Política determinista: la dirección la decide la ESTRUCTURA.

    La cabeza de dirección del obs se fuerza al signo de la estructura
    (SMA200 + pendiente), con la magnitud neutra (0,9) para no saturar el
    decodificador. Todo lo demás (SL/TP, sizing, costes, la regla de 3 € y
    el ancla anti-churn) es EXACTAMENTE el del env ⇒ la evaluación mide la
    regla con la mecánica de producción.
    """

    observation_space = None  # se fija abajo (shape del contrato vivo)

    def __init__(self, df: pd.DataFrame):
        from gymnasium.spaces import Box
        self.observation_space = Box(-np.inf, np.inf, shape=(20, 25), dtype=np.float32)
        self.action_space = Box(-1.0, 1.0, shape=(4,), dtype=np.float32)
        self.df = df.reset_index(drop=True)
        close = self.df["close"].astype(float)
        self.sma = close.rolling(200).mean()
        self.slope = (self.sma - self.sma.shift(5)) / self.sma.shift(5)

    def action_for(self, step: int) -> float:
        if step < 210 or pd.isna(self.sma.iloc[step]) or pd.isna(self.slope.iloc[step]):
            return 0.0
        c = float(self.df["close"].iloc[step])
        s = float(self.sma.iloc[step])
        sl = float(self.slope.iloc[step])
        if c > s and sl > 0:
            return 0.9
        if c < s and sl < 0:
            return -0.9
        return 0.0

    def predict(self, observation, deterministic=True):
        # El env se reinicia con random_reset=False ⇒ el step 0 es fijo; el
        # avance lo controla el env, así que se usa un contador compartido.
        step = getattr(self, "_step", 0)
        self._step = step + 1
        direction = self.action_for(step)
        vol = np.array([direction, 0.18, 0.15, 0.15], dtype=np.float32)
        return vol, None


async def main():
    print("=" * 74)
    print("E1b · LA REGLA DE ESTRUCTURA COMO POLÍTICA (evaluación OOS, sin entrenar)")
    print("=" * 74)
    cfg = ar.RetrainConfig()
    candles = await ar._fetch_historical_candles(symbol="EURUSD", count=30000)
    df = ar._build_training_dataframe(candles)
    df = ar._trim_trend_warmup(df)
    print(f"  datos: {len(df)} filas · {df['time'].iloc[0]} → {df['time'].iloc[-1]}")

    # Reutiliza EXACTAMENTE el env_cfg del retrain (misma mecánica: spread,
    # SL/TP, sizing, regla 3 €, ancla anti-churn, costes) ⇒ los números son
    # comparables con los de un candidato entrenado.
    from config import settings
    env_cfg = dict(
        df=df,
        window_size=cfg.retrain_window_size,
        initial_balance=cfg.initial_balance,
        lot_size=100000.0,
        max_lot=settings.max_volume,
        risk_pct=settings.ppo_risk_pct,
        pip_size=0.0001,
        spread_pips=0.17,
        commission_per_lot=cfg.commission,
        churn_friction_per_lot=cfg.churn_friction_per_lot,
        max_episode_steps=1000,
        max_holding_steps=120,
        reward_scale=100.0,
        random_reset=False,
        real_outcomes=[],
        real_outcome_weight=cfg.real_outcome_weight,
        trailing_enabled=True,
        trailing_percent=0.25,
        trailing_min_pips=15.0,
        reward_version=cfg.reward_version,
    )

    split = build_walk_forward_split(
        len(df), n_folds=4, train_frac=0.70, holdout_frac=0.10,
        window_size=cfg.retrain_window_size,
    )
    print(f"  split: {split}")

    # La política necesita el df COMPLETO (para la SMA200) pero se evalúa
    # sobre los segmentos ⇒ se le da el df del segmento con la SMA200 calculada
    # sobre la historia (se recalcula por segmento; es una regla de estructura
    # y el warm-up la neutraliza).
    def eval_segment(policy, segment_df, rms):
        pol = StructurePolicy(segment_df)
        return ar._evaluate_model(pol, segment_df, env_cfg, obs_rms=rms)

    report = evaluate_walk_forward(
        df, split,
        candidate=StructurePolicy(df),
        candidate_obs_rms=None,
        baseline=None,
        baseline_obs_rms=None,
        evaluate_fn=eval_segment,
        initial_balance=cfg.initial_balance,
    )
    r = report.to_dict() if hasattr(report, "to_dict") else dict(report)
    print()
    print("  FOLDS:")
    for f in r["folds"]:
        c = f["candidate"]
        da = c.get("direction_accuracy")
        print(f"    fold{f['fold']}: reward={c['reward']:+8.2f} sharpe={c['sharpe']:+6.2f} "
              f"trades={c['trades']:3d} win={c['win_rate']*100:3.0f}% DD={c['max_dd']*100:5.2f}% "
              f"exp={c['expectancy']:+7.3f} dir={('%.1f%%'%(da*100)) if da is not None else 'n/d'}")
    h = r["holdout"]
    c = h["candidate"]
    da = c.get("direction_accuracy")
    print()
    print(f"  HOLDOUT: reward={c['reward']:+8.3f} sharpe={c['sharpe']:+6.2f} "
          f"trades={c['trades']:3d} win={c['win_rate']*100:3.0f}% DD={c['max_dd']*100:5.2f}% "
          f"exp={c['expectancy']:+7.3f} dir={('%.1f%%'%(da*100)) if da is not None else 'n/d'}")
    agg = r.get("candidate", {})
    print(f"  AGREGADO folds: reward={agg.get('sum_reward')} sharpe={agg.get('mean_sharpe')} "
          f"trades={agg.get('total_trades')} dir={agg.get('direction_accuracy')}")


if __name__ == "__main__":
    asyncio.run(main())
