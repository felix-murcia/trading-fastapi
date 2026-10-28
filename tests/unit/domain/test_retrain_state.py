"""Test de paridad: RetrainState value object."""

from src.domain.rules.retrain_state import RetrainState


def test_retrain_state_defaults():
    s = RetrainState()
    assert s.filled_count == 0
    assert s.last_retrain_time == 0.0
    assert s.outcomes == ()
    assert s.retrain_in_progress is False
    assert s.scheduled_due is False
    assert s.schedule_anchor == 0.0


def test_retrain_state_with_values():
    s = RetrainState(
        filled_count=7,
        last_retrain_time=1700000000.0,
        outcomes=(("EURUSD", 1.0),),
        retrain_in_progress=True,
        scheduled_due=True,
        schedule_anchor=1699990000.0,
    )
    assert s.filled_count == 7
    assert s.last_retrain_time == 1700000000.0
    assert s.retrain_in_progress is True
    assert s.scheduled_due is True
    assert s.schedule_anchor == 1699990000.0


def test_retrain_state_frozen():
    s = RetrainState()
    try:
        s.filled_count = 99  # type: ignore[misc]
        assert False, "debería ser frozen"
    except Exception:
        pass
