"""
T-31/Promo (orden Félix 2026-10-19) — REGLA DE PROMOCIÓN POR MEJORA DRÁSTICA.

*«quiero que cuando el candidato sea drásticamente mejor que el modelo vivo,
se promocione el drásticamente mejor. Haz lo que tengas que hacer, pero
quiero exactamente eso»*

La regla (services/promotion_rule.py) exige 8 criterios congelados:
4/4 folds, sharpe de holdout > 0, drawdown ≤ 10 % (suelo absoluto) y ≤ la
mitad del vivo, ≤ la mitad de operaciones, reward y expectativa del holdout
mejores, y dominancia en la suma de reward de los folds.

Estos tests fijan:
- el caso REAL de exp39 (drásticamente mejor ⇒ promovido);
- que los casos近 del T-30 (exp38: 3/4 folds y más operaciones) NO pasan;
- que "mejor que un baseline terrible" no es puerta trasera (suelo absoluto);
- que la vía normal (suelo absoluto) manda cuando pasa.
"""
import copy

import pytest

from services.promotion_rule import (
    MAX_HOLDOUT_DD,
    evaluate_drastic_improvement,
    promotion_path,
)

# Informe REAL del exp39 (medido 2026-10-19): candidato = bundle vivo + 100k
# pasos más. Gana 4/4 folds; en el holdout congelado pasa de −39,86 a −1,86,
# de 42,86 % a 4,51 % de drawdown y de 137 a 16 operaciones.
REPORT_EXP39 = {
    "n_folds": 4,
    "fold_wins": 4,
    "candidate": {
        "n_steps": 4000, "sum_reward": -33.42259004613152,
        "mean_sharpe": -1.3092433035657578, "mean_sortino": -0.8410917719821255,
        "total_trades": 121, "worst_max_dd": 0.21227078589044457,
        "total_openings": 122, "total_expectancy": -6.768880057701596,
    },
    "baseline": {
        "n_steps": 4000, "sum_reward": -138.879,
        "mean_sharpe": -1.00, "mean_sortino": -0.5,
        "total_trades": 137, "worst_max_dd": 0.4286,
        "total_openings": 137, "total_expectancy": -20.0,
    },
    "holdout": {
        "candidate": {
            "reward": -1.863, "sharpe": 0.19, "expectancy": -0.527,
            "trades": 16, "win_rate": 0.38, "max_dd": 0.0451,
        },
        "baseline": {
            "reward": -39.857, "sharpe": -1.07, "expectancy": -1.595,
            "trades": 137, "win_rate": 0.31, "max_dd": 0.4286,
        },
    },
}


class TestDrasticImprovementRule:
    def test_exp39_qualifies(self):
        """El caso real: el candidato domina al vivo en los 8 criterios."""
        v = evaluate_drastic_improvement(REPORT_EXP39)
        assert v["granted"] is True, v["failed"]
        assert v["failed"] == []
        assert all(c["pass"] for c in v["checks"].values())

    def test_fold_wins_must_be_unanimous(self):
        """3/4 folds NO es dominio: exp38 (T-30) queda fuera por esto."""
        r = copy.deepcopy(REPORT_EXP39)
        r["fold_wins"] = 3
        v = evaluate_drastic_improvement(r)
        assert v["granted"] is False
        assert "fold_wins_all" in v["failed"]

    def test_more_trades_than_baseline_is_rejected(self):
        """Anti-churn: un candidato que opera MÁS no es 'drásticamente mejor'
        (exp38: 62 trades vs 31 del baseline)."""
        r = copy.deepcopy(REPORT_EXP39)
        r["holdout"]["candidate"]["trades"] = 62
        r["holdout"]["baseline"]["trades"] = 31
        v = evaluate_drastic_improvement(r)
        assert v["granted"] is False
        assert "holdout_trades_vs_baseline" in v["failed"]

    def test_absolute_risk_floor_blocks(self):
        """'Mejor que un baseline terrible' NO es puerta trasera: con 25 % de
        drawdown absoluto (aunque la mitad del vivo), no se promueve."""
        r = copy.deepcopy(REPORT_EXP39)
        r["holdout"]["candidate"]["max_dd"] = 0.25
        r["holdout"]["baseline"]["max_dd"] = 0.60
        v = evaluate_drastic_improvement(r)
        assert v["granted"] is False
        assert "holdout_dd_absolute_floor" in v["failed"]

    def test_negative_sharpe_blocks(self):
        """Calidad absoluta mínima: sin sharpe positivo en el holdout, no."""
        r = copy.deepcopy(REPORT_EXP39)
        r["holdout"]["candidate"]["sharpe"] = -0.10
        v = evaluate_drastic_improvement(r)
        assert v["granted"] is False
        assert "holdout_sharpe_positive" in v["failed"]

    def test_missing_data_is_not_a_pass(self):
        """Sin datos no hay Promoting (fail-closed de los criterios)."""
        v = evaluate_drastic_improvement({})
        assert v["granted"] is False
        assert len(v["failed"]) == 8

    def test_thresholds_are_frozen(self):
        assert MAX_HOLDOUT_DD == 0.10


class TestPromotionPath:
    def test_absolute_floor_path_wins_when_it_passes(self):
        """Si el gate normal pasa, manda el normal (sin evaluar la regla)."""
        p = promotion_path(True, "ok", REPORT_EXP39)
        assert p["promote"] is True
        assert p["path"] == "absolute_floor"
        assert p["drastic"] is None

    def test_drastic_path_when_floor_fails_but_rule_grants(self):
        p = promotion_path(False, "absolute_floor_sum_reward", REPORT_EXP39)
        assert p["promote"] is True
        assert p["path"] == "drastic_improvement"
        assert "absolute_floor_sum_reward" in p["reason"]

    def test_rejection_when_neither(self):
        r = copy.deepcopy(REPORT_EXP39)
        r["fold_wins"] = 2
        p = promotion_path(False, "absolute_floor_sum_reward", r)
        assert p["promote"] is False
        assert p["path"] == "rejected"
        assert "absolute_floor_sum_reward" in p["reason"]
        assert "fold_wins_all" in p["reason"]
