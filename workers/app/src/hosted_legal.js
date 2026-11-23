// The hosted service's privacy policy and terms of service, readable by anyone, signed in or not.
// Every fact the pages state must stay true of the code and configuration: a change to what the
// hosted Worker stores, where, or who receives it changes these pages in the same commit. A number
// the Worker holds is read from where the Worker holds it, and the rest from hosted_legal.json.
// No workerd-only imports: vitest runs this under Node.
//
//   GET /privacy    the privacy policy
//   GET /terms      the terms of service
//
// Traditional Chinese is the authoritative text and English a translation (product owner,
// 2026-11-24). Each page shows both on the one URL, the Chinese first, each block tagged with its
// own `lang`, and says on both that the Chinese prevails. No language is chosen for the reader:
// the page is read before any sign-in, so there is no tenant's language to follow, and a page
// picked by Accept-Language would show some readers a translation without the text it defers to.
// Every string is a key of src/cyris/locales/en.json with its zh-Hant entry (ADR-0027).

import LANGUAGES from "../../../src/cyris/service_layer/languages.json";
import LEGAL from "./hosted_legal.json";
import { PRIVATE_HEADERS } from "./hosted_pages.js";
import { SESSION_COOKIE, SESSION_TTL_MS, SIGN_IN_TTL_MS } from "./hosted_session.js";
import { translator } from "./hosted_settings.js";
import { POLICY } from "./hosted_settings_rules.js";

export const PRIVACY_PATH = "/privacy";
export const TERMS_PATH = "/terms";
export const LEGAL_PATHS = new Set([PRIVACY_PATH, TERMS_PATH]);

// The authoritative language first, then its translation.
const AUTHORITATIVE = "zh-Hant";
const TRANSLATION = "en";
// The page's chrome is English, and each block names its own language (ui-language.md §1.7).
const CHROME = "en";

const DAY_MS = 24 * 60 * 60 * 1000;
// The longest a deleted row stays in the backups: an export's lifetime, and the time R2 may take
// to remove it once expired, in whole days.
const BACKUP_MOST_DAYS = LEGAL.backup_days + Math.ceil(LEGAL.backup_removal_hours / 24);

const escape = (text) =>
  String(text).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

// A sentence, escaped, with each contact address in it made a mail link. The addresses are
// hosted_legal.json's, which hold no character escape() changes.
function linked(text) {
  let html = escape(text);
  for (const address of Object.values(LEGAL.contacts)) {
    html = html.replaceAll(address, `<a href="mailto:${address}">${address}</a>`);
  }
  return html;
}

const paragraph = (text) => `<p>${linked(text)}</p>`;
const section = (heading, body) => `<section><h2 class="heading">${escape(heading)}</h2>${body}</section>`;
const list = (items) => `<ul>${items.map((item) => `<li>${linked(item)}</li>`).join("")}</ul>`;

const contacts = (t) =>
  section(
    t("legal.contact_heading"),
    "<dl>" +
      [
        [t("legal.contact_privacy"), LEGAL.contacts.privacy],
        [t("legal.contact_support"), LEGAL.contacts.support],
        [t("legal.contact_security"), LEGAL.contacts.security],
      ]
        .map(([label, address]) => `<dt>${escape(label)}</dt><dd>${linked(address)}</dd>`)
        .join("") +
      "</dl>",
  );

function privacyBody(t) {
  const sessionDays = String(SESSION_TTL_MS / DAY_MS);
  const deletion = String(LEGAL.deletion_days);
  const answer = { see: String(LEGAL.answer_days.see), change: String(LEGAL.answer_days.change) };
  return (
    paragraph(t("privacy.intro")) +
    section(
      t("privacy.kept_heading"),
      list([
        t("privacy.kept_account"),
        t("privacy.kept_sign_in", { minutes: String(SIGN_IN_TTL_MS / 60_000), days: sessionDays }),
        t("privacy.kept_digest"),
        t("privacy.kept_newsletters", { days: String(LEGAL.newsletter_hold_days) }),
        t("privacy.kept_spend"),
        t("privacy.kept_logs", { days: String(LEGAL.log_days) }),
      ]),
    ) +
    section(t("privacy.where_heading"), paragraph(t("privacy.where"))) +
    section(t("privacy.use_heading"), paragraph(t("privacy.use")) + paragraph(t("privacy.required"))) +
    section(
      t("privacy.recipients_heading"),
      list([
        t("privacy.recipient_cloudflare"),
        t("privacy.recipient_anthropic", { model: POLICY.model }),
        t("privacy.recipient_discord"),
        t("privacy.recipient_browser"),
      ]),
    ) +
    section(t("privacy.cookies_heading"), paragraph(t("privacy.cookies", { cookie: SESSION_COOKIE, days: sessionDays }))) +
    section(t("privacy.operator_heading"), paragraph(t("privacy.operator"))) +
    section(
      t("privacy.retention_heading"),
      paragraph(
        t("privacy.retention", {
          deletion,
          months: String(LEGAL.inactive_months),
          reminder: String(LEGAL.inactive_reminder_days),
          end: String(LEGAL.end_notice_days),
          days: String(LEGAL.backup_days),
          hours: String(LEGAL.backup_removal_hours),
          most: String(BACKUP_MOST_DAYS),
        }),
      ),
    ) +
    section(
      t("privacy.safeguards_heading"),
      paragraph(t("privacy.safeguards")) + paragraph(t("privacy.security", { security: LEGAL.contacts.security })),
    ) +
    section(
      t("privacy.rights_heading"),
      paragraph(t("privacy.rights", { privacy: LEGAL.contacts.privacy, ...answer, deletion })) +
        paragraph(t("privacy.deletion", { days: deletion })),
    ) +
    paragraph(t("privacy.changes", { days: String(LEGAL.notice_days) })) +
    contacts(t)
  );
}

function termsBody(t) {
  return (
    paragraph(t("terms.intro")) +
    section(t("terms.beta_heading"), paragraph(t("terms.beta")) + paragraph(t("terms.age"))) +
    section(
      t("terms.availability_heading"),
      paragraph(t("terms.availability")) +
        paragraph(t("terms.end", { days: String(LEGAL.end_notice_days) })) +
        paragraph(t("terms.leave", { privacy: LEGAL.contacts.privacy })),
    ) +
    section(t("terms.use_heading"), paragraph(t("terms.use")) + paragraph(t("terms.content"))) +
    section(t("terms.liability_heading"), paragraph(t("terms.liability"))) +
    section(
      t("terms.law_heading"),
      paragraph(t("terms.law")) +
        paragraph(t("terms.forum", { support: LEGAL.contacts.support })) +
        paragraph(t("terms.severability")),
    ) +
    paragraph(t("terms.changes", { days: String(LEGAL.notice_days) })) +
    contacts(t)
  );
}

// Each page: its path, its title and description, and its body, by catalog.
const PAGES = {
  [PRIVACY_PATH]: {
    title: (t) => t("privacy.title"),
    description: (t) => t("privacy.description"),
    body: privacyBody,
  },
  [TERMS_PATH]: {
    title: (t) => t("terms.title"),
    description: (t) => t("terms.description"),
    body: termsBody,
  },
};

// The tokens of docs/design/ui-language.md §2 the page uses, as _tokens.css.j2 has them
// (tests/test_hosted_settings_policy.py), and its components as _components.css.j2 draws them: the
// site bar, the label, display, heading, body and small roles, the notice and the section marker
// (§6 *document*). No web font: the page loads nothing from another site.
const STYLE = `
:root{--bg:#07070a;--bg-elev:#0d0d12;--surface:#11111a;--border:#1f1f2e;--border-strong:#2a2a40;
--text:#ebebf2;--text-dim:#8a8aa0;--accent:#c6ff3d;--accent-tint:rgba(198, 255, 61, 0.07);
--s-1:4px;--s-2:8px;--s-3:12px;--s-4:16px;--s-5:20px;--s-6:24px;--s-8:32px;--s-12:48px;--s-16:64px;--s-20:80px;
--font-sans:'Geist', system-ui, -apple-system, sans-serif;--font-mono:'Geist Mono', ui-monospace, monospace;
--type-scale:1;--t-fast:150ms ease-out;--bar-h:60px;--measure:640px}
*{margin:0;padding:0;box-sizing:border-box}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.5}}
body{background:var(--bg);color:var(--text);font-family:var(--font-sans)}
.site-bar{position:sticky;top:0;z-index:10;height:var(--bar-h);background:rgba(7, 7, 10, .88);border-bottom:1px solid var(--border)}
.site-bar-inner{height:100%;max-width:960px;margin:0 auto;padding:0 var(--s-4);display:flex;align-items:center;justify-content:space-between;gap:var(--s-4)}
.brand{display:flex;align-items:center;gap:var(--s-3);text-decoration:none;color:var(--text)}
.brand-mark{width:12px;height:12px;background:var(--accent);box-shadow:0 0 12px rgba(198,255,61,.45);animation:pulse 2.4s ease-in-out infinite}
.brand-name{font-family:var(--font-mono);font-size:calc(14px * var(--type-scale));letter-spacing:.18em;font-weight:600}
.site-nav{display:flex;gap:var(--s-5)}
.site-nav a{text-decoration:none;padding:var(--s-1) 0;border-bottom:1px solid transparent;transition:color var(--t-fast), border-color var(--t-fast)}
.site-nav a:hover{color:var(--text)}
.site-nav a[aria-current="page"]{color:var(--text);border-bottom-color:var(--accent)}
.brand,.site-nav a{position:relative}
.brand::after,.site-nav a::after{content:"";position:absolute;inset:min(0px, calc((100% - 44px) / 2))}
.label{font-family:var(--font-mono);font-weight:500;font-size:calc(12px * var(--type-scale));text-transform:uppercase;letter-spacing:.1em;color:var(--text-dim)}
.page{max-width:960px;margin:0 auto;padding:var(--s-12) var(--s-4) var(--s-20)}
.page-head{display:grid;gap:var(--s-4);margin-bottom:var(--s-12)}
.display{font-weight:500;font-size:calc(clamp(46px, 7vw, 84px) * var(--type-scale));line-height:1;letter-spacing:-.02em;text-wrap:balance}
.heading{font-weight:600;font-size:calc(24px * var(--type-scale));letter-spacing:-.015em;line-height:1.3;color:var(--text)}
.small{font-size:calc(14px * var(--type-scale));line-height:1.5;color:var(--text-dim)}
.notice{max-width:var(--measure);padding:var(--s-2) var(--s-3);border-left:2px solid var(--accent);background:var(--accent-tint);font-size:calc(14px * var(--type-scale));line-height:1.5;display:grid;gap:var(--s-2)}
.versions{display:flex;flex-wrap:wrap;gap:var(--s-5)}
.versions a{text-decoration:none}
.versions a:hover{color:var(--text)}
.section-marker{display:flex;align-items:center;gap:var(--s-3)}
.section-marker::before{content:"";width:24px;height:1px;background:var(--accent)}
.doc{max-width:var(--measure);font-size:calc(18px * var(--type-scale));line-height:1.65;color:var(--text-dim)}
.doc + .doc{margin-top:var(--s-16);padding-top:var(--s-12);border-top:1px solid var(--border)}
.doc > * + *{margin-top:var(--s-6)}
.doc section > * + *{margin-top:var(--s-3)}
.doc ul{padding-left:var(--s-6);display:grid;gap:var(--s-3)}
.doc dl{display:grid;grid-template-columns:auto 1fr;gap:var(--s-2) var(--s-6)}
.doc dt{color:var(--text)}
.doc a{color:var(--text);overflow-wrap:anywhere;transition:color var(--t-fast)}
.doc a:hover{color:var(--accent)}
:focus-visible{outline:1px solid var(--accent);outline-offset:2px}
@media (max-width:720px){.site-nav{gap:var(--s-3)}.brand-name{display:none}.page{padding-top:var(--s-8)}.site-nav .label{letter-spacing:.08em}.doc dl{grid-template-columns:1fr}}
@media (prefers-reduced-motion:reduce){*,*::before,*::after{transition:none !important;animation:none !important}}
`;

const sha256Base64 = async (text) =>
  btoa(String.fromCharCode(...new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text)))));
let styleHash;

// Stricter than the sign-in pages' policy: no script at all, and the one style block by its hash.
// Nothing may load, post or frame it.
export async function legalPolicy() {
  styleHash ??= sha256Base64(STYLE);
  return [
    "default-src 'none'",
    `style-src 'sha256-${await styleHash}'`,
    "form-action 'none'",
    "base-uri 'none'",
    "frame-ancestors 'none'",
  ].join("; ");
}

// The site bar's words and the page head's lines, by catalog.
const labels = (t) => ({
  nav: t("legal.nav"),
  privacy: t("legal.privacy"),
  terms: t("legal.terms"),
  signIn: t("legal.sign_in"),
  versions: t("legal.versions"),
});
const headLines = (t) => ({ updated: t("legal.updated", { date: LEGAL.updated }), prevails: t("legal.prevails") });

function render(path) {
  const page = PAGES[path];
  const chrome = translator(CHROME);
  const words = labels(chrome);
  const [original, translation] = [translator(AUTHORITATIVE), translator(TRANSLATION)];
  const [originalHead, translationHead] = [headLines(original), headLines(translation)];
  const nav = (href, text) =>
    `<a class="label" href="${href}"${href === path ? ' aria-current="page"' : ""}>${escape(text)}</a>`;
  const version = (tag, t) =>
    `<article class="doc" id="${tag}" lang="${tag}">` +
    `<p class="label section-marker" lang="${CHROME}">${escape(LANGUAGES[tag])}</p>` +
    `<h2 class="heading">${escape(page.title(t))}</h2>${page.body(t)}</article>`;
  return (
    `<!doctype html><html lang="${CHROME}"><head><meta charset="utf-8">` +
    '<meta name="viewport" content="width=device-width, initial-scale=1">' +
    '<meta name="color-scheme" content="dark">' +
    `<meta name="description" content="${escape(page.description(chrome))}">` +
    `<title>${escape(`${page.title(original)} · ${page.title(translation)}`)}</title><style>${STYLE}</style></head><body>` +
    '<header class="site-bar"><div class="site-bar-inner">' +
    '<a class="brand" href="/login"><span class="brand-mark"></span><span class="brand-name">CYRIS</span></a>' +
    `<nav class="site-nav" aria-label="${escape(words.nav)}">` +
    nav(PRIVACY_PATH, words.privacy) +
    nav(TERMS_PATH, words.terms) +
    nav("/login", words.signIn) +
    "</nav></div></header>" +
    '<main class="page"><div class="page-head">' +
    `<h1 class="display" lang="${AUTHORITATIVE}">${escape(page.title(original))}</h1>` +
    `<p class="heading" lang="${TRANSLATION}">${escape(page.title(translation))}</p>` +
    `<p class="small"><span lang="${AUTHORITATIVE}">${escape(originalHead.updated)}</span> · ` +
    `<span lang="${TRANSLATION}">${escape(translationHead.updated)}</span></p>` +
    `<div class="notice"><p lang="${AUTHORITATIVE}">${escape(originalHead.prevails)}</p>` +
    `<p lang="${TRANSLATION}">${escape(translationHead.prevails)}</p></div>` +
    `<nav class="versions" aria-label="${escape(words.versions)}">` +
    [AUTHORITATIVE, TRANSLATION].map((tag) => `<a class="label" href="#${tag}" lang="${tag}">${escape(LANGUAGES[tag])}</a>`).join("") +
    "</nav></div>" +
    version(AUTHORITATIVE, original) +
    version(TRANSLATION, translation) +
    "</main></body></html>"
  );
}

// Answers GET or HEAD on one of LEGAL_PATHS with its page, and any other method with 405. No
// session is read: the pages are the same for everyone. The router adds nothing a page needs;
// the private headers are set here as well, so the page carries them whoever serves it.
export async function handleLegal(request) {
  const path = new URL(request.url).pathname;
  if (request.method !== "GET" && request.method !== "HEAD") {
    return new Response(JSON.stringify({ error: `${path} takes GET or HEAD` }), {
      status: 405,
      headers: { "Content-Type": "application/json", Allow: "GET, HEAD", "Cache-Control": "no-store", ...PRIVATE_HEADERS },
    });
  }
  return new Response(request.method === "HEAD" ? null : render(path), {
    status: 200,
    headers: {
      "Content-Type": "text/html; charset=utf-8",
      "Cache-Control": "no-store",
      "Content-Security-Policy": await legalPolicy(),
      ...PRIVATE_HEADERS,
    },
  });
}
