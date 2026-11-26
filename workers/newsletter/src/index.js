// Cloudflare Email Worker: receives forwarded newsletters, stores parsed fields
// in KV; cyris pulls them via GET /newsletters and clears them via POST /ack.
// Mirrors workers/promote (Bearer auth, list/ack over KV), plus an email() handler.
// The record each message is stored as is record.js's, which the hosted Worker shares.
import { newsletterRecord } from "./record.js";

const CORS_HEADERS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
  "Access-Control-Allow-Headers": "Authorization, Content-Type",
};

// KV lists at most 1000 keys a call, and a bulk get reads at most 100 keys as one of an
// invocation's 1000 operations (https://developers.cloudflare.com/kv/api/list-keys/,
// https://developers.cloudflare.com/kv/api/read-key-value-pairs/). Fewer than 100 a batch: a bulk
// get's answer is capped at 25 MB, and one issue's HTML can run to hundreds of KB.
const READ_BATCH = 50;

// Every queued message, in key order: each page of the list by its cursor until KV says the list
// is complete, since a page may come back short or empty before then.
async function queued(kv) {
  const names = [];
  let cursor;
  do {
    const page = await kv.list({ prefix: "nl:", cursor });
    names.push(...page.keys.map(({ name }) => name));
    cursor = page.list_complete ? null : page.cursor;
  } while (cursor);
  const items = [];
  for (let start = 0; start < names.length; start += READ_BATCH) {
    const batch = names.slice(start, start + READ_BATCH);
    const values = await kv.get(batch, "json");
    items.push(...batch.map((name) => values.get(name)).filter(Boolean));
  }
  return items;
}

function json(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json", ...CORS_HEADERS },
  });
}

export default {
  // Inbound email from Cloudflare Email Routing. `received_at` is when it arrived, which a run
  // reads to ack mail it could not file once its hold has passed (UNMATCHED_HOLD_DAYS).
  async email(message, env) {
    const { id, record } = await newsletterRecord(message);
    await env.NEWSLETTERS.put(id, JSON.stringify({ id, ...record, received_at: new Date().toISOString() }));
  },

  // HTTP: cyris pulls (GET /newsletters) and clears (POST /ack).
  async fetch(request, env) {
    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: CORS_HEADERS });
    }

    const auth = request.headers.get("Authorization");
    // An unset secret would otherwise accept the literal header "Bearer undefined".
    if (!env.NEWSLETTER_TOKEN || auth !== `Bearer ${env.NEWSLETTER_TOKEN}`) {
      return json({ error: "unauthorized" }, 401);
    }

    const { pathname } = new URL(request.url);

    if (request.method === "GET" && pathname === "/newsletters") {
      return json(await queued(env.NEWSLETTERS));
    }

    if (request.method === "POST" && pathname === "/ack") {
      const body = await request.json().catch(() => null);
      if (!Array.isArray(body?.ids)) return json({ error: "missing ids" }, 400);
      await Promise.all(body.ids.map((id) => env.NEWSLETTERS.delete(id)));
      return json({ ok: true });
    }

    return json({ error: "not found" }, 404);
  },
};
