"""
G3 (2026-10-21) · EL WATCHDOG DE SILENCIO VIGILA CADA MUNDO.

BUG QUE ESTE FICHERO FIJA (G2-6, commit 62ea660, 2026-10-20): `_enabled_pairs`
se declaró `async def` y se llamó SIN `await` ⇒ el watchdog llevaba 12 h CAÍDO
con «'coroutine' object is not iterable» ⇒ no vigilaba NINGÚN mundo (ni
EURUSD ni GBPUSD ni USDCHF): si el EA de un mundo moría, nadie se enteraba.

Contrato:
  1. el bucle hace `await _enabled_pairs()` (sin await, no vigila nada);
  2. vigila CADA mundo habilitado por separado (con dos o tres mundos, el
     ciclo vivo de uno no puede enmascarar la muerte del otro);
  3. si el bucle falla, avisa (ERROR) — pero no muere en silencio.
"""
import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services import predict_silence


class _Ctx:
    def __init__(self, symbol: str):
        self.config = type("C", (), {"symbol": symbol})()


class TestSilenceWatchdog:
    def test_source_awaits_the_enabled_pairs(self):
        src = (Path(__file__).resolve().parents[1] / "services" / "predict_silence.py").read_text(
            encoding="utf-8")
        assert "await _enabled_pairs()" in src, (
            "el watchdog DEBE awaitar la lista de mundos habilitados "
            "(sin await no vigila ninguno)"
        )

    @pytest.mark.asyncio
    async def test_watches_every_enabled_world(self):
        """Con dos mundos habilitados, check_silence se llama para CADA uno."""
        checked: list = []

        async def _check(symbol=None, now=None):
            checked.append(symbol)
            if len(checked) >= 2:
                raise asyncio.CancelledError()   # corta el bucle tras la vuelta
            return 10

        with patch.object(predict_silence, "FIRST_CHECK_DELAY_S", 0), \
             patch.object(predict_silence, "CHECK_INTERVAL_S", 10_000), \
             patch.object(predict_silence, "_enabled_pairs", AsyncMock(
                 return_value=[_Ctx("EURUSD"), _Ctx("USDCHF")])), \
             patch.object(predict_silence, "check_silence", _check):
            with pytest.raises(asyncio.CancelledError):
                await predict_silence._predict_silence_loop()

        assert sorted(checked) == ["EURUSD", "USDCHF"], checked

    @pytest.mark.asyncio
    async def test_enabled_pairs_returns_a_list_not_a_coroutine(self):
        """El contrato de la función: devuelve la LISTA de mundos."""
        with patch("core.container.build_registry_from_settings") as reg:
            reg.return_value.enabled.return_value = [_Ctx("EURUSD")]
            out = await predict_silence._enabled_pairs()
        assert [c.config.symbol for c in out] == ["EURUSD"]
