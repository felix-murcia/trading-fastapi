"""Contrato de dinero de una entrada — los TRES invariantes que la orden 80 rompió.

Caso real (USDCHF, 22-10-2026 10:02 UTC, orden 80):
    lote 0,02 · SL 45,4 pips · TP 13,7 pips · equity 486,69 €
    · TP bruto 3,34 € (audit 3,36) < Safe-4 4,00 € ⇒ el trailing no podía
      dispararse NUNCA y el cierre sellaba por debajo de la regla de 3 €;
    · riesgo real 11,06 € > tope 2 % (9,73 €) ⇒ +13,5 %.

Este módulo es la regresión: si alguien vuelve a decidir lote, SL, TP o
recompensa en otro sitio, estos tests fallan.
"""
from __future__ import annotations

import pytest

from services.position_sizing import expected_close_eur
from services.trade_contract import (
    EXEC_COST_PIPS,
    evaluate_trade,
    pip_value_eur_per_lot,
)

# Parámetros vivos (.env 22-10): riesgo 2 %, lote 0,01…0,50, Safe-4 4 €.
RISK_PCT, MIN_LOT, MAX_LOT, LOCK = 0.02, 0.01, 0.50, 4.0


def _contract(**kw):
    base = dict(
        symbol="EURUSD", price=1.12391, equity=489.49, sl_pips=10.0,
        head_lots=0.04, tp_candidates_pips=[11.0], risk_pct=RISK_PCT,
        min_lot=MIN_LOT, max_lot=MAX_LOT, min_lock_eur=LOCK,
        eurusd_price=1.10,
    )
    base.update(kw)
    return evaluate_trade(**base)


class TestOrden80:
    """El caso que Félix señaló: debe ser IMPOSIBLE, no 'poco probable'."""

    def test_usdchf_debe_rechazarse(self):
        c = _contract(
            symbol="USDCHF", price=0.82066, equity=486.69,
            sl_pips=45.4, head_lots=0.02, tp_candidates_pips=[13.7],
        )
        assert c.ok is False
        assert c.reason == "tp_no_paga_bloqueo"
        # el TP más cercano (13,7 p) no paga 4 € netos con ningún lote que
        # quepa en el tope de riesgo
        assert c.reward_net_eur == 0.0

    def test_el_mismo_stop_con_el_techo_que_paga_SI_es_viable(self):
        """Un TP más lejos hace el trade viable CON MENOS lote (la
        coherencia lote↔geometría que faltaba)."""
        c = _contract(
            symbol="USDCHF", price=0.82066, equity=486.69,
            sl_pips=45.4, head_lots=0.02, tp_candidates_pips=[13.7, 40.0],
        )
        assert c.ok is True
        assert c.tp_pips == 40.0
        assert c.lots == pytest.approx(0.01)
        assert c.risk_eur <= c.cap_eur + 1e-9
        assert c.reward_net_eur >= LOCK - 1e-9

    def test_el_riesgo_nunca_supera_el_tope(self):
        c = _contract(
            symbol="USDCHF", price=0.82066, equity=486.69,
            sl_pips=45.4, head_lots=MAX_LOT, tp_candidates_pips=[40.0],
        )
        assert c.ok is True
        assert c.risk_eur <= c.cap_eur + 1e-9
        assert c.risk_eur == pytest.approx(0.01 * 45.4 * c.ppl_eur)


class TestInvariantes:
    def test_entrada_sana_levanta_el_lote_hasta_que_el_tp_paga(self):
        # 0,04 lotes pedidos ⇒ el TP de 11 p no pagaría 4 € netos ⇒ sube a 0,05
        c = _contract(head_lots=0.04, sl_pips=10.0, tp_candidates_pips=[11.0])
        assert c.ok is True
        assert c.lots == pytest.approx(0.05)
        assert c.risk_eur >= LOCK and c.risk_eur <= c.cap_eur
        assert c.reward_net_eur >= LOCK and c.reward_net_eur <= c.reward_gross_eur
        assert c.cost_eur == pytest.approx(c.lots * EXEC_COST_PIPS * c.ppl_eur)

    def test_head_alto_se_recorta_al_tope(self):
        c = _contract(head_lots=MAX_LOT, sl_pips=10.0, tp_candidates_pips=[11.0])
        assert c.ok is True
        assert c.risk_eur <= c.cap_eur + 1e-9

    def test_head_cero_no_abre(self):
        c = _contract(head_lots=0.0)
        assert c.ok is False and c.reason == "sizing_inviable"

    def test_stop_imposible(self):
        c = _contract(sl_pips=200.0, tp_candidates_pips=[400.0])
        assert c.ok is False and c.reason == "riesgo_no_cabe"

    def test_tp_por_debajo_del_coste(self):
        c = _contract(sl_pips=10.0, tp_candidates_pips=[1.5])
        assert c.ok is False and c.reason == "tp_no_paga_bloqueo"

    def test_faltan_magnitudes_fisicas(self):
        with pytest.raises(ValueError):
            _contract(sl_pips=0.0)
        with pytest.raises(ValueError):
            _contract(price=0.0)


class TestConversionEUR:
    def test_pip_value_eurgun_las_mismas_funciones(self):
        # una sola conversión: la del gate/ledger
        for sym, px in (("EURUSD", 1.12391), ("USDCHF", 0.82066)):
            assert pip_value_eur_per_lot(sym, px) == pytest.approx(
                expected_close_eur(sym, 1.0, 1.0, px)
            )
        # USDCHF: 10 USD/pip/lote ÷ 0,82066 = 12,18 € ⇒ el sizing ya no
        # mezcla unidades (era el +13,5 % de riesgo)
        assert pip_value_eur_per_lot("USDCHF", 0.82066) == pytest.approx(12.185, rel=1e-3)
        assert pip_value_eur_per_lot("EURUSD", 1.12391) == pytest.approx(8.897, rel=1e-3)

    def test_el_serve_usa_las_mismas_funciones(self):
        import routers.ai as ai_router
        assert ai_router.evaluate_trade is evaluate_trade
        assert ai_router.pip_value_eur_per_lot is pip_value_eur_per_lot
