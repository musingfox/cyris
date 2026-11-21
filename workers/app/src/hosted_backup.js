// The hosted Worker's daily backup: once a day, from the hourly tick, every registered tenant's
// database and the operator's registry are dumped (sqlite_dump.js), written to the R2 bucket
// BACKUP, and copied to Backblaze B2 through its S3-compatible API (s3_sigv4.js), so a copy
// survives the Cloudflare account itself. A copy that fails is told to the operator through
// alertOperator, naming the tenant and the copy; a backup that did not run or finish is told by a
// later tick (checkBackup); and the operator restores one export into a new, empty tenant id
// (restoreExport). Durable Objects have point-in-time recovery but no export,
// so this is the only copy of a tenant's data outside its object.
// docs/decisions/0041-the-hosted-databases-are-exported-daily-to-r2-and-b2.md.

import { alertOperator, workerAlert } from "./hosted_alerts.js";
import WORDING from "./hosted_alerts.json";
import { b2Region, putObjectRequest, sha256Hex } from "./s3_sigv4.js";
import { DUMP_FORMAT, DUMP_VERSION } from "./sqlite_dump.js";
import { TENANT, tenantDb } from "./tenant_db.js";
import { refuseTenantId, tenantRegistry } from "./tenant_registry.js";

// The UTC hour of the tick that backs up: one a day, so one export per UTC date, each under that
// date. Away from the seed's digest hours (08:00 and 20:00 UTC), so a run writing to a tenant is
// rarely what its dump holds back, and an alert lands in East Asia's afternoon and the Americas'
// evening, read the same day.
export const BACKUP_HOUR_UTC = 4;

// How many hours after BACKUP_HOUR_UTC a tick checks that the day's backup reached its end: past
// the cron's 15-minute limit on the backup's own invocation, with a skipped hourly tick to spare,
// and still the same UTC date, so one tick a day checks and one alert at most is sent.
// https://developers.cloudflare.com/workers/runtime-apis/handlers/scheduled/
export const BACKUP_CHECK_HOURS = 2;

// The R2 binding of wrangler.hosted.toml the exports go to.
export const BACKUP_BINDING = "BACKUP";

// The B2 copy's settings: the endpoint and bucket are the deployment's (grade B), the key id and
// application key its credential (grade C), all set by `scripts/hosted.sh secrets`.
export const B2_SETTINGS = {
  endpoint: "CYRIS_HOSTED_B2_ENDPOINT",
  bucket: "CYRIS_HOSTED_B2_BUCKET",
  keyId: "CYRIS_HOSTED_B2_KEY_ID",
  applicationKey: "CYRIS_HOSTED_B2_APPLICATION_KEY",
};

// The registry's export name, beside the tenants' ids: outside TENANT, so no tenant can take it.
export const REGISTRY_EXPORT = "_registry";

export const EXPORT_EXTENSION = "jsonl.gz";
const CONTENT_TYPE = "application/gzip";

// How long one B2 upload may take before it is given up and alerted. An export is at most 32 MiB
// (sqlite_dump.js), which at the 5 MB/s a Worker's upload to B2 should keep at the least is under
// 7 s; the cron's 15 minutes are every tenant's, so one stalled upload must not take them all.
// https://developers.cloudflare.com/workers/runtime-apis/handlers/scheduled/
export const B2_PUT_TIMEOUT_MS = 60_000;

const DATE = /^\d{4}-\d{2}-\d{2}$/;

export const backupKey = (date, name) => `${date}/${name}.${EXPORT_EXTENSION}`;
export const backupDate = (now) => now.toISOString().slice(0, 10);
export const backupDue = (now) => now.getUTCHours() === BACKUP_HOUR_UTC;
export const backupCheckDue = (now) => now.getUTCHours() === BACKUP_HOUR_UTC + BACKUP_CHECK_HOURS;

const message = (error) => String(error?.message ?? error);

// The B2 copy's settings from `env`: `{settings}`, or `{unset}`, why there is no off-Cloudflare copy.
export function b2Settings(env) {
  const missing = Object.values(B2_SETTINGS).filter((name) => !env[name]);
  if (missing.length) return { unset: `not set: ${missing.join(", ")}` };
  const endpoint = env[B2_SETTINGS.endpoint];
  const region = b2Region(endpoint);
  if (!region) return { unset: `${B2_SETTINGS.endpoint} is not https://s3.<region>.backblazeb2.com: ${endpoint}` };
  return {
    settings: {
      endpoint,
      region,
      bucket: env[B2_SETTINGS.bucket],
      accessKeyId: env[B2_SETTINGS.keyId],
      secretAccessKey: env[B2_SETTINGS.applicationKey],
    },
  };
}

async function putB2(settings, key, body, sha256) {
  const request = await putObjectRequest({ ...settings, key, body, sha256, contentType: CONTENT_TYPE, now: new Date() });
  const response = await fetch(request, { signal: AbortSignal.timeout(B2_PUT_TIMEOUT_MS) });
  if (!response.ok) throw new Error(`HTTP ${response.status}: ${(await response.text()).slice(0, 300)}`);
  await response.body?.cancel();
}

// Every export of the day `now` falls on, in UTC: each registered tenant's, whatever its status,
// then the registry's, one at a time so one export is held at once. The registry's is last, so its
// key in R2 says the backup reached its end (checkBackup). Each is written to R2 and
// copied to B2 from the same bytes, so a failed R2 write still leaves the B2 copy and the other
// way round; any failure is alerted and the next export goes on. Never throws. Answers what it
// did, and logs it as one line.
export async function backupAll(env, { now = new Date() } = {}) {
  const started = Date.now();
  const date = backupDate(now);
  const bucket = env[BACKUP_BINDING];
  const b2 = b2Settings(env);
  const tell = (name, kind, detail) => alertOperator(env, name, workerAlert(kind, detail));
  const done = { date, exports: 0, r2: 0, b2: 0, bytes: 0, failed: [] };
  const fail = async (name, copy, kind, detail) => {
    done.failed.push({ name, copy });
    await tell(name, kind, detail);
  };
  // An alert about every export at once names them all in the tenant's place.
  if (!bucket) await tell(WORDING.backup_every_export, "backup_r2_unbound", `no ${BACKUP_BINDING} binding`);
  if (b2.unset) await tell(WORDING.backup_every_export, "backup_b2_unset", b2.unset);

  const registry = tenantRegistry(env);
  const exports = [];
  try {
    for (const row of await registry.tenants()) exports.push({ name: row.id, dump: () => tenantDb(env, row.id).exportDump() });
  } catch (error) {
    await fail(REGISTRY_EXPORT, "list", "backup_list_failed", message(error));
  }
  exports.push({ name: REGISTRY_EXPORT, dump: () => registry.exportDump() });

  for (const { name, dump } of exports) {
    const key = backupKey(date, name);
    let bytes;
    try {
      bytes = await dump();
    } catch (error) {
      await fail(name, "export", "backup_export_failed", `key=${key}\n${message(error)}`);
      continue;
    }
    done.exports += 1;
    done.bytes += bytes.byteLength;
    const sha256 = await sha256Hex(bytes);
    if (bucket) {
      try {
        await bucket.put(key, bytes, {
          sha256,
          httpMetadata: { contentType: CONTENT_TYPE },
          customMetadata: { format: `${DUMP_FORMAT}/${DUMP_VERSION}` },
        });
        done.r2 += 1;
      } catch (error) {
        await fail(name, "r2", "backup_r2_failed", `key=${key}\n${message(error)}`);
      }
    }
    if (b2.settings) {
      try {
        await putB2(b2.settings, key, bytes, sha256);
        done.b2 += 1;
      } catch (error) {
        await fail(name, "b2", "backup_b2_failed", `bucket=${b2.settings.bucket} key=${key}\n${message(error)}`);
      }
    }
  }
  console.log("hosted backup", { ...done, wall_ms: Date.now() - started });
  return done;
}

// Whether the backup of the day `now` falls on reached its end: its last export, the registry's,
// is in R2. If not, because the backup's tick was skipped, its invocation was cut off, or that
// export failed, the operator is told; so is a check that could not read R2. Without the R2
// binding nothing is checked, as the backup alerts that itself. Never throws. Answers
// `{key, present}`, or `{unbound}`.
export async function checkBackup(env, { now = new Date() } = {}) {
  const bucket = env[BACKUP_BINDING];
  if (!bucket) return { unbound: `no ${BACKUP_BINDING} binding` };
  const key = backupKey(backupDate(now), REGISTRY_EXPORT);
  const tell = (kind, detail) => alertOperator(env, WORDING.backup_every_export, workerAlert(kind, detail));
  let present;
  try {
    present = (await bucket.head(key)) !== null;
  } catch (error) {
    await tell("backup_check_failed", `key=${key}\n${message(error)}`);
    return { key, present: false };
  }
  if (!present) await tell("backup_missing", `key=${key}`);
  console.log("hosted backup check", { key, present });
  return { key, present };
}

// What the tick of `now` does for the backup: at BACKUP_HOUR_UTC the backup, BACKUP_CHECK_HOURS
// later its check, and nothing in any other hour.
export async function dailyBackup(env, { now = new Date() } = {}) {
  if (backupDue(now)) return backupAll(env, { now });
  if (backupCheckDue(now)) return checkBackup(env, { now });
  return null;
}

// Loads the R2 export of tenant `from` on `date` into tenant `into`, a new id whose object holds no
// table and which the registry does not hold, so no run, poll or reader reaches it while it fills;
// registering it is the operator's next step. Answers `{tenant, from, date, key, restored}`, or
// `{refused, status}`, having written nothing. The registry's export is not restored here: the
// registry makes its own tables as it starts (docs/hosted-tenants.md says how to read one).
export async function restoreExport(env, { date, from, into }) {
  const refusedId = refuseTenantId(into);
  if (refusedId) return { refused: refusedId, status: 400 };
  if (typeof date !== "string" || !DATE.test(date)) return { refused: `date is YYYY-MM-DD, not ${date}`, status: 400 };
  if (typeof from !== "string" || !TENANT.test(from)) {
    return { refused: `from is the tenant id whose export is restored, matching ${TENANT}: ${from}`, status: 400 };
  }
  if (await tenantRegistry(env).tenant(into)) {
    return { refused: `${into} is already registered; restore into a new tenant id`, status: 409 };
  }
  const bucket = env[BACKUP_BINDING];
  if (!bucket) return { refused: `the Worker has no ${BACKUP_BINDING} binding`, status: 503 };
  const key = backupKey(date, from);
  const object = await bucket.get(key);
  if (!object) return { refused: `no export at ${key}`, status: 404 };
  const answer = await tenantDb(env, into).restoreDump(new Uint8Array(await object.arrayBuffer()));
  if (answer.refused) return { refused: `${into} ${answer.refused}; restore into a new tenant id`, status: 409 };
  return { tenant: into, from, date, key, restored: answer.restored };
}
