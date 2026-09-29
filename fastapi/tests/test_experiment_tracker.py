"""
P3-2a: services/experiment_tracker.py — una fila consultable por corrida de retrain.
"""
import json
from unittest.mock import AsyncMock

import pytest


@pytest.fixture
def pool(monkeypatch):
    """Pool falso: fetchval devuelve el id de la fila insertada."""
    from unittest.mock import MagicMock

    p = MagicMock()
    p.fetchval = AsyncMock(return_value=7)
    p.execute = AsyncMock(return_value="UPDATE 1")
    monkeypatch.setattr("db.connection.get_pool", lambda: p)
    return p


class TestStartExperiment:
    @pytest.mark.asyncio
    async def test_inserts_running_row_and_returns_id(self, pool):
        from services import experiment_tracker

        exp_id = await experiment_tracker.start_experiment(
            "scheduled", "/snap/a.csv.gz", "a" * 64, symbol="EURUSD"
        )

        assert exp_id == 7
        sql = pool.fetchval.await_args.args[0]
        assert "INSERT INTO training_experiments" in sql
        assert "RETURNING id" in sql
        assert pool.fetchval.await_args.args[1:] == (
            "scheduled", "EURUSD", "/snap/a.csv.gz", "a" * 64,
        )

    @pytest.mark.asyncio
    async def test_invalid_trigger_raises_before_touching_db(self, pool):
        from services import experiment_tracker

        with pytest.raises(ValueError, match="triggered_by"):
            await experiment_tracker.start_experiment("random_trigger", symbol="EURUSD")

        pool.fetchval.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_snapshot_optional(self, pool):
        from services import experiment_tracker

        # symbol es OBLIGATORIO (P3-1c): todo experimento declara su mundo.
        await experiment_tracker.start_experiment("manual", symbol="EURUSD")

        assert pool.fetchval.await_args.args[1:] == ("manual", "EURUSD", None, None)


class TestFinishExperiment:
    @pytest.mark.asyncio
    async def test_updates_with_running_guard_and_serializes_metrics(self, pool):
        from services import experiment_tracker

        await experiment_tracker.finish_experiment(
            7, "promoted",
            gate_reason="ok", gate_metrics={"n_folds": 4},
            model_sha256="b" * 64,
        )

        args = pool.execute.await_args.args
        sql = args[0]
        assert "UPDATE training_experiments" in sql
        # Guarda anti-pisado: un segundo cierre no pisa el primero.
        assert "status = 'running'" in sql
        assert args[1] == 7
        assert args[2] == "promoted"
        assert args[3] == "ok"
        assert json.loads(args[4]) == {"n_folds": 4}
        assert args[5] == "b" * 64

    @pytest.mark.asyncio
    async def test_invalid_status_raises_before_touching_db(self, pool):
        from services import experiment_tracker

        with pytest.raises(ValueError, match="status"):
            await experiment_tracker.finish_experiment(7, "running")

        pool.execute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_zero_rows_is_logged_noop_not_error(self, monkeypatch):
        """UPDATE 0 (ya cerrado por otro flujo) → no-op, no excepción."""
        from unittest.mock import MagicMock

        from services import experiment_tracker

        p = MagicMock()
        p.execute = AsyncMock(return_value="UPDATE 0")
        monkeypatch.setattr("db.connection.get_pool", lambda: p)

        await experiment_tracker.finish_experiment(1, "failed", error="x")

        p.execute.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_finish_allows_null_gate_fields(self, pool):
        from services import experiment_tracker

        await experiment_tracker.finish_experiment(7, "failed", error="boom")

        args = pool.execute.await_args.args
        assert args[2] == "failed"
        assert args[3] is None and args[4] is None and args[5] is None
        assert args[6] == "boom"
