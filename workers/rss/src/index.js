// Cloudflare RSS Worker: polls feeds hourly into D1 so the 24h digest window
// sees more than the 2-4h a feed snapshot holds. cyris reads the entries that
// entered the buffer in a time window via GET /articles; there is no ack — this
// is a retention buffer, not a queue.
import { pollFeeds } from "./buffer.js";
import { loadFeeds } from "./feeds.js";

const CORS_HEADERS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, OPTIONS",
  "Access-Control-Allow-Headers": "Authorization, Content-Type",
};

// The buffer's own tables: a clean Cloudflare account provisions an empty D1, and
// nothing else creates `articles`. `feed_health` is also in the app's schema.sql,
// since either side may reach the database first; tests/test_store_parity.py
// holds the two statements equal. Applied
// on every entry point because `IF NOT EXISTS` makes it a no-op, which is
// cheaper than asking whether it is needed. One statement per line — that is
// what `exec()` splits on.
const SCHEMA = [
  `CREATE TABLE IF NOT EXISTS articles (url TEXT PRIMARY KEY, guid TEXT, title TEXT NOT NULL DEFAULT '', content TEXT NOT NULL DEFAULT '', author TEXT, published_at TEXT NOT NULL, source_name TEXT NOT NULL, fetched_at TEXT NOT NULL, image_url TEXT);`,
  `CREATE INDEX IF NOT EXISTS idx_articles_published_at ON articles(published_at);`,
  `CREATE INDEX IF NOT EXISTS idx_articles_fetched_at ON articles(fetched_at);`,
  `CREATE TABLE IF NOT EXISTS feed_health (name TEXT PRIMARY KEY, consecutive_failures INTEGER NOT NULL DEFAULT 0, last_error TEXT, last_failed_at TEXT, last_ok_at TEXT);`,
].join("\n");

// Once per isolate. `exec()` is Cloudflare's maintenance path, not a hot one:
// re-running it per request put a DDL call in front of a read that has no need
// of one, so a transient failure would have turned `GET /articles` into a 500
// and cost that digest every RSS article.
let schemaApplied = false;
async function hasImageColumn(env) {
  const { results } = await env.DB.prepare("PRAGMA table_info('articles')").all();
  return results.some((column) => column.name === "image_url");
}
async function ensureSchema(env) {
  if (schemaApplied) return;
  await env.DB.exec(SCHEMA);
  // A table created before `image_url` existed keeps its old shape under
  // `IF NOT EXISTS`, and SQLite has no `ADD COLUMN IF NOT EXISTS`.
  if (!(await hasImageColumn(env))) {
    try {
      await env.DB.prepare("ALTER TABLE articles ADD COLUMN image_url TEXT").run();
    } catch (error) {
      // Two isolates can start on the old table together; the one that loses adds nothing.
      if (!(await hasImageColumn(env))) throw error;
    }
  }
  schemaApplied = true;
}

function json(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json", ...CORS_HEADERS },
  });
}

// The poll's rules are buffer.js's, which the hosted Worker shares; this Worker's part is
// the `DB` binding's schema and its `sources` table.
async function poll(env, feeds) {
  await ensureSchema(env);
  feeds = feeds ?? (await loadFeeds(env));
  return pollFeeds(env.DB, feeds);
}

export default {
  async scheduled(_controller, env, ctx) {
    ctx.waitUntil(poll(env));
  },

  async fetch(request, env) {
    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: CORS_HEADERS });
    }
    // An unset secret would otherwise accept the literal header "Bearer undefined".
    if (!env.RSS_TOKEN || request.headers.get("Authorization") !== `Bearer ${env.RSS_TOKEN}`) {
      return json({ error: "unauthorized" }, 401);
    }

    // Every route below reads or writes `articles`, and the first digest — or
    // `doctor`'s /stats health check — can land before the first cron tick. Per
    // route was tried and dropped: /stats was missed, and the check it feeds
    // then reported a clean deployment as an unreachable Worker.
    await ensureSchema(env);

    const url = new URL(request.url);

    // Idempotent window read by buffer-entry time, so an entry its feed listed late
    // still reaches the next digest — no ack, so a crashed digest simply reads again.
    if (request.method === "GET" && url.pathname === "/articles") {
      const after = url.searchParams.get("after");
      const before = url.searchParams.get("before");
      if (!after || !before) return json({ error: "after and before required" }, 400);
      const limit = Math.min(Number(url.searchParams.get("limit")) || 500, 2000);

      const { results } = await env.DB.prepare(
        `SELECT url, guid, title, content, author, published_at, source_name, image_url
           FROM articles
          WHERE fetched_at >= ? AND fetched_at < ?
          ORDER BY fetched_at DESC
          LIMIT ?`
      )
        .bind(after, before, limit)
        .all();
      return json(results ?? []);
    }

    // Manual trigger, so accumulation can be verified without waiting for cron.
    if (request.method === "POST" && url.pathname === "/poll") {
      return json(await poll(env));
    }

    if (request.method === "GET" && url.pathname === "/stats") {
      const row = await env.DB.prepare(
        `SELECT count(*) AS total, min(published_at) AS oldest, max(published_at) AS newest
           FROM articles`
      ).first();
      return json(row ?? {});
    }

    return json({ error: "not found" }, 404);
  },
};
