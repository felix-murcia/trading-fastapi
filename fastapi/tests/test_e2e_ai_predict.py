import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import numpy as np
import pandas as pd
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
from routers.ai import PredictRequest, PredictResponse, TradeFilledRequest
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
    model.observation_space = Box(-np.inf, np.inf, shape=(20, 24), dtype=np.float32)
    model.action_space = Box(-1.0, 1.0, shape=(4,), dtype=np.float32)

    dist = MagicMock()
    dist.distribution.mean.numpy.return_value = np.array([0.1, 0.1, 0.1, 0.1], dtype=np.float32)
    dist.distribution.stddev.numpy.return_value = np.array([0.5, 0.5, 0.5, 0.5], dtype=np.float32)
    model.policy.get_distribution.return_value = dist

    with patch("main.init_pool", AsyncMock()), \
         patch("main.close_pool", AsyncMock()), \
         patch("routers.ai.is_news_blackout", AsyncMock(return_value=(False, None))), \
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
