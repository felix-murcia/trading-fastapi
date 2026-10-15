"""T-02 (2026-10-15): presupuesto de latencia del journal QWEN-INSIGHT.

El swap a Llama-3.1-8B (b397353, 13/10) alineó el camino unificado a 45 s
pero dejó el journal en 20 s («unified prompt only»). Con un modelo de
~41 s reales el 100 % de los insights moría en ReadTimeout: 0 insights
entre 14/10 13:40 y 15/10 09:00, 16 fallos QWEN-INSIGHT en 48 h.

Este módulo clava el timeout del journal al MISMO presupuesto que el
camino unificado (QWEN_TIMEOUT_UNIFIED = 45.0): el journal no está en el
camino caliente de decisión (fire-and-forget por cierre) y el
escalonamiento 45 s < 60 s (EA) se conserva.
"""
import json
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

import services.trade_journal as trade_journal
from services.qwen_client import QWEN_TIMEOUT_UNIFIED


def _capture_client(captured: dict, content: str):
    """Fake httpx.AsyncClient que captura el timeout y devuelve JSON válido."""

    class _Resp:
        status_code = 200
        text = json.dumps({"choices": [{"message": {"content": content}}]})

        def json(self):
            return {"choices": [{"message": {"content": content}}]}

    class _Client:
        def __init__(self, timeout=None, **kwargs):
            captured["timeout"] = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, *args, **kwargs):
            captured["url"] = kwargs.get("url") or (args[0] if args else None)
            return _Resp()

    return _Client


@pytest.mark.asyncio
async def test_journal_uses_unified_timeout_budget(monkeypatch):
    """El cliente del journal se construye con QWEN_TIMEOUT_UNIFIED (45 s),
    no con un literal de 20 s — el presupuesto es UNA sola fuente."""
    captured: dict = {}
    client_cls = _capture_client(
        captured,
        '{"insight": "trend_continuation", "confidence": 0.8, '
        '"regime": "TRENDING", "quality_score": 7.0}',
    )
    monkeypatch.setattr(trade_journal.httpx, "AsyncClient", client_cls)

    insight, confidence, regime, quality = await trade_journal._query_qwen_trade_insight(
        symbol="EURUSD",
        direction="SHORT",
        entry_time=datetime(2026, 10, 15, 8, 0, tzinfo=timezone.utc),
        exit_time=datetime(2026, 10, 15, 11, 30, tzinfo=timezone.utc),
        pnl=0.0,
        pnl_pct=0.0,
        exit_reason="sl",
        sl_hit=True,
        tp_hit=False,
    )

    assert captured["timeout"] == QWEN_TIMEOUT_UNIFIED == 45.0
    # El parse completo sigue vivo:4 campos, no basura.
    assert insight == "trend_continuation"
    assert confidence == pytest.approx(0.8)
    assert regime == "TRENDING"
    assert quality == pytest.approx(7.0)


@pytest.mark.asyncio
async def test_journal_connection_error_still_raises(monkeypatch):
    """T-04 del incidente: el fallo sigue propagando (no silencioso) —
    el timeout más largo no convierte un corte de red en éxito."""

    class _Client:
        def __init__(self, timeout=None, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, *args, **kwargs):
            raise trade_journal.httpx.ReadTimeout("simulated")

    monkeypatch.setattr(trade_journal.httpx, "AsyncClient", _Client)

    with pytest.raises(RuntimeError, match="QWEN-INSIGHT"):
        await trade_journal._query_qwen_trade_insight(
            symbol="EURUSD",
            direction="LONG",
            entry_time=datetime(2026, 10, 15, 8, 0, tzinfo=timezone.utc),
            exit_time=datetime(2026, 10, 15, 9, 0, tzinfo=timezone.utc),
            pnl=15.68,
            pnl_pct=0.0348,
            exit_reason="tp",
            sl_hit=False,
            tp_hit=True,
        )
