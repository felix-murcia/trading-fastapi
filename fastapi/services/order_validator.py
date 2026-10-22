"""
Order validation logic — extracted from order_manager.prepare.

Follows Single Responsibility Principle: each validator does one thing.
"""
import logging

from config import settings
from models.orders import OrderPrepareRequest
# P2-2: pip por símbolo centralizado — fuente única en position_sizing
# (antes había una copia divergente; el resto de validadores importan aquí).
from services.position_sizing import PIP_SIZE

logger = logging.getLogger(__name__)

VALID_RANGES = {
    "EURUSD": (1.05, 1.20),
    "GBPUSD": (1.20, 1.45),
    "AUDUSD": (0.55, 0.90),
    "NZDUSD": (0.50, 0.80),
    "USDJPY": (130.0, 175.0),
    "USDCHF": (0.75, 1.05),
    "USDCAD": (1.20, 1.55),
    "USDCNH": (6.50, 8.50),
    "USDSEK": (8.00, 12.00),
    "XAUUSD": (2000.0, 8000.0),
    "XAUEUR": (2000.0, 8000.0),
}

SL_LIMITS: dict[str, tuple[float, float]] = {
    "USDJPY": (5.0, 300.0),
    "XAUUSD": (10.0, 2000.0),
    "XAUEUR": (10.0, 2000.0),
}
_SL_DEFAULT = (3.0, 300.0)


class OrderValidationError(Exception):
    """Raised when order validation fails."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def _pip(symbol: str) -> float:
    """Get pip size for symbol."""
    return PIP_SIZE.get(symbol, 0.0001)


def _pips(symbol: str, a: float, b: float) -> float:
    """Calculate pips between two prices."""
    return abs(a - b) / _pip(symbol)


def validate_price_range(symbol: str, entry: float) -> None:
    """Validate entry price is within valid range for symbol.

    Raises:
        OrderValidationError: If price is out of range.
    """
    lo, hi = VALID_RANGES.get(symbol, (0, 99999))
    if not (lo <= entry <= hi):
        raise OrderValidationError(f"entry_out_of_range:{entry}")


def validate_geometry(req: OrderPrepareRequest) -> None:
    """Validate order geometry (SL/TP relative to entry).

    Raises:
        OrderValidationError: If geometry is invalid.
    """
    if req.type == "BUY":
        if not (req.sl < req.entry):
            raise OrderValidationError("invalid_geometry_buy")
        if req.tp != 0 and req.tp <= req.entry:
            raise OrderValidationError("invalid_geometry_buy")
    elif req.type == "SELL":
        if not (req.entry < req.sl):
            raise OrderValidationError("invalid_geometry_sell")
        if req.tp != 0 and req.tp >= req.entry:
            raise OrderValidationError("invalid_geometry_sell")
    else:
        raise OrderValidationError(f"unknown_order_type:{req.type}")


def validate_sl_pips(symbol: str, entry: float, sl: float) -> None:
    """Validate SL distance in pips.

    Raises:
        OrderValidationError: If SL is too tight or too wide.
    """
    sl_pips = _pips(symbol, entry, sl)
    min_pips, max_pips = SL_LIMITS.get(symbol, _SL_DEFAULT)
    if sl_pips < min_pips:
        raise OrderValidationError(f"sl_too_tight:{round(sl_pips, 1)}pips")
    if sl_pips > max_pips:
        raise OrderValidationError(f"sl_too_wide:{round(sl_pips, 1)}pips")


def validate_volume(volume: float) -> None:
    """Validate volume is within allowed range.

    Raises:
        OrderValidationError: If volume is out of range.
    """
    if not (settings.min_volume <= volume <= settings.max_volume):
        raise OrderValidationError(f"volume_out_of_range:{volume}")


async def check_duplicate(pool, symbol: str, entry: float) -> None:
    """Check for duplicate order (same symbol + entry within 1 pip).

    Raises:
        OrderValidationError: If duplicate exists.
    """
    tolerance = _pip(symbol)
    dup = await pool.fetchrow(
        "SELECT id FROM orders WHERE symbol=$1 AND status IN ('pending','placed') "
        "AND created_at >= NOW() - INTERVAL '" + str(settings.order_max_age_hours) + " hours' "
        "AND ABS(entry - $2) <= $3",
        symbol, entry, tolerance,
    )
    if dup:
        raise OrderValidationError(f"duplicate_order:{symbol}@{entry}")
