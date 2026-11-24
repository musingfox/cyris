// The words and the refusals of a hosted tenant's settings routes, which the shared settings page
// posts to (hosted_shared_settings.js, ADR-0043), and the translator the legal pages word from
// (hosted_legal.js). No workerd-only imports: vitest runs this under Node.
//
// Every string a reader sees is a key of src/cyris/locales/*.json (ADR-0027), worded in the catalog
// the tenant's own output language reaches.

import EN from "../../../src/cyris/locales/en.json";
import ZH_HANT from "../../../src/cyris/locales/zh-Hant.json";
import { MessageFormat } from "../../../src/cyris/entrypoints/static/vendor/messageformat.js";
import { POLICY } from "./hosted_settings_rules.js";
import { boundedText } from "./hosted_vote.js";

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

// Each refusal's words, by its reason (hosted_settings_rules.js, TenantDb); one literal key each,
// so tests/test_catalog_coverage.py reads every one.
const REFUSALS = {
  body: (t, v) => t("hosted_settings.refused_body", v),
  empty: (t, v) => t("hosted_settings.refused_empty", v),
  too_large: (t, v) => t("hosted_settings.refused_too_large", v),
  cross_origin: (t, v) => t("hosted_settings.refused_cross_origin", v),
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
};
export const REFUSAL_REASONS = Object.freeze(Object.keys(REFUSALS));

// A refusal's words: a value's refusal names the field by its label, a key's by the key itself.
export function refusalText(t, { reason, key, ...values }) {
  const label = key && `settings_field.${key}` in EN ? t(`settings_field.${key}`) : key;
  return REFUSALS[reason](t, { ...values, ...(key ? { key, label } : {}) });
}

export const STATUS = {
  cross_origin: 403,
  too_large: 413,
  not_seeded: 409,
  source_cap: 409,
  source_taken: 409,
  source_missing: 404,
};

// The body's JSON, or a refusal: past `max_body_bytes` unread (hosted_vote.js `boundedText`).
export async function bodyOf(request) {
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
