// The one settings page every edition serves, on the hosted Worker (ADR-0043): the page
// TriageServer renders from settings.html.j2, built per catalog with its /static/ files by
// scripts/build_shared_settings.py, and the JSON settings.js reads in TriageServer's shape,
// answered over the session's tenant's object with the keys the hosted policy locks. No container
// starts for any of it (ADR-0040). No workerd-only imports: vitest runs this under Node.
//
// STUB (the edition-parity plan): the page lives at /t/<tenant>/settings-next, beside the page
// hosted_settings.js renders, which stays as it is until M3 replaces it. It saves what that page
// saves: the keys the policy lets a tenant write, and the schedule's hours and zone, which go to
// the registry; the policy's `shared_page` locks every other key for the operator.
//
//   GET  /t/<tenant>/settings-next   the page, in the catalog the tenant's output language reaches
//   GET  /static/<file>              the page's scripts, its style and its browser messages
//   GET  /api/settings               values, fields, what is missing, `locked` and `limits`
//   POST /api/settings/values        {values: {key: value}}: an open key is saved, a locked one refused
//   POST /api/settings/schedule      {times: [...]}: the hours, through the registry
//   GET  /api/sources, /api/topics   STUB: read-only and empty
//
// The router has checked the session (hosted_router.js). The tenant is the session's: no path,
// body or header names one, so these paths sit outside /t/<tenant>/ and its routes for the
// hosted page, which take another body.

import FIELDS from "../../../src/cyris/settings_fields.json";
import PROVIDER_DEFAULTS from "../../../src/cyris/provider_defaults.json";
import STARTING_VALUES from "../../../src/cyris/settings_starting_values.json";
import BUILT from "./shared_settings.build.json";
import { CATALOGS, STATUS, bodyOf, refusalText, translator } from "./hosted_settings.js";
import { POLICY, SCHEDULE_KEY, TIMEZONE_KEY, catalogTag, checkSettings } from "./hosted_settings_rules.js";
import { sameOrigin } from "./hosted_session.js";
import { DEFAULT_MAX_DIGEST_TIMES, checkSchedule, registrySchedule } from "./run_queue.js";
import { HOSTED_TIMES, digestHours } from "./schedule.js";

export const SHARED_PAGE = "/settings-next";
const SETTINGS_API = "/api/settings";
const VALUES_API = "/api/settings/values";
const SCHEDULE_API = "/api/settings/schedule";
const WRITES = new Set([VALUES_API, SCHEDULE_API]);
// STUB: the reads the page makes of the tabs this milestone leaves empty, each answered as a
// deployment with no writable table answers it.
const EMPTY_READS = {
  "/api/sources": { writable: false, sources: [] },
  "/api/topics": { writable: false, topics: [], embedding_model: "" },
};
const STATIC = "/static/";
const OUTPUT_LANGUAGE = "digest.output_language";
const ARTICLE_CAP = "digest.max_articles_per_digest";

// The paths outside /t/<tenant>/ this module answers, to a live session of any tenant.
export const isSharedPath = (path) =>
  path === SETTINGS_API || WRITES.has(path) || Object.hasOwn(EMPTY_READS, path) || path.startsWith(STATIC);

const json = (body, status = 200) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", "Cache-Control": "no-store" },
  });

// TriageServer's refusal shape (`_refused`): the reason, and the field the page marks.
const refused = (error, field, status = 400) => json({ ok: false, error, ...(field ? { field } : {}) }, status);
// A refusal of hosted_settings.js's reason, worded from the catalog, for the field it names.
const refusedFor = (t, refusal, field = refusal.key ?? null) => refused(refusalText(t, refusal), field, STATUS[refusal.reason] ?? 400);

// The keys a tenant writes somewhere: the policy's, and the schedule, which goes to the registry.
const TENANT_WRITES = new Set([...POLICY.writable_settings, SCHEDULE_KEY, TIMEZONE_KEY]);

// Each grade-D key the shared page locks for every tenant, with its reason (ADR-0043): every key
// no tenant writes is the operator's.
export function tenantLocks() {
  const reason = POLICY.shared_page.operator_reason;
  return Object.fromEntries(Object.keys(FIELDS).filter((key) => !TENANT_WRITES.has(key)).map((key) => [key, { reason }]));
}

// The first locked key whose value `sent` changes from `current`, or null. A route that saves
// several keys at once refuses only a change to a locked one, as TriageServer's
// `_refuse_locked_changes` does: sent as it is held, a locked key is no change.
function changedLock(locks, sent, current) {
  return Object.keys(sent).find((key) => Object.hasOwn(locks, key) && JSON.stringify(sent[key]) !== JSON.stringify(current[key] ?? null)) ?? null;
}

const lockRefusal = (t, locks, key) =>
  refused(t("settings_api.locked", { label: t(`settings_field.${key}`), reason: t(`lock_reason.${locks[key].reason}`) }), key);

// What the tenant may set at most, as `GET /api/settings` names it: the registry's most digest
// times a day, which the operator sets, and the policy's article cap.
const limitsOf = (row) => ({
  digest_times_max: row?.max_digest_times ?? DEFAULT_MAX_DIGEST_TIMES,
  max_articles_per_digest_max: POLICY.max_articles_per_digest,
});

// A provider's entry in TriageServer's shape: its key is ready only when every variable its run
// reads it from (BUILT.key_env, TriageServer's `key_env`) is among those the operator set, which
// are what each run container is handed (hosted_container.js `handedSecrets`).
function providerEntry(name, defaultModel, variables = [], secrets) {
  return { name, env_var: variables[0] ?? "", default_model: defaultModel, configured: variables.every((variable) => secrets.has(variable)) };
}

// The keys read from the tenant's own rows: every grade-D key but what the run pins and the
// schedule, which is the registry's.
const PINNED = new Set(["llm_provider.provider", "llm_provider.model", ...POLICY.blank_settings]);
const STORED_KEYS = Object.keys(FIELDS).filter((key) => !PINNED.has(key) && key !== SCHEDULE_KEY && key !== TIMEZONE_KEY);

// What a hosted run takes for each grade-D key: the tenant's rows with what it does not decide put
// back, as `pin_tenant_settings` (src/cyris/adapters/hosted.py) puts it back as a run's settings
// load (the policy's provider and model, blank notify targets, an article cap at most the
// policy's), and the schedule the registry holds, which the tick reads (ADR-0032). Null for a key
// with no row.
export function hostedValues(stored, schedule) {
  const values = Object.fromEntries(Object.keys(FIELDS).map((key) => [key, stored?.[key] ?? null]));
  values["llm_provider.provider"] = POLICY.provider;
  values["llm_provider.model"] = POLICY.model;
  for (const key of POLICY.blank_settings) values[key] = "";
  if (Number.isInteger(values[ARTICLE_CAP]) && values[ARTICLE_CAP] > POLICY.max_articles_per_digest) {
    values[ARTICLE_CAP] = POLICY.max_articles_per_digest;
  }
  values[SCHEDULE_KEY] = schedule?.times ?? null;
  values[TIMEZONE_KEY] = schedule?.timeZone ?? null;
  return values;
}

// `GET /api/settings` in TriageServer's shape (`_handle_get_settings`). A hosted run's keys are
// the operator's (hosted_container.js `HOSTED_SECRETS`), so the page names only the provider and
// embedder the run uses, ready when the operator set their keys; every choice of them is locked.
// Clef is offered by no tenant.
function settingsAnswer(t, stored, row, secrets, locks) {
  const values = hostedValues(stored, registrySchedule(row));
  const absent = Object.keys(FIELDS).filter((key) => values[key] === null);
  const embedder = values["vote_similarity.provider"];
  return {
    values,
    fields: Object.fromEntries(Object.entries(FIELDS).map(([key, field]) => [key, { ...field, label: t(`settings_field.${key}`) }])),
    missing: absent.sort(),
    sent_as_shown: BUILT.sent_as_shown,
    starting: Object.fromEntries(Object.entries(STARTING_VALUES).filter(([key]) => absent.includes(key))),
    providers: [providerEntry(POLICY.provider, PROVIDER_DEFAULTS.llm_models[POLICY.provider], BUILT.key_env.providers[POLICY.provider], secrets)],
    embedding_providers: Object.hasOwn(PROVIDER_DEFAULTS.embedding, embedder ?? "")
      ? [providerEntry(embedder, PROVIDER_DEFAULTS.embedding[embedder].model, BUILT.key_env.embedding_providers[embedder], secrets)]
      : [],
    clef_ready: false,
    languages: BUILT.languages,
    writable: stored !== null,
    locked: locks,
    limits: limitsOf(row),
  };
}

// Names in `t`'s words for a list, as TriageServer's `_list`: "A, B, C".
const listOf = (t, [head, ...rest]) => rest.reduce((joined, item) => t("settings.list_more", { head: joined, item }), head);

// When the saved keys take effect, as TriageServer's `_values_note`: a live key reaches the pages,
// the rest the next run.
function valuesNote(t, keys) {
  const live = keys.filter((key) => FIELDS[key].live).map((key) => t(`settings_field.${key}`));
  if (!live.length) return t("settings_api.effective_next_run");
  if (live.length === keys.length) return t("settings_api.pages_within_a_minute");
  return t("settings_api.some_within_a_minute", { labels: listOf(t, live) });
}

// The schedule `previous` held with `change` put over it, written to the registry, or a refusal:
// the hours one or two whole ones and no more than the registry's maximum, and a zone the
// registry stores, as hosted_settings.js's save holds them. `setSchedule` writes the registry and
// its copy in the tenant (hosted.js).
async function writeSchedule(t, change, { row, previous, setSchedule }) {
  const schedule = { times: change.times ?? previous?.times, timeZone: change.timeZone ?? previous?.timeZone };
  const field = change.times ? SCHEDULE_KEY : TIMEZONE_KEY;
  if (!digestHours(schedule.times, HOSTED_TIMES)) return { refused: refusedFor(t, { reason: "schedule" }, field) };
  const check = checkSchedule(schedule, row?.max_digest_times ?? DEFAULT_MAX_DIGEST_TIMES);
  if (check.tooMany) return { refused: refusedFor(t, { reason: "schedule_one" }, SCHEDULE_KEY) };
  const zone = { reason: "time_zone", zone: String(schedule.timeZone) };
  if (check.refused) return { refused: refusedFor(t, zone, TIMEZONE_KEY) };
  const answer = await setSchedule({ times: check.schedule.digestTimes, timeZone: check.schedule.timeZone });
  if (answer.refused) return { refused: refusedFor(t, zone, TIMEZONE_KEY) };
  return { schedule: check.schedule };
}

// `POST /api/settings/values` in TriageServer's shape (`_handle_post_values`): every key checked
// in its order before anything is written, the whole save or none of it. A locked key is refused
// before its own rule is asked, as TriageServer refuses one; an open key is held to the rule
// hosted_settings_rules.js holds it to, and written through TenantDb.saveSettings, which refuses
// a key the policy does not let a tenant write whoever calls it. The zone is the registry's, as
// hosted_settings.js saves it: written there first, and put back if the rows are refused or fail.
async function saveValues(t, values, { db, stored, locks, registryRow, setSchedule }) {
  if (values === null || typeof values !== "object" || Array.isArray(values) || !Object.keys(values).length) {
    return refused(t("settings_api.values_empty"));
  }
  const rows = [];
  let zone;
  for (const [key, value] of Object.entries(values)) {
    const label = Object.hasOwn(FIELDS, key) ? t(`settings_field.${key}`) : null;
    if (label === null) return refused(t("settings_api.not_a_setting", { key: `'${key}'` }));
    if (Object.hasOwn(locks, key)) return lockRefusal(t, locks, key);
    if (FIELDS[key].route !== "plain") return refused(t("settings_api.own_form", { label }), key);
    if (key === TIMEZONE_KEY) {
      zone = value;
      continue;
    }
    const checked = checkSettings({ [key]: value });
    if (checked.refused) return refusedFor(t, checked.refused);
    rows.push(...checked.rows);
  }
  let previous = null;
  const written = Object.fromEntries(rows);
  if (zone !== undefined) {
    const row = await registryRow();
    previous = registrySchedule(row);
    const wrote = await writeSchedule(t, { timeZone: zone }, { row, previous, setSchedule });
    if (wrote.refused) return wrote.refused;
    written[TIMEZONE_KEY] = wrote.schedule.timeZone;
  }
  if (rows.length) {
    let saved;
    try {
      saved = await db.saveSettings(rows);
    } catch (error) {
      if (previous) await setSchedule(previous);
      throw error;
    }
    if (saved.refused) {
      if (previous) await setSchedule(previous);
      return refused(refusalText(t, { reason: saved.refused, key: saved.key }), saved.key, saved.status);
    }
  }
  const language = written[OUTPUT_LANGUAGE] ?? stored[OUTPUT_LANGUAGE];
  return json({
    ok: true,
    values: written,
    note: valuesNote(t, Object.keys(written)),
    // The catalog the page is worded from now, as TriageServer's `ui_lang`.
    ui_lang: catalogTag(language, Object.keys(CATALOGS)),
  });
}

// `POST /api/settings/schedule` in TriageServer's shape (`_handle_post_schedule`): `{times}`, the
// hours, written through the registry with the zone it holds kept, and answered sorted. Refused
// as a lock only when the hours change from the registry's.
async function saveSchedule(t, body, { locks, registryRow, setSchedule }) {
  const times = (Array.isArray(body?.times) ? body.times : []).map((time) => String(time).trim());
  const row = await registryRow();
  const previous = registrySchedule(row);
  const locked = changedLock(locks, { [SCHEDULE_KEY]: times }, { [SCHEDULE_KEY]: previous?.times });
  if (locked) return lockRefusal(t, locks, locked);
  const wrote = await writeSchedule(t, { times }, { row, previous, setSchedule });
  if (wrote.refused) return wrote.refused;
  return json({ ok: true, times: wrote.schedule.digestTimes, note: t("settings_api.effective_next_tick") });
}

// The page's policy, its own and none of the sandboxed site's: scripts and styles from this
// Worker's /static/ and the build's hashes of the page's inline ones (its probe and its layout),
// fonts as every page loads them, fetches to this origin, and no form, base or frame.
export function sharedPagePolicy(origin, page) {
  const hashes = (list) => list.map((hash) => `'${hash}'`).join(" ");
  return [
    "default-src 'none'",
    `script-src ${origin}${STATIC} ${hashes(page.script_hashes)}`,
    `style-src ${origin}${STATIC} ${hashes(page.style_hashes)} https://fonts.googleapis.com`,
    "font-src https://fonts.gstatic.com",
    "img-src 'self'",
    "connect-src 'self'",
    "form-action 'none'",
    "base-uri 'none'",
    "frame-ancestors 'none'",
  ].join("; ");
}

function servePage(request, tag) {
  const page = BUILT.pages[tag] ?? BUILT.pages.en;
  return new Response(page.html, {
    status: 200,
    headers: {
      "Content-Type": "text/html; charset=utf-8",
      "Cache-Control": "no-store",
      "Content-Security-Policy": sharedPagePolicy(new URL(request.url).origin, page),
    },
  });
}

// One built /static/ file, kept by the browser and asked for again each time, as TriageServer
// serves /static/ (`no-cache` with an ETag); private, since only a session reaches it.
function serveStatic(request, path) {
  const file = Object.hasOwn(BUILT.files, path) ? BUILT.files[path] : null;
  if (!file) return json({ error: "not found" }, 404);
  const headers = { "Content-Type": file.type, "Cache-Control": "private, no-cache", ETag: file.etag };
  const asked = (request.headers.get("If-None-Match") ?? "").split(",").map((tag) => tag.trim().replace(/^W\//, ""));
  if (asked.includes(file.etag)) return new Response(null, { status: 304, headers });
  return new Response(file.body, { status: 200, headers });
}

// `deps`: `db`, the session's tenant's object (TenantDb: settings, saveSettings); `registryRow()`,
// that tenant's registry row, whose schedule the page shows; `setSchedule({times, timeZone})`,
// which writes the registry's schedule and its copy and answers `{refused}` or not; `secrets`, the
// names of the operator's keys a run is handed; and `locks`, the policy's (`tenantLocks`) unless
// a test names others.
export async function handleSharedSettings(request, { path, db, registryRow, setSchedule, secrets = new Set(), locks = tenantLocks() }) {
  const method = request.method === "HEAD" ? "GET" : request.method;
  const post = WRITES.has(path);
  if (method !== (post ? "POST" : "GET")) return json({ ok: false, error: `${path} takes ${post ? "POST" : "GET"}` }, 405);
  if (path.startsWith(STATIC)) return serveStatic(request, path);
  if (Object.hasOwn(EMPTY_READS, path)) return json(EMPTY_READS[path]);
  const stored = await db.settings(STORED_KEYS);
  const tag = catalogTag(stored?.[OUTPUT_LANGUAGE], Object.keys(CATALOGS));
  if (path === SHARED_PAGE) return servePage(request, tag);
  const t = translator(tag);
  if (path === SETTINGS_API) return json(settingsAnswer(t, stored, await registryRow(), secrets, locks));
  // A same-origin fetch only, as every hosted settings write (hosted_session.js `sameOrigin`); its
  // `reason` is TriageServer's for the same refusal, which the page words as its own.
  if (!sameOrigin(request)) {
    return json({ ok: false, error: refusalText(t, { reason: "cross_origin" }), reason: "cross_origin" }, STATUS.cross_origin);
  }
  if (stored === null) return refusedFor(t, { reason: "not_seeded" }, null);
  const read = await bodyOf(request);
  if (read.refused) return refusedFor(t, read.refused, null);
  const deps = { db, stored, locks, registryRow, setSchedule };
  if (path === SCHEDULE_API) return saveSchedule(t, read.body, deps);
  return saveValues(t, read.body?.values, deps);
}
