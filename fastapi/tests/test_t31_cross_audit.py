"""
T-31.9 / P2 (2026-10-20) — AUDITORÍA CRUZADA (fiabilidad absoluta).

Contrato:
- recomputa INDEPENDIENTIENTE los números publicados y compara;
- una discrepancia se DECLARA (check con status="discrepancia") y se avisa;
- nunca lanza (un fallo es un check con status="error", no una excepción);
- sólo lectura: no muta el trading.
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from main import app
from services import cross_audit


class TestCrossAudit:
    @pytest.mark.asyncio
    async def test_reports_all_four_checks(self):
        with patch.object(cross_audit, "_cross_audit_loop", new=AsyncMock()):
            v = await cross_audit.run_cross_audit("EURUSD")
        names = {c["check"] for c in v["checks"]}
        assert {"llm_skill", "live_track_record", "drift_psi", "benchmark_sma200"} <= names
        assert v["verdict"] in ("COHERENTE", "DISCREPANCIA")
        assert "generated_at" in v

    @pytest.mark.asyncio
    async def test_skill_discrepancy_is_flagged(self):
        """Si la skill recomputada difiere de la publicada más que la
        tolerancia ⇒ DISCREPANCIA (no se maquilla)."""
        with patch("services.qwen_authority._measure", AsyncMock(return_value={
            "n": 46, "hits": 32, "acc": 0.696, "wilson_lower": 0.552,
            "granted": True, "reason": "x", "calibration": {},
        })), patch("services.qwen_authority.peek", return_value={
            "n": 46, "hits": 10, "acc": 0.217, "wilson_lower": 0.10,
            "granted": False, "reason": "y", "calibration": {},
        }):
            v = await cross_audit.run_cross_audit("EURUSD")
        chk = next(c for c in v["checks"] if c["check"] == "llm_skill")
        assert chk["status"] == "discrepancia"
        assert v["verdict"] == "DISCREPANCIA"
        assert v["n_discrepancias"] >= 1

    @pytest.mark.asyncio
    async def test_no_sample_is_not_a_discrepancy(self):
        """Sin muestra madura (n=0) NO es una discrepancia: es «sin datos»."""
        with patch("services.qwen_authority._measure", AsyncMock(return_value={
            "n": 0, "hits": 0, "acc": None, "wilson_lower": None,
            "granted": False, "reason": "muestra insuficiente (0 < 30)",
            "calibration": {},
        })), patch("services.qwen_authority.peek", return_value={}):
            v = await cross_audit.run_cross_audit("EURUSD")
        chk = next(c for c in v["checks"] if c["check"] == "llm_skill")
        assert chk["status"] == "sin_datos"

    @pytest.mark.asyncio
    async def test_never_raises(self):
        """Un fallo interno es un check con error, nunca una excepción."""
        with patch("services.qwen_authority._measure", AsyncMock(side_effect=RuntimeError("boom"))):
            v = await cross_audit.run_cross_audit("EURUSD")
        chk = next(c for c in v["checks"] if c["check"] == "llm_skill")
        assert chk["status"] == "error"
        assert "boom" in chk["error"]


class TestCrossAuditEndpoint:
    def test_endpoint_runs_the_audit(self):
        client = TestClient(app)
        r = client.get("/api/v1/ai/cross-audit")
        assert r.status_code == 200, r.text
        body = r.json()
        assert "checks" in body and "verdict" in body
