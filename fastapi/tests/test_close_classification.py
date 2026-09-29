"""
Convención crítica (AGENTS.md): "Any close with < 3 EUR profit is a loss."

Antes de este cambio NINGÚN punto del sistema implementaba la regla: el
dashboard contaba `pnl > 0`, el Journal de Qwen etiquetaba `pnl_pct > 0` y
el env de entrenamiento contaba `realized_pnl > 0` — un cierre de +0.95€
se clasificaba como WIN en los tres sitios. Cubrimos:

- core.rules.is_winning_close: frontera exacta MIN_WIN_EUR (3€).
- /v1/smc/analytics: wins/losses del dashboard con la regla.
- Journal Qwen: etiqueta WIN/LOSS + pnl EUR en el prompt.
- Env PPO: win_rate del entrenamiento con la misma regla.
- record_trade_filled: el pnl (EUR) llega al journal.
"""
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from core.rules import MIN_WIN_EUR, is_winning_close
from main import app
from services import auto_retrain, trade_journal
from tests.test_trading_env_step import (
    COMM,
    FLAT,
    HALF_SPREAD,
    LOT,
    VOL_OPEN,
    _make_opened_env,
)

AUTH_HEADERS = {"x-internal-token": "test-token", "x-cycle-id": "classification"}


class TestIsWinningClose:
    """Frontera exacta: >= 3€ es ganancia, por debajo (incluido 0) es pérdida."""

    def test_constant_is_3_eur(self):
        assert MIN_WIN_EUR == 3.0

    @pytest.mark.parametrize("pnl", [3.0, 3.01, 6.94, 100.0])
    def test_closes_at_or_above_threshold_are_wins(self, pnl):
        assert is_winning_close(pnl) is True

    @pytest.mark.parametrize("pnl", [2.9999, 1.37, 0.95, 0.0, -0.01, -4.16])
    def test_closes_below_threshold_are_losses(self, pnl):
        assert is_winning_close(pnl) is False


def _fake_httpx_client(deals):
    """httpx.AsyncClient de mentira: /history/deals y /account/info."""

    class _Resp:
        status_code = 200

        def __init__(self, payload):
            self._payload = payload

        def json(self):
            return self._payload

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, *args, **kwargs):
            if url.endswith("/api/v1/history/deals"):
                return _Resp(deals)
            return _Resp({"balance": 4200.0})

    return _Client


def _deal(profit, time_msc, entry=1):
    return {
        "entry": entry,
        "profit": profit,
        "swap": 0.0,
        "commission": 0.0,
        "time_msc": time_msc,
        "symbol": "EURUSD",
    }


class TestAnalyticsWinLossClassification:
    """/v1/smc/analytics: el gráfico W/L del dashboard clasifica con la regla."""

    def _analytics(self, deals):
        with patch("main.init_pool", AsyncMock()), \
             patch("main.close_pool", AsyncMock()), \
             patch("routers.ai.load_inference_artifacts"), \
             patch("httpx.AsyncClient", _fake_httpx_client(deals)):
            with TestClient(app) as client:
                return client.get("/v1/smc/analytics", headers=AUTH_HEADERS)

    def test_sub_3eur_positives_count_as_losses(self):
        deals = [
            _deal(0.95, 1790000000000),   # positivo < 3€ → pérdida
            _deal(5.00, 1790000100000),   # ≥ 3€ → win
            _deal(3.00, 1790000200000),   # frontera exacta → win
            _deal(0.00, 1790000300000),   # 0€ → pérdida
            _deal(50.0, 1790000400000, entry=0),  # apertura: fuera del conteo
        ]
        res = self._analytics(deals)
        assert res.status_code == 200
        data = res.json()
        assert data["wins"] == 2
        assert data["losses"] == 2

    def test_small_positive_breaks_the_win_streak(self):
        deals = [
            _deal(5.0, 1790000000000),
            _deal(1.37, 1790000100000),   # < 3€ → es una racha de pérdida
            _deal(4.0, 1790000200000),
        ]
        data = self._analytics(deals).json()
        assert data["wins"] == 2
        assert data["losses"] == 1
        assert data["kpis"]["max_win_streak"] == 1
        assert data["kpis"]["max_loss_streak"] == 1


class TestJournalWinLossLabel:
    """El prompt que recibe Qwen etiqueta WIN/LOSS por el pnl en euros."""

    async def _prompt_for(self, pnl, pnl_pct):
        captured = {}

        class _Resp:
            status_code = 200

            def json(self):
                return {"choices": [{"message": {"content":
                    '{"insight":"timing_error","confidence":0.9,'
                    '"regime":"RANGING","quality_score":5.0}'}}]}

        class _Client:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def post(self, url, json=None, **kwargs):
                captured["prompt"] = json["messages"][1]["content"]
                return _Resp()

        with patch("httpx.AsyncClient", _Client):
            await trade_journal._query_qwen_trade_insight(
                symbol="EURUSD",
                direction="SHORT",
                entry_time=datetime(2026, 9, 29, 15, 5, tzinfo=timezone.utc),
                exit_time=datetime(2026, 9, 29, 15, 10, tzinfo=timezone.utc),
                pnl=pnl,
                pnl_pct=pnl_pct,
                exit_reason="ea",
                sl_hit=False,
                tp_hit=False,
            )
        return captured["prompt"]

    @pytest.mark.asyncio
    async def test_positive_close_below_3eur_is_labeled_loss(self):
        prompt = await self._prompt_for(1.37, 0.0229)
        assert "(LOSS)" in prompt
        assert "1.37 EUR" in prompt

    @pytest.mark.asyncio
    async def test_close_reaching_3eur_is_labeled_win(self):
        prompt = await self._prompt_for(6.94, 0.1733)
        assert "(WIN)" in prompt
        assert "6.94 EUR" in prompt


class TestEnvWinRateUsesRule:
    """El win_rate del entrenamiento cuenta con la misma regla que el mercado."""

    def _env_closing_at(self, target_pnl):
        """Env abierta (LONG) cuyo cierre discrecional rinde target_pnl euros.

        realized = VOL_OPEN × LOT × (close − HALF_SPREAD − entry) − comisión
        → close = entry + HALF_SPREAD + (target + comisión) / (LOT × VOL)
        """
        env, entry = _make_opened_env()
        close = entry + HALF_SPREAD + (target_pnl + VOL_OPEN * COMM) / (VOL_OPEN * LOT)
        env.df.loc[6, ["high", "low", "close"]] = [close + 0.0002, close - 0.0002, close]
        return env

    def test_positive_close_below_3eur_counts_as_loss(self):
        env = self._env_closing_at(1.5)
        balance_before = env.balance

        _, _, terminated, _, info = env.step(FLAT)

        assert info["trades"] == 1
        assert info["balance"] > balance_before       # el pnl es POSITIVO…
        assert info["win_rate"] == 0.0                # …y aun así es pérdida
        assert terminated is False

    def test_close_reaching_3eur_counts_as_win(self):
        env = self._env_closing_at(3.5)

        _, _, terminated, _, info = env.step(FLAT)

        assert info["trades"] == 1
        assert info["win_rate"] == 1.0
        assert terminated is False


class TestRecordTradeFilledThreadsPnl:
    """record_trade_filled entrega el pnl (EUR) al Journal para clasificar."""

    @pytest.mark.asyncio
    async def test_pnl_eur_reaches_journal(self, monkeypatch):
        state = auto_retrain.RetrainState()
        monkeypatch.setattr(
            auto_retrain, "_persist_trade_outcome", AsyncMock(return_value=42)
        )
        monkeypatch.setattr(
            auto_retrain, "_ensure_retrain_state", AsyncMock(return_value=state)
        )
        monkeypatch.setattr(auto_retrain, "_save_retrain_state", AsyncMock())

        with patch(
            "services.trade_journal.record_trade_closed", AsyncMock()
        ) as journal:
            await auto_retrain.record_trade_filled(
                symbol="EURUSD",
                entry_time=1725450000,
                exit_time=1725453600,
                pnl=1.37,
                pnl_pct=0.0229,
                direction="SHORT",
                sl_hit=False,
                tp_hit=False,
                exit_reason="ea",
            )

        assert journal.call_args.kwargs["pnl"] == 1.37
        assert state.filled_count == 1
