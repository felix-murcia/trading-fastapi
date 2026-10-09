"""
Tests for services/qwen_client.py — Qwen LLM client extracted from routers/ai.py.

Covers:
- Successful JSON response parsing
- Connection error fallback (returns safe defaults)
- HTTP error handling
- Invalid JSON handling
- SL/TP validator logic
"""
import pytest
from unittest.mock import patch, MagicMock, AsyncMock
import httpx

import services.qwen_client as qwen_client
from services.qwen_client import (
    QwenHealthState,
    get_qwen_state,
    query_qwen_unified,
    query_qwen_sltp_validator,
)


@pytest.fixture(autouse=True)
def fresh_qwen_state(monkeypatch):
    """Estado de Qwen aislado por test — el módulo es estado compartido."""
    monkeypatch.setattr(qwen_client, "_QWEN_STATE", QwenHealthState())


_UNIFIED_BASE = dict(
    symbol="EURUSD", decision="BUY", ml_prob=0.75, rsi=55.0, atr=0.00100,
    atr_pct=0.01, macd_hist=0.0005, bb_pos=0.6, range_pct=0.02, last_ret=0.001,
    hour=10, news="No major news", candle_context="last 10 H1 candles",
    sl_proposed=0.20, tp_proposed=0.40, cycle_id="EURUSD_1234567890",
    logger=MagicMock(),
)


async def _call_unified(**overrides):
    return await query_qwen_unified(**{**_UNIFIED_BASE, **overrides})


class TestQueryQwenUnified:
    """Tests for the unified Qwen query function."""

    @pytest.fixture
    def mock_success_response(self):
        """Mock successful Qwen response with valid JSON."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "choices": [{
                "message": {
                    "content": '{"quality": 7.5, "reason": "good setup", "bias": "BULLISH", "confidence_modifier": 1.2, "sl_adjusted": 0.20, "tp_adjusted": 0.28, "regime": "TRENDING"}'
                }
            }]
        }
        return mock_response

    @pytest.mark.asyncio
    async def test_successful_response_parses_correctly(self, mock_success_response):
        """Should parse JSON response and return structured data."""
        with patch("httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__.return_value.post = AsyncMock(
                return_value=mock_success_response
            )

            result = await query_qwen_unified(
                symbol="EURUSD",
                decision="BUY",
                ml_prob=0.75,
                rsi=55.0,
                atr=0.00100,
                atr_pct=0.01,
                macd_hist=0.0005,
                bb_pos=0.6,
                range_pct=0.02,
                last_ret=0.001,
                hour=10,
                news="No major news",
                candle_context="last 10 H1 candles",
                sl_proposed=0.20,
                tp_proposed=0.40,
                cycle_id="EURUSD_1234567890",
                logger=MagicMock(),
            )

        assert result["quality"] == 7.5
        assert result["bias"] == "BULLISH"
        assert result["confidence_modifier"] == 1.2
        assert result["regime"] == "TRENDING"
        assert result["sl_adjusted"] == 0.20
        assert result["tp_adjusted"] == 0.40  # sl_adj * 2.0
        assert result["ok"] is True

    @pytest.mark.asyncio
    async def test_connection_error_returns_safe_defaults(self):
        """Should return safe defaults when Qwen is unreachable."""
        with patch("httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__.return_value.post = AsyncMock(
                side_effect=httpx.ConnectTimeout("timeout")
            )

            result = await _call_unified()

        assert result["quality"] == 0.0
        assert result["bias"] == "NEUTRAL"
        assert result["confidence_modifier"] == 1.0
        assert result["sl_adjusted"] == 0.20
        assert result["tp_adjusted"] == 0.40
        assert result["ok"] is False
        assert result["reason"] == "Qwen unavailable"

    @pytest.mark.asyncio
    async def test_invalid_json_degrades_to_safe_defaults(self):
        """P1-6: basura del LLM → safe defaults + estado degradado (nunca crash).

        Antes lanzaba RuntimeError y cascateaba un 500 en /predict sin dejar
        rastro en /pipeline/health; ahora degrada con razón explícita.
        """
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "choices": [{
                "message": {
                    "content": "This is not JSON at all"
                }
            }]
        }

        with patch("httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__.return_value.post = AsyncMock(
                return_value=mock_response
            )

            result = await _call_unified()

        assert result["ok"] is False
        assert result["quality"] == 0.0
        assert "sin JSON" in result["reason"]
        assert result["effective_prob"] == 0.75  # ml_prob intacto
        state = get_qwen_state()
        assert state.consecutive_failures == 1
        assert "sin JSON" in state.last_error

    @pytest.mark.asyncio
    async def test_http_500_degrades_without_crash(self):
        """P1-6: HTTP 500 del servidor LLM → safe defaults, nunca raise."""
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.text = "boom"

        with patch("httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__.return_value.post = AsyncMock(
                return_value=mock_response
            )

            result = await _call_unified()

        assert result["ok"] is False
        assert result["quality"] == 0.0
        assert result["reason"] == "Qwen degraded: HTTP 500"
        state = get_qwen_state()
        assert state.consecutive_failures == 1
        assert "HTTP 500" in state.last_error

    @pytest.mark.asyncio
    async def test_success_updates_health_state(self, mock_success_response):
        with patch("httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__.return_value.post = AsyncMock(
                return_value=mock_success_response
            )
            result = await _call_unified()

        assert result["ok"] is True
        state = get_qwen_state()
        assert (state.calls_ok, state.calls_failed, state.consecutive_failures) == (1, 0, 0)
        assert state.last_ok_at is not None
        assert state.last_error is None

    @pytest.mark.asyncio
    async def test_connection_error_updates_health_state(self):
        with patch("httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__.return_value.post = AsyncMock(
                side_effect=httpx.ConnectTimeout("timeout")
            )
            result = await _call_unified()

        assert result["ok"] is False
        state = get_qwen_state()
        assert state.calls_failed == 1
        assert state.consecutive_failures == 1
        assert state.last_ok_at is None
        assert state.last_error.startswith("connection")

    @pytest.mark.asyncio
    async def test_recovery_resets_failure_streak(self, mock_success_response):
        with patch("httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__.return_value.post = AsyncMock(
                side_effect=httpx.ConnectTimeout("timeout")
            )
            await _call_unified()
        assert get_qwen_state().consecutive_failures == 1

        with patch("httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__.return_value.post = AsyncMock(
                return_value=mock_success_response
            )
            await _call_unified()

        state = get_qwen_state()
        assert state.consecutive_failures == 0
        assert (state.calls_ok, state.calls_failed) == (1, 1)
        assert state.last_error is None

    @pytest.mark.asyncio
    async def test_confidence_modifier_clamped(self):
        """Should clamp confidence_modifier to [0.5, 1.5]."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "choices": [{
                "message": {
                    "content": '{"quality": 5.0, "reason": "test", "bias": "NEUTRAL", "confidence_modifier": 2.5, "sl_adjusted": 0.20, "tp_adjusted": 0.40, "regime": "RANGING"}'
                }
            }]
        }

        with patch("httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__.return_value.post = AsyncMock(
                return_value=mock_response
            )

            result = await query_qwen_unified(
                symbol="EURUSD",
                decision="BUY",
                ml_prob=0.75,
                rsi=55.0,
                atr=0.00100,
                atr_pct=0.01,
                macd_hist=0.0005,
                bb_pos=0.6,
                range_pct=0.02,
                last_ret=0.001,
                hour=10,
                news="No major news",
                candle_context="last 10 H1 candles",
                sl_proposed=0.20,
                tp_proposed=0.40,
                cycle_id="EURUSD_1234567890",
                logger=MagicMock(),
            )

        assert result["confidence_modifier"] == 1.5

    @pytest.mark.asyncio
    async def test_sl_tp_clamped_to_bounds(self):
        """Should clamp SL/TP to configured bounds."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "choices": [{
                "message": {
                    "content": '{"quality": 5.0, "reason": "test", "bias": "NEUTRAL", "confidence_modifier": 1.0, "sl_adjusted": 0.50, "tp_adjusted": 0.80, "regime": "RANGING"}'
                }
            }]
        }

        with patch("httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__.return_value.post = AsyncMock(
                return_value=mock_response
            )

            result = await query_qwen_unified(
                symbol="EURUSD",
                decision="BUY",
                ml_prob=0.75,
                rsi=55.0,
                atr=0.00100,
                atr_pct=0.01,
                macd_hist=0.0005,
                bb_pos=0.6,
                range_pct=0.02,
                last_ret=0.001,
                hour=10,
                news="No major news",
                candle_context="last 10 H1 candles",
                sl_proposed=0.20,
                tp_proposed=0.40,
                cycle_id="EURUSD_1234567890",
                logger=MagicMock(),
            )

        assert result["sl_adjusted"] <= 0.30  # SL_MAX_NORM
        assert result["tp_adjusted"] <= 0.50  # TP_MAX_NORM


class TestReentryContext:
    """R1: bloque «Reentry check» del prompt unificado + parse de `reentry_ok`.

    El veto de re-entrada dejó de ser una constante (`churn_cooldown`) y
    pasa a ser un juicio del cerebro: estos tests fijan (1) que el prompt
    SIEMPRE lleva el bloque (esquema de respuesta constante — estable
    frente al anti-ancla H2), (2) el contexto que recibe (ventana, cierre,
    movimiento) y (3) el parse con default fail-open documentado."""

    _REENTRY = {
        "bars_since_close": 1,
        "last_exit": {
            "exit_time": "2026-10-09T13:10:00+00:00",
            "exit_reason": "ea",
            "pnl": -0.08,
            "direction": "SHORT",
            "is_win": False,
            "price_move_since_close_pips": 2.5,
        },
    }

    @staticmethod
    def _mock_qwen(content: str):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "choices": [{"message": {"content": content}}]
        }
        # Patrón probado en la suite: `async with` cede __aenter__.return_value
        client = MagicMock()
        client.__aenter__.return_value = client
        client.post = AsyncMock(return_value=mock_response)
        return client

    @staticmethod
    def _prompt_of(mock_client) -> str:
        call = mock_client.post.call_args
        return call.kwargs["json"]["messages"][1]["content"]

    @pytest.mark.asyncio
    async def test_prompt_carries_reentry_context(self):
        """Con cierre reciente: ventana, detalle del cierre y rúbrica."""
        client = self._mock_qwen(
            '{"quality": 8.0, "reason": "ok", "bias": "BULLISH",'
            ' "confidence_modifier": 1.1, "sl_adjusted": 0.20,'
            ' "tp_adjusted": 0.40, "regime": "TRENDING", "reentry_ok": true}'
        )
        with patch("httpx.AsyncClient", return_value=client):
            result = await query_qwen_unified(**{**_UNIFIED_BASE, "reentry": self._REENTRY})

        assert result["ok"] is True
        prompt = self._prompt_of(client)
        assert "Reentry check" in prompt
        assert "Bars since close: 1 H1" in prompt
        assert "SHORT (ea)" in prompt
        assert "-0.08" in prompt
        assert "+2.5 pips" in prompt
        assert "reentry_ok=false" in prompt   # rúbrica churn

    @pytest.mark.asyncio
    async def test_prompt_without_recent_close_instructs_true(self):
        """Fuera de ventana / sin entrada: bloque presente (esquema
        constante) con la instrucción explícita reentry_ok=true."""
        client = self._mock_qwen(
            '{"quality": 8.0, "reason": "ok", "bias": "BULLISH",'
            ' "confidence_modifier": 1.1, "sl_adjusted": 0.20,'
            ' "tp_adjusted": 0.40, "regime": "TRENDING", "reentry_ok": true}'
        )
        with patch("httpx.AsyncClient", return_value=client):
            result = await query_qwen_unified(**_UNIFIED_BASE)

        assert result["ok"] is True
        prompt = self._prompt_of(client)
        assert "Reentry check" in prompt
        assert "Set reentry_ok=true" in prompt

    @pytest.mark.asyncio
    async def test_prompt_exit_details_unavailable(self):
        """Cierre registrado pero sin detalles: el cerebro juzga sólo con
        las velas (None honesto, nunca un cierre inventado)."""
        client = self._mock_qwen(
            '{"quality": 8.0, "reason": "ok", "bias": "BULLISH",'
            ' "confidence_modifier": 1.1, "sl_adjusted": 0.20,'
            ' "tp_adjusted": 0.40, "regime": "TRENDING", "reentry_ok": true}'
        )
        with patch("httpx.AsyncClient", return_value=client):
            await query_qwen_unified(**{
                **_UNIFIED_BASE, "reentry": {"bars_since_close": 1, "last_exit": None},
            })
        prompt = self._prompt_of(client)
        assert "Exit details unavailable" in prompt
        assert "Bars since close: 1 H1" in prompt

    @pytest.mark.asyncio
    async def test_parses_reentry_ok_false(self):
        """El juicio del cerebro viaja al serve: reentry_ok=false se parsea
        tal cual (es lo que dispara veto_reason=reentry_veto)."""
        client = self._mock_qwen(
            '{"quality": 8.0, "reason": "churn reopen", "bias": "BULLISH",'
            ' "confidence_modifier": 1.1, "sl_adjusted": 0.20,'
            ' "tp_adjusted": 0.40, "regime": "TRENDING", "reentry_ok": false}'
        )
        with patch("httpx.AsyncClient", return_value=client):
            result = await query_qwen_unified(**{**_UNIFIED_BASE, "reentry": self._REENTRY})

        assert result["reentry_ok"] is False

    @pytest.mark.asyncio
    async def test_missing_reentry_ok_defaults_true_fail_open(self):
        """Respuesta sin el campo (modelo no siguió el esquema) → true
        fail-open + WARNING en el logger del ciclo: nunca silencioso,
        nunca un veto inventado."""
        log = MagicMock()
        client = self._mock_qwen(
            '{"quality": 8.0, "reason": "ok", "bias": "BULLISH",'
            ' "confidence_modifier": 1.1, "sl_adjusted": 0.20,'
            ' "tp_adjusted": 0.40, "regime": "TRENDING"}'
        )
        with patch("httpx.AsyncClient", return_value=client):
            result = await query_qwen_unified(
                **{**_UNIFIED_BASE, "reentry": self._REENTRY, "logger": log},
            )

        assert result["reentry_ok"] is True
        assert any(
            "reentry_ok ausente" in str(call.args[0])
            for call in log.warning.call_args_list
        )


class TestQueryQwenSltpValidator:
    """Tests for the SL/TP validator function."""

    @pytest.fixture
    def mock_success_response(self):
        """Mock successful validation response."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "choices": [{
                "message": {
                    "content": '{"sl_ok": true, "tp_ok": true, "sl_adjusted": 0.20, "tp_adjusted": 0.40, "reason": "valid"}'
                }
            }]
        }
        return mock_response

    @pytest.mark.asyncio
    async def test_successful_validation_returns_approved(self, mock_success_response):
        """Should return approved SL/TP when both are valid."""
        with patch("httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__.return_value.post = AsyncMock(
                return_value=mock_success_response
            )

            sl, tp, reason = await query_qwen_sltp_validator(
                symbol="EURUSD",
                decision="BUY",
                atr=0.00100,
                atr_pct=0.01,
                rsi=55.0,
                bb_pos=0.6,
                macd_hist=0.0005,
                range_pct=0.02,
                hour=10,
                sl_proposed=0.20,
                tp_proposed=0.40,
                cycle_id="EURUSD_1234567890",
                logger=MagicMock(),
            )

        assert sl == 0.20
        assert tp == 0.40
        assert reason == "valid"

    @pytest.mark.asyncio
    async def test_connection_error_returns_original_values(self):
        """Should return original SL/TP when Qwen is unreachable."""
        with patch("httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__.return_value.post = AsyncMock(
                side_effect=httpx.ConnectTimeout("timeout")
            )

            sl, tp, reason = await query_qwen_sltp_validator(
                symbol="EURUSD",
                decision="BUY",
                atr=0.00100,
                atr_pct=0.01,
                rsi=55.0,
                bb_pos=0.6,
                macd_hist=0.0005,
                range_pct=0.02,
                hour=10,
                sl_proposed=0.20,
                tp_proposed=0.40,
                cycle_id="EURUSD_1234567890",
                logger=MagicMock(),
            )

        assert sl == 0.20
        assert tp == 0.40
        assert "unavailable" in reason.lower()

    @pytest.mark.asyncio
    async def test_rejected_sl_tp_returns_adjusted(self):
        """Should return adjusted SL/TP when validator rejects."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "choices": [{
                "message": {
                    "content": '{"sl_ok": false, "tp_ok": false, "sl_adjusted": 0.25, "tp_adjusted": 0.50, "reason": "too tight"}'
                }
            }]
        }

        with patch("httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__.return_value.post = AsyncMock(
                return_value=mock_response
            )

            sl, tp, reason = await query_qwen_sltp_validator(
                symbol="EURUSD",
                decision="BUY",
                atr=0.00100,
                atr_pct=0.01,
                rsi=55.0,
                bb_pos=0.6,
                macd_hist=0.0005,
                range_pct=0.02,
                hour=10,
                sl_proposed=0.20,
                tp_proposed=0.40,
                cycle_id="EURUSD_1234567890",
                logger=MagicMock(),
            )

        assert sl == 0.25
        assert tp == 0.50
        assert "too tight" in reason

    @pytest.mark.asyncio
    async def test_http_500_returns_originals_without_crash(self):
        """P1-6: HTTP 500 del validador → propuestos originales, nunca raise."""
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.text = "boom"

        with patch("httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__.return_value.post = AsyncMock(
                return_value=mock_response
            )

            sl, tp, reason = await query_qwen_sltp_validator(
                symbol="EURUSD",
                decision="BUY",
                atr=0.00100,
                atr_pct=0.01,
                rsi=55.0,
                bb_pos=0.6,
                macd_hist=0.0005,
                range_pct=0.02,
                hour=10,
                sl_proposed=0.20,
                tp_proposed=0.40,
                cycle_id="EURUSD_1234567890",
                logger=MagicMock(),
            )

        assert sl == 0.20
        assert tp == 0.40
        assert reason == "Qwen degraded: HTTP 500"
        assert get_qwen_state().consecutive_failures == 1

    @pytest.mark.asyncio
    async def test_sltp_success_records_state(self, mock_success_response):
        with patch("httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__.return_value.post = AsyncMock(
                return_value=mock_success_response
            )
            await query_qwen_sltp_validator(
                symbol="EURUSD",
                decision="BUY",
                atr=0.00100,
                atr_pct=0.01,
                rsi=55.0,
                bb_pos=0.6,
                macd_hist=0.0005,
                range_pct=0.02,
                hour=10,
                sl_proposed=0.20,
                tp_proposed=0.40,
                cycle_id="EURUSD_1234567890",
                logger=MagicMock(),
            )

        state = get_qwen_state()
        assert state.calls_ok == 1
        assert state.consecutive_failures == 0
