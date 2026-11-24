// What a hosted tenant's sources list says of each RSS feed's poll: TriageServer's `health` object
// (`_health_json`, src/cyris/entrypoints/triage_server.py), judged by the rules
// `FeedHealth.findings` (src/cyris/adapters/store/feed_health.py) judges `cyris doctor` and the
// self-hosted page by. Both read their two thresholds from src/cyris/feed_health_rules.json, so
// neither side copies a number. No workerd-only imports: vitest runs this under Node.

import RULES from "../../../src/cyris/feed_health_rules.json";

export const UNHEALTHY_FAILURE_STREAK = RULES.unhealthy_failure_streak;
export const QUIET_DAYS = RULES.quiet_days;

const DAY_MS = 24 * 60 * 60 * 1000;

// A stored time as milliseconds: `_utc`'s reading, an ISO-8601 stamp with no zone being UTC.
const utc = (stamp) => Date.parse(/(?:Z|[+-]\d\d:?\d\d)$/i.test(stamp) ? stamp : `${stamp.replace(" ", "T")}Z`);

// Why a feed is unhealthy, one code per reason, as `FeedHealth.findings`.
export function findings(health, now) {
  const found = [];
  if (health.consecutive_failures >= UNHEALTHY_FAILURE_STREAK) found.push("failing");
  if (health.newest_article_at === null) found.push("never_stored");
  else if (now - utc(health.newest_article_at) >= QUIET_DAYS * DAY_MS) found.push("quiet");
  return found;
}

// The last poll error on one line, as `FeedHealth.error_line`.
const errorLine = (error) => (error ?? "").split(/\s+/).filter(Boolean).join(" ");

// One finding in `t`'s words, as `_feed_problem`.
function problem(t, health, code) {
  if (code === "failing") {
    const count = health.consecutive_failures;
    const error = errorLine(health.last_error);
    return error ? t("settings_api.feed_failing_error", { count, error }) : t("settings_api.feed_failing", { count });
  }
  if (code === "never_stored") return t("settings_api.feed_never_stored");
  return t("settings_api.feed_quiet", { days: QUIET_DAYS });
}

// A feed's poll record with the problems `cyris doctor` would name, as `_health_json`; null for a
// feed with no record (a source nothing polls). `health` is the row `TenantDb.sourceList` reads.
export function healthJson(t, health, now) {
  if (!health) return null;
  return {
    consecutive_failures: health.consecutive_failures,
    last_error: health.last_error,
    last_failed_at: health.last_failed_at,
    last_ok_at: health.last_ok_at,
    newest_article_at: health.newest_article_at,
    problems: findings(health, now).map((code) => problem(t, health, code)),
  };
}
