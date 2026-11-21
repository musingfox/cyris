// A hosted tenant's vote queue: its Durable Object takes each vote a signed-in reader cast
// (hosted_vote.js) in its own `hosted_votes` table (hosted_schema.sql), and the tenant's next run
// applies it with the write rule `promote-sync` uses (src/cyris/adapters/hosted_votes.py). The
// Worker queues and never writes `triaged_at` or a topic vote itself: those have one writer, the
// run's. docs/decisions/0039-a-hosted-vote-is-queued-by-the-worker-and-applied-by-the-run.md
// No workerd-only imports: vitest runs this under Node.

import HOSTED_SCHEMA_SQL from "../../../src/cyris/adapters/store/hosted_schema.sql";

// The most votes a tenant's queue holds. A run drains it at every digest, twice a day by default,
// and a reader who votes on every article of two issues is under 150: this is a bound on a
// leaked token's flood, not a limit a reader meets. A full queue refuses the vote with 429.
export const MAX_QUEUED_VOTES = 2000;

// Queues one vote on `url` for the tenant whose object `storage` is, and answers `{queued: true}`,
// `{refused, status: 404}` when the tenant stores no such article, or `{refused, status: 429}`
// when the queue is full. The URL must be one of the tenant's own `stored_articles`: a vote on
// a URL the run never stored would stamp nothing, and the table is the tenant's alone, so no
// other tenant's article is ever found. The hosted schema is applied first, as storeNewsletter
// does, so a tenant whose object has not had it yet gets the table. One synchronous turn of the
// object, so the check and the insert commit together.
export function queueVote(storage, { url, vote }, { now = new Date() } = {}) {
  const sql = storage.sql;
  sql.exec(HOSTED_SCHEMA_SQL);
  let stored;
  try {
    stored = sql.exec("SELECT 1 AS held FROM stored_articles WHERE url = ?", url).toArray().length > 0;
  } catch (error) {
    // A tenant not seeded yet has no articles table, so no article to vote on.
    if (!/no such table/.test(String(error?.message ?? error))) throw error;
    stored = false;
  }
  if (!stored) return { refused: "that article is not in this tenant's store", status: 404 };
  if (sql.exec("SELECT count(*) AS n FROM hosted_votes").one().n >= MAX_QUEUED_VOTES) {
    return { refused: "this tenant's vote queue is full until its next run", status: 429 };
  }
  sql.exec("INSERT INTO hosted_votes (url, vote, ts) VALUES (?, ?, ?)", url, vote, now.toISOString());
  return { queued: true };
}
