-- Idempotencia: un cycle_id = un ciclo. No se reprocesa.
CREATE TABLE IF NOT EXISTS cycles (
    cycle_id     TEXT PRIMARY KEY,
    status       TEXT NOT NULL CHECK (status IN ('processing', 'skipped', 'executed', 'rejected', 'error')),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    skip_reason  TEXT,
    pair         TEXT,
    action       TEXT
);

-- Órdenes enviadas a MT5
CREATE TABLE IF NOT EXISTS orders (
    id           BIGSERIAL PRIMARY KEY,
    cycle_id     TEXT NOT NULL REFERENCES cycles(cycle_id),
    symbol       TEXT NOT NULL,
    type         TEXT NOT NULL CHECK (type IN ('BUY', 'SELL')),
    entry        NUMERIC(10,5) NOT NULL,
    sl           NUMERIC(10,5) NOT NULL,
    tp           NUMERIC(10,5) NOT NULL,
    volume       NUMERIC(6,3)  NOT NULL,
    mt5_order_id TEXT,
    status       TEXT NOT NULL CHECK (status IN ('pending', 'placed', 'filled', 'rejected', 'unconfirmed', 'cancelled')),
    fill_price   NUMERIC(10,5),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    confirmed_at TIMESTAMPTZ
);

-- Índice para detección rápida de duplicados activos
CREATE INDEX IF NOT EXISTS idx_orders_active
    ON orders(symbol, entry)
    WHERE status IN ('pending', 'placed');

-- Auditoría append-only: cada evento del ciclo se registra aquí
CREATE TABLE IF NOT EXISTS audit_log (
    id         BIGSERIAL PRIMARY KEY,
    cycle_id   TEXT NOT NULL,
    event      TEXT NOT NULL,
    data       JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Índice para consultas de auditoría por cycle_id
CREATE INDEX IF NOT EXISTS idx_audit_cycle ON audit_log(cycle_id);
