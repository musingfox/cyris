-- A hosted tenant's tables beyond schema.sql. Only a tenant's own Durable Object gets them, so a
-- self-hosted D1 and a local install's file never hold a table they do not use. Two paths apply
-- this file, as two apply schema.sql: the hosted Worker's seed (workers/app/src/hosted_seed.js)
-- and a hosted container's first load (`bootstrap.load_effective_config`). Every statement is
-- `CREATE ... IF NOT EXISTS` and seeds no row, so applying it again changes nothing. The hosted
-- Worker's hourly poll applies it too, before it writes the buffer, so a tenant seeded before the
-- buffer existed gets its table at its next tick.
-- docs/decisions/0029-a-hosted-tenants-pages-live-in-its-durable-object.md

-- The tenant's site: every page and asset its runs published, keyed by the path a Pages project
-- would serve it at ("/2026-11-18-morning.html", "/index.html", "/favicon.svg"). The hosted
-- Worker serves each one under /t/<tenant>. A run writes the body as a bound parameter: a raw
-- page is about 105 KB, over a Durable Object statement's 100 KB and far under a value's 8 MiB.
CREATE TABLE IF NOT EXISTS hosted_pages (
    path TEXT PRIMARY KEY,
    body TEXT NOT NULL,
    content_type TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- The tenant's RSS buffer: what the hosted Worker's hourly poll of the tenant's own `sources` found
-- (workers/app/src/tenant_buffer.js), kept 8 days from buffer entry and read by a hosted run's
-- `TenantRssSource` by `fetched_at`. The shape `workers/rss` creates for a self-hosted D1 (`SCHEMA`
-- in workers/rss/src/index.js; tests/test_store_parity.py holds the two equal), so one row
-- mapping reads both. Not in schema.sql: a self-hosted D1 gets its `articles` from that Worker.
-- docs/decisions/0001-rss-arrives-through-an-hourly-buffer.md
CREATE TABLE IF NOT EXISTS articles (url TEXT PRIMARY KEY, guid TEXT, title TEXT NOT NULL DEFAULT '', content TEXT NOT NULL DEFAULT '', author TEXT, published_at TEXT NOT NULL, source_name TEXT NOT NULL, fetched_at TEXT NOT NULL, image_url TEXT);
CREATE INDEX IF NOT EXISTS idx_articles_published_at ON articles(published_at);
CREATE INDEX IF NOT EXISTS idx_articles_fetched_at ON articles(fetched_at);

-- The tenant's newsletter queue: each message the hosted Worker's email() handler took at the
-- tenant's own address (workers/app/src/hosted_inbound.js), as the newsletter Worker's KV holds
-- one (workers/newsletter/src/record.js), keyed by the same `nl:<sha256(from|subject|date)>` so a
-- re-delivered copy replaces its row. A hosted run pulls it with `TenantNewsletterSource` through
-- its own gateway and deletes what it processed, its ack. The address it came to is not kept: the
-- map from addresses to tenants is the operator's registry alone. `headers` is the JSON list
-- `record.js` keeps, values only for the list headers it names. `received_at` orders the pull.
-- `held_at` is when a run found no source for the message and left it queued
-- (`TenantNewsletterSource`); the email() handler evicts the oldest such message when the queue is
-- full, so held mail never crowds out new mail. NULL until a run holds it.
CREATE TABLE IF NOT EXISTS newsletters (
    id TEXT PRIMARY KEY,
    sender TEXT NOT NULL,
    subject TEXT NOT NULL,
    html TEXT NOT NULL,
    text TEXT NOT NULL,
    date TEXT NOT NULL,
    headers TEXT NOT NULL,
    raw_size INTEGER,
    received_at TEXT NOT NULL,
    held_at TEXT
);
