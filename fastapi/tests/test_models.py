"""
Tests del contrato TradeFilledRequest (P1-4: modelo REAL, sin stubs).

El stub anterior se activaba cuando `routers.ai` no importaba, y sus tests de
dirección/exit_reason pasaban por campos faltantes (sl_hit/tp_hit), no por la
regla de negocio — cobertura falsa sobre dinero real.

Ahora se importa el modelo real (la suite ya depende de torch/routers vía E2E)
y las aserciones exigen el error CORRECTO (match del campo). Los validadores
deben igualar los CHECK de trade_outcomes en schema.sql: payload inválido →
422 en el borde, nunca un CheckViolation 500 desde asyncpg.
"""
import pytest

from routers.ai import TradeFilledRequest


def _req(**overrides):
    """Payload válido por defecto; cada test solo declara su variación."""
    base = {
        "symbol": "EURUSD",
        "entry_time": "1725450000",
        "exit_time": "1725453600",
        "pnl": 12.5,
        "pnl_pct": 0.0125,
        "direction": "LONG",
        "sl_hit": False,
        "tp_hit": True,
        "exit_reason": "tp",
    }
    base.update(overrides)
    return TradeFilledRequest(**base)


class TestTradeFilledRequestValidation:
    """Validate TradeFilledRequest model enforces all constraints."""

    def test_valid_short_sl_hit(self):
        req = _req(direction="SHORT", sl_hit=True, tp_hit=False, exit_reason="sl",
                   pnl=-15.50, pnl_pct=-0.0155)
        assert req.symbol == "EURUSD"
        assert req.direction == "SHORT"
        assert req.sl_hit is True
        assert req.exit_reason == "sl"

    def test_valid_long_tp_hit_with_iso_timestamps(self):
        req = _req(
            symbol="XAUUSD",
            entry_time="2024-09-04T10:00:00Z",
            exit_time="2024-09-04T11:00:00Z",
            pnl=25.00,
            pnl_pct=0.025,
        )
        assert req.symbol == "XAUUSD"
        assert req.direction == "LONG"
        assert req.tp_hit is True

    def test_invalid_direction_rejected(self):
        # Todos los demás campos válidos → el error debe ser por direction
        with pytest.raises(ValueError, match="direction"):
            _req(direction="FLAT")

    def test_empty_direction_rejected(self):
        with pytest.raises(ValueError, match="direction"):
            _req(direction="")

    def test_invalid_exit_reason_rejected(self):
        with pytest.raises(ValueError, match="exit_reason"):
            _req(exit_reason="stop_loss")

    @pytest.mark.parametrize("incoming,canonical", [
        ("long", "LONG"),
        ("LONG", "LONG"),
        ("buy", "LONG"),
        ("BUY", "LONG"),
        ("short", "SHORT"),
        ("sell", "SHORT"),
    ])
    def test_direction_normalizes_to_db_contract(self, incoming, canonical):
        """El CHECK exige LONG/SHORT: case-insensitive + buy/sell canónicos."""
        assert _req(direction=incoming).direction == canonical

    @pytest.mark.parametrize("incoming,canonical", [
        ("SL", "sl"),
        (" TP ", "tp"),
        ("Manual", "manual"),
        ("signal_end", "signal_end"),
        ("ea", "ea"),
    ])
    def test_exit_reason_normalizes_to_lowercase(self, incoming, canonical):
        """El CHECK de exit_reason es lowercase exacto: normalizar en el borde."""
        assert _req(exit_reason=incoming).exit_reason == canonical

    def test_negative_pnl_accepted(self):
        req = _req(pnl=-100.0, pnl_pct=-0.10, direction="SHORT",
                   sl_hit=True, tp_hit=False, exit_reason="sl")
        assert req.pnl == -100.0

    def test_zero_pnl_accepted(self):
        req = _req(pnl=0.0, pnl_pct=0.0, sl_hit=False, tp_hit=False,
                   exit_reason="manual")
        assert req.pnl == 0.0

    def test_string_pnl_stays_string_for_router_coercion(self):
        """pnl: float | str — el router hace float() antes de persistir."""
        req = _req(pnl="12.5", pnl_pct="0.0125")
        assert req.pnl == "12.5"

    def test_schema_domains_match_db_check(self):
        """Los dominios del modelo == CHECK de trade_outcomes (schema.sql)."""
        import os
        from routers.ai import VALID_EXIT_REASONS

        schema_path = os.path.join(os.path.dirname(__file__), "..", "db", "schema.sql")
        with open(schema_path, "r") as f:
            schema = f.read()

        assert "exit_reason  TEXT NOT NULL CHECK (exit_reason IN ('sl', 'tp', 'manual', 'news', 'signal_end', 'ea'))" in schema
        assert set(VALID_EXIT_REASONS) == {"sl", "tp", "manual", "news", "signal_end", "ea"}
