"""
T-31/Promo (orden Félix 2026-10-19) — endpoint de promoción de un
candidato YA MEDIDO.

Contrato:
- NO se salta la regla: el informe se vuelve a evaluar con
  `services/promotion_rule.py` y sin concesión NO promueve;
- con concesión, promueve (artefactos → paths vivos + manifest + registry +
  hot-reload);
- token interno obligatorio;
- todo queda en la auditoría (`candidate_promoted` / `..._rejected`).
"""
import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from main import app
from services import candidate_promotion

REPORT_OK = {
    "exp_id": 99, "symbol": "EURUSD", "obs_shape": [20, 25],
    "gate": {"ok": False, "reason": "absolute_floor_sum_reward"},
    "walk_forward": {
        "n_folds": 4, "fold_wins": 4,
        "candidate": {"sum_reward": -33.4, "mean_sharpe": -1.3, "total_expectancy": -6.7},
        "baseline": {"sum_reward": -138.9, "mean_sharpe": -1.0, "total_expectancy": -20.0},
        "holdout": {
            "candidate": {"reward": -1.86, "sharpe": 0.19, "expectancy": -0.53,
                          "trades": 16, "max_dd": 0.045},
            "baseline": {"reward": -39.86, "sharpe": -1.07, "expectancy": -1.60,
                         "trades": 137, "max_dd": 0.4286},
        },
    },
    "t28_alignment": {"ok": False, "conflict_rate": 0.95, "n_active": 100},
}

REPORT_MEJOR_BUT_NOT_DRAMATIC = json.loads(json.dumps(REPORT_OK))
REPORT_MEJOR_BUT_NOT_DRAMATIC["walk_forward"]["fold_wins"] = 2


def _write_artifacts(tmp_path, report):
    art = tmp_path / "exp99"
    art.mkdir(parents=True, exist_ok=True)
    (art / "report.json").write_text(json.dumps(report))
    (art / "candidate.zip").write_bytes(b"PK-fake-zip")
    (art / "candidate_vec_norm.pkl").write_bytes(b"fake-pickle")
    return str(art)


class TestPromoteCandidateEndpoint:
    def test_requires_token(self):
        client = TestClient(app)
        r = client.post("/api/v1/ai/retrain/promote-candidate?exp_id=99")
        assert r.status_code in (401, 403, 422), r.text

    @pytest.mark.asyncio
    async def test_rule_blocks_when_not_dramatic(self, tmp_path):
        """Sin concesión de la regla → NO promueve (aunque se lo pida)."""
        _write_artifacts(tmp_path, REPORT_MEJOR_BUT_NOT_DRAMATIC)
        with patch.object(candidate_promotion, "_artifact_dir", return_value=str(tmp_path / "exp99")), \
             patch.object(candidate_promotion, "_promote_bundle_files", AsyncMock()) as promo_mock:
            client = TestClient(app)
            r = client.post(
                "/api/v1/ai/retrain/promote-candidate?exp_id=99&symbol=EURUSD",
                headers={"x-internal-token": "test-token"},
            )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["promoted"] is False
        assert "fold_wins_all" in body["promotion_path"]["drastic"]["failed"]
        promo_mock.assert_not_called(), "no debe promoting sin concesión"

    @pytest.mark.asyncio
    async def test_rule_grants_and_promotes(self, tmp_path):
        """Con concesión → promueve y devuelve el detalle de los 8 criterios."""
        _write_artifacts(tmp_path, REPORT_OK)
        with patch.object(candidate_promotion, "_artifact_dir", return_value=str(tmp_path / "exp99")), \
             patch.object(candidate_promotion, "_load_training_df", AsyncMock(return_value=MagicMock())), \
             patch.object(candidate_promotion, "_promote_bundle_files", AsyncMock(
                 return_value={"model_sha256": "deadbeef"})) as promo_mock:
            client = TestClient(app)
            r = client.post(
                "/api/v1/ai/retrain/promote-candidate?exp_id=99&symbol=EURUSD",
                headers={"x-internal-token": "test-token"},
            )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["promoted"] is True
        assert body["promotion_path"]["path"] == "drastic_improvement"
        assert body["model_sha256"] == "deadbeef"
        promo_mock.assert_called_once()
        assert promo_mock.await_args.kwargs["obs_shape"] == [20, 25]

    @pytest.mark.asyncio
    async def test_missing_artifacts_is_reported(self, tmp_path):
        with patch.object(candidate_promotion, "_artifact_dir", return_value=str(tmp_path / "nope")):
            client = TestClient(app)
            r = client.post(
                "/api/v1/ai/retrain/promote-candidate?exp_id=99&symbol=EURUSD",
                headers={"x-internal-token": "test-token"},
            )
        assert r.status_code == 200, r.text
        assert r.json()["promoted"] is False
        assert "artefacto ausente" in r.json()["error"]
