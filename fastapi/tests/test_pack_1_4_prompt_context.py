"""
Paquete 1-4 (Félix 2026-10-09): contexto REAL en el prompt unificado de Qwen.

Criterio de aceptación por ítem:

1. Estructura → el prompt recibe `sma200_dist/slope` (SMA200 calculado sobre
   la serie COMPLETA de hasta 240 velas) y `support_level/resistance_level`
   (ya presentes en `df`). El obs NO cambia: las features siguen calculándose
   sobre las 120 velas más recientes (byte-idéntico 120 vs 240).
2. Calendario → `get_upcoming_news([symbol])` filtrado a −120…+240 min,
   ordenado ascendente y con tope 3 → línea `Calendar:` justo tras `News:`.
   Con `news_filter_enabled=False` no se consulta y el prompt lo dice
   explícitamente (`calendar_events=None`).
3. Trade journal → el bloque `recent_insights` (hoy muerto: se consultaba a
   la BD en cada ciclo y NUNCA se inyectaba) se renderiza SIEMPRE con
   esquema constante (cabecera + filas o `- none yet`), SIN
   `quality_score`/`qwen_confidence` (anti-ancla H2) y con la insight
   truncada a 48 caracteres. Tope de 5 filas (presupuesto de tokens).
4. Contrato sin SL/TP → `sl_ok/tp_ok/sl_adjusted/tp_adjusted` salen del
   prompt, del parse, del log y del dict de resultado: campos muertos en
   /predict (el único consumidor del sistema es el validador Opción 2,
   `query_qwen_sltp_validator`, que NO está cableado). `sl_proposed/
   tp_proposed` desaparecen de la firma y de puerto/adapter (paridad
   verificada con `inspect.signature`).

Cada clase fija una propiedad observable; RED → GREEN sin tocar umbrales,
`QUALITY_THRESHOLD` ni heurísticas de SL/TP (restricciones del sistema).
"""
import json
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient
from gymnasium.spaces import Box

import routers.ai
import services.news_filter as news_filter
import services.qwen_client as qwen_client
from config import settings
from main import app
from ml.trading_env_v2 import engineer_market_features as _real_engineer
from routers.ai import Brain


# ─── Datos sintéticos ─────────────────────────────────────────────────────────

def _candles(n: int = 240, t0: str = "2026-01-01", base: float = 1.10) -> list[dict]:
    """Velas horarias con closes crecientes, en orden DESCENDENTE
    (el orden real del MCP — P0: test_p0_time_order)."""
    times = pd.date_range(t0, periods=n, freq="h")
    rows = []
    for i, t in enumerate(times):
        close = round(base + i * 0.0001, 5)
        rows.append({
            "time": t.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "open": round(close - 0.0001, 5),
            "high": round(close + 0.0002, 5),
            "low": round(close - 0.0002, 5),
            "close": close,
            "tick_volume": 1000 + i,
        })
    return rows[::-1]


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
    # Bundle EURUSD activo = memoria (manifest obs_shape [20,25])
    model.observation_space = Box(-np.inf, np.inf, shape=(20, 25), dtype=np.float32)
    model.action_space = Box(-1.0, 1.0, shape=(4,), dtype=np.float32)
    dist = MagicMock()
    dist.distribution.mean.numpy.return_value = np.array([0.1, 0.1, 0.1, 0.1], dtype=np.float32)
    dist.distribution.stddev.numpy.return_value = np.array([0.5, 0.5, 0.5, 0.5], dtype=np.float32)
    model.policy.get_distribution.return_value = dist
    return model


class _FakeMT5:
    """Fake httpx.AsyncClient de /predict: MT5 (account/candles), calendario
    (URL desconocida → `{}` → 0 eventos) y Qwen (POST → `{}` — los tests de
    wiring mockean `routers.ai.query_qwen_unified`, no el transporte).

    `candles` y `requests` son atributos de CLASE: los resetea `_serve` y los
    muta el test de byte-identidad entre ciclos."""

    candles: list = []
    requests: list = []

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
def _serve(monkeypatch, model, candles, qwen=None, upcoming=()):
    """Stack de /predict espejo de test_p0 + calendario y Qwen mockados.

    El startup carga el bundle real del repo en `_BRAINS`; el brain bajo
    prueba se inyecta DESPUÉS del enter (setitem) para servir con
    `vec_norm=None` (obs crudo, byte-comparable)."""
    _FakeMT5.candles = candles
    _FakeMT5.requests = []
    qwen_mock = AsyncMock(return_value=dict(_QWEN_OK if qwen is None else qwen))
    cal_mock = AsyncMock(return_value=list(upcoming))
    with patch("main.init_pool", AsyncMock()), \
         patch("main.close_pool", AsyncMock()), \
         patch("routers.ai.is_news_blackout", AsyncMock(return_value=(False, None))), \
         patch("routers.ai.fetch_bars_since_close", AsyncMock(return_value=None)), \
         patch("routers.ai.get_pool", return_value=MagicMock(execute=AsyncMock())), \
         patch("routers.ai.PPO.load", return_value=model), \
         patch("services.news_scraper.get_macro_news", AsyncMock(return_value="No macro news")), \
         patch("routers.ai.query_qwen_unified", qwen_mock), \
         patch("routers.ai.get_upcoming_news", cal_mock), \
         patch("httpx.AsyncClient", _FakeMT5):
        with TestClient(app) as client:
            monkeypatch.setitem(
                routers.ai._BRAINS, "EURUSD",
                Brain(model=model, vec_norm=None, bundle={}, memory_channel=True),
            )
            yield client, qwen_mock, cal_mock


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
    cycle_id="pack14-unified", logger=MagicMock(),
)

# Canned que ECOA campos SL/TP: el contrato nuevo los debe DESCARTAR
# (tolerancia al modelo — no trae campos muertos en el prompt).
_ECHO_CANNED = (
    '{"quality": 5.0, "reason": "echo test", "bias": "NEUTRAL",'
    ' "confidence_modifier": 1.0, "sl_adjusted": 0.50, "tp_adjusted": 0.80,'
    ' "regime": "RANGING"}'
)

# El sentinel `_PAST48` cae EXACTAMENTE en la posición 48 → si el formatter
# no trunca, aparece en el prompt (y delata el desborde).
_JOURNAL_ROW = {
    "symbol": "EURUSD",
    "created_at": datetime(2026, 10, 8, 18, 0, tzinfo=timezone.utc),
    "direction": "SHORT",
    "pnl_pct": Decimal("-0.0091"),
    "exit_reason": "ea",
    "qwen_insight": "timing_error " + "w" * 35 + "_PAST48",
    "qwen_confidence": 0.93,      # centinela anti-ancla: NO debe renderizar
    "regime": "RANGING",
    "quality_score": Decimal("9.5"),   # centinela anti-ancla: NO debe renderizar
}


# ─── 1. SMA200: helper puro ──────────────────────────────────────────────────

class TestSma200Context:
    """`routers.ai._sma200_context` — (distancia, pendiente) del SMA200 sobre
    la serie ASCENDENTE completa. Guardas: sin `time`, <205 velas (SMA200 +
    5 velas de margen para la pendiente) o NaN en los puntos → (None, None):
    contexto de calidad, nunca inventado."""

    @staticmethod
    def _frame(n: int = 240, with_time: bool = True, nan_at: int | None = None) -> pd.DataFrame:
        times = pd.date_range("2026-01-01", periods=n, freq="h")
        closes = [1.1 + i * 0.0001 for i in range(n)]
        if nan_at is not None:
            closes[nan_at] = float("nan")
        data = {"close": closes}
        if with_time:
            data["time"] = [t.strftime("%Y-%m-%dT%H:%M:%SZ") for t in times]
        return pd.DataFrame(data)

    def test_linear_series_gives_distance_and_slope(self):
        dist, slope = routers.ai._sma200_context(self._frame(240))
        # close[-1]=1.1239; SMA200 (medias de i=40..239) = 1.11395
        assert dist == pytest.approx(0.00995, abs=1e-6)
        # la media se desplaza 5 barras por ciclo → pendiente = 5·0.0001
        assert slope == pytest.approx(0.0005, abs=1e-6)

    def test_shorter_than_205_bars_returns_none(self):
        # 204 velas: sma.iloc[-6] (índice 198) aún es NaN → sin contexto
        assert routers.ai._sma200_context(self._frame(204)) == (None, None)

    def test_205_bars_is_the_minimum_with_values(self):
        dist, slope = routers.ai._sma200_context(self._frame(205))
        assert dist is not None
        assert slope is not None

    def test_nan_guard_returns_none(self):
        # NaN en la última vela → sma.iloc[-1] NaN → (None, None), nunca NaN
        assert routers.ai._sma200_context(self._frame(240, nan_at=-1)) == (None, None)

    def test_missing_time_column_returns_none(self):
        assert routers.ai._sma200_context(self._frame(240, with_time=False)) == (None, None)


# ─── 1+2. Cableado en /predict ───────────────────────────────────────────────

class TestPredictWiring:
    """Cableado extremo a extremo: fetch de 240 velas, helper SMA200,
    niveles estructurales reutilizados, calendario filtrado/limitado y
    OBS BYTE-IDÉNTICO con cualquier profundidad de fetch."""

    def test_candles_fetch_requests_240(self, monkeypatch):
        model = _model()
        with _serve(monkeypatch, model, _candles(240)) as (client, *_):
            resp = _post(client, "pack14-count")
        assert resp.status_code == 200, resp.text
        candle_urls = [u for u in _FakeMT5.requests if "market/candles/latest" in u]
        assert candle_urls, "sin fetch de velas"
        assert "count=240" in candle_urls[0], (
            f"la serie completa debe llegar para el SMA200: {candle_urls[0]}"
        )

    def test_structure_context_forwarded_to_qwen(self, monkeypatch):
        model = _model()
        with _serve(monkeypatch, model, _candles(240)) as (client, qwen_mock, _):
            resp = _post(client, "pack14-struct")
        assert resp.status_code == 200, resp.text
        kw = qwen_mock.call_args.kwargs
        assert kw["sma200_dist"] == pytest.approx(0.00995, abs=1e-6)
        assert kw["sma200_slope"] == pytest.approx(0.0005, abs=1e-6)
        assert kw["support_level"] is not None
        assert kw["resistance_level"] is not None
        # Item 4: la firma ya no arrastra los campos muertos de SL/TP
        assert "sl_proposed" not in kw
        assert "tp_proposed" not in kw

    def test_engineer_receives_newest_120_ascending(self, monkeypatch):
        """Las features se calculan sobre las 120 velas MÁS RECIENTES en
        orden ascendente — el contrato de obs no cambia con el fetch de 240."""
        model = _model()
        with _serve(monkeypatch, model, _candles(240)) as (client, *_):
            with patch("routers.ai.engineer_market_features", wraps=_real_engineer) as eng:
                resp = _post(client, "pack14-engineer")
            assert resp.status_code == 200, resp.text
        (raw_input,) = eng.call_args.args
        assert len(raw_input) == 120, f"features deben salir de 120 velas, no {len(raw_input)}"
        assert raw_input["time"].is_monotonic_increasing
        assert raw_input["time"].iloc[0] == "2026-01-06T00:00:00Z"
        assert raw_input["time"].iloc[-1] == "2026-01-10T23:00:00Z"

    def test_calendar_filtered_sorted_and_capped(self, monkeypatch):
        events = [
            {"title": "Way later", "currency": "USD",
             "time_utc": "2026-10-10T09:00:00+00:00", "minutes_until": 600.0},
            {"title": "Recent", "currency": "USD",
             "time_utc": "2026-10-09T10:00:00+00:00", "minutes_until": -60.0},
            {"title": "Too late", "currency": "USD",
             "time_utc": "2026-10-09T20:00:00+00:00", "minutes_until": 400.0},
            {"title": "Later", "currency": "USD",
             "time_utc": "2026-10-09T14:00:00+00:00", "minutes_until": 120.0},
            {"title": "Too old", "currency": "USD",
             "time_utc": "2026-10-09T02:00:00+00:00", "minutes_until": -500.0},
            {"title": "Soon", "currency": "USD",
             "time_utc": "2026-10-09T11:15:00+00:00", "minutes_until": 15.0},
        ]
        model = _model()
        with _serve(monkeypatch, model, _candles(240), upcoming=events) as (client, qwen_mock, cal_mock):
            resp = _post(client, "pack14-cal")
        assert resp.status_code == 200, resp.text
        cal_mock.assert_awaited_once_with(["EURUSD"])
        titles = [ev["title"] for ev in qwen_mock.call_args.kwargs["calendar_events"]]
        # ventana −120…+240 min, orden ascendente, tope 3
        assert titles == ["Recent", "Soon", "Later"]

    def test_news_filter_disabled_skips_calendar(self, monkeypatch):
        monkeypatch.setattr(settings, "news_filter_enabled", False)
        model = _model()
        with _serve(monkeypatch, model, _candles(240)) as (client, qwen_mock, cal_mock):
            resp = _post(client, "pack14-nocal")
        assert resp.status_code == 200, resp.text
        cal_mock.assert_not_awaited()
        assert qwen_mock.call_args.kwargs["calendar_events"] is None

    def test_obs_byte_identical_whatever_fetch_depth(self, monkeypatch):
        """Mismo modelo + mismas 120 velas recientes → obs byte-idéntico
        vengan del fetch que vengan: el SMA200 usa la serie larga, las
        features nunca la ven (prevención de train/serve skew)."""
        model = _model()
        with _serve(monkeypatch, model, _candles(120)) as (client, *_):
            r1 = _post(client, "pack14-obs-a")
            assert r1.status_code == 200, r1.text
            obs_a = model.predict.call_args.args[0].copy()
            # mismo ciclo con 240 velas: 120 viejas (base=1.05) + las mismas 120 nuevas
            _FakeMT5.candles = _candles(120) + _candles(120, t0="2025-12-27", base=1.05)
            r2 = _post(client, "pack14-obs-b")
            assert r2.status_code == 200, r2.text
            obs_b = model.predict.call_args.args[0].copy()
        assert obs_a.shape == (20, 25)
        assert obs_a.tobytes() == obs_b.tobytes()


# ─── 2. Línea de calendario ──────────────────────────────────────────────────

class TestCalendarLine:
    """`qwen_client._calendar_line` — formato único de la línea `Calendar:`."""

    _EVENTS = [
        {"title": "Nonfarm Payrolls", "currency": "USD",
         "time_utc": "2026-10-09T12:30:00+00:00", "minutes_until": 25.0},
        {"title": "CPI y/y", "currency": "USD",
         "time_utc": "2026-10-09T08:00:00+00:00", "minutes_until": -30.0},
    ]

    def test_none_means_filter_disabled(self):
        assert qwen_client._calendar_line(None) == (
            "Calendar: unavailable (news filter disabled)"
        )

    def test_empty_means_no_events_in_window(self):
        assert qwen_client._calendar_line([]) == "Calendar: no events in next 4h"

    def test_events_line_format(self):
        assert qwen_client._calendar_line(self._EVENTS) == (
            "Calendar: 12:30 USD Nonfarm Payrolls (in 25 min) | "
            "08:00 USD CPI y/y (30 min ago)"
        )

    def test_title_truncated_at_40_chars(self):
        title = "Very long event title " + "x" * 50
        line = qwen_client._calendar_line([
            {"title": title, "currency": "EUR",
             "time_utc": "2026-10-09T14:00:00+00:00", "minutes_until": 10.0},
        ])
        assert title[:40] in line
        assert title[:41] not in line

    @pytest.mark.asyncio
    async def test_prompt_placement_between_news_and_ml_signal(self):
        client = _capturing_client(
            '{"quality": 6.0, "reason": "ok", "bias": "NEUTRAL",'
            ' "confidence_modifier": 1.0, "regime": "RANGING"}'
        )
        with patch("httpx.AsyncClient", client):
            result = await qwen_client.query_qwen_unified(
                **{**_UNIFIED_ARGS, "calendar_events": self._EVENTS},
            )
        assert result["ok"] is True, "canned debe parsear (setup del test)"
        prompt = client.captured["prompt"]
        assert prompt.index("News:") < prompt.index("Calendar:") < prompt.index("ML Signal:")


# ─── 3. Bloque de trade journal ──────────────────────────────────────────────

class TestTradeJournalBlock:
    """Bloque `Trade journal (recent closes)` — esquema SIEMPRE constante
    (anti-ancla H2): cabecera + filas o `- none yet`, sin
    `quality_score`/`qwen_confidence`, insight truncada a 48 chars, tope 5."""

    @staticmethod
    async def _journal_prompt(insights) -> str:
        client = _capturing_client(
            '{"quality": 6.0, "reason": "ok", "bias": "NEUTRAL",'
            ' "confidence_modifier": 1.0, "regime": "RANGING"}'
        )
        with patch("httpx.AsyncClient", client):
            result = await qwen_client.query_qwen_unified(
                **{**_UNIFIED_ARGS, "recent_insights": insights},
            )
        assert result["ok"] is True, "canned debe parsear (setup del test)"
        return client.captured["prompt"]

    @pytest.mark.asyncio
    async def test_row_rendered_with_pnl_and_truncated_insight(self):
        prompt = await self._journal_prompt([_JOURNAL_ROW])
        insight = _JOURNAL_ROW["qwen_insight"]
        assert insight[:48] == "timing_error " + "w" * 35
        row_line = f"- 10-08 18:00 SHORT pnl=-0.91% exit=ea regime=RANGING: {insight[:48]}"
        assert row_line in prompt
        assert "_PAST48" not in prompt, "la insight debe truncarse a 48 caracteres"

    @pytest.mark.asyncio
    async def test_quality_and_confidence_are_never_rendered(self):
        """Anti-ancla H2: el journal es contexto de CIERRES, no de puntuación
        — los valores de quality/confidence de filas pasadas anclarían la
        respuesta del modelo."""
        prompt = await self._journal_prompt([_JOURNAL_ROW])
        assert "9.5" not in prompt
        assert "0.93" not in prompt

    @pytest.mark.asyncio
    @pytest.mark.parametrize("insights", [[], None], ids=["empty", "none"])
    async def test_header_and_none_yet_always_present(self, insights):
        prompt = await self._journal_prompt(insights)
        assert "Trade journal (recent closes) — context only — do NOT copy values:" in prompt
        assert "- none yet" in prompt

    @pytest.mark.asyncio
    async def test_placement_after_ml_signal_before_reentry(self):
        prompt = await self._journal_prompt([_JOURNAL_ROW])
        assert prompt.index("ML Signal") < prompt.index("Trade journal")
        assert prompt.index("Trade journal") < prompt.index("Reentry check")

    @pytest.mark.asyncio
    async def test_capped_at_five_rows(self):
        rows = [{**_JOURNAL_ROW, "qwen_insight": f"insight_{i}"} for i in range(7)]
        prompt = await self._journal_prompt(rows)
        assert "insight_4" in prompt
        assert "insight_5" not in prompt, "el journal debe limitarse a 5 filas"


# ─── 4. Contrato unificado sin SL/TP ─────────────────────────────────────────

class TestUnifiedContractWithoutSlTp:
    """El contrato `/predict`→Qwen deja de arrastrar campos muertos de SL/TP
    (prompt, parse, log, resultado, firma, puerto y adapter). El validador
    Opción 2 (`query_qwen_sltp_validator`) se conserva SIN cablear."""

    @staticmethod
    async def _prompt_and_result() -> tuple[str, dict]:
        client = _capturing_client(_ECHO_CANNED)
        with patch("httpx.AsyncClient", client):
            result = await qwen_client.query_qwen_unified(**_UNIFIED_ARGS)
        return client.captured["prompt"], result

    @pytest.mark.asyncio
    async def test_prompt_has_no_sl_tp_contract_tokens(self):
        prompt, result = await self._prompt_and_result()
        assert result["ok"] is True, "canned debe parsear (setup del test)"
        for token in ("sl_ok", "tp_ok", "sl_adjusted", "tp_adjusted", "TP:SL"):
            assert token not in prompt, f"campo SL/TP muerto en el prompt: {token}"

    @pytest.mark.asyncio
    async def test_echoed_sl_tp_fields_are_dropped(self):
        """El modelo puede ecoar campos que ya no se le piden: el parse los
        DESCARTA (hoy se recortaban a bounds y viajaban al serve)."""
        _, result = await self._prompt_and_result()
        assert result["ok"] is True
        assert "sl_adjusted" not in result
        assert "tp_adjusted" not in result

    @pytest.mark.asyncio
    async def test_degraded_result_has_no_sl_tp(self):
        client = MagicMock()
        client.__aenter__.return_value = client
        client.post = AsyncMock(side_effect=httpx.ConnectTimeout("timeout"))
        with patch("httpx.AsyncClient", return_value=client):
            result = await qwen_client.query_qwen_unified(**_UNIFIED_ARGS)
        assert result["ok"] is False
        assert "sl_adjusted" not in result
        assert "tp_adjusted" not in result

    def test_degraded_helper_contract_is_three_args(self):
        out = qwen_client._degraded_unified("TRENDING", 0.33, "Qwen unavailable")
        assert out == {
            "ok": False,
            "quality": 0.0,
            "reason": "Qwen unavailable",
            "bias": "NEUTRAL",
            "confidence_modifier": 1.0,
            "effective_prob": 0.33,
            "regime": "TRENDING",
        }

    def test_signature_drops_sl_and_carries_new_context(self):
        import inspect
        params = set(inspect.signature(qwen_client.query_qwen_unified).parameters)
        assert "sl_proposed" not in params
        assert "tp_proposed" not in params
        for name in (
            "adx", "regime", "sma200_dist", "sma200_slope",
            "support_level", "resistance_level", "reentry", "calendar_events",
        ):
            assert name in params, f"falta {name} en la firma del unificado"

    def test_ai_router_no_longer_imports_dead_validator(self):
        assert not hasattr(routers.ai, "query_qwen_sltp_validator"), (
            "el import muerto de Opción 2 debe salir de /predict "
            "(el servicio se conserva, no se cablea)"
        )

    def test_port_adapter_signature_parity(self):
        import inspect
        from core.adapters import QwenLLMAdapter
        from core.ports import LLMPort

        unified = set(inspect.signature(qwen_client.query_qwen_unified).parameters)
        port = set(inspect.signature(LLMPort.query_unified).parameters) - {"self"}
        adapter = set(inspect.signature(QwenLLMAdapter.query_unified).parameters) - {"self"}
        assert port == unified, f"LLMPort desincronizado: {sorted(port ^ unified)}"
        assert adapter == unified, f"QwenLLMAdapter desincronizado: {sorted(adapter ^ unified)}"

    @pytest.mark.asyncio
    async def test_adapter_forwards_new_context_params(self):
        from core.adapters import QwenLLMAdapter

        with patch(
            "services.qwen_client.query_qwen_unified",
            AsyncMock(return_value={"ok": True}),
        ) as m:
            await QwenLLMAdapter().query_unified(
                symbol="EURUSD", decision="BUY", ml_prob=0.61, rsi=55.0,
                atr=0.0012, atr_pct=0.11, macd_hist=0.0001, bb_pos=0.7,
                range_pct=0.4, last_ret=0.0002, hour=10, news="none",
                candle_context="h1", cycle_id="cyc-1", logger=MagicMock(),
                adx=0.31, regime="TRENDING", sma200_dist=0.00995,
                sma200_slope=0.0005, support_level=1.09500,
                resistance_level=1.10500, reentry=None,
                calendar_events=[{"title": "NFP"}],
            )
        kw = m.await_args.kwargs
        assert kw["adx"] == 0.31
        assert kw["regime"] == "TRENDING"
        assert kw["sma200_dist"] == 0.00995
        assert kw["sma200_slope"] == 0.0005
        assert kw["support_level"] == 1.09500
        assert kw["resistance_level"] == 1.10500
        assert kw["reentry"] is None
        assert kw["calendar_events"] == [{"title": "NFP"}]
        assert "sl_proposed" not in kw
        assert "tp_proposed" not in kw


# ─── 2. Backoff del fetch del calendario ─────────────────────────────────────

class TestCalendarFetchBackoff:
    """`news_filter._fetch_calendar` — el fallo ARMa el backoff.

    Sin este fix, el gate `… < TTL and _cache` ignora el timestamp en
    caché-fría: cada ciclo de /predict pagaría el timeout de 15 s DOS veces
    (blackout + prompt) mientras dure la caída. Con el fix: el fallo fija
    `_cache_ts`, no se reintenta dentro del TTL, y el fallo en caliente
    sirve la caché vieja (fail-open, ya existente) y rearma."""

    @pytest.fixture(autouse=True)
    def _reset(self, monkeypatch):
        monkeypatch.setattr(news_filter, "_cache", [])
        monkeypatch.setattr(news_filter, "_cache_ts", 0.0)

    @staticmethod
    def _failing_client(attempts: list):
        class _Fail:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def get(self, *args, **kwargs):
                attempts.append(1)
                raise httpx.ConnectTimeout("calendar caído")

        return _Fail

    @staticmethod
    def _ok_client(events: list, attempts: list):
        class _Ok:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def get(self, *args, **kwargs):
                attempts.append(1)
                response = MagicMock()
                response.raise_for_status = MagicMock()
                response.json = MagicMock(return_value=events)
                return response

        return _Ok

    @staticmethod
    def _event(title: str) -> list[dict]:
        return [{
            "title": title, "country": "USD", "impact": "High",
            "date": "2026-10-10T12:00:00+00:00",
        }]

    @pytest.mark.asyncio
    async def test_cold_failure_arms_backoff_and_skips_retry(self):
        attempts: list = []
        with patch("httpx.AsyncClient", self._failing_client(attempts)):
            first = await news_filter._fetch_calendar()
            second = await news_filter._fetch_calendar()
        assert first == []
        assert second == []
        assert len(attempts) == 1, (
            "fallo en frío debe armar backoff: sin reintento dentro del TTL"
        )
        assert news_filter._cache_ts > 0, "el fallo debe fijar _cache_ts (backoff)"

    @pytest.mark.asyncio
    async def test_warm_failure_serves_stale_and_rearms(self):
        ok_attempts: list = []
        with patch("httpx.AsyncClient", self._ok_client(self._event("NFP"), ok_attempts)):
            warm = await news_filter._fetch_calendar()
        assert [e["title"] for e in warm] == ["NFP"]

        # expirar el TTL para forzar el próximo intento de red
        news_filter._cache_ts = time.time() - news_filter._CACHE_TTL - 1
        fail_attempts: list = []
        with patch("httpx.AsyncClient", self._failing_client(fail_attempts)):
            stale = await news_filter._fetch_calendar()
            assert [e["title"] for e in stale] == ["NFP"], "fallo en caliente sirve caché"
            again = await news_filter._fetch_calendar()
        assert [e["title"] for e in again] == ["NFP"]
        assert len(fail_attempts) == 1, "el fallo en caliente rearma el backoff"
        assert news_filter._cache_ts > time.time() - news_filter._CACHE_TTL, (
            "tras el fallo, _cache_ts debe quedar rearmed dentro del TTL"
        )

    @pytest.mark.asyncio
    async def test_success_fetches_again_after_ttl(self):
        attempts: list = []
        with patch("httpx.AsyncClient", self._ok_client(self._event("OLD"), attempts)):
            first = await news_filter._fetch_calendar()
        assert [e["title"] for e in first] == ["OLD"]

        news_filter._cache_ts = time.time() - news_filter._CACHE_TTL - 1
        with patch("httpx.AsyncClient", self._ok_client(self._event("NEW"), attempts)):
            second = await news_filter._fetch_calendar()
        assert [e["title"] for e in second] == ["NEW"], "TTL expirado → refetch, no caché vieja"
        assert len(attempts) == 2
