-- Señal activa por símbolo (upsert desde el EA SignalBridge)
CREATE TABLE IF NOT EXISTS smc_signals (
    symbol      TEXT PRIMARY KEY,
    entry_zone  BOOLEAN NOT NULL DEFAULT FALSE,
    direction   TEXT,
    zone_high   NUMERIC(12,5),
    zone_low    NUMERIC(12,5),
    timeframe   TEXT,
    source      TEXT DEFAULT 'crystal_liquidity',
    received_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Órdenes enviadas a MT5
CREATE TABLE IF NOT EXISTS orders (
    id           BIGSERIAL PRIMARY KEY,
    cycle_id     TEXT NOT NULL,
    symbol       TEXT NOT NULL,
    type         TEXT NOT NULL CHECK (type IN ('BUY', 'SELL')),
    entry        NUMERIC(10,5) NOT NULL,
    sl           NUMERIC(10,5) NOT NULL,
    tp           NUMERIC(10,5) NOT NULL,
    volume       NUMERIC(6,3)  NOT NULL,
    mt5_order_id TEXT,
    status       TEXT NOT NULL CHECK (status IN ('pending', 'placed', 'filled', 'rejected', 'unconfirmed', 'cancelled')),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_orders_active
    ON orders(symbol, entry)
    WHERE status IN ('pending', 'placed');

-- Auditoría append-only
CREATE TABLE IF NOT EXISTS audit_log (
    id         BIGSERIAL PRIMARY KEY,
    cycle_id   TEXT NOT NULL,
    event      TEXT NOT NULL,
    data       JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_audit_cycle ON audit_log(cycle_id);
