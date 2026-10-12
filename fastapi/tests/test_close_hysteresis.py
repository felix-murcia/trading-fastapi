"""Tests TDD — Histeresis CLOSE K=2 ciclos (ítem cierres tempranos).

El contrafactual con datos reales (2026-10-12) mostró que los cierres `ea`
(zona muerta) dejan +257€ sobre la mesa (83% tocó TP en 48h). La histeresis
K=2 ciclos da tiempo al modelo para salir de la zona muerta antes de cerrar:
1er ciclo en zona muerta → HOLD, 2º ciclo consecutivo → CLOSE.
"""
import logging

import routers.ai


def _reset_state():
    """Resetea el contador de zona muerta entre tests."""
    routers.ai._DEAD_ZONE_COUNTER.clear()


class TestHisteresisCloseK2:
    """Comportamiento de la histeresis K=2: 1er ciclo zona muerta → HOLD,
    2º ciclo consecutivo → CLOSE. Interrupción resetea el contador."""

    def setup_method(self):
        _reset_state()

    def teardown_method(self):
        _reset_state()

    def test_primer_ciclo_zona_muerta_hold(self):
        # 1er ciclo en zona muerta (K=2): NO cierra
        assert routers.ai._dead_zone_close("EURUSD", decision="CLOSE") is False

    def test_segundo_ciclo_consecutivo_cierra(self):
        # 1er ciclo: HOLD, 2º ciclo consecutivo: CLOSE
        routers.ai._dead_zone_close("EURUSD", decision="CLOSE")
        assert routers.ai._dead_zone_close("EURUSD", decision="CLOSE") is True

    def test_fuera_zona_muerta_resetea(self):
        # 1 ciclo zona muerta → fuera → 1 ciclo zona muerta = HOLD (reset)
        routers.ai._dead_zone_close("EURUSD", decision="CLOSE")  # 1º HOLD
        routers.ai._dead_zone_close("EURUSD", decision="BUY")    # fuera → reset
        assert routers.ai._dead_zone_close("EURUSD", decision="CLOSE") is False  # 1º HOLD

    def test_ciclo_fuera_tras_dos_consecutivos_cierra(self):
        # 2 consecutivos → CLOSE (no sigue contando)
        routers.ai._dead_zone_close("EURUSD", decision="CLOSE")  # 1º HOLD
        assert routers.ai._dead_zone_close("EURUSD", decision="CLOSE") is True  # 2º CLOSE

    def test_simbolos_independientes(self):
        # Contadores por símbolo son independientes
        routers.ai._dead_zone_close("EURUSD", decision="CLOSE")  # 1º HOLD
        # GBPUSD lleva 1 ciclo → HOLD
        assert routers.ai._dead_zone_close("GBPUSD", decision="CLOSE") is False
        # EURUSD lleva 2 → CLOSE
        assert routers.ai._dead_zone_close("EURUSD", decision="CLOSE") is True

    def test_decision_flat_con_posicion_resetea(self):
        # FLAT con posición = no es zona muerta → resetea
        routers.ai._dead_zone_close("EURUSD", decision="CLOSE")   # 1º HOLD
        routers.ai._dead_zone_close("EURUSD", decision="FLAT")    # reset
        assert routers.ai._dead_zone_close("EURUSD", decision="CLOSE") is False
