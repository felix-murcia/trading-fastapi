"""
Tests for services/order_validator.py — extracted from order_manager.prepare.

Covers:
- Price range validation
- Geometry validation (BUY/SELL)
- SL pips validation
- Volume validation
- Duplicate detection
"""
import pytest
from unittest.mock import patch, MagicMock, AsyncMock

from services.order_validator import (
    validate_price_range,
    validate_geometry,
    validate_sl_pips,
    validate_volume,
    check_duplicate,
    OrderValidationError,
)
from models.orders import OrderPrepareRequest


class TestValidatePriceRange:
    """Tests for price range validation."""

    def test_valid_price_passes(self):
        """Should return None for valid price."""
        result = validate_price_range("EURUSD", 1.10)
        assert result is None

    def test_price_below_range_fails(self):
        """Should raise error for price below range."""
        with pytest.raises(OrderValidationError) as exc_info:
            validate_price_range("EURUSD", 1.00)
        assert "entry_out_of_range" in str(exc_info.value)

    def test_price_above_range_fails(self):
        """Should raise error for price above range."""
        with pytest.raises(OrderValidationError) as exc_info:
            validate_price_range("EURUSD", 1.30)
        assert "entry_out_of_range" in str(exc_info.value)

    def test_unknown_symbol_uses_default_range(self):
        """Should use default range for unknown symbols."""
        result = validate_price_range("UNKNOWN", 100.0)
        assert result is None


class TestValidateGeometry:
    """Tests for order geometry validation."""

    def test_valid_buy_geometry(self):
        """Should pass for valid BUY: sl < entry < tp."""
        req = OrderPrepareRequest(
            cycle_id="test", symbol="EURUSD", type="BUY",
            entry=1.10, sl=1.09, tp=1.12, volume=0.1
        )
        result = validate_geometry(req)
        assert result is None

    def test_valid_buy_without_tp(self):
        """Should pass for valid BUY without TP (tp=0)."""
        req = OrderPrepareRequest(
            cycle_id="test", symbol="EURUSD", type="BUY",
            entry=1.10, sl=1.09, tp=0, volume=0.1
        )
        result = validate_geometry(req)
        assert result is None

    def test_invalid_buy_sl_above_entry(self):
        """Should fail when BUY sl >= entry."""
        req = OrderPrepareRequest(
            cycle_id="test", symbol="EURUSD", type="BUY",
            entry=1.10, sl=1.11, tp=1.12, volume=0.1
        )
        with pytest.raises(OrderValidationError) as exc_info:
            validate_geometry(req)
        assert "invalid_geometry_buy" in str(exc_info.value)

    def test_invalid_buy_tp_below_entry(self):
        """Should fail when BUY tp <= entry."""
        req = OrderPrepareRequest(
            cycle_id="test", symbol="EURUSD", type="BUY",
            entry=1.10, sl=1.09, tp=1.08, volume=0.1
        )
        with pytest.raises(OrderValidationError) as exc_info:
            validate_geometry(req)
        assert "invalid_geometry_buy" in str(exc_info.value)

    def test_valid_sell_geometry(self):
        """Should pass for valid SELL: tp < entry < sl."""
        req = OrderPrepareRequest(
            cycle_id="test", symbol="EURUSD", type="SELL",
            entry=1.10, sl=1.11, tp=1.08, volume=0.1
        )
        result = validate_geometry(req)
        assert result is None

    def test_invalid_sell_sl_below_entry(self):
        """Should fail when SELL sl <= entry."""
        req = OrderPrepareRequest(
            cycle_id="test", symbol="EURUSD", type="SELL",
            entry=1.10, sl=1.09, tp=1.08, volume=0.1
        )
        with pytest.raises(OrderValidationError) as exc_info:
            validate_geometry(req)
        assert "invalid_geometry_sell" in str(exc_info.value)

    def test_unknown_type_fails(self):
        """Should fail for unknown order type."""
        req = OrderPrepareRequest(
            cycle_id="test", symbol="EURUSD", type="UNKNOWN",
            entry=1.10, sl=1.09, tp=1.12, volume=0.1
        )
        with pytest.raises(OrderValidationError) as exc_info:
            validate_geometry(req)
        assert "unknown_order_type" in str(exc_info.value)


class TestValidateSlPips:
    """Tests for SL pips validation."""

    def test_valid_sl_passes(self):
        """Should pass for valid SL distance."""
        result = validate_sl_pips("EURUSD", 1.10, 1.095)
        assert result is None

    def test_sl_too_tight_fails(self):
        """Should fail when SL is too tight."""
        with pytest.raises(OrderValidationError) as exc_info:
            validate_sl_pips("EURUSD", 1.10, 1.0999)
        assert "sl_too_tight" in str(exc_info.value)

    def test_sl_too_wide_fails(self):
        """Should fail when SL is too wide."""
        with pytest.raises(OrderValidationError) as exc_info:
            validate_sl_pips("EURUSD", 1.10, 1.05)
        assert "sl_too_wide" in str(exc_info.value)

    def test_jpy_pair_uses_correct_pip_size(self):
        """Should use 0.01 pip size for JPY pairs."""
        result = validate_sl_pips("USDJPY", 150.0, 149.5)
        assert result is None


class TestValidateVolume:
    """Tests for volume validation."""

    def test_valid_volume_passes(self):
        """Should pass for valid volume."""
        with patch("services.order_validator.settings") as mock_settings:
            mock_settings.min_volume = 0.01
            mock_settings.max_volume = 0.50
            result = validate_volume(0.1)
        assert result is None

    def test_volume_too_low_fails(self):
        """Should fail when volume is too low."""
        with patch("services.order_validator.settings") as mock_settings:
            mock_settings.min_volume = 0.01
            mock_settings.max_volume = 0.50
            with pytest.raises(OrderValidationError) as exc_info:
                validate_volume(0.005)
        assert "volume_out_of_range" in str(exc_info.value)

    def test_volume_too_high_fails(self):
        """Should fail when volume is too high."""
        with patch("services.order_validator.settings") as mock_settings:
            mock_settings.min_volume = 0.01
            mock_settings.max_volume = 0.50
            with pytest.raises(OrderValidationError) as exc_info:
                validate_volume(0.60)
        assert "volume_out_of_range" in str(exc_info.value)


class TestCheckDuplicate:
    """Tests for duplicate order detection."""

    @pytest.mark.asyncio
    async def test_no_duplicate_passes(self):
        """Should return None when no duplicate exists."""
        mock_pool = MagicMock()
        mock_pool.fetchrow = AsyncMock(return_value=None)

        result = await check_duplicate(mock_pool, "EURUSD", 1.10)
        assert result is None

    @pytest.mark.asyncio
    async def test_duplicate_found_raises(self):
        """Should raise error when duplicate exists."""
        mock_pool = MagicMock()
        mock_pool.fetchrow = AsyncMock(return_value={"id": 123})

        with pytest.raises(OrderValidationError) as exc_info:
            await check_duplicate(mock_pool, "EURUSD", 1.10)
        assert "duplicate_order" in str(exc_info.value)
