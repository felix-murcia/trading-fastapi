"""Test de paridad: walk_forward_gate regla pura."""

from src.domain.rules.walk_forward_gate import walk_forward_gate, GateDecision


def _candidate(sum_reward=1.0, mean_sharpe=1.0, mean_sortino=1.0, worst_max_dd=0.1,
               total_expectancy=0.5, total_openings=100):
    return {
        "sum_reward": sum_reward,
        "mean_sharpe": mean_sharpe,
        "mean_sortino": mean_sortino,
        "worst_max_dd": worst_max_dd,
        "total_expectancy": total_expectancy,
        "total_openings": total_openings,
    }


def test_walk_forward_gate_approved_with_baseline():
    report = {
        "candidate": _candidate(sum_reward=5.0, mean_sharpe=2.0, total_expectancy=1.0),
        "baseline": {"sum_reward": 1.0, "mean_sharpe": 0.5, "total_expectancy": 0.1},
        "fold_wins": 3,
        "n_folds": 4,
        "holdout": {
            "candidate": {"reward": 2.0, "expectancy": 0.5},
            "baseline": {"reward": 1.0, "expectancy": 0.1},
        },
        "folds": [{"candidate": {"sharpe": 2.0}} for _ in range(4)],
    }
    decision = walk_forward_gate(report, min_openings=20)
    assert decision.ok is True
    assert decision.reason == "walk_forward_improved"


def test_walk_forward_gate_rejected_insufficient_openings():
    report = {"candidate": _candidate(total_openings=5), "holdout": {"candidate": {"reward": 1.0, "expectancy": 0.5}}}
    decision = walk_forward_gate(report, min_openings=20)
    assert decision.ok is False
    assert decision.reason == "insufficient_openings"


def test_walk_forward_gate_rejected_absolute_floor():
    report = {
        "candidate": _candidate(sum_reward=-1.0, mean_sharpe=2.0, total_expectancy=1.0),
        "holdout": {"candidate": {"reward": 2.0, "expectancy": 0.5}},
    }
    decision = walk_forward_gate(report, min_openings=20)
    assert decision.ok is False
    assert decision.reason.startswith("absolute_floor")


def test_walk_forward_gate_rejected_holdout_floor():
    report = {
        "candidate": _candidate(sum_reward=5.0, mean_sharpe=2.0, total_expectancy=1.0),
        "holdout": {"candidate": {"reward": -1.0, "expectancy": 0.5}},
    }
    decision = walk_forward_gate(report, min_openings=20)
    assert decision.ok is False
    assert decision.reason.startswith("holdout_floor")


def test_walk_forward_gate_first_model_validated():
    report = {
        "candidate": _candidate(sum_reward=5.0, mean_sharpe=2.0, total_expectancy=1.0, total_openings=300),
        "baseline": None,
        "fold_wins": None,
        "n_folds": 4,
        "holdout": {"candidate": {"reward": 2.0, "expectancy": 0.5}},
        "folds": [{"candidate": {"sharpe": 2.0}} for _ in range(4)],
    }
    decision = walk_forward_gate(report, min_openings=20)
    assert decision.ok is True
    assert decision.reason == "first_model_validated"


def test_walk_forward_gate_canary_sharpe_below():
    report = {
        "candidate": _candidate(sum_reward=5.0, mean_sharpe=2.0, total_expectancy=1.0, total_openings=300),
        "baseline": None,
        "fold_wins": None,
        "n_folds": 4,
        "holdout": {"candidate": {"reward": 2.0, "expectancy": 0.5}},
        "folds": [{"candidate": {"sharpe": 0.5}} for _ in range(4)],  # todos debajo de 1.0
    }
    decision = walk_forward_gate(report, min_openings=20)
    assert decision.ok is False
    assert decision.reason == "canary_sharpe_below"
