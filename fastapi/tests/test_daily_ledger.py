"""
Registro diario inmutable (append-only) — la confianza es estructural, no verbal.

Contrato:
- cada línea se AÑADE (nunca se reescribe ni se edita una anterior);
- el fichero se versiona en git ⇒ cualquier retoque posterior deja rastro;
- cualquiera puede contrastar los números con el histórico de la BD
  (`trade_outcomes`) sin creerle a nadie.
"""
import asyncio
import datetime as dt
import json
import os
import tempfile
from unittest.mock import AsyncMock, patch

import pytest

from services import daily_ledger


class TestAppendOnly:
    def test_appends_without_rewriting(self, tmp_path):
        path = str(tmp_path / "ledger.jsonl")
        daily_ledger.append_entry({"ts": "2026-10-20 10:00", "equity_eur": 500.0}, path)
        daily_ledger.append_entry({"ts": "2026-10-20 11:00", "equity_eur": 505.0}, path)
        lines = open(path, encoding="utf-8").read().strip().split("\n")
        assert len(lines) == 2
        assert json.loads(lines[0])["equity_eur"] == 500.0, "la primera línea no puede cambiar"
        assert json.loads(lines[1])["equity_eur"] == 505.0

    def test_creates_the_directory(self, tmp_path):
        path = str(tmp_path / "a" / "b" / "ledger.jsonl")
        daily_ledger.append_entry({"ts": "x"}, path)
        assert os.path.exists(path)

    def test_entry_has_the_essentials(self):
        """La línea debe llevar lo que hace falta para juzgarla sola."""
        from services.daily_ledger import build_daily_entry
        import inspect
        src = inspect.getsource(build_daily_entry)
        for key in ("equity_eur", "trades_today", "pnl_today_eur",
                    "today_pnl_from_winners_eur", "today_pnl_from_losers_eur",
                    "history", "bundle_sha", "llm_authority"):
            assert key in src, f"falta {key} en la línea del registro"
        assert '"equity_scope": "account"' in src, "el equity es de la cuenta, no del símbolo"
        assert '"trades_scope": "symbol"' in src, "las operaciones son del símbolo"

    def test_authority_is_measured_not_read_from_a_stale_cache(self):
        """T-31.10: la línea publica la MEDICIÓN (n, base, skill, IC), no un
        `peek()` que tras un reinicio deja `granted: null` (ambigüedad)."""
        from services.daily_ledger import build_daily_entry
        import inspect
        src = inspect.getsource(build_daily_entry)
        assert "get_authority" in src
        assert "qwen_authority.peek" not in src
        for key in ("base", "skill_pp", "wilson_lower", "measured_at"):
            assert key in src, f"falta {key} en la autoridad publicada"


class TestLedgerPath:
    def test_path_is_inside_the_repo(self):
        p = daily_ledger.LEDGER_PATH
        assert p.endswith("daily_ledger.jsonl")
        assert os.path.isabs(p)


class TestPerSymbol:
    """G3 (2026-10-21): el registro sigue a TODOS los mundos vivos.

    Antes la línea era sólo EURUSD; la operación USDCHF #278 (+0,54 €) estaba
    en `trade_outcomes` pero no en el registro diario.
    """

    def test_active_symbols_follows_enabled_pairs(self, monkeypatch):
        from config import settings
        monkeypatch.setattr(settings, "pairs", [
            {"symbol": "EURUSD", "enabled": True},
            {"symbol": "GBPUSD", "enabled": False},
            {"symbol": "USDCHF", "enabled": True},
        ], raising=False)
        assert daily_ledger.active_symbols() == ["EURUSD", "USDCHF"]

    def test_records_one_line_per_symbol(self, monkeypatch):
        async def fake(sym):
            return {"symbol": sym}
        monkeypatch.setattr(daily_ledger, "record_now", fake)
        out = asyncio.run(daily_ledger.record_all(["EURUSD", "USDCHF"]))
        assert [e["symbol"] for e in out] == ["EURUSD", "USDCHF"]

    def test_one_failure_does_not_stop_the_other(self, monkeypatch):
        async def fake(sym):
            if sym == "EURUSD":
                raise RuntimeError("boom")
            return {"symbol": sym}
        monkeypatch.setattr(daily_ledger, "record_now", fake)
        out = asyncio.run(daily_ledger.record_all(["EURUSD", "USDCHF"]))
        assert [e["symbol"] for e in out] == ["USDCHF"]


class _FakePool:
    """Pool mínimo: registra la SQL y devuelve filas controladas."""

    def __init__(self, trades, hist):
        self.trades = trades
        self.hist = hist
        self.fetch_query = None
        self.fetch_args = None
        self.fetchrow_query = None
        self.fetchrow_args = None

    async def fetch(self, query, *args):
        self.fetch_query = query
        self.fetch_args = args
        return self.trades

    async def fetchrow(self, query, *args):
        self.fetchrow_query = query
        self.fetchrow_args = args
        return self.hist


class _FakeResponse:
    def raise_for_status(self):
        return None

    def json(self):
        return {"equity": 500.0}


class _FakeHttpxClient:
    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, *args, **kwargs):
        return _FakeResponse()


class TestWinClassification:
    """AGENTS.md: un cierre < 3 € es PÉRDIDA, aunque sea positivo.

    Antes `today_wins`/`history.wins` usaban `pnl > 0`: el cierre USDCHF
    +0,54 € y el EURUSD +0,77 € aparecían como ganancias. Ahora la línea
    declara `win_threshold_eur` y la SQL usa el mismo umbral.
    """

    def _entry(self, trades, hist):
        pool = _FakePool(trades, hist)
        with patch("db.connection.get_pool", lambda: pool), \
             patch("httpx.AsyncClient", _FakeHttpxClient), \
             patch("services.timebase.broker_offset_hours",
                   AsyncMock(return_value=3)), \
             patch("services.timebase.utc_day_bounds",
                   lambda now: (now, now)), \
             patch("core.pair.default_model_path",
                   side_effect=FileNotFoundError), \
             patch("services.qwen_authority.get_authority",
                   AsyncMock(return_value={
                       "granted": False, "n": 0, "acc": None, "base": None,
                       "skill_pp": None, "wilson_lower": None,
                       "measured_at": None, "reason": "sin muestra",
                   })):
            entry = asyncio.run(daily_ledger.build_daily_entry("EURUSD"))
        return entry, pool

    def test_sub_3eur_positives_count_as_losses(self):
        entry, pool = self._entry(
            [{"pnl": 0.77}, {"pnl": 5.0}, {"pnl": -1.0}],
            {"n": 3, "pnl": 4.77, "wins": 1, "losses": 2},
        )
        assert entry["today_wins"] == 1
        assert entry["today_losses"] == 2
        assert entry["today_pnl_from_winners_eur"] == 5.0
        assert entry["today_pnl_from_losers_eur"] == -0.23
        assert entry["history"]["wins"] == 1
        assert entry["history"]["losses"] == 2
        assert entry["win_threshold_eur"] == 3.0
        assert "pnl >= $2" in pool.fetchrow_query
        assert "pnl < $2" in pool.fetchrow_query
        assert pool.fetchrow_args[1] == 3.0

    def test_close_at_3eur_is_a_win(self):
        entry, _ = self._entry(
            [{"pnl": 3.0}],
            {"n": 1, "pnl": 3.0, "wins": 1, "losses": 0},
        )
        assert entry["today_wins"] == 1
        assert entry["today_losses"] == 0
        assert entry["today_pnl_from_winners_eur"] == 3.0
