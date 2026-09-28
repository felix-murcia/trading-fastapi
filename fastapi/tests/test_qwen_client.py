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

from services.qwen_client import query_qwen_unified, query_qwen_sltp_validator


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

    @pytest.mark.asyncio
    async def test_connection_error_returns_safe_defaults(self):
        """Should return safe defaults when Qwen is unreachable."""
        with patch("httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__.return_value.post = AsyncMock(
                side_effect=httpx.ConnectTimeout("timeout")
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

        assert result["quality"] == 0.0
        assert result["bias"] == "NEUTRAL"
        assert result["confidence_modifier"] == 1.0
        assert result["sl_adjusted"] == 0.20
        assert result["tp_adjusted"] == 0.40

    @pytest.mark.asyncio
    async def test_invalid_json_raises_runtime_error(self):
        """Should raise RuntimeError when response contains no JSON."""
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

            with pytest.raises(RuntimeError, match="sin JSON"):
                await query_qwen_unified(
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
