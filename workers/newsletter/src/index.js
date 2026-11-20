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

function json(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json", ...CORS_HEADERS },
  });
}

export default {
  // Inbound email from Cloudflare Email Routing.
  async email(message, env) {
    const { id, record } = await newsletterRecord(message);
    await env.NEWSLETTERS.put(id, JSON.stringify({ id, ...record }));
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
      const list = await env.NEWSLETTERS.list({ prefix: "nl:" });
      const items = await Promise.all(
        list.keys.map(async ({ name }) => JSON.parse(await env.NEWSLETTERS.get(name)))
      );
      return json(items.filter(Boolean));
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
