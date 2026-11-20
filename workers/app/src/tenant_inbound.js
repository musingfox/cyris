// A hosted tenant's newsletter queue: its Durable Object stores each message the hosted Worker's
// email() handler took at the tenant's address (hosted_inbound.js) in its own `newsletters`
// table (hosted_schema.sql), and the tenant's run drains it through its own gateway. Two tenants
// sent one issue each get a row of their own, in objects of their own: nothing is keyed across
// tenants. docs/decisions/0034-a-hosted-tenants-newsletters-arrive-at-a-random-address.md.
// No workerd-only imports: vitest runs this under Node.

import HOSTED_SCHEMA_SQL from "../../../src/cyris/adapters/store/hosted_schema.sql";

// The most messages a tenant's queue holds. Its run drains the queue at every digest, twice a
// day by default, and a reader with 50 newsletters a day is far under it. When it is full, the
// oldest message a run held (`held_at`: seen, matched by no source, left queued for one to be
// added) is evicted for the new one, so held mail, a flood to a leaked address included, never
// crowds out new mail past one run. With nothing held, the new message is dropped: what fills the
// queue then arrived since the last run, which the next run sorts.
export const MAX_QUEUED = 200;

// The first arrival of an id is kept: a re-delivered copy, or a message whose forged From, Subject
// and Date give it a queued issue's id, changes nothing, so no sender can overwrite an issue
// already queued.
const INSERT = `INSERT OR IGNORE INTO newsletters
  (id, sender, subject, html, text, date, headers, raw_size, received_at)
  VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)`;

// Stores one message's record, `id` as record.js keys it, and answers `{stored: true}` (with
// `evicted`, the held message it replaced, when the queue was full), `{duplicate: true}` when that
// id is already queued, or `{refused}` when the queue is full of messages no run has held yet. The
// hosted schema is applied first, so a tenant whose object has not had it yet gets the table.
// Eviction and insert run in one synchronous turn of the object, which commits them together.
export function storeNewsletter(storage, id, record, { now = new Date() } = {}) {
  const sql = storage.sql;
  sql.exec(HOSTED_SCHEMA_SQL);
  if (sql.exec("SELECT 1 AS held FROM newsletters WHERE id = ?", id).toArray().length) return { duplicate: true };
  const queued = sql.exec("SELECT count(*) AS n FROM newsletters").one().n;
  let evicted;
  if (queued >= MAX_QUEUED) {
    const [oldest] = sql
      .exec("SELECT id FROM newsletters WHERE held_at IS NOT NULL ORDER BY held_at, received_at, id LIMIT 1")
      .toArray();
    if (!oldest) return { refused: "queue full" };
    sql.exec("DELETE FROM newsletters WHERE id = ?", oldest.id);
    evicted = oldest.id;
  }
  sql.exec(
    INSERT,
    id,
    record.from,
    record.subject,
    record.html,
    record.text,
    record.date,
    JSON.stringify(record.headers),
    record.raw_size,
    now.toISOString(),
  );
  return evicted ? { stored: true, evicted } : { stored: true };
}
