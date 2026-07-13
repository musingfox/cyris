// Cloudflare Email Worker: receives forwarded newsletters, stores parsed fields
// in KV; cyris pulls them via GET /newsletters and clears them via POST /ack.
// Mirrors workers/promote (Bearer auth, list/ack over KV), plus an email() handler.
import PostalMime from "postal-mime";

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

async function sha256(text) {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

export default {
  // Inbound email from Cloudflare Email Routing.
  async email(message, env) {
    const parsed = await PostalMime.parse(message.raw);
    // Prefer the original sender's From header (survives Gmail auto-forward);
    // fall back to the envelope sender.
    const from = parsed.from?.address || message.from || "";
    const record = {
      from,
      subject: parsed.subject || "",
      html: parsed.html || "",
      text: parsed.text || "",
      date: (parsed.date ? new Date(parsed.date) : new Date()).toISOString(),
    };
    // Dedup by sender+subject+date so a re-delivered copy overwrites, not duplicates.
    const key = `nl:${await sha256(`${from}|${record.subject}|${record.date}`)}`;
    await env.NEWSLETTERS.put(key, JSON.stringify({ id: key, ...record }));
  },

  // HTTP: cyris pulls (GET /newsletters) and clears (POST /ack).
  async fetch(request, env) {
    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: CORS_HEADERS });
    }

    const auth = request.headers.get("Authorization");
    if (auth !== `Bearer ${env.NEWSLETTER_TOKEN}`) {
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
