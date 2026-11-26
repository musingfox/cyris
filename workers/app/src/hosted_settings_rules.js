// What a hosted tenant may write of its own settings and sources, value by value, as the policy
// file says (hosted_settings_policy.json): the rules the settings routes hold a save to
// (hosted_shared_settings.js) and the poll holds each feed URL to at every hop (tenant_buffer.js).
// docs/decisions/0040-a-hosted-tenant-edits-its-settings-on-the-hosted-worker.md.
// No workerd-only imports: vitest runs this under Node.
//
// A value is held to the rule `validate_setting` holds it to in the container (src/cyris/config.py),
// taken as JSON types only: a number for a number, a boolean for on and off.
// test/hosted_settings_cases.json is run by this suite and by tests/test_hosted_settings_policy.py.

import FIELDS from "../../../src/cyris/settings_fields.json";
import POLICY from "./hosted_settings_policy.json";
import { LANES, SCHEDULE_KEY, TIMEZONE_KEY } from "./hosted_seed.js";
import { HOSTED_TIMES, digestHours } from "./schedule.js";

export { POLICY, SCHEDULE_KEY, TIMEZONE_KEY };

// A refusal: the catalog key's tail (`hosted_settings.refused_<reason>`) and what it names.
const refuse = (reason, values = {}) => ({ refused: { reason, ...values } });

const whole = (min, max) => (value) =>
  typeof value === "number" && Number.isInteger(value) && value >= min && value <= max
    ? null
    : { reason: "whole_number", min: String(min), max: String(max) };
// `Field(ge=min)` with no upper bound in config.py: the cap on what a run spends is the tenant's
// monthly allowance (ADR-0038), not the key's range.
const wholeFrom = (min) => (value) =>
  typeof value === "number" && Number.isInteger(value) && value >= min ? null : { reason: "whole_number_min", min: String(min) };
const onOff = (value) => (typeof value === "boolean" ? null : { reason: "on_off" });
const text = (value) => (typeof value === "string" ? null : { reason: "text" });
// `_not_blank` in config.py: a string with something besides whitespace.
const nonBlank = (value) => text(value) ?? (value.trim() ? null : { reason: "blank" });
// `DigestConfig.type_scale`'s steps, which type_scale.js serves.
const TYPE_SCALES = [0.875, 1, 1.125];
const typeScale = (value) => (TYPE_SCALES.includes(value) ? null : { reason: "type_scale" });

// Every key of `writable_settings`, with its rule: the bounds config.py sets on the field, and
// for the article cap the policy's, which is tighter than the field's. A Map, so a key a request
// names is only ever one of these and never an object's own, as `__proto__` or `constructor`.
const RULES = new Map([
  ["digest.output_language", nonBlank],
  ["digest.style_prompt", text],
  ["digest.type_scale", typeScale],
  ["digest.max_articles_per_digest", whole(1, POLICY.max_articles_per_digest)],
  ["general.digest_window_hours", whole(1, 168)],
  ["digest.rank_by_preference", onOff],
  ["digest.summarize_follow", onOff],
  ["routing.score_threshold", whole(0, 100)],
  ["digest.vote_half_life_days", whole(1, 365)],
  ["digest.topic_lifetime_days", whole(1, 365)],
  ["vote_similarity.max_seeds", wholeFrom(1)],
  ["digest.scoring_snippet_length", wholeFrom(1)],
  ["digest.summarize_snippet_length", wholeFrom(1)],
  ["digest.filter_snippet_length", wholeFrom(1)],
]);
export const WRITABLE = Object.freeze([...RULES.keys()]);

// A key the operator alone sets: any other settings key, and what lives outside `settings`.
const operatorKey = (key) => Object.hasOwn(FIELDS, key) || key.startsWith("notify.") || POLICY.operator_keys.includes(key);

// `body`, a save's JSON, as `{rows, schedule}`: the settings rows to write, as [key, value], and
// the schedule's parts it names, or null for none. Or `{refused}` for the first key it refuses,
// nothing written: the whole save passes or none of it does.
export function checkSettings(body) {
  if (body === null || typeof body !== "object" || Array.isArray(body)) return refuse("body");
  const entries = Object.entries(body);
  if (entries.length === 0) return refuse("empty");
  const rows = [];
  let schedule = null;
  for (const [key, value] of entries) {
    if (key === SCHEDULE_KEY || key === TIMEZONE_KEY) {
      schedule = { ...schedule, [key === SCHEDULE_KEY ? "times" : "timeZone"]: value };
      continue;
    }
    const rule = RULES.get(key);
    if (!rule) return refuse(operatorKey(key) ? "operator_key" : "unknown_key", { key });
    const wrong = rule(value);
    if (wrong) return { refused: { ...wrong, key } };
    rows.push([key, value]);
  }
  if (schedule?.times !== undefined && !digestHours(schedule.times, HOSTED_TIMES)) return refuse("schedule");
  return { rows, schedule };
}

// The UI catalog a tenant's `digest.output_language` is worded from: the catalog of the same
// language and script, else English, as `i18n.resolve` matches a tag in the container.
export function catalogTag(language, catalogs) {
  let wanted;
  try {
    wanted = new Intl.Locale(String(language)).maximize();
  } catch {
    return "en";
  }
  for (const tag of catalogs) {
    const offered = new Intl.Locale(tag).maximize();
    if (offered.language === wanted.language && offered.script === wanted.script) return tag;
  }
  return "en";
}

const IPV4 = /^\d+\.\d+\.\d+\.\d+$/;
const endsWithAny = (host, suffixes) => suffixes.some((suffix) => host.endsWith(suffix));

// Why `raw` is no URL a tenant's feed may have, as a refusal reason, or null when it may. Held to
// at the save and at each hop of a poll: http(s) on its standard port, no user name or password, a
// public domain name and not an IP address, and not this Worker's own host (`ownHosts`, in lower
// case) or any workers.dev name. The host is the URL parser's, so a numeric host in any spelling
// it reads as an address (`0x7f.1`, `2130706433`) is caught as the address. No DNS lookup: a
// public name that resolves to a private address is not caught here (ADR-0040).
export function urlRefusal(raw, ownHosts = []) {
  const rules = POLICY.source_url;
  if (typeof raw !== "string" || raw.trim() === "") return { reason: "url_invalid" };
  if (raw.length > rules.max_length) return { reason: "url_length", max: String(rules.max_length) };
  let url;
  try {
    url = new URL(raw.trim());
  } catch {
    return { reason: "url_invalid" };
  }
  if (url.protocol !== "http:" && url.protocol !== "https:") return { reason: "url_scheme" };
  if (url.username || url.password) return { reason: "url_userinfo" };
  if (!rules.ports.includes(url.port)) return { reason: "url_port" };
  const host = url.hostname.toLowerCase().replace(/\.$/, "");
  if (host.startsWith("[") || IPV4.test(host) || !host.includes(".")) {
    return rules.local_hosts.includes(host) ? { reason: "url_local" } : { reason: "url_host" };
  }
  if (rules.local_hosts.includes(host) || endsWithAny(host, rules.local_suffixes)) return { reason: "url_local" };
  if (ownHosts.includes(host) || rules.service_hosts.includes(host) || endsWithAny(host, rules.service_suffixes)) {
    return { reason: "url_service" };
  }
  return null;
}

// As newsletterSource's (hosted_seed.js): what a source's name may be.
const NAME = /^[^\u0000-\u001f\u007f]{1,80}$/;

// The `sources` row an RSS source a tenant adds or rewrites is, from its JSON `{name, url, lane}`:
// lane `skim` unless named, as `SourceConfig` defaults it. Its config names the lane alone, so a
// rewrite keeps the tags and the rest the row held (TenantDb.saveSource), and a new source has
// `SourceConfig`'s own none. Or `{refused}`.
export function rssSource(body, ownHosts = []) {
  if (body === null || typeof body !== "object" || Array.isArray(body)) return refuse("body");
  const name = typeof body.name === "string" ? body.name.trim() : "";
  if (!NAME.test(name)) return refuse("source_name");
  const lane = body.lane ?? "skim";
  if (!LANES.includes(lane)) return refuse("lane");
  const wrong = urlRefusal(body.url, ownHosts);
  if (wrong) return { refused: wrong };
  return { row: { name, url: new URL(body.url.trim()).href, type: "rss", config: { lane } } };
}
