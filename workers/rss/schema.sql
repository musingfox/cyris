-- Retention buffer for feed entries. URL is the primary key, matching the
-- ArticleStore's dedup key so the same article never lands twice.
CREATE TABLE IF NOT EXISTS articles (
  url          TEXT PRIMARY KEY,
  guid         TEXT,
  title        TEXT NOT NULL DEFAULT '',
  content      TEXT NOT NULL DEFAULT '',
  author       TEXT,
  published_at TEXT NOT NULL,  -- ISO8601 UTC, string-comparable
  source_name  TEXT NOT NULL,
  fetched_at   TEXT NOT NULL
);

-- The read path is always a published_at window.
CREATE INDEX IF NOT EXISTS idx_articles_published_at ON articles(published_at);
