-- A hosted tenant's tables beyond schema.sql. Only a tenant's own Durable Object gets them, so a
-- self-hosted D1 and a local install's file never hold a table they do not use. Two paths apply
-- this file, as two apply schema.sql: the hosted Worker's seed (workers/app/src/hosted_seed.js)
-- and a hosted container's first load (`bootstrap.load_effective_config`). Every statement is
-- `CREATE ... IF NOT EXISTS` and seeds no row, so applying it again changes nothing.
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
