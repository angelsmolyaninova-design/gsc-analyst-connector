CREATE TABLE IF NOT EXISTS tool_calls (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     uuid REFERENCES users(id) ON DELETE SET NULL,
    tool_name   text NOT NULL,
    site_id     uuid REFERENCES sites(id) ON DELETE SET NULL,
    called_at   timestamptz DEFAULT now(),
    duration_ms int,
    success     bool DEFAULT true
);

CREATE INDEX IF NOT EXISTS idx_tool_calls_user_date
    ON tool_calls (user_id, called_at DESC);
