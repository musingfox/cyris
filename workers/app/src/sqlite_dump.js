// A full logical dump of one SQLite Durable Object's own tables, and its restore into an empty
// one: what the hosted Worker's daily backup exports for each tenant database and the registry
// (hosted_backup.js). Why this format and these bounds:
// docs/decisions/0041-the-hosted-databases-are-exported-daily-to-r2-and-b2.md.
//
// The dump is gzip-compressed JSON lines. The first line is the header, `{"format", "version"}`.
// Each table follows in name order: one `{"table", "sql", "columns"}` line, then one line per
// row, a JSON array of its values in `columns`' order, in rowid order. Then each index, trigger
// and view, `{"schema": type, "name", "table", "sql"}`, in type and name order, and last a
// trailer, `{"end": {"tables", "rows"}}`, without which a dump is taken as cut short. A value is
// JSON as it is, except a BLOB, `{"blob": base64}`, and a REAL with no fraction, `{"real": n}`,
// which JSON would otherwise read back as an INTEGER. Nothing in it is a clock or a random draw,
// so the same data dumps to the same bytes.

import { isTenantTable, quotedName } from "./tenant_db.js";

export const DUMP_FORMAT = "cyris-sqlite-dump";
export const DUMP_VERSION = 1;

// Rows read per statement while dumping: each page is read in one synchronous exec, then
// compressed while the next waits, so only one page is held as text. The largest rows a tenant
// holds are its site's pages, about 150 KB each (ADR-0029), so a page of them is about 7.5 MB.
export const PAGE_ROWS = 50;

// A Durable Object method's argument and answer are each at most 32 MiB serialized; the dump
// crosses that boundary whole, out of the object and back into one, so it is refused above it
// with this margin left for the structured-clone envelope.
// https://developers.cloudflare.com/workers/runtime-apis/rpc/
export const MAX_DUMP_BYTES = 32 * 1024 * 1024 - 64 * 1024;

const SCHEMA_ORDER = ["index", "trigger", "view"];

const encoder = new TextEncoder();

function base64(bytes) {
  let binary = "";
  for (let i = 0; i < bytes.length; i += 0x8000) binary += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  return btoa(binary);
}

function unbase64(text) {
  const binary = atob(text);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return bytes;
}

const isBytes = (value) => value instanceof ArrayBuffer || ArrayBuffer.isView(value);
const asBytes = (value) =>
  value instanceof ArrayBuffer ? new Uint8Array(value) : new Uint8Array(value.buffer, value.byteOffset, value.byteLength);

// One value as the dump holds it; `type` is SQLite's own typeof() of it.
function encodeValue(value, type) {
  if (isBytes(value)) return { blob: base64(asBytes(value)) };
  if (type === "real" && (Number.isInteger(value) || !Number.isFinite(value))) {
    return { real: Number.isFinite(value) ? value : String(value) };
  }
  return value;
}

// One dumped value as a bound parameter, and the placeholder that binds it as SQLite stored it.
// A whole number is cast to INTEGER and a tagged one to REAL, so a column with no type gets back
// the type it held whichever way the runtime binds a JavaScript number.
function decodeValue(value) {
  if (value !== null && typeof value === "object") {
    if (typeof value.blob === "string") return [unbase64(value.blob), "?"];
    if ("real" in value) return [Number(value.real), "CAST(? AS REAL)"];
    throw new Error(`a dumped value is null, a number, a string, a blob or a real: ${JSON.stringify(value)}`);
  }
  return [value, Number.isInteger(value) ? "CAST(? AS INTEGER)" : "?"];
}

const schemaRows = (sql) =>
  sql
    .exec("SELECT type, name, tbl_name, sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type, name")
    .toArray()
    .filter((row) => isTenantTable(row.name) && isTenantTable(row.tbl_name));

// The tables a dump carries, in name order. A virtual table's own shadow tables (`<name>_...`,
// as FTS5 makes them) are left out: creating the virtual table makes them again, and its rows
// are dumped through it.
function dumpedTables(rows) {
  const tables = rows.filter((row) => row.type === "table");
  const virtual = tables.filter((row) => /^\s*CREATE\s+VIRTUAL\s+TABLE/i.test(row.sql)).map((row) => row.name);
  return tables
    .filter((row) => !virtual.some((name) => row.name !== name && row.name.startsWith(`${name}_`)))
    .sort((a, b) => (a.name < b.name ? -1 : a.name > b.name ? 1 : 0));
}

// How a table's rows are paged: by its rowid under a name no column takes, or, for a WITHOUT
// ROWID table (or one whose columns take all three rowid names), by its primary key's order.
function pager(sql, table, columns) {
  const taken = new Set(columns.map((column) => column.name.toLowerCase()));
  const withoutRowid = /\)\s*(?:STRICT\s*,\s*)?WITHOUT\s+ROWID\b[^)]*$/i.test(table.sql);
  const rowid = withoutRowid ? undefined : ["rowid", "_rowid_", "oid"].find((name) => !taken.has(name));
  if (rowid) return { key: rowid };
  const pk = columns.filter((column) => column.pk > 0).sort((a, b) => a.pk - b.pk);
  return { order: (pk.length ? pk : columns).map((column) => quotedName(column.name)).join(", ") };
}

// Each page of `table`'s rows as JSON lines, read one synchronous statement at a time.
function* tablePages(sql, table, columns) {
  const names = columns.map((column) => quotedName(column.name));
  const types = names.map((name) => `typeof(${name})`);
  const values = [...names, ...types].join(", ");
  const from = quotedName(table.name);
  const { key, order } = pager(sql, table, columns);
  let after = null;
  for (let offset = 0; ; offset += PAGE_ROWS) {
    const cursor = key
      ? after === null
        ? sql.exec(`SELECT ${key}, ${values} FROM ${from} ORDER BY ${key} LIMIT ?`, PAGE_ROWS)
        : sql.exec(`SELECT ${key}, ${values} FROM ${from} WHERE ${key} > ? ORDER BY ${key} LIMIT ?`, after, PAGE_ROWS)
      : sql.exec(`SELECT 0, ${values} FROM ${from} ORDER BY ${order} LIMIT ? OFFSET ?`, PAGE_ROWS, offset);
    const rows = [...cursor.raw()];
    if (rows.length === 0) return;
    const n = names.length;
    yield rows.map((row) => JSON.stringify(row.slice(1, 1 + n).map((value, i) => encodeValue(value, row[1 + n + i]))));
    if (rows.length < PAGE_ROWS) return;
    after = rows[rows.length - 1][0];
  }
}

// The gzip of `lines`, one string per line, written as each comes so only one page is held
// uncompressed; refused once it passes `maxBytes`.
async function gzipLines(lines, maxBytes) {
  const stream = new CompressionStream("gzip");
  const writer = stream.writable.getWriter();
  const chunks = [];
  let size = 0;
  const read = (async () => {
    for await (const chunk of stream.readable) {
      chunks.push(chunk);
      size += chunk.byteLength;
    }
  })();
  try {
    for (const page of lines) {
      await writer.write(encoder.encode(`${page.join("\n")}\n`));
      if (size > maxBytes) throw new Error(`the dump passed ${maxBytes} bytes compressed`);
    }
    await writer.close();
  } catch (error) {
    await writer.abort(error).catch(() => {});
    await read.catch(() => {});
    throw error;
  }
  await read;
  if (size > maxBytes) throw new Error(`the dump is ${size} bytes compressed, over ${maxBytes}`);
  const out = new Uint8Array(size);
  let at = 0;
  for (const chunk of chunks) {
    out.set(chunk, at);
    at += chunk.byteLength;
  }
  return out;
}

// Every line of `sql`'s dump, a page of lines at a time.
function* dumpLines(sql) {
  const schema = schemaRows(sql);
  const tables = dumpedTables(schema);
  let rows = 0;
  yield [JSON.stringify({ format: DUMP_FORMAT, version: DUMP_VERSION })];
  for (const table of tables) {
    // table_info lists the columns an INSERT can name: a generated column is made again from them.
    const columns = sql.exec(`PRAGMA table_info(${quotedName(table.name)})`).toArray();
    yield [JSON.stringify({ table: table.name, sql: table.sql, columns: columns.map((column) => column.name) })];
    for (const page of tablePages(sql, table, columns)) {
      rows += page.length;
      yield page;
    }
  }
  const rest = schema
    .filter((row) => SCHEMA_ORDER.includes(row.type))
    .sort((a, b) => SCHEMA_ORDER.indexOf(a.type) - SCHEMA_ORDER.indexOf(b.type) || (a.name < b.name ? -1 : 1));
  for (const row of rest) yield [JSON.stringify({ schema: row.type, name: row.name, table: row.tbl_name, sql: row.sql })];
  yield [JSON.stringify({ end: { tables: tables.length, rows } })];
}

// The dump of the object whose state is `ctx`, as gzip bytes. No other event reaches the object
// until it is made, so it is one moment's data even while a run writes to the same object; the
// runtime resets an object that blocks for 30 s, which a dump of the size ADR-0041 designs for
// stays far under. It also resets one whose callback throws, so a dump refused as over size, or
// a failed read, is caught inside and thrown only once the object is unblocked.
// `maxBytes` is the size it is refused above, MAX_DUMP_BYTES unless a test passes a smaller one.
// https://developers.cloudflare.com/durable-objects/api/state/#blockconcurrencywhile
export async function dumpObject(ctx, { maxBytes = MAX_DUMP_BYTES } = {}) {
  const made = await ctx.blockConcurrencyWhile(async () => {
    try {
      return { bytes: await gzipLines(dumpLines(ctx.storage.sql), maxBytes) };
    } catch (error) {
      return { error };
    }
  });
  if (made.error) throw made.error;
  return made.bytes;
}

// Each line of a gzip dump, as it is decompressed.
async function* gunzipLines(bytes) {
  const stream = new Blob([bytes]).stream().pipeThrough(new DecompressionStream("gzip")).pipeThrough(new TextDecoderStream());
  let rest = "";
  for await (const text of stream) {
    const lines = (rest + text).split("\n");
    rest = lines.pop();
    yield* lines;
  }
  if (rest) yield rest;
}

// Loads `bytes`, a dump, into the object whose state is `ctx`, which must hold no table of its
// own: `{restored: {table: rows}}`, or `{refused}`, writing nothing. Every table is made first,
// its rows inserted, and only then its indexes, triggers and views, so no trigger fires on a
// restored row. It runs as one transaction, so a dump that fails part way, or is cut short,
// leaves the object as empty as it was; foreign keys are checked at its end, since a row may name
// one inserted after it.
export async function restoreObject(ctx, bytes) {
  const sql = ctx.storage.sql;
  const held = schemaRows(sql).filter((row) => row.type === "table");
  if (held.length) return { refused: `it already holds ${held.length} table(s), ${held.map((row) => row.name).join(", ")}` };
  const restored = {};
  await ctx.storage.transaction(async () => {
    sql.exec("PRAGMA defer_foreign_keys = ON");
    const later = [];
    let table = null;
    let header = null;
    let end = null;
    let rows = 0;
    for await (const line of gunzipLines(bytes)) {
      if (!line) continue;
      if (end) throw new Error("the dump goes on after its end line");
      const record = JSON.parse(line);
      if (!header) {
        if (record?.format !== DUMP_FORMAT || record?.version !== DUMP_VERSION) {
          throw new Error(`not a ${DUMP_FORMAT} version ${DUMP_VERSION}: ${line.slice(0, 200)}`);
        }
        header = record;
      } else if (Array.isArray(record)) {
        if (!table) throw new Error("a row comes before any table");
        if (record.length !== table.columns.length) throw new Error(`a row of ${table.name} has ${record.length} values`);
        const bound = record.map(decodeValue);
        const marks = bound.map(([, mark]) => mark).join(", ");
        sql.exec(`INSERT INTO ${quotedName(table.name)} (${table.names}) VALUES (${marks})`, ...bound.map(([value]) => value));
        restored[table.name] += 1;
        rows += 1;
      } else if (typeof record.table === "string" && typeof record.sql === "string" && Array.isArray(record.columns)) {
        if (!isTenantTable(record.table)) throw new Error(`the dump names the object's own table ${record.table}`);
        sql.exec(record.sql);
        table = { name: record.table, columns: record.columns, names: record.columns.map(quotedName).join(", ") };
        restored[record.table] = 0;
      } else if (SCHEMA_ORDER.includes(record.schema) && typeof record.sql === "string") {
        later.push(record.sql);
      } else if (record.end) {
        end = record.end;
      } else {
        throw new Error(`a dump line is a table, a row, a schema entry or the end: ${line.slice(0, 200)}`);
      }
    }
    if (!end) throw new Error("the dump is cut short: it has no end line");
    if (end.tables !== Object.keys(restored).length || end.rows !== rows) {
      throw new Error(`the dump's end line counts ${end.tables} tables and ${end.rows} rows, not ${Object.keys(restored).length} and ${rows}`);
    }
    for (const statement of later) sql.exec(statement);
  });
  return { restored };
}
