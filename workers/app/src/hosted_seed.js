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

// The lanes a source may take (`Lane` in src/cyris/domain/models.py), and a newsletter's when the
// operator names none: read, so its issues are scored and summarized like a feed's articles.
const LANES = ["skim", "read", "follow"];
const NEWSLETTER_LANE = "read";
const SENDER = /^[^\s@<>"(),;:]+@[^\s@<>"(),;:]+\.[^\s@<>"(),;:]+$/;
const NAME = /^[^\u0000-\u001f\u007f]{1,80}$/;

// The `sources` row a newsletter source is, from the operator's query: `name`, `from` (the
// address its issues come from, which `email_match` names as `from:<address>`, the shape a
// self-hosted /settings writes), and optionally `homepage` (http(s)) and `lane`. Answers
// `{row}` or `{refused}`, the reason. tests/test_tenant_newsletter_source.py reads the same cases
// (workers/app/test/newsletter_source_cases.json) back as the run's `SourceConfig`.
export function newsletterSource({ name, from, homepage, lane = NEWSLETTER_LANE } = {}) {
  const trimmed = typeof name === "string" ? name.trim() : "";
  if (!NAME.test(trimmed)) return { refused: "name is 1 to 80 characters with no control character" };
  const sender = typeof from === "string" ? from.trim().toLowerCase() : "";
  if (!SENDER.test(sender)) return { refused: "from is the address the newsletter is sent from" };
  if (!LANES.includes(lane)) return { refused: `lane is one of ${LANES.join(", ")}` };
  const config = { lane, tags: [], email_match: `from:${sender}` };
  if (homepage !== undefined && homepage !== "") {
    let parsed;
    try {
      parsed = new URL(homepage);
    } catch {
      parsed = null;
    }
    if (!parsed || !["http:", "https:"].includes(parsed.protocol)) return { refused: "homepage is an http(s) URL" };
    config.homepage = homepage;
  }
  return { row: { name: trimmed, url: null, type: "newsletter", config } };
}

// Adds, or rewrites, one newsletter source in a seeded tenant's database, so the tenant's runs
// ingest the issues its address receives from that sender: a run takes an issue only for a
// source whose `email_match` names its sender. Answers `{source}`, or `{refused, status}`: 400 for
// a query that is not a source, 409 for a tenant not seeded yet or a name an RSS source holds.
export async function addNewsletterSource({ db, params }) {
  const made = newsletterSource(params);
  if (made.refused) return { refused: made.refused, status: 400 };
  const { name, url, type, config } = made.row;
  if (!("sources" in (await db.tables()))) return { refused: "seed the tenant first", status: 409 };
  const [held] = (await db.query("SELECT type FROM sources WHERE name = ?", [name])).rows;
  if (held && held.type !== type) return { refused: `${name} is already a ${held.type} source`, status: 409 };
  await db.query(SOURCE_UPSERT, [name, url, type, JSON.stringify(config)]);
  return { source: made.row };
}

// What decides whether a tenant's runs, issues and pages are its own (scripts/hosted_check.py):
// its tables and their row counts, its sources, which sources its stored articles came from, its
// runs, its issues, and the page /t/<tenant>/ serves, by path and SHA-256. A table the tenant
// does not have yet reads as empty, and a site with no page as null. Its queued newsletters are
// listed by sender and subject, never by body, so the operator can see an issue reach each
// tenant it was sent to, and read the code in the subject of Gmail's forwarding confirmation
// (docs/hosted-tenants.md) before the tenant's next run acks it.
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
    newsletters: await rows(
      "newsletters",
      "SELECT id, sender, subject, date, received_at, held_at FROM newsletters ORDER BY received_at, id",
    ),
    latest_page: latest ? { path: latest.path, sha256: await sha256Hex(latest.body) } : null,
  };
}
