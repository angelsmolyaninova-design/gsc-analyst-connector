-- GSC Analyst — initial schema

CREATE EXTENSION IF NOT EXISTS "pgcrypto";

CREATE TABLE IF NOT EXISTS users (
    id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    email                text UNIQUE NOT NULL,
    user_token           text UNIQUE NOT NULL,
    google_refresh_token text NOT NULL,
    created_at           timestamptz DEFAULT now(),
    is_active            bool DEFAULT true
);

CREATE TABLE IF NOT EXISTS sites (
    id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id    uuid REFERENCES users(id) ON DELETE CASCADE,
    property   text NOT NULL,
    created_at timestamptz DEFAULT now(),
    UNIQUE (user_id, property)
);

CREATE TABLE IF NOT EXISTS daily_totals (
    site_id     uuid REFERENCES sites(id) ON DELETE CASCADE,
    date        date NOT NULL,
    clicks      int,
    impressions int,
    ctr         real,
    position    real,
    PRIMARY KEY (site_id, date)
);

CREATE TABLE IF NOT EXISTS daily_queries (
    site_id     uuid REFERENCES sites(id) ON DELETE CASCADE,
    date        date NOT NULL,
    query       text NOT NULL,
    clicks      int,
    impressions int,
    ctr         real,
    position    real,
    PRIMARY KEY (site_id, date, query)
);

CREATE TABLE IF NOT EXISTS daily_pages (
    site_id     uuid REFERENCES sites(id) ON DELETE CASCADE,
    date        date NOT NULL,
    page        text NOT NULL,
    clicks      int,
    impressions int,
    ctr         real,
    position    real,
    PRIMARY KEY (site_id, date, page)
);

CREATE TABLE IF NOT EXISTS daily_ai_appearance (
    site_id         uuid REFERENCES sites(id) ON DELETE CASCADE,
    date            date NOT NULL,
    appearance_type text NOT NULL,
    clicks          int,
    impressions     int,
    PRIMARY KEY (site_id, date, appearance_type)
);

-- Indexes for common query patterns
CREATE INDEX IF NOT EXISTS idx_daily_totals_site_date
    ON daily_totals (site_id, date DESC);

CREATE INDEX IF NOT EXISTS idx_daily_queries_site_date
    ON daily_queries (site_id, date DESC);

CREATE INDEX IF NOT EXISTS idx_daily_pages_site_date
    ON daily_pages (site_id, date DESC);

CREATE INDEX IF NOT EXISTS idx_daily_ai_site_date
    ON daily_ai_appearance (site_id, date DESC);
