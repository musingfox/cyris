// A hosted reader's sign-in pages and the mail that carries the link: an address on the
// operator's invite list asks for a link, opens it, and gets a session for the tenant the
// registry names. docs/decisions/0033-hosted-readers-sign-in-with-a-mailed-link.md.
// No workerd-only imports: vitest runs this under Node.
//
//   GET  /login            the form that asks for a link
//   POST /login            mails a link if the address is on the list; one answer either way
//   GET  /login/confirm    the page the link opens: it moves the token from the fragment into a form
//   POST /login/confirm    spends the link, sets the session cookie, and sends the reader to /t/<tenant>/
//   GET  /logout           the sign-out button
//   POST /logout           ends the session
//
// The token travels in the link's fragment and then in a POST body, never in a URL a server or
// a log sees, and the confirm page asks for a press: a mail scanner that opens the link spends
// nothing. Every string a reader sees is a key of src/cyris/locales/en.json (ADR-0027).

import EN from "../../../src/cyris/locales/en.json";
import { MessageFormat } from "../../../src/cyris/entrypoints/static/vendor/messageformat.js";
import { PRIVATE_HEADERS } from "./hosted_pages.js";
import {
  SIGN_IN_TTL_MS,
  clearedSessionCookie,
  crossSite,
  isEmail,
  isToken,
  newToken,
  normalEmail,
  sessionCookie,
  sessionToken,
  tokenHash,
} from "./hosted_session.js";

export const LOGIN_PATHS = new Set(["/login", "/login/confirm", "/logout"]);

// The tenant's own language is in its database, which no one signed out may read, so the
// sign-in is worded in English.
const LANGUAGE = "en";
const formatters = new Map();
const raise = (error) => {
  throw error;
};

function t(key, values = {}) {
  if (!(key in EN)) throw new Error(`${key} is not in en.json`);
  if (!formatters.has(key)) formatters.set(key, new MessageFormat(LANGUAGE, EN[key], { bidiIsolation: "none" }));
  return formatters.get(key).format(values, raise);
}

const escape = (text) =>
  String(text).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

const MINUTES = String(SIGN_IN_TTL_MS / 60_000);

// The mail's link and its words: the token in the fragment, and the form to ask again.
export function signInMail({ to, domain, token }) {
  const lines = [
    t("sign_in.mail_open"),
    `https://${domain}/login/confirm#${token}`,
    t("sign_in.mail_once", { minutes: MINUTES }),
    t("sign_in.mail_later", { login: `https://${domain}/login` }),
  ];
  return { to, subject: t("sign_in.mail_subject"), text: lines.join("\n\n") };
}

// `signIn(email)`: issues a link for `email` through `issue(email, codeHash)` (the registry's
// `issueSignIn`) and mails it from `from` through `mail`, the Email Service binding. Answers
// `{sent, tenant, expires_at}`, `{refused}` when the registry issued none (an address off the
// list, one asking again within the gap, or one past SIGN_IN_DAILY_CAP or SIGN_IN_HOURLY_CAP), or
// `{error}` when the Worker cannot mail or the mail was refused. The token exists only here and
// in the mail; an error names the binding's code, never the token or the address.
export function createSignIn({ issue, mail, from, domain }) {
  return async (rawEmail) => {
    const unset = [!mail && "MAIL binding", !from && "CYRIS_HOSTED_MAIL_FROM", !domain && "CYRIS_STORE_HOSTED_DOMAIN"];
    if (unset.some(Boolean)) return { error: `the Worker has no ${unset.filter(Boolean).join(", ")}, so no link can be mailed` };
    const email = normalEmail(rawEmail);
    const token = newToken();
    const issued = await issue(email, await tokenHash(token));
    if (!issued.tenant) {
      // A cap reached is the operator's to see, since it also holds a reader's own link back; the
      // line names the cap, never the address.
      if (issued.capped) console.warn(`sign-in mail held: the ${issued.capped} cap is reached`);
      return { refused: issued.refused };
    }
    try {
      await mail.send({ ...signInMail({ to: email, domain, token }), from });
    } catch (error) {
      const code = String(error?.code ?? error?.name ?? "unknown error");
      console.error("sign-in mail not sent:", code);
      return { error: `the sign-in mail was not sent: ${code}` };
    }
    return { sent: true, tenant: issued.tenant, expires_at: issued.expires_at };
  };
}

const STYLE =
  ":root{--bg:#07070a;--surface:#11111a;--border:#1f1f2e;--text:#ebebf2;--text-dim:#8a8aa0;--accent:#c6ff3d;" +
  "--r-control:6px;--font-sans:system-ui,-apple-system,sans-serif}" +
  "*{margin:0;padding:0;box-sizing:border-box}" +
  "body{background:var(--bg);color:var(--text);font:16px/1.5 var(--font-sans);display:grid;place-items:center;min-height:100vh;padding:24px}" +
  "main{width:100%;max-width:420px;background:var(--surface);border:1px solid var(--border);border-radius:var(--r-control);padding:32px}" +
  "h1{font-size:20px;margin-bottom:16px}p{color:var(--text-dim);margin-bottom:16px}" +
  "label{display:block;font-size:13px;color:var(--text-dim);margin-bottom:8px}" +
  "input{width:100%;padding:10px 12px;margin-bottom:16px;background:var(--bg);color:var(--text);border:1px solid var(--border);border-radius:var(--r-control);font:inherit}" +
  "button{padding:10px 16px;background:var(--accent);color:var(--bg);border:0;border-radius:var(--r-control);font:inherit;font-weight:600;cursor:pointer}" +
  "a{color:var(--accent)}";

// Moves the link's token from the fragment into the form, then drops the fragment from the
// address bar and the history, so a later visit or a shared screen does not carry it.
const CONFIRM_SCRIPT =
  "const token = location.hash.slice(1);" +
  "history.replaceState(null, '', location.pathname);" +
  "document.getElementById('token').value = token;" +
  "document.getElementById(token ? 'confirm' : 'missing').hidden = false;";

const sha256Base64 = async (text) =>
  btoa(String.fromCharCode(...new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text)))));
let confirmHash;

// The one script any sign-in page runs is CONFIRM_SCRIPT, allowed by its hash.
async function pagePolicy() {
  confirmHash ??= sha256Base64(CONFIRM_SCRIPT);
  return [
    "default-src 'none'",
    `script-src 'sha256-${await confirmHash}'`,
    "style-src 'unsafe-inline'",
    "form-action 'self'",
    "base-uri 'none'",
    "frame-ancestors 'none'",
  ].join("; ");
}

async function page(title, body, { status = 200, script = "", cookie } = {}) {
  const html =
    `<!doctype html><html lang="${LANGUAGE}"><head><meta charset="utf-8">` +
    '<meta name="viewport" content="width=device-width, initial-scale=1">' +
    `<title>${escape(title)}</title><style>${STYLE}</style></head>` +
    `<body><main><h1>${escape(title)}</h1>${body}</main>${script && `<script>${script}</script>`}</body></html>`;
  const headers = new Headers({
    "Content-Type": "text/html; charset=utf-8",
    "Cache-Control": "no-store",
    "Content-Security-Policy": await pagePolicy(),
    ...PRIVATE_HEADERS,
  });
  if (cookie) headers.append("Set-Cookie", cookie);
  return new Response(html, { status, headers });
}

function redirect(location, cookie) {
  const headers = new Headers({ Location: location, "Cache-Control": "no-store", ...PRIVATE_HEADERS });
  if (cookie) headers.append("Set-Cookie", cookie);
  return new Response(null, { status: 303, headers });
}

const askForm = () =>
  `<form method="post" action="/login"><label for="email">${escape(t("sign_in.email_label"))}</label>` +
  '<input id="email" name="email" type="email" autocomplete="email" required>' +
  `<button type="submit">${escape(t("sign_in.send"))}</button></form>`;

const askAgain = () => `<p><a href="/login">${escape(t("sign_in.ask_again"))}</a></p>`;

const refused = () =>
  new Response(JSON.stringify({ error: "a sign-in form is sent from this site" }), {
    status: 403,
    headers: { "Content-Type": "application/json", "Cache-Control": "no-store", ...PRIVATE_HEADERS },
  });

// The form fields of a POST, or an empty set for a body that is not a form.
async function formOf(request) {
  try {
    return new URLSearchParams(await request.text());
  } catch {
    return new URLSearchParams();
  }
}

// Answers a request on one of LOGIN_PATHS. `deps`: `signIn(email)` (createSignIn's), `waitUntil`,
// `redeem(codeHash, sessionHash)` (the registry's `redeemSignIn`) and `endSession(sessionHash)`.
export async function handleLogin(request, deps) {
  const path = new URL(request.url).pathname;
  const method = request.method === "HEAD" ? "GET" : request.method;
  if (method !== "GET" && method !== "POST") return page(t("sign_in.title"), askForm(), { status: 405 });
  if (method === "POST" && crossSite(request)) return refused();

  if (path === "/login") {
    if (method === "GET") return page(t("sign_in.title"), `<p>${escape(t("sign_in.intro"))}</p>${askForm()}`);
    // The registry is asked, and the mail sent, after the answer: the answer and its timing are
    // the same for an address on the list and one off it.
    const email = normalEmail((await formOf(request)).get("email"));
    if (isEmail(email)) {
      deps.waitUntil(deps.signIn(email).catch((error) => console.error("sign-in failed:", String(error?.message ?? error))));
    }
    return page(t("sign_in.title"), `<p>${escape(t("sign_in.sent", { minutes: MINUTES }))}</p>${askAgain()}`);
  }

  if (path === "/login/confirm") {
    if (method === "GET") {
      const body =
        `<form id="confirm" method="post" action="/login/confirm" hidden><input id="token" name="token" type="hidden">` +
        `<button type="submit">${escape(t("sign_in.confirm"))}</button></form>` +
        `<div id="missing" hidden><p>${escape(t("sign_in.confirm_missing"))}</p>${askAgain()}</div>`;
      return page(t("sign_in.confirm_title"), body, { script: CONFIRM_SCRIPT });
    }
    const token = (await formOf(request)).get("token");
    const session = newToken();
    const redeemed = isToken(token) ? await deps.redeem(await tokenHash(token), await tokenHash(session)) : null;
    if (!redeemed) {
      return page(t("sign_in.confirm_title"), `<p>${escape(t("sign_in.failed"))}</p>${askAgain()}`, { status: 400 });
    }
    return redirect(`/t/${redeemed.tenant}/`, sessionCookie(session));
  }

  // /logout
  if (method === "GET") {
    const body = `<form method="post" action="/logout"><button type="submit">${escape(t("sign_in.sign_out"))}</button></form>`;
    return page(t("sign_in.sign_out_title"), body);
  }
  const token = sessionToken(request);
  if (token) await deps.endSession(await tokenHash(token));
  return redirect("/login", clearedSessionCookie());
}
