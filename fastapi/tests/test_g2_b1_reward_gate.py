"""
G2/B1 (orden Félix 2026-10-20: «continúa con GBPUSD hasta el final») —
gate de recompensa mínima por símbolo.

BUG (G2-0, gap B1): `expected_close_eur` era EURUSD-only ⇒ para GBPUSD
lanzaba ValueError ⇒ el gate de recompensa (3 €) **crasheaba el ciclo de
GBPUSD** (predict 500) ⇒ GBPUSD no podía operar en ninguna entrada con TP.

FIX: la conversión USD→EUR se extiende a los pares con quote=USD y base≠EUR
(GBPUSD, AUDUSD, NZDUSD, XAUUSD) con el MISMO convenio que el bróker y que
el risk_guardian (G2-5/D2): el P/L en USD se divide por el precio EURUSD.
Sin ese precio NO hay conversión honesta ⇒ ValueError (ruidoso, nunca
silencioso) — no se inventan tasas.
"""
import pytest

from services.position_sizing import expected_close_eur, pip_value_per_lot


class TestExpectedCloseEur:
    def test_eurusd_unchanged(self):
        """EURUSD: el comportamiento byte-idéntico (el trade #255: 21,6 p ×
        0,01 lot a 1,12094 → 1,927 € calculado vs 1,93 € pagados)."""
        v = expected_close_eur("EURUSD", 21.6, 0.01, 1.12094)
        assert 1.92 < v < 1.94, v

    def test_gbpusd_uses_eurusd_conversion(self):
        """GBPUSD: P/L en USD (pip=10 USD/lot) convertido vía EURUSD."""
        eurusd = 1.1212
        v = expected_close_eur("GBPUSD", 20.0, 0.01, 1.33450, eurusd_price=eurusd)
        usd = 20.0 * 0.01 * pip_value_per_lot("GBPUSD", 1.3345)   # = 2,00 USD
        assert abs(v - usd / eurusd) < 1e-9, (v, usd / eurusd)
        assert 1.7 < v < 1.9, v

    def test_gbpusd_without_eurusd_fails_loudly(self):
        """Sin el precio EURUSD no hay conversión honesta ⇒ ValueError."""
        with pytest.raises(ValueError, match="EURUSD"):
            expected_close_eur("GBPUSD", 20.0, 0.01, 1.33450)

    def test_unknown_symbol_still_fails(self):
        """Un símbolo sin conversión honesta sigue fallando (nada inventado)."""
        with pytest.raises(ValueError):
            expected_close_eur("USDJPY", 20.0, 0.01, 150.0, eurusd_price=1.12)

    def test_invalid_inputs_fail(self):
        for args in ((0.0, 0.01, 1.1), (20.0, 0.0, 1.1), (20.0, 0.01, 0.0)):
            with pytest.raises(ValueError):
                expected_close_eur("EURUSD", *args)


class TestRewardGatePerSymbol:
    """El gate de recompensa mínima (3 €) no debe romper el ciclo de GBPUSD."""

    @pytest.mark.asyncio
    async def test_predict_gbpusd_does_not_crash(self, monkeypatch):
        """Un ciclo GBPUSD con entrada propuesta NO revienta (el bug B1)."""
        import numpy as np
        from fastapi.testclient import TestClient
        from main import app
        from services import qwen_client
        from services.qwen_client import QwenHealthState
        from tests.test_qwen_semantics import _mock_ppo_model

        model = _mock_ppo_model()
        model.predict.return_value = (np.array([-0.8, 0.18, 0.15, 0.15], dtype=np.float32), None)
        monkeypatch.setattr(qwen_client, "_QWEN_STATE", QwenHealthState())

        class _FakeClient:
            def __init__(self, *a, **k):
                self.timeout = k.get("timeout")

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            def _r(self, payload):
                import json
                r = type("R", (), {})()
                r.status_code = 200
                r.json = lambda: payload
                r.text = json.dumps(payload)
                r.raise_for_status = lambda: None
                return r

            async def get(self, url, **kwargs):
                if "/account/info" in url:
                    return self._r({"equity": 1000.0, "balance": 1000.0})
                if "candles/latest" in url:
                    if "GBPUSD" in url:
                        return self._r(_gbpusd_candles())
                    return self._r(_eurusd_candles())
                return self._r({})

            async def post(self, url, **kwargs):
                return self._r({"choices": [{"message": {"content":
                    '{"quality": 8.0, "reason": "ADX 24 rising", "bias": "BEARISH", '
                    '"bias_prob": 0.6, "confidence_modifier": 1.0, "reentry_ok": true, '
                    '"regime": "TRENDING"}'}}]})

        from unittest.mock import AsyncMock, MagicMock, patch
        with patch("main.init_pool", AsyncMock()), \
             patch("main.close_pool", AsyncMock()), \
             patch("routers.ai.is_news_blackout", AsyncMock(return_value=(False, None))), \
             patch("routers.ai.fetch_bars_since_close", AsyncMock(return_value=None)), \
             patch("routers.ai.get_pool", return_value=MagicMock(execute=AsyncMock())), \
             patch("routers.ai.PPO.load", return_value=model), \
             patch("services.news_scraper.get_macro_news", AsyncMock(return_value="No news")), \
             patch("routers.ai.log_to_audit", AsyncMock()), \
             patch("httpx.AsyncClient", _FakeClient):
            from services.trade_journal import get_recent_insights
            monkeypatch.setattr("services.trade_journal.get_recent_insights",
                                AsyncMock(return_value=[]))
            with TestClient(app) as client:
                r = client.post(
                    "/api/v1/ai/predict",
                    json={"symbol": "GBPUSD", "timeframe": "H1", "position": 0},
                    headers={"x-internal-token": "test-token", "x-cycle-id": "g2-b1"},
                )
        assert r.status_code == 200, r.text
        assert r.json()["decision"] in ("BUY", "SELL", "HOLD", "CLOSE")


def _eurusd_candles(n=260):
    import datetime as dt
    base = dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc)
    out = []
    for i in range(n):
        c = 1.12 + i * 0.0001
        out.append({
            "time": (base + dt.timedelta(hours=n - 1 - i)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "open": c, "high": c + 0.0005, "low": c - 0.0005, "close": c,
            "tick_volume": 1000, "real_volume": 1000,
        })
    return out


def _gbpusd_candles(n=260):
    import datetime as dt
    base = dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc)
    out = []
    for i in range(n):
        c = 1.33 + i * 0.0001
        out.append({
            "time": (base + dt.timedelta(hours=n - 1 - i)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "open": c, "high": c + 0.0005, "low": c - 0.0005, "close": c,
            "tick_volume": 1000, "real_volume": 1000,
        })
    return out
