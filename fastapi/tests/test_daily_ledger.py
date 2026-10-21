"""
Registro diario inmutable (append-only) — la confianza es estructural, no verbal.

Contrato:
- cada línea se AÑADE (nunca se reescribe ni se edita una anterior);
- el fichero se versiona en git ⇒ cualquier retoque posterior deja rastro;
- cualquiera puede contrastar los números con el histórico de la BD
  (`trade_outcomes`) sin creerle a nadie.
"""
import asyncio
import json
import os
import tempfile

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
