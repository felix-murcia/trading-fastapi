"""T-26 (2026-10-17): test de validación DIP-VS-TENDENCIA — la prueba concreta
de REQ-2 («distinguir dip de cambio») sobre el bundle comprometido en
`fastapi/ml/`.

Conjunto etiquetado mínimo (fixture `tests/fixtures/t26_dip_vs_trend.json`,
construido a ciegas de predicciones por `audits/t26_build_fixture.py`):

- (a) DIP-IN-UPTREND (precio retrocede hacia un SMA200 ascendente, ADX ≥ 0,25)
  → el PPO **NO** debe emitir SELL contracorriente (ni HOLD ni BUY fallan).
- (b) REAL TREND CHANGE (precio cruza por debajo un SMA200 que gira a la baja,
  ADX cayendo) → el PPO **SÍ** debe poder girar: no puede seguir comprando
  (HOLD o SELL). La capacidad de girar = no estar bloqueado en BUY; el %SELL
  se reporta como diagnóstico (el reward v2 supervisa «no comprar contra
  tendencia» — A_t < 0 sostenido mientras se mantiene — y las pérdidas por
  Δe; el short-from-flat no está directamente supervisionado, exigirlo sería
  exigir lo que el mecanismo no garantiza).

Adaptativo al contrato: el ancho del observation_space del bundle determina
canales vía `derive_obs_contract` (25 = 17 market + memoria → hoy ROJO en (a)
y (b); 28 = 20 market + memoria → objetivo VERDE tras T-27). El fixture guarda
las 20 columnas (17 base + bloque de tendencia T-22) para servir ambos
contratos.

Mecánica del estado ROJO→VERDE: `xfail(strict=True)` sobre la clase que hoy
falla — la suite queda VERDE mientras el bundle vigente la tenga en rojo, y
el strict convierte el primer pase en FALLO hasta que se quite el marker
(el handoff obligatorio de T-27: promover bundle (20,28) → quitar marker →
aserción viva).

Decisiones del PPO en estado plano (protocolo T-23/T-25: position=0, canal de
memoria 1,0, precio/atr de la última vela) — mismo harness de replays.
"""
import json
from pathlib import Path

import gymnasium as gym
import numpy as np
import pytest
from gymnasium import spaces
from stable_baselines3 import PPO
from stable_baselines3.common.vec_env import DummyVecEnv, VecNormalize

from ml.trading_env_v2 import (
    MARKET_FEATURES,
    TREND_MARKET_FEATURES,
    build_v3_observation,
    decode_v3_direction,
    derive_obs_contract,
)

OBS = 20
SELL, FLAT, BUY = -1, 0, +1

_FIXTURE = Path(__file__).parent / "fixtures" / "t26_dip_vs_trend.json"
_MODEL = Path(__file__).parent.parent / "ml" / "ppo_trading_bot_v3.zip"
_VECNORM = Path(__file__).parent.parent / "ml" / "ppo_trading_bot_v3_vec_norm.pkl"


class _DummyEnv(gym.Env):
    """Env vacío sólo para VecNormalize.load (patrón de los sondeos T-23/T-25)."""

    def __init__(self, shape):
        self.observation_space = spaces.Box(-np.inf, np.inf, shape=shape, dtype=np.float32)
        self.action_space = spaces.Box(-1.0, 1.0, shape=(2,), dtype=np.float32)

    def reset(self, *, seed=None, options=None):
        return np.zeros(self.observation_space.shape, dtype=np.float32), {}

    def step(self, action):
        return np.zeros(self.observation_space.shape, dtype=np.float32), 0.0, False, False, {}


@pytest.fixture(scope="module")
def bundle():
    """(fixture JSON, model, vec_norm, columnas del fixture, contrato)."""
    with open(_FIXTURE) as fh:
        data = json.load(fh)
    model = PPO.load(str(_MODEL))
    shape = tuple(model.observation_space.shape)
    vn = VecNormalize.load(str(_VECNORM), DummyVecEnv([lambda: _DummyEnv(shape)]))
    vn.training = False
    vn.norm_reward = False
    width = shape[1]
    contract = derive_obs_contract(width)
    assert contract is not None, f"ancho {width} sin contrato conocido (T-22)"
    return data, model, vn, data["meta"]["columns"], contract


def _decide(model, vn, cols, contract, scenario):
    """Decisión determinista del bundle en estado plano sobre la ventana."""
    trend, _memory = contract
    feats = list(MARKET_FEATURES) + (list(TREND_MARKET_FEATURES) if trend else [])
    window = scenario["window"]
    market = np.array(
        [[window[i][cols.index(f)] for f in feats] for i in range(OBS)],
        dtype=np.float32,
    )
    last = dict(zip(cols, window[-1]))
    obs = build_v3_observation(
        market_values=market,
        expected_market_features=len(feats),
        position=0,
        entry_price=0.0,
        current_price=float(scenario["context"]["close"]),
        sl_price=float(scenario["context"]["close"]),
        tp_price=float(scenario["context"]["close"]),
        current_atr=float(last["atr"]),
        steps_in_trade=0,
        max_holding_steps=120,
        balance=1000.0,
        initial_balance=1000.0,
        window_size=OBS,
        bars_since_close_norm=1.0,
    )
    action, _ = model.predict(vn.normalize_obs(obs)[None], deterministic=True)
    dir_val = float(np.asarray(action, dtype=np.float32).reshape(-1)[0])
    target, decision = decode_v3_direction(dir_val, 0)
    return target, decision, dir_val


def _run_class(bundle, cls):
    data, model, vn, cols, contract = bundle
    rows = [s for s in data["scenarios"] if s["class"] == cls]
    assert rows, f"fixture sin escenarios de la clase {cls}"
    out = []
    for s in rows:
        target, decision, dir_val = _decide(model, vn, cols, contract, s)
        out.append((s["id"], target, decision, dir_val))
    return out


def _report(results):
    n = len(results)
    shares = {name: sum(1 for _, t, _, _ in results if t == code) / n
              for code, name in ((SELL, "SELL"), (FLAT, "HOLD"), (BUY, "BUY"))}
    mean_dir = float(np.mean([d for *_, d in results]))
    detail = ", ".join(f"{i}:{dec}({d:+.2f})" for i, _, dec, d in results)
    return (f"n={n} · SELL {100*shares['SELL']:.0f}% · HOLD {100*shares['HOLD']:.0f}% · "
            f"BUY {100*shares['BUY']:.0f}% · dir media {mean_dir:+.3f} · [{detail}]")


# Estado medido 2026-10-17 sobre el bundle vivo (20,25), 48 escenarios:
# (a) SELL 29 % contracorriente (dir media +0,402) → ROJO (el fallo
# pre-registrado del backlog: «Hoy el PPO falla (a)»);
# (b) BUY 79 % en cambio confirmado (dir media +0,497) → TAMBIÉN ROJO (el PPO
# no distingue dip de cambio: decisiones casi idénticas entre clases;
# HOLD 0 % en ambas — saturación de T-23).
# `strict=True` = handoff obligatorio de T-27: al promover el bundle (20,28)
# con v2_align, el primer pase inesperado FALLA la suite hasta quitar el
# marker (y la aserción queda viva).
@pytest.mark.xfail(
    reason="T-26 (a): ROJO sobre el bundle vivo (20,25) sin canales de tendencia "
           "— VERDE esperado con el bundle T-27 (20,28)+v2_align; strict=True "
           "fuerza a quitar este marker al promover",
    strict=True,
)
def test_a_dip_in_uptrend_no_counter_trend_sell(bundle):
    results = _run_class(bundle, "dip_uptrend")
    bad = [r for r in results if r[1] == SELL]
    assert not bad, (
        f"(a) SELL contracorriente en {len(bad)}/{len(results)} dips-dentro-de-"
        f"tendencia-alcista (REQ-2: el dip NO es cambio de tendencia). "
        f"{_report(results)}"
    )


@pytest.mark.xfail(
    reason="T-26 (b): ROJO sobre el bundle vivo (20,25) — bloqueado en BUY en el "
           "79 % de cambios reales; VERDE esperado con el bundle T-27 (20,28)"
           "+v2_align; strict=True fuerza a quitar el marker al promover",
    strict=True,
)
def test_b_real_change_is_not_buy_locked(bundle):
    results = _run_class(bundle, "real_change")
    bad = [r for r in results if r[1] == BUY]
    assert not bad, (
        f"(b) bloqueado en BUY en {len(bad)}/{len(results)} cambios reales de "
        f"tendencia (el PPO no puede girar — REQ-2). {_report(results)}"
    )
