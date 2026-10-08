"""
Fase 2 / D-5: CAPA ESTRATEGA SHADOW — deliberación por ciclo auditable.

Contrato (plan 2026-09-30 §4-§6):
- Estado narrativo (§4.1): último cierre con regla 3 €, tesis (trade_insights),
  racha, bars_since_close y movimiento desde el cierre.
- Criterios C1–C8 (§4.2): config versionada, **OFF por defecto** — activados
  uno a PR con evidencia de replay aprobada (§6). Hoy C2 activo (v1.1.0,
  aprobación Félix 2026-10-01); el resto OFF.
- Verdict ENTER/WAIT/NO_CANDIDATE registrado en `deliberation`; la capa
  SHADOW tiene CERO poder de decisión (la respuesta nunca cambia por el
  estratega — `shadow: true`), activar un criterio solo cambia el verdict.
- Fail-safe: criterio activo solo pasa con `pass is True` (duda → ESPERAR).
- Vetos existentes integrados en la misma traza (`vetoes.fired` = lo que
  decidió `_veto_reason`, la autoridad de la cadena).
- Errores sin silencios: `deliberate()` propaga; la contención vive en la
  frontera del router (verdict=ERROR + WARNING) y está testeada aquí.
"""
import copy
import logging
from collections import deque
from contextlib import ExitStack
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from main import app
from routers import ai
from services import qwen_client, strategist
from services.qwen_client import QwenHealthState
from tests.test_e2e_ai_predict import FakeAsyncClient
from tests.test_qwen_semantics import _mock_ppo_model

ACTION_BUY = [0.80, 0.18, 0.15, 0.15]        # candidato BUY sin posición
ACTION_FLIP_SELL = [-0.80, 0.18, 0.15, 0.15]  # flip SELL con posición LONG

QWEN_OK = {
    "ok": True,
    "quality": 8.5,
    "reason": "strong setup",
    "bias": "BULLISH",
    "confidence": 1.1,
    "confidence_modifier": 1.1,
    "effective_prob": 0.72,
    "regime": "TRENDING",
}

NARRATIVE = {
    "last_exit": {
        "exit_time": "2024-01-01T06:00:00+00:00",
        "exit_reason": "tp",
        "pnl": 21.17,
        "direction": "LONG",
        "is_win": True,
    },
    "thesis": {"regime": "TRENDING", "quality_score": 8.0, "confidence": 0.75,
               "insight": "continuación alcista"},
    "consecutive_losses": 1,
    "bars_since_close": 3,
    "price_move_since_close_pips": 25.0,
    "limits": {"equity": 1000.0, "daily_start_equity": 1000.0, "peak_equity": 1010.0,
               "daily_loss_limit_pct": 5.0, "max_drawdown_block_pct": None},
}

_DEFAULTS = dict(
    symbol="EURUSD",
    candidate="BUY",
    veto_reason=None,
    qwen=QWEN_OK,
    qwen_degraded=False,
    direction_head=0.8,
    lots=0.05,
    sizing_below_min=False,
    env_position=0,
    df=pd.DataFrame(),
    current_price=1.1000,
    current_atr=0.0012,
    pip_size=0.0001,
    equity=1000.0,
    quality_threshold=5.0,
)


@pytest.fixture(autouse=True)
def clean_spread_samples():
    """Ventana de spread a 0 entre tests (estado en memoria del módulo)."""
    strategist._SPREAD_SAMPLES.clear()
    yield
    strategist._SPREAD_SAMPLES.clear()


def _narr(**over):
    n = copy.deepcopy(NARRATIVE)
    n.update(over)
    return n


async def _deliberate(narrative=None, *, spread=0.00012, fetch_side_effect=None, **over):
    """`spread` en unidades de PRECIO (ask−bid, como mt5_client.get_spread);
    el módulo lo convierte a pips con pip_size."""
    kwargs = {**_DEFAULTS, **over}
    narr = NARRATIVE if narrative is None else narrative
    with patch.object(strategist, "fetch_narrative", AsyncMock(return_value=narr)) as fn, \
         patch.object(strategist, "get_spread", AsyncMock(return_value=spread)):
        if fetch_side_effect is not None:
            fn.side_effect = fetch_side_effect
        return await strategist.deliberate(**kwargs)


def _activate(monkeypatch, cid, **over):
    """Activa UN criterio en la config temporal del test (monkeypatch la restaura)."""
    monkeypatch.setitem(strategist.CRITERIA, cid, {**strategist.CRITERIA[cid], "active": True, **over})


def _c(d, cid):
    return d["criteria"][cid]


# ─── Config versionada, OFF por defecto (C2 activo v1.1.0) ────────────────────

class TestConfigShadow:
    def test_only_replay_approved_criteria_on_and_versioned(self):
        """V1.1.0: solo C2 activo (replay aprobado 2026-10-01); el resto OFF."""
        assert strategist.STRATEGIST_VERSION == "1.1.0"
        assert list(strategist.CRITERIA) == [
            "C1_direction", "C2_executable_size", "C3_quality", "C4_thesis_regime",
            "C5_change_since_exit", "C6_post_loss_evidence", "C7_costs_vs_edge",
            "C8_late_join",
        ]
        for cid, cfg in strategist.CRITERIA.items():
            expected = cid == "C2_executable_size"
            assert cfg["active"] is expected, (
                f"{cid}: active={cfg['active']}, se esperaba {expected}"
            )
            assert isinstance(cfg["justified_by"], (str, type(None))), cid
            assert cfg["metric"] and cfg["notes"], cid
        # Un criterio activo SIN evidencia de replay está prohibido (§6.5)
        assert strategist.CRITERIA["C2_executable_size"]["justified_by"]

    def test_unjustified_thresholds_are_marked(self):
        """Ningún umbral placeholder finge evidencia: solo C2 (contrato broker)
        y C7 (k=2 del plan) tienen justified_by; el resto espera replay (§6)."""
        unjustified = [cid for cid, cfg in strategist.CRITERIA.items()
                       if cfg["justified_by"] is None]
        assert set(unjustified) == {
            "C1_direction", "C3_quality", "C4_thesis_regime",
            "C5_change_since_exit", "C6_post_loss_evidence", "C8_late_join",
        }
        assert strategist.CRITERIA["C2_executable_size"]["justified_by"]
        assert strategist.CRITERIA["C7_costs_vs_edge"]["justified_by"]


# ─── Verdict (ENTER / WAIT / NO_CANDIDATE) ────────────────────────────────────

class TestVerdict:
    @pytest.mark.asyncio
    async def test_clean_candidate_enters(self):
        d = await _deliberate()   # lots 0.05 ≥ min_volume → C2 (único activo) pasa
        assert d["verdict"] == "ENTER"
        assert d["blocked_by"] == []
        assert _c(d, "C2_executable_size")["pass"] is True
        assert d["shadow"] is True
        assert d["config_version"] == strategist.STRATEGIST_VERSION
        assert len(d["criteria"]) == 8
        assert d["narrative"] == NARRATIVE

    @pytest.mark.asyncio
    async def test_model_hold_is_no_candidate_but_still_traced(self):
        d = await _deliberate(candidate="HOLD", direction_head=0.02)
        assert d["verdict"] == "NO_CANDIDATE"
        assert d["blocked_by"] == []
        # Los criterios se EVALÚAN igual (distribuciones para el replay §6)
        assert _c(d, "C1_direction")["pass"] is False       # |0.02| < 0.25
        assert _c(d, "C4_thesis_regime")["pass"] is None    # sin candidato: no aplica

    @pytest.mark.asyncio
    async def test_existing_veto_waits_and_is_named(self):
        d = await _deliberate(veto_reason="position_veto")
        assert d["verdict"] == "WAIT"
        assert d["blocked_by"] == ["veto:position_veto"]

    @pytest.mark.asyncio
    async def test_veto_and_failing_active_criterion_both_listed(self, monkeypatch):
        _activate(monkeypatch, "C3_quality")
        d = await _deliberate(veto_reason="quality_veto", qwen={**QWEN_OK, "quality": 4.0})
        assert d["verdict"] == "WAIT"
        assert d["blocked_by"] == ["veto:quality_veto", "C3_quality"]
        assert _c(d, "C3_quality")["pass"] is False

    @pytest.mark.asyncio
    async def test_active_criterion_with_pass_none_is_wait_fail_safe(self, monkeypatch):
        """Duda → ESPERAR: un criterio activo solo pasa con pass is True."""
        _activate(monkeypatch, "C4_thesis_regime")
        d = await _deliberate(qwen={**QWEN_OK, "bias": "NEUTRAL"})
        assert _c(d, "C4_thesis_regime")["pass"] is None
        assert d["verdict"] == "WAIT"
        assert d["blocked_by"] == ["C4_thesis_regime"]

    @pytest.mark.asyncio
    async def test_active_passing_criterion_does_not_block(self, monkeypatch):
        _activate(monkeypatch, "C3_quality")   # quality 8.5 ≥ 7.0
        d = await _deliberate()
        assert _c(d, "C3_quality")["pass"] is True
        assert d["verdict"] == "ENTER"


# ─── Vetos existentes integrados en la traza ──────────────────────────────────

class TestVetoesSection:
    @pytest.mark.asyncio
    async def test_veto_inputs_and_authority(self):
        d = await _deliberate(
            veto_reason="quality_veto",
            qwen={**QWEN_OK, "regime": "RANGING", "bias": "BEARISH", "quality": 2.0},
            env_position=1,
            lots=0.005,
            sizing_below_min=True,
        )
        v = d["vetoes"]
        assert v["fired"] == "quality_veto"           # autoridad: _veto_reason
        assert v["position_open"] is True
        assert v["news_blackout"] is False            # solo llega quien PASÓ el gate
        assert v["ranging"] is True
        assert v["quality_below"] is True             # 2.0 < QUALITY_THRESHOLD 5.0
        assert v["bias_opposed"] is True              # BUY vs BEARISH
        assert v["sizing_below_min"] is True

    @pytest.mark.asyncio
    async def test_clean_cycle_has_no_veto_flags(self):
        d = await _deliberate()
        v = d["vetoes"]
        assert v == {
            "fired": None,
            "position_open": False,
            "news_blackout": False,
            "ranging": False,
            "quality_below": False,
            "bias_opposed": False,
            "sizing_below_min": False,
        }


# ─── Criterios C1–C8 (evaluación) ─────────────────────────────────────────────

class TestCriteria:
    @pytest.mark.asyncio
    async def test_c1_direction_vs_threshold(self):
        weak = await _deliberate(direction_head=0.07)
        strong = await _deliberate(direction_head=0.80)
        assert _c(weak, "C1_direction")["pass"] is False
        assert _c(weak, "C1_direction")["value"] == pytest.approx(0.07)
        assert _c(strong, "C1_direction")["pass"] is True

    @pytest.mark.asyncio
    async def test_c2_executable_size_against_broker_min(self):
        tiny = await _deliberate(lots=0.005)   # < settings.min_volume (0.01)
        ok = await _deliberate(lots=0.05)
        assert _c(tiny, "C2_executable_size")["pass"] is False
        assert _c(tiny, "C2_executable_size")["threshold"] == pytest.approx(0.01)
        assert _c(ok, "C2_executable_size")["pass"] is True
        # C2 ACTIVO (v1.1.0): el fallo entra en el verdict (aunque la capa
        # sigue shadow — la respuesta no cambia, lo comprueba el router)
        assert tiny["verdict"] == "WAIT"
        assert tiny["blocked_by"] == ["C2_executable_size"]
        assert ok["verdict"] == "ENTER"

    @pytest.mark.asyncio
    async def test_c3_quality_uses_its_own_threshold_not_the_veto(self):
        d = await _deliberate(qwen={**QWEN_OK, "quality": 6.0})
        c3 = _c(d, "C3_quality")
        assert c3["pass"] is False          # 6.0 < 7.0 (C3) aunque ≥ 5.0 (veto)
        assert c3["threshold"] == 7.0

    @pytest.mark.asyncio
    async def test_c4_coherence_rules(self):
        ranging = await _deliberate(qwen={**QWEN_OK, "regime": "RANGING"})
        assert _c(ranging, "C4_thesis_regime")["pass"] is False

        opposed = await _deliberate(qwen={**QWEN_OK, "bias": "BEARISH"})
        assert _c(opposed, "C4_thesis_regime")["pass"] is False

        neutral = await _deliberate(qwen={**QWEN_OK, "bias": "NEUTRAL"})
        assert _c(neutral, "C4_thesis_regime")["pass"] is None

        degraded = await _deliberate(qwen_degraded=True, qwen={**QWEN_OK, "quality": 0.0, "bias": "NEUTRAL"})
        assert _c(degraded, "C4_thesis_regime")["pass"] is None
        assert "degradado" in _c(degraded, "C4_thesis_regime")["detail"]

        coherent = await _deliberate()
        assert _c(coherent, "C4_thesis_regime")["pass"] is True

    @pytest.mark.asyncio
    async def test_c5_scoped_to_same_direction_reentry(self):
        # Misma dirección (BUY tras LONG) con movimiento ≥ 10 pips → pasa
        same = await _deliberate()
        assert _c(same, "C5_change_since_exit")["pass"] is True

        # Misma dirección pero movimiento insuficiente → no pasa
        small = await _deliberate(narrative=_narr(price_move_since_close_pips=4.0))
        assert _c(small, "C5_change_since_exit")["pass"] is False

        # Dirección contraria → el criterio no aplica (no evaluable, no "pasa")
        opp = await _deliberate(candidate="SELL")
        assert _c(opp, "C5_change_since_exit")["pass"] is None
        assert "dirección distinta" in _c(opp, "C5_change_since_exit")["detail"]

        # Sin salida previa / fuera de ventana → no evaluable
        none_exit = await _deliberate(narrative=_narr(last_exit=None))
        assert _c(none_exit, "C5_change_since_exit")["pass"] is None

        out_win = await _deliberate(narrative=_narr(price_move_since_close_pips=None))
        assert _c(out_win, "C5_change_since_exit")["pass"] is None
        assert "ventana" in _c(out_win, "C5_change_since_exit")["detail"]

    @pytest.mark.asyncio
    async def test_c6_post_loss_evidence(self):
        streak_ok = await _deliberate()   # racha 1 + quality 8.5 ≥ 8.0
        assert _c(streak_ok, "C6_post_loss_evidence")["pass"] is True
        assert _c(streak_ok, "C6_post_loss_evidence")["value"] == 1

        weak = await _deliberate(qwen={**QWEN_OK, "quality": 6.5})
        assert _c(weak, "C6_post_loss_evidence")["pass"] is False

        no_streak = await _deliberate(narrative=_narr(consecutive_losses=0))
        assert _c(no_streak, "C6_post_loss_evidence")["pass"] is True

        unknown = await _deliberate(narrative=_narr(consecutive_losses=None))
        assert _c(unknown, "C6_post_loss_evidence")["pass"] is None

    @pytest.mark.asyncio
    async def test_c7_needs_live_samples_then_compares_mu_2sigma(self):
        # Sin historia → no evaluable (y NO cuenta como "pasa")
        fresh = await _deliberate(spread=0.00012)   # precio ask−bid → 1.2 pips
        c7 = _c(fresh, "C7_costs_vs_edge")
        assert c7["pass"] is None
        assert "sin historia" in c7["detail"]

        # ≥30 muestras → μ+2σ y comparación real
        strategist._SPREAD_SAMPLES["EURUSD"] = deque([1.0] * 30, maxlen=strategist.SPREAD_WINDOW)
        normal = await _deliberate(spread=0.0001)   # 1.0 pip
        c7 = _c(normal, "C7_costs_vs_edge")
        stats = strategist._spread_stats("EURUSD")
        assert stats["threshold"] == pytest.approx(1.0)     # σ=0 sobre 31×1.0
        assert c7["pass"] is True
        assert c7["value"] == pytest.approx(1.0)
        assert c7["threshold"] == stats["threshold"]

        spike = await _deliberate(spread=0.0003)    # 3.0 pips ≫ μ+2σ
        assert _c(spike, "C7_costs_vs_edge")["pass"] is False

    @pytest.mark.asyncio
    async def test_c7_spread_failure_degrades_only_c7(self):
        with patch.object(strategist, "fetch_narrative", AsyncMock(return_value=NARRATIVE)), \
             patch.object(strategist, "get_spread",
                          AsyncMock(side_effect=httpx.ConnectError("precio caído"))):
            d = await strategist.deliberate(**_DEFAULTS)
        c7 = _c(d, "C7_costs_vs_edge")
        assert c7["pass"] is None
        assert "ConnectError" in c7["error"]
        # El resto de la traza se emite intacta: un endpoint no la borra
        assert d["verdict"] == "ENTER"
        assert _c(d, "C3_quality")["pass"] is True
        assert d["narrative"] == NARRATIVE

    @pytest.mark.asyncio
    async def test_c8_late_join_normalized_to_atr(self):
        # atr=0.0012 → 12 pips: +15 pips a favor = 1.25 ATR → tarde
        late = await _deliberate(narrative=_narr(price_move_since_close_pips=15.0))
        c8 = _c(late, "C8_late_join")
        assert c8["pass"] is False
        assert c8["value"] == pytest.approx(1.25)

        # Movimiento en contra (−15 pips a favor de BUY) → no es llegar tarde
        fresh = await _deliberate(narrative=_narr(price_move_since_close_pips=-15.0))
        assert _c(fresh, "C8_late_join")["pass"] is True

        no_exit = await _deliberate(narrative=_narr(last_exit=None))
        assert _c(no_exit, "C8_late_join")["pass"] is None

    @pytest.mark.asyncio
    async def test_spread_samples_isolated_per_pair(self):
        strategist._SPREAD_SAMPLES["EURUSD"] = deque([1.0] * 30, maxlen=strategist.SPREAD_WINDOW)
        d = await _deliberate(symbol="GBPUSD")
        assert _c(d, "C7_costs_vs_edge")["pass"] is None
        assert strategist._spread_stats("EURUSD")["samples"] == 30
        assert strategist._spread_stats("GBPUSD")["samples"] == 1  # solo la suya

    @pytest.mark.asyncio
    async def test_narrative_failure_propagates(self):
        """La contención NO está aquí: el error sube a la frontera (router)."""
        with patch.object(strategist, "fetch_narrative",
                          AsyncMock(side_effect=RuntimeError("db caida"))):
            with pytest.raises(RuntimeError, match="db caida"):
                await strategist.deliberate(**_DEFAULTS)


# ─── Estado narrativo (fetch_narrative) ───────────────────────────────────────

def _pool(fetchrow):
    pool = MagicMock()
    pool.fetchrow = AsyncMock(return_value=fetchrow)
    return pool


_ROW = {
    "id": 7,
    "exit_time": datetime(2024, 1, 1, 6, 0, tzinfo=timezone.utc),
    "exit_reason": "tp",
    "pnl": Decimal("21.17"),
    "direction": "LONG",
    "regime": "TRENDING",
    "quality_score": Decimal("8.0"),
    "qwen_confidence": Decimal("0.75"),
    "qwen_insight": "patrón de continuación " * 30,
}


class TestFetchNarrative:
    def _df(self):
        return pd.DataFrame({
            "time": [f"2024-01-01T{h:02d}:00:00Z" for h in range(10)],
            "close": [round(1.1000 + i * 0.0005, 5) for i in range(10)],
        })

    @pytest.mark.asyncio
    async def test_last_close_with_3eur_rule_and_thesis(self):
        with patch.object(strategist, "get_pool", return_value=_pool(_ROW)):
            n = await strategist.fetch_narrative(
                "ZZZUSD", df=self._df(), current_price=1.1045, pip_size=0.0001, equity=500.0,
            )
        # Regla 3 €: 21.17 ≥ 3 → win
        assert n["last_exit"]["is_win"] is True
        assert n["last_exit"]["pnl"] == pytest.approx(21.17)
        assert n["last_exit"]["exit_time"] == "2024-01-01T06:00:00+00:00"
        # Tesis unida por trade_id y truncada
        assert n["thesis"]["quality_score"] == pytest.approx(8.0)
        assert len(n["thesis"]["insight"]) <= 280
        # Ventana: salida 06:00, ventana 00:00–09:00 → 3 velas, +15 pips
        assert n["bars_since_close"] == 3
        assert n["price_move_since_close_pips"] == pytest.approx(15.0)
        # ZZZUSD sin guardián → racha None honesta, nunca 0 inventado
        assert n["consecutive_losses"] is None
        assert n["limits"]["equity"] == pytest.approx(500.0)

    @pytest.mark.asyncio
    async def test_below_3eur_close_is_a_loss(self):
        row = {**_ROW, "pnl": Decimal("2.99")}
        with patch.object(strategist, "get_pool", return_value=_pool(row)):
            n = await strategist.fetch_narrative(
                "ZZZUSD", df=self._df(), current_price=1.1045, pip_size=0.0001, equity=500.0,
            )
        assert n["last_exit"]["is_win"] is False   # 2.99 € < 3 € → pérdida

    @pytest.mark.asyncio
    async def test_no_closes_yet_is_none_not_default(self):
        with patch.object(strategist, "get_pool", return_value=_pool(None)):
            n = await strategist.fetch_narrative(
                "ZZZUSD", df=self._df(), current_price=1.1045, pip_size=0.0001, equity=500.0,
            )
        assert n["last_exit"] is None
        assert n["thesis"] is None
        assert n["bars_since_close"] is None
        assert n["price_move_since_close_pips"] is None


# ─── Matemática de ventana (bars/move desde la salida) ────────────────────────

class TestComputeSinceExit:
    def _df(self):
        return pd.DataFrame({
            "time": [f"2024-01-01T{h:02d}:00:00Z" for h in range(10)],
            "close": [round(1.1000 + i * 0.0005, 5) for i in range(10)],
        })

    def test_exit_inside_forming_candle(self):
        bars, move = strategist.compute_since_exit(
            self._df(), datetime(2024, 1, 1, 9, 30, tzinfo=timezone.utc), 1.1045, 0.0001)
        assert bars == 0
        assert move == pytest.approx(0.0)

    def test_exit_exactly_at_candle_open_counts_that_candle(self):
        bars, move = strategist.compute_since_exit(
            self._df(), datetime(2024, 1, 1, 6, 0, tzinfo=timezone.utc), 1.1045, 0.0001)
        assert bars == 3                     # velas 07, 08, 09 posteriores
        assert move == pytest.approx(15.0)   # 1.1045 − close(06:00)=1.1030

    def test_exit_between_candles(self):
        bars, move = strategist.compute_since_exit(
            self._df(), datetime(2024, 1, 1, 5, 59, tzinfo=timezone.utc), 1.1045, 0.0001)
        assert bars == 4
        assert move == pytest.approx(20.0)

    def test_exit_before_window_is_none_not_wrong_number(self):
        bars, move = strategist.compute_since_exit(
            self._df(), datetime(2023, 12, 31, 23, 0, tzinfo=timezone.utc), 1.1045, 0.0001)
        assert bars is None
        assert move is None


# ─── Canal de memoria del obs (fetch_bars_since_close, Fase 3) ────────────────

class TestFetchBarsSinceClose:
    """Fase 3: el serve construye el canal bars_since_close con el MISMO
    dominio (broker) y la misma ventana que el narrativo del estratega."""

    def _df(self):
        return pd.DataFrame({
            "time": [f"2024-01-01T{h:02d}:00:00Z" for h in range(10)],
            "close": [round(1.1000 + i * 0.0005, 5) for i in range(10)],
        })

    @pytest.mark.asyncio
    async def test_bars_from_last_close(self):
        pool = _pool(_ROW)
        with patch.object(strategist, "get_pool", return_value=pool):
            bars = await strategist.fetch_bars_since_close(
                "ZZZUSD", df=self._df(), current_price=1.1045, pip_size=0.0001)
        assert bars == 3   # misma ventana que fetch_narrative (salida 06:00)
        assert pool.fetchrow.await_args.args[1] == "ZZZUSD"

    @pytest.mark.asyncio
    async def test_no_closes_returns_none(self):
        with patch.object(strategist, "get_pool", return_value=_pool(None)):
            bars = await strategist.fetch_bars_since_close(
                "ZZZUSD", df=self._df(), current_price=1.1045, pip_size=0.0001)
        assert bars is None   # el serve normaliza a 1.0 (sin cierre reciente)

    @pytest.mark.asyncio
    async def test_exit_before_window_returns_none(self):
        row = {**_ROW, "exit_time": datetime(2023, 12, 31, 23, 0, tzinfo=timezone.utc)}
        with patch.object(strategist, "get_pool", return_value=_pool(row)):
            bars = await strategist.fetch_bars_since_close(
                "ZZZUSD", df=self._df(), current_price=1.1045, pip_size=0.0001)
        assert bars is None   # salida anterior a la ventana → serve normaliza a 1.0


# ─── Router: traza en audit + shadow sin poder de decisión ────────────────────

def _post_predict(action, monkeypatch, position=0, *, deliberate_side_effect=None):
    model = _mock_ppo_model()
    model.predict.return_value = (np.array(action, dtype=np.float32), None)
    monkeypatch.setattr(qwen_client, "_QWEN_STATE", QwenHealthState())
    with ExitStack() as stack:
        stack.enter_context(patch("main.init_pool", AsyncMock()))
        stack.enter_context(patch("main.close_pool", AsyncMock()))
        stack.enter_context(patch("routers.ai.is_news_blackout", AsyncMock(return_value=(False, None))))
        stack.enter_context(patch("routers.ai.fetch_bars_since_close", AsyncMock(return_value=None)))
        stack.enter_context(patch("routers.ai.get_pool", return_value=MagicMock(execute=AsyncMock())))
        stack.enter_context(patch("routers.ai.PPO.load", return_value=model))
        stack.enter_context(patch("services.news_scraper.get_macro_news", AsyncMock(return_value="No macro news")))
        stack.enter_context(patch.object(strategist, "fetch_narrative", AsyncMock(return_value=NARRATIVE)))
        stack.enter_context(patch.object(strategist, "get_spread", AsyncMock(return_value=0.00012)))
        stack.enter_context(patch("httpx.AsyncClient", FakeAsyncClient))
        audit_mock = stack.enter_context(patch("routers.ai.log_to_audit", AsyncMock()))
        if deliberate_side_effect is not None:
            stack.enter_context(patch(
                "routers.ai.deliberate", AsyncMock(side_effect=deliberate_side_effect)))
        with TestClient(app) as client:
            response = client.post(
                "/api/v1/ai/predict",
                json={"symbol": "EURUSD", "timeframe": "H1", "position": position},
                headers={"x-internal-token": "test-token", "x-cycle-id": "strategist-shadow"},
            )
    return response, audit_mock


class TestRouterDeliberation:
    def test_audit_row_contains_full_deliberation(self, monkeypatch):
        response, audit_mock = _post_predict(ACTION_BUY, monkeypatch, position=0)
        assert response.status_code == 200, response.text
        data = audit_mock.await_args.args[1]

        d = data["deliberation"]
        assert d["verdict"] == "ENTER"   # candidato sin veto; C2 (activo) pasa
        assert d["shadow"] is True
        assert d["config_version"] == strategist.STRATEGIST_VERSION
        assert set(d["criteria"]) == set(strategist.CRITERIA)
        assert d["vetoes"]["fired"] == data["veto_reason"]   # misma verdad, una fuente
        assert d["narrative"]["consecutive_losses"] == 1
        # La respuesta es la del pipeline: el estratega no la toca
        assert response.json()["decision"] == "BUY"

    def test_shadow_never_changes_the_decision(self, monkeypatch):
        """Criterio activo que FALLA → verdict WAIT, respuesta INALTERADA."""
        _activate(monkeypatch, "C3_quality", threshold=9.5)  # quality 8.5 < 9.5
        response, audit_mock = _post_predict(ACTION_BUY, monkeypatch, position=0)
        assert response.status_code == 200, response.text

        d = audit_mock.await_args.args[1]["deliberation"]
        assert d["verdict"] == "WAIT"
        assert d["blocked_by"] == ["C3_quality"]
        # CERO poder de decisión: la cadena no se entera
        assert response.json()["decision"] == "BUY"
        assert audit_mock.await_args.args[1]["veto"] is False

    def test_position_veto_cycle_traces_wait_with_fired_reason(self, monkeypatch):
        response, audit_mock = _post_predict(ACTION_FLIP_SELL, monkeypatch, position=1)
        assert response.status_code == 200, response.text
        data = audit_mock.await_args.args[1]

        d = data["deliberation"]
        assert d["verdict"] == "WAIT"
        assert d["blocked_by"] == ["veto:position_veto"]
        assert d["vetoes"]["fired"] == "position_veto" == data["veto_reason"]
        assert d["vetoes"]["position_open"] is True
        assert response.json()["decision"] == "HOLD"   # contrato EA intacto

    def test_strategist_failure_is_contained_as_error_trace(self, monkeypatch, caplog):
        """Contención en la frontera: el fallo del shadow NO tumba el ciclo."""
        with caplog.at_level(logging.DEBUG):
            response, audit_mock = _post_predict(
                ACTION_BUY, monkeypatch, position=0,
                deliberate_side_effect=RuntimeError("boom"),
            )
        assert response.status_code == 200, response.text
        assert response.json()["decision"] == "BUY"

        d = audit_mock.await_args.args[1]["deliberation"]
        assert d["verdict"] == "ERROR"
        assert d["shadow"] is True
        assert d["config_version"] == strategist.STRATEGIST_VERSION
        assert d["error"] == "RuntimeError: boom"

        warns = [r for r in caplog.records if "deliberación falló" in r.getMessage()]
        assert len(warns) == 1
        assert warns[0].levelno == logging.WARNING
