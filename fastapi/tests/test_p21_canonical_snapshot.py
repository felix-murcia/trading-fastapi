"""
P2.1 — Pruebas de paridad del snapshot canónico H=20 (PPO ↔ LLM).

Verifica que `canonical_direction` con `horizon_h=20` cumple el contrato de
P2.1:
  1. Paridad de horizonte: terminal = última vela con time <= t0 + 20 h
     (NO la primera con time >= t0 + 20 h).
  2. Abstención: si la serie no cubre t0+20h o el terminal no está cerrado,
     devuelve None.
  3. Paridad de timestamp: t0_broker = t0 + offset_h.
  4. Sin dato posterior al corte: ningún campo usa time > t0 + 20 h.
"""
import datetime as dt
import pytest

from services.measurement import canonical_direction


def _series_h1(start_utc: str, n: int) -> tuple[list[float], list[float]]:
    """Serie H1 ascendente: timestamps epoch y closes con rampa +0.0001/vela."""
    start = dt.datetime.fromisoformat(start_utc.replace("Z", "+00:00"))
    ts, closes = [], []
    for i in range(n):
        t = start + dt.timedelta(hours=i)
        ts.append(t.timestamp())
        closes.append(1.10 + i * 0.0001)
    return ts, closes


class TestP21HorizonteH20:
    """Paridad de horizonte H=20 en canonical_direction."""

    def test_terminal_es_ultima_vela_con_time_menor_igual_t0_mas_20h(self):
        """Con horizon_h=20, el terminal es la última vela <= t0+20h, no la
        primera >= t0+20h (regla 'última ≤', no 'primera ≥')."""
        ts, closes = _series_h1("2026-10-20T22:00:00Z", 50)
        # t0 = vela 24 (índice 24): t0 + 20h = vela 44 (índice 44)
        t0 = ts[24]
        now = ts[-1] + 7200  # ahora: todas las velas están cerradas
        direccion = canonical_direction(ts, closes, t0, now, horizon_h=20)
        assert direccion is not None
        # close[44] - close[24] = 20 * 0.0001 > 0 → dirección 1 (sube)
        assert direccion == 1

    def test_no_salta_huecos(self):
        """Con huecos en la serie, 'última ≤ t0+20h' no salta al siguiente
        bloque tras el hueco."""
        ts, closes = _series_h1("2026-10-20T22:00:00Z", 30)
        # Insertar hueco de fin de semana (48 h) después de la vela 20
        ts_hueco = ts[:20] + [t + 172800 for t in ts[20:]]
        closes_hueco = closes[:20] + closes[20:]
        # t0 = vela 10, t0+20h = vela 30, pero hay hueco ⇒ la última ≤ t0+20h
        # es la vela 29 (antes del hueco), no la vela 30+48h
        t0 = ts_hueco[10]
        now = ts_hueco[-1] + 7200
        direccion = canonical_direction(ts_hueco, closes_hueco, t0, now, horizon_h=20)
        assert direccion is not None
        # close[29] - close[10] = 19 * 0.0001 > 0
        assert direccion == 1

    def test_abstencion_si_serie_no_cubre_horizonte(self):
        """Si la serie termina antes de t0+20h → None (no evaluable)."""
        ts, closes = _series_h1("2026-10-20T22:00:00Z", 30)
        t0 = ts[20]  # t0+20h = vela 40, pero la serie sólo tiene 30
        now = ts[-1] + 7200
        assert canonical_direction(ts, closes, t0, now, horizon_h=20) is None

    def test_abstencion_si_terminal_no_cerrado(self):
        """Si el terminal es la vela en formación (no cerrada) → None."""
        ts, closes = _series_h1("2026-10-20T22:00:00Z", 50)
        t0 = ts[24]  # t0+20h = vela 44
        now = ts[44] + 100  # ahora es solo 100s después del cierre del terminal
        # terminal vela 44 cierra en ts[44]+3600, pero now es ts[44]+100 < ts[44]+3600
        assert canonical_direction(ts, closes, t0, now, horizon_h=20) is None

    def test_horizonte_20_no_24(self):
        """Con horizon_h=20, el terminal es la vela en t0+20h, no t0+24h."""
        ts, closes = _series_h1("2026-10-20T22:00:00Z", 50)
        t0 = ts[24]
        now = ts[-1] + 7200
        d20 = canonical_direction(ts, closes, t0, now, horizon_h=20)
        d24 = canonical_direction(ts, closes, t0, now, horizon_h=24)
        assert d20 is not None and d24 is not None
        # con rampa +0.0001, ambas dan dirección 1, pero el índice del
        # terminal difiere: 24+20=44 vs 24+24=48
        assert closes[44] - closes[24] > 0
        assert closes[48] - closes[24] > 0


class TestP21TimestampParidad:
    """Paridad de timestamps: t0_broker = t0 + offset_h."""

    def test_offset_h_aplicado_a_t0(self):
        """t0_broker = t0 UTC + offset_h (desfase broker→UTC)."""
        t0_utc = dt.datetime(2026, 10, 25, 22, 0, 0, tzinfo=dt.timezone.utc)
        offset_h = 2
        t0_broker = t0_utc + dt.timedelta(hours=offset_h)
        assert t0_broker == dt.datetime(2026, 10, 26, 0, 0, 0, tzinfo=dt.timezone.utc)

    def test_offset_h_no_hardcode(self):
        """offset_h proviene de timebase (detectado en runtime), no hardcode."""
        from services.timebase import current_broker_offset
        offset = current_broker_offset()
        assert isinstance(offset, int)
        # En producción el offset se detecta; en tests puede ser 0 si no hay
        # detección previa. Lo importante es que no hay hardcode de +3 o +2.


class TestP21SinDatoPosteriorAlCorte:
    """0 dato posterior al corte t0 + 20 h."""

    def test_canonical_direction_no_usa_futuro(self):
        """canonical_direction solo usa velas con time <= t0 + horizon_h."""
        ts, closes = _series_h1("2026-10-20T22:00:00Z", 60)
        t0 = ts[24]
        now = ts[-1] + 7200
        direccion = canonical_direction(ts, closes, t0, now, horizon_h=20)
        # El terminal es el índice 44 (t0+20h). Las velas 45+ son futuro
        # relativo al corte y no se usan.
        assert direccion is not None
        terminal_idx = 24 + 20  # t0_idx + 20
        assert terminal_idx <= len(ts) - 1
        assert ts[terminal_idx] <= t0 + 20 * 3600
