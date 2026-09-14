import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pandas as pd
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parents[1] / "ml"))

from ml.trading_env_v2 import (
    MARKET_FEATURES,
    build_v3_observation,
    decode_v3_direction,
    engineer_market_features,
)
from ml.train_ppo_v3 import FEATURE_NAMES
from main import app
from routers.ai import PredictRequest, PredictResponse, TradeFilledRequest


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
    return candles


def test_ai_predict_e2e_flow():
    model = MagicMock()
    model.predict.return_value = (np.array([0.80, 0.18, 0.15, 0.15], dtype=np.float32), None)
    model.observation_space.shape = (10, 21)

    dist = MagicMock()
    dist.distribution.mean.numpy.return_value = np.array([0.1, 0.1, 0.1, 0.1], dtype=np.float32)
    dist.distribution.stddev.numpy.return_value = np.array([0.5, 0.5, 0.5, 0.5], dtype=np.float32)
    model.policy.get_distribution.return_value = dist

    with patch("main.init_pool", AsyncMock()), \
         patch("main.close_pool", AsyncMock()), \
         patch("routers.ai.get_pool", return_value=MagicMock(execute=AsyncMock())), \
         patch("routers.ai.os.path.exists", side_effect=lambda path: path == "/app/ml/ppo_trading_bot_v3.zip"), \
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


def test_ea_backend_predict_contract_fields_are_aligned():
    payload = PredictResponse(
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
