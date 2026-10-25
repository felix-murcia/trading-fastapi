from unittest.mock import AsyncMock, MagicMock

import pytest

from services import broker_clock_store, timebase


@pytest.mark.asyncio
async def test_restore_broker_offset_marks_persisted_value_as_validated(monkeypatch):
    pool = MagicMock()
    pool.execute = AsyncMock()
    pool.fetchrow = AsyncMock(return_value={"offset_h": 3, "source": "endpoint_precio"})
    monkeypatch.setattr(broker_clock_store, "get_pool", lambda: pool)
    monkeypatch.setattr(timebase, "_KNOWN_OFFSET", {
        "offset_h": 0, "source": "sin_detecccion", "validated": False,
    })

    restored = await broker_clock_store.restore_broker_offset()

    assert restored is True
    assert timebase.current_broker_offset() == 3
    assert timebase.broker_offset_is_validated()
    assert timebase.broker_clock_ready()
    pool.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_restore_without_persisted_offset_leaves_clock_unvalidated(monkeypatch):
    pool = MagicMock()
    pool.execute = AsyncMock()
    pool.fetchrow = AsyncMock(return_value=None)
    monkeypatch.setattr(broker_clock_store, "get_pool", lambda: pool)
    monkeypatch.setattr(timebase, "_KNOWN_OFFSET", {
        "offset_h": 0, "source": "sin_detecccion", "validated": False,
    })

    restored = await broker_clock_store.restore_broker_offset()

    assert restored is False
    assert not timebase.broker_offset_is_validated()


@pytest.mark.asyncio
async def test_persist_rejects_implausible_offset_before_database_write(monkeypatch):
    pool = MagicMock()
    pool.execute = AsyncMock()
    monkeypatch.setattr(broker_clock_store, "get_pool", lambda: pool)

    with pytest.raises(ValueError, match="outside the valid range"):
        await broker_clock_store.persist_broker_offset(13, "endpoint_precio")

    pool.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_persist_writes_validated_offset_and_source(monkeypatch):
    pool = MagicMock()
    pool.execute = AsyncMock()
    monkeypatch.setattr(broker_clock_store, "get_pool", lambda: pool)
    monkeypatch.setattr(timebase, "_KNOWN_OFFSET", {
        "offset_h": 2, "source": "endpoint_precio", "validated": True,
        "persisted": False,
    })

    await broker_clock_store.persist_broker_offset(2, "endpoint_precio")

    query, offset_h, source = pool.execute.await_args.args
    assert "INSERT INTO broker_clock_state" in query
    assert (offset_h, source) == (2, "endpoint_precio")
    assert timebase.broker_clock_ready()
