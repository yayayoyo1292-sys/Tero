-- =============================================================================
-- TIROO — Complete PostgreSQL Schema
-- Run this entirely in your new Supabase project's SQL Editor
-- =============================================================================

-- ── 1. news ──────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS news (
    id           SERIAL PRIMARY KEY,
    title        TEXT UNIQUE,
    url          TEXT UNIQUE,
    image        TEXT,
    category     TEXT,                      -- always 'رياضة' for tiroo
    template_key TEXT,                      -- always 'رياضة' for tiroo
    content      TEXT,
    source_url   TEXT,                      -- category page URL scraped from
    source_name  TEXT,                      -- internal name (e.g. 'uae_1')
    source_label TEXT,                      -- Arabic label (e.g. 'ستاد الإمارات')
    detected_at  TIMESTAMPTZ,
    scraped_at   TIMESTAMPTZ,
    inserted_at  TIMESTAMPTZ DEFAULT NOW(),
    processed_at TIMESTAMPTZ,
    queued_at    TIMESTAMPTZ,
    created_at   TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_news_url          ON news(url);
CREATE INDEX IF NOT EXISTS idx_news_inserted_at  ON news(inserted_at DESC);
CREATE INDEX IF NOT EXISTS idx_news_source_url   ON news(source_url);

-- ── 2. news_queue ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS news_queue (
    id           SERIAL PRIMARY KEY,
    article_id   INT REFERENCES news(id) ON DELETE CASCADE,
    title        TEXT,
    url          TEXT UNIQUE,
    content      TEXT,

    -- Timestamps (epoch float for scoring, TIMESTAMPTZ for display)
    created_at       DOUBLE PRECISION,
    detected_at      TIMESTAMPTZ,
    scraped_at       TIMESTAMPTZ,
    inserted_at      TIMESTAMPTZ DEFAULT NOW(),
    queued_at        TIMESTAMPTZ DEFAULT NOW(),
    processing_at    TIMESTAMPTZ,
    published_at     TIMESTAMPTZ,
    last_updated     TIMESTAMPTZ DEFAULT NOW(),

    -- Scoring (simplified — no AI score needed, kept for schema compat)
    keyword_score    FLOAT   DEFAULT 0,
    aging_score      FLOAT   DEFAULT 0,
    ai_score         FLOAT   DEFAULT 0,
    final_score      FLOAT   DEFAULT 0,
    priority_score   INT     DEFAULT 0,     -- always 0 in tiroo (no priority engine)

    -- Queue lifecycle
    status           TEXT    DEFAULT 'pending',   -- pending | processing | published
    retry_count      INT     DEFAULT 0,
    retry_after      TIMESTAMPTZ,

    -- Per-platform status
    telegram_status  TEXT    DEFAULT 'pending',
    instagram_status TEXT    DEFAULT 'pending',
    twitter_status   TEXT    DEFAULT 'pending',
    facebook_status  TEXT    DEFAULT 'pending',

    -- Per-platform attempt counters
    telegram_attempts  INT   DEFAULT 0,
    instagram_attempts INT   DEFAULT 0,
    twitter_attempts   INT   DEFAULT 0,
    facebook_attempts  INT   DEFAULT 0,

    -- Image
    generated_image  TEXT,
    image_url        TEXT,

    -- Category info
    category         TEXT    DEFAULT 'رياضة',
    template_key     TEXT    DEFAULT 'رياضة',
    source_label     TEXT
);

CREATE INDEX IF NOT EXISTS idx_queue_status_queued
    ON news_queue(status, queued_at ASC)
    WHERE status = 'pending';

CREATE INDEX IF NOT EXISTS idx_queue_retry_after
    ON news_queue(retry_after)
    WHERE status = 'pending';

CREATE INDEX IF NOT EXISTS idx_queue_article_id
    ON news_queue(article_id);

-- ── 3. publish_log ────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS publish_log (
    id          SERIAL PRIMARY KEY,
    article_id  INT  NOT NULL,
    queue_id    INT,
    platform    TEXT NOT NULL,
    status      TEXT NOT NULL,              -- sent | failed | skipped
    fingerprint TEXT UNIQUE NOT NULL,
    error_msg   TEXT,
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    updated_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_publish_log_article_platform
    ON publish_log(article_id, platform, status);

-- ── 4. social_rate_log ────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS social_rate_log (
    id          SERIAL PRIMARY KEY,
    platform    TEXT NOT NULL,
    sent_at     TIMESTAMPTZ DEFAULT NOW(),
    article_id  INT,
    queue_id    INT
);

CREATE INDEX IF NOT EXISTS idx_rate_log_platform
    ON social_rate_log(platform, sent_at DESC);

-- ── 5. scraper_health ─────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS scraper_health (
    id                   SERIAL PRIMARY KEY,
    category_url         TEXT UNIQUE,
    category_name        TEXT,
    last_checked         TIMESTAMPTZ DEFAULT NOW(),
    last_success         TIMESTAMPTZ,
    last_failure         TIMESTAMPTZ,
    consecutive_failures INT DEFAULT 0,
    articles_found       INT DEFAULT 0,
    status               TEXT DEFAULT 'ok'  -- ok | degraded | down
);

CREATE INDEX IF NOT EXISTS idx_scraper_health_url
    ON scraper_health(category_url);

-- ── 6. Helper function: fail stale processing rows ───────────────────────────
CREATE OR REPLACE FUNCTION reset_stale_processing(max_minutes INT DEFAULT 10)
RETURNS INT AS $$
DECLARE
    affected INT;
BEGIN
    UPDATE news_queue
    SET status       = 'pending',
        last_updated = NOW()
    WHERE status        = 'processing'
      AND processing_at < NOW() - (max_minutes || ' minutes')::interval;

    GET DIAGNOSTICS affected = ROW_COUNT;
    RETURN affected;
END;
$$ LANGUAGE plpgsql;

-- ── 7. RLS Policies (enable if using Supabase Auth; skip for service-role key)
-- These policies allow full access when using the service-role key (backend),
-- and restrict anon access. Enable RLS only if you expose these tables via the
-- Supabase REST API from a frontend.
--
-- ALTER TABLE news              ENABLE ROW LEVEL SECURITY;
-- ALTER TABLE news_queue        ENABLE ROW LEVEL SECURITY;
-- ALTER TABLE publish_log       ENABLE ROW LEVEL SECURITY;
-- ALTER TABLE social_rate_log   ENABLE ROW LEVEL SECURITY;
-- ALTER TABLE scraper_health    ENABLE ROW LEVEL SECURITY;
--
-- CREATE POLICY "service_role_full_access" ON news
--     USING (auth.role() = 'service_role');
-- (repeat for each table)
--
-- For the backend using DATABASE_URL directly (psycopg2), RLS is bypassed
-- automatically since it connects as the database owner / postgres role.

-- ── 8. Supabase Storage ───────────────────────────────────────────────────────
-- Create bucket named "generated" in Supabase Dashboard → Storage → New Bucket
-- Settings: Public bucket = ON (so image URLs are publicly accessible)
-- Or via SQL:
-- INSERT INTO storage.buckets (id, name, public)
-- VALUES ('generated', 'generated', true)
-- ON CONFLICT DO NOTHING;
