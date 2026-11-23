// A hosted tenant's own settings and sources, edited by its signed-in reader on the hosted Worker:
// a page the Worker renders itself and the JSON routes it posts to, over the tenant's Durable
// Object. No container starts for any of it. Why here and not in TriageServer or the run:
// docs/decisions/0040-a-hosted-tenant-edits-its-settings-on-the-hosted-worker.md.
// No workerd-only imports: vitest runs this under Node.
//
//   GET  /t/<tenant>/settings              the page
//   GET  /t/<tenant>/api/settings          the writable settings, the schedule and the caps
//   POST /t/<tenant>/api/settings          {key: value, ...}: writable keys and the schedule's parts
//   GET  /t/<tenant>/api/sources           the sources, RSS and newsletter
//   POST /t/<tenant>/api/sources           {name, url, lane}: one RSS source added or rewritten
//   POST /t/<tenant>/api/sources/delete    {name}: one source removed
//
// The router has checked the session and that its tenant is the path's (hosted_router.js); every
// call here goes to that tenant's object and registry row, and nothing in a body or a path names
// a tenant. Every string a reader sees is a key of src/cyris/locales/*.json (ADR-0027), worded in
// the catalog the tenant's own output language reaches.

import EN from "../../../src/cyris/locales/en.json";
import ZH_HANT from "../../../src/cyris/locales/zh-Hant.json";
import LANGUAGES from "../../../src/cyris/service_layer/languages.json";
import { MessageFormat } from "../../../src/cyris/entrypoints/static/vendor/messageformat.js";
import { LANES } from "./hosted_seed.js";
import { POLICY, catalogTag, checkSettings, rssSource, SCHEDULE_KEY, TIMEZONE_KEY } from "./hosted_settings_rules.js";
import { boundedText } from "./hosted_vote.js";
import { sameOrigin } from "./hosted_session.js";
import { DEFAULT_MAX_DIGEST_TIMES, checkSchedule, registrySchedule } from "./run_queue.js";
import { HOSTED_TIMES, PERIODS, digestHours } from "./schedule.js";

export const SETTINGS_PAGE = "/settings";
const SETTINGS_API = "/api/settings";
const SOURCES_API = "/api/sources";
const DELETE_SOURCE_API = "/api/sources/delete";
const ROUTES = new Set([SETTINGS_PAGE, SETTINGS_API, SOURCES_API, DELETE_SOURCE_API]);

export const isSettingsPath = (rest) => ROUTES.has(rest);

// Every file of src/cyris/locales, by its tag; tests/test_hosted_settings_policy.py holds the two
// lists equal. English is the fallback of every key, as `i18n.translator`'s.
export const CATALOGS = {
  en: EN,
  "zh-Hant": Object.fromEntries(Object.entries(ZH_HANT).map(([key, entry]) => [key, entry.text])),
};

const formatters = new Map();
const raise = (error) => {
  throw error;
};

// `t` for one catalog: a key's message formatted in that catalog's locale. The legal pages
// (hosted_legal.js) word from both catalogs through it too.
export function translator(tag) {
  return (key, values = {}) => {
    if (!(key in EN)) throw new Error(`${key} is not in en.json`);
    const [locale, source] = key in CATALOGS[tag] ? [tag, CATALOGS[tag][key]] : ["en", EN[key]];
    const id = `${locale}\u0000${key}`;
    if (!formatters.has(id)) formatters.set(id, new MessageFormat(locale, source, { bidiIsolation: "none" }));
    return formatters.get(id).format(values, raise);
  };
}

const escape = (text) =>
  String(text).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

// Each refusal's words, by its reason (hosted_settings_rules.js, TenantDb); one literal key each,
// so tests/test_catalog_coverage.py reads every one.
const REFUSALS = {
  body: (t, v) => t("hosted_settings.refused_body", v),
  empty: (t, v) => t("hosted_settings.refused_empty", v),
  too_large: (t, v) => t("hosted_settings.refused_too_large", v),
  cross_origin: (t, v) => t("hosted_settings.refused_cross_origin", v),
  method: (t, v) => t("hosted_settings.refused_method", v),
  not_seeded: (t, v) => t("hosted_settings.refused_not_seeded", v),
  unknown_key: (t, v) => t("hosted_settings.refused_unknown_key", v),
  operator_key: (t, v) => t("hosted_settings.refused_operator_key", v),
  whole_number: (t, v) => t("hosted_settings.refused_whole_number", v),
  on_off: (t, v) => t("hosted_settings.refused_on_off", v),
  text: (t, v) => t("hosted_settings.refused_text", v),
  blank: (t, v) => t("hosted_settings.refused_blank", v),
  type_scale: (t, v) => t("hosted_settings.refused_type_scale", v),
  schedule: (t, v) => t("hosted_settings.refused_schedule", v),
  schedule_one: (t, v) => t("hosted_settings.refused_schedule_one", v),
  time_zone: (t, v) => t("hosted_settings.refused_time_zone", v),
  source_name: (t, v) => t("hosted_settings.refused_source_name", v),
  lane: (t, v) => t("hosted_settings.refused_lane", v),
  url_invalid: (t, v) => t("hosted_settings.refused_url_invalid", v),
  url_length: (t, v) => t("hosted_settings.refused_url_length", v),
  url_scheme: (t, v) => t("hosted_settings.refused_url_scheme", v),
  url_port: (t, v) => t("hosted_settings.refused_url_port", v),
  url_userinfo: (t, v) => t("hosted_settings.refused_url_userinfo", v),
  url_host: (t, v) => t("hosted_settings.refused_url_host", v),
  url_local: (t, v) => t("hosted_settings.refused_url_local", v),
  url_service: (t, v) => t("hosted_settings.refused_url_service", v),
  source_cap: (t, v) => t("hosted_settings.refused_source_cap", v),
  source_taken: (t, v) => t("hosted_settings.refused_source_taken", v),
  source_missing: (t, v) => t("hosted_settings.refused_source_missing", v),
};
export const REFUSAL_REASONS = Object.freeze(Object.keys(REFUSALS));

// A refusal's words: a value's refusal names the field by its label, a key's by the key itself.
function refusalText(t, { reason, key, ...values }) {
  const label = key && `settings_field.${key}` in EN ? t(`settings_field.${key}`) : key;
  return REFUSALS[reason](t, { ...values, ...(key ? { key, label } : {}) });
}

const STATUS = {
  cross_origin: 403,
  too_large: 413,
  not_seeded: 409,
  source_cap: 409,
  source_taken: 409,
  source_missing: 404,
  method: 405,
};

const json = (body, status = 200) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", "Cache-Control": "no-store" },
  });

const refused = (t, refusal) =>
  json({ ok: false, error: refusalText(t, refusal), reason: refusal.reason }, STATUS[refusal.reason] ?? 400);

// The body's JSON, or a refusal: past `max_body_bytes` unread (hosted_vote.js `boundedText`).
async function bodyOf(request) {
  let text;
  try {
    text = await boundedText(request, POLICY.max_body_bytes);
  } catch {
    return { refused: { reason: "body" } };
  }
  if (text === null) return { refused: { reason: "too_large" } };
  try {
    return { body: JSON.parse(text) };
  } catch {
    return { refused: { reason: "body" } };
  }
}

// `deps`: `db`, the session's tenant's object (TenantDb: settings, saveSettings, sources,
// saveSource, deleteSource); `registryRow()`, that tenant's registry row; `setSchedule({times,
// timeZone})`, which writes the registry's schedule and its copy and answers `{refused}` or not;
// and `ownHosts`, this Worker's host names, which no feed may name.
export async function handleTenantSettings(request, { rest, db, registryRow, setSchedule, ownHosts = [] }) {
  const method = request.method === "HEAD" ? "GET" : request.method;
  const stored = await db.settings();
  const tag = catalogTag(stored?.["digest.output_language"], Object.keys(CATALOGS));
  const t = translator(tag);
  const post = rest !== SETTINGS_PAGE && method === "POST";
  if (method !== "GET" && !post) return refused(t, { reason: "method" });
  if (rest === DELETE_SOURCE_API && !post) return refused(t, { reason: "method" });
  // A form or fetch from another site carries the reader's Lax cookie on no POST but a top-level
  // form's, and that one sends its own Origin; a request with none, or `null`, is not this page's.
  if (post && !sameOrigin(request)) return refused(t, { reason: "cross_origin" });

  if (rest === SETTINGS_PAGE) return page(tag, t, stored, await db.sources(), await registryRow());
  if (stored === null) return refused(t, { reason: "not_seeded" });

  if (rest === SETTINGS_API && !post) {
    const row = await registryRow();
    return json({
      settings: Object.fromEntries(Object.entries(stored).filter(([key]) => key !== SCHEDULE_KEY && key !== TIMEZONE_KEY)),
      schedule: registrySchedule(row),
      max_digest_times: row?.max_digest_times ?? DEFAULT_MAX_DIGEST_TIMES,
      max_articles_per_digest: POLICY.max_articles_per_digest,
      max_sources: POLICY.max_sources,
    });
  }
  if (rest === SOURCES_API && !post) return json({ sources: await db.sources(), max_sources: POLICY.max_sources });

  const read = await bodyOf(request);
  if (read.refused) return refused(t, read.refused);
  const hosts = [...new Set([new URL(request.url).hostname, ...ownHosts].map((host) => host.toLowerCase()))];
  if (rest === SETTINGS_API) return saveSettings(t, read.body, { db, registryRow, setSchedule });
  if (rest === SOURCES_API) {
    const made = rssSource(read.body, hosts);
    if (made.refused) return refused(t, made.refused);
    const saved = await db.saveSource(made.row, POLICY.max_sources);
    if (saved.refused) return refused(t, { reason: saved.refused, name: made.row.name, max: String(POLICY.max_sources) });
    return json({ ok: true, source: saved.source, note: t("settings.item_saved", { name: made.row.name, note: t("settings_api.effective_next_run") }) });
  }
  const name = read.body?.name;
  if (typeof name !== "string" || name === "") return refused(t, { reason: "source_name" });
  const deleted = await db.deleteSource(name);
  if (deleted.refused) return refused(t, { reason: deleted.refused });
  if (deleted.deleted === 0) return refused(t, { reason: "source_missing", name });
  return json({ ok: true, note: t("settings.source_retired", { name, note: t("settings_api.effective_next_run") }) });
}

// One save: every key checked before anything is written. The schedule goes to the registry,
// which the tick reads (ADR-0032), with the hours or zone it does not name kept; the run cap is
// never named, so the registry keeps the operator's. The whole save lands or none of it: the
// registry can still refuse what passed the check here (a tenant no longer registered), so the
// schedule is written first and the rows only once it is, and rows refused or failing then put
// the schedule the registry held back.
async function saveSettings(t, body, { db, registryRow, setSchedule }) {
  const checked = checkSettings(body);
  if (checked.refused) return refused(t, checked.refused);
  let schedule = null;
  let previous = null;
  if (checked.schedule) {
    const row = await registryRow();
    previous = registrySchedule(row);
    const current = previous ?? {};
    schedule = { times: checked.schedule.times ?? current.times, timeZone: checked.schedule.timeZone ?? current.timeZone };
    if (!digestHours(schedule.times, HOSTED_TIMES)) return refused(t, { reason: "schedule" });
    const check = checkSchedule(schedule, row?.max_digest_times ?? DEFAULT_MAX_DIGEST_TIMES);
    if (check.tooMany) return refused(t, { reason: "schedule_one" });
    if (check.refused) return refused(t, { reason: "time_zone", zone: String(schedule.timeZone) });
    const answer = await setSchedule(schedule);
    if (answer.refused) return refused(t, { reason: "time_zone", zone: String(schedule.timeZone) });
  }
  if (checked.rows.length) {
    let saved;
    try {
      saved = await db.saveSettings(checked.rows);
    } catch (error) {
      if (schedule && previous) await setSchedule(previous);
      throw error;
    }
    if (saved.refused) {
      if (schedule && previous) await setSchedule(previous);
      return refused(t, { reason: saved.refused, key: saved.key });
    }
  }
  return json({
    ok: true,
    saved: [...checked.rows.map(([key]) => key), ...(schedule ? [SCHEDULE_KEY, TIMEZONE_KEY] : [])],
    note: t("settings.item_saved", { name: t("site.settings"), note: t("settings_api.effective_next_run") }),
  });
}

// The page's one script: each form posts its fields as JSON to its own action, a route of this
// tenant's (relative, so the script names no tenant), and shows the answer. A field that holds
// `data-was`, the schedule's hours and zone, is sent only when the reader changed it, so a save of
// another setting leaves the registry's schedule as it is; an hour select left empty is no digest.
// Allowed by its hash.
// The page is served `Referrer-Policy: no-referrer` (hosted_pages.js), under which a browser sends
// a POST's Origin as `null` (Fetch, "append a request `Origin` header") and the save would be
// refused cross-origin; each fetch names `same-origin`, which sends this origin to this origin
// and nothing elsewhere.
const SCRIPT = `
const changed = (field) => !("was" in field.dataset) || field.value !== field.dataset.was;
const read = (field) => {
  if (field.dataset.kind === "number") return field.value.trim() === "" ? null : Number(field.value);
  if (field.dataset.kind === "boolean") return field.value === "true";
  return field.value;
};
for (const form of document.querySelectorAll("form[data-status]")) {
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const body = {};
    for (const field of form.querySelectorAll("[data-key]")) if (changed(field)) body[field.dataset.key] = read(field);
    const selects = [...form.querySelectorAll("[data-hour]")];
    const hours = selects.filter((hour) => hour.value !== "").map((hour) => hour.value.padStart(2, "0") + ":00");
    if (selects.some(changed)) body["general.digest_schedule"] = hours;
    let answer = {};
    let ok = false;
    try {
      const response = await fetch(form.action, {
        method: "POST",
        referrerPolicy: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      ok = response.ok;
      answer = await response.json();
    } catch {}
    const out = document.getElementById(form.dataset.status);
    out.textContent = answer.note ?? answer.error ?? form.dataset.failed;
    out.className = ok ? "notice ok" : "notice err";
    out.hidden = false;
    if (ok && form.dataset.reload !== undefined) location.reload();
  });
}
`;

const sha256Base64 = async (text) =>
  btoa(String.fromCharCode(...new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text)))));
let scriptHash;

// Not sandboxed, unlike every page a run wrote (hosted_pages.js): this one is the Worker's own,
// holds no text a feed or a model wrote but the tenant's own values, each escaped, and runs one
// script, by hash. It may post to this origin and nowhere else.
export async function settingsPolicy() {
  scriptHash ??= sha256Base64(SCRIPT);
  return [
    "default-src 'none'",
    `script-src 'sha256-${await scriptHash}'`,
    "style-src 'unsafe-inline'",
    "connect-src 'self'",
    "form-action 'self'",
    "base-uri 'none'",
    "frame-ancestors 'none'",
  ].join("; ");
}

// The tokens of docs/design/ui-language.md §2 the page uses, as _tokens.css.j2 has them
// (tests/test_hosted_settings_policy.py), and its components in their spec's shape.
const STYLE = `
:root{--bg:#07070a;--bg-elev:#0d0d12;--surface:#11111a;--surface-2:#161622;--border:#1f1f2e;--border-strong:#2a2a40;
--text:#ebebf2;--text-dim:#8a8aa0;--accent:#c6ff3d;--warn:#ff5b8a;--accent-tint:rgba(198, 255, 61, 0.07);--warn-tint:rgba(255, 91, 138, 0.12);
--s-1:4px;--s-2:8px;--s-3:12px;--s-4:16px;--s-6:24px;--s-8:32px;--s-12:48px;
--font-sans:'Geist', system-ui, -apple-system, sans-serif;--font-mono:'Geist Mono', ui-monospace, monospace;
--r-control:6px;--bar-h:60px;--measure:640px}
*{margin:0;padding:0;box-sizing:border-box}
body{background:var(--bg);color:var(--text);font:16px/1.5 var(--font-sans)}
.site-bar{position:sticky;top:0;height:var(--bar-h);background:rgba(7,7,10,.88);border-bottom:1px solid var(--border)}
.site-bar-inner{max-width:1240px;margin:0 auto;height:100%;padding:0 var(--s-6);display:flex;align-items:center;justify-content:space-between}
.brand{display:flex;align-items:center;gap:var(--s-2);color:var(--text);text-decoration:none;font-weight:600;letter-spacing:.08em}
.brand-mark{width:var(--s-3);height:var(--s-3);background:var(--accent);box-shadow:0 0 var(--s-3) rgba(198,255,61,.45)}
.site-nav{display:flex;gap:var(--s-4)}
.label{font-family:var(--font-mono);font-size:12px;letter-spacing:.08em;text-transform:uppercase;color:var(--text-dim);text-decoration:none}
.site-nav a[aria-current=page]{color:var(--accent)}
.container{max-width:1240px;margin:0 auto;padding:var(--s-8) var(--s-6) var(--s-12)}
.page-head{margin-bottom:var(--s-8)}
.display{font-size:32px;font-weight:600;line-height:1.2;margin:var(--s-2) 0}
.small{font-size:14px;color:var(--text-dim);max-width:var(--measure)}
.heading{font-size:19px;font-weight:600;margin-bottom:var(--s-4)}
.panel{background:var(--surface);border:1px solid var(--border);padding:var(--s-6);margin-bottom:var(--s-6)}
.field{margin-bottom:var(--s-4);max-width:var(--measure)}
.field .small{margin:var(--s-1) 0 var(--s-2)}
.input,.select{width:100%;padding:var(--s-2) var(--s-3);background:var(--bg);color:var(--text);border:1px solid var(--border);border-radius:var(--r-control);font:inherit}
.input.narrow,.select.narrow{max-width:160px}
.hours{display:flex;gap:var(--s-3)}
.btn{padding:var(--s-2) var(--s-4);border-radius:var(--r-control);border:1px solid var(--border-strong);background:var(--surface-2);color:var(--text);font:inherit;cursor:pointer}
.btn.primary{background:var(--accent);border-color:var(--accent);color:var(--bg);font-weight:600}
.btn.primary:hover{background:#d4ff66}
.btn.danger{color:var(--warn)}
.actions-line{display:flex;gap:var(--s-3);align-items:center;flex-wrap:wrap;margin-top:var(--s-4)}
.notice{padding:var(--s-2) var(--s-3);border:1px solid var(--border);font-size:14px}
.notice.ok{border-color:var(--accent);background:var(--accent-tint)}
.notice.err{border-color:var(--warn);background:var(--warn-tint)}
.list-row{display:grid;grid-template-columns:minmax(0,2fr) auto auto minmax(0,3fr) auto;gap:var(--s-3);align-items:center;padding:var(--s-2) 0;border-bottom:1px solid var(--border)}
.list-row .url{font-family:var(--font-mono);font-size:12px;color:var(--text-dim);overflow-wrap:anywhere}
.add-source{display:grid;grid-template-columns:minmax(0,2fr) minmax(0,3fr) auto auto;gap:var(--s-3);align-items:end;margin-top:var(--s-4)}
@media (max-width:720px){.list-row,.add-source{grid-template-columns:1fr}}
`;

const option = (value, label, selected) =>
  `<option value="${escape(value)}"${selected ? " selected" : ""}>${escape(label)}</option>`;

function field(t, key, help, control) {
  const id = key.replaceAll(".", "-");
  return (
    `<div class="field"><label class="label" for="${id}">${escape(t(`settings_field.${key}`))}</label>` +
    `${help ? `<p class="small">${escape(help)}</p>` : ""}${control(id)}</div>`
  );
}

const numberInput = (key, value, min, max) => (id) =>
  `<input class="input narrow" id="${id}" data-key="${key}" data-kind="number" type="number" min="${min}" max="${max}" step="1" value="${escape(value ?? "")}">`;

const onOffSelect = (t, key, value) => (id) =>
  `<select class="select narrow" id="${id}" data-key="${key}" data-kind="boolean">` +
  option("false", t("settings_option.off"), value !== true) +
  option("true", t("settings_option.on"), value === true) +
  "</select>";

function languageSelect(value) {
  const languages = Object.entries(LANGUAGES).filter(([tag]) => tag !== "_comment");
  if (typeof value === "string" && !languages.some(([tag]) => tag === value)) languages.push([value, value]);
  return (id) =>
    `<select class="select" id="${id}" data-key="digest.output_language">` +
    languages.map(([tag, name]) => option(tag, name, tag === value)).join("") +
    "</select>";
}

const typeScaleSelect = (t, value) => (id) =>
  `<select class="select narrow" id="${id}" data-key="digest.type_scale" data-kind="number">` +
  option("0.875", t("settings_option.type_scale_smaller"), value === 0.875) +
  option("1", t("settings_option.type_scale_default"), value === 1) +
  option("1.125", t("settings_option.type_scale_larger"), value === 1.125) +
  "</select>";

// One hour's select; its period names it, so the script sends the hours in the page's order. With
// `none`, the label of the choice of no hour, it is first in the select and the one selected when
// the registry holds no hour for this period. Otherwise a missing hour shows 00:00, as the browser
// does, and `data-was` says so.
function hourSelect(label, which, hour, none = null) {
  const hours = Array.from({ length: 24 }, (_, h) => option(String(h), `${String(h).padStart(2, "0")}:00`, h === hour));
  const was = hour ?? (none === null ? 0 : "");
  if (none !== null) hours.unshift(option("", none, hour === undefined));
  return (
    `<label class="small">${escape(label)} ` +
    `<select class="select narrow" data-hour="${which}" data-was="${was}">${hours.join("")}</select></label>`
  );
}

// The hours a tenant may choose: one, its morning's, labelled as the day's only issue, or two when
// the operator allows them (`max_digest_times`, ADR-0042), morning and evening; the evening select
// offers no second digest, selected while the registry holds one hour.
function hourSelects(t, hours, most) {
  if (most < 2) return hourSelect(t("settings_option.schedule_daily"), PERIODS[0], hours[0]);
  const [morning, evening] = PERIODS;
  return (
    hourSelect(t("settings_option.schedule_morning"), morning, hours[0]) +
    hourSelect(t("settings_option.schedule_evening"), evening, hours[1], t("settings_option.schedule_none"))
  );
}

function settingsForm(t, values, row) {
  const schedule = registrySchedule(row);
  const hours = digestHours(schedule?.times, HOSTED_TIMES) ?? [];
  const most = row?.max_digest_times ?? DEFAULT_MAX_DIGEST_TIMES;
  const status = "settings-result";
  return (
    `<form class="panel" action="api/settings" method="post" data-status="${status}" data-failed="${escape(t("hosted_settings.failed"))}" novalidate>` +
    `<h2 class="heading">${escape(t("settings.tab_digest"))}</h2>` +
    field(t, "digest.output_language", t("settings_help.digest.output_language"), languageSelect(values["digest.output_language"])) +
    field(t, "digest.style_prompt", t("settings_help.digest.style_prompt"), (id) => `<textarea class="input" id="${id}" data-key="digest.style_prompt" rows="4">${escape(values["digest.style_prompt"] ?? "")}</textarea>`) +
    field(t, "digest.type_scale", t("settings_help.digest.type_scale"), typeScaleSelect(t, values["digest.type_scale"])) +
    field(t, "digest.rank_by_preference", t("settings_help.digest.rank_by_preference"), onOffSelect(t, "digest.rank_by_preference", values["digest.rank_by_preference"])) +
    field(t, "digest.summarize_follow", t("settings_help.digest.summarize_follow"), onOffSelect(t, "digest.summarize_follow", values["digest.summarize_follow"])) +
    field(t, "general.digest_window_hours", t("settings_help.general.digest_window_hours"), numberInput("general.digest_window_hours", values["general.digest_window_hours"], 1, 168)) +
    field(t, "digest.max_articles_per_digest", t("settings_help.digest.max_articles_per_digest"), numberInput("digest.max_articles_per_digest", values["digest.max_articles_per_digest"], 1, POLICY.max_articles_per_digest)) +
    field(t, "routing.score_threshold", t("settings_help.routing.score_threshold"), numberInput("routing.score_threshold", values["routing.score_threshold"], 0, 100)) +
    field(t, SCHEDULE_KEY, "", () => `<div class="hours">${hourSelects(t, hours, most)}</div>`) +
    field(t, TIMEZONE_KEY, t("settings_help.general.timezone"), (id) => `<input class="input narrow" id="${id}" data-key="${TIMEZONE_KEY}" type="text" autocomplete="off" data-was="${escape(schedule?.timeZone ?? "")}" value="${escape(schedule?.timeZone ?? "")}">`) +
    `<p class="small">${escape(t("hosted_settings.operator_note"))}</p>` +
    `<div class="actions-line"><button class="btn primary" type="submit">${escape(t("settings.save"))}</button>` +
    `<div class="notice" id="${status}" role="status" hidden></div></div></form>`
  );
}

function sourcesPanel(t, sources) {
  const status = "sources-result";
  const failed = escape(t("hosted_settings.failed"));
  const rows = sources.length
    ? sources
        .map(
          (source) =>
            `<div class="list-row"><span>${escape(source.name)}</span>` +
            `<span class="label">${escape(t(`source_type.${source.type === "newsletter" ? "newsletter" : "rss"}`))}</span>` +
            `<span class="label">${source.lane && LANES.includes(source.lane) ? escape(t(`lane.${source.lane}`)) : ""}</span>` +
            `<span class="url">${escape(source.url ?? "")}</span>` +
            `<form action="api/sources/delete" method="post" data-status="${status}" data-failed="${failed}" data-reload>` +
            `<input type="hidden" data-key="name" value="${escape(source.name)}">` +
            `<button class="btn danger" type="submit">${escape(t("settings.remove"))}</button></form></div>`,
        )
        .join("")
    : `<p class="small">${escape(t("settings.no_sources"))}</p>`;
  const lanes = LANES.map((lane) => option(lane, t(`lane.${lane}`), lane === "skim")).join("");
  const full = sources.length >= POLICY.max_sources;
  return (
    `<section class="panel"><h2 class="heading">${escape(t("settings.tab_sources"))}</h2>` +
    `<p class="small">${escape(t("settings.sources_intro"))} ${escape(t("hosted_settings.sources_count", { count: String(sources.length), max: String(POLICY.max_sources) }))}</p>` +
    rows +
    `<form class="add-source" action="api/sources" method="post" data-status="${status}" data-failed="${failed}" data-reload>` +
    `<label class="label">${escape(t("settings.col_name"))}<input class="input" data-key="name" type="text" maxlength="80" required></label>` +
    `<label class="label">${escape(t("settings.feed_url"))}<input class="input" data-key="url" type="url" maxlength="${POLICY.source_url.max_length}" required></label>` +
    `<label class="label">${escape(t("settings.col_lane"))}<select class="select" data-key="lane">${lanes}</select></label>` +
    `<button class="btn primary" type="submit"${full ? " disabled" : ""}>${escape(t("settings.add_source"))}</button></form>` +
    `<div class="actions-line"><div class="notice" id="${status}" role="status" hidden></div></div></section>`
  );
}

async function page(tag, t, stored, sources, row) {
  const title = t("site.settings");
  const body =
    stored === null || sources === null
      ? `<div class="notice err">${escape(t("hosted_settings.refused_not_seeded"))}</div>`
      : settingsForm(t, stored, row) + sourcesPanel(t, sources);
  const html =
    `<!doctype html><html lang="${tag}"><head><meta charset="utf-8">` +
    '<meta name="viewport" content="width=device-width, initial-scale=1">' +
    `<title>${escape(title)}</title><style>${STYLE}</style></head><body>` +
    '<header class="site-bar"><div class="site-bar-inner">' +
    '<a class="brand" href="./"><span class="brand-mark"></span><span class="brand-name">CYRIS</span></a>' +
    `<nav class="site-nav" aria-label="${escape(t("site.nav"))}">` +
    `<a class="label" href="index.html">${escape(t("site.archive"))}</a>` +
    `<a class="label" href="settings" aria-current="page">${escape(title)}</a>` +
    `<a class="label" href="/logout">${escape(t("hosted_settings.sign_out"))}</a></nav></div></header>` +
    `<main class="container"><div class="page-head"><span class="label">${escape(title)}</span>` +
    `<h1 class="display">${escape(title)}</h1><p class="small">${escape(t("hosted_settings.intro"))}</p></div>` +
    `${body}</main><script>${SCRIPT}</script></body></html>`;
  return new Response(html, {
    status: 200,
    headers: {
      "Content-Type": "text/html; charset=utf-8",
      "Cache-Control": "no-store",
      "Content-Security-Policy": await settingsPolicy(),
    },
  });
}
