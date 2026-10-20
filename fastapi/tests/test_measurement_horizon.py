"""
L1 · LA DEFINICIÓN CANÓNICA DE SKILL (services/measurement) — red de
regresión del HORIZONTE (T-31.9, 2026-10-20).

Contexto: la definición canónica se usa para TODO (autoridad del veto,
calibración, drift, ledger, benchmark, mapa de edge). El 20-10 se detectó
que el terminal se calculaba como "la PRIMERA vela con time >= t0 + 24 h",
lo que SALTA los huecos de la serie (pausa diaria del bróker de 1 h y fin
de semana) ⇒ el horizonte medido era de 26 a 72 h en el 19-26 % de los
casos, no 24 h. Medido sobre el momentum de 24 h de EURUSD (7 d):
37,2 % (regla antigua) vs 52,7 % (regla exacta) ⇒ 15 pp de diferencia
por el hueco, no por el mercado.

Ahora el terminal es "la ÚLTIMA vela con time <= t0 + H" ⇒ horizonte real
(23-24 h) y determinista (los huecos del mercado son permanentes: no se
rellenan nunca).

Este fichero fija ese contrato para que nadie lo rompa sin querer.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.measurement import canonical_direction


def _series(horas: list[float], closes: list[float]) -> tuple[list, list]:
    """Serie con huecos: `horas` son horas RELATIVAS a t0 (0 = t0).
    Los sellos de la serie van en SEGUNDOS (unix), como en el sistema."""
    return [h * 3600.0 for h in horas], list(closes)


class TestCanonicalHorizon:
    def test_horizon_is_never_longer_than_the_window(self):
        """Con un hueco EN el horizonte, el terminal no puede estar más allá.

        Serie: hay vela a las 22 h y a las 26 h, NINGUNA a las 24 h.
        · regla antigua ("primera ≥ 24 h") → terminal = 26 h → close 90 → −1
        · regla correcta ("última ≤ 24 h") → terminal = 22 h → close 120 → +1
        El test discrimina: con la regla antigua FALLA.
        """
        horas = [0, 2, 4, 6, 8, 10, 12, 14, 16, 18, 20, 22, 26, 28]
        closes = [100, 101, 102, 103, 104, 105, 106, 107, 108, 109, 110, 120, 90, 85]
        bt, bc = _series(horas, closes)
        now = bt[-1] + 10 * 3600
        d = canonical_direction(bt, bc, bt[0], now)
        assert d == 1, (
            "el terminal se estiró más allá de las 24 h (hueco saltado)"
        )

    def test_terminal_is_the_last_candle_not_after_the_horizon(self):
        """El terminal es la última vela ANTES o IGUAL a t0+H (no la de H+1)."""
        horas = [0, 1, 2, 3, 24, 25]
        closes = [100, 100, 100, 100, 110, 90]
        bt, bc = _series(horas, closes)
        now = bt[-1] + 10 * 3600
        d = canonical_direction(bt, bc, bt[0], now)
        assert d == 1, "el terminal debe ser la vela de las 24 h (close 110), no la de las 25"

    def test_weekend_gap_does_not_stretch_the_horizon(self):
        """Viernes → el terminal es el viernes+24 h, NO el lunes."""
        # base = viernes 10:00; huecos: 12, 13, ..., 34 (fin de semana)
        horas = [0.0, 1.0, 2.0, 26.0, 27.0]
        closes = [100.0, 101.0, 102.0, 90.0, 80.0]
        bt, bc = _series(horas, closes)
        now = bt[-1] + 10 * 3600
        d = canonical_direction(bt, bc, bt[0], now)
        # el terminal es la vela de la hora 2 (la última ≤ 24) ⇒ 102 > 100 ⇒ +1
        # (con la regla antigua el terminal sería la vela del lunes: 90 ⇒ −1)
        assert d == 1, "el fin de semana no debe estirar el horizonte"

    def test_determinism(self):
        """La misma t0 da SIEMPRE el mismo resultado."""
        horas = [float(i) for i in range(40)]
        closes = [100 + (i % 7) - 3 for i in range(40)]
        bt, bc = _series(horas, closes)
        now = bt[-1] + 10 * 3600
        primero = canonical_direction(bt, bc, bt[5], now)
        for _ in range(20):
            assert canonical_direction(bt, bc, bt[5], now) == primero

    def test_not_evaluable_when_the_terminal_is_not_closed(self):
        """El terminal en formación NO se evalúa (tickearía con el precio)."""
        horas = [float(i) for i in range(30)]
        closes = [100 + i for i in range(30)]
        bt, bc = _series(horas, closes)
        # now justo al abrir la vela final ⇒ el terminal (la vela 24) está
        # cerrada pero el caso límite (t0 = 5 → terminal 29) no
        assert canonical_direction(bt, bc, bt[5], bt[-1] + 0.5 * 3600) is None

    def test_flat_move_is_not_evaluable(self):
        """Sin movimiento no hay acierto ni fallo."""
        horas = [float(i) for i in range(30)]
        closes = [100.0] * 30
        bt, bc = _series(horas, closes)
        assert canonical_direction(bt, bc, bt[5], bt[-1] + 10) is None
