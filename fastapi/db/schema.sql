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

-- P2-4: estado de riesgo persistente (la protección sobrevive a reinicios)
-- Cuenta: baseline del día (daily_start) + peak — fila única.
CREATE TABLE IF NOT EXISTS risk_account_state (
    id                 SMALLINT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    day                DATE NOT NULL,
    daily_start_equity DOUBLE PRECISION NOT NULL,
    peak_equity        DOUBLE PRECISION NOT NULL,
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Par: racha de pérdidas (convención >= 3 EUR) + cooldown de alertas.
CREATE TABLE IF NOT EXISTS risk_pair_state (
    symbol             TEXT PRIMARY KEY,
    consecutive_losses INTEGER NOT NULL DEFAULT 0,
    last_alert_ts      DOUBLE PRECISION NOT NULL DEFAULT 0,
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Trade outcomes para auto-retrain y métricas
--
-- DOMINIO DE LAS MARCAS (T-31.9, 2026-10-20): `entry_time` y `exit_time`
-- se guardan en el RELOJ DEL BRÓKER (el EA reporta TimeCurrent / el tiempo
-- del deal; medido +3 h respecto al UTC real, +2 h tras el cambio de
-- horario del 25-10-2026). Se guarda la marca EN BRUTO (sin false
-- convertir) ⇒ quien agrupe por día o las compare con velas debe
-- convertirla antes (services.timebase). `created_at` sí es UTC real
-- (NOW() de Postgres).
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
    -- P3-1c: mundo al que pertenece el bundle (cada par certifica el suyo)
    symbol          TEXT        NOT NULL DEFAULT 'EURUSD',
    model_sha256    TEXT        NOT NULL,
    vec_norm_sha256 TEXT,
    source          TEXT        NOT NULL,
    obs_shape       JSONB       NOT NULL,
    extra           JSONB,
    gate_reason     TEXT,
    promoted_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    is_active       BOOLEAN     NOT NULL DEFAULT TRUE
);

-- Índice global previo a P3-1c: un único activo ENTRE todos los pares.
-- Se elimina a favor del índice por par (los mundos son independientes).
DROP INDEX IF EXISTS idx_model_registry_one_active;

-- Un único bundle activo POR PAR (P3-1c): la unicidad es por mundo.
CREATE UNIQUE INDEX IF NOT EXISTS idx_model_registry_one_active_per_symbol
    ON model_registry(symbol)
    WHERE is_active;

CREATE INDEX IF NOT EXISTS idx_model_registry_promoted_at
    ON model_registry(promoted_at DESC);

-- P3-2: experimentos de entrenamiento — cada retrain deja una fila consultable
-- (disparador, snapshot de datos, decisión del gate, bundle promovido o error).
-- Antes solo existían eventos sueltos en audit_log sin fila por corrida.
CREATE TABLE IF NOT EXISTS training_experiments (
    id               BIGSERIAL PRIMARY KEY,
    -- P3-1c: mundo entrenado por este experimento
    symbol           TEXT NOT NULL DEFAULT 'EURUSD',
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

-- P3-1c: máquina de estado del retrain, como clave-valor. Cada par tiene SU
-- namespace de claves: `retrain_state.<SYMBOL>.<campo>` (filled_count,
-- last_retrain_time, scheduled_due, schedule_anchor) — los contadores de un
-- par nunca alimentan el disparo de otro.
CREATE TABLE IF NOT EXISTS retrain_state (
    id         SERIAL PRIMARY KEY,
    key        TEXT        NOT NULL UNIQUE,
    value      JSONB       NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Curva de equity mark-to-market (2026-10-21): el DD del 30,2 % del 20-10 era
-- mark-to-market y no tenía serie almacenada; el reconstruido sólo con cierres
-- daba 15,49 %. Una foto por minuto permite recalcular el DD con marcas propias.
CREATE TABLE IF NOT EXISTS equity_snapshots (
    id       BIGSERIAL PRIMARY KEY,
    ts       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    equity   NUMERIC(12,2) NOT NULL,
    balance  NUMERIC(12,2) NOT NULL,
    floating NUMERIC(12,2) NOT NULL,
    source   TEXT NOT NULL DEFAULT 'mt5_account_info'
);

CREATE INDEX IF NOT EXISTS idx_equity_snapshots_ts
    ON equity_snapshots(ts);
