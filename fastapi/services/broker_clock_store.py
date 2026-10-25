"""Persist and restore the last validated broker clock offset."""
from db.connection import get_pool
from services.timebase import set_broker_offset

_CREATE_TABLE = """
    CREATE TABLE IF NOT EXISTS broker_clock_state (
        id SMALLINT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
        offset_h SMALLINT NOT NULL CHECK (offset_h BETWEEN -12 AND 12),
        source TEXT NOT NULL,
        validated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
"""

_UPSERT = """
    INSERT INTO broker_clock_state (id, offset_h, source)
    VALUES (1, $1, $2)
    ON CONFLICT (id) DO UPDATE
        SET offset_h = $1, source = $2, validated_at = NOW()
"""


async def restore_broker_offset() -> bool:
    """Create the state table and restore its validated offset, if present."""
    pool = get_pool()
    await pool.execute(_CREATE_TABLE)
    row = await pool.fetchrow(
        "SELECT offset_h, source FROM broker_clock_state WHERE id = 1"
    )
    if row is None:
        return False

    offset_h = int(row["offset_h"])
    if abs(offset_h) > 12:
        raise ValueError(f"Persisted broker offset outside the valid range: {offset_h}")
    set_broker_offset(offset_h, source=f"persisted:{row['source']}")
    return True


async def persist_broker_offset(offset_h: int, source: str) -> None:
    """Persist a broker offset only after the caller validated its source."""
    offset_h = int(offset_h)
    if abs(offset_h) > 12:
        raise ValueError(f"Broker offset outside the valid range: {offset_h}")
    await get_pool().execute(_UPSERT, offset_h, source)
