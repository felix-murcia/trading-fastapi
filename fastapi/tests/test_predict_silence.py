"""
P2-H14 — watchdog de silencio de ai_predict_cycle (services/predict_silence.py).

El blackout de ~62 h (H14) se detectó leyendo la BD a mano: no existía
ninguna alerta de silencio. Estos tests fijan:

  - La ventana de supresión derivada de la evidencia histórica del
    audit_log (0 ciclos sáb/dom; viernes último ciclo 20:55 → supresión
    desde 21:00; lunes con ciclos desde 00:00 → NO se suprime).
  - El umbral de 45 min con su frontera exacta (sin falsos positivos por
    un ciclo perdido, alerta real si el EA/MT5 cae).
  - El registro del loop en el lifespan de main.py (patrón de los demás
    loops programados).
"""
import os
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import services.predict_silence as ps
from services.predict_silence import SILENCE_THRESHOLD_S, _market_closed, check_silence

UTC = timezone.utc
WED_NOON = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)  # miércoles (mercado activo)


# ── _market_closed: ventana de supresión ──────────────────────────────────

@pytest.mark.parametrize("weekday", [5, 6])  # sábado(5), domingo(6)
def test_weekend_suppressed(weekday):
    base = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)  # lunes
    dt = base + timedelta(days=(weekday - base.weekday()) % 7)
    assert _market_closed(dt) is True


@pytest.mark.parametrize("hour", [21, 22, 23])
def test_friday_evening_suppressed(hour):
    # Evidencia: el último ciclo del viernes es 20:55 UTC (cierre FX ~21:00).
    dt = datetime(2026, 10, 9, hour, 0, tzinfo=UTC)
    assert dt.weekday() == 4
    assert _market_closed(dt) is True


def test_friday_last_cycle_hour_not_suppressed():
    dt = datetime(2026, 10, 9, 20, 59, tzinfo=UTC)  # viernes, antes del cierre
    assert _market_closed(dt) is False


def test_monday_midnight_not_suppressed():
    # Evidencia: los lunes hay ciclos desde 00:00 → el silencio del lunes
    # a medianoche es REAL (así se habría visto el incidente del 05).
    dt = datetime(2026, 10, 5, 0, 0, tzinfo=UTC)
    assert dt.weekday() == 0
    assert _market_closed(dt) is False


@pytest.mark.parametrize("hour", [0, 3, 9, 14, 20, 23])
def test_weekday_active_all_day(hour):
    dt = datetime(2026, 10, 7, hour, 0, tzinfo=UTC)  # miércoles
    assert dt.weekday() == 2
    assert _market_closed(dt) is False


# ── check_silence: umbral y alertas ───────────────────────────────────────

async def _check(last, now):
    """check_silence con pool y alerta mockados → (edad_s, mock_alert)."""
    pool = MagicMock()
    pool.fetchval = AsyncMock(return_value=last)
    with patch("db.connection.get_pool", return_value=pool), \
         patch.object(ps, "send_alert") as alert:
        age = await check_silence(now=now)
    return age, alert


@pytest.mark.asyncio
async def test_recent_cycle_no_alert():
    age, alert = await _check(WED_NOON - timedelta(minutes=2), WED_NOON)
    assert age == 120
    alert.assert_not_called()


@pytest.mark.asyncio
async def test_stale_cycle_weekday_alerts():
    age, alert = await _check(WED_NOON - timedelta(hours=3), WED_NOON)
    assert age == 3 * 3600
    alert.assert_called_once()
    args, kwargs = alert.call_args
    assert args[1] == "PREDICT-SILENCE"
    assert "180 min" in args[2]
    assert kwargs["context"]["edad_s"] == 3 * 3600


@pytest.mark.asyncio
async def test_threshold_boundary():
    """Un ciclo perdido de vez en cuando no alerta: la frontera es 45 min."""
    _, alert = await _check(
        WED_NOON - timedelta(seconds=SILENCE_THRESHOLD_S - 60), WED_NOON
    )
    alert.assert_not_called()

    _, alert = await _check(
        WED_NOON - timedelta(seconds=SILENCE_THRESHOLD_S + 60), WED_NOON
    )
    alert.assert_called_once()


@pytest.mark.asyncio
async def test_stale_saturday_suppressed():
    sat = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
    assert sat.weekday() == 5
    age, alert = await _check(sat - timedelta(hours=6), sat)
    assert age == 6 * 3600  # la edad se devuelve…
    alert.assert_not_called()  # …pero NO se alerta en fin de semana


@pytest.mark.asyncio
async def test_stale_friday_close_suppressed():
    fri = datetime(2026, 10, 9, 21, 30, tzinfo=UTC)  # viernes tras el cierre
    last = datetime(2026, 10, 9, 20, 55, tzinfo=UTC)  # último ciclo real
    age, alert = await _check(last, fri)
    assert age == 35 * 60
    alert.assert_not_called()


@pytest.mark.asyncio
async def test_no_cycles_ever_alerts_on_weekday():
    age, alert = await _check(None, WED_NOON)
    assert age is None
    alert.assert_called_once()
    args, _ = alert.call_args
    assert "NINGÚN" in args[2]


@pytest.mark.asyncio
async def test_no_cycles_weekend_suppressed():
    sat = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
    age, alert = await _check(None, sat)
    assert age is None
    alert.assert_not_called()


@pytest.mark.asyncio
async def test_naive_timestamp_treated_as_utc():
    age, alert = await _check(datetime(2026, 10, 7, 9, 0), WED_NOON)
    assert age == 3 * 3600
    alert.assert_called_once()


# ── registro del loop ─────────────────────────────────────────────────────

def test_loop_wired_in_lifespan():
    here = os.path.dirname(ps.__file__)
    with open(os.path.join(here, "..", "main.py")) as f:
        src = f.read()
    assert "_bg_tasks.add(asyncio.create_task(_predict_silence_loop()))" in src
