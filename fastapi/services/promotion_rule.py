"""
REGLA DE PROMOCIÓN POR MEJORA DRÁSTICA (orden de Félix 2026-10-19).

*«Quiero que cuando pase esto — "el candidato es drásticamente mejor que el
modelo vivo" — promocione el que es drásticamente mejor. Haz lo que tengas
que hacer, pero quiero exactamente eso».*

Por qué existe
--------------
El suelo absoluto del walk-forward (`services/walk_forward.py`, P0 G-1) exige
que el candidato sea **positivo en absoluto** (sum_reward, mean_sharpe y
total_expectancy > 0). Lleva tres intentos sin que ningún candidato lo
alcance (exp37/38/39, todos `absolute_floor_sum_reward`), mientras el
modelo VIVO, evaluado hoy con el mismo protocolo, es **profundamente
negativo**:

| holdout @26799 (congelado) | modelo vivo | candidato exp39 |
|---|---|---|
| reward | −39,86 | **−1,86** |
| sharpe | −1,07 | **+0,19** |
| expectancy | −1,60 € | **−0,53 €** |
| max drawdown | **42,86 %** | **4,51 %** |
| operaciones | **137** | **16** |

El vivo fue promovido el 07-10 por ser "mejor que un baseline peor" (la
deriva en cadena que el sueloabortó). Con este criterio, el sistema puede
seguir esperando a la perfección mientras opera con un modelo que arriesga
el 43 % de drawdown y gira 8 veces más de lo necesario.

La regla mantiene un **suelo absoluto de seguridad** para que "mejor que un
baseline terrible" no sea una puerta trasera: el candidato tiene que ser
mejor en términos RELATIVOS **y** tener calidad ABSOLUTA mínima.

Criterios (congelados — no se re-tunea sin orden expresa)
--------------------------------------------------------
1. `fold_wins == n_folds` — gana en TODOS los folds del walk-forward (no es
   ruido estadístico).
2. `holdout.sharpe > 0` — calidad absoluta mínima en el holdout congelado.
3. `holdout.max_dd <= 0.10` — suelo ABSOLUTO de riesgo (10 %): por debajo
   delDrawdown del holdout, no se promueve nada.
4. `holdout.max_dd <= 0.5 × baseline.max_dd` — al menos la mitad de drawdown
   que el modelo vivo.
5. `holdout.trades <= 0.5 × baseline.trades` — al menos la mitad menos de
   operaciones (anti-churn: el coste O-2 ancla el churn, no lo elimina).
6. `holdout.reward > baseline.reward` — el candidato gana en el holdout.
7. `holdout.expectancy > baseline.expectancy` — gana también en expectativa
   por operación (no sólo en suma).
8. `candidate.sum_reward > baseline.sum_reward` — dominancia también en los
   folds (no sólo en el holdout).

Sobre el gate T-28 en esta vía
------------------------------
T-28 (alineación PPO↔LLM) NO bloquea esta vía, y es una desviación
explícita del gate pre-registrado: el conflicto que T-28 mide está
**medido** como causado por el LLM, no por el PPO (T-31 Fase A: el sesgo del
LLM acierta el 43,9 % — por debajo del azar — y su veto se equivocaba el
85 % de las veces; además el veto ya está condicionado a autoridad
demostrada). Mantenerlo como bloqueante haría la promoción imposible por
un motivo que no dice nada de la calidad del PPO. El estado de T-28 se
registra SIEMPRE en el manifiesto, en la auditoría y en el log de la
promoción: es evidencia, no obstáculo.
"""
from typing import Optional

# Suelo absoluto de riesgo en el holdout (fracción del equity).
MAX_HOLDOUT_DD = 0.10
# El drawdown del candidato debe ser como máximo la mitad del del vivo.
DD_RATIO_MAX = 0.5
# Las operaciones del candidato: como máximo la mitad de las del vivo.
TRADES_RATIO_MAX = 0.5


def evaluate_drastic_improvement(report: dict) -> dict:
    """¿El candidato es drásticamente mejor que el modelo vivo?

    `report` es el dict del walk-forward (el mismo que se guarda en
    `report.json` y en `training_experiments.gate_metrics`): con las claves
    `candidate` (agregados de los folds), `baseline`, `fold_wins`,
    `n_folds` y `holdout` (`candidate`/`baseline`).

    Devuelve SIEMPRE el detalle de cada criterio (los que fallan y con qué
    valor) — nunca un veredicto opaco.
    """
    cand = (report or {}).get("candidate") or {}
    base = (report or {}).get("baseline") or {}
    hold = (report or {}).get("holdout") or {}
    h_cand = hold.get("candidate") or {}
    h_base = hold.get("baseline") or {}

    n_folds = int((report or {}).get("n_folds") or 0)
    fold_wins = int((report or {}).get("fold_wins") or 0)

    def num(d, k):
        v = d.get(k)
        return float(v) if isinstance(v, (int, float)) else None

    h_dd_c = num(h_cand, "max_dd")
    h_dd_b = num(h_base, "max_dd")
    h_tr_c = num(h_cand, "trades")
    h_tr_b = num(h_base, "trades")
    h_rw_c = num(h_cand, "reward")
    h_rw_b = num(h_base, "reward")
    h_ex_c = num(h_cand, "expectancy")
    h_ex_b = num(h_base, "expectancy")
    h_sh_c = num(h_cand, "sharpe")
    c_sr = num(cand, "sum_reward")
    b_sr = num(base, "sum_reward")

    checks = {
        # 1) dominio de los folds
        "fold_wins_all": {
            "pass": bool(n_folds) and fold_wins == n_folds,
            "value": f"{fold_wins}/{n_folds}",
        },
        # 2) calidad absoluta mínima
        "holdout_sharpe_positive": {
            "pass": h_sh_c is not None and h_sh_c > 0.0,
            "value": h_sh_c,
        },
        # 3) suelo absoluto de riesgo
        "holdout_dd_absolute_floor": {
            "pass": h_dd_c is not None and h_dd_c <= MAX_HOLDOUT_DD,
            "value": h_dd_c,
            "limit": MAX_HOLDOUT_DD,
        },
        # 4) drawdown al menos la mitad mejor
        "holdout_dd_vs_baseline": {
            "pass": (
                h_dd_c is not None and h_dd_b is not None and h_dd_b > 0
                and h_dd_c <= DD_RATIO_MAX * h_dd_b
            ),
            "value": h_dd_c, "baseline": h_dd_b, "ratio_limit": DD_RATIO_MAX,
        },
        # 5) churn al menos la mitad menor
        "holdout_trades_vs_baseline": {
            "pass": (
                h_tr_c is not None and h_tr_b is not None and h_tr_b > 0
                and h_tr_c <= TRADES_RATIO_MAX * h_tr_b
            ),
            "value": h_tr_c, "baseline": h_tr_b, "ratio_limit": TRADES_RATIO_MAX,
        },
        # 6) recompensa en el holdout
        "holdout_reward_vs_baseline": {
            "pass": h_rw_c is not None and h_rw_b is not None and h_rw_c > h_rw_b,
            "value": h_rw_c, "baseline": h_rw_b,
        },
        # 7) expectativa por operación
        "holdout_expectancy_vs_baseline": {
            "pass": h_ex_c is not None and h_ex_b is not None and h_ex_c > h_ex_b,
            "value": h_ex_c, "baseline": h_ex_b,
        },
        # 8) dominancia en los folds
        "folds_sum_reward_vs_baseline": {
            "pass": c_sr is not None and b_sr is not None and c_sr > b_sr,
            "value": c_sr, "baseline": b_sr,
        },
    }

    failed = [k for k, v in checks.items() if not v["pass"]]
    return {
        "granted": not failed,
        "checks": checks,
        "failed": failed,
        "reason": (
            "mejora drástica demostrada (8/8 criterios)"
            if not failed
            else "mejora drástica NO demostrada: fallan " + ", ".join(failed)
        ),
    }


def promotion_path(
    gate_ok: bool,
    gate_reason: str,
    report: dict,
) -> dict:
    """Veredicto de promoción: por el suelo absoluto o por mejora drástica.

    El suelo absoluto (walk-forward) manda cuando pasa. Si NO pasa, se
    evalúa la regla de mejora drástica (orden Félix 2026-10-19) — que es la
    única vía por la que un candidato con reward negativo puede promoverse,
    y sólo si domina en todos los criterios congelados.
    """
    if gate_ok:
        return {
            "promote": True,
            "path": "absolute_floor",
            "reason": gate_reason,
            "drastic": None,
        }
    drastic = evaluate_drastic_improvement(report)
    if drastic["granted"]:
        return {
            "promote": True,
            "path": "drastic_improvement",
            "reason": f"drastic_improvement (suelo absoluto no superado: {gate_reason})",
            "drastic": drastic,
        }
    return {
        "promote": False,
        "path": "rejected",
        "reason": f"{gate_reason} | {drastic['reason']}",
        "drastic": drastic,
    }
