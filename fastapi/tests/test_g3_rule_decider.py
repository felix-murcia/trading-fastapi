"""
G3 (2026-10-20) · E4 · EL DECISOR POR REGLA DE USDCHF.

Contrato:
  1. `services/rule_decider.momentum24_signal` devuelve la dirección del
     momentum de 24 h y `None` si no hay señal (historia corta o movimiento
     plano ⇒ fail-closed, nunca una dirección inventada);
  2. un par con `decision_source="momentum24"` decide con la REGLA y NO
     necesita bundle PPO;
  3. sin señal ⇒ HOLD (nunca FLAT/CLOSE: la regla no cierra por falta de
     señal);
  4. EURUSD (decision_source="ppo") NO cambia: sigue con su bundle.
"""
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.test_e2e_ai_predict import ConnectTimeoutAsyncClient
from tests.test_p31b_pair_gate import TOKEN
from services.rule_decider import (
    RULE_MOMENTUM24_ACC,
    RULE_MOMENTUM24_BASE,
    momentum24_signal,
)


class TestMomentum24Signal:
    def test_up_and_down(self):
        up = [1.0 + i * 0.0005 for i in range(30)]
        assert momentum24_signal(up)[0] == 1
        down = [1.0 - i * 0.0005 for i in range(30)]
        assert momentum24_signal(down)[0] == -1

    def test_flat_market_has_no_signal(self):
        flat = [0.8000] * 40
        assert momentum24_signal(flat) is None, (
            "sin movimiento no hay dirección que capturar (fail-closed)"
        )

    def test_insufficient_history_has_no_signal(self):
        assert momentum24_signal([1.0, 1.1]) is None

    def test_measured_parameters_are_declared(self):
        """La prob que se publica es la MEDICIÓN, no un número inventado."""
        assert RULE_MOMENTUM24_ACC == 0.574
        assert RULE_MOMENTUM24_BASE == 0.503
        assert (RULE_MOMENTUM24_ACC - RULE_MOMENTUM24_BASE) == pytest.approx(0.071)


class TestPairConfigDecisionSource:
    def test_default_is_ppo(self):
        from core.pair import PairConfig

        cfg = PairConfig.from_spec({"symbol": "EURUSD", "enabled": True})
        assert cfg.decision_source == "ppo"

    def test_momentum24_is_accepted(self):
        from core.pair import PairConfig

        cfg = PairConfig.from_spec(
            {"symbol": "USDCHF", "enabled": False, "decision_source": "momentum24"}
        )
        assert cfg.decision_source == "momentum24"

    def test_unknown_source_is_rejected(self):
        from core.pair import PairConfig

        with pytest.raises(ValueError, match="decision_source"):
            PairConfig.from_spec(
                {"symbol": "USDCHF", "enabled": False, "decision_source": "vibes"}
            )


class TestUsdChfCurrencyConvention:
    """G3: USDCHF es base=USD / quote=CHF ⇒ su convención de P/L es
    DISTINTA de la de GBPUSD (quote=USD). Chequeo de cordura: el P/L
    esperado de 20 pips × 0,01 lot debe ser comparable al de EURUSD
    (si la convención estuviera mal, saldría ~13,6 % desviado)."""

    def test_expected_close_eur_is_comparable_across_pairs(self):
        from services.position_sizing import expected_close_eur

        eur = expected_close_eur("EURUSD", 20, 0.01, 1.1234, eurusd_price=1.1234)
        chf = expected_close_eur("USDCHF", 20, 0.01, 0.8800, eurusd_price=1.1234)
        assert 1.5 < eur < 2.1, eur
        assert 1.8 < chf < 2.8, chf
        # la desviación entre pares es sólo el precio (no un factor de 100)
        assert 0.8 < chf / eur < 1.6, (chf, eur)

    def test_pip_value_of_usdchf_is_the_fixed_usd_notional(self):
        from services.position_sizing import pip_value_per_lot

        assert pip_value_per_lot("USDCHF", 0.88) == 10.0
        assert pip_value_per_lot("USDCHF", 0.90) == 10.0


class TestServeRulePath:
    """El serve con un par por regla: decide la regla y no pide bundle."""

    @staticmethod
    def _usdchf_candles(n: int = 130, step: float = 0.0005):
        """Serie ASCENDENTE con momentum de 24 h claramente al alza."""
        import pandas as pd
        close = [0.8000 + i * step for i in range(n)]
        ts = pd.date_range("2026-10-19", periods=n, freq="h", tz="UTC")
        return [
            {"time": t.strftime("%Y-%m-%dT%H:%M:%SZ"), "open": c,
             "high": c + 0.0002, "low": c - 0.0002, "close": c,
             "tick_volume": 1000}
            for t, c in zip(ts, close)
        ][::-1]   # el MCP devuelve descendente

    def _client(self, candles):
        import json
        from unittest.mock import MagicMock

        class _C:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            @staticmethod
            def _resp(payload):
                r = MagicMock()
                r.status_code = 200
                r.json.return_value = payload
                r.text = json.dumps(payload)
                return r

            async def get(self, url, **kw):
                if "/api/v1/account/info" in url:
                    return self._resp({"equity": 1000.0, "balance": 1000.0})
                if "/api/v1/market/candles/latest" in url:
                    return self._resp(candles)
                if "/api/v1/positions" in url:
                    return self._resp({"open": [], "pending": []})
                return self._resp({})

            async def post(self, url, **kw):
                if "/v1/chat/completions" in url:
                    return self._resp({"choices": [{"message": {"content": json.dumps({
                        "quality": 8.0, "reason": "ADX 26 trend strong",
                        "bias": "BULLISH", "confidence_modifier": 1.0,
                        "regime": "TRENDING", "bias_prob": 0.6,
                    })}}]})
                return self._resp({})

        return _C

    @pytest.mark.asyncio
    async def test_rule_decides_without_a_bundle(self, monkeypatch):
        from config import settings
        from routers import ai

        monkeypatch.setattr(settings, "pairs", [
            {"symbol": "EURUSD", "enabled": True},
            {"symbol": "USDCHF", "enabled": True, "decision_source": "momentum24",
             "daily_loss_pct": 2.0, "equity_allocation_pct": 20},
        ])
        ai._REGISTRY = None
        ai._BRAINS.pop("USDCHF", None)
        client = self._client(self._usdchf_candles())

        with patch("main.init_pool", AsyncMock()), \
             patch("main.close_pool", AsyncMock()), \
             patch("routers.ai.is_news_blackout", AsyncMock(return_value=(False, None))), \
             patch("routers.ai.fetch_bars_since_close", AsyncMock(return_value=None)), \
             patch("routers.ai.get_pool", return_value=MagicMock(execute=AsyncMock())), \
             patch("services.news_scraper.get_macro_news", AsyncMock(return_value="No macro news")), \
             patch("httpx.AsyncClient", client):
            from fastapi.testclient import TestClient
            from main import app
            with TestClient(app) as c:
                r = c.post("/api/v1/ai/predict",
                           json={"symbol": "USDCHF", "timeframe": "M5", "position": 0},
                           headers=TOKEN)
        assert r.status_code == 200, r.text
        payload = r.json()
        assert payload["decision"] in ("BUY", "HOLD"), payload
        if payload["decision"] == "BUY":
            assert payload["ml_prob"] == pytest.approx(RULE_MOMENTUM24_ACC)

    @pytest.mark.asyncio
    async def test_eurusd_still_uses_its_ppo_bundle(self, monkeypatch):
        """El mundo vivo NO cambia: sin bundle ⇒ HOLD (como siempre)."""
        from config import settings
        from routers import ai

        monkeypatch.setattr(settings, "pairs", [
            {"symbol": "EURUSD", "enabled": True},
            {"symbol": "USDCHF", "enabled": True, "decision_source": "momentum24"},
        ])
        ai._REGISTRY = None
        ai._BRAINS.pop("EURUSD", None)
        with patch("main.init_pool", AsyncMock()), \
             patch("main.close_pool", AsyncMock()), \
             patch("routers.ai.load_inference_artifacts", MagicMock()), \
             patch("httpx.AsyncClient", ConnectTimeoutAsyncClient):
            from fastapi.testclient import TestClient
            from main import app
            with TestClient(app) as c:
                r = c.post("/api/v1/ai/predict",
                           json={"symbol": "EURUSD", "timeframe": "M5", "position": 0},
                           headers=TOKEN)
        assert r.status_code == 200, r.text
        # sin bundle, EURUSD ⇒ HOLD con el error declarado (nunca una regla)
        assert r.json()["decision"] == "HOLD"
        assert r.json()["llm_bias"] == "ERROR"
