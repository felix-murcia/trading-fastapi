"""
P1-1: harness de validación walk-forward (folds disjuntos, embargo, holdout).

Cobertura (comportamiento puro, sin red):
- build_walk_forward_split: ≥4 folds, scoring sin solape (train/embargo/folds/
  holdout particionan el eje temporal), contexto = window antes de cada pliego,
  holdout al final, y fallo ruidoso si los datos no bastan.
- segment_metrics: Sharpe/Sortino/max_dd/expectancy con valores sintéticos
  verificables.
- gate_walk_forward: reglas multi-métricas estrictas (empate = fallo),
  mayoría de folds, primer modelo, aperturas insuficientes, no finitos.
- evaluate_walk_forward: patrón de llamadas emparejado (candidato y baseline
  sobre los mismos pliegues), fold_wins, agregados; holdout reportado aparte.
"""
import numpy as np
import pandas as pd
import pytest

from services.walk_forward import (
    WalkForwardReport,
    build_walk_forward_split,
    evaluate_walk_forward,
    gate_walk_forward,
    segment_metrics,
)

SPLIT_KW = dict(
    window_size=20,
    n_folds=4,
    train_frac=0.70,
    holdout_frac=0.10,
    min_fold_bars=10,
)


def _split(n=500, **over):
    kw = dict(SPLIT_KW)
    kw.update(over)
    return build_walk_forward_split(n, **kw)


class TestBuildWalkForwardSplit:
    def test_four_folds_scoring_partition_without_overlap(self):
        s = _split(500)
        assert s.n_folds == 4
        assert s.embargo == 20

        scored = [(0, s.train_end)]
        scored += [(f.start, f.end) for f in s.folds]
        scored += [(s.holdout.start, s.holdout.end)]

        assert scored[0][0] == 0
        for (a0, a1), (b0, b1) in zip(scored, scored[1:]):
            assert a1 <= b0, f"solape de scoring entre {a0}-{a1} y {b0}-{b1}"
        assert scored[-1][1] == 500, "el holdout debe llegar al final del df"

    def test_train_excludes_embargo_and_folds(self):
        s = _split(500)
        assert s.folds[0].start == s.train_end + s.embargo
        # contexto del fold 1 = barras de embargo (ni entrenadas ni puntuadas)
        assert s.folds[0].context_start == s.train_end

    def test_every_fold_context_is_window_before_scored(self):
        s = _split(500)
        for f in s.folds:
            assert f.context_start == f.start - 20
            assert f.end > f.start
        assert s.holdout.context_start == s.holdout.start - 20

    def test_folds_are_contiguous_until_holdout(self):
        s = _split(500)
        for prev, nxt in zip(s.folds, s.folds[1:]):
            assert prev.end == nxt.start, "hueco o solape entre folds"
        assert s.folds[-1].end == s.holdout.start

    def test_too_few_bars_fails_loudly(self):
        with pytest.raises(RuntimeError, match="región de folds"):
            _split(200)

    def test_fewer_than_four_folds_rejected(self):
        with pytest.raises(RuntimeError, match=">=4 folds"):
            _split(500, n_folds=3)

    def test_min_fold_bars_enforced(self):
        with pytest.raises(RuntimeError, match="región de folds"):
            _split(310, min_fold_bars=100)

    def test_to_dict_is_serializable(self):
        import json
        payload = _split(500).to_dict()
        assert json.loads(json.dumps(payload)) == payload
        assert payload["train_end"] > 0
        assert len(payload["folds"]) == 4


class TestSegmentMetrics:
    @staticmethod
    def _result(**over):
        base = {
            "openings": 4, "trades": 4, "wins": 2, "losses": 2,
            "win_rate": 0.5, "reward": 1.0, "balance": 1040.0,
            "equity_curve": [1000.0, 1040.0],
            "step_rewards": [40.0],
            # R-1: pnl por trade consistente con balance (suma 40); todos
            # ≥3€ o negativos → expectancy clasificada == cruda.
            "trade_pnls": [25.0, 25.0, -5.0, -5.0],
        }
        base.update(over)
        return base

    def test_expectancy_is_realized_pnl_per_trade(self):
        m = segment_metrics(self._result(balance=1040.0, trades=4), initial_balance=1000.0)
        assert m["expectancy"] == pytest.approx(10.0)      # media de trade_pnls
        assert m["expectancy_raw"] == pytest.approx(10.0)  # (1040-1000)/4

    def test_expectancy_counts_sub_3euro_wins_as_losses(self):
        # R-1: los closes <3€ se invierten — crudo 10, clasificado 8.5
        m = segment_metrics(
            self._result(balance=1040.0, trades=4,
                         trade_pnls=[1.5, 1.5, 40.0, -3.0]),
            initial_balance=1000.0,
        )
        assert m["expectancy_raw"] == pytest.approx(10.0)
        assert m["expectancy"] == pytest.approx((-1.5 - 1.5 + 40.0 - 3.0) / 4)

    def test_expectancy_all_sub_3euro_wins_is_negative(self):
        # El escenario R-1 del plan: "ganancias" <3€ → expectancy ≤ 0 aunque
        # la media cruda sea positiva → el suelo absoluto del gate lo mata.
        m = segment_metrics(
            self._result(balance=1003.0, trades=2, wins=2, losses=0,
                         win_rate=1.0, trade_pnls=[1.5, 1.5]),
            initial_balance=1000.0,
        )
        assert m["expectancy_raw"] == pytest.approx(1.5)
        assert m["expectancy"] == pytest.approx(-1.5)

    def test_expectancy_requires_trade_pnls(self):
        with pytest.raises(RuntimeError, match="trade_pnls"):
            segment_metrics(
                self._result(trade_pnls=None), initial_balance=1000.0,
            )

    def test_zero_trades_expectancy_is_zero(self):
        m = segment_metrics(
            self._result(trades=0, balance=1000.0, equity_curve=[1000.0, 990.0],
                         step_rewards=[-10.0], reward=-10.0),
            initial_balance=1000.0,
        )
        assert m["expectancy"] == 0.0

    def test_max_dd_on_drawdown_equity(self):
        equity = [1000.0, 1100.0, 990.0, 1050.0]  # pico 1100 → trough 990
        m = segment_metrics(
            self._result(equity_curve=equity, step_rewards=[100.0, -110.0, 60.0]),
            initial_balance=1000.0,
        )
        assert m["max_dd"] == pytest.approx(110.0 / 1100.0)

    def test_max_dd_zero_on_monotonic_equity(self):
        m = segment_metrics(
            self._result(equity_curve=[1000.0, 1001.0, 1005.0, 1010.0],
                         step_rewards=[1.0, 4.0, 5.0]),
            initial_balance=1000.0,
        )
        assert m["max_dd"] == 0.0
        assert m["sharpe"] > 0.0

    def test_sortino_zero_without_downside_moves(self):
        m = segment_metrics(
            self._result(equity_curve=[1000.0, 1002.0, 1004.0],
                         step_rewards=[2.0, 2.0]),
            initial_balance=1000.0,
        )
        assert m["sortino"] == 0.0  # documentado: sin downside no hay señal medible

    def test_sharpe_zero_when_flat_equity(self):
        m = segment_metrics(
            self._result(equity_curve=[1000.0, 1000.0, 1000.0],
                         step_rewards=[0.0, 0.0]),
            initial_balance=1000.0,
        )
        assert m["sharpe"] == 0.0


def _agg(sum_reward=10.0, mean_sharpe=0.5, total_openings=8, **over):
    agg = {
        "sum_reward": sum_reward,
        "mean_sharpe": mean_sharpe,
        "mean_sortino": 0.4,
        "worst_max_dd": 0.05,
        "total_expectancy": 2.0,
        "total_trades": 4,
        "total_openings": total_openings,
        "n_steps": 80,
    }
    agg.update(over)
    return agg


def _report(cand, base=None, fold_wins=None, n_folds=4, folds=None):
    return WalkForwardReport(
        n_folds=n_folds,
        embargo_bars=20,
        train_bars=350,
        folds=folds if folds is not None else [],
        holdout={"bars": 50, "candidate": {}, "baseline": None},
        candidate=cand,
        baseline=base,
        fold_wins=fold_wins,
    )


def _canary_folds(sharpes):
    """Pliegues con la forma real para el gate canario (candidate.sharpe)."""
    return [
        {"fold": i, "bars": 100, "candidate": {"sharpe": s}, "baseline": None}
        for i, s in enumerate(sharpes)
    ]


class TestGateWalkForward:
    def test_improvement_on_all_rules_promotes(self):
        decision = gate_walk_forward(
            _report(_agg(sum_reward=12.0, mean_sharpe=1.0),
                    _agg(sum_reward=10.0, mean_sharpe=0.5),
                    fold_wins=3),
            min_openings=1,
        )
        assert decision.ok is True
        assert decision.reason == "walk_forward_improved"
        assert decision.details["checks"] == {
            "sum_reward_strict": True,
            "fold_majority": True,
            "mean_sharpe_strict": True,
        }
        assert decision.details["majority_needed"] == 3  # 4 folds → 3 de 4

    def test_tie_in_sum_reward_fails(self):
        decision = gate_walk_forward(
            _report(_agg(sum_reward=10.0, mean_sharpe=1.0),
                    _agg(sum_reward=10.0, mean_sharpe=0.5),
                    fold_wins=3),
            min_openings=1,
        )
        assert decision.ok is False
        assert decision.reason == "failed:sum_reward_strict"

    def test_minority_of_fold_wins_fails(self):
        decision = gate_walk_forward(
            _report(_agg(sum_reward=12.0, mean_sharpe=1.0),
                    _agg(sum_reward=10.0, mean_sharpe=0.5),
                    fold_wins=1),
            min_openings=1,
        )
        assert decision.ok is False
        assert decision.reason == "failed:fold_majority"

    def test_tie_in_sharpe_fails(self):
        decision = gate_walk_forward(
            _report(_agg(sum_reward=12.0, mean_sharpe=0.5),
                    _agg(sum_reward=10.0, mean_sharpe=0.5),
                    fold_wins=3),
            min_openings=1,
        )
        assert decision.ok is False
        assert decision.reason == "failed:mean_sharpe_strict"

    def test_first_model_below_canary_openings_rejected(self):
        # P3-3: sin baseline (primer modelo del par) ya NO basta con validez —
        # umbrales absolutos de canario (aperturas ≥ 200).
        decision = gate_walk_forward(
            _report(_agg(total_openings=8), folds=_canary_folds([2.0] * 4)),
            min_openings=1,
        )
        assert decision.ok is False
        assert decision.reason == "canary_insufficient_openings"

    def test_first_model_below_canary_fold_sharpe_rejected(self):
        # Aperturas suficientes pero calidad por pliego insuficiente:
        # ≥4 pliegues deben superar sharpe 1.0 (aquí solo 1 lo hace).
        decision = gate_walk_forward(
            _report(_agg(total_openings=500),
                    folds=_canary_folds([1.5, 0.4, 0.9, 0.2])),
            min_openings=1,
        )
        assert decision.ok is False
        assert decision.reason == "canary_sharpe_below"
        assert decision.details["canary"]["folds_strong"] == 1

    def test_first_model_validated_with_canary_thresholds(self):
        decision = gate_walk_forward(
            _report(_agg(total_openings=500),
                    folds=_canary_folds([1.5, 1.2, 2.1, 1.0])),
            min_openings=1,
        )
        assert decision.ok is True
        assert decision.reason == "first_model_validated"

    def test_first_model_report_without_folds_rejected(self):
        # Informe malformado sin pliegues: jamás se valida en silencio.
        decision = gate_walk_forward(_report(_agg(total_openings=500)),
                                     min_openings=1)
        assert decision.ok is False
        assert decision.reason == "canary_sharpe_below"

    def test_insufficient_openings_rejected_even_without_baseline(self):
        decision = gate_walk_forward(
            _report(_agg(total_openings=0)), min_openings=1
        )
        assert decision.ok is False
        assert decision.reason == "insufficient_openings"

    def test_non_finite_metrics_rejected(self):
        decision = gate_walk_forward(
            _report(_agg(sum_reward=float("nan"))), min_openings=1
        )
        assert decision.ok is False
        assert decision.reason == "non_finite_metrics"

    # ── P0 G-1: suelo absoluto (plan 2026-09-30) ──────────────────────────

    def test_negative_candidate_blocked_even_vs_worse_baseline(self):
        # Promoción 2026-09-30 03:14: cand(sum_reward=-4.716, sharpe=-3.19,
        # expectancy=-10.26) superó a base(-19.397/-18.72) y se APROBÓ con
        # fold_wins=3/4. Con suelo absoluto: rechazo pese a ganar los 3 checks.
        decision = gate_walk_forward(
            _report(_agg(sum_reward=-4.716, mean_sharpe=-3.19,
                         total_expectancy=-10.26, total_openings=8),
                    _agg(sum_reward=-19.397, mean_sharpe=-18.72),
                    fold_wins=3),
            min_openings=1,
        )
        assert decision.ok is False
        assert decision.reason == "absolute_floor_sum_reward"
        assert decision.details["absolute_floor"]["sum_reward"] is False

    def test_negative_sharpe_blocked_with_positive_reward(self):
        decision = gate_walk_forward(
            _report(_agg(sum_reward=5.0, mean_sharpe=-0.3),
                    _agg(sum_reward=-10.0, mean_sharpe=-2.0),
                    fold_wins=4),
            min_openings=1,
        )
        assert decision.ok is False
        assert decision.reason == "absolute_floor_mean_sharpe"

    def test_negative_expectancy_blocked(self):
        decision = gate_walk_forward(
            _report(_agg(sum_reward=5.0, mean_sharpe=0.8,
                         total_expectancy=-0.5, total_openings=8),
                    _agg(sum_reward=-10.0, mean_sharpe=-2.0),
                    fold_wins=4),
            min_openings=1,
        )
        assert decision.ok is False
        assert decision.reason == "absolute_floor_total_expectancy"

    def test_absolute_floor_applies_to_canary_path_too(self):
        # Sin baseline: pliegues "fuertes" no salvan un agregado no positivo.
        decision = gate_walk_forward(
            _report(_agg(sum_reward=-1.0, total_openings=500),
                    folds=_canary_folds([1.5, 1.2, 2.1, 1.0])),
            min_openings=1,
        )
        assert decision.ok is False
        assert decision.reason == "absolute_floor_sum_reward"


def _fake_df(n=500):
    return pd.DataFrame({"close": np.linspace(1.0, 1.1, n)})


def _fake_eval(reward_per_bar=0.1, model_rewards=None):
    """evaluate_fn determinista: reward proporcional a la longitud del segmento."""
    calls = []

    def fn(model, segment_df, rms):
        n = len(segment_df)
        calls.append((model, n))
        per_bar = (model_rewards or {}).get(model, reward_per_bar)
        return {
            "openings": 2, "trades": 1, "wins": 1, "losses": 0,
            "win_rate": 1.0, "reward": per_bar * n,
            "balance": 1010.0,
            # 3 puntos: np.diff deja 2 retornos → sharpe computable > 0
            # (con [1000, 1010] el sharpe es 0.0 y el suelo G-1 intercepta
            # el gate antes de las reglas comparativas que aquí se prueban).
            "equity_curve": [1000.0, 1005.0, 1010.0],
            "step_rewards": [10.0],
            # R-1: consistente con balance (+10) y win ≥3€ → expectancy +10
            "trade_pnls": [10.0],
        }

    return fn, calls


class TestEvaluateWalkForward:
    def test_candidate_only_runs_four_folds_plus_holdout(self):
        split = _split(500)
        fn, calls = _fake_eval()
        report = evaluate_walk_forward(
            _fake_df(), split,
            candidate="CAND", candidate_obs_rms="rms_c",
            baseline=None, baseline_obs_rms=None,
            evaluate_fn=fn, initial_balance=1000.0,
        )
        assert len(calls) == 5  # 4 folds + holdout (sin baseline)
        assert all(model == "CAND" for model, _ in calls)
        assert report.baseline is None
        assert report.fold_wins is None
        assert len(report.folds) == 4
        assert report.holdout["candidate"]["reward"] > 0

    def test_paired_evaluation_same_segments_for_both_models(self):
        split = _split(500)
        # CAND con mejor reward por barra que BASE → gana todos los folds.
        fn, calls = _fake_eval(model_rewards={"CAND": 0.2, "BASE": 0.1})
        report = evaluate_walk_forward(
            _fake_df(), split,
            candidate="CAND", candidate_obs_rms="rms_c",
            baseline="BASE", baseline_obs_rms="rms_b",
            evaluate_fn=fn, initial_balance=1000.0,
        )
        assert len(calls) == 10  # (4 folds + holdout) × 2
        # emparejado: cada par consecutivo recorre el MISMO segmento
        for i in range(0, 10, 2):
            assert calls[i][1] == calls[i + 1][1]
        assert report.baseline is not None
        assert report.fold_wins == 4  # CAND gana todos los folds (reward mayor)

    def test_aggregate_sum_reward_excludes_holdout(self):
        split = _split(500)
        fn, _ = _fake_eval(reward_per_bar=0.5)
        report = evaluate_walk_forward(
            _fake_df(), split,
            candidate="CAND", candidate_obs_rms=None,
            baseline=None, baseline_obs_rms=None,
            evaluate_fn=fn, initial_balance=1000.0,
        )
        expected = 0.5 * sum(
            len(_fake_df().iloc[f.context_start:f.end]) for f in split.folds
        )
        assert report.candidate["sum_reward"] == pytest.approx(expected)
        # el holdout se reporta aparte y NO entra en el agregado del gate
        assert report.holdout["bars"] == split.holdout.bars

    def test_fold_wins_zero_when_baseline_stronger(self):
        split = _split(500)
        fn, _ = _fake_eval(model_rewards={"CAND": 0.1, "BASE": 0.9})
        report = evaluate_walk_forward(
            _fake_df(), split,
            candidate="CAND", candidate_obs_rms=None,
            baseline="BASE", baseline_obs_rms=None,
            evaluate_fn=fn, initial_balance=1000.0,
        )
        assert report.fold_wins == 0
        decision = gate_walk_forward(report, min_openings=1)
        assert decision.ok is False
        assert "sum_reward_strict" in decision.reason
