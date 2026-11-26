// The self-hosted deployment's `sources` table, read and written from the app Worker over D1's
// REST API, as type_scale.js reads `settings`: the Worker has no D1 binding. It answers the vote
// door's source decisions without waking the container (ADR-0050). No workerd-only imports:
// vitest runs this under Node.

import { reviewsOf } from "./source_review.js";

const ACCOUNTS_API = "https://api.cloudflare.com/client/v4/accounts";

const REVIEWS_SQL =
  "SELECT name, config FROM sources WHERE type = 'newsletter' AND json_extract(config, '$.review') IS NOT NULL";
// One statement, so the type check and the write cannot be split by another writer.
const SET_SQL =
  "UPDATE sources SET config = json_set(coalesce(config, '{}'), '$.review', ?) WHERE name = ? AND type = 'newsletter'";
const TYPE_SQL = "SELECT type FROM sources WHERE name = ?";

export function createD1Sources({ env, fetchImpl, timeoutMs = 3000 }) {
  const account = env.CLOUDFLARE_ACCOUNT_ID;
  const database = env.CYRIS_STORE_DATABASE_ID;
  const token = env.CLOUDFLARE_API_TOKEN;
  if (!account || !database || !token) return null;

  // The statement's first result, or a thrown error naming only the status: an error body may
  // echo the request.
  async function query(sql, params) {
    const response = await fetchImpl(`${ACCOUNTS_API}/${account}/d1/database/${database}/query`, {
      method: "POST",
      headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
      body: JSON.stringify({ sql, params }),
      signal: AbortSignal.timeout(timeoutMs),
    });
    if (!response.ok) throw new Error(`D1 answered ${response.status}`);
    const data = await response.json();
    if (!data.success || !data.result?.[0]) throw new Error("D1 answered no result");
    return data.result[0];
  }

  return {
    // The review each newsletter source holds, by name, or null when the table cannot be read:
    // the page then keeps the marks it was rendered with.
    async reviews() {
      try {
        const { results = [] } = await query(REVIEWS_SQL, []);
        return reviewsOf(results.map(({ name, config }) => ({ name, config: JSON.parse(config || "{}") })));
      } catch (error) {
        console.warn("source reviews read failed:", String(error?.message ?? error));
        return null;
      }
    },
    async decide(name, review) {
      try {
        const written = await query(SET_SQL, [review, name]);
        if (written.meta?.changes > 0) return { changed: 1 };
        const { results = [] } = await query(TYPE_SQL, [name]);
        return results.length ? { refused: "not_newsletter" } : { refused: "source_missing" };
      } catch (error) {
        console.warn("source decision failed:", String(error?.message ?? error));
        return { refused: "source store failed", status: 502 };
      }
    },
  };
}
