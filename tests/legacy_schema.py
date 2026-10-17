"""The schema as it stood before the tier-to-lane rename, frozen for migration tests.

A database built from this is what an existing deployment holds before its first boot
after the rename. Nothing in the live code imports it; it must not track `schema.sql`.
Comments are stripped; the statements are unchanged.
"""

from fakes import SqliteD1

LEGACY_SCHEMA_SQL = """\
CREATE TABLE IF NOT EXISTS stored_articles (
  url              TEXT PRIMARY KEY,
  original_id,
  title            TEXT NOT NULL DEFAULT '',
  content          TEXT NOT NULL DEFAULT '',
  author           TEXT,
  published_at     TEXT NOT NULL,
  source_name      TEXT NOT NULL,
  source_tier      TEXT NOT NULL,
  source_tags      TEXT NOT NULL DEFAULT '[]',
  ref_urls         TEXT NOT NULL DEFAULT '[]',
  state            TEXT NOT NULL DEFAULT 'pending',
  first_seen_at    TEXT NOT NULL,
  digest_date      TEXT,
  rejection_reason TEXT,
  score            REAL,
  language         TEXT,
  triaged_at       TEXT
);
CREATE INDEX IF NOT EXISTS idx_stored_articles_first_seen_at ON stored_articles(first_seen_at);
CREATE INDEX IF NOT EXISTS idx_stored_articles_state ON stored_articles(state);
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
CREATE TABLE IF NOT EXISTS sources (
  name    TEXT PRIMARY KEY,
  url     TEXT,
  type    TEXT NOT NULL DEFAULT 'rss',
  config  TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS tracked_topics (
  name        TEXT PRIMARY KEY,
  description TEXT NOT NULL,
  threshold   REAL NOT NULL,
  model       TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS areas (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  name              TEXT NOT NULL UNIQUE COLLATE NOCASE,
  position          INTEGER NOT NULL DEFAULT 0,
  status            TEXT NOT NULL CHECK (status IN ('proposed', 'active', 'dismissed', 'merged')),
  merged_into       INTEGER REFERENCES areas(id),
  proposal_articles INTEGER NOT NULL DEFAULT 0,
  proposal_issues   INTEGER NOT NULL DEFAULT 0,
  last_proposed_run TEXT
);
CREATE TABLE IF NOT EXISTS area_birth_threshold (
  id       INTEGER PRIMARY KEY CHECK (id = 1),
  articles INTEGER NOT NULL,
  issues   INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
  key        TEXT PRIMARY KEY,
  value      TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pages_manifest (
  path       TEXT PRIMARY KEY,
  hash       TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pages_deploy_receipt (
  project     TEXT PRIMARY KEY,
  created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS stories (
  id          TEXT PRIMARY KEY,
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
CREATE TABLE IF NOT EXISTS tags (
  name       TEXT PRIMARY KEY,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS article_tags (
  article_url TEXT NOT NULL,
  tag         TEXT NOT NULL,
  tagged_at   TEXT NOT NULL,
  PRIMARY KEY (article_url, tag)
);
CREATE INDEX IF NOT EXISTS idx_article_tags_tag ON article_tags(tag);
CREATE TABLE IF NOT EXISTS digest_runs (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  finished_at    TEXT NOT NULL,
  status         TEXT NOT NULL,
  period         TEXT NOT NULL,
  dry_run        INTEGER NOT NULL,
  wall_seconds   REAL,
  fetched        INTEGER,
  failed_sources TEXT,
  build_sha      TEXT NOT NULL DEFAULT '',
  degraded       INTEGER,
  summary        TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_digest_runs_finished ON digest_runs(finished_at);
CREATE TABLE IF NOT EXISTS digests (
  date     TEXT NOT NULL,
  period   TEXT NOT NULL,
  content  TEXT NOT NULL,
  raw_page INTEGER NOT NULL CHECK (raw_page IN (0, 1)),
  saved_at TEXT NOT NULL,
  PRIMARY KEY (date, period)
);
CREATE TABLE IF NOT EXISTS digest_inputs (
  date     TEXT NOT NULL,
  period   TEXT NOT NULL,
  urls     TEXT NOT NULL,
  saved_at TEXT NOT NULL,
  PRIMARY KEY (date, period)
);
CREATE TABLE IF NOT EXISTS feed_health (
  name                 TEXT PRIMARY KEY,
  consecutive_failures INTEGER NOT NULL DEFAULT 0,
  last_error           TEXT,
  last_failed_at       TEXT,
  last_ok_at           TEXT
);
CREATE TABLE IF NOT EXISTS vote_similarity_shadow (
  run_at        TEXT NOT NULL,
  period        TEXT NOT NULL,
  model         TEXT NOT NULL,
  candidate_url TEXT NOT NULL,
  up_url        TEXT,
  up_cosine     REAL,
  down_url      TEXT,
  down_cosine   REAL,
  PRIMARY KEY (run_at, candidate_url)
);
CREATE TABLE IF NOT EXISTS blind_labels (
  url            TEXT NOT NULL,
  position       INTEGER NOT NULL,
  news           INTEGER NOT NULL CHECK (news IN (0, 1)),
  pipeline_state TEXT NOT NULL,
  stratum_size   INTEGER NOT NULL,
  seed           INTEGER NOT NULL,
  pool_since     TEXT NOT NULL,
  drawn_at       TEXT NOT NULL,
  label          TEXT CHECK (label IN ('up', 'down', 'skip')),
  labeled_at     TEXT,
  PRIMARY KEY (drawn_at, url)
);
"""


def old_db() -> SqliteD1:
    db = SqliteD1(with_schema=False)
    db.query(LEGACY_SCHEMA_SQL)
    return db


def columns(db: SqliteD1) -> set[str]:
    return {r["name"] for r in db.query("PRAGMA table_info('stored_articles')").rows}


def recorded(db: SqliteD1) -> list[str]:
    return [
        r["id"] for r in db.query("SELECT id FROM schema_migrations ORDER BY applied_at, id").rows
    ]
