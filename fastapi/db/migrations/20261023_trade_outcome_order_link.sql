BEGIN;

ALTER TABLE trade_outcomes
    ADD COLUMN IF NOT EXISTS order_db_id BIGINT;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'fk_trade_outcomes_order_db_id'
          AND conrelid = 'trade_outcomes'::regclass
    ) THEN
        ALTER TABLE trade_outcomes
            ADD CONSTRAINT fk_trade_outcomes_order_db_id
            FOREIGN KEY (order_db_id) REFERENCES orders(id);
    END IF;
END;
$$;

CREATE INDEX IF NOT EXISTS idx_trade_outcomes_order_db_id
    ON trade_outcomes(order_db_id);

ALTER TABLE orders
    ADD COLUMN IF NOT EXISTS mt5_position_id BIGINT;

CREATE INDEX IF NOT EXISTS idx_orders_mt5_position_id
    ON orders(mt5_position_id);

COMMIT;
