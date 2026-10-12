-- GoldenTrinity Database Schema
-- Run once against TimescaleDB after containers start
-- v2: added qty to position_log, dxy_12m_mean to macro_snapshots

CREATE TABLE IF NOT EXISTS macro_snapshots (
    timestamp           TIMESTAMPTZ NOT NULL,
    cpi_yoy             NUMERIC(8,4),
    fed_funds_rate      NUMERIC(8,4),
    real_rate           NUMERIC(8,4),
    dxy                 NUMERIC(10,4),
    dxy_12m_mean        NUMERIC(10,4),
    vix                 NUMERIC(8,4),
    t10y2y              NUMERIC(8,4),
    gvz                 NUMERIC(8,4),
    gvz_vix_ratio       NUMERIC(8,4),
    news_sentiment      NUMERIC(6,4),
    regime              VARCHAR(10) NOT NULL,
    mm_cot_percentile   NUMERIC(6,2),
    comm_cot_percentile NUMERIC(6,2)
);
SELECT create_hypertable('macro_snapshots', 'timestamp', if_not_exists => TRUE);

CREATE TABLE IF NOT EXISTS signal_log (
    timestamp         TIMESTAMPTZ NOT NULL,
    asset             VARCHAR(20),
    direction         VARCHAR(10),
    confidence_up     NUMERIC(6,4),
    confidence_flat   NUMERIC(6,4),
    confidence_down   NUMERIC(6,4),
    regime            VARCHAR(10),
    session           VARCHAR(20),
    passed_gate       BOOLEAN,
    drop_reason       VARCHAR(100)
);
SELECT create_hypertable('signal_log', 'timestamp', if_not_exists => TRUE);

CREATE TABLE IF NOT EXISTS position_log (
    id                    SERIAL PRIMARY KEY,
    opened_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    closed_at             TIMESTAMPTZ,
    asset                 VARCHAR(20) NOT NULL,
    direction             VARCHAR(10) NOT NULL,
    entry_price           NUMERIC(12,4),
    fill_price            NUMERIC(12,4),
    exit_price            NUMERIC(12,4),
    qty                   NUMERIC(12,4),
    size_pct              NUMERIC(8,4),
    stop_loss             NUMERIC(12,4),
    take_profit           NUMERIC(12,4),
    expiry_at             TIMESTAMPTZ,
    regime_at_entry       VARCHAR(10),
    confidence_at_entry   NUMERIC(6,4),
    session               VARCHAR(20),
    slippage_pct          NUMERIC(8,4),
    exit_type             VARCHAR(30),
    time_in_trade_min     NUMERIC(10,2),
    pnl_pct               NUMERIC(10,4),
    broker                VARCHAR(20)
);

-- Index for fast exit queries and daily report queries
CREATE INDEX IF NOT EXISTS idx_position_log_opened_at ON position_log (opened_at DESC);
CREATE INDEX IF NOT EXISTS idx_position_log_closed_at ON position_log (closed_at DESC) WHERE closed_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_position_log_open ON position_log (broker, asset) WHERE closed_at IS NULL;

CREATE TABLE IF NOT EXISTS risk_alert_log (
    timestamp     TIMESTAMPTZ NOT NULL,
    alert_type    VARCHAR(50) NOT NULL,
    details       JSONB,
    action_taken  TEXT
);
SELECT create_hypertable('risk_alert_log', 'timestamp', if_not_exists => TRUE);

CREATE TABLE IF NOT EXISTS health_checks (
    timestamp   TIMESTAMPTZ NOT NULL,
    service     VARCHAR(30) NOT NULL,
    status      VARCHAR(20) NOT NULL,
    latency_ms  NUMERIC(10,2)
);
SELECT create_hypertable('health_checks', 'timestamp', if_not_exists => TRUE);

CREATE TABLE IF NOT EXISTS daily_reports (
    date              DATE PRIMARY KEY,
    total_pnl_pct     NUMERIC(10,4),
    win_rate          NUMERIC(6,4),
    trade_count       INTEGER,
    avg_confidence    NUMERIC(6,4),
    avg_slippage_pct  NUMERIC(8,4),
    sharpe_30d        NUMERIC(8,4),
    bullish_pnl_pct   NUMERIC(10,4),
    neutral_pnl_pct   NUMERIC(10,4),
    bearish_pnl_pct   NUMERIC(10,4),
    high_conf_pnl     NUMERIC(10,4),
    med_conf_pnl      NUMERIC(10,4),
    overlap_pnl       NUMERIC(10,4),
    asian_pnl         NUMERIC(10,4),
    stop_loss_exits   INTEGER,
    take_profit_exits INTEGER,
    reversal_exits    INTEGER,
    expiry_exits      INTEGER,
    regime_exits      INTEGER
);

CREATE TABLE IF NOT EXISTS cot_snapshots (
    date                  DATE PRIMARY KEY,
    managed_money_net     BIGINT,
    commercial_net        BIGINT,
    mm_percentile_52w     NUMERIC(6,2),
    comm_percentile_52w   NUMERIC(6,2),
    mm_signal             VARCHAR(20),
    comm_signal           VARCHAR(20)
);

-- ── Migration: add missing columns to existing installs ───────────────────
-- Safe to run on an existing DB — IF NOT EXISTS guards prevent duplicates.
-- These columns were added in v2 and won't exist on installs created before the update.

DO $$
BEGIN
    -- Add qty to position_log if missing
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name='position_log' AND column_name='qty'
    ) THEN
        ALTER TABLE position_log ADD COLUMN qty NUMERIC(12,4);
        RAISE NOTICE 'Added qty column to position_log';
    END IF;

    -- Add dxy_12m_mean to macro_snapshots if missing
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name='macro_snapshots' AND column_name='dxy_12m_mean'
    ) THEN
        ALTER TABLE macro_snapshots ADD COLUMN dxy_12m_mean NUMERIC(10,4);
        RAISE NOTICE 'Added dxy_12m_mean column to macro_snapshots';
    END IF;

    -- Add SPX 20-day return column
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name='macro_snapshots' AND column_name='spx_20d_return'
    ) THEN
        ALTER TABLE macro_snapshots ADD COLUMN spx_20d_return NUMERIC(10,6);
        RAISE NOTICE 'Added spx_20d_return column to macro_snapshots';
    END IF;

    -- Add AUD/USD 20-day return column
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name='macro_snapshots' AND column_name='audusd_20d_return'
    ) THEN
        ALTER TABLE macro_snapshots ADD COLUMN audusd_20d_return NUMERIC(10,6);
        RAISE NOTICE 'Added audusd_20d_return column to macro_snapshots';
    END IF;
END
$$;

