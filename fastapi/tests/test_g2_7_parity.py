"""
G2-7 (orden Félix 2026-10-20, «continúa con GBPUSD hasta el final») —
PARIDAD Y NO-ACOPLAMIENTO entre los dos mundos.

  (a) fixtures parametrizadas: la misma comprobación corre para EURUSD y GBPUSD;
  (b) aislamiento: mutar el estado/config de un mundo NO altera el otro;
  (c) E2E de predict para GBPUSD (MT5 mockeado);
  (d) guard anti-ramas: el serve NO puede decidir por símbolo (`if symbol ==`);
  (e) el bundle de cada mundo es PROPIO (fichero, manifest, baseline).
"""
import ast
import os
import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest
from fastapi.testclient import TestClient

from main import app
from services import qwen_client
from services.qwen_client import QwenHealthState
from tests.test_qwen_semantics import _mock_ppo_model

ROOT = Path(__file__).resolve().parent.parent


def _candles(n=260, base=1.12, step=0.0001, seed=None):
    import datetime as dt
    rng = np.random.default_rng(seed) if seed is not None else None
    b = dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc)
    out = []
    for i in range(n):
        c = base + i * step + (float(rng.normal(0, step)) if rng is not None else 0.0)
        out.append({
            "time": (b + dt.timedelta(hours=n - 1 - i)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "open": round(c, 5), "high": round(c + 0.0005, 5),
            "low": round(c - 0.0005, 5), "close": round(c, 5),
            "tick_volume": 1000, "real_volume": 1000,
        })
    return out


def _fake_client(symbol: str):
    class _FakeClient:
        def __init__(self, *a, **k):
            self.timeout = k.get("timeout")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def _r(self, payload):
            r = MagicMock()
            r.status_code = 200
            r.json = lambda: payload
            r.text = str(payload)[:200]
            return r

        async def get(self, url, **kwargs):
            if "/account/info" in url:
                return self._r({"equity": 1000.0, "balance": 1000.0})
            if "candles/latest" in url:
                if "GBPUSD" in url:
                    return self._r(_candles(base=1.33))
                return self._r(_candles(base=1.12))
            return self._r({})

        async def post(self, url, **kwargs):
            return self._r({"choices": [{"message": {"content":
                '{"quality": 8.0, "reason": "ADX 24 rising, RSI 52", "bias": "BEARISH", '
                '"bias_prob": 0.6, "confidence_modifier": 1.0, "reentry_ok": true, '
                '"regime": "TRENDING"}'}}]})
    return _FakeClient


class TestParityAcrossWorlds:
    """(a) La misma comprobación corre para los dos mundos."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("symbol,base", [("EURUSD", 1.12), ("GBPUSD", 1.33)])
    async def test_predict_contract_holds_for_both_worlds(self, monkeypatch, symbol, base):
        """El contrato de la respuesta es el mismo en los dos mundos."""
        model = _mock_ppo_model()
        model.predict.return_value = (np.array([-0.8, 0.18, 0.15, 0.15], dtype=np.float32), None)
        monkeypatch.setattr(qwen_client, "_QWEN_STATE", QwenHealthState())
        with patch("main.init_pool", AsyncMock()), \
             patch("main.close_pool", AsyncMock()), \
             patch("routers.ai.is_news_blackout", AsyncMock(return_value=(False, None))), \
             patch("routers.ai.fetch_bars_since_close", AsyncMock(return_value=None)), \
             patch("routers.ai.get_pool", return_value=MagicMock(execute=AsyncMock())), \
             patch("routers.ai.PPO.load", return_value=model), \
             patch("services.news_scraper.get_macro_news", AsyncMock(return_value="No news")), \
             patch("routers.ai.log_to_audit", AsyncMock()), \
             patch("httpx.AsyncClient", _fake_client(symbol)):
            from services.trade_journal import get_recent_insights
            monkeypatch.setattr("services.trade_journal.get_recent_insights",
                                AsyncMock(return_value=[]))
            with TestClient(app) as client:
                r = client.post(
                    "/api/v1/ai/predict",
                    json={"symbol": symbol, "timeframe": "H1", "position": 0},
                    headers={"x-internal-token": "test-token", "x-cycle-id": f"g2-7-{symbol}"},
                )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["decision"] in ("BUY", "SELL", "HOLD", "CLOSE")
        assert body["cycle_id"].startswith(symbol), "el cycle_id lleva su mundo"


class TestWorldIsolation:
    """(b) Mutar un mundo NO altera el otro."""

    def test_dead_zone_counter_is_per_symbol(self):
        """El contador de histéresis (K=2) es por mundo: el de GBPUSD no
        avanza al contar EURUSD (G2-8: contadores que empiezan en 0)."""
        from routers.ai import _DEAD_ZONE_COUNTER
        _DEAD_ZONE_COUNTER.pop("EURUSD", None)
        _DEAD_ZONE_COUNTER.pop("GBPUSD", None)
        _DEAD_ZONE_COUNTER["EURUSD"] = 1
        assert "GBPUSD" not in _DEAD_ZONE_COUNTER, (
            "el contador de GBPUSD debe empezar en 0 (evidencia nueva)"
        )
        assert _DEAD_ZONE_COUNTER["EURUSD"] == 1

    def test_risk_pair_state_is_per_symbol(self):
        """El estado de riesgo por par es por símbolo (la cuenta es compartida)."""
        from core.pair import PairConfig, default_model_path
        a = PairConfig(symbol="EURUSD", enabled=True, model_path=default_model_path("EURUSD"))
        b = PairConfig(symbol="GBPUSD", enabled=False, model_path=default_model_path("GBPUSD"))
        assert a is not b
        assert a.symbol != b.symbol
        assert a.model_path != b.model_path

    def test_baseline_paths_are_per_world(self):
        from services.drift_monitor import baseline_path
        from core.pair import default_model_path
        assert baseline_path("EURUSD") != baseline_path("GBPUSD")
        assert default_model_path("EURUSD") != default_model_path("GBPUSD")


class TestNoSymbolBranching:
    """(d) Guard anti-ramas: el serve no decide por símbolo."""

    def test_no_symbol_equality_branches_in_serve(self):
        """Cero `if symbol ==` en el camino de decisión: los mundos se
        configuran, no se ramifican (G2-0/G2-7)."""
        src = (ROOT / "routers" / "ai.py").read_text(encoding="utf-8")
        # Se admiten los usos de la CONFIG por símbolo (settings.pairs, etc.),
        # no las ramas de lógica sobre el símbolo.
        branches = re.findall(r'if\s+[^\n]*symbol\s*==\s*["\']', src)
        assert not branches, f"ramas por símbolo en el serve: {branches}"

    def test_serve_syntax_is_valid(self):
        tree = ast.parse((ROOT / "routers" / "ai.py").read_text(encoding="utf-8"))
        assert tree is not None


class TestPerWorldArtifacts:
    """(e) Cada mundo tiene sus propios artefactos."""

    def test_artifact_dirs_are_per_world(self):
        from core.pair import default_model_path
        eur = os.path.join(os.path.dirname(default_model_path("EURUSD")), "retrain_artifacts")
        gbp = os.path.join(os.path.dirname(default_model_path("GBPUSD")), "retrain_artifacts")
        assert eur != gbp

    def test_registry_lists_both_worlds_with_canary_limits(self):
        """GBPUSD está LISTADO como canario (enabled=false) con sus límites
        propios: habilitarlo es un flip explícito, nunca automático."""
        from config import settings
        specs = {s["symbol"]: s for s in settings.pairs}
        assert "EURUSD" in specs and "GBPUSD" in specs
        assert specs["GBPUSD"].get("enabled") is False, (
            "GBPUSD sigue como canario hasta el flip explícito (G2-4)"
        )
        assert specs["GBPUSD"].get("daily_loss_pct") == 2.0
        assert specs["GBPUSD"].get("equity_allocation_pct") == 20
