"""
Walk-forward OOS evaluation (P1-1) — validación sin solape con embargo.

Diseño (auditoría P1-1: "≥4 folds, embargo, test holdout congelado, métricas
persistidas, gate multi-métrico sin solape"):

    [0, train_end)              train      — entrenamiento; NUNCA evaluado aquí
    [train_end, train_end+emb)  embargo     — contexto del fold 1: ni entrenado
                                              ni puntuado (window_size barras)
    folds (4) consecutivas      test folds  — puntuación DISJUNTA entre sí y
                                              respecto de train y holdout; el
                                              contexto de cada fold son las
                                              window_size barras anteriores
                                              (puntuadas en el fold anterior,
                                              NO puntuadas en este)
    [holdout_start, n)          holdout     — CONGELADO: se evalúa y persiste
                                              pero NUNCA decide la promoción

Solape de SCORING = 0 en todas las fronteras. Las regiones puntuadas
particionan el eje temporal completo.

Métricas (velas H1; anualización 24*252 barras/año):
- sharpe / sortino: retornos por paso de la curva de equity del episodio
- max_dd: peak-to-trough de la curva de equity
- expectancy: PnL medio por trade CON regla 3 € (R-1): cada close <3 €
  cuenta como pérdida de la misma magnitud; el crudo
  ((balance_final − inicial)/trades) se reporta aparte como expectancy_raw
- rewards agregados: sum_reward (decisión primaria) — el reward del env ya
  clasifica los closes sub-3 € como pérdida (R-1), así que sum_reward y
  expectancy hablan el mismo idioma que AGENTS.md

Gate multi-métrico (todas las reglas estrictas; empates = fallo):
1. total_openings >= min_openings (candidato evaluable, no HOLD permanente)
2. métricas finitas
3. Suelo absoluto (P0 G-1, plan 2026-09-30): en el agregado de pliegues,
   sum_reward > 0, mean_sharpe > 0 y total_expectancy > 0 — en ambas vías.
   Un candidato no positivo en absoluto no promueve aunque supere a un
   baseline peor. El holdout sigue sin decidir.
4. Si hay baseline: sum_reward > baseline, mayoría de folds ganados
   (>= n_folds//2 + 1) y mean_sharpe > baseline.

Sin baseline (primer modelo): reglas 1-3 + umbrales canario absolutos.
"""
from dataclasses import dataclass

import numpy as np
import pandas as pd

from core.rules import is_winning_close

H1_BARS_PER_YEAR = 24 * 252  # Forex: 24h de sesiones × 252 días hábiles

# Sin fallback ni valores por defecto: si el split es imposible, se lanza
# RuntimeError con mensaje accionable (nunca un split silencioso).


@dataclass(frozen=True)
class Slice:
    """Segmento evaluado: contexto [context_start, start) + puntuado [start, end)."""
    context_start: int
    start: int
    end: int

    @property
    def bars(self) -> int:
        return self.end - self.start


@dataclass(frozen=True)
class WalkForwardSplit:
    train_end: int          # exclusivo; train = [0, train_end)
    embargo: int            # barras de embargo tras train
    folds: tuple            # tuple[Slice, ...] con scored disjunto
    holdout: Slice          # congelado

    @property
    def n_folds(self) -> int:
        return len(self.folds)

    def to_dict(self) -> dict:
        return {
            "train_end": self.train_end,
            "embargo": self.embargo,
            "folds": [
                {"fold": i, "context_start": s.context_start, "start": s.start, "end": s.end}
                for i, s in enumerate(self.folds)
            ],
            "holdout": {
                "context_start": self.holdout.context_start,
                "start": self.holdout.start,
                "end": self.holdout.end,
            },
        }


@dataclass
class GateDecision:
    ok: bool
    reason: str
    details: dict


def build_walk_forward_split(
    n_bars: int,
    *,
    window_size: int,
    n_folds: int = 4,
    train_frac: float = 0.70,
    holdout_frac: float = 0.10,
    min_fold_bars: int = 10,
) -> WalkForwardSplit:
    """Corta df en train + embargo + n_folds disjuntos + holdout congelado.

    Falla ruidosamente si los datos no soportan el diseño (mejor abortar el
    retrain que evaluar sobre pliegues de 3 barras).
    """
    if n_folds < 4:
        raise RuntimeError(f"[WALK-FORWARD] La auditoría exige >=4 folds (recibido {n_folds})")
    if window_size < 1:
        raise RuntimeError(f"[WALK-FORWARD] window_size inválido: {window_size}")
    if not 0.0 < holdout_frac < 0.5:
        raise RuntimeError(f"[WALK-FORWARD] holdout_frac fuera de rango: {holdout_frac}")
    if not 0.5 <= train_frac <= 0.9:
        raise RuntimeError(f"[WALK-FORWARD] train_frac fuera de rango: {train_frac}")

    train_end = int(n_bars * train_frac)
    holdout_start = int(n_bars * (1.0 - holdout_frac))
    embargo = window_size
    region_start = train_end + embargo

    if train_end <= window_size + 2:
        raise RuntimeError(
            f"[WALK-FORWARD] train demasiado corto: {train_end} barras "
            f"(n_bars={n_bars}). Aumentar lookback_candles."
        )
    if holdout_start - region_start < n_folds * min_fold_bars:
        raise RuntimeError(
            f"[WALK-FORWARD] región de folds demasiado corta: "
            f"{holdout_start - region_start} < {n_folds}×{min_fold_bars} barras "
            f"(n_bars={n_bars}, train_end={train_end}, embargo={embargo}). "
            f"Aumentar lookback_candles."
        )
    if n_bars - holdout_start < min_fold_bars:
        raise RuntimeError(
            f"[WALK-FORWARD] holdout demasiado corto: {n_bars - holdout_start} "
            f"< {min_fold_bars} barras (n_bars={n_bars}). Aumentar lookback_candles."
        )

    fold_len = (holdout_start - region_start) // n_folds
    folds = []
    for i in range(n_folds):
        start = region_start + i * fold_len
        # El último pliego absorbe el resto del floor-division para no dejar
        # barras muertas entre el fold final y el holdout.
        end = holdout_start if i == n_folds - 1 else start + fold_len
        folds.append(Slice(context_start=start - window_size, start=start, end=end))

    holdout = Slice(
        context_start=holdout_start - window_size,
        start=holdout_start,
        end=n_bars,
    )
    return WalkForwardSplit(
        train_end=train_end,
        embargo=embargo,
        folds=tuple(folds),
        holdout=holdout,
    )


def _sharpe(returns: np.ndarray) -> float:
    if len(returns) < 2:
        return 0.0
    std = float(np.std(returns))
    if std == 0.0:
        return 0.0
    return float(np.mean(returns) / std * np.sqrt(H1_BARS_PER_YEAR))


def _sortino(returns: np.ndarray) -> float:
    if len(returns) < 1:
        return 0.0
    downside = returns[returns < 0]
    if len(downside) < 1:
        # Sin downside no hay señal medible: 0.0 (conservador) en vez de inf.
        return 0.0
    dstd = float(np.std(downside))
    if dstd == 0.0:
        return 0.0
    return float(np.mean(returns) / dstd * np.sqrt(H1_BARS_PER_YEAR))


def _max_dd(equity: np.ndarray) -> float:
    if len(equity) < 2:
        return 0.0
    peak = np.maximum.accumulate(equity)
    dd = (peak - equity) / np.maximum(peak, 1e-9)
    return float(np.max(dd))


def segment_metrics(result: dict, initial_balance: float) -> dict:
    """Métricas de un episodio (_evaluate_model) en unidades comparables.

    R-1 / regla 3 € (plan D-2): `expectancy` clasifica el pnl de cada trade
    con `core.rules.is_winning_close` — un close ∈ [0, 3 €) cuenta como
    PÉRDIDA de la misma magnitud (signo invertido). Así un candidato que
    sobreviva de closes sub-3 € no puede superar el suelo absoluto del gate.
    `expectancy_raw` conserva la media cruda como diagnóstico; el gate usa
    `expectancy`.

    Sin trade_pnls coherentes no se calcula: RuntimeError (sin datos, sin
    valor inventado).
    """
    equity = np.asarray(result.get("equity_curve", []), dtype=float)
    rewards = np.asarray(result.get("step_rewards", []), dtype=float)
    trades = int(result.get("trades", 0))
    balance = float(result.get("balance", initial_balance))
    pnls = result.get("trade_pnls")

    if len(equity) >= 2:
        rets = np.diff(equity) / np.maximum(equity[:-1], 1e-9)
    else:
        rets = np.array([], dtype=float)

    expectancy_raw = float((balance - initial_balance) / trades) if trades else 0.0
    if trades == 0:
        expectancy = 0.0
    else:
        if pnls is None or len(pnls) != trades:
            raise RuntimeError(
                f"[WALK-FORWARD] trades={trades} sin trade_pnls coherentes "
                f"(len={len(pnls) if pnls is not None else 'ausente'}) — la "
                "expectancy con regla 3 € exige el pnl por trade"
            )
        # close <3 € → pérdida de la MISMA magnitud; losses (pnl<0) no cambian
        expectancy = float(np.mean([
            p if is_winning_close(p) else -abs(p) for p in pnls
        ]))

    return {
        "reward": float(result.get("reward", 0.0)),
        "sharpe": _sharpe(rets),
        "sortino": _sortino(rets),
        "max_dd": _max_dd(equity),
        "expectancy": expectancy,
        "expectancy_raw": expectancy_raw,
        "trades": trades,
        "wins": int(result.get("wins", 0)),
        "losses": int(result.get("losses", 0)),
        "win_rate": float(result.get("win_rate", 0.0)),
        "openings": int(result.get("openings", 0)),
        "n_steps": int(len(rewards)),
        "final_equity": float(equity[-1]) if len(equity) else float(initial_balance),
    }


def _aggregate(folds_metrics: list) -> dict:
    if not folds_metrics:
        raise RuntimeError("[WALK-FORWARD] aggregate sobre 0 folds")
    return {
        "sum_reward": float(sum(m["reward"] for m in folds_metrics)),
        "mean_sharpe": float(np.mean([m["sharpe"] for m in folds_metrics])),
        "mean_sortino": float(np.mean([m["sortino"] for m in folds_metrics])),
        "worst_max_dd": float(max(m["max_dd"] for m in folds_metrics)),
        "total_expectancy": float(sum(m["expectancy"] for m in folds_metrics)),
        "total_trades": int(sum(m["trades"] for m in folds_metrics)),
        "total_openings": int(sum(m["openings"] for m in folds_metrics)),
        "n_steps": int(sum(m["n_steps"] for m in folds_metrics)),
    }


@dataclass
class WalkForwardReport:
    n_folds: int
    embargo_bars: int
    train_bars: int
    folds: list           # [{"fold", "bars", "candidate", "baseline"}]
    holdout: dict         # {"bars", "candidate", "baseline"} — solo reporte
    candidate: dict       # agregado sobre folds
    baseline: dict | None
    fold_wins: int | None

    def to_dict(self) -> dict:
        return {
            "n_folds": self.n_folds,
            "embargo_bars": self.embargo_bars,
            "train_bars": self.train_bars,
            "folds": self.folds,
            "holdout": self.holdout,
            "candidate": self.candidate,
            "baseline": self.baseline,
            "fold_wins": self.fold_wins,
        }


def evaluate_walk_forward(
    df: pd.DataFrame,
    split: WalkForwardSplit,
    *,
    candidate,
    candidate_obs_rms,
    baseline,
    baseline_obs_rms,
    evaluate_fn,
    initial_balance: float,
) -> WalkForwardReport:
    """Evalúa candidato y baseline (si existe) en los mismos folds disjuntos.

    evaluate_fn(model, segment_df, obs_rms) -> dict de _evaluate_model.
    El holdout se evalúa para reporte; el gate no lo lee.
    """

    def _run(model, sl: Slice, rms):
        segment = df.iloc[sl.context_start:sl.end]
        return segment_metrics(evaluate_fn(model, segment, rms), initial_balance)

    cand_folds, base_folds, folds_out = [], [], []
    for i, sl in enumerate(split.folds):
        cm = _run(candidate, sl, candidate_obs_rms)
        bm = _run(baseline, sl, baseline_obs_rms) if baseline is not None else None
        cand_folds.append(cm)
        if bm is not None:
            base_folds.append(bm)
        folds_out.append({"fold": i, "bars": sl.bars, "candidate": cm, "baseline": bm})

    hold_c = _run(candidate, split.holdout, candidate_obs_rms)
    hold_b = _run(baseline, split.holdout, baseline_obs_rms) if baseline is not None else None

    fold_wins = None
    if baseline is not None:
        fold_wins = sum(1 for c, b in zip(cand_folds, base_folds) if c["reward"] > b["reward"])

    return WalkForwardReport(
        n_folds=split.n_folds,
        embargo_bars=split.embargo,
        train_bars=split.train_end,
        folds=folds_out,
        holdout={"bars": split.holdout.bars, "candidate": hold_c, "baseline": hold_b},
        candidate=_aggregate(cand_folds),
        baseline=_aggregate(base_folds) if baseline is not None else None,
        fold_wins=fold_wins,
    )


# P3-3: umbrales ABSOLUTOS del primer modelo de un par (sin baseline que
# comparar). Antes "first_model_validated" solo exigía aperturas del
# caller (min_openings=1) y métricas finitas: un primer modelo casi vacío
# pasaba. El canario exige masa estadística real + calidad por pliego.
CANARY_MIN_OPENINGS = 200        # aperturas agregadas en los pliegues OOS
CANARY_SHARPE_MIN = 1.0          # sharpe mínimo por pliego
CANARY_MIN_FOLDS = 4             # pliegues que deben superar CANARY_SHARPE_MIN


def gate_walk_forward(report: WalkForwardReport, min_openings: int) -> GateDecision:
    """Gate multi-métrico sin solape: todas las reglas estrictas (empate = fallo).

    Con baseline (modelo activo existente): comparativo estricto — sin
    cambios. Sin baseline (primer modelo del par): umbrales canario
    absolutos (P3-3) — la vía que valida GBPUSD u otros pares nuevos.
    """
    cand = report.candidate
    details = {
        "candidate": cand,
        "baseline": report.baseline,
        "fold_wins": report.fold_wins,
        "n_folds": report.n_folds,
    }

    if cand["total_openings"] < min_openings:
        return GateDecision(False, "insufficient_openings", details)

    values = [cand["sum_reward"], cand["mean_sharpe"], cand["mean_sortino"], cand["worst_max_dd"]]
    if not all(np.isfinite(v) for v in values):
        return GateDecision(False, "non_finite_metrics", details)

    # P0 G-1 (plan 2026-09-30): suelo ABSOLUTO en ambas vías — un candidato
    # que no es positivo en absoluto no promueve nunca, aunque supere a un
    # baseline peor. Sin este suelo, la promoción 2026-09-30 03:14 se aprobó
    # con sum_reward=-4.716 / mean_sharpe=-3.19 / total_expectancy=-10.26
    # solo porque el baseline era aún peor (cada "menos malo" era el nuevo
    # baseline: deriva en cadena). Forma positiva en todas: un NaN no supera
    # `> 0` y cae al rechazo (expectancy no está en el chequeo de finitud).
    absolute_floor = {
        "sum_reward": cand["sum_reward"] > 0.0,
        "mean_sharpe": cand["mean_sharpe"] > 0.0,
        "total_expectancy": cand["total_expectancy"] > 0.0,
    }
    if not all(absolute_floor.values()):
        details["absolute_floor"] = absolute_floor
        failed_floor = next(k for k, v in absolute_floor.items() if not v)
        return GateDecision(False, f"absolute_floor_{failed_floor}", details)

    if report.baseline is None:
        if cand["total_openings"] < CANARY_MIN_OPENINGS:
            details["canary"] = {"min_openings": CANARY_MIN_OPENINGS}
            return GateDecision(False, "canary_insufficient_openings", details)
        fold_sharpes = [f["candidate"]["sharpe"] for f in report.folds]
        # max(1, …): un informe sin pliegues no puede validarse (0 < 1 → rechazo).
        needed = max(1, min(CANARY_MIN_FOLDS, len(fold_sharpes)))
        strong = sum(
            1 for s in fold_sharpes
            if np.isfinite(s) and s >= CANARY_SHARPE_MIN
        )
        details["canary"] = {
            "min_openings": CANARY_MIN_OPENINGS,
            "sharpe_min": CANARY_SHARPE_MIN,
            "folds_strong": strong,
            "folds_needed": needed,
            "fold_sharpes": fold_sharpes,
        }
        if strong < needed:
            return GateDecision(False, "canary_sharpe_below", details)
        return GateDecision(True, "first_model_validated", details)

    base = report.baseline
    majority = (report.n_folds // 2) + 1
    checks = {
        "sum_reward_strict": cand["sum_reward"] > base["sum_reward"],
        "fold_majority": report.fold_wins >= majority,
        "mean_sharpe_strict": cand["mean_sharpe"] > base["mean_sharpe"],
    }
    details["checks"] = checks
    details["majority_needed"] = majority

    if all(checks.values()):
        return GateDecision(True, "walk_forward_improved", details)

    failed = [k for k, v in checks.items() if not v]
    return GateDecision(False, "failed:" + ",".join(failed), details)
