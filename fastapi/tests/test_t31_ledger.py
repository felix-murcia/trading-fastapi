"""
T-31.9 / L4 (2026-10-20) — LEDGER DE FIABILIDAD.

Contrato:
- publica, por fuente, las MÉTRICAS DE TRADING OOS (la vara que decide),
  el track record vivo, la skill de clasificación (diagnóstico) con n y CI,
  y su ADMISIÓN con el motivo;
- "lo no medido no tiene poder": sin muestra madura ⇒ sin admisión, y el
  motivo se declara;
- el criterio de admisión de un challenger está PRE-REGISTRADO (expectancy
  OOS > la del sistema vigente, DD ≤ 10 %, ops ≤ 40 por holdout, n ≥ 30);
- el ledger NO muta nada (es un informe de estado).
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from main import app
from services import ledger as ledger_mod
from services.ledger import (
    ADMISSION_MAX_DD,
    ADMISSION_MAX_OPS_PER_HOLDOUT,
    ADMISSION_MIN_TRADES,
    build_ledger,
)


class TestAdmissionRules:
    @pytest.mark.asyncio
    async def test_rules_are_preregistered(self):
        assert ADMISSION_MIN_TRADES == 30
        assert ADMISSION_MAX_DD == 0.10
        assert ADMISSION_MAX_OPS_PER_HOLDOUT == 40

    @pytest.mark.asyncio
    async def test_classification_is_diagnostic_not_admission(self):
        """La clasificación NO decide la admisión (L1/E1b): sólo las
        métricas de trading OOS."""
        l = await build_ledger()
        assert "métricas de TRADING OOS" in l["method"]["admission_var"]
        assert "NO decide admisión" in l["method"]["classification_var"]


class TestLedgerContent:
    @pytest.mark.asyncio
    async def test_contains_every_source_with_a_reason(self):
        l = await build_ledger()
        for name in ("sistema_vivo", "llm", "quorum"):
            assert name in l["sources"], f"falta la fuente {name}"
            src = l["sources"][name]
            assert "admitted" in src, f"{name} sin veredicto de admisión"
            assert src.get("admission_reason"), f"{name} sin motivo de admisión"

    @pytest.mark.asyncio
    async def test_llm_without_mature_sample_is_not_admitted(self):
        """Sin muestra madura ⇒ el LLM NO tiene poder (y se dice por qué)."""
        with patch.object(ledger_mod.qwen_authority, "peek", return_value={
            "granted": False, "n": 0, "acc": None, "wilson_lower": None,
            "reason": "muestra insuficiente (0 < 30)", "calibration": {},
        }):
            l = await build_ledger()
        assert l["sources"]["llm"]["admitted"] is False
        assert "muestra insuficiente" in l["sources"]["llm"]["admission_reason"]

    @pytest.mark.asyncio
    async def test_llm_with_mature_sample_can_be_admitted(self):
        with patch.object(ledger_mod.qwen_authority, "peek", return_value={
            "granted": True, "n": 46, "acc": 0.696, "wilson_lower": 0.552,
            "reason": "skill demostrada (69.6% sobre 46 llamadas)",
            "calibration": {"0.75": {"n": 29, "hits": 25, "acc": 0.86}},
        }):
            l = await build_ledger()
        assert l["sources"]["llm"]["admitted"] is True
        assert l["sources"]["llm"]["authority"]["n"] == 46
        assert l["sources"]["llm"]["authority"]["wilson_lower"] == 0.552

    @pytest.mark.asyncio
    async def test_publishes_the_pending_evidence_criteria(self):
        l = await build_ledger()
        crit = l["summary"]["pending_evidence"]["challengers"]
        assert str(ADMISSION_MIN_TRADES) in crit
        assert "expectancy" in crit


class TestLedgerEndpoint:
    @pytest.mark.asyncio
    async def test_endpoint_returns_the_ledger(self):
        client = TestClient(app)
        r = client.get("/api/v1/ai/ledger")
        assert r.status_code == 200, r.text
        body = r.json()
        assert "sources" in body and "method" in body
        assert "sistema_vivo" in body["sources"]


class _TrackRecordPool:
    def __init__(self):
        self.query = None
        self.args = None

    async def fetch(self, query, *args):
        self.query = query
        self.args = args
        return [{
            "n": 3, "pnl": 4.77, "expectancy": 1.59,
            "wins": 1, "losses": 2,
            "first_entry": None, "last_exit": None,
        }]


class TestLiveTrackRecordWinRule:
    """El track record vivo también clasifica con la regla de 3 €.

    Un +0,54 € o +0,77 € es una pérdida; antes la SQL usaba `pnl > 0` y
    inflaba el win rate del ledger de fiabilidad.
    """

    @pytest.mark.asyncio
    async def test_uses_win_threshold_and_publishes_it(self):
        pool = _TrackRecordPool()
        with patch("db.connection.get_pool", lambda: pool):
            tr = await ledger_mod._live_track_record("EURUSD")
        assert tr["wins"] == 1
        assert tr["losses"] == 2
        assert tr["win_rate"] == 0.3333
        assert tr["win_threshold_eur"] == 3.0
        assert "pnl >= $2" in pool.query
        assert "pnl < $2" in pool.query
        assert pool.args[1] == 3.0


# ─── B4.2: desacoplamiento de broker ──────────────────────────────────────────

class TestB42LedgerNoMt5ClientDirectImport:
    """services/ledger.py no importa services.mt5_client directamente."""

    def test_no_module_level_import_of_mt5_client(self):
        """El módulo ledger no hace `from services import mt5_client` ni `from services.mt5_client import`."""
        import services.ledger as ledger_mod
        source = open(ledger_mod.__file__).read()
        assert "from services import mt5_client" not in source
        assert "from services.mt5_client import" not in source
        assert "import services.mt5_client" not in source
