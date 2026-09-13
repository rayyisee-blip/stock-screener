-- =============================================================================
-- Stock Screener - Database Schema
-- =============================================================================
-- Run this file once against a fresh Supabase Postgres project (Supabase SQL
-- Editor -> paste this whole file -> Run). It creates every table the app
-- needs. There is no ORM and no migration tool (e.g. Alembic) here on
-- purpose - plain SQL is easier for a beginner to read top to bottom.
--
-- Design notes for a Python beginner reading this for the first time:
--   * "PRIMARY KEY" means "the column(s) that uniquely identify a row".
--   * "UNIQUE(a, b)" means "no two rows may have the same (a, b) pair". We
--     use this on (ticker, date) tables so that re-running an ingest job
--     twice for the same day UPDATES the existing row instead of creating a
--     duplicate ("upsert" - see ingest/db.py for how that works in Python).
--   * "REFERENCES universe(ticker)" is a foreign key: it stops us from
--     inserting price/news/rating rows for a ticker that was never added to
--     the universe table in the first place.
--   * "TIMESTAMPTZ" stores a timestamp WITH timezone info, always as UTC
--     internally. The Streamlit app converts to Singapore time for display.
-- =============================================================================

-- -----------------------------------------------------------------------------
-- universe: every ticker the screener knows about (index constituents,
-- curated ETFs, indices, plus anything a user manually adds via the UI).
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS universe (
    ticker          TEXT PRIMARY KEY,        -- yfinance-style ticker, e.g. 'AAPL', 'SAP.DE', '600519.SS'
    name            TEXT,                    -- human-readable company/fund/index name
    market          TEXT NOT NULL,           -- 'US', 'EU', 'CN', 'HK', 'TW', 'SG'
    sector          TEXT,                    -- GICS-style sector, filled in from yfinance metadata where available
    market_cap      NUMERIC,                 -- latest known market cap, in the instrument's local currency
    is_index        BOOLEAN NOT NULL DEFAULT FALSE,   -- true for things like ^GSPC, ^STI
    is_etf          BOOLEAN NOT NULL DEFAULT FALSE,   -- true for the curated ETF list
    source_index    TEXT,                    -- which seed list this came from, e.g. 'SP500', 'NASDAQ100', 'USER_ADDED'
    added_by_user   BOOLEAN NOT NULL DEFAULT FALSE,   -- true if a user typed this ticker into the UI
    active          BOOLEAN NOT NULL DEFAULT TRUE,    -- set to false instead of deleting a ticker that stops working
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- -----------------------------------------------------------------------------
-- ohlcv: daily Open/High/Low/Close/Volume bars, one row per ticker per day.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ohlcv (
    id          BIGSERIAL PRIMARY KEY,
    ticker      TEXT NOT NULL REFERENCES universe(ticker) ON DELETE CASCADE,
    date        DATE NOT NULL,
    open        NUMERIC,
    high        NUMERIC,
    low         NUMERIC,
    close       NUMERIC,
    adj_close   NUMERIC,                 -- close price adjusted for splits/dividends; used for % change math
    volume      BIGINT,
    source      TEXT NOT NULL DEFAULT 'yfinance',  -- 'yfinance' or 'stooq' (fallback) - see ingest/prices.py
    fetched_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (ticker, date)                 -- required by the acceptance criteria: no duplicate bars
);

-- One index to make "give me the last N days for this ticker" queries fast.
CREATE INDEX IF NOT EXISTS idx_ohlcv_ticker_date ON ohlcv (ticker, date DESC);

-- -----------------------------------------------------------------------------
-- indicators: one row of technical indicators per ticker per day.
-- Computed in ingest/indicators.py from the ohlcv table above.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS indicators (
    id                  BIGSERIAL PRIMARY KEY,
    ticker              TEXT NOT NULL REFERENCES universe(ticker) ON DELETE CASCADE,
    date                DATE NOT NULL,

    -- Trend
    sma_20              NUMERIC,
    sma_50              NUMERIC,
    sma_200             NUMERIC,
    ema_12              NUMERIC,
    ema_26              NUMERIC,
    price_vs_sma20_pct  NUMERIC,     -- (close - sma_20) / sma_20 * 100
    price_vs_sma50_pct  NUMERIC,
    price_vs_sma200_pct NUMERIC,
    ma_cross_state      TEXT,        -- 'golden_cross', 'death_cross', or 'none' (sma_50 vs sma_200)
    adx_14              NUMERIC,

    -- Momentum
    rsi_14              NUMERIC,
    macd_line           NUMERIC,
    macd_signal         NUMERIC,
    macd_hist           NUMERIC,
    stoch_k             NUMERIC,
    stoch_d             NUMERIC,
    roc_10              NUMERIC,

    -- Volatility
    bb_upper            NUMERIC,     -- Bollinger upper band (20, 2)
    bb_mid              NUMERIC,     -- Bollinger middle band = SMA 20
    bb_lower            NUMERIC,     -- Bollinger lower band
    bb_pct_b            NUMERIC,     -- where price sits within the bands, 0 = lower band, 1 = upper band
    bb_bandwidth        NUMERIC,     -- (upper - lower) / mid, a measure of how "squeezed" the bands are
    atr_14              NUMERIC,

    -- Volume
    obv                 NUMERIC,     -- On-Balance Volume (running total)
    volume_vs_avg20     NUMERIC,     -- today's volume / 20-day average volume

    -- Position within the 52-week range
    pct_from_52w_high   NUMERIC,
    pct_from_52w_low    NUMERIC,

    computed_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (ticker, date)
);

CREATE INDEX IF NOT EXISTS idx_indicators_ticker_date ON indicators (ticker, date DESC);

-- -----------------------------------------------------------------------------
-- ratings: the composite Buy/Sell rating and its five bucket scores.
-- Computed in ingest/rating.py from the indicators + news tables.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ratings (
    id                  BIGSERIAL PRIMARY KEY,
    ticker              TEXT NOT NULL REFERENCES universe(ticker) ON DELETE CASCADE,
    date                DATE NOT NULL,

    trend_score         NUMERIC NOT NULL,   -- -100..+100
    momentum_score      NUMERIC NOT NULL,   -- -100..+100
    volatility_score    NUMERIC NOT NULL,   -- -100..+100
    volume_score        NUMERIC NOT NULL,   -- -100..+100
    news_score          NUMERIC NOT NULL,   -- -100..+100

    composite_score     NUMERIC NOT NULL,   -- weighted mean of the five bucket scores above
    label               TEXT NOT NULL,      -- 'Strong Buy' | 'Buy' | 'Neutral' | 'Sell' | 'Strong Sell'
    low_news_coverage   BOOLEAN NOT NULL DEFAULT FALSE,  -- true if fewer than 3 headlines were found in the last 72h

    computed_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (ticker, date)
);

CREATE INDEX IF NOT EXISTS idx_ratings_ticker_date ON ratings (ticker, date DESC);

-- -----------------------------------------------------------------------------
-- news: individual headlines matched to a ticker, with VADER sentiment.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS news (
    id                  BIGSERIAL PRIMARY KEY,
    ticker              TEXT NOT NULL REFERENCES universe(ticker) ON DELETE CASCADE,
    headline            TEXT NOT NULL,
    url                 TEXT NOT NULL,
    source              TEXT,               -- 'yahoo_rss', 'google_news_rss', or 'gdelt'
    published_at        TIMESTAMPTZ,        -- when the article was published (UTC)
    sentiment_compound  NUMERIC,            -- VADER compound score, -1.0 (very negative) to +1.0 (very positive)
    fetched_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (ticker, url)                    -- avoid storing the same article twice for the same ticker
);

CREATE INDEX IF NOT EXISTS idx_news_ticker_published ON news (ticker, published_at DESC);

-- -----------------------------------------------------------------------------
-- watchlist: tickers a user has chosen to track closely. The Streamlit app
-- is the only thing that writes here (it is otherwise read-only).
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS watchlist (
    id          BIGSERIAL PRIMARY KEY,
    ticker      TEXT NOT NULL REFERENCES universe(ticker) ON DELETE CASCADE,
    added_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (ticker)
);
