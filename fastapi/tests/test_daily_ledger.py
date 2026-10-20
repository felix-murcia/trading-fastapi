"""
Registro diario inmutable (append-only) — la confianza es estructural, no verbal.

Contrato:
- cada línea se AÑADE (nunca se reescribe ni se edita una anterior);
- el fichero se versiona en git ⇒ cualquier retoque posterior deja rastro;
- cualquiera puede contrastar los números con el histórico de la BD
  (`trade_outcomes`) sin creerle a nadie.
"""
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


class TestLedgerPath:
    def test_path_is_inside_the_repo(self):
        p = daily_ledger.LEDGER_PATH
        assert p.endswith("daily_ledger.jsonl")
        assert os.path.isabs(p)
