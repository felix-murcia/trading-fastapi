"""T-07 (2026-10-15, orden de Félix «aprobado t07»): breaker del feed H4/D1.

El incidente MCP 14/10 (14:05→15:45 UTC) mostró que el bug vive en el
binding MetaTrader5: la PRIMERA petición H4/D1 al servidor wedgado vuelve
a tumbar su sesión (ventanas vivas de 1-2 min muertas por el 1er H4
posterior) → cada ciclo reintentaba H4 y re-mataba el servidor.

El breaker: el primer fallo de fetch H4/D1 abre la puerta — durante el
cooldown (`H4D_BREAKER_COOLDOWN_MINUTES`, default 30) los ciclos NO
fetchan H4/D1 (la línea del prompt se renderiza «Unavailable» con esquema
constante = sin cambio anti-ancla) y el MCP se queda vivo para los
críticos equity/H1. Al expirar el cooldown el siguiente ciclo sondea de
nuevo (semántica half-open): si el fetch vuelve a fallar, re-abre otro
cooldown.

Contexto ≠ crítico intacto: equity/H1 conservan su camino HOLD de
siempre. Telemetría: `h4d_context` en `audit_log` —
"ok" (fetch respondió) | "unavailable" (fetch falló → breaker abierto) |
"breaker_open" (omitido por cooldown).
"""
import time
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient
from gymnasium.spaces import Box

import routers.ai
import services.feed_breaker as feed_breaker
from config import settings
from main import app
from routers.ai import Brain


_QWEN_OK = {
    "ok": True, "quality": 8.5, "reason": "Strong setup",
    "bias": "BULLISH", "confidence_modifier": 1.1,
    "effective_prob": 0.7, "regime": "TRENDING", "reentry_ok": True,
}


def _model() -> MagicMock:
    model = MagicMock()
    model.predict.return_value = (
        np.array([0.80, 0.18, 0.15, 0.15], dtype=np.float32), None,
    )
    model.observation_space = Box(-np.inf, np.inf, shape=(20, 25), dtype=np.float32)
    model.action_space = Box(-1.0, 1.0, shape=(4,), dtype=np.float32)
    return model


def _candles(n: int = 250) -> list[dict]:
    """Velas H1 sintéticas en tendencia, orden DESCENDENTE (orden real MCP)."""
    times = pd.date_range("2026-01-01", periods=n, freq="h")
    rows = []
    for i, t in enumerate(times):
        close = round(1.10 + i * 0.0001, 5)
        rows.append({
            "time": t.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "open": round(close - 0.0001, 5),
            "high": round(close + 0.0002, 5),
            "low": round(close - 0.0002, 5),
            "close": close,
            "tick_volume": 1000 + i,
        })
    return rows[::-1]


class _FakeMT5:
    """Fake httpx.AsyncClient de /predict (espejo de test_qwen_h4d_context):
    account, candles (cualquier timeframe → mismas velas) y resto → {}."""

    candles: list = []
    requests: list = []
    fail_timeframes: set = set()

    def __init__(self, *args, **kwargs):
        self.timeout = kwargs.get("timeout")

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, url, **kwargs):
        type(self).requests.append(url)
        if "/api/v1/account/info" in url:
            return self._response({"equity": 1000.0, "balance": 1000.0})
        if "/api/v1/market/candles/latest" in url:
            for tf in type(self).fail_timeframes:
                if f"timeframe={tf}" in url:
                    raise httpx.ConnectTimeout(f"{tf} caído")
            return self._response(type(self).candles)
        return self._response({})

    async def post(self, url, **kwargs):
        return self._response({})

    @staticmethod
    def _response(payload, status_code=200):
        response = MagicMock()
        response.status_code = status_code
        response.json.return_value = payload
        response.text = "{}"
        return response


@contextmanager
def _serve(monkeypatch, model, candles, fail_timeframes=()):
    """Stack de /predict espejo (Qwen, calendario y audit mockados)."""
    _FakeMT5.candles = candles
    _FakeMT5.requests = []
    _FakeMT5.fail_timeframes = set(fail_timeframes)
    qwen_mock = AsyncMock(return_value=dict(_QWEN_OK))
    with patch("main.init_pool", AsyncMock()), \
         patch("main.close_pool", AsyncMock()), \
         patch("routers.ai.is_news_blackout", AsyncMock(return_value=(False, None))), \
         patch("routers.ai.fetch_bars_since_close", AsyncMock(return_value=None)), \
         patch("routers.ai.get_pool", return_value=MagicMock(execute=AsyncMock())), \
         patch("routers.ai.log_to_audit", AsyncMock()) as audit_mock, \
         patch("routers.ai.PPO.load", return_value=model), \
         patch("services.news_scraper.get_macro_news", AsyncMock(return_value="No macro news")), \
         patch("routers.ai.query_qwen_unified", qwen_mock), \
         patch("routers.ai.get_upcoming_news", AsyncMock(return_value=[])), \
         patch("httpx.AsyncClient", _FakeMT5):
        with TestClient(app) as client:
            monkeypatch.setitem(
                routers.ai._BRAINS, "EURUSD",
                Brain(model=model, vec_norm=None, bundle={}, memory_channel=True),
            )
            yield client, qwen_mock, audit_mock


def _post(client, cycle_id: str):
    return client.post(
        "/api/v1/ai/predict",
        json={"symbol": "EURUSD", "timeframe": "H1", "position": 0},
        headers={"x-internal-token": "test-token", "x-cycle-id": cycle_id},
    )


def _cycle_audit(audit_mock) -> dict:
    """Payload del ciclo de predict (log_to_audit puede recibir otros eventos)."""
    for call in audit_mock.await_args_list:
        if call.args[0] == "ai_predict_cycle":
            return call.args[1]
    raise AssertionError("sin fila ai_predict_cycle en audit")


@pytest.fixture(autouse=True)
def _fresh_breaker():
    """Estado del breaker aislado por test (módulo con estado compartido)."""
    feed_breaker.h4d_breaker_reset()
    yield
    feed_breaker.h4d_breaker_reset()


class TestH4dBreaker:
    """T-07: primer fallo abre → cooldown sin fetch → sonda half-open."""

    def test_h4_failure_trips_breaker_and_cycle_survives(self, monkeypatch):
        """Primer fallo H4: ciclo 200 (contexto, no crítico), breaker abre
        con cooldown futuro y el audit registra `unavailable`."""
        model = _model()
        with _serve(monkeypatch, model, _candles(250),
                    fail_timeframes=("H4",)) as (client, qwen_mock, audit):
            resp = _post(client, "brk-trip")
        assert resp.status_code == 200, resp.text
        state = feed_breaker.get_feed_breaker()
        assert state.open_until > time.time(), "el breaker debía abrirse"
        assert state.openings == 1
        assert "H4" in (state.last_failure or "")
        aud = _cycle_audit(audit)
        assert aud["h4d_context"] == "unavailable"
        block = qwen_mock.call_args.kwargs["higher_tf_context"]
        assert "- H4: Unavailable" in block

    def test_breaker_open_skips_h4_d1_fetches(self, monkeypatch):
        """Con el breaker abierto los ciclos NO fetchan H4/D1 (el MCP deja
        de recibir el golpe) pero equity/H1 siguen y el esquema del prompt
        se mantiene con «Unavailable»."""
        feed_breaker.h4d_breaker_trip("manual: H4 pre-test")
        model = _model()
        with _serve(monkeypatch, model, _candles(250)) as (client, qwen_mock, audit):
            resp = _post(client, "brk-open")
        assert resp.status_code == 200, resp.text
        joined = " ".join(_FakeMT5.requests)
        assert "timeframe=H4" not in joined, "el breaker debía omitir el fetch H4"
        assert "timeframe=D1" not in joined, "el breaker debía omitir el fetch D1"
        assert "timeframe=H1&count=240" in joined, "el H1 crítico sigue"
        assert "/api/v1/account/info" in joined, "el equity crítico sigue"
        aud = _cycle_audit(audit)
        assert aud["h4d_context"] == "breaker_open"
        block = qwen_mock.call_args.kwargs["higher_tf_context"]
        assert "- H4: Unavailable" in block and "- D1: Unavailable" in block

    def test_failed_probe_reopens_breaker(self, monkeypatch):
        """Al expirar el cooldown se sondea de nuevo (half-open): si vuelve
        a fallar, re-abre otro cooldown (contador de aperturas = 2)."""
        feed_breaker.h4d_breaker_trip("manual")
        feed_breaker.get_feed_breaker().open_until = time.time() - 1.0
        model = _model()
        with _serve(monkeypatch, model, _candles(250),
                    fail_timeframes=("D1",)) as (client, *_):
            resp = _post(client, "brk-reopen")
        assert resp.status_code == 200, resp.text
        joined = " ".join(_FakeMT5.requests)
        assert "timeframe=D1" in joined, "la sonda debía reintentar el fetch"
        state = feed_breaker.get_feed_breaker()
        assert state.open_until > time.time(), "la sonda fallida debía reabrir"
        assert state.openings == 2
        assert "D1" in (state.last_failure or "")

    def test_successful_probe_leaves_ok(self, monkeypatch):
        """Sonda exitosa tras el cooldown: contexto restaurado, audit `ok`
        y el breaker queda cerrado."""
        feed_breaker.h4d_breaker_trip("manual")
        feed_breaker.get_feed_breaker().open_until = time.time() - 1.0
        model = _model()
        with _serve(monkeypatch, model, _candles(250)) as (client, qwen_mock, audit):
            resp = _post(client, "brk-recover")
        assert resp.status_code == 200, resp.text
        joined = " ".join(_FakeMT5.requests)
        assert "timeframe=H4&count=250" in joined, "la sonda debía reintentar H4"
        assert feed_breaker.get_feed_breaker().open_until <= time.time()
        aud = _cycle_audit(audit)
        assert aud["h4d_context"] == "ok"
        block = qwen_mock.call_args.kwargs["higher_tf_context"]
        assert "Unavailable" not in block

    def test_critics_keep_hold_path_while_breaker_open(self, monkeypatch):
        """Con el breaker abierto el equity caído sigue tirando HOLD — el
        breaker no toca el camino de los críticos."""
        feed_breaker.h4d_breaker_trip("manual")
        model = _model()

        class _NoEquity(_FakeMT5):
            async def get(self, url, **kwargs):
                if "/api/v1/account/info" in url:
                    return self._response({}, status_code=500)
                return await super().get(url, **kwargs)

        with patch("main.init_pool", AsyncMock()), \
             patch("main.close_pool", AsyncMock()), \
             patch("routers.ai.is_news_blackout", AsyncMock(return_value=(False, None))), \
             patch("routers.ai.fetch_bars_since_close", AsyncMock(return_value=None)), \
             patch("routers.ai.get_pool", return_value=MagicMock(execute=AsyncMock())), \
             patch("routers.ai.log_to_audit", AsyncMock()), \
             patch("routers.ai.PPO.load", return_value=model), \
             patch("services.news_scraper.get_macro_news", AsyncMock(return_value="x")), \
             patch("routers.ai.query_qwen_unified", AsyncMock(return_value=dict(_QWEN_OK))), \
             patch("routers.ai.get_upcoming_news", AsyncMock(return_value=[])), \
             patch("httpx.AsyncClient", _NoEquity):
            with TestClient(app) as client:
                monkeypatch.setitem(
                    routers.ai._BRAINS, "EURUSD",
                    Brain(model=model, vec_norm=None, bundle={}, memory_channel=True),
                )
                resp = _post(client, "brk-noequity")
        assert resp.status_code == 200, resp.text
        assert resp.json()["decision"] == "HOLD"

    def test_cooldown_default_pinned(self):
        """Cooldown 30 min por defecto (env H4D_BREAKER_COOLDOWN_MINUTES lo
        sobreescribe) — el presupuesto de probing es UNA sola fuente."""
        assert settings.h4d_breaker_cooldown_minutes == 30

    def test_trip_uses_configured_cooldown(self, monkeypatch):
        """La apertura respeta el cooldown configurado (no un literal)."""
        monkeypatch.setattr(
            feed_breaker, "settings",
            SimpleNamespace(h4d_breaker_cooldown_minutes=10),
        )
        before = time.time()
        feed_breaker.h4d_breaker_trip("manual")
        state = feed_breaker.get_feed_breaker()
        assert state.open_until == pytest.approx(before + 10 * 60.0, abs=2.0)
