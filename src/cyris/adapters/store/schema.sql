-- Persistent article store, the pipeline's system of record.
--
-- Shares the `cyris-rss` D1 database with the feed buffer (workers/rss/schema.sql,
-- table `articles`). Different lifecycles, same database: the buffer is disposable
-- and retention-pruned, this table is not. Kept together because that database is
-- already declared as a binding in workers/rss/wrangler.toml, which is what a
-- Deploy to Cloudflare button provisions from.
--
--   wrangler d1 execute cyris-rss --remote --file=src/cyris/adapters/store/schema.sql

CREATE TABLE IF NOT EXISTS stored_articles (
  url              TEXT PRIMARY KEY,     -- dedup key, same as the JSON store's
  original_id,                           -- feed ids are ints, newsletter ids are strings;
                                         -- no declared type, so SQLite keeps each as given
  title            TEXT NOT NULL DEFAULT '',
  content          TEXT NOT NULL DEFAULT '',
  author           TEXT,
  published_at     TEXT NOT NULL,        -- ISO8601 UTC with microseconds, string-comparable
  source_name      TEXT NOT NULL,
  source_tier      TEXT NOT NULL,
  source_tags      TEXT NOT NULL DEFAULT '[]',   -- JSON array
  ref_urls         TEXT NOT NULL DEFAULT '[]',   -- JSON array

  state            TEXT NOT NULL DEFAULT 'pending',
  first_seen_at    TEXT NOT NULL,
  digest_date      TEXT,
  rejection_reason TEXT,
  score            REAL,
  language         TEXT,
  scored_at        TEXT,
  triaged_at       TEXT,                 -- non-null ⇒ a human decided; the pipeline must not overwrite
  exported_at      TEXT
);

-- The digest window, the triage queue, and `cyris learn` are the three read paths.
CREATE INDEX IF NOT EXISTS idx_stored_articles_first_seen_at ON stored_articles(first_seen_at);
CREATE INDEX IF NOT EXISTS idx_stored_articles_state ON stored_articles(state);

-- LLM spend per run, previously usage.jsonl.
CREATE TABLE IF NOT EXISTS usage_log (
  logged_at         TEXT NOT NULL,
  digest_date       TEXT,
  period            TEXT,
  articles_received INTEGER NOT NULL DEFAULT 0,
  articles_included INTEGER NOT NULL DEFAULT 0,
  model             TEXT,
  api_calls         INTEGER NOT NULL DEFAULT 0,
  input_tokens      INTEGER NOT NULL DEFAULT 0,
  output_tokens     INTEGER NOT NULL DEFAULT 0,
  cost_usd          REAL NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_usage_log_logged_at ON usage_log(logged_at);

-- Source definitions, so adding a feed is a write rather than a rebuild.
-- `sources.yaml` stays the editable format and the fallback; `cyris sources push`
-- copies it here, and both readers (cyris and workers/rss) prefer this table when
-- it has rows. The columns are the ones the Worker's poll query needs; everything
-- else rides in `config` as JSON, so a new SourceConfig field costs no migration.
CREATE TABLE IF NOT EXISTS sources (
  name    TEXT PRIMARY KEY,
  url     TEXT,
  type    TEXT NOT NULL DEFAULT 'rss',
  config  TEXT NOT NULL DEFAULT '{}'
);

-- Runtime-mutable settings (grade D in docs/architecture.md §5), so changing the
-- LLM provider or the digest times is a write rather than an image rebuild.
-- `cyris.toml` is baked into the image and mounted `:ro` in the container, which
-- is why the settings page cannot write it. Read order is D1 first, file second;
-- values are JSON so a list (digest_schedule) round-trips like a scalar.
CREATE TABLE IF NOT EXISTS settings (
  key        TEXT PRIMARY KEY,
  value      TEXT NOT NULL,        -- JSON-encoded
  updated_at TEXT NOT NULL
);
