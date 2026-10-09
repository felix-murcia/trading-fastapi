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


class QwenReentryRejectClient(FakeAsyncClient):
    """R1: Qwen (cerebro sano) que rechaza la re-entrada — `reentry_ok=false`.
    Calidad 8.5 + bias BULLISH para que el ÚNICO veto posible en ventana
    sea el juicio de re-entrada (ni calidad ni bias compiten)."""

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
                            "reentry_ok": False,
                        })
                    }
                }]
            })
        return self._response({})


# Último cierre de ejemplo para el contexto de re-entrada (R1) — régimen de
# la casuística vivida en el trade 251: cierre del modelo tras 10 min.
_LAST_EXIT = {
    "exit_time": "2026-10-09T13:10:00+00:00",
    "exit_reason": "ea",
    "pnl": -0.08,
    "direction": "SHORT",
    "is_win": False,
    "price_move_since_close_pips": 2.5,
}


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


class _ServeHarness:
    """Harness de serve (TestClient + Qwen fake + MT5 fake) compartido por
    las suites P1 (time-stop/cooldown) y P0 (gate de confianza). No se
    recolecta como tests (no empieza por `Test`)."""

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
    def _serve(model, bars=None, last_exit=None, qwen_client=FakeAsyncClient):
        with patch("main.init_pool", AsyncMock()), \
             patch("main.close_pool", AsyncMock()), \
             patch("routers.ai.is_news_blackout", AsyncMock(return_value=(False, None))), \
             patch("routers.ai.fetch_bars_since_close", AsyncMock(return_value=bars)), \
             patch("routers.ai.fetch_last_exit", AsyncMock(return_value=last_exit)), \
             patch("routers.ai.get_pool", return_value=MagicMock(execute=AsyncMock())), \
             patch("routers.ai.PPO.load", return_value=model), \
             patch("services.news_scraper.get_macro_news", AsyncMock(return_value="No macro news")), \
             patch("routers.ai.log_to_audit", AsyncMock()) as audit, \
             patch("httpx.AsyncClient", qwen_client):
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


class TestP1TimeStopAndServeReentry(_ServeHarness):
    """P1 (Félix 2026-10-08) + R1 (Félix 2026-10-09): time-stop serve y
    re-entrada juzgada por el CEREBRO.

    - time-stop: posición abierta ≥ 12 H1 = 144 pasos M5 (la unidad que mide
      el EA: (TimeCurrent−pTime)/PeriodSeconds(M5)) → `decision=CLOSE` con
      `time_stop=True` y `veto_reason=time_stop`: es una SALIDA mecánica,
      gana a toda la cadena Qwen/vetos.
    - R1 (`reentry_veto`): el veto duro `churn_cooldown` (constante de
      2 velas «grabada a fuego») se sustituyó por el juicio contextual de
      Qwen (`reentry_ok`) con los datos del cierre reciente. La VENTANA
      sigue siendo ≤ CHURN_BARS=2 velas H1 desde el último cierre de
      `trade_outcomes` (paridad con la fricción anti-churn del env); lo que
      cambia es QUIÉN decide: reentry_ok=false → HOLD con
      `veto_reason=reentry_veto`.
    - Qwen degradado → red de emergencia documentada: sólo se bloquea la
      1ª vela (bars=0); en velas tardías el juicio deja pasar y el
      fail-closed de calidad (quality=0) mantiene el HOLD.
    - audit_log (ai_predict_cycle) lleva `time_stop`, `reentry_window`
      (condición cruda, continuidad de medición con el antiguo
      `churn_cooldown`), `reentry_ok` (juicio del cerebro; None = no
      evaluado) y `bars_since_close`."""

    def test_p1_boundaries_pin_confirmed_parameters(self):
        from ml.trading_env_v2 import CHURN_BARS, TIME_STOP_BARS
        from routers.ai import _M5_PER_H1

        assert CHURN_BARS == 2                    # frontera 2/3 de la ventana R1
        assert TIME_STOP_BARS == 12               # 12 velas H1 = fronteras 143/144
        assert _M5_PER_H1 == 12                   # conversión edad EA (M5) → H1
        assert TIME_STOP_BARS * _M5_PER_H1 == 144 # frontera en pasos M5 que mide el EA

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
        assert trace["reentry_window"] is False   # posición abierta → sin ventana

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

    def test_holding_norm_converts_m5_steps_to_h1_domain(self, monkeypatch):
        """holding_norm (fix de unidades, Félix 2026-10-08): el EA mide
        `steps_in_trade` en pasos M5 y el canal lo normaliza contra
        max_holding_steps=120 H1 → serve convierte M5→H1 (//_M5_PER_H1)
        ANTES de build_v3_observation. 60 M5 (1 h) → 5/120 ≈ 0.042; sin la
        conversión el canal daba 0.5 a la 1.ª hora y saturaba a 1.0 a las 2 h,
        mientras el env entrena con barras H1 (paridad restaurada)."""
        from routers.ai import Brain

        model = self._model()
        with self._serve(model) as (client, audit):
            # Brain SIN VecNormalize → assertamos el obs CRUDO que construye
            # serve (el contrato de unidades, no las stats de normalización).
            monkeypatch.setitem(routers.ai._BRAINS, "EURUSD", Brain(
                model=model, vec_norm=None, memory_channel=True,
                bundle={"model_sha256": "b" * 64, "vec_norm_sha256": None,
                        "source": "test", "loaded_at": "2026-10-08T00:00:00+00:00"},
            ))
            response = self._predict(client, {
                "position": 1, "entry_price": 1.1000, "sl_price": 1.0985,
                "tp_price": 1.1030, "steps_in_trade": 60,
            })

        assert response.status_code == 200, response.text
        obs = model.predict.call_args[0][0]
        # 17 market features + holding_norm (6º canal de estado) = col 23
        assert obs[0, 23] == pytest.approx(5 / 120)
        trace = self._cycle_audit(audit)
        assert trace["steps_in_trade_h1"] == 5       # traza en el dominio entrenado
        assert trace["time_stop"] is False           # 5 < 12: aún lejos del cierre

    @pytest.mark.parametrize("bars", [0, 1, 2])
    def test_brain_vetoes_reentry_within_window(self, bars):
        """R1: dentro de ventana (≤ CHURN_BARS) decide el CEREBRO — Qwen
        responde `reentry_ok=false` (churn) → HOLD con `reentry_veto`."""
        assert bars <= 2                            # frontera = CHURN_BARS
        with self._serve(
            self._model(), bars=bars, last_exit=_LAST_EXIT,
            qwen_client=QwenReentryRejectClient,
        ) as (client, audit):
            response = self._predict(client, {"position": 0, "steps_in_trade": 0})

        assert response.status_code == 200, response.text
        assert response.json()["decision"] == "HOLD"
        trace = self._cycle_audit(audit)
        assert trace["reentry_window"] is True
        assert trace["reentry_ok"] is False
        assert trace["veto_reason"] == "reentry_veto"
        assert trace["bars_since_close"] == bars

    @pytest.mark.parametrize("bars", [0, 2])
    def test_brain_approves_reentry_within_window(self, bars):
        """R1: el cerebro puede APROBAR la re-entrada — `reentry_ok=true`
        (o ausente → fail-open) → la entrada adelanta sin veto. Sin esta
        pieza el veto duro antiguo bloqueaba aquí siempre."""
        with self._serve(
            self._model(), bars=bars, last_exit=_LAST_EXIT,
        ) as (client, audit):
            response = self._predict(client, {"position": 0, "steps_in_trade": 0})

        assert response.status_code == 200, response.text
        assert response.json()["decision"] == "BUY"
        trace = self._cycle_audit(audit)
        assert trace["reentry_window"] is True
        assert trace["reentry_ok"] is True
        assert trace["veto_reason"] is None

    @pytest.mark.parametrize("bars", [3, None])
    def test_no_reentry_judgment_outside_window_or_without_history(self, bars):
        """Fuera de ventana (> CHURN_BARS) o sin cierre: no se juzga nada
        (`reentry_window=False`, `reentry_ok=None`) y no se consulta el
        último cierre."""
        with self._serve(self._model(), bars=bars) as (client, audit):
            response = self._predict(client, {"position": 0, "steps_in_trade": 0})

        assert response.status_code == 200, response.text
        assert response.json()["decision"] == "BUY"
        trace = self._cycle_audit(audit)
        assert trace["reentry_window"] is False
        assert trace["reentry_ok"] is None
        assert trace["veto_reason"] is None

    @pytest.mark.parametrize("bars,expected_veto,expected_ok", [
        (0, "reentry_veto", False),   # 1ª vela: red de emergencia (bars=0)
        (2, "quality_veto", True),    # vela tardía: pasa el juicio; calidad=0 (fail-closed) bloquea
    ])
    def test_degraded_brain_safe_default(self, bars, expected_veto, expected_ok):
        """R1: Qwen degradado → safe default documentado: sólo se bloquea
        la 1ª vela tras el cierre; el resto del HOLD lo garantiza el
        fail-closed de calidad (quality=0). Nunca silencioso: WARNING
        `REENTRY-DEFAULT` en log + `qwen_degraded` en audit."""
        from services.qwen_client import _degraded_unified

        degraded = _degraded_unified("TRENDING", 0.20, 0.40, 0.33, "Qwen unavailable")
        with patch("routers.ai.query_qwen_unified", AsyncMock(return_value=degraded)), \
             self._serve(self._model(), bars=bars, last_exit=_LAST_EXIT) as (client, audit):
            response = self._predict(client, {"position": 0, "steps_in_trade": 0})

        assert response.status_code == 200, response.text
        assert response.json()["decision"] == "HOLD"
        trace = self._cycle_audit(audit)
        assert trace["reentry_window"] is True
        assert trace["reentry_ok"] is expected_ok
        assert trace["veto_reason"] == expected_veto
        assert trace["qwen_degraded"] is True

    def test_veto_reason_time_stop_and_reentry_are_authority(self):
        from routers.ai import _veto_reason

        ranging = {"regime": "RANGING", "quality": 9.0, "bias": "NEUTRAL"}
        # La salida por time-stop va por delante de cualquier veto de Qwen
        assert _veto_reason("HOLD", "CLOSE", ranging, time_stop=True) == "time_stop"
        # El veto de re-entrada va por delante del ranging INFERIDO
        # (final=HOLD+RANGING): es la razón más específica del ciclo
        assert _veto_reason("BUY", "HOLD", ranging, reentry_rejected=True) == "reentry_veto"
        # Sin flags: el inferido sigue siendo la autoridad (sin regresión)
        assert _veto_reason("BUY", "HOLD", ranging) == "ranging_veto"


class TestP0EntryConfidenceGate(_ServeHarness):
    """P0 / H1 (Félix 2026-10-09): GATE DE CONFIANZA DE ENTRADA — construido
    DESARMADO (`settings.min_entry_confidence = 0.0` → no-op) por la evidencia
    pre-registrada en README (muestra n=18, trades 229–250): las bandas de
    confianza son INVERSIAS (<0.5 = +4.58 €/trade, ≥0.7 = −1.89 €/trade) y un
    gate armado habría restado dinero esa semana. Decisión de Félix:
    «mecanismo listo, sin armar» — revisión pre-registrada a N≥50 trades
    post-fix Qwen (conf_mod ya variable) antes de considerar armarlo.

    - Armado (env MIN_ENTRY_CONFIDENCE / monkeypatch): propuesta BUY/SELL con
      `effective_prob` < umbral → HOLD con `veto_reason=low_confidence`,
      flag `low_confidence` + snapshot del umbral (`min_entry_confidence`) en
      audit (habilita el replay N≥50 sin recomputar).
    - Prioridad mecánica > calidad: time-stop > position > reentry > gate >
      ranging/quality/bias (el gate solo se evalúa con BUY/SELL propuestos).
    - Solo entradas: ciclos FLAT/HOLD/CLOSE no tocan el flag."""

    def test_default_threshold_is_disarmed_noop(self, monkeypatch):
        """Umbral 0.0 (default): propuesta de confianza BAJA (eff 0.33)
        se adelanta igual — el mecanismo existe pero NO opera."""
        monkeypatch.setattr(settings, "min_entry_confidence", 0.0)
        with self._serve(self._model(direction=0.30)) as (client, audit):
            response = self._predict(client, {"position": 0, "steps_in_trade": 0})

        assert response.status_code == 200, response.text
        assert response.json()["decision"] == "BUY"
        trace = self._cycle_audit(audit)
        assert trace["low_confidence"] is False
        assert trace["veto_reason"] is None
        assert trace["min_entry_confidence"] == 0.0

    def test_armed_gate_blocks_low_confidence_entry(self, monkeypatch):
        """Umbral 0.5 armado: direction 0.30 → eff 0.33 < 0.5 → HOLD con
        `low_confidence` (flag + label + snapshot del umbral en audit)."""
        monkeypatch.setattr(settings, "min_entry_confidence", 0.5)
        with self._serve(self._model(direction=0.30)) as (client, audit):
            response = self._predict(client, {"position": 0, "steps_in_trade": 0})

        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["decision"] == "HOLD"
        assert payload["effective_prob"] == pytest.approx(0.33, abs=0.01)
        trace = self._cycle_audit(audit)
        assert trace["low_confidence"] is True
        assert trace["veto_reason"] == "low_confidence"
        assert trace["min_entry_confidence"] == 0.5

    def test_armed_gate_passes_confident_entry(self, monkeypatch):
        """Umbral 0.5 armado: direction 0.80 → eff 0.88 ≥ 0.5 → BUY pasa
        sin flag (el gate no degrada entradas sanas)."""
        monkeypatch.setattr(settings, "min_entry_confidence", 0.5)
        with self._serve(self._model(direction=0.80)) as (client, audit):
            response = self._predict(client, {"position": 0, "steps_in_trade": 0})

        assert response.status_code == 200, response.text
        assert response.json()["decision"] == "BUY"
        trace = self._cycle_audit(audit)
        assert trace["low_confidence"] is False
        assert trace["veto_reason"] is None

    def test_gate_ignores_cycles_without_entry_proposal(self, monkeypatch):
        """FLAT (direction 0.0 → eff 0.11 < umbral 0.5) sin propuesta de
        entrada: el gate es de ENTRADAS — no inventa veto donde no hay
        nada que bloquear."""
        monkeypatch.setattr(settings, "min_entry_confidence", 0.5)
        with self._serve(self._model(direction=0.0)) as (client, audit):
            response = self._predict(client, {"position": 0, "steps_in_trade": 0})

        assert response.status_code == 200, response.text
        trace = self._cycle_audit(audit)
        assert trace["low_confidence"] is False
        assert trace["veto_reason"] is None

    def test_time_stop_wins_over_armed_gate(self, monkeypatch):
        """Mecánica > calidad: posición ≥ 12 H1 → CLOSE aunque eff (0.88)
        esté por debajo del umbral armado (0.9) — el gate no toca salidas."""
        monkeypatch.setattr(settings, "min_entry_confidence", 0.9)
        with self._serve(self._model(direction=0.80)) as (client, audit):
            response = self._predict(client, {
                "position": 1, "entry_price": 1.1000, "sl_price": 1.0985,
                "tp_price": 1.1030, "steps_in_trade": 144,
            })

        assert response.status_code == 200, response.text
        assert response.json()["decision"] == "CLOSE"
        trace = self._cycle_audit(audit)
        assert trace["low_confidence"] is False
        assert trace["veto_reason"] == "time_stop"

    def test_reentry_veto_wins_over_armed_gate(self, monkeypatch):
        """Orden de la cadena: position > reentry > gate > ranging/quality.
        Con el cerebro rechazando la re-entrada Y eff bajo, el label es
        `reentry_veto` (el juicio de re-entrada opera primero)."""
        monkeypatch.setattr(settings, "min_entry_confidence", 0.5)
        with self._serve(
            self._model(direction=0.30), bars=0, last_exit=_LAST_EXIT,
            qwen_client=QwenReentryRejectClient,
        ) as (client, audit):
            response = self._predict(client, {"position": 0, "steps_in_trade": 0})

        assert response.status_code == 200, response.text
        assert response.json()["decision"] == "HOLD"
        trace = self._cycle_audit(audit)
        assert trace["reentry_window"] is True
        assert trace["reentry_ok"] is False
        assert trace["low_confidence"] is False
        assert trace["veto_reason"] == "reentry_veto"

    def test_veto_reason_priority_mechanical_beats_gate_beats_inferred(self):
        from routers.ai import _veto_reason

        ranging = {"regime": "RANGING", "quality": 9.0, "bias": "NEUTRAL"}
        # El gate (calidad explícita operada) gana a los inferidos de Qwen
        assert _veto_reason("BUY", "HOLD", ranging, low_confidence=True) == "low_confidence"
        # ...pero pierde ante lo mecánico: time-stop > position > reentry
        assert _veto_reason(
            "BUY", "HOLD", ranging, low_confidence=True, reentry_rejected=True,
        ) == "reentry_veto"
        assert _veto_reason(
            "BUY", "HOLD", ranging, low_confidence=True, position_rejected=True,
        ) == "position_veto"
        assert _veto_reason(
            "BUY", "CLOSE", ranging, low_confidence=True, time_stop=True,
        ) == "time_stop"

    def test_config_min_entry_confidence_default_and_range(self, monkeypatch):
        """Setting P0: default 0.0 (desarmado) y rango [0,1] validado."""
        from pydantic import ValidationError

        from config import Settings

        monkeypatch.delenv("MIN_ENTRY_CONFIDENCE", raising=False)
        monkeypatch.delenv("min_entry_confidence", raising=False)
        assert Settings().min_entry_confidence == 0.0
        with pytest.raises(ValidationError):
            Settings(min_entry_confidence=1.5)
        with pytest.raises(ValidationError):
            Settings(min_entry_confidence=-0.1)
