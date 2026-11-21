---
status: accepted
date: 2026-11-22
decision-makers: ""
---

# The hosted databases are exported daily to R2, and copied to B2 outside Cloudflare

## Context and Problem Statement

Each hosted tenant's data lives in its own SQLite Durable Object, `TenantDb`
([ADR-0028](0028-one-sqlite-durable-object-per-hosted-tenant.md)). The operator's registry is
one more such object, `TenantRegistry`
([ADR-0030](0030-the-operator-registry-is-one-sqlite-durable-object.md)). ADR-0030 chose that
technology partly so that the registry "shares their backup path". No backup path existed yet.

A SQLite Durable Object has 30 days of point-in-time recovery. It has no export API.
`wrangler d1 export` and D1 Time Travel are D1 tools, and they do not reach a Durable Object.
Point-in-time recovery also lives inside the same Cloudflare account. For a deployment run by
one person, the largest risk is that the account itself is suspended or lost. Then every
tenant's data is gone at once, recovery included.

The owner decided on two copies. The first is in R2. The second is in Backblaze B2, reached
through its S3-compatible API. This record fixes the export format, the schedule, how the B2
copy is written, the restore, and what was rejected.

## Decision Drivers

* A copy must exist outside Cloudflare, and it must be readable without Cloudflare.
* The restore must be exact: every table, every row, and each value's SQLite type.
* Durable Object limits: a statement is at most 100 KB, a row or value at most 8 MB, an RPC
  argument or answer at most 32 MiB, and a `blockConcurrencyWhile` callback at most 30 s.
* The backup must not delay the hourly tick's polls or its run queue (ADR-0031, ADR-0032).
* One tenant's failure must not stop the other tenants' exports. Every failure must reach the
  operator, naming the tenant and the copy that failed.
* No new npm dependency without a reason. The Worker's only runtime dependency today is `jose`.

## Considered Options

* Format: gzip JSON lines with bound values on restore; SQL text (`INSERT` statements with
  literals); one JSON document per database.
* Schedule: one fixed UTC hour of the existing hourly tick, run after the tick; a second cron;
  the backup running beside the tick's polls.
* B2: SigV4 written on Web Crypto; `aws4fetch` or `@aws-sdk/client-s3`; B2's native API.
* Restore: an operator route into a new, empty tenant id; restore in place over a live tenant.

## Decision Outcome

Chosen: gzip JSON lines, at `BACKUP_HOUR_UTC` of the existing tick and after it, to R2 and then
to B2 by a SigV4 `PutObject` written on Web Crypto. A restore is an operator route into a new,
unregistered, empty tenant id.

### The format

`workers/app/src/sqlite_dump.js` writes the dump and reads it back. `TenantDb.exportDump`,
`TenantDb.restoreDump` and `TenantRegistry.exportDump` call it.

* The dump is gzip-compressed UTF-8, one JSON value per line. Line 1 is the header
  `{"format": "cyris-sqlite-dump", "version": 1}`.
* Each table from `sqlite_master` follows, in name order. Internal tables (`_cf_*`,
  `sqlite_*`) are skipped. A table is one `{"table", "sql", "columns"}` line: its name, its
  `CREATE` statement as `sqlite_master` holds it, and the columns from `PRAGMA table_info`, so
  generated columns are left out. Its rows follow, one JSON array per row, in rowid order. A
  `WITHOUT ROWID` table is ordered by its primary key.
* Then each index, trigger and view, as `{"schema": type, "name", "table", "sql"}`, in that type
  order and by name.
* The last line is `{"end": {"tables", "rows"}}`. A dump without it, or whose counts differ, is
  refused as cut short.
* A value is plain JSON, with two exceptions. A BLOB is `{"blob": base64}`. A REAL with no
  fractional part is `{"real": n}`, because JSON alone would read it back as an INTEGER. On
  restore, a whole number is bound as `CAST(? AS INTEGER)` and a tagged REAL as
  `CAST(? AS REAL)`. A column with no declared type therefore gets back the type it held,
  whichever way the runtime binds a JavaScript number. An integer past 2^53 loses precision,
  as every number the Durable Object's SQL API returns does.
* A virtual table's shadow tables (`<name>_*`) are left out. Creating the virtual table makes
  them again, and its rows are dumped through the virtual table.
* Nothing in the dump is a clock or a random value. The same data dumps to the same bytes,
  including the dump of a restored copy (`workers/app/test/sqlite_dump.test.js`).
* Table and column names are always `quotedName` (`tenant_db.js`). A tenant's container can
  create a table of any name, and a name is never more than that name.
* `sqlite_sequence` is not carried. Restoring rows with their ids moves each `AUTOINCREMENT`
  counter to the highest id restored. A counter that was past it, because the newest rows had
  been deleted, starts lower after a restore. No table relies on an id never being reused.
* Rowids of a table without an `INTEGER PRIMARY KEY` are not carried. Rows are inserted in
  rowid order, so their relative order is kept. That order is what `digests ... ORDER BY
  saved_at DESC, rowid DESC` relies on.

### Sizes designed for

* Reading: rows are read 50 to a statement (`PAGE_ROWS`). Each page is compressed before the
  next is read, so one page is held as text at a time. The largest rows are a tenant's pages,
  about 150 KB each (ADR-0029), so a page is about 7.5 MB.
* The whole dump crosses one RPC, out of the object and into the restoring one. It is refused
  above `MAX_DUMP_BYTES`, 32 MiB less 64 KiB. Gzip shrinks this HTML and JSON several times
  over, so the design covers a tenant database of roughly 100 MB. A year of two issues a day
  at 150 KB is about 110 MB of pages. A tenant past that limit gets an alert naming it, and
  then needs a streamed export (a `ReadableStream` over RPC). That is a later change.
* The dump runs inside `blockConcurrencyWhile`, so it is one moment's data even while the
  tenant's run writes. The runtime resets an object that blocks for more than 30 s. Compressing
  100 MB takes a few seconds of CPU.
* The runtime also terminates and resets an object whose `blockConcurrencyWhile` callback
  throws. A dump refused as over size, or a failed read, would then reset a tenant, or the
  registry, at the backup hour every day, possibly mid-run. So the callback never throws: it
  answers the error, and `dumpObject` throws it once the object is unblocked. Dropping
  `blockConcurrencyWhile` for one synchronous read was rejected: compression is asynchronous,
  so that read would hold the whole database as text, about 100 MB, in a 128 MB isolate.
* The restore streams the decompression line by line. It does not hold the whole uncompressed
  dump. It runs as one `ctx.storage.transaction()` with `PRAGMA defer_foreign_keys = ON`, so a
  row may name a row inserted after it (`areas.merged_into`). A restore that fails, or a dump
  that is cut short, leaves the object as empty as it was. Indexes, triggers and views are made
  after the rows, so no trigger fires on a restored row.

### The schedule

`BACKUP_HOUR_UTC` is 4, a grade-A constant in `workers/app/src/hosted_backup.js`
(`docs/architecture.md` §5). The existing hourly `scheduled()` runs the tick, and then, in that
hour only, `backupAll`. The default export wraps the tick's own `scheduled()`, left where it
was, rather than moving its body. The backup runs in a `finally`, so a tick that threw still
backs up, and a backup that throws is only logged, so the tick's failure is the one the
invocation reports. Because it
runs after the tick, the polls have run and the runs are queued in the registry before any
export starts. The registry's alarm starts those runs on its own, so they do not wait for the
backup either. The exports run one at a time, the registry first and then every registered
tenant, paused ones included, so one export is held in memory at a time. Each export is
written to R2 and copied to B2 from the same bytes. One failed copy leaves the other, and a
failure moves on to the next export. The cron's 15-minute limit
(https://developers.cloudflare.com/workers/runtime-apis/handlers/scheduled/) bounds the whole
backup. Each B2 upload gives up after `B2_PUT_TIMEOUT_MS`, 60 s, so one stalled upload cannot
take that time from every tenant. A day's export is keyed by that UTC date. `POST /hosted/backup`
runs the same backup on demand, for the restore drill.

### Where the copies go

* R2: the binding `BACKUP` (`wrangler.hosted.toml`, bucket `cyris-hosted-backup`), key
  `<YYYY-MM-DD>/<tenant>.jsonl.gz`. The registry's key is `<YYYY-MM-DD>/_registry.jsonl.gz`;
  `_registry` is outside `TENANT`, so no tenant can take it. Each object is put with its
  SHA-256, which R2 checks on receipt, and with content type `application/gzip`.
* B2: the same keys, in the bucket `CYRIS_HOSTED_B2_BUCKET` at `CYRIS_HOSTED_B2_ENDPOINT`
  (`https://s3.<region>.backblazeb2.com`, path-style), signed with `CYRIS_HOSTED_B2_KEY_ID` and
  `CYRIS_HOSTED_B2_APPLICATION_KEY`. B2's S3-compatible API takes only SigV4. An application
  key and its id are the S3 secret and access key id, and the master key is refused, so the key
  is one made for this bucket.
* `workers/app/src/s3_sigv4.js` signs the request with `crypto.subtle`. Its payload hash is the
  body's own SHA-256, not `UNSIGNED-PAYLOAD`. The body is in memory anyway, and a body changed on
  the way is refused. It is tested against Amazon's two published S3 worked examples and a
  B2-shaped `PutObject` signed by botocore.
* With any of the four B2 settings unset, or an endpoint that is not B2's, R2 is still written
  and the operator gets one alert that day: there is no off-Cloudflare copy, with the reason.
* Retention is not in code. It is an R2 lifecycle rule and a B2 lifecycle setting, set when the
  buckets are created (`docs/hosted-tenants.md`).

### Alerts

Every failure goes through `alertOperator` (`hosted_alerts.js`,
[ADR-0035](0035-a-hosted-runs-alerts-go-to-the-operator-through-the-hosted-worker.md)). The
tenant (or `_registry`) is in front, and the headline is from `hosted_alerts.json`: an export
that failed, an R2 write, a B2 copy, no B2 settings, no R2 binding, or a tenant list that could
not be read. The body names the key and the error. An alert about every export at once names
`every export` in the tenant's place. Each failure is one alert. Twenty tenants whose B2 copy
fails on a revoked key are twenty alerts, the way twenty failed runs are.

### The restore

`POST /hosted/tenants/<new>/restore?date=D&from=T`, run by `scripts/hosted.sh restore D T NEW`,
is an operator route behind Access and the operator list
([ADR-0037](0037-the-hosted-operator-routes-admit-only-named-identities.md)). It loads T's R2
export of day D into the new id. The route refuses, writing nothing:

* an id that `register` would refuse (`refuseTenantId`);
* an id the registry already holds, of any status;
* an id whose object already holds a table.

It comes before the router's registry check, the one route that does. Registering the id is a
separate step, `scripts/hosted.sh register NEW paused`, after which the operator's report and
`cyris --tenant NEW` read it. The registry is not restored by a route, because it makes its own
tables as it starts. `scripts/backup_to_sqlite.py` reads any export, the registry's included,
into an ordinary SQLite file with Python's standard library, on any machine. That is the manual
path for the registry, and the way to reach a tenant's data if the account is gone.

### Consequences

* Good, because a copy of every tenant and of the registry is outside Cloudflare every day, and
  it reads back without Cloudflare.
* Good, because the dump is exact: types, BLOBs, odd names, foreign keys, indexes, triggers and
  views come back as they were, and the same data is the same bytes.
* Good, because the tick's polls and runs never wait on it, and one tenant's failure is one
  alert and nothing more.
* Good, because no dependency was added: Web Crypto signs the B2 request.
* Bad, because a tenant past about 100 MB, or 32 MiB compressed, cannot be exported until the
  dump streams over RPC. Its alert says so every day.
* Bad, because the exports are not encrypted beyond what R2 and B2 do at rest. The registry's
  export holds the vote key and the hashes of live sessions and sign-in links. Encrypting the
  exports with a key held outside both providers is a later decision.
* Bad, because `PRAGMA defer_foreign_keys` and async `transaction()` were verified on Node's
  SQLite, not on workerd. The coordinator's live drill is their first run on Cloudflare.
* Neutral, because each day's backup is a full copy, not an increment. At a few MB per tenant a
  day, the lifecycle rules keep the cost within R2's and B2's free tiers for a beta
  (`docs/hosting-and-cost.md`).

### Confirmation

* `workers/app/test/sqlite_dump.test.js`: a seeded tenant's export, restored into an empty
  object, equals the source table by table with each value's type. Covered: both schemas, more
  than two pages, a rowid gap, a BLOB, whole-number REALs, a quoted table name, `WITHOUT ROWID`,
  FTS5, a forward foreign key, an index, a trigger and a view. The test also checks the same
  bytes twice and from the restored copy, the line format, `blockConcurrencyWhile`, that a dump
  over `MAX_DUMP_BYTES` and a failed read are refused without resetting the object, that the
  registry's export round-trips, a refused non-empty object, and that a cut-short or foreign
  dump writes nothing. The golden dump pins the format against `tests/test_backup_to_sqlite.py`.
* `workers/app/test/hosted_backup.test.js`: the tick at its hour writes every tenant, paused
  included, and the registry to R2 and B2 under the same keys. No other hour backs up. The
  backup starts after every poll and queued run, and runs when the tick throws. Each failed copy
  is alerted with its tenant and copy while the rest go on. Missing or wrong B2 settings still
  write R2 and alert once. A stalled B2 upload is given up. The restore's refusals and its
  success are tested.
* `workers/app/test/s3_sigv4.test.js`: the signer reproduces Amazon's two published S3 examples
  and botocore's B2 `PutObject`.
* `workers/app/test/hosted.test.js`: the restore and backup routes take only an operator's POST
  from this site.
* `tests/test_hosted_config.py` holds the binding and its bucket to the hosted file only, the
  single cron, and the B2 names to `hosted.sh` and out of a container's environment.
  `tests/test_hosted_script.py` holds the B2 prompts on stdin and the `restore`, `backup` and
  `register TENANT STATUS` subcommands.
* The live drill on the trial account (`docs/hosted-tenants.md` §11) is the coordinator's,
  after deploy.

## Pros and Cons of the Options

### Gzip JSON lines, values bound on restore

* Good, because a row of any size is inserted through bound values, under the 100 KB
  statement limit.
* Good, because it streams both ways, a line at a time, and any language reads it.
* Bad, because it is this project's own format, so a reader had to be written
  (`scripts/backup_to_sqlite.py`).

### SQL text with literals

* Good, because `sqlite3 new.db < dump.sql` would read it.
* Bad, because a tenant's page row (about 150 KB) is over the 100 KB statement limit as a
  literal, so the restore could not run it in a Durable Object.
* Bad, because `quote()` of a REAL is not guaranteed to round-trip in every SQLite version.

### One JSON document per database

* Bad, because the whole database would be one string in a 128 MB isolate, on the way out and
  on the way back.

### A second cron for the backup

* Rejected, because the ticket keeps one cron, and a second `scheduled()` would race the
  hourly tick for the same registry.

### The backup beside the tick's polls

* Bad, because it would share the invocation's connections with every tenant's poll and delay
  them, which the drivers rule out.

### `aws4fetch` or `@aws-sdk/client-s3`

* Good, because someone else maintains them.
* Bad, because the signer is about 80 lines over Web Crypto, and the SDK is much larger than the
  one `PutObject` it would make.

### B2's native API

* Bad, because it takes a separate authorization call per session and a per-upload URL, and
  nothing else in this project speaks it. The S3-compatible API is one signed PUT.

### Restore in place over a live tenant

* Bad, because a mistyped date or tenant would overwrite live data, and a half-done restore
  would leave a tenant its run reads. A new id, registered paused, is checked before anyone
  is invited to it or it is resumed.

## More Information

* `docs/architecture.md` §4 has the row for the backup locations. §5 grades `BACKUP_HOUR_UTC`,
  `PAGE_ROWS`, `MAX_DUMP_BYTES`, `B2_PUT_TIMEOUT_MS`, the key layout, the four B2 settings, the
  R2 binding and the alert wording.
* `docs/hosted-tenants.md` §11 is the runbook: create the buckets and their lifecycle rules,
  set the B2 settings, run a backup, and drill a restore.
* Encryption at rest beyond R2's and B2's own is a later decision. So is a streamed export for
  tenants past the RPC limit.
* Docs read: R2 Workers API, `put()` and its `sha256` option
  (https://developers.cloudflare.com/r2/api/workers/workers-api-reference/); R2 bindings
  (https://developers.cloudflare.com/r2/api/workers/workers-api-usage/); R2 object lifecycles
  (https://developers.cloudflare.com/r2/buckets/object-lifecycles/) and
  `wrangler r2 bucket lifecycle add` (https://developers.cloudflare.com/workers/wrangler/commands/r2/);
  Durable Object limits (https://developers.cloudflare.com/durable-objects/platform/limits/);
  the SQL storage API, `raw()`, `transaction()` and `transactionSync()`
  (https://developers.cloudflare.com/durable-objects/api/sqlite-storage-api/);
  `blockConcurrencyWhile` (https://developers.cloudflare.com/durable-objects/api/state/); RPC
  limits (https://developers.cloudflare.com/workers/runtime-apis/rpc/); the scheduled handler
  (https://developers.cloudflare.com/workers/runtime-apis/handlers/scheduled/); B2's
  S3-compatible API (https://www.backblaze.com/docs/cloud-storage-s3-compatible-api and
  https://www.backblaze.com/docs/cloud-storage-call-the-s3-compatible-api); B2 S3 application
  keys (https://www.backblaze.com/docs/cloud-storage-s3-compatible-app-keys); and B2 lifecycle
  rules (https://www.backblaze.com/docs/cloud-storage-lifecycle-rules).
* Not read, because the sandbox refused the host: Amazon's SigV4 page
  (https://docs.aws.amazon.com/AmazonS3/latest/API/sig-v4-header-based-auth.html) and Backblaze's
  S3 API reference on `backblaze-prod.us.document360.io`. The signer instead reproduces that
  page's two published signatures byte for byte, and a third from botocore. Whether B2's
  S3-compatible API accepts `UNSIGNED-PAYLOAD` is not established, and nothing relies on it.
