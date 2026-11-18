// The deployment's type size, read from D1 `settings` over REST, the way the
// container reads it: the Worker has no D1 binding. No workerd-only imports:
// vitest runs this under Node.
//
// A page view is not a run, so it never fails on this setting: a missing,
// invalid or unreadable value is 1. Each isolate asks at most once per TTL,
// failures included, because the REST API's rate limit is the one the
// pipeline's own D1 writes and Pages publish share. Given `read`, which
// answers rows or null as schedule.js's reads do, it asks that instead: the
// hosted Worker's reads go to the tenant's Durable Object (tenant_db.js).

// The steps of `DigestConfig.type_scale`; tests/test_type_scale_steps.py keeps
// the two, and the /settings select, equal.
export const TYPE_SCALES = [0.875, 1, 1.125];

const KEY = "digest.type_scale";
const FALLBACK = 1;
const ACCOUNTS_API = "https://api.cloudflare.com/client/v4/accounts";

export const typeScaleStyle = (scale) => `<style>html:root{--type-scale:${scale}}</style>`;

const SQL = "SELECT value FROM settings WHERE key = ?";

export function createTypeScale({
  env,
  fetchImpl,
  now = Date.now,
  ttlMs = 60000,
  timeoutMs = 1000,
  read: readRows,
}) {
  // The stored row over REST, or null when there is none to read.
  async function restRow() {
    const account = env.CLOUDFLARE_ACCOUNT_ID;
    const database = env.CYRIS_STORE_DATABASE_ID;
    const token = env.CLOUDFLARE_API_TOKEN;
    if (!account || !database || !token) return null;
    const response = await fetchImpl(`${ACCOUNTS_API}/${account}/d1/database/${database}/query`, {
      method: "POST",
      headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
      body: JSON.stringify({ sql: SQL, params: [KEY] }),
      signal: AbortSignal.timeout(timeoutMs),
    });
    // The status only: an error body or message may echo the request.
    if (!response.ok) {
      console.warn("type size read failed:", response.status);
      return null;
    }
    const data = await response.json();
    return (data.success && data.result?.[0]?.results?.[0]) || null;
  }

  async function read() {
    try {
      const row = readRows ? (await readRows(SQL, [KEY], "type size"))?.[0] : await restRow();
      if (!row) return FALLBACK;
      const value = JSON.parse(row.value);
      return TYPE_SCALES.includes(value) ? value : FALLBACK;
    } catch {
      console.warn("type size read failed");
      return FALLBACK;
    }
  }

  // The read in flight or settled, and when it goes stale. forget() drops it,
  // so a read already in flight cannot put the old value back.
  let held = null;
  return {
    current() {
      if (!held || now() >= held.expiresAt) {
        held = { value: read(), expiresAt: now() + ttlMs };
      }
      return held.value;
    },
    forget() {
      held = null;
    },
  };
}
