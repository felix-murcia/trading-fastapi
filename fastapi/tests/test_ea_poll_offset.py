"""
G2-2 (orden Félix «Termina los C», 2026-10-17) — escalonado de sondeo.

Cada par es un mundo completo con su propio EA (principio rector 1 de G2).
Sin escalonamiento, dos EA sobre sus respectivos pares dispararían sus
ciclos en el MISMO tick de apertura M5 y dos WebRequests colisionarían en
el mismo instante (cola, latencia p99, ruido de telemetría).

El input `PollOffsetSeconds` (default 0 = byte-idéntico al gate clásico)
retrasa el sondeo respecto a la apertura de la barra M5 por INSTANCIA:
EURUSD 0 → :00/:05; GBPUSD 120 → :02/:07 ⇒ ≥2 s de separación entre
ciclos de los dos mundos (aceptación G2-2).

Estos tests son de ESTRUCTURA de fuente MQL5 (patrón del repo:
test_ea_close_outbox / test_orders_prepare leen el .mq5); la semántica de
tiempo no se puede ejecutar desde pytest, pero el ORDEN de las líneas sí
fija el comportamiento: la barra no se consume (`lastCheckedBar =`) hasta
después del gate de desfase.
"""
import re
from pathlib import Path

_EA = Path(__file__).resolve().parents[2] / "mql5" / "AI_Quant_Terminal.mq5"


def _ea_source() -> str:
    return _EA.read_text(encoding="utf-8", errors="replace")


class TestPollOffsetInput:
    def test_input_exists_with_default_zero(self):
        """El input existe y su default es 0 (comportamiento clásico intacto)."""
        src = _ea_source()
        assert re.search(
            r"input\s+int\s+PollOffsetSeconds\s*=\s*0\s*;", src
        ), "Falta `input int PollOffsetSeconds = 0;` (default 0 = byte-idéntico)"

    def test_input_documented_per_instance(self):
        """El comentario del input fija el reparto por instancia (:00/:05 vs :02/:07)."""
        src = _ea_source()
        m = re.search(r"input\s+int\s+PollOffsetSeconds", src)
        assert m is not None, "Falta `input int PollOffsetSeconds`"
        window = src[max(0, m.start() - 700) : m.start() + 300]
        assert "EURUSD" in window and "GBPUSD" in window, (
            "el reparto de offsets por par no queda documentado en la fuente"
        )


class TestPollOffsetGate:
    def test_gate_exists_before_bar_consumed(self):
        """El gate de desfase ANTES de consumir la barra (lastCheckedBar =)."""
        src = _ea_source()
        gate = src.find("if(TimeCurrent() - currentBar < PollOffsetSeconds) return;")
        consume = src.find("lastCheckedBar = currentBar;")
        assert gate != -1, "Falta el gate de desfase tras apertura M5"
        assert consume != -1, "Falta el consumo de barra (lastCheckedBar = currentBar)"
        assert gate < consume, (
            "el gate de desfase está DESPUÉS de consumir la barra: el offset "
            "no tendría efecto (el poll saltaría en el 1er tick igualmente)"
        )

    def test_bar_not_consumed_on_early_return(self):
        """El `return` del gate no puede ir acompañado de lastCheckedBar en la misma rama."""
        src = _ea_source()
        gate_line_idx = src.find(
            "if(TimeCurrent() - currentBar < PollOffsetSeconds) return;"
        )
        # La línea del gate devuelve SIN consumir: la siguiente sentencia
        # significativa tras la línea del gate es el consumo (sin return previo).
        after = src[gate_line_idx:].splitlines()
        assert after[0].rstrip().endswith("return;"), (
            "el gate no devuelve: la barra se consumiría aunque falte el offset"
        )
        assert "lastCheckedBar = currentBar;" in after[1], (
            "la sentencia tras el gate no es el consumo de barra (orden roto)"
        )


class TestVersionBump:
    def test_version_13_9_in_property_and_define(self):
        """Bump obligatorio en cada cambio MQL5 (v13.7 → v13.9, rename
        genérico AI_Quant_Terminal.mq5 — orden Félix 2026-10-18)."""
        src = _ea_source()
        assert re.search(r'#property\s+version\s+"13\.9"', src)
        assert re.search(r'#define\s+EA_VERSION\s+"13\.9"', src)
