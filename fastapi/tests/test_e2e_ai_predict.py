import json
import sys
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient
from gymnasium.spaces import Box

sys.path.insert(0, str(Path(__file__).parents[1] / "ml"))

from ml.trading_env_v2 import (
    MARKET_FEATURES,
    build_v3_observation,
    decode_v3_direction,
    engineer_market_features,
)
from ml.train_ppo_v3 import FEATURE_NAMES
from config import settings
from main import app
import routers.ai
from routers.ai import Brain, PredictRequest, PredictResponse, TradeFilledRequest
from services.position_sizing import pip_value_per_lot, size_ppo_position


class FakeAsyncClient:
    def __init__(self, *args, **kwargs):
        self.timeout = kwargs.get("timeout")

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, url, **kwargs):
        if "/api/v1/account/info" in url:
            return self._response({"equity": 1000.0, "balance": 1000.0})
        if "/api/v1/market/candles/latest" in url:
            return self._response(_build_candles())
        return self._response({})

    async def post(self, url, **kwargs):
        if "/v1/chat/completions" in url:
            return self._response({
                "choices": [{
                    "message": {
                        "content": json.dumps({
                            "quality": 8.5,
                            "reason": "Strong bullish setup",
                            "bias": "BULLISH",
                            "confidence_modifier": 1.1,
                            "sl_adjusted": 0.20,
                            "tp_adjusted": 0.28,
                            "regime": "TRENDING",
                        })
                    }
                }]
            })
        return self._response({})

    @staticmethod
    def _response(payload, status_code=200):
        response = MagicMock()
        response.status_code = status_code
        response.json.return_value = payload
        response.text = json.dumps(payload)
        return response


class ConnectTimeoutAsyncClient(FakeAsyncClient):
    """MT5 inaccesible: cualquier GET lanza ConnectTimeout."""

    async def get(self, url, **kwargs):
        raise httpx.ConnectTimeout("MT5 down (test)")


def _build_candles():
    candles = []
    base = 1.1000
    for i in range(100):
        hour = i // 60
        minute = i % 60
        timestamp = f"2024-01-01T{hour:02d}:{minute:02d}:00Z"
        open_price = base + i * 0.0003
        close_price = open_price + 0.0005 + (i % 5) * 0.0002
        high = max(open_price, close_price) + 0.0008
        low = min(open_price, close_price) - 0.0008
        candles.append({
            "time": timestamp,
            "open": round(open_price, 5),
            "high": round(high, 5),
            "low": round(low, 5),
            "close": round(close_price, 5),
            "tick_volume": 1000 + i,
        })
    # P0: el MCP real devuelve las velas más NUEVA primero (descendente) — el
    # harness ascendente nunca reproducía el orden de producción y por eso la
    # suite no detectó el bug de orden temporal (test_p0_time_order.py).
    return candles[::-1]


def test_ai_predict_e2e_flow():
    model = MagicMock()
    model.predict.return_value = (np.array([0.80, 0.18, 0.15, 0.15], dtype=np.float32), None)
    # Espacios Box REALES: si el bundle certificado declara vec_norm, el startup
    # construye DummyVecEnv(_DummyEnv(obs_space, action_space)) y SB3 valida los
    # spaces (un MagicMock falla con 'Unsupported structured space').
    # Bundle EURUSD activo = memoria (manifest obs_shape [20,25]) → mock = manifest.
    model.observation_space = Box(-np.inf, np.inf, shape=(20, 25), dtype=np.float32)
    model.action_space = Box(-1.0, 1.0, shape=(4,), dtype=np.float32)

    dist = MagicMock()
    dist.distribution.mean.numpy.return_value = np.array([0.1, 0.1, 0.1, 0.1], dtype=np.float32)
    dist.distribution.stddev.numpy.return_value = np.array([0.5, 0.5, 0.5, 0.5], dtype=np.float32)
    model.policy.get_distribution.return_value = dist

    with patch("main.init_pool", AsyncMock()), \
         patch("main.close_pool", AsyncMock()), \
         patch("routers.ai.is_news_blackout", AsyncMock(return_value=(False, None))), \
         patch("routers.ai.fetch_bars_since_close", AsyncMock(return_value=None)), \
         patch("routers.ai.get_pool", return_value=MagicMock(execute=AsyncMock())), \
         patch("routers.ai.PPO.load", return_value=model), \
         patch("services.news_scraper.get_macro_news", AsyncMock(return_value="No macro news")), \
         patch("httpx.AsyncClient", FakeAsyncClient):
        with TestClient(app) as client:
            response = client.post(
                "/api/v1/ai/predict",
                json={"symbol": "EURUSD", "timeframe": "H1", "position": 0},
                headers={"x-internal-token": "test-token", "x-cycle-id": "e2e-123"},
            )

            assert response.status_code == 200, response.text
            payload = response.json()
            assert payload["decision"] in {"BUY", "HOLD"}
            assert payload["llm_bias"] == "BULLISH"
            assert payload["quality_score"] >= 8.0
            assert payload["model_version"] == "v3"
            assert payload["volume"] is not None
            assert payload["sl_pips"] is not None
            assert payload["tp_pips"] is not None
            assert payload["sl_pips"] > 0
            assert payload["tp_pips"] > 0
            assert payload["tp_pips"] == payload["sl_pips"] * 2.0
            # P2-2: el volumen numérico sale del sizing delegado cableado con
            # el equity mockeado (1000), la vol_norm del modelo (acción[1]=0.18)
            # y settings — ni constante ni la heurística "3€" previa.
            expected_lots = size_ppo_position(
                volume_norm=0.18,
                sl_pips=payload["sl_pips"],
                equity=1000.0,
                pip_value=pip_value_per_lot("EURUSD", 1.1),
                risk_pct=settings.ppo_risk_pct,
                max_lot=settings.max_volume,
                min_lot=settings.min_volume,
            )
            assert abs(payload["volume"] - expected_lots) < 1e-9


def test_predict_memory_contract_adds_normalized_bars_since_close(monkeypatch):
    """Fase 3: bundle con memoria (20,25) → el obs lleva bars_since_close
    normalizado IGUAL que el env (min(1, bars/max_holding); None → 1.0);
    el bundle pre-memoria (20,24) sigue sin canal en el obs (contrato
    intacto). Desde P1 la consulta de velas desde el cierre se hace para
    TODOS los bundles: la necesita el veto de cooldown (H6), agnóstico
    del contrato del obs."""
    model_legacy = MagicMock()
    model_legacy.predict.return_value = (np.array([0.80, 0.18], dtype=np.float32), None)
    model_legacy.observation_space = Box(-np.inf, np.inf, shape=(20, 24), dtype=np.float32)
    model_legacy.action_space = Box(-1.0, 1.0, shape=(4,), dtype=np.float32)

    model_mem = MagicMock()
    model_mem.predict.return_value = (np.array([0.80, 0.18], dtype=np.float32), None)
    model_mem.observation_space = Box(-np.inf, np.inf, shape=(20, 25), dtype=np.float32)
    model_mem.action_space = Box(-1.0, 1.0, shape=(4,), dtype=np.float32)

    for m in (model_legacy, model_mem):
        dist = MagicMock()
        dist.distribution.mean.numpy.return_value = np.array([0.1, 0.1, 0.1, 0.1], dtype=np.float32)
        dist.distribution.stddev.numpy.return_value = np.array([0.5, 0.5, 0.5, 0.5], dtype=np.float32)
        m.policy.get_distribution.return_value = dist

    fetch_bars = AsyncMock(return_value=60)
    base_headers = {"x-internal-token": "test-token"}

    # El manifest real certifica el bundle de memoria [20,25] (promoción
    # 2026-10-07); este paso arranca con el modelo pre-memoria (20,24), así que
    # el manifest parcheado declara (a) obs_shape [20,24] y (b) SIN vec_norm:
    # el pkl real está guardado con obs (20,25) y SB3 (set_venv →
    # check_shape_equal) rechazaría un DummyVecEnv (20,24).
    legacy_manifest = {
        "schema_version": 1,
        "model_file": "ppo_trading_bot_v3.zip",
        "model_sha256": "test" * 16,
        "obs_shape": [20, 24],
        "vec_norm_file": None,
        "vec_norm_sha256": None,
        "features": None,
        "source": "test_legacy_contract",
        "extra": {},
    }

    with patch("main.init_pool", AsyncMock()), \
         patch("main.close_pool", AsyncMock()), \
         patch("services.model_registry.validate_bundle",
               return_value=legacy_manifest), \
         patch("routers.ai.is_news_blackout", AsyncMock(return_value=(False, None))), \
         patch("routers.ai.get_pool", return_value=MagicMock(execute=AsyncMock())), \
         patch("routers.ai.PPO.load", return_value=model_legacy), \
         patch("routers.ai.fetch_bars_since_close", fetch_bars), \
         patch("services.news_scraper.get_macro_news", AsyncMock(return_value="No macro news")), \
         patch("httpx.AsyncClient", FakeAsyncClient):
        with TestClient(app) as client:
            # 1) arranque con el bundle pre-memoria real → obs (20,24), sin
            #    canal en el obs (contrato intacto). La consulta de velas
            #    desde el cierre ocurre igualmente (P1: la necesita el veto
            #    de cooldown H6); bars=60 > CHURN_BARS → sin cooldown.
            r1 = client.post(
                "/api/v1/ai/predict",
                json={"symbol": "EURUSD", "timeframe": "H1", "position": 0},
                headers={**base_headers, "x-cycle-id": "e2e-mem-legacy"},
            )
            assert r1.status_code == 200, r1.text
            obs = model_legacy.predict.call_args[0][0]
            assert obs.shape == (20, 24)
            fetch_bars.assert_awaited_once()

            # 2) swap a un bundle con memoria (contrato (20,25)) → consulta
            #    las velas desde el cierre y normaliza como el env: 60/120
            monkeypatch.setitem(routers.ai._BRAINS, "EURUSD", Brain(
                model=model_mem, vec_norm=None, memory_channel=True,
                bundle={"model_sha256": "a" * 64, "vec_norm_sha256": None,
                        "source": "test", "loaded_at": "2026-10-01T00:00:00+00:00"},
            ))
            r2 = client.post(
                "/api/v1/ai/predict",
                json={"symbol": "EURUSD", "timeframe": "H1", "position": 0},
                headers={**base_headers, "x-cycle-id": "e2e-mem-25"},
            )
            assert r2.status_code == 200, r2.text
            assert fetch_bars.await_args.args[0] == "EURUSD"
            obs = model_mem.predict.call_args[0][0]
            assert obs.shape == (20, 25)
            np.testing.assert_allclose(obs[:, -1], 0.5)   # 60/120

            # 3) sin cierre registrado (None) → 1.0, idéntico al arranque del env
            fetch_bars.return_value = None
            r3 = client.post(
                "/api/v1/ai/predict",
                json={"symbol": "EURUSD", "timeframe": "H1", "position": 0},
                headers={**base_headers, "x-cycle-id": "e2e-mem-none"},
            )
            assert r3.status_code == 200, r3.text
            obs = model_mem.predict.call_args[0][0]
            np.testing.assert_allclose(obs[:, -1], 1.0)


def test_predict_mt5_down_returns_ok_hold_with_cycle_id():
    """P0-2: con MT5 caído, /predict debe devolver 200 + HOLD + cycle_id (nunca 500)."""
    with patch("main.init_pool", AsyncMock()), \
         patch("main.close_pool", AsyncMock()), \
         patch("routers.ai.is_news_blackout", AsyncMock(return_value=(False, None))), \
         patch("routers.ai.get_pool", return_value=MagicMock(execute=AsyncMock())), \
         patch("routers.ai.load_inference_artifacts"), \
         patch("httpx.AsyncClient", ConnectTimeoutAsyncClient):
        with TestClient(app) as client:
            response = client.post(
                "/api/v1/ai/predict",
                json={"symbol": "EURUSD", "timeframe": "H1", "position": 0},
                headers={"x-internal-token": "test-token", "x-cycle-id": "e2e-mt5-down"},
            )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["decision"] == "HOLD"
    assert payload["llm_bias"] == "ERROR"
    assert payload["cycle_id"]


def test_predict_holds_when_equity_unavailable():
    """P2-2: equity=0 → HOLD visible (antes: volumen 0 / orden inválida)."""
    class ZeroEquityClient(FakeAsyncClient):
        async def get(self, url, **kwargs):
            if "/api/v1/account/info" in url:
                return self._response({"equity": 0.0, "balance": 0.0})
            return await super().get(url, **kwargs)

    with patch("main.init_pool", AsyncMock()), \
         patch("main.close_pool", AsyncMock()), \
         patch("routers.ai.is_news_blackout", AsyncMock(return_value=(False, None))), \
         patch("routers.ai.get_pool", return_value=MagicMock(execute=AsyncMock())), \
         patch("httpx.AsyncClient", ZeroEquityClient):
        with TestClient(app) as client:
            response = client.post(
                "/api/v1/ai/predict",
                json={"symbol": "EURUSD", "timeframe": "H1", "position": 0},
                headers={"x-internal-token": "test-token", "x-cycle-id": "e2e-no-equity"},
            )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["decision"] == "HOLD"
    assert payload["llm_bias"] == "ERROR"
    assert payload["volume"] is None


def test_predict_holds_for_symbol_without_central_pip(monkeypatch):
    """P2-2: pip centralizado en position_sizing.PIP_SIZE — símbolo HABILITADO
    en PAIRS pero sin pip declarado → HOLD (antes: heurística de dígitos que
    inventaba 0.0001). El gate de mundo (P3-1b) lo deja pasar porque está
    configurado: la compuerta del pip existe de forma independiente."""
    from config import settings
    from routers import ai
    monkeypatch.setattr(settings, "pairs", [
        {"symbol": "EURUSD", "enabled": True},
        {"symbol": "XXXYZZ", "enabled": True},
    ])
    monkeypatch.setattr(ai, "_REGISTRY", None)   # rebuild con este PAIRS
    with patch("main.init_pool", AsyncMock()), \
         patch("main.close_pool", AsyncMock()), \
         patch("routers.ai.is_news_blackout", AsyncMock(return_value=(False, None))), \
         patch("routers.ai.get_pool", return_value=MagicMock(execute=AsyncMock())), \
         patch("routers.ai.load_inference_artifacts", MagicMock()), \
         patch("httpx.AsyncClient", FakeAsyncClient):
        with TestClient(app) as client:
            response = client.post(
                "/api/v1/ai/predict",
                json={"symbol": "XXXYZZ", "timeframe": "H1", "position": 0},
                headers={"x-internal-token": "test-token", "x-cycle-id": "e2e-bad-symbol"},
            )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["decision"] == "HOLD"
    assert payload["llm_bias"] == "ERROR"
    assert payload["volume"] is None


def test_predict_holds_during_news_blackout():
    """P2-3: ventana de noticias de alto impacto → HOLD (mismo mecanismo que
    el SMC), antes de tocar MT5/modelo/Qwen. llm_bias NEUTRAL (es política,
    no un fallo) y el motivo queda en quality_reason."""
    with patch("main.init_pool", AsyncMock()), \
         patch("main.close_pool", AsyncMock()), \
         patch("routers.ai.get_pool", return_value=MagicMock(execute=AsyncMock())), \
         patch("routers.ai.is_news_blackout", AsyncMock(return_value=(True, "Non-Farm Payrolls"))), \
         patch("httpx.AsyncClient", FakeAsyncClient):
        with TestClient(app) as client:
            response = client.post(
                "/api/v1/ai/predict",
                json={"symbol": "EURUSD", "timeframe": "H1", "position": 0},
                headers={"x-internal-token": "test-token", "x-cycle-id": "e2e-blackout"},
            )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["decision"] == "HOLD"
    assert payload["llm_bias"] == "NEUTRAL"
    assert payload["quality_reason"] == "news_blackout:Non-Farm Payrolls"
    assert payload["volume"] is None
    assert payload["sl_pips"] is None


def test_ea_backend_predict_contract_fields_are_aligned():
    payload = PredictResponse(
        cycle_id="contract-test",
        ml_prob=0.5,
        llm_bias="NEUTRAL",
        decision="HOLD",
        volume=0.01,
        sl_pips=15.0,
        tp_pips=30.0,
        model_version="v3",
        quality_score=7.5,
        quality_reason="ok",
        confidence_modifier=1.0,
        effective_prob=0.5,
        regime="TRENDING",
    )

    assert payload.cycle_id == "contract-test"
    assert payload.volume is not None
    assert payload.sl_pips is not None
    assert payload.tp_pips is not None


def test_ea_backend_trade_filled_contract_is_aligned():
    payload = {
        "symbol": "EURUSD",
        "entry_time": "1720000000",
        "exit_time": "1720003600",
        "pnl": 12.50,
        "pnl_pct": 0.125,
        "direction": "LONG",
        "sl_hit": False,
        "tp_hit": True,
        "exit_reason": "tp",
        "deal_ticket": 123456,
        "position_id": 789012,
    }

    request = TradeFilledRequest(**payload)

    assert request.symbol == "EURUSD"
    assert request.direction == "LONG"
    assert request.deal_ticket == 123456
    assert request.position_id == 789012


def test_v3_training_and_environment_share_the_same_feature_contract():
    training_features = tuple(FEATURE_NAMES)

    assert training_features == MARKET_FEATURES


def test_v3_training_and_inference_calculate_identical_features():
    candles = pd.DataFrame(_build_candles())

    training_features = engineer_market_features(candles)
    inference_features = engineer_market_features(candles)

    pd.testing.assert_frame_equal(
        training_features[list(MARKET_FEATURES)].reset_index(drop=True),
        inference_features[list(MARKET_FEATURES)].reset_index(drop=True),
    )


def test_v3_observation_encodes_flat_position_and_pads_market_features():
    market_values = np.ones((10, 10), dtype=np.float32)

    observation = build_v3_observation(
        market_values=market_values,
        expected_market_features=12,
        position=0,
        entry_price=1.2345,
        current_price=1.2345,
        sl_price=1.2145,
        tp_price=1.2545,
        current_atr=0.001,
        steps_in_trade=0,
        max_holding_steps=120,
        balance=10000.0,
        initial_balance=10000.0,
        window_size=10,
    )

    assert observation.shape == (10, 19)
    np.testing.assert_array_equal(observation[:10, :10], market_values)
    np.testing.assert_array_equal(observation[:10, 10], np.zeros(10))
    np.testing.assert_array_equal(observation[:10, 11], np.zeros(10))
    np.testing.assert_array_equal(observation[:10, 12], np.zeros(10))
    np.testing.assert_array_equal(observation[:10, 13], np.zeros(10))
    np.testing.assert_array_equal(observation[:10, 14], np.zeros(10))
    np.testing.assert_array_equal(observation[:10, 15], np.ones(10))
    np.testing.assert_array_equal(observation[:10, 16], np.zeros(10))
    np.testing.assert_array_equal(observation[:10, 17], np.zeros(10))
    np.testing.assert_array_equal(observation[:10, 18], np.zeros(10))


def test_v3_observation_encodes_short_position_and_truncates_market_features():
    market_values = np.arange(170, dtype=np.float32).reshape(10, 17)

    observation = build_v3_observation(
        market_values=market_values,
        expected_market_features=12,
        position=-1,
        entry_price=1.2,
        current_price=1.2,
        sl_price=1.22,
        tp_price=1.18,
        current_atr=0.001,
        steps_in_trade=0,
        max_holding_steps=120,
        balance=10000.0,
        initial_balance=10000.0,
        window_size=10,
    )

    assert observation.shape == (10, 19)
    np.testing.assert_array_equal(observation[:10, :12], market_values[:, :12])
    np.testing.assert_array_equal(observation[:10, 12], np.zeros(10))
    np.testing.assert_array_equal(observation[:10, 13], np.full(10, -1.0))
    np.testing.assert_array_equal(observation[:10, 14], np.zeros(10))
    np.testing.assert_array_equal(observation[:10, 15], np.ones(10))
    np.testing.assert_array_equal(observation[:10, 16], np.full(10, 5.0))
    np.testing.assert_array_equal(observation[:10, 17], np.full(10, 5.0))
    np.testing.assert_array_equal(observation[:10, 18], np.zeros(10))


def test_v3_direction_decoding_preserves_buy_sell_hold_mapping():
    assert decode_v3_direction(0.8, current_position=0) == (1, "BUY")
    assert decode_v3_direction(-0.8, current_position=0) == (-1, "SELL")
    assert decode_v3_direction(0.0, current_position=0) == (0, "FLAT")
    assert decode_v3_direction(0.8, current_position=1) == (1, "HOLD")
    assert decode_v3_direction(-0.8, current_position=-1) == (-1, "HOLD")


class TestP1TimeStopAndCooldownServe:
    """P1 (Félix 2026-10-08): time-stop serve + cooldown de re-entrada (H6).

    - time-stop: posición abierta ≥ 12 H1 = 144 pasos M5 (la unidad que mide
      el EA: (TimeCurrent−pTime)/PeriodSeconds(M5)) → `decision=CLOSE` con
      `time_stop=True` y `veto_reason=time_stop`: es una SALIDA mecánica,
      gana a toda la cadena Qwen/vetos.
    - cooldown: ≤ CHURN_BARS=2 velas H1 desde el último cierre de
      `trade_outcomes` → BUY/SELL revertido a HOLD (`churn_cooldown`):
      espejo serve del anti-churn del env entrenado.
    - audit_log (ai_predict_cycle) lleva `time_stop`, `churn_cooldown` y
      `bars_since_close` para que la traza distinga cada salida/veto."""

    @staticmethod
    def _model(direction=0.80, volume=0.18):
        model = MagicMock()
        model.predict.return_value = (
            np.array([direction, volume, 0.15, 0.15], dtype=np.float32), None,
        )
        # Bundle EURUSD activo = memoria (manifest obs_shape [20,25])
        model.observation_space = Box(-np.inf, np.inf, shape=(20, 25), dtype=np.float32)
        model.action_space = Box(-1.0, 1.0, shape=(4,), dtype=np.float32)
        dist = MagicMock()
        dist.distribution.mean.numpy.return_value = np.array([0.1, 0.1, 0.1, 0.1], dtype=np.float32)
        dist.distribution.stddev.numpy.return_value = np.array([0.5, 0.5, 0.5, 0.5], dtype=np.float32)
        model.policy.get_distribution.return_value = dist
        return model

    @staticmethod
    @contextmanager
    def _serve(model, bars=None):
        with patch("main.init_pool", AsyncMock()), \
             patch("main.close_pool", AsyncMock()), \
             patch("routers.ai.is_news_blackout", AsyncMock(return_value=(False, None))), \
             patch("routers.ai.fetch_bars_since_close", AsyncMock(return_value=bars)), \
             patch("routers.ai.get_pool", return_value=MagicMock(execute=AsyncMock())), \
             patch("routers.ai.PPO.load", return_value=model), \
             patch("services.news_scraper.get_macro_news", AsyncMock(return_value="No macro news")), \
             patch("routers.ai.log_to_audit", AsyncMock()) as audit, \
             patch("httpx.AsyncClient", FakeAsyncClient):
            with TestClient(app) as client:
                yield client, audit

    @staticmethod
    def _predict(client, body):
        payload = {"symbol": "EURUSD", "timeframe": "M5", **body}
        return client.post(
            "/api/v1/ai/predict",
            json=payload,
            headers={"x-internal-token": "test-token", "x-cycle-id": "p1-serve"},
        )

    @staticmethod
    def _cycle_audit(audit_mock):
        calls = [c for c in audit_mock.await_args_list if c.args[0] == "ai_predict_cycle"]
        assert calls, "sin traza ai_predict_cycle en audit_log"
        return calls[-1].args[1]

    def test_p1_boundaries_pin_confirmed_parameters(self):
        from ml.trading_env_v2 import CHURN_BARS, TIME_STOP_BARS
        from routers.ai import _TIME_STOP_M5_BARS

        assert CHURN_BARS == 2                    # fronteras 2/3 del cooldown
        assert TIME_STOP_BARS == 12               # 12 velas H1 = fronteras 143/144
        assert _TIME_STOP_M5_BARS == TIME_STOP_BARS * 12 == 144

    def test_time_stop_sends_close_at_144_m5_steps(self):
        with self._serve(self._model()) as (client, audit):
            response = self._predict(client, {
                "position": 1, "entry_price": 1.1000, "sl_price": 1.0985,
                "tp_price": 1.1030, "steps_in_trade": 144,
            })

        assert response.status_code == 200, response.text
        assert response.json()["decision"] == "CLOSE"
        trace = self._cycle_audit(audit)
        assert trace["time_stop"] is True
        assert trace["veto_reason"] == "time_stop"
        assert trace["churn_cooldown"] is False

    @pytest.mark.parametrize("steps_in_trade", [0, 143])
    def test_no_time_stop_below_threshold(self, steps_in_trade):
        with self._serve(self._model()) as (client, audit):
            response = self._predict(client, {
                "position": 1, "entry_price": 1.1000, "sl_price": 1.0985,
                "tp_price": 1.1030, "steps_in_trade": steps_in_trade,
            })

        assert response.status_code == 200, response.text
        assert response.json()["decision"] == "HOLD"   # modelo long + posición long
        trace = self._cycle_audit(audit)
        assert trace["time_stop"] is False
        assert trace["veto_reason"] is None

    @pytest.mark.parametrize("bars", [0, 1, 2])
    def test_cooldown_blocks_entry_within_churn_window(self, bars):
        assert bars <= 2                            # frontera = CHURN_BARS
        with self._serve(self._model(), bars=bars) as (client, audit):
            response = self._predict(client, {"position": 0, "steps_in_trade": 0})

        assert response.status_code == 200, response.text
        assert response.json()["decision"] == "HOLD"
        trace = self._cycle_audit(audit)
        assert trace["churn_cooldown"] is True
        assert trace["veto_reason"] == "churn_cooldown"
        assert trace["bars_since_close"] == bars

    @pytest.mark.parametrize("bars", [3, None])
    def test_cooldown_releases_outside_window_or_without_history(self, bars):
        with self._serve(self._model(), bars=bars) as (client, audit):
            response = self._predict(client, {"position": 0, "steps_in_trade": 0})

        assert response.status_code == 200, response.text
        assert response.json()["decision"] == "BUY"
        trace = self._cycle_audit(audit)
        assert trace["churn_cooldown"] is False
        assert trace["veto_reason"] is None

    def test_veto_reason_time_stop_and_churn_are_authority(self):
        from routers.ai import _veto_reason

        ranging = {"regime": "RANGING", "quality": 9.0, "bias": "NEUTRAL"}
        # La salida por time-stop va por delante de cualquier veto de Qwen
        assert _veto_reason("HOLD", "CLOSE", ranging, time_stop=True) == "time_stop"
        # El cooldown va por delante del ranging INFERIDO (final=HOLD+RANGING)
        assert _veto_reason("BUY", "HOLD", ranging, churn_rejected=True) == "churn_cooldown"
        # Sin flags: el inferido sigue siendo la autoridad (sin regresión)
        assert _veto_reason("BUY", "HOLD", ranging) == "ranging_veto"
