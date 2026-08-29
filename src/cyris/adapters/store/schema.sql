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

-- Which files the deployed digest site is made of: path → Pages asset hash.
-- A Pages deployment is a full snapshot, so every deploy must name every file
-- that should stay reachable. Cloudflare already holds the *bytes* in its
-- account-wide asset store (that is what `check-missing` answers about), so
-- this table is the only thing cyris has to keep — a few KB, not an archive.
-- When an asset does age out, the deployed site itself is where it is recovered
-- from: it serves the same bytes it was uploaded with.
CREATE TABLE IF NOT EXISTS pages_manifest (
  path       TEXT PRIMARY KEY,     -- "/2026-08-27-morning.html", slash-prefixed
  hash       TEXT NOT NULL,        -- blake3(base64(bytes) + ext)[:32]
  updated_at TEXT NOT NULL
);

-- Pre-truncation story membership: which articles the clustering step grouped
-- together, per digest window. Written delete-then-insert per (digest_date,
-- period), so a re-run of the same window replaces its rows rather than
-- accumulating duplicates.
CREATE TABLE IF NOT EXISTS stories (
  id          TEXT PRIMARY KEY,    -- "{digest_date}-{period}-{urlhash}": content-derived
                                   -- from the sorted member URLs, so the same story keeps
                                   -- its id across re-runs of the window
  digest_date TEXT NOT NULL,
  period      TEXT NOT NULL,
  heading     TEXT NOT NULL,
  created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_stories_window ON stories(digest_date, period);

CREATE TABLE IF NOT EXISTS story_members (
  story_id    TEXT NOT NULL,
  article_url TEXT NOT NULL,
  PRIMARY KEY (story_id, article_url)
);

CREATE INDEX IF NOT EXISTS idx_story_members_url ON story_members(article_url);

-- Normalized tag vocabulary and its URL-keyed article memberships. A story's
-- tags live here (on its member articles), never on `stories`: one normalized
-- home, no raw-LLM-string sibling to drift from it.
CREATE TABLE IF NOT EXISTS tags (
  name       TEXT PRIMARY KEY,
  created_at TEXT NOT NULL      -- first sighting; INSERT OR IGNORE keeps it
);

CREATE TABLE IF NOT EXISTS article_tags (
  article_url TEXT NOT NULL,
  tag         TEXT NOT NULL,
  tagged_at   TEXT NOT NULL,    -- latest write; INSERT OR REPLACE refreshes it
  PRIMARY KEY (article_url, tag)
);

CREATE INDEX IF NOT EXISTS idx_article_tags_tag ON article_tags(tag);
