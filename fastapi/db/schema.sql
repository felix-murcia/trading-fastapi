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

-- Cola de órdenes offline/deferred (resilience para MT5 desconectado)
CREATE TABLE IF NOT EXISTS deferred_orders (
    id           BIGSERIAL PRIMARY KEY,
    cycle_id     TEXT NOT NULL,
    symbol       TEXT NOT NULL,
    order_type   TEXT NOT NULL CHECK (order_type IN ('BUY', 'SELL')),
    volume       NUMERIC(6,3)  NOT NULL,
    entry_price  NUMERIC(10,5) NOT NULL,
    stop_loss    NUMERIC(10,5) NOT NULL,
    take_profit  NUMERIC(10,5) NOT NULL,
    comment      TEXT,
    status       TEXT NOT NULL CHECK (status IN ('pending', 'processing', 'failed', 'placed')),
    retry_count  INTEGER NOT NULL DEFAULT 0,
    last_error   TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_deferred_pending
    ON deferred_orders(created_at)
    WHERE status = 'pending';

-- Trade outcomes para auto-retrain y métricas
CREATE TABLE IF NOT EXISTS trade_outcomes (
    id           BIGSERIAL PRIMARY KEY,
    symbol       TEXT NOT NULL,
    direction    TEXT NOT NULL CHECK (direction IN ('LONG', 'SHORT')),
    entry_time   TIMESTAMPTZ NOT NULL,
    exit_time    TIMESTAMPTZ NOT NULL,
    pnl          NUMERIC(12,4) NOT NULL,
    pnl_pct      NUMERIC(8,4) NOT NULL,
    exit_reason  TEXT NOT NULL CHECK (exit_reason IN ('sl', 'tp', 'manual', 'news', 'signal_end', 'ea')),
    sl_hit      BOOLEAN NOT NULL DEFAULT FALSE,
    tp_hit      BOOLEAN NOT NULL DEFAULT FALSE,
    deal_ticket  BIGINT,
    position_id  BIGINT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Dedup: un mismo deal de MT5 solo puede registrarse una vez (webhooks reintentados / backfill EA)
CREATE UNIQUE INDEX IF NOT EXISTS idx_trade_outcomes_deal_ticket
    ON trade_outcomes(deal_ticket)
    WHERE deal_ticket IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_trade_outcomes_symbol ON trade_outcomes(symbol);
CREATE INDEX IF NOT EXISTS idx_trade_outcomes_exit_time ON trade_outcomes(exit_time);

-- P1-2: historial de bundles promovidos — qué modelo corre, por qué, cuándo
CREATE TABLE IF NOT EXISTS model_registry (
    id              BIGSERIAL PRIMARY KEY,
    model_sha256    TEXT        NOT NULL,
    vec_norm_sha256 TEXT,
    source          TEXT        NOT NULL,
    obs_shape       JSONB       NOT NULL,
    extra           JSONB,
    gate_reason     TEXT,
    promoted_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    is_active       BOOLEAN     NOT NULL DEFAULT TRUE
);

-- Un único bundle activo a la vez (rollback informado)
CREATE UNIQUE INDEX IF NOT EXISTS idx_model_registry_one_active
    ON model_registry(is_active)
    WHERE is_active;

CREATE INDEX IF NOT EXISTS idx_model_registry_promoted_at
    ON model_registry(promoted_at DESC);

-- P3-2: experimentos de entrenamiento — cada retrain deja una fila consultable
-- (disparador, snapshot de datos, decisión del gate, bundle promovido o error).
-- Antes solo existían eventos sueltos en audit_log sin fila por corrida.
CREATE TABLE IF NOT EXISTS training_experiments (
    id               BIGSERIAL PRIMARY KEY,
    triggered_by     TEXT NOT NULL CHECK (triggered_by IN ('manual', 'forced', 'approved', 'trade_count', 'scheduled')),
    status           TEXT NOT NULL DEFAULT 'running' CHECK (status IN ('running', 'promoted', 'rejected', 'failed')),
    snapshot_path    TEXT,
    snapshot_sha256  TEXT,
    started_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at      TIMESTAMPTZ,
    gate_reason      TEXT,
    gate_metrics     JSONB,
    model_sha256     TEXT,
    error            TEXT
);

CREATE INDEX IF NOT EXISTS idx_training_experiments_started
    ON training_experiments(started_at DESC);
