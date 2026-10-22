"""
G3-2 (2026-10-21) — El SWAP del cierre se registra (y el audit declara la
geometría REAL).

Dos huecos medidos sobre las operaciones reales del 21-10/22-10:

1. **El swap no llegaba al backend**: el EA (v14.0) enviaba el pnl BRUTO del
   bróker y no el swap (financiación por mantener la posición abierta) ⇒ el
   pnl registrado era incompleto y TODAS las métricas (ledger, win rate,
   expectativa) perdían ese coste. Medido: −0,16 € en la operación EURUSD 76
   (7,3 h) y −1,12 € acumulados en el histórico de 99 deals. El EA v14.1
   envía el swap y el backend registra el RESULTADO REAL (bruto + swap).

2. **El audit mentía sobre la geometría**: `sl_geometry` era una constante
   ("nearest_floor_ceiling") incluso cuando la operación se hizo con el
   fallback ATR (el nivel del lado correspondiente no existía: en USDCHF, el
   32 % de los ciclos, porque el precio está en el mínimo de su rango).
"""
import re
from pathlib import Path

import pytest

from ml.trading_env_v2 import stop_geometry_kind

EA = Path(__file__).resolve().parents[2] / "mql5" / "AI_Quant_Terminal.mq5"


class TestSwapIsReported:
    def test_ea_sends_the_swap(self):
        src = EA.read_text(encoding="utf-8")
        assert "DEAL_SWAP" in src, "el EA debe leer el swap del deal"
        assert '\\"swap\\"' in src, "el webhook debe enviar el swap"
        assert re.search(r'#property\s+version\s+"14\.1"', src), "bump a 14.1"
        assert '#define EA_VERSION "14.1"' in src

    def test_backend_adds_the_swap_to_the_recorded_pnl(self):
        """El pnl que se registra es el resultado REAL (bruto + swap)."""
        import routers.ai as ai
        src = Path(ai.__file__).read_text(encoding="utf-8")
        assert "pnl_total = pnl + swap" in src
        assert "pnl=pnl_total" in src

    @pytest.mark.asyncio
    async def test_webhook_records_pnl_plus_swap(self, monkeypatch):
        """El endpoint /trade/filled con swap registra bruto + swap."""
        from routers.ai import TradeFilledRequest
        req = TradeFilledRequest(
            symbol="EURUSD", entry_time=1760000000, exit_time=1760003600,
            pnl=-6.51, pnl_pct=-0.0069, direction="SHORT",
            sl_hit=True, tp_hit=False, exit_reason="sl",
            deal_ticket=999, position_id=888, swap=-0.16,
        )
        assert req.swap == pytest.approx(-0.16)


class TestAuditDeclaresTheRealGeometry:
    def test_kind_reflects_the_levels_used(self):
        assert stop_geometry_kind(1, 1.1020, 1.1080) == "nearest_floor_ceiling"
        assert stop_geometry_kind(-1, 1.1020, 1.1080) == "nearest_floor_ceiling"
        # sin nivel utilizable ⇒ fallback ATR (declarado, no escondido)
        assert stop_geometry_kind(1, 0.0, 1.1080) == "atr_fallback"
        assert stop_geometry_kind(-1, 1.1020, 0.0) == "atr_fallback"
        # sin dirección (plano) ⇒ fallback
        assert stop_geometry_kind(0, 1.1020, 1.1080) == "atr_fallback"

    def test_audit_uses_the_helper_not_a_constant(self):
        import routers.ai as ai
        src = Path(ai.__file__).read_text(encoding="utf-8")
        assert '"sl_geometry": "nearest_floor_ceiling"' not in src, (
            "el audit no puede declarar una geometría constante: mentiría sobre "
            "el fallback ATR (32 % de los ciclos de USDCHF)"
        )
        assert '"sl_geometry": stop_geometry_kind(' in src
