"""
T-10 — memoria a largo plazo como DATO (inyección determinista), orden de
Félix 2026-10-15 «escribe las tareas y comienza» (backlog T-10).

La paridad 8080-vs-8090 (2026-10-15, mismo Q2_K, payload de producción
idéntico) demostró que en `response_format: json_object` el modelo NUNCA
emite tool_calls → la memoria-as-tool del proxy es inalcanzable en el
camino de decisión (y su system inyectado deriva el muestreo: conf_mod
1.1→1.0). Este módulo fija el reemplazo aprobado: memory como ENTRADA
determinista — el backend consulta memory_api (:8095) directamente y el
bloque viaja en el user prompt, trazable en QWEN-PROMPT y en audit.

Propiedades fijadas:

1. `fetch_relevant_memories` filtra client-side (categoría permitida +
   símbolo en el texto + distancia ≤ 0.4), ordena ascendente (más cercano
   primero) y corta a top-K 3 — el contrato de memory_api no expone
   timestamps ni filtro por símbolo (OpenAPI verificado 2026-10-15).
2. El fetch es FAIL-OPEN: timeout / HTTP != 200 / JSON malformado → []
   con WARNING — jamás una excepción hacia /predict (la memoria es
   enriquecedor, no crítico; misma clase que H4/D1).
3. Prompt SIN memorias = sin sección (byte-idéntico al pre-T-10:
   back-compat del prompt validado).
4. CON memorias: bloque `Relevant memories` en el USER tras `- Candles:`
   y antes de `News:`; el SYSTEM queda intacto (restricción H2); ≤3
   líneas con marca de fuente (categoria + distancia visibles).
5. Routing en /predict: off → sin fetch (audit `memory=null`) | shadow →
   fetch + candidatos en audit, CERO inyección | on → inyección real |
   skip LR-1 → sin fetch (no hay prompt que enriquecer).
6. Presupuesto: prompt con TODOS los bloques máximos (incl. 3 memorias
   del contrato ≤30 palabras) cabe en el hueco 2048 − respuesta.
"""
import json
import json as json_mod
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import numpy as np
import pytest
from fastapi.testclient import TestClient
from gymnasium.spaces import Box

import routers.ai
import services.qwen_client as qwen_client
from config import settings
from main import app
from routers.ai import Brain
from services.memory_client import (
    MEMORY_ALLOWED_CATEGORIES,
    MEMORY_MAX_DISTANCE,
    MEMORY_TOP_K,
    build_memory_query,
    fetch_relevant_memories,
    format_memory_lines,
)


# ─── Datos ────────────────────────────────────────────────────────────────────

_CANDIDATES = [
    {"texto": "EURUSD TRENDING regime last 30d: 12 trades WR 58 pct avg +1.4 EUR",
     "categoria": "regime_detail", "score": 0.22},
    {"texto": "EURUSD SHORT 2026-10-08 exit=tp pnl=+15.68 EUR insight=trend_continuation",
     "categoria": "trade_insight", "score": 0.31},
]

_SEARCH_PAYLOAD = {"recuerdos": [
    # Dentro de contrato (categoría permitida + símbolo + umbral) — el orden
    # del payload es irrelevante: el cliente re-ordena por score.
    _CANDIDATES[0],
    _CANDIDATES[1],
    # Símbolo ajeno → descartado (el filtro por símbolo es client-side).
    {"texto": "GBPUSD SHORT exit=sl insight=structure_failure",
     "categoria": "edge_loss", "score": 0.10},
    # Categoría de las pruebas T5.3 (no poblada) → fuera del prompt decisión.
    {"texto": "EURUSD note in a non-production category",
     "categoria": "trading", "score": 0.05},
    # Sobre el umbral de distancia → ruido, descartado.
    {"texto": "EURUSD setup success older memory",
     "categoria": "setup_success", "score": 0.55},
], "total": 5}

_VALID_QWEN = json.dumps({
    "quality": 7.0, "reason": "trend aligns with signal", "bias": "BULLISH",
    "confidence_modifier": 1.1, "reentry_ok": True, "regime": "TRENDING",
})


class _FakeSearch:
    """Fake httpx.AsyncClient que responde /buscar con _SEARCH_PAYLOAD."""

    payload: dict = _SEARCH_PAYLOAD
    status: int = 200
    raise_exc: Exception | None = None
    requests: list = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None, **kwargs):
        type(self).requests.append((url, json))
        if type(self).raise_exc is not None:
            raise type(self).raise_exc
        response = MagicMock()
        response.status_code = type(self).status
        response.json.return_value = type(self).payload
        response.text = "{}"
        return response


def _use_fake(monkeypatch, payload=_SEARCH_PAYLOAD, status=200, raise_exc=None):
    _FakeSearch.payload = payload
    _FakeSearch.status = status
    _FakeSearch.raise_exc = raise_exc
    _FakeSearch.requests = []
    monkeypatch.setattr("httpx.AsyncClient", _FakeSearch)


# ─── 1-2. Adapter: fetch ──────────────────────────────────────────────────────

class TestFetchRelevantMemories:
    """Filtrado client-side (categoría + símbolo + umbral) y fail-open."""

    def test_filters_by_category_symbol_and_distance(self, monkeypatch):
        _use_fake(monkeypatch)
        import asyncio
        result = asyncio.run(fetch_relevant_memories("EURUSD BUY setup", "EURUSD"))
        assert [m["categoria"] for m in result] == ["regime_detail", "trade_insight"], (
            "sólo categorías permitidas con símbolo y distancia ≤ umbral, "
            "ordenadas por score ascendente"
        )
        assert all(m["score"] <= MEMORY_MAX_DISTANCE for m in result)
        assert len(result) <= MEMORY_TOP_K

    def test_request_shape_overfetches_for_client_side_filtering(self, monkeypatch):
        _use_fake(monkeypatch)
        import asyncio
        asyncio.run(fetch_relevant_memories("EURUSD BUY setup", "EURUSD"))
        url, body = _FakeSearch.requests[0]
        assert url.endswith("/buscar")
        assert body["limite"] == MEMORY_TOP_K * 3, (
            "sobre-consulta: el filtro de categorías es client-side"
        )
        assert "symbol" not in body, (
            "memory_api no acepta filtro por símbolo (contrato verificado) "
            "— el símbolo se filtra en el cliente"
        )

    def test_timeout_fails_open_with_warning(self, monkeypatch, caplog):
        _use_fake(monkeypatch, raise_exc=httpx.ConnectTimeout("Jetson caído"))
        import asyncio
        with caplog.at_level(logging.WARNING):
            result = asyncio.run(fetch_relevant_memories("q", "EURUSD"))
        assert result == []
        assert "MEMORY fetch" in caplog.text, "la degradación debe dejar WARNING"

    def test_http_500_fails_open(self, monkeypatch):
        _use_fake(monkeypatch, status=500)
        import asyncio
        assert asyncio.run(fetch_relevant_memories("q", "EURUSD")) == []

    def test_malformed_json_fails_open(self, monkeypatch):
        _use_fake(monkeypatch)
        # response.json lanza → el cliente no propaga
        original_post = _FakeSearch.post

        async def _bad_json_post(self, url, json=None, **kwargs):
            response = MagicMock()
            response.status_code = 200
            response.json.side_effect = ValueError("json roto")
            response.text = "not json"
            return response

        _FakeSearch.post = _bad_json_post
        try:
            import asyncio
            assert asyncio.run(fetch_relevant_memories("q", "EURUSD")) == []
        finally:
            _FakeSearch.post = original_post

    def test_allowed_categories_constant(self):
        assert MEMORY_ALLOWED_CATEGORIES == (
            "trade_insight", "setup_success", "edge_loss", "regime_detail",
        )


class TestBuildMemoryQuery:
    """Query de retrieval: determinista, en inglés, desde el ciclo actual."""

    def test_query_is_deterministic_and_describes_the_setup(self):
        q1 = build_memory_query(symbol="EURUSD", decision="BUY", rsi=58.34,
                                adx=24.7, hour=11)
        q2 = build_memory_query(symbol="EURUSD", decision="BUY", rsi=58.34,
                                adx=24.7, hour=11)
        assert q1 == q2, "mismos inputs → misma query (reproducibilidad)"
        assert "EURUSD" in q1 and "BUY" in q1
        assert "regime TRENDING" in q1 and "RSI 58" in q1

    def test_optional_fields_are_omitted_not_invented(self):
        q = build_memory_query(symbol="EURUSD", decision="SELL")
        assert q == "EURUSD SELL, regime setup"


# ─── 3-4. Prompt: bloque de memorias ──────────────────────────────────────────

class _CapturingClient:
    """Fake httpx.AsyncClient que captura el payload completo del LLM."""

    captured: dict = {}

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None, **kwargs):
        type(self).captured["payload"] = json
        body = {"choices": [{"message": {"content": _VALID_QWEN}}]}
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = body
        response.text = json_mod.dumps(body)
        return response


_ARGS = dict(
    symbol="EURUSD", decision="BUY", ml_prob=0.75, rsi=55.0, atr=0.00100,
    atr_pct=0.01, macd_hist=0.0005, bb_pos=0.6, range_pct=0.02, last_ret=0.001,
    hour=10, news="No major news", candle_context="last 10 H1 candles",
    cycle_id="t10-memory", logger=MagicMock(),
)


async def _capture_prompt(**kwargs):
    with patch("httpx.AsyncClient", _CapturingClient):
        await qwen_client.query_qwen_unified(**{**_ARGS, **kwargs})
    payload = _CapturingClient.captured["payload"]
    return payload["messages"][0]["content"], payload["messages"][1]["content"]


class TestPromptMemoriesBlock:
    """Bloque en USER tras las velas, SYSTEM intacto (H2), back-compat."""

    @pytest.mark.asyncio
    async def test_without_memories_prompt_is_backcompat(self):
        system, user = await _capture_prompt()
        assert "Relevant memories" not in user
        assert "Relevant memories" not in system
        system2, user2 = await _capture_prompt(memories=None)
        assert (system, user) == (system2, user2), (
            "memories=None debe producir exactamente el prompt sin memoria "
            "(back-compat byte-idéntica del prompt validado)"
        )

    @pytest.mark.asyncio
    async def test_block_lives_in_user_after_candles_before_news(self):
        _, user = await _capture_prompt(memories=_CANDIDATES)
        assert "Relevant memories from past trades" in user
        assert user.index("- Candles:") < user.index("Relevant memories")
        assert user.index("Relevant memories") < user.index("News:")

    @pytest.mark.asyncio
    async def test_system_message_is_untouched(self):
        system, _ = await _capture_prompt(memories=_CANDIDATES)
        assert "Relevant memories" not in system
        assert system == "You are a quantitative trading analyst. Always respond in valid JSON."

    @pytest.mark.asyncio
    async def test_lines_carry_source_marks_and_are_capped(self):
        memories = [
            {"texto": f"EURUSD memory number {i}", "categoria": "trade_insight",
             "score": 0.10 + i * 0.05}
            for i in range(5)   # contrato: el cliente corta a 3, pero el
        ]                       # prompt debe ser robusto a listas largas
        _, user = await _capture_prompt(memories=memories[:MEMORY_TOP_K])
        memory_lines = [l for l in user.splitlines() if l.startswith("- [")]
        assert len(memory_lines) == MEMORY_TOP_K
        assert all("trade_insight" in l and "d=0." in l for l in memory_lines), (
            "cada línea lleva marca de fuente (categoria + distancia) — el "
            "modelo infiere staleness y procedencia (punto 5 del diseño)"
        )


# ─── 5. Routing en /predict ───────────────────────────────────────────────────

def _candles(n: int = 250, base: float = 1.10, step: float = 0.0001) -> list[dict]:
    import pandas as pd
    times = pd.date_range("2026-01-01", periods=n, freq="h")
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


class _FakeMT5:
    """Fake httpx.AsyncClient de /predict (espejo test_qwen_h4d_context)."""

    candles: list = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, **kwargs):
        if "/api/v1/account/info" in url:
            return self._response({"equity": 1000.0, "balance": 1000.0})
        if "/api/v1/market/candles/latest" in url:
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


def _model() -> MagicMock:
    model = MagicMock()
    model.predict.return_value = (
        np.array([0.80, 0.18, 0.15, 0.15], dtype=np.float32), None,
    )
    model.observation_space = Box(-np.inf, np.inf, shape=(20, 25), dtype=np.float32)
    model.action_space = Box(-1.0, 1.0, shape=(4,), dtype=np.float32)
    return model


def _run_predict(monkeypatch, mode: str, position: int = 0,
                 candidates=_CANDIDATES):
    """Un ciclo /predict completo con memoria en modo `mode` — devuelve
    (qwen_mock, fetch_mock, audit_payload)."""
    _FakeMT5.candles = _candles()
    model = _model()
    qwen_mock = AsyncMock(return_value=dict(
        ok=True, quality=8.5, reason="Strong setup", bias="BULLISH",
        confidence_modifier=1.1, effective_prob=0.7, regime="TRENDING",
        reentry_ok=True,
    ))
    fetch_mock = AsyncMock(return_value=list(candidates))
    log_mock = AsyncMock()
    monkeypatch.setattr(settings, "memory_injection_mode", mode)
    with patch("main.init_pool", AsyncMock()), \
         patch("main.close_pool", AsyncMock()), \
         patch("routers.ai.is_news_blackout", AsyncMock(return_value=(False, None))), \
         patch("routers.ai.fetch_bars_since_close", AsyncMock(return_value=None)), \
         patch("routers.ai.get_pool", return_value=MagicMock(execute=AsyncMock())), \
         patch("routers.ai.PPO.load", return_value=model), \
         patch("services.news_scraper.get_macro_news", AsyncMock(return_value="No macro news")), \
         patch("routers.ai.query_qwen_unified", qwen_mock), \
         patch("routers.ai.get_upcoming_news", AsyncMock(return_value=[])), \
         patch("routers.ai.fetch_relevant_memories", fetch_mock), \
         patch("routers.ai.log_to_audit", log_mock), \
         patch("httpx.AsyncClient", _FakeMT5):
        with TestClient(app) as client:
            monkeypatch.setitem(
                routers.ai._BRAINS, "EURUSD",
                Brain(model=model, vec_norm=None, bundle={}, memory_channel=True),
            )
            res = client.post(
                "/api/v1/ai/predict",
                json={"symbol": "EURUSD", "timeframe": "H1", "position": position},
                headers={"x-internal-token": "test-token", "x-cycle-id": "t10-route"},
            )
    assert res.status_code == 200
    cycles = [
        c.args[1] for c in log_mock.await_args_list
        if c.args and c.args[0] == "ai_predict_cycle"
    ]
    assert cycles, "el ciclo debe auditar ai_predict_cycle"
    return qwen_mock, fetch_mock, cycles[-1]


class TestMemoryModeRouting:
    """off | shadow | on y el skip LR-1 — la memoria sólo decide la MODE."""

    def test_off_never_fetches_and_audits_null(self, monkeypatch):
        qwen_mock, fetch_mock, audit = _run_predict(monkeypatch, mode="off")
        fetch_mock.assert_not_awaited()
        assert audit["memory"] is None
        assert qwen_mock.call_args.kwargs["memories"] is None

    def test_shadow_fetches_but_never_injects(self, monkeypatch):
        qwen_mock, fetch_mock, audit = _run_predict(monkeypatch, mode="shadow")
        fetch_mock.assert_awaited_once()
        assert qwen_mock.call_args.kwargs["memories"] is None, (
            "shadow = CERO inyección (patrón estratega D-5): se traza lo que "
            "HABRÍA entrado, se decide el arme con evidencia de N días"
        )
        assert audit["memory"] == {
            "mode": "shadow", "candidates": 2, "injected": False,
            "top_distance": 0.22,
        }

    def test_on_injects_and_audits(self, monkeypatch):
        qwen_mock, fetch_mock, audit = _run_predict(monkeypatch, mode="on")
        assert qwen_mock.call_args.kwargs["memories"] == _CANDIDATES
        assert audit["memory"]["injected"] is True
        assert audit["memory"]["mode"] == "on"
        assert audit["memory"]["candidates"] == 2

    def test_on_with_empty_candidates_does_not_inject(self, monkeypatch):
        qwen_mock, _, audit = _run_predict(monkeypatch, mode="on", candidates=[])
        assert qwen_mock.call_args.kwargs["memories"] is None
        assert audit["memory"] == {
            "mode": "on", "candidates": 0, "injected": False, "top_distance": None,
        }

    def test_skip_lr1_does_not_fetch(self, monkeypatch):
        qwen_mock, fetch_mock, audit = _run_predict(
            monkeypatch, mode="on", position=1,
        )
        # El skip no fetcha: no hay prompt que enriquecer (sin llamada LLM).
        fetch_mock.assert_not_awaited()
        qwen_mock.assert_not_awaited()
        assert audit["memory"] is None
        assert audit["qwen_skipped"] is True


# ─── 6. Presupuesto de tokens ─────────────────────────────────────────────────

class TestTokenBudget:
    """Todos los bloques máximos + 3 memorias (contrato ≤30 palabras) caben
    en el hueco 2048 − respuesta. El techo 6400 chars del pack H4/D se
    amplía en el presupuesto del propio bloque de memoria (~700 chars):
    7100 chars ≈ 1775 t + 150 t respuesta ≈ 1925 t < 2048."""

    @pytest.mark.asyncio
    async def test_prompt_fits_budget_with_memories_maxed(self):
        from datetime import datetime, timezone
        block = (
            "- Higher-timeframe context (H4/D1) — contrast the H1 signal with "
            "the larger trend:\n"
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
        memories = [
            {"texto": "EURUSD " + "word " * 27, "categoria": "trade_insight",
             "score": 0.10 + i * 0.05}
            for i in range(MEMORY_TOP_K)
        ]   # contrato de población: ≤30 palabras por memoria
        _, user = await _capture_prompt(
            higher_tf_context=block, recent_insights=insights,
            calendar_events=calendar, memories=memories,
            news="Macro headline. " * 40,
        )
        assert len(user) <= 7100, (
            f"prompt {len(user)} chars con todos los bloques máximos excede "
            "el presupuesto (2048 − respuesta)"
        )
