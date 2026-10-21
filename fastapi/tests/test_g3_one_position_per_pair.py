"""
G3 (2026-10-21) · UNA OPERACIÓN A LA VEZ POR PAR.

Orden de Félix: *«el sistema ha abierto una segunda operación en USDCHF —
eso contradice la política de una operación a la vez por par de divisas»*.

Causa RAÍZ (medida en el incidente del 21-10 09:45):
  · el backend NO aplicaba la política: el tope de posiciones por par era
    **5** (`RiskState.max_positions`) ⇒ con 1 posición abierta, el guardián
    autorizaba la segunda;
  · y el veto de posición del serve (que viaja en el payload del EA) no
    disparó porque el EA contaba con `PositionSelect(Symbol())` — que en
    modo HEDGING elige UNA sola posición (la de menor ticket) — y luego
    comparaba el magic ⇒ si esa no era la suya, `currentPos` quedaba en 0
    con una posición abierta (el audit lo enseñó: `"position": 0` con la
    posición viva).

Este fichero fija:
  1. el tope por par es 1 (config) y el guardián lo aplica;
  2. con 1 posición abierta del par, el guardián RECHAZA la segunda;
  3. el EA cuenta las posiciones por magic (bucle) y avisa si hay más de una;
  4. el EA sube de versión (regla del proyecto: todo cambio en el EA bumpea).
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

MQL5 = Path(__file__).resolve().parents[2] / "mql5" / "AI_Quant_Terminal.mq5"


@pytest.fixture
def fresh(monkeypatch):
    """Guardián limpio (aislamiento: el registro es un singleton)."""
    from services import risk_guardian as rg
    monkeypatch.setattr(rg, "_GUARDIANS", {})
    monkeypatch.setattr(rg, "_ACCOUNT", rg.AccountRiskState())
    return rg


class TestPerPairCap:
    def test_cap_is_one_position_per_pair(self):
        from config import settings

        assert settings.max_positions_per_pair == 1, (
            "la política es UNA operación a la vez por par de divisas"
        )

    def test_guardian_uses_the_configured_cap(self, fresh):
        g = fresh.get_risk_guardian("USDCHF")
        assert g.state.max_positions == 1

    def test_second_position_is_blocked(self, fresh):
        """Con 1 posición abierta del par, la segunda se rechaza."""
        check_risk = fresh.check_risk
        ok, reason = check_risk(
            500.0, 1,                       # 1 posición abierta de este par
            total_open_positions=1,
            total_risk_eur=0.0,
            same_dir_open=0,
            symbol="USDCHF",
        )
        assert ok is False
        assert "MAX_POSITIONS 1" in reason

    def test_first_position_is_allowed(self, fresh):
        check_risk = fresh.check_risk
        ok, reason = check_risk(
            500.0, 0,
            total_open_positions=0,
            total_risk_eur=0.0,
            same_dir_open=0,
            symbol="USDCHF",
        )
        assert ok is True, reason


class TestEaPositionCounting:
    """El EA debe CONTAR sus posiciones, no elegir una con PositionSelect."""

    def test_ea_counts_positions_by_magic(self):
        src = MQL5.read_text(encoding="utf-8")
        assert "PositionsTotal()" in src, (
            "el EA debe recorrer las posiciones y filtrar por magic"
        )
        assert "PositionGetInteger(POSITION_MAGIC) != MagicNumber" in src
        # el patrón frágil (una sola posición + filtro) no debe seguir
        assert "if(PositionSelect(Symbol()))\n     {\n      if(PositionGetInteger(POSITION_MAGIC) == MagicNumber)" not in src, (
            "el recuento por PositionSelect+magick no puede seguir: en hedging "
            "elige UNA posición y el veto de posición deja de disparar"
        )

    def test_ea_warns_when_more_than_one_position(self):
        src = MQL5.read_text(encoding="utf-8")
        assert "myPositions > 1" in src, (
            "el EA debe avisar si detecta más de una posición suya"
        )

    def test_ea_version_was_bumped(self):
        src = MQL5.read_text(encoding="utf-8")
        assert '#property version   "13.9"' in src
        assert '#define EA_VERSION "13.9"' in src
