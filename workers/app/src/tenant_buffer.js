// A hosted tenant's RSS buffer: its Durable Object polls the tenant's own `sources` into its own
// `articles` table (hosted_schema.sql), by the rules workers/rss polls a self-hosted D1 with
// (workers/rss/src/buffer.js, imported, not copied). The hosted Worker's hourly tick asks each
// active tenant's object for one poll before it plans that tenant's run (hosted.js), so the
// fetches and the parsing are the object's own invocation's, not the tick's.
// docs/decisions/0001-rss-arrives-through-an-hourly-buffer.md and
// docs/decisions/0031-a-hosted-tenants-rss-is-polled-by-the-hosted-worker.md.
// No workerd-only imports: vitest runs this under Node.

import HOSTED_SCHEMA_SQL from "../../../src/cyris/adapters/store/hosted_schema.sql";
import { pollFeeds } from "../../rss/src/buffer.js";
import { loadFeeds } from "../../rss/src/feeds.js";

// D1's prepare/bind/all/first/run/batch over a Durable Object's storage, which is what buffer.js
// and feeds.js are written against. `changes` is the difference of two `total_changes()`
// readings, as TenantDb.query reports it; a batch runs in one transaction, as D1 runs one.
export function d1Handle(storage) {
  const sql = storage.sql;
  const total = () => sql.exec("SELECT total_changes() AS n").one().n;
  const runNow = (query, params) => {
    const before = total();
    sql.exec(query, ...params).toArray();
    return { meta: { changes: total() - before } };
  };
  const statement = (query, params = []) => ({
    bind: (...values) => statement(query, values),
    runNow: () => runNow(query, params),
    all: async () => ({ results: sql.exec(query, ...params).toArray() }),
    first: async () => sql.exec(query, ...params).toArray()[0] ?? null,
    run: async () => runNow(query, params),
  });
  return {
    prepare: (query) => statement(query),
    batch: async (statements) => storage.transactionSync(() => statements.map((s) => s.runNow())),
  };
}

// One poll of the tenant's buffer, and what it did. The sources are read first, so a tenant that
// was never seeded fails here and gets no table; then the hosted schema is applied, which gives a
// tenant seeded before the buffer existed its `articles` table. `fetches` is the feed requests
// this poll made, one per feed: the subrequests the object's invocation spends (a redirect a feed
// answers with is one more, which nothing here can count).
export async function pollTenantBuffer(storage) {
  const db = d1Handle(storage);
  const feeds = await loadFeeds({ DB: db });
  storage.sql.exec(HOSTED_SCHEMA_SQL);
  const polled = await pollFeeds(db, feeds);
  return { ...polled, fetches: feeds.length };
}
