// The operator's seed of one hosted tenant, and the report that shows what its database holds.
// STUB: per-tenant onboarding replaces the seed. The route seeds only a tenant the operator's
// registry holds (hosted_router.js). Both run on a TenantDb stub's `query` and `tables`, so a
// test hands them an object over local SQLite. No workerd-only imports: vitest runs this under Node.

import { latestPage, sha256Hex } from "./hosted_pages.js";

const SETTING_UPSERT =
  "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) " +
  "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at";
const SOURCE_UPSERT =
  "INSERT INTO sources (name, url, type, config) VALUES (?, ?, ?, ?) " +
  "ON CONFLICT(name) DO UPDATE SET url = excluded.url, type = excluded.type, config = excluded.config";

// The settings the seed writes: every starting value, then the seed's own over them.
export const seedSettings = (startingValues, seed) => ({ ...startingValues, ...seed.settings });

// The sources the seed writes for `tenant`: each catalog feed it names, without the catalog's
// own `topics`, as the onboarding wizard writes a pick through POST /api/sources.
export function seedSources(catalog, seed, tenant) {
  const names = seed.sources?.[tenant];
  if (!Array.isArray(names) || names.length === 0) throw new Error(`the seed names no sources for ${tenant}`);
  const feeds = new Map(catalog.feeds.map((feed) => [feed.name, feed]));
  return names.map((name) => {
    const feed = feeds.get(name);
    if (!feed) throw new Error(`the seed names ${name}, which onboarding_feeds.json does not list`);
    const { name: _name, url, topics: _topics, ...config } = feed;
    return { name, url, type: "rss", config };
  });
}

// The time a row is written at, as the container's stores write it (seconds, UTC offset).
const stamp = (now) => `${now.toISOString().slice(0, 19)}+00:00`;

// Writes the schema and the hosted tables (hosted_schema.sql: the tenant's pages), then the
// settings and the sources, into a tenant's database, and says what it wrote. Every write is an
// upsert, so seeding again puts these values back and leaves any other row alone. The
// container's first run applies the schema's versioned steps.
export async function seedTenant({
  db,
  tenant,
  schemaSql,
  hostedSchemaSql,
  startingValues,
  seed,
  catalog,
  now = new Date(),
}) {
  const settings = seedSettings(startingValues, seed);
  const sources = seedSources(catalog, seed, tenant);
  await db.query(schemaSql, []);
  await db.query(hostedSchemaSql, []);
  for (const [key, value] of Object.entries(settings)) {
    await db.query(SETTING_UPSERT, [key, JSON.stringify(value), stamp(now)]);
  }
  for (const { name, url, type, config } of sources) {
    await db.query(SOURCE_UPSERT, [name, url, type, JSON.stringify(config)]);
  }
  return {
    tenant,
    settings: Object.keys(settings).length,
    sources: sources.map((source) => source.name),
    tables: Object.keys(await db.tables()).length,
  };
}

// The registry's schedule (`registrySchedule` in run_queue.js), copied into the tenant's own
// `settings` so its run reads its window and dates its times in the zone it is ticked in and its
// settings show the hours it runs at. A copy only: the tick reads the registry, and a container
// that rewrites these rows changes neither when nor how often it runs (ADR-0032). Answers false,
// having logged why, when the copy could not be written: for a tenant never seeded, which has no
// settings table, its seed writes the copy.
export async function writeSchedule({ db, schedule, now = new Date() }) {
  try {
    await db.query(SETTING_UPSERT, ["general.digest_schedule", JSON.stringify(schedule.times), stamp(now)]);
    await db.query(SETTING_UPSERT, ["general.timezone", JSON.stringify(schedule.timeZone), stamp(now)]);
    return true;
  } catch (error) {
    console.warn("schedule copy not written:", String(error?.message ?? error));
    return false;
  }
}

// What decides whether a tenant's runs, issues and pages are its own (scripts/hosted_check.py):
// its tables and their row counts, its sources, which sources its stored articles came from, its
// runs, its issues, and the page /t/<tenant>/ serves, by path and SHA-256. A table the tenant
// does not have yet reads as empty, and a site with no page as null.
export async function tenantReport({ db, tenant }) {
  const tables = await db.tables();
  const rows = async (table, sql) => (table in tables ? (await db.query(sql, [])).rows : []);
  const latest = "hosted_pages" in tables && "digests" in tables ? await latestPage(db) : undefined;
  return {
    tenant,
    tables,
    sources: (await rows("sources", "SELECT name FROM sources ORDER BY name")).map((row) => row.name),
    stored_articles: await rows(
      "stored_articles",
      "SELECT source_name, count(*) AS articles FROM stored_articles GROUP BY source_name ORDER BY source_name",
    ),
    digest_runs: await rows(
      "digest_runs",
      "SELECT id, finished_at, status, period, dry_run, fetched FROM digest_runs ORDER BY id",
    ),
    digests: await rows(
      "digests",
      "SELECT date, period, saved_at, json_extract(content, '$.articles_included') AS articles_included " +
        "FROM digests ORDER BY date, period",
    ),
    latest_page: latest ? { path: latest.path, sha256: await sha256Hex(latest.body) } : null,
  };
}
