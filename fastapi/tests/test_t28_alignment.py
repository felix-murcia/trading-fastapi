"""T-28 (REQ-1): métrica de alineación PPO↔LLM, registro por ciclo y gate.

Pre-registración: `audits/t28-alignment-metric-2026-10-17.md` (D1–D6). Los
umbrales están congelados ANTES de la primera medición — `TestThresholdsFrozen`
fija sus valores exactos como guardia anti-deriva (cambio = nueva pre-registración).
"""
import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest
from fastapi.testclient import TestClient

from main import app
from services import qwen_client
from services.alignment_metric import (
    T28_AGREEMENT_MIN,
    T28_CONFLICT_MAX,
    T28_ENTROPY_MIN_BITS,
    T28_MIN_ACTIVE,
    T28_WINDOW,
    alignment_gate,
    compute_report,
    joint_entropy_bits,
    load_recent_pairs,
    pair_state,
    polarity,
    rolling_alignment,
)
from services.qwen_client import QwenHealthState
from tests.test_e2e_ai_predict import FakeAsyncClient
from tests.test_qwen_semantics import _mock_ppo_model

# Cabeza direction positiva SIN posición → decode v3 da BUY (entrada legítima)
ACTION_BUY = [0.80, 0.18, 0.15, 0.15]


# ───────────────────────────── D1: estados de par ─────────────────────────────

class TestPairState:
    """Vocabulario completo (D1) + estados (D3). Ningún caso adivina."""

    @pytest.mark.parametrize("ppo,llm,expected", [
        # aligned: misma dirección (ambos con dirección)
        ("BUY", "BULLISH", "aligned"),
        ("SELL", "BEARISH", "aligned"),
        # conflict: direcciones opuestas — la violación directa de REQ-1
        ("BUY", "BEARISH", "conflict"),
        ("SELL", "BULLISH", "conflict"),
        # neutral: ambos sin dirección (fuera del denominador de tasas)
        ("HOLD", "NEUTRAL", "neutral"),
        ("FLAT", "NEUTRAL", "neutral"),
        ("CLOSE", "NEUTRAL", "neutral"),
        # split: exactamente uno se abstiene (CLOSE/FLAT/HOLD = polaridad 0)
        ("BUY", "NEUTRAL", "split"),
        ("SELL", "NEUTRAL", "split"),
        ("HOLD", "BULLISH", "split"),
        ("CLOSE", "BEARISH", "split"),
        ("FLAT", "BULLISH", "split"),
    ])
    def test_vocab_matrix(self, ppo, llm, expected):
        assert pair_state(ppo, llm) == expected

    def test_unknown_vocab_is_explicit_not_guessed(self):
        """Vocabulario fuera de contrato → unknown explícito (nunca se infiere
        polaridad: p. ej. llm_bias 'ERROR' de los early-paths)."""
        assert pair_state("HOBBY", "BULLISH") == "unknown"
        assert pair_state("BUY", "ERROR") == "unknown"
        assert polarity("ERROR") is None
        assert polarity("buy") is None   # contrato en mayúsculas: sin normalizar


# ──────────────────────── D4: umbrales congelados a priori ────────────────────

class TestThresholdsFrozen:
    """Guardia anti-deriva: estos valores son los pre-registrados el
    2026-10-17 ANTES de la primera medición. Cambiarlos = nueva pre-registración."""

    def test_pre_registered_values(self):
        assert T28_WINDOW == 1000
        assert T28_MIN_ACTIVE == 50
        assert T28_AGREEMENT_MIN == 0.60
        assert T28_CONFLICT_MAX == 0.20
        assert T28_ENTROPY_MIN_BITS == 1.0


# ─────────────────────── D3/D4: aritmética del reporte ───────────────────────

def _rows(n, ppo, llm):
    return [(ppo, llm)] * n


class TestComputeReport:
    def test_perfectly_aligned_and_varied_passes(self):
        """50 BUY/BULLISH + 50 SELL/BEARISH: acuerdo total, H = 1 bit exacto
        (dos estados equiprobables) → las 4 condiciones en verde."""
        rows = _rows(50, "BUY", "BULLISH") + _rows(50, "SELL", "BEARISH")
        r = compute_report(rows)
        assert r["n_active"] == 100
        assert r["agreement_rate"] == 1.0
        assert r["conflict_rate"] == 0.0
        assert r["joint_entropy_bits"] == 1.0
        assert r["ok"] is True
        assert r["reasons"] == []

    def test_both_always_buy_fails_entropy(self):
        """El fallo trivial nominal de la acceptance: «los dos siempre dicen
        BUY» → acuerdo 100% pero H=0 → RECHAZADO por entropía."""
        r = compute_report(_rows(100, "BUY", "BULLISH"))
        assert r["agreement_rate"] == 1.0
        assert r["joint_entropy_bits"] == 0.0
        assert r["ok"] is False
        assert r["reasons"] == ["entropy_below_threshold"]

    def test_mixed_healthy_profile_passes(self):
        """60 aligned / 20 conflict (pares BUY↔BEARISH) / 20 split: en los
        umbrales exactos (agreement 0.60, conflict 0.20) → pasa; H ≈ 1.37 bits."""
        rows = (_rows(60, "BUY", "BULLISH") + _rows(20, "BUY", "BEARISH")
                + _rows(20, "HOLD", "BULLISH"))
        r = compute_report(rows)
        assert r["agreement_rate"] == 0.6
        assert r["conflict_rate"] == 0.2
        assert r["split_rate"] == 0.2
        assert r["joint_entropy_bits"] == pytest.approx(1.3710, abs=1e-3)
        assert r["ok"] is True

    def test_agreement_below_threshold(self):
        rows = (_rows(59, "BUY", "BULLISH") + _rows(20, "BUY", "BEARISH")
                + _rows(21, "HOLD", "BULLISH"))
        r = compute_report(rows)
        assert r["agreement_rate"] == 0.59
        assert r["ok"] is False
        assert r["reasons"] == ["agreement_below_threshold"]

    def test_conflict_above_threshold(self):
        rows = _rows(70, "BUY", "BULLISH") + _rows(21, "BUY", "BEARISH") + _rows(9, "BUY", "NEUTRAL")
        r = compute_report(rows)
        assert r["conflict_rate"] == 0.21
        assert r["ok"] is False
        assert r["reasons"] == ["conflict_above_threshold"]

    def test_neutrals_do_not_inflate_the_sample(self):
        """neutral-neutral no es evidencia: no entra en n_active ni en tasas."""
        rows = _rows(60, "HOLD", "NEUTRAL") + _rows(50, "BUY", "BULLISH")
        r = compute_report(rows)
        assert r["n_window"] == 110
        assert r["n_neutral"] == 60
        assert r["n_active"] == 50
        assert r["agreement_rate"] == 1.0   # solo los activos cuentan

    def test_unknown_rows_are_counted_and_excluded(self):
        rows = _rows(10, "HOBBY", "BULLISH") + _rows(50, "BUY", "BULLISH") + _rows(50, "SELL", "BEARISH")
        r = compute_report(rows)
        assert r["n_unknown"] == 10
        assert r["n_window"] == 110
        assert r["n_active"] == 100
        assert r["ok"] is True

    def test_insufficient_sample_first_reason_in_fixed_order(self):
        """Orden fijo de reasons (D4): insufficient_sample va primero."""
        r = compute_report(_rows(5, "SELL", "BULLISH"))
        assert r["sufficient"] is False
        assert r["reasons"] == [
            "insufficient_sample",
            "agreement_below_threshold",
            "conflict_above_threshold",
            "entropy_below_threshold",
        ]

    def test_empty_window_is_fully_declared(self):
        r = compute_report([])
        assert r["n_window"] == 0
        assert r["n_active"] == 0
        assert r["ok"] is False
        assert r["reasons"][0] == "insufficient_sample"

    def test_report_echoes_window_and_thresholds(self):
        r = compute_report([], window=250)
        assert r["window"] == 250
        assert r["thresholds"] == {
            "min_active": T28_MIN_ACTIVE,
            "agreement_min": T28_AGREEMENT_MIN,
            "conflict_max": T28_CONFLICT_MAX,
            "entropy_min_bits": T28_ENTROPY_MIN_BITS,
        }

    def test_joint_entropy_hand_values(self):
        assert joint_entropy_bits([]) == 0.0
        assert joint_entropy_bits([(1, 1)] * 7) == 0.0
        assert joint_entropy_bits([(1, 1), (-1, -1)]) == 1.0
        # 1/4, 1/4, 1/2 → 0.5 + 0.5 + 0.5 = 1.5 bits
        assert joint_entropy_bits([(1, 1), (1, 1), (-1, -1), (0, 1)]) == pytest.approx(1.5)


# ─────────────────────────── D2: loader de la ventana ─────────────────────────

class _FakePool:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    async def fetch(self, query, *args):
        self.calls.append((query, args))
        return self.rows


class TestLoadRecentPairs:
    @pytest.mark.asyncio
    async def test_filters_and_limit_are_in_the_query(self):
        pool = _FakePool([{"ppo": "BUY", "llm": "BULLISH"},
                          {"ppo": "SELL", "llm": "BEARISH"}])
        rows = await load_recent_pairs(pool, "EURUSD")
        assert rows == [("BUY", "BULLISH"), ("SELL", "BEARISH")]
        query, args = pool.calls[0]
        assert "event = 'ai_predict_cycle'" in query
        assert "'ppo_direction' IS NOT NULL" in query      # sin historial pre-T-28
        assert "'qwen_skipped')::boolean" in query         # D2: skip LR-1 excluido
        assert "'qwen_degraded')::boolean" in query        # D2: sin juicio LLM excluido
        assert "LIMIT $2" in query
        assert args == ("EURUSD", T28_WINDOW)

    @pytest.mark.asyncio
    async def test_uninitialized_pool_propagates(self):
        """Sin pool el error se propaga (alignment_gate lo convierte en
        fail-closed; el router lo contiene con error explícito)."""
        with patch("db.connection.get_pool", side_effect=RuntimeError("DB pool not initialized")):
            with pytest.raises(RuntimeError):
                await load_recent_pairs(None, "EURUSD")


# ───────────────────── D6: gate de alineación (fail-closed) ──────────────────

class TestAlignmentGate:
    @pytest.mark.asyncio
    async def test_gate_passes_with_aligned_window(self, monkeypatch):
        rows = _rows(50, "BUY", "BULLISH") + _rows(50, "SELL", "BEARISH")
        monkeypatch.setattr(
            "services.alignment_metric.load_recent_pairs",
            AsyncMock(return_value=rows),
        )
        r = await alignment_gate("EURUSD")
        assert r["ok"] is True
        assert r["n_active"] == 100

    @pytest.mark.asyncio
    async def test_gate_fail_closed_on_loader_error(self, monkeypatch):
        """Un error de cómputo NUNCA aprueba en silencio: ok=false con la causa."""
        monkeypatch.setattr(
            "services.alignment_metric.load_recent_pairs",
            AsyncMock(side_effect=RuntimeError("connection refused")),
        )
        r = await alignment_gate("EURUSD")
        assert r["ok"] is False
        assert r["reasons"][0].startswith("error:RuntimeError:")

    @pytest.mark.asyncio
    async def test_rolling_alignment_matches_compute(self, monkeypatch):
        rows = _rows(50, "BUY", "BULLISH") + _rows(50, "SELL", "BEARISH")
        monkeypatch.setattr(
            "services.alignment_metric.load_recent_pairs",
            AsyncMock(return_value=rows),
        )
        r = await rolling_alignment("EURUSD")
        assert r["ok"] is True
        assert r["window"] == T28_WINDOW


# ──────────────────── (b): registro por ciclo en audit ───────────────────────

def _post_predict(monkeypatch, *, rolling=None, rolling_exc=None):
    model = _mock_ppo_model()
    model.predict.return_value = (np.array(ACTION_BUY, dtype=np.float32), None)
    monkeypatch.setattr(qwen_client, "_QWEN_STATE", QwenHealthState())
    rolling_mock = AsyncMock(
        return_value=rolling if rolling is not None else {"ok": False}
    )
    if rolling_exc is not None:
        rolling_mock = AsyncMock(side_effect=rolling_exc)
    with patch("main.init_pool", AsyncMock()), \
         patch("main.close_pool", AsyncMock()), \
         patch("routers.ai.is_news_blackout", AsyncMock(return_value=(False, None))), \
         patch("routers.ai.fetch_bars_since_close", AsyncMock(return_value=None)), \
         patch("routers.ai.get_pool", return_value=MagicMock(execute=AsyncMock())), \
         patch("routers.ai.PPO.load", return_value=model), \
         patch("services.news_scraper.get_macro_news", AsyncMock(return_value="No macro news")), \
         patch("routers.ai.rolling_alignment", rolling_mock), \
         patch("routers.ai.log_to_audit", AsyncMock()) as audit_mock, \
         patch("httpx.AsyncClient", FakeAsyncClient):
        with TestClient(app) as client:
            response = client.post(
                "/api/v1/ai/predict",
                json={"symbol": "EURUSD", "timeframe": "H1", "position": 0},
                headers={"x-internal-token": "test-token", "x-cycle-id": "t28"},
            )
    return response, audit_mock, rolling_mock


class TestPredictAuditRegistration:
    """Acceptance (b): la alineación se registra por ciclo en audit."""

    def test_cycle_logs_raw_ppo_direction_and_pair_state(self, monkeypatch):
        rolling = {"ok": True, "n_active": 60, "agreement_rate": 0.7}
        response, audit_mock, rolling_mock = _post_predict(monkeypatch, rolling=rolling)
        assert response.status_code == 200, response.text

        data = audit_mock.await_args.args[1]
        # D1: la decisión CRUDA del PPO (BUY en este ciclo, sin posición)
        assert data["ppo_direction"] == "BUY"
        # El par se clasifica con las MISMAS funciones del módulo (fuente única)
        assert data["pair_state"] == pair_state(data["ppo_direction"], data["llm_bias"])
        # D5: snapshot de la ventana PREVIA, patrón min_entry_confidence
        assert data["alignment_rolling"] == rolling
        rolling_mock.assert_awaited_once_with("EURUSD")

    def test_rolling_failure_is_declared_not_silent(self, monkeypatch):
        """Capa observacional: su fallo no tumba el ciclo y el error queda
        EXPLÍCITO en el payload (nada de claves ausentes ni pasos en silencio)."""
        response, audit_mock, _ = _post_predict(
            monkeypatch, rolling_exc=RuntimeError("boom")
        )
        assert response.status_code == 200, response.text
        data = audit_mock.await_args.args[1]
        assert data["alignment_rolling"]["ok"] is False
        assert "RuntimeError: boom" in data["alignment_rolling"]["error"]
        assert data["ppo_direction"] == "BUY"   # el registro crudo no depende del rolling


# ──────────── (c): condición del gate de retrain en _do_retrain_core ─────────

class TestRetrainGateT28:
    """Rama T-28 en el pipeline de retrain (wf OK + alineación)."""

    @staticmethod
    def _t28_report(ok=False, reasons=("conflict_above_threshold",), **over):
        report = {
            "window": T28_WINDOW, "n_window": 300, "n_neutral": 50, "n_unknown": 0,
            "n_active": 250, "n_aligned": 150, "n_conflict": 75, "n_split": 25,
            "agreement_rate": 0.6, "conflict_rate": 0.3, "split_rate": 0.1,
            "joint_entropy_bits": 1.3, "sufficient": True, "ok": ok,
            "reasons": list(reasons),
        }
        report.update(over)
        return report

    @pytest.mark.asyncio
    async def test_t28_failure_rejects_even_with_wf_ok(self, tmp_path, monkeypatch):
        from tests.test_auto_retrain import TestDoRetrainPromotion

        ar, state, model_path, record_promo, reload_mock = (
            TestDoRetrainPromotion._patch_common(monkeypatch, tmp_path)
        )
        monkeypatch.setattr(ar, "_evaluate_model", TestDoRetrainPromotion._fake_metrics)
        monkeypatch.setattr(
            ar, "alignment_gate",
            AsyncMock(return_value=self._t28_report()),
        )
        finish = AsyncMock()
        monkeypatch.setattr("services.experiment_tracker.finish_experiment", finish)

        await ar._do_retrain()

        # Rechazo con la causa T-28, bundle intacto, ancla anti-tormenta
        assert not os.path.exists(str(tmp_path / "ppo_trading_bot_v3.manifest.json"))
        record_promo.assert_not_awaited()
        reload_mock.assert_not_called()
        assert state.last_retrain_time > 1.0

        finish.assert_awaited_once()
        assert finish.await_args.args == (42, "rejected")
        assert finish.await_args.kwargs["gate_reason"] == (
            "t28_alignment:conflict_above_threshold"
        )

        ar._log_retrain_event.assert_awaited_once()
        assert ar._log_retrain_event.await_args.args[0] == "retrain_rejected"
        payload = ar._log_retrain_event.await_args.args[1]
        assert payload["reason"] == "t28_alignment:conflict_above_threshold"
        assert payload["t28_alignment"]["ok"] is False

        # El artefacto del candidato congela el porqué (auditable sin promover)
        art = tmp_path / "retrain_artifacts" / "exp42" / "report.json"
        frozen = json.loads(art.read_text(encoding="utf-8"))
        assert frozen["t28_alignment"]["ok"] is False
        assert frozen["gate"]["ok"] is True      # wf OK: el rechazo es del T-28

    @pytest.mark.asyncio
    async def test_insufficient_sample_is_not_promotable(self, tmp_path, monkeypatch):
        """Fail-closed (D6): sin muestra suficiente NO se promueve — la causa
        va completa (todas las condiciones fallidas, orden fijo)."""
        from tests.test_auto_retrain import TestDoRetrainPromotion

        ar, *_ = TestDoRetrainPromotion._patch_common(monkeypatch, tmp_path)
        monkeypatch.setattr(ar, "_evaluate_model", TestDoRetrainPromotion._fake_metrics)
        monkeypatch.setattr(ar, "alignment_gate", AsyncMock(return_value=self._t28_report(
            reasons=("insufficient_sample", "entropy_below_threshold"),
            n_active=12, sufficient=False,
        )))

        await ar._do_retrain()

        assert ar._log_retrain_event.await_args.args[0] == "retrain_rejected"
        payload = ar._log_retrain_event.await_args.args[1]
        assert payload["reason"] == (
            "t28_alignment:insufficient_sample,entropy_below_threshold"
        )

    @pytest.mark.asyncio
    async def test_bootstrap_no_bundle_and_zero_rows_is_na_and_promotes(
        self, tmp_path, monkeypatch,
    ):
        """T-28 opción A (orden Félix 2026-10-17): bootstrap de mundo nuevo.

        Sin bundle previo (el modelo fresco sólo se guarda al promover) y con
        0 ciclos auditados → la métrica 7 es N/A con `status` EXPLÍCITO (un
        N/A jamás se confunde con un pase de la métrica) y el gate cae sólo
        sobre las métricas 1-6 → promoción permitida. El caso contrapuesto
        (sin bundle pero CON filas → rechazo estricto) lo cubre
        test_t28_failure_rejects_even_with_wf_ok (n_window=300).
        """
        from tests.test_auto_retrain import TestDoRetrainPromotion
        from services.model_registry import validate_bundle

        ar, state, model_path, record_promo, reload_mock = (
            TestDoRetrainPromotion._patch_common(monkeypatch, tmp_path)
        )
        monkeypatch.setattr(ar, "_evaluate_model", TestDoRetrainPromotion._fake_metrics)
        # Lo que el gate real devuelve para un par nunca servido: 0 filas.
        monkeypatch.setattr(ar, "alignment_gate", AsyncMock(return_value={
            "window": T28_WINDOW, "n_window": 0, "n_neutral": 0, "n_unknown": 0,
            "n_active": 0, "n_aligned": 0, "n_conflict": 0, "n_split": 0,
            "agreement_rate": 0.0, "conflict_rate": 0.0, "split_rate": 0.0,
            "joint_entropy_bits": 0.0, "sufficient": False, "ok": False,
            "reasons": ["insufficient_sample"],
        }))

        # GBPUSD está listado (aunque enabled=false): entrenar el canario
        # ANTES de activarlo es el flujo P3-3 — el gate N/A es su desbloqueo.
        await ar._do_retrain(symbol="GBPUSD")

        record_promo.assert_awaited_once()
        reload_mock.assert_called()
        assert state.last_retrain_time > 1.0

        # El N/A queda certificado con SU status en el manifest del bundle
        manifest = validate_bundle(model_path)
        t28 = manifest["extra"]["t28_alignment"]
        assert t28["ok"] is True
        assert t28["status"] == "not_applicable_bootstrap"
        assert t28["reasons"] == ["not_applicable_no_prior_bundle"]
        assert t28["n_window"] == 0 and t28["n_active"] == 0

        # …y en el evento de promoción (auditable, inequívoco)
        ar._log_retrain_event.assert_awaited_once()
        assert ar._log_retrain_event.await_args.args[0] == "retrain_promoted"
        payload = ar._log_retrain_event.await_args.args[1]
        assert payload["t28_alignment"]["status"] == "not_applicable_bootstrap"

    @pytest.mark.asyncio
    async def test_second_retrain_applies_strict_gate_even_with_zero_rows(
        self, tmp_path, monkeypatch,
    ):
        """Transición N/A → estricto (opción A): el 1º retrain promueve con
        N/A (mundo recién creado, sin bundle y sin ciclos); el 2º NO goza de
        N/A aunque sigan 0 filas — hay bundle previo en disco ⇒ fail-closed
        estricto. Un N/A jamás se vuelve costumbre."""
        from tests.test_auto_retrain import TestDoRetrainPromotion

        ar, state, model_path, record_promo, reload_mock = (
            TestDoRetrainPromotion._patch_common(monkeypatch, tmp_path)
        )
        monkeypatch.setattr(ar, "_evaluate_model", TestDoRetrainPromotion._fake_metrics)
        zero_rows = {
            "window": T28_WINDOW, "n_window": 0, "n_neutral": 0, "n_unknown": 0,
            "n_active": 0, "n_aligned": 0, "n_conflict": 0, "n_split": 0,
            "agreement_rate": 0.0, "conflict_rate": 0.0, "split_rate": 0.0,
            "joint_entropy_bits": 0.0, "sufficient": False, "ok": False,
            "reasons": ["insufficient_sample"],
        }
        monkeypatch.setattr(ar, "alignment_gate", AsyncMock(return_value=zero_rows))
        # El wf compararía candidato==baseline (métricas falsas deterministas)
        # y rechazaría ANTES de T-28: este test aísla la rama 7, wf tiene los
        # suyos (walk_forward.py intocado).
        monkeypatch.setattr(
            ar, "gate_walk_forward",
            MagicMock(return_value=SimpleNamespace(
                ok=True, reason="fake-wf-ok", details={"checks": {}},
            )),
        )

        # 1er retrain: bootstrap (sin bundle + 0 filas) → N/A → promueve
        await ar._do_retrain(symbol="GBPUSD")
        record_promo.assert_awaited_once()
        assert os.path.exists(model_path)  # bundle REAL en disco para el 2º

        # 2º retrain: bundle previo + aún 0 filas → estricto, rechazado
        record_promo.reset_mock()
        await ar._do_retrain(symbol="GBPUSD")
        record_promo.assert_not_awaited()
        assert ar._log_retrain_event.await_args.args[0] == "retrain_rejected"
        payload = ar._log_retrain_event.await_args.args[1]
        assert payload["reason"] == "t28_alignment:insufficient_sample"
        assert "status" not in payload["t28_alignment"]  # jamás un N/A disfrazado

    @pytest.mark.asyncio
    async def test_aligned_pair_promotes_and_certifies_t28(self, tmp_path, monkeypatch):
        """wf OK + T-28 OK → promoción; la condición queda certificada en el
        manifest y en el evento (el bundle lleva su prueba de alineación)."""
        from tests.test_auto_retrain import TestDoRetrainPromotion
        from services.model_registry import validate_bundle

        ar, state, model_path, record_promo, reload_mock = (
            TestDoRetrainPromotion._patch_common(monkeypatch, tmp_path)
        )
        monkeypatch.setattr(ar, "_evaluate_model", TestDoRetrainPromotion._fake_metrics)
        monkeypatch.setattr(ar, "alignment_gate", AsyncMock(return_value=self._t28_report(
            ok=True, reasons=[], agreement_rate=0.78, conflict_rate=0.09,
        )))

        await ar._do_retrain()

        manifest = validate_bundle(model_path)
        assert manifest["extra"]["t28_alignment"]["ok"] is True
        assert manifest["extra"]["t28_alignment"]["agreement_rate"] == 0.78
        record_promo.assert_awaited_once()
        assert ar._log_retrain_event.await_args.args[0] == "retrain_promoted"
        payload = ar._log_retrain_event.await_args.args[1]
        assert payload["t28_alignment"]["ok"] is True
