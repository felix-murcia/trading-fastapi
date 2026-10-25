"""
Foco ampliado H4/D (Félix 2026-10-12): contexto de marco SUPERIOR en el
prompt unificado de Qwen.

Origen (historial recuperado): backlog P2 "Qwen Prompt Engineering (CoT +
Contexto H4/D)" (H2, H19) — el veredicto de memoria del 10-09 constató que
la Fase 2 cableó solo `adx` y el pack 1-4 el SMA200/S-R DERIVADOS DE H1: el
contexto H4/D real nunca existió. Alcance del pack: datos de marcos
superiores (SMA200 de estructura + RSI14 + ADX por timeframe) en el bloque
Context del prompt, sin tocar rúmbre/examples/escala de puntuación
(restricción H2 intacta, precedente pack 1-4).

Propiedades fijadas:

1. `/predict` fetcha H4 y D1 (count=250) EN EL MISMO gather que equity+H1
   (sin latencia secuencial añadida); el fallo de H4/D1 degrada la línea a
   "Unavailable" con warning — NUNCA tumba el ciclo (son contexto, no
   críticos; equity/H1 siguen siendo los críticos con su camino HOLD de
   siempre).
2. `routers.ai._higher_tf_context_line(tf, res, symbol)` construye la línea con
   guardas de calidad (HTTP/JSON/<30 velas/NaN → "Unavailable") y guarda de
   CONTINUIDAD (hueco >7d en la serie → "Unavailable": el feed real del MCP
   tiene un agujero de 9.5 meses visto el 2026-10-12 y un contexto calculado
   sobre él sería mentira): contexto, nunca inventado.
3. El bloque viaja a Qwen como `higher_tf_context` y se renderiza DENTRO
   del bloque Context (antes de `- Candles:`), con esquema constante
   (`- H4:` / `- D1:` siempre presentes → anti-ancla H2).
4. El presupuesto de tokens se vigila: prompt completo (bloques máximos)
   acotado por debajo del hueco 2048 − respuesta.
5. Paridad de firma puerto/adapter la fija el test existente
   `test_pack_1_4_prompt_context.TestUnifiedContractWithoutSlTp.
   test_port_adapter_signature_parity` (RED until ports/actualizados).

RED → GREEN sin tocar `QUALITY_THRESHOLD`, ejemplos ni rúmbre de scoring.
"""
import json
from contextlib import contextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient
from gymnasium.spaces import Box

import routers.ai
import services.qwen_client as qwen_client
from main import app
from routers.ai import Brain


# ─── Datos sintéticos ─────────────────────────────────────────────────────────

def _candles(n: int = 250, t0: str = "2026-01-01", base: float = 1.10,
             step: float = 0.0001, freq: str = "h") -> list[dict]:
    """Velas con closes en tendencia (step>0 alcista, step<0 bajista),
    en orden DESCENDENTE (el orden real del MCP — P0: test_p0_time_order)."""
    times = pd.date_range(t0, periods=n, freq=freq)
    rows = []
    for i, t in enumerate(times):
        close = round(base + i * step, 5)
        rows.append({
            "time": t.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "open": round(close - 0.0001, 5),
            "high": round(close + 0.0002, 5),
            "low": round(close - 0.0002, 5),
            "close": close,
            "tick_volume": 1000 + i,
        })
    return rows[::-1]


def _resp(payload, status_code: int = 200) -> MagicMock:
    response = MagicMock()
    response.status_code = status_code
    response.json.return_value = payload
    return response


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


class _FakeMT5:
    """Fake httpx.AsyncClient de /predict (espejo del pack 1-4): account,
    candles (cualquier timeframe → mismas velas) y resto → `{}`."""

    candles: list = []
    requests: list = []
    fail_timeframes: set = set()   # timeframes cuyo GET lanza ConnectTimeout

    def __init__(self, *args, **kwargs):
        self.timeout = kwargs.get("timeout")

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, url, **kwargs):
        type(self).requests.append(url)
        params = kwargs.get("params", {}) or {}
        qs = "&".join(f"{k}={v}" for k, v in params.items()) if params else ""
        if qs:
            type(self).requests[-1] = url + "?" + qs
        if "/api/v1/account/info" in url:
            return self._response({"equity": 1000.0, "balance": 1000.0})
        if "/api/v1/market/candles/latest" in url:
            url_tf = params.get("timeframe", "")
            for tf in type(self).fail_timeframes:
                if f"timeframe={tf}" in url or url_tf == tf:
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
        response.text = json.dumps(payload)
        return response


@contextmanager
def _serve(monkeypatch, model, candles, qwen=None, fail_timeframes=()):
    """Stack de /predict espejo del pack 1-4 (Qwen y calendario mockados)."""
    _FakeMT5.candles = candles
    _FakeMT5.requests = []
    _FakeMT5.fail_timeframes = set(fail_timeframes)
    qwen_mock = AsyncMock(return_value=dict(_QWEN_OK if qwen is None else qwen))
    with patch("main.init_pool", AsyncMock()), \
         patch("main.close_pool", AsyncMock()), \
         patch("routers.ai.is_news_blackout", AsyncMock(return_value=(False, None))), \
         patch("routers.ai.fetch_bars_since_close", AsyncMock(return_value=None)), \
         patch("routers.ai.get_pool", return_value=MagicMock(execute=AsyncMock())), \
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
            yield client, qwen_mock


def _post(client, cycle_id: str):
    return client.post(
        "/api/v1/ai/predict",
        json={"symbol": "EURUSD", "timeframe": "H1", "position": 0},
        headers={"x-internal-token": "test-token", "x-cycle-id": cycle_id},
    )


def _capturing_client(canned_content: str):
    """Fake de httpx.AsyncClient que captura el prompt user y devuelve JSON."""

    body = {"choices": [{"message": {"content": canned_content}}]}
    body_text = json.dumps(body)

    class _Resp:
        status_code = 200
        text = body_text

        def json(self):
            return body

    class _Client:
        captured: dict = {}

        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json=None, **kwargs):
            _Client.captured["prompt"] = json["messages"][1]["content"]
            return _Resp()

    return _Client


_UNIFIED_ARGS = dict(
    symbol="EURUSD", decision="BUY", ml_prob=0.75, rsi=55.0, atr=0.00100,
    atr_pct=0.01, macd_hist=0.0005, bb_pos=0.6, range_pct=0.02, last_ret=0.001,
    hour=10, news="No major news", candle_context="last 10 H1 candles",
    cycle_id="h4d-unified", logger=MagicMock(),
)

_HEADER = (
    "- Higher-timeframe context (H4/D1) — contrast the H1 signal with the "
    "larger trend:"
)


# ─── 1. Helper `_higher_tf_context_line` ──────────────────────────────────────

class TestHigherTfContextLine:
    """Línea de contexto H4/D con guardas de calidad: fallo HTTP/JSON,
    payload sin velas o <30 velas → "Unavailable" (nunca inventado, nunca
    una excepción hacia /predict)."""

    def test_uptrend_line_has_structure_rsi_and_adx(self):
        line = routers.ai._higher_tf_context_line("H4", _resp(_candles(250)), "EURUSD")
        assert line.startswith("- H4: ")
        assert "Price ABOVE SMA200" in line
        assert "SMA200 RISING" in line
        assert "RSI " in line
        assert "ADX " in line

    def test_downtrend_line_marks_below_and_falling(self):
        line = routers.ai._higher_tf_context_line(
            "D1", _resp(_candles(250, base=1.20, step=-0.0001)), "EURUSD",
        )
        assert line.startswith("- D1: ")
        assert "Price BELOW SMA200" in line
        assert "SMA200 FALLING" in line

    def test_exception_response_degrades_to_unavailable(self, caplog):
        import logging
        with caplog.at_level(logging.WARNING):
            line = routers.ai._higher_tf_context_line(
                "H4", httpx.ConnectTimeout("MCP caído"), "EURUSD",
            )
        assert line == "- H4: Unavailable"
        assert "H4" in caplog.text, "la degradación debe dejar warning (sin fallos silenciosos)"

    def test_http_error_degrades_to_unavailable(self):
        assert routers.ai._higher_tf_context_line(
            "D1", _resp({"detail": "boom"}, status_code=500), "EURUSD",
        ) == "- D1: Unavailable"

    def test_invalid_json_degrades_to_unavailable(self):
        bad = MagicMock()
        bad.status_code = 200
        bad.json.side_effect = ValueError("no JSON")
        assert routers.ai._higher_tf_context_line("H4", bad, "EURUSD") == "- H4: Unavailable"

    @pytest.mark.parametrize("payload", [[], {}, {"candles": []}, "garbage"],
                             ids=["empty-list", "empty-dict", "candles-empty", "string"])
    def test_payload_without_enough_candles_degrades(self, payload):
        assert routers.ai._higher_tf_context_line("H4", _resp(payload), "EURUSD") == "- H4: Unavailable"

    def test_short_series_degrades_to_unavailable(self):
        # <30 velas no sostienen ni RSI14 ni ADX con honestidad
        assert routers.ai._higher_tf_context_line(
            "H4", _resp(_candles(20)), "EURUSD",
        ) == "- H4: Unavailable"


# ─── 1b. Guarda de continuidad (agujero del feed) ──────────────────────────────


def _split_with_gap(gap_h: float, old_t0: str = "2026-09-01T00:00") -> list[dict]:
    """Serie DESC en dos bloques separados por `gap_h` horas (bloque nuevo
    primero, como el MCP). El bloque viejo tiene 30 barras horarias."""
    old = _candles(30, t0=old_t0)
    old_last = pd.Timestamp(old_t0) + pd.Timedelta(hours=29)
    new_t0 = (old_last + pd.Timedelta(hours=gap_h)).strftime("%Y-%m-%dT%H:%M")
    return _candles(30, t0=new_t0) + old


class TestContinuityGuard:
    """Guarda de continuidad (vuelta roja 2026-10-12: el feed del MCP tiene un
    agujero de 9.5 meses — historia hasta 2025-12-31 + solo el día vivo — y
    `candles/date` no puede recuperar el rango). Una serie con un hueco >7 días
    invalida SMA200/RSI/ADX calculados sobre ella (ejemplo real: RSI 2.7 y
    SMA200 dist -0.047 mezclando dic-2025 con hoy) → "Unavailable", nunca
    contexto mentiroso. Ningún cierre de mercado/feriado legítimo supera 7d
    (fin de semana ≤56h, festivos ≤~120h)."""

    def test_real_hole_shape_degrades_to_unavailable(self, caplog):
        import logging
        # Forma EXACTA del feed real: barras de hoy + bloque viejo pegados
        recent = _candles(16, t0="2026-10-12T00:00")   # DESC (más nueva 1ª)
        old = _candles(200, t0="2025-11-01T00:00")      # DESC
        with caplog.at_level(logging.WARNING):
            line = routers.ai._higher_tf_context_line("H4", _resp(recent + old), "EURUSD")
        assert line == "- H4: Unavailable"
        assert "hueco" in caplog.text, "la degradación por hueco debe dejar warning"

    def test_weekend_gap_still_builds_line(self):
        # cierre de finde ~50h: legítimo → la línea se construye
        line = routers.ai._higher_tf_context_line("H4", _resp(_split_with_gap(50)), "EURUSD")
        assert line.startswith("- H4: ") and "Unavailable" not in line
        assert "RSI" in line and "ADX" in line

    def test_long_holiday_gap_still_builds_line(self):
        # festivo largo (viernes→martes ≈120h): legítimo → se construye
        line = routers.ai._higher_tf_context_line("D1", _resp(_split_with_gap(120)), "EURUSD")
        assert line.startswith("- D1: ") and "Unavailable" not in line

    def test_gap_just_over_seven_days_degrades(self):
        # frontera: >168h nunca es un cierre de mercado → inválido
        assert routers.ai._higher_tf_context_line(
            "H4", _resp(_split_with_gap(169)), "EURUSD",
        ) == "- H4: Unavailable"


# ─── 2. Cableado en /predict ──────────────────────────────────────────────────

class TestHigherTfWiring:
    """Fetch H4+D1 en paralelo con equity+H1, forward al Qwen y degradación
    controlada si un timeframe falla."""

    def test_fetches_h4_and_d1_next_to_h1(self, monkeypatch):
        model = _model()
        with _serve(monkeypatch, model, _candles(250)) as (client, *_):
            resp = _post(client, "h4d-fetch")
        assert resp.status_code == 200, resp.text
        joined = " ".join(_FakeMT5.requests)
        assert "timeframe=H4&count=250" in joined, "falta el fetch H4"
        assert "timeframe=D1&count=250" in joined, "falta el fetch D1"
        assert "timeframe=H1&count=240" in joined, "el H1 de siempre sigue ahí"

    def test_context_block_forwarded_to_qwen(self, monkeypatch):
        model = _model()
        with _serve(monkeypatch, model, _candles(250)) as (client, qwen_mock):
            resp = _post(client, "h4d-forward")
        assert resp.status_code == 200, resp.text
        kw = qwen_mock.call_args.kwargs
        block = kw["higher_tf_context"]
        assert block is not None, "el bloque H4/D debe viajar al Qwen"
        assert block.splitlines()[0] == _HEADER
        assert "- H4: " in block
        assert "- D1: " in block
        assert "Unavailable" not in block, "con datos completos no debe degradar"

    def test_h4_failure_degrades_line_but_cycle_survives(self, monkeypatch):
        """El fallo del fetch H4 NO tumba el ciclo (contexto, no crítico):
        la línea dice "Unavailable" con warning y Qwen se sigue llamando."""
        model = _model()
        with _serve(monkeypatch, model, _candles(250),
                    fail_timeframes=("H4",)) as (client, qwen_mock):
            resp = _post(client, "h4d-h4-down")
        assert resp.status_code == 200, resp.text
        kw = qwen_mock.call_args.kwargs
        block = kw["higher_tf_context"]
        assert "- H4: Unavailable" in block
        assert "- D1: " in block and "Unavailable" not in block.splitlines()[2]

    def test_equity_failure_still_holds(self, monkeypatch):
        """Los críticos (equity/H1) conservan su camino: H4/D1 no lo tocan."""
        model = _model()
        _FakeMT5.candles = _candles(250)
        _FakeMT5.requests = []
        _FakeMT5.fail_timeframes = set()

        class _NoEquity(_FakeMT5):
            async def get(self, url, **kwargs):
                if "/api/v1/account/info" in url:
                    return self._response({}, status_code=500)
                return await super().get(url, **kwargs)

        qwen_mock = AsyncMock(return_value=dict(_QWEN_OK))
        with patch("main.init_pool", AsyncMock()), \
             patch("main.close_pool", AsyncMock()), \
             patch("routers.ai.is_news_blackout", AsyncMock(return_value=(False, None))), \
             patch("routers.ai.fetch_bars_since_close", AsyncMock(return_value=None)), \
             patch("routers.ai.get_pool", return_value=MagicMock(execute=AsyncMock())), \
             patch("routers.ai.PPO.load", return_value=model), \
             patch("services.news_scraper.get_macro_news", AsyncMock(return_value="x")), \
             patch("routers.ai.query_qwen_unified", qwen_mock), \
             patch("routers.ai.get_upcoming_news", AsyncMock(return_value=[])), \
             patch("httpx.AsyncClient", _NoEquity):
            with TestClient(app) as client:
                monkeypatch.setitem(
                    routers.ai._BRAINS, "EURUSD",
                    Brain(model=model, vec_norm=None, bundle={}, memory_channel=True),
                )
                resp = _post(client, "h4d-noequity")
        assert resp.status_code == 200, resp.text
        assert resp.json()["decision"] == "HOLD"
        qwen_mock.assert_not_awaited()


# ─── 3. Bloque en el prompt ───────────────────────────────────────────────────

class TestPromptHigherTfBlock:
    """El bloque se renderiza dentro de Context (antes de `- Candles:`) y sin
    parámetro el prompt queda como antes (back-compat)."""

    @staticmethod
    async def _prompt(**overrides) -> str:
        client = _capturing_client(
            '{"quality": 6.0, "reason": "ok", "bias": "NEUTRAL",'
            ' "confidence_modifier": 1.0, "regime": "RANGING"}'
        )
        with patch("httpx.AsyncClient", client):
            result = await qwen_client.query_qwen_unified(
                **{**_UNIFIED_ARGS, **overrides},
            )
        assert result["ok"] is True, "canned debe parsear (setup del test)"
        return client.captured["prompt"]

    @pytest.mark.asyncio
    async def test_block_rendered_inside_context_before_candles(self):
        block = (
            f"{_HEADER}\n"
            "- H4: Price ABOVE SMA200 (+0.0030), SMA200 RISING (+0.0005), "
            "RSI 58.3, ADX 0.28\n"
            "- D1: Price BELOW SMA200 (-0.0150), SMA200 FALLING (-0.0002), "
            "RSI 44.1, ADX 0.19"
        )
        prompt = await self._prompt(higher_tf_context=block)
        assert _HEADER in prompt
        assert "- H4: Price ABOVE SMA200" in prompt
        assert "- D1: Price BELOW SMA200" in prompt
        # dentro del bloque Context: tras las líneas de indicadores, antes de Candles
        assert prompt.index("- H4:") < prompt.index("- Candles:"), (
            "el bloque H4/D debe vivir dentro de Context"
        )
        assert prompt.index("- D1:") < prompt.index("- Candles:")

    @pytest.mark.asyncio
    async def test_absent_param_keeps_prompt_as_before(self):
        prompt = await self._prompt()
        assert "Higher-timeframe" not in prompt
        assert "- H4:" not in prompt
        assert "- D1:" not in prompt

    @pytest.mark.asyncio
    async def test_prompt_fits_token_budget_with_blocks_maxed(self):
        """Presupuesto de tokens: con todos los bloques en su máximo (journal
        5 filas, calendario 3 eventos, bloque H4/D completo) el prompt queda
        por debajo del hueco 2048 − respuesta JSON (~150 t). 6400 chars ≈
        1600 t + 150 t de respuesta ≈ 1750 t < 2048."""
        block = (
            f"{_HEADER}\n"
            "- H4: Price ABOVE SMA200 (+0.0030), SMA200 RISING (+0.0005), "
            "RSI 58.3, ADX 0.28\n"
            "- D1: Price BELOW SMA200 (-0.0150), SMA200 FALLING (-0.0002), "
            "RSI 44.1, ADX 0.19"
        )
        insights = [{
            "symbol": "EURUSD",
            "created_at": datetime(2026, 10, 8, 18, 0, tzinfo=timezone.utc),
            "direction": "SHORT", "pnl_pct": "-0.0091", "exit_reason": "ea",
            "qwen_insight": "x" * 48, "regime": "RANGING",
        }] * 5
        calendar = [{
            "title": f"Event {i}", "currency": "USD",
            "time_utc": "2026-10-10T09:00:00+00:00", "minutes_until": 60.0,
        } for i in range(3)]
        prompt = await self._prompt(
            higher_tf_context=block, recent_insights=insights,
            calendar_events=calendar,
            news="Macro headline. " * 40,   # noticia larga → se trunca a 400
        )
        assert len(prompt) <= 6400, (
            f"prompt {len(prompt)} chars excede el presupuesto de tokens "
            "definido (2048 − respuesta)"
        )
