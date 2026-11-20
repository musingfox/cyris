// A hosted tenant's newsletter queue: its Durable Object stores each message the hosted Worker's
// email() handler took at the tenant's address (hosted_inbound.js) in its own `newsletters`
// table (hosted_schema.sql), and the tenant's run drains it through its own gateway. Two tenants
// sent one issue each get a row of their own, in objects of their own: nothing is keyed across
// tenants. docs/decisions/0034-a-hosted-tenants-newsletters-arrive-at-a-random-address.md.
// No workerd-only imports: vitest runs this under Node.

import HOSTED_SCHEMA_SQL from "../../../src/cyris/adapters/store/hosted_schema.sql";

// The most messages a tenant's queue holds. Its run drains the queue at every digest, twice a
// day by default, and a reader with 50 newsletters a day is far under it; past it a flood to a
// leaked address is refused, and the tenant's object stops growing, until a run or a new address.
export const MAX_QUEUED = 200;

// A re-delivered copy has the same id and replaces its row, so the cap counts it once.
const INSERT = `INSERT OR REPLACE INTO newsletters
  (id, sender, subject, html, text, date, headers, raw_size, received_at)
  VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)`;

// Stores one message's record, `id` as record.js keys it, and answers `{stored: true}`, or
// `{refused}` when the queue is full. The hosted schema is applied first, so a tenant whose
// object has not had it yet gets the table.
export function storeNewsletter(storage, id, record, { now = new Date() } = {}) {
  const sql = storage.sql;
  sql.exec(HOSTED_SCHEMA_SQL);
  const queued = sql.exec("SELECT count(*) AS n FROM newsletters WHERE id <> ?", id).one().n;
  if (queued >= MAX_QUEUED) return { refused: "queue full" };
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
  return { stored: true };
}
