// The Worker in front of the cyris Container: the hourly tick, and the only
// door to the triage deck and /settings.
//
// Auth is two layers, and they answer different questions. Cloudflare Access
// sits on the route and decides *who* — email policy, MFA, audit log, no code
// here. The cookie below decides *whether this request carries the deployment's
// own secret*, so a misconfigured Access application is not the only thing
// between the internet and a write surface. Access is a dashboard step (§5
// grade B); this half deploys with a secret.
import { Container, getContainer } from "@cloudflare/containers";
import { env } from "cloudflare:workers";

const COOKIE = "cyris_session";

// Every secret the pipeline reads from the environment. The provider is a
// runtime setting in D1, so all three LLM keys ride along — passing only the
// configured one would make switching provider on /settings a redeploy.
const SECRETS = {
  CLOUDFLARE_ACCOUNT_ID: env.CLOUDFLARE_ACCOUNT_ID,
  CLOUDFLARE_API_TOKEN: env.CLOUDFLARE_API_TOKEN,
  CLOUDFLARE_EMBEDDING_API_TOKEN: env.CLOUDFLARE_EMBEDDING_API_TOKEN,
  CYRIS_D1_API_TOKEN: env.CYRIS_D1_API_TOKEN,
  CYRIS_PROMOTE_TOKEN: env.CYRIS_PROMOTE_TOKEN,
  CYRIS_NEWSLETTER_TOKEN: env.CYRIS_NEWSLETTER_TOKEN,
  CYRIS_RSS_TOKEN: env.CYRIS_RSS_TOKEN,
  CYRIS_DISCORD_WEBHOOK_URL: env.CYRIS_DISCORD_WEBHOOK_URL,
  ANTHROPIC_API_KEY: env.ANTHROPIC_API_KEY,
  GEMINI_API_KEY: env.GEMINI_API_KEY,
  OPENAI_API_KEY: env.OPENAI_API_KEY,
};

export class CyrisContainer extends Container {
  defaultPort = 8766;
  // Idle time is billed. §7 called the default 10 minutes ~10 container-hours
  // per 60 runs, which is why the hook below exists at all.
  sleepAfter = "5m";

  // Without a role the image runs the Mac mini's supercronic loop, which in the
  // cloud would be a second scheduler racing the Workers Cron above.
  envVars = { ...SECRETS, CYRIS_ROLE: "ui" };

  // Overriding this hook without stopping renews the timer instead of sleeping,
  // and the instance bills on. This is the whole point of the override.
  async onActivityExpired() {
    await this.stop();
  }

  onStop({ exitCode, reason }) {
    console.log("container stopped", { exitCode, reason });
  }

  onError(error) {
    console.log("container error:", error);
  }
}

const sha256 = async (text) => {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
};

const cookieOf = (request) =>
  (request.headers.get("Cookie") || "")
    .split(";")
    .map((c) => c.trim().split("="))
    .find(([k]) => k === COOKIE)?.[1];

// The cookie is the token's digest, so the secret itself is never at rest in a
// browser and the comparison is between two fixed-length hex strings.
async function authorized(request) {
  const token = env.CYRIS_UI_TOKEN;
  if (!token) return false;
  const cookie = cookieOf(request);
  return Boolean(cookie) && cookie === (await sha256(token));
}

const LOGIN_PAGE = (message) => `<!DOCTYPE html>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Cyris</title>
<style>
 body{background:#111;color:#eee;font:14px/1.6 system-ui,sans-serif;display:grid;place-items:center;height:100vh;margin:0}
 form{display:flex;flex-direction:column;gap:.8rem;width:min(20rem,80vw)}
 input,button{padding:.7rem;border-radius:6px;border:1px solid #333;font:inherit}
 input{background:#1b1b1b;color:#eee}
 button{background:#c6ff3d;color:#111;border:0;font-weight:600;cursor:pointer}
 p{color:#ff5b8a;margin:0}
</style>
<form method="POST" action="/login">
  <input type="password" name="token" placeholder="access token" autofocus autocomplete="current-password">
  <button type="submit">Enter</button>
  ${message ? `<p>${message}</p>` : ""}
</form>`;

export default {
  async fetch(request) {
    const url = new URL(request.url);

    if (url.pathname === "/login") {
      if (request.method !== "POST") return html(LOGIN_PAGE(""));
      const form = await request.formData();
      if (form.get("token") !== env.CYRIS_UI_TOKEN) return html(LOGIN_PAGE("Wrong token."), 401);
      return new Response(null, {
        status: 302,
        headers: {
          Location: "/",
          "Set-Cookie": `${COOKIE}=${await sha256(env.CYRIS_UI_TOKEN)}; HttpOnly; Secure; ` +
            "SameSite=Lax; Path=/; Max-Age=2592000",
        },
      });
    }

    if (!(await authorized(request))) {
      // A browser gets the form; anything else gets the status code, so an
      // unauthenticated API call fails loudly instead of parsing HTML.
      const wantsHtml = (request.headers.get("Accept") || "").includes("text/html");
      return wantsHtml ? html(LOGIN_PAGE(""), 401) : json({ error: "unauthorized" }, 401);
    }

    // The scheduled tick without waiting an hour for it.
    if (request.method === "POST" && url.pathname === "/run") {
      return json(await startRun());
    }

    return getContainer(env.CYRIS, "ui").fetch(request);
  },

  async scheduled() {
    await startRun();
  },
};

// A separate instance from the UI: this one runs the pipeline once and exits,
// so it stops billing without waiting for a sleep timer.
async function startRun() {
  await getContainer(env.CYRIS, "run").start({ envVars: { ...SECRETS, CYRIS_ROLE: "run" } });
  return { started: "run", at: new Date().toISOString() };
}

const html = (body, status = 200) =>
  new Response(body, { status, headers: { "Content-Type": "text/html; charset=utf-8" } });

const json = (body, status = 200) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
