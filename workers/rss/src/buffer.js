// The buffer's poll: fetch each feed, drop what its feed dates past the retention window,
// INSERT OR IGNORE the rest stamped with the poll's start, prune by that stamp, then record
// each feed's outcome in `feed_health`. Kept apart from index.js so the hosted Worker polls
// each tenant's own buffer by these very rules (workers/app/src/tenant_buffer.js), over a
// D1-shaped handle on the tenant's Durable Object rather than the `DB` binding. No
// workerd-only imports: `node --test` and vitest both load it.
import { parseFeed } from "./parse.js";

export const RETENTION_DAYS = 8; // counted from buffer entry; matches the D1ArticleStore's dedup scan window
const FETCH_TIMEOUT_MS = 20000;
// Substack rate-limits Cloudflare's egress; 10 at once drew HTTP 429s.
const CONCURRENCY = 4;

async function fetchFeed(feed) {
  const response = await fetch(feed.url, {
    headers: { "User-Agent": "cyris-rss/1.0 (+https://github.com/musingfox/cyris)" },
    signal: AbortSignal.timeout(FETCH_TIMEOUT_MS),
    cf: { cacheTtl: 0 },
  });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return parseFeed(await response.text(), feed.name, feed.url);
}

// One upsert per polled feed, so a feed that keeps failing outlives Workers Logs'
// 7 days. A success resets the streak but keeps the last error and its time.
async function recordHealth(db, outcomes, now) {
  if (!outcomes.length) return;
  const ok = db.prepare(
    `INSERT INTO feed_health (name, consecutive_failures, last_ok_at) VALUES (?, 0, ?)
     ON CONFLICT(name) DO UPDATE SET consecutive_failures = 0, last_ok_at = excluded.last_ok_at`
  );
  const failed = db.prepare(
    `INSERT INTO feed_health (name, consecutive_failures, last_error, last_failed_at)
     VALUES (?, 1, ?, ?)
     ON CONFLICT(name) DO UPDATE SET
       consecutive_failures = feed_health.consecutive_failures + 1,
       last_error = excluded.last_error,
       last_failed_at = excluded.last_failed_at`
  );
  await db.batch(
    outcomes.map(({ name, error }) =>
      error === null ? ok.bind(name, now) : failed.bind(name, error, now)
    )
  );
}

// Polls `feeds` ({name, url} each) into `db`, which answers D1's prepare/bind/run/batch, and
// says what it found. The tables are the caller's to have created.
export async function pollFeeds(db, feeds) {
  const fetchedAt = new Date().toISOString();
  const rows = [];
  const failures = [];
  const outcomes = [];

  for (let i = 0; i < feeds.length; i += CONCURRENCY) {
    const batch = feeds.slice(i, i + CONCURRENCY);
    const results = await Promise.allSettled(batch.map(fetchFeed));
    results.forEach((result, index) => {
      const name = batch[index].name;
      if (result.status === "fulfilled") {
        rows.push(...result.value);
        outcomes.push({ name, error: null });
      } else {
        failures.push(`${name}: ${result.reason}`);
        outcomes.push({ name, error: result.reason?.message ?? String(result.reason) });
      }
    });
  }

  // Blogs keep months of history in their feed. The read and the prune go by buffer
  // entry, so an old entry inserted now would reach the next digest as new: drop
  // whatever the feed dates past the retention window before it reaches the database.
  const cutoff = new Date(Date.now() - RETENTION_DAYS * 86400_000).toISOString();
  const fresh = rows.filter((r) => r.published_at >= cutoff);

  // INSERT OR IGNORE: re-seeing an entry every hour must not overwrite or error.
  let written = 0;
  const insert = db.prepare(
    `INSERT OR IGNORE INTO articles
       (url, guid, title, content, author, published_at, source_name, fetched_at, image_url)
     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)`
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
        fetchedAt,
        r.image_url
      )
    );
    const results = await db.batch(batch);
    written += results.reduce((sum, r) => sum + (r.meta?.changes ?? 0), 0);
  }

  const pruned = await db.prepare(
    `DELETE FROM articles WHERE fetched_at < ?`
  )
    .bind(cutoff)
    .run();

  // After the articles, and never fatal: losing one poll's health record costs a
  // streak one count, while failing the poll would cost the articles.
  try {
    await recordHealth(db, outcomes, fetchedAt);
  } catch (error) {
    console.error(`could not record feed_health — ${error.message ?? error}`);
  }

  console.log(
    `polled ${feeds.length} feeds: ${rows.length} entries, ${fresh.length} in window, ` +
      `${written} new, ${pruned.meta?.changes ?? 0} pruned, ${failures.length} failed`
  );
  for (const failure of failures) console.warn(`feed failed — ${failure}`);

  return { feeds: feeds.length, entries: rows.length, fresh: fresh.length, written, failures };
}
