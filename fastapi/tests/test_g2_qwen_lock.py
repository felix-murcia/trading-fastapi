"""
G2-1 (orden Félix «Termina los C», 2026-10-17) — serialización global de Qwen.

Qwen es limitado: la concurrencia efectiva tiene que ser SIEMPRE 1, sea
cuantos símbolos corran (principio rector 4 de G2). El lock (`QWEN_LOCK`)
vive DENTRO de las funciones de entrada — `query_qwen_unified`,
`query_qwen_sltp_validator` y el path de trade-insight — para que todos los
llamadores queden serializados sin que tengan que saberlo (cero
acoplamiento con routers/).

Propiedades fijadas por estos tests:
  1. N llamadas concurrentes (unified / sltp / trade-insight, y mezcladas)
     → MÁXIMO 1 request en vuelo contra el modelo.
  2. El lock se libera también en el camino de fallo (fail-closed: si
     cualquier llamador falla, el lock NO queda cogido).
  3. Métrica de cola: la espera en el lock se registra (INFO) y dispara
     WARNING por encima de QWEN_LOCK_WARN_S.
  4. Orden único de adquisición ⇒ sin deadlock: el path Qwen NUNCA corre
     bajo JOURNAL_LOCK (QWEN_LOCK es siempre el más interno) y qwen_client
     no importa locks de otros módulos (sin arista inversa posible).
"""
import asyncio
import json
import logging
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from services import qwen_client, trade_journal
from services.qwen_client import (
    query_qwen_sltp_validator,
    query_qwen_unified,
)

# Canned con contrato de parse idéntico al de test_qwen_prompt_anchoring;
# aquí el objetivo es la CONCURRENCIA, no el prompt. Se rota una LISTA de
# respuestas con razones distintas porque la guarda anti-parrota
# (PARROT_STREAK_LIMIT=3, razones similares + quality fija por símbolo)
# degradaría a la 3ª idéntica consecutiva — los 5 caller concurrentes
# emiten razones disímiles, como 5 ciclos reales.
_UNIFIED_VARIANTS = [
    ('{"quality": 6.5, "reason": "mixed signals", "bias": "NEUTRAL", '
     '"confidence_modifier": 1.0, "sl_adjusted": 0.20, "tp_adjusted": 0.40, '
     '"regime": "RANGING"}'),
    ('{"quality": 7.5, "reason": "trend continuation aligned", "bias": "BULLISH", '
     '"confidence_modifier": 1.1, "sl_adjusted": 0.20, "tp_adjusted": 0.45, '
     '"regime": "TRENDING"}'),
    ('{"quality": 5.5, "reason": "range bound rejection", "bias": "BEARISH", '
     '"confidence_modifier": 0.9, "sl_adjusted": 0.18, "tp_adjusted": 0.36, '
     '"regime": "RANGING"}'),
    ('{"quality": 8.0, "reason": "liquidity sweep reversal", "bias": "NEUTRAL", '
     '"confidence_modifier": 1.2, "sl_adjusted": 0.22, "tp_adjusted": 0.50, '
     '"regime": "BREAKOUT"}'),
    ('{"quality": 6.0, "reason": "momentum divergence fading", "bias": "BULLISH", '
     '"confidence_modifier": 1.0, "sl_adjusted": 0.20, "tp_adjusted": 0.40, '
     '"regime": "VOLATILE"}'),
]
_UNIFIED_CANNED = _UNIFIED_VARIANTS[0]
_JOURNAL_CANNED = (
    '{"insight":"timing_error","confidence":0.9,'
    '"regime":"RANGING","quality_score":5.0}'
)

_UNIFIED_ARGS = dict(
    symbol="EURUSD", decision="BUY", ml_prob=0.75, rsi=55.0, atr=0.00100,
    atr_pct=0.01, macd_hist=0.0005, bb_pos=0.6, range_pct=0.02, last_ret=0.001,
    hour=10, news="No major news", candle_context="last 10 H1 candles",
    cycle_id="EURUSD_1234567890",
    logger=MagicMock(),
)

_SLTP_ARGS = dict(
    symbol="EURUSD", decision="BUY", atr=0.00100, atr_pct=0.01, rsi=55.0,
    bb_pos=0.6, macd_hist=0.0005, range_pct=0.02, hour=10,
    sl_proposed=0.20, tp_proposed=0.40,
    cycle_id="EURUSD_1234567890",
    logger=MagicMock(),
)

_JOURNAL_ARGS = dict(
    symbol="EURUSD", direction="SHORT",
    entry_time=datetime(2026, 10, 8, 18, 0, tzinfo=timezone.utc),
    exit_time=datetime(2026, 10, 9, 5, 20, tzinfo=timezone.utc),
    pnl=-5.72, pnl_pct=-0.0091, exit_reason="ea",
    sl_hit=False, tp_hit=False,
)


@pytest.fixture(autouse=True)
def _fresh_qwen_lock(monkeypatch):
    """Lock nuevo por test (aislamiento entre event loops de pytest-asyncio)."""
    monkeypatch.setattr(qwen_client, "QWEN_LOCK", asyncio.Lock())


def _tracking_client(canned: list, delay: float = 0.05):
    """Fake de httpx.AsyncClient con contador de requests en vuelo.

    `canned` es una LISTA de cuerpos JSON-LLM que rota por índice de post
    (serialización ⇒ orden determinista), para que las respuestas de
    unified consecutivas tengan razones distintas (anti-parrota real).
    """
    bodies = [{"choices": [{"message": {"content": c}}]} for c in canned]
    texts = [json.dumps(b) for b in bodies]

    class _Client:
        state = {"in_flight": 0, "max_in_flight": 0, "posts": 0}

        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json=None, **kwargs):
            s = _Client.state
            idx = s["posts"] % len(bodies)
            s["in_flight"] += 1
            s["posts"] += 1
            s["max_in_flight"] = max(s["max_in_flight"], s["in_flight"])
            await asyncio.sleep(delay)
            s["in_flight"] -= 1

            class _Resp:
                status_code = 200
                text = texts[idx]

                def json(self, _b=bodies[idx]):
                    return _b

            return _Resp()

    return _Client


class TestQwenSingleFlight:
    """Propiedad 1 — concurrencia efectiva = 1 en todos los entry points."""

    @pytest.mark.asyncio
    async def test_unified_n_concurrent_single_flight(self):
        """N unified concurrentes → máx 1 request en vuelo, todos OK."""
        client = _tracking_client(_UNIFIED_VARIANTS, delay=0.05)
        with patch("httpx.AsyncClient", client):
            results = await asyncio.gather(
                *[query_qwen_unified(**_UNIFIED_ARGS) for _ in range(5)]
            )
        assert client.state["max_in_flight"] == 1, (
            f"Qwen debe ver 1 request en vuelo, vio {client.state['max_in_flight']}"
        )
        assert client.state["posts"] == 5
        assert all(r["ok"] is True for r in results)

    @pytest.mark.asyncio
    async def test_mixed_entrypoints_single_flight(self):
        """unified + sltp + trade-insight mezclados → siguen siendo 1 en vuelo."""
        client = _tracking_client(_UNIFIED_VARIANTS, delay=0.04)
        with patch("httpx.AsyncClient", client):
            await asyncio.gather(
                query_qwen_unified(**_UNIFIED_ARGS),
                query_qwen_sltp_validator(**_SLTP_ARGS),
                trade_journal._query_qwen_trade_insight(**_JOURNAL_ARGS),
                query_qwen_unified(**{**_UNIFIED_ARGS, "cycle_id": "E_2"}),
                query_qwen_sltp_validator(**{**_SLTP_ARGS, "cycle_id": "E_2"}),
            )
        assert client.state["max_in_flight"] == 1, (
            f"entry points mezclados vieron {client.state['max_in_flight']} en vuelo"
        )
        assert client.state["posts"] == 5

    @pytest.mark.asyncio
    async def test_journal_insight_single_flight(self):
        """Dos trade-insights concurrentes (cierres casi simultáneos)."""
        client = _tracking_client([_JOURNAL_CANNED], delay=0.04)
        with patch("httpx.AsyncClient", client):
            await asyncio.gather(
                trade_journal._query_qwen_trade_insight(**_JOURNAL_ARGS),
                trade_journal._query_qwen_trade_insight(**_JOURNAL_ARGS),
                trade_journal._query_qwen_trade_insight(**_JOURNAL_ARGS),
            )
        assert client.state["max_in_flight"] == 1
        assert client.state["posts"] == 3


class TestQwenLockFailClosed:
    """Propiedad 2 — el lock se libera SIEMPRE (incluido camino de fallo)."""

    @pytest.mark.asyncio
    async def test_lock_released_on_connection_failure(self):
        """Timeout de red → resultado degradado + lock libre para la siguiente."""
        class _Fail:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def post(self, url, json=None, **k):
                raise httpx.ConnectTimeout("boom")

        with patch("httpx.AsyncClient", _Fail):
            degraded = await query_qwen_unified(**_UNIFIED_ARGS)
        assert degraded["ok"] is False, "fallo de red ⇒ resultado degradado"
        assert not qwen_client.QWEN_LOCK.locked(), (
            "el lock quedó cogido tras una excepción (serialización fail-open)"
        )

        # Siguiente llamada con red buena ⇒ debe poder ejecutar.
        client = _tracking_client([_UNIFIED_CANNED], delay=0.0)
        with patch("httpx.AsyncClient", client):
            ok = await query_qwen_unified(**_UNIFIED_ARGS)
        assert ok["ok"] is True, "el lock sobrevivió al fallo y se liberó"

    @pytest.mark.asyncio
    async def test_lock_free_after_all_calls(self):
        client = _tracking_client([_UNIFIED_CANNED], delay=0.0)
        with patch("httpx.AsyncClient", client):
            await asyncio.gather(
                query_qwen_unified(**_UNIFIED_ARGS),
                query_qwen_sltp_validator(**_SLTP_ARGS),
                trade_journal._query_qwen_trade_insight(**_JOURNAL_ARGS),
            )
        assert not qwen_client.QWEN_LOCK.locked()


class TestQwenQueueMetric:
    """Propiedad 3 — métrica de cola: espera registrada + alerta por umbral."""

    @pytest.mark.asyncio
    async def test_queue_wait_logged(self, caplog):
        client = _tracking_client([_UNIFIED_CANNED], delay=0.0)
        with caplog.at_level(logging.INFO, logger="services.qwen_client"):
            with patch("httpx.AsyncClient", client):
                await query_qwen_unified(**_UNIFIED_ARGS)
        assert any("QWEN-LOCK wait=" in r.message for r in caplog.records), (
            "la espera en el lock no se registró (métrica de cola ausente)"
        )

    @pytest.mark.asyncio
    async def test_queue_wait_warning_above_threshold(self, monkeypatch, caplog):
        """Espera > QWEN_LOCK_WARN_S ⇒ WARNING (alerta de apilamiento)."""
        monkeypatch.setattr(qwen_client, "QWEN_LOCK_WARN_S", 0.02)
        client = _tracking_client([_UNIFIED_CANNED], delay=0.0)

        # Otro caller mantiene el lock 60 ms → espera claramente > 20 ms.
        await qwen_client.QWEN_LOCK.acquire()
        task = asyncio.create_task(query_qwen_unified(**_UNIFIED_ARGS))
        await asyncio.sleep(0.06)
        qwen_client.QWEN_LOCK.release()

        # BUG CORREGIDO (2026-10-20): la llamada HTTP va MOCKEADA. Antes este
        # test pegaba al LLM REAL (100.90.16.33:8080): tardaba 22-27 s, gastaba
        # el modelo y FALLABA siempre que el LLM no estuviera (CI, red caída,
        # LLM ocupado) ⇒ la suite no era hermética. Su hermano
        # `test_queue_wait_logged` ya mockeaba; aquí faltaba.
        with caplog.at_level(logging.WARNING, logger="services.qwen_client"), \
             patch("httpx.AsyncClient", client):
            result = await task
        assert result["ok"] is True
        assert any(
            "QWEN-LOCK wait=" in r.message and "cola de Qwen apilada" in r.message
            for r in caplog.records
        ), "la espera por encima del umbral no disparó WARNING"


class TestLockOrderingNoDeadlock:
    """Propiedad 4 — orden único de adquisición ⇒ sin deadlock."""

    @pytest.mark.asyncio
    async def test_journal_qwen_call_outside_journal_lock(self):
        """El path Qwen de _analyze_trade_async NUNCA corre bajo JOURNAL_LOCK."""
        observed = {}

        async def _fake_insight(**kwargs):
            observed["journal_locked"] = trade_journal.JOURNAL_LOCK.locked()
            return ("timing_error", 0.9, "RANGING", 5.0)

        fake_pool = MagicMock()
        fake_pool.execute = AsyncMock()

        with patch.object(trade_journal, "_query_qwen_trade_insight", _fake_insight), \
             patch.object(trade_journal, "get_pool", lambda: fake_pool):
            await trade_journal._analyze_trade_async(
                trade_id=1, symbol="EURUSD", direction="SHORT",
                entry_time=datetime(2026, 10, 8, 18, 0, tzinfo=timezone.utc),
                exit_time=datetime(2026, 10, 9, 5, 20, tzinfo=timezone.utc),
                pnl=-5.72, pnl_pct=-0.0091, exit_reason="ea",
                sl_hit=False, tp_hit=False,
            )

        assert observed["journal_locked"] is False, (
            "Qwen corrió bajo JOURNAL_LOCK ⇒ orden de adquisición doble ⇒ "
            "riesgo de deadlock (G2-1 exige QWEN_LOCK siempre el más interno)"
        )
        assert fake_pool.execute.await_count == 1, "el insight ≥0.5 debe persistirse"

    @pytest.mark.asyncio
    async def test_journal_lock_independent_of_qwen_lock(self):
        """Qwen funciona con JOURNAL_LOCK cogido ⇒ locks sin dependencia mutua."""
        await trade_journal.JOURNAL_LOCK.acquire()
        try:
            client = _tracking_client([_UNIFIED_CANNED], delay=0.0)
            with patch("httpx.AsyncClient", client):
                result = await query_qwen_unified(**_UNIFIED_ARGS)
            assert result["ok"] is True, (
                "Qwen quedó bloqueado por JOURNAL_LOCK ⇒ dependencia mutua"
            )
        finally:
            trade_journal.JOURNAL_LOCK.release()
        assert not trade_journal.JOURNAL_LOCK.locked()
        assert not qwen_client.QWEN_LOCK.locked()

    def test_qwen_client_has_no_reverse_lock_edges(self):
        """qwen_client no importa locks de otros módulos (sin arista QWEN→JOURNAL/RETRAIN)."""
        import inspect
        src = inspect.getsource(qwen_client)
        for forbidden in ("JOURNAL_LOCK", "RETRAIN_LOCK", "trade_journal", "auto_retrain"):
            assert forbidden not in src, (
                f"qwen_client referencia '{forbidden}' ⇒ posible arista inversa del grafo de locks"
            )
