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
  async fetch(request, env) {
    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: CORS_HEADERS });
    }

    const auth = request.headers.get("Authorization");
    if (auth !== `Bearer ${env.PROMOTE_TOKEN}`) {
      return json({ error: "unauthorized" }, 401);
    }

    const { pathname } = new URL(request.url);

    if (request.method === "POST" && pathname === "/promote") {
      const body = await request.json().catch(() => null);
      if (!body?.url) return json({ error: "missing url" }, 400);
      const key = `promote:${await sha256(body.url)}`;
      await env.PROMOTIONS.put(
        key,
        JSON.stringify({
          url: body.url,
          digest_date: body.digest_date ?? null,
          ts: new Date().toISOString(),
        })
      );
      return json({ ok: true });
    }

    if (request.method === "GET" && pathname === "/promotions") {
      const list = await env.PROMOTIONS.list({ prefix: "promote:" });
      const items = await Promise.all(
        list.keys.map(async ({ name }) => JSON.parse(await env.PROMOTIONS.get(name)))
      );
      return json(items.filter(Boolean));
    }

    if (request.method === "POST" && pathname === "/ack") {
      const body = await request.json().catch(() => null);
      if (!Array.isArray(body?.urls)) return json({ error: "missing urls" }, 400);
      await Promise.all(body.urls.map(async (u) => env.PROMOTIONS.delete(`promote:${await sha256(u)}`)));
      return json({ ok: true });
    }

    return json({ error: "not found" }, 404);
  },
};
