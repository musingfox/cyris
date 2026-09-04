// Cloudflare RSS Worker: polls feeds hourly into D1 so the 24h digest window
// sees more than the 2-4h a feed snapshot holds. cyris reads a time window via
// GET /articles; there is no ack — this is a retention buffer, not a queue.
import BUNDLED_FEEDS from "./feeds.json";
import { loadFeeds } from "./feeds.js";
import { parseFeed } from "./parse.js";

const CORS_HEADERS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, OPTIONS",
  "Access-Control-Allow-Headers": "Authorization, Content-Type",
};

// The buffer's own tables, and the only definition of them: a clean Cloudflare
// account provisions an empty D1, and nothing else creates `articles`. Applied
// on every entry point because `IF NOT EXISTS` makes it a no-op, which is
// cheaper than asking whether it is needed. One statement per line — that is
// what `exec()` splits on.
const SCHEMA = [
  `CREATE TABLE IF NOT EXISTS articles (url TEXT PRIMARY KEY, guid TEXT, title TEXT NOT NULL DEFAULT '', content TEXT NOT NULL DEFAULT '', author TEXT, published_at TEXT NOT NULL, source_name TEXT NOT NULL, fetched_at TEXT NOT NULL);`,
  `CREATE INDEX IF NOT EXISTS idx_articles_published_at ON articles(published_at);`,
].join("\n");

const RETENTION_DAYS = 8; // matches the ArticleStore's dedup scan window
const FETCH_TIMEOUT_MS = 20000;
// Substack rate-limits Cloudflare's egress; 10 at once drew HTTP 429s.
const CONCURRENCY = 4;
function json(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json", ...CORS_HEADERS },
  });
}

async function fetchFeed(feed) {
  const response = await fetch(feed.url, {
    headers: { "User-Agent": "cyris-rss/1.0 (+https://github.com/musingfox/cyris)" },
    signal: AbortSignal.timeout(FETCH_TIMEOUT_MS),
    cf: { cacheTtl: 0 },
  });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return parseFeed(await response.text(), feed.name);
}

async function poll(env, feeds) {
  await env.DB.exec(SCHEMA);
  feeds = feeds ?? (await loadFeeds(env, BUNDLED_FEEDS));
  const fetchedAt = new Date().toISOString();
  const rows = [];
  const failures = [];

  for (let i = 0; i < feeds.length; i += CONCURRENCY) {
    const batch = feeds.slice(i, i + CONCURRENCY);
    const results = await Promise.allSettled(batch.map(fetchFeed));
    results.forEach((result, index) => {
      if (result.status === "fulfilled") rows.push(...result.value);
      else failures.push(`${batch[index].name}: ${result.reason}`);
    });
  }

  // Blogs keep months of history in their feed. Inserting those and letting the
  // prune below delete them again burns ~1.5k writes per tick against D1's daily
  // quota, so drop them before they reach the database.
  const cutoff = new Date(Date.now() - RETENTION_DAYS * 86400_000).toISOString();
  const fresh = rows.filter((r) => r.published_at >= cutoff);

  // INSERT OR IGNORE: re-seeing an entry every hour must not overwrite or error.
  let written = 0;
  const insert = env.DB.prepare(
    `INSERT OR IGNORE INTO articles
       (url, guid, title, content, author, published_at, source_name, fetched_at)
     VALUES (?, ?, ?, ?, ?, ?, ?, ?)`
  );
  for (let i = 0; i < fresh.length; i += 50) {
    const batch = fresh.slice(i, i + 50).map((r) =>
      insert.bind(
        r.url,
        r.guid,
        r.title,
        r.content,
        r.author,
        r.published_at,
        r.source_name,
        fetchedAt
      )
    );
    const results = await env.DB.batch(batch);
    written += results.reduce((sum, r) => sum + (r.meta?.changes ?? 0), 0);
  }

  const pruned = await env.DB.prepare(
    `DELETE FROM articles WHERE published_at < datetime('now', ?)`
  )
    .bind(`-${RETENTION_DAYS} days`)
    .run();

  console.log(
    `polled ${feeds.length} feeds: ${rows.length} entries, ${fresh.length} in window, ` +
      `${written} new, ${pruned.meta?.changes ?? 0} pruned, ${failures.length} failed`
  );
  for (const failure of failures) console.warn(`feed failed — ${failure}`);

  return { feeds: feeds.length, entries: rows.length, fresh: fresh.length, written, failures };
}

export default {
  async scheduled(_controller, env, ctx) {
    ctx.waitUntil(poll(env));
  },

  async fetch(request, env) {
    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: CORS_HEADERS });
    }
    if (request.headers.get("Authorization") !== `Bearer ${env.RSS_TOKEN}`) {
      return json({ error: "unauthorized" }, 401);
    }

    // The first digest can land before the first cron tick, so the read path
    // cannot assume the table is already there.
    await env.DB.exec(SCHEMA);

    const url = new URL(request.url);

    // Idempotent window read — no ack, so a crashed digest simply reads again.
    if (request.method === "GET" && url.pathname === "/articles") {
      const after = url.searchParams.get("after");
      const before = url.searchParams.get("before");
      if (!after || !before) return json({ error: "after and before required" }, 400);
      const limit = Math.min(Number(url.searchParams.get("limit")) || 500, 2000);

      const { results } = await env.DB.prepare(
        `SELECT url, guid, title, content, author, published_at, source_name
           FROM articles
          WHERE published_at >= ? AND published_at < ?
          ORDER BY published_at DESC
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
