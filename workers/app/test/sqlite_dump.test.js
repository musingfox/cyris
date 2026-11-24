// The dump the daily backup exports of each hosted database (sqlite_dump.js), over real SQLite: a
// seeded tenant's every table, exported and loaded into an empty object, comes back row for row.
import { randomBytes } from "node:crypto";
import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import { DUMP_FORMAT, DUMP_VERSION, MAX_DUMP_BYTES, PAGE_ROWS } from "../src/sqlite_dump.js";
import { isTenantTable, quotedName } from "../src/tenant_db.js";
import { HOSTED_SCHEMA_SQL, SCHEMA_SQL, registryObject, tenantDb } from "./sql_storage.js";

const STAMP = "2026-11-22T04:00:00+00:00";

const gunzip = async (bytes) =>
  new Response(new Blob([bytes]).stream().pipeThrough(new DecompressionStream("gzip"))).text();
const gzip = async (text) =>
  new Uint8Array(await new Response(new Blob([text]).stream().pipeThrough(new CompressionStream("gzip"))).arrayBuffer());

// Every schema entry and every row of every table the tenant owns, with each value's SQLite type,
// in rowid order, read straight from SQLite rather than through the dump.
function contents(sql) {
  const db = sql.db;
  const schema = db
    .prepare("SELECT type, name, tbl_name, sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type, name")
    .all()
    .filter((row) => isTenantTable(row.name))
    .map((row) => ({ ...row }));
  const tables = {};
  for (const { name, sql: create } of schema.filter((row) => row.type === "table")) {
    const columns = db.prepare(`PRAGMA table_info(${quotedName(name)})`).all().map((column) => column.name);
    const types = columns.map((column) => `typeof(${quotedName(column)})`).join(", ");
    const order = /WITHOUT ROWID/i.test(create) ? "" : " ORDER BY rowid";
    const select = db.prepare(`SELECT ${columns.map(quotedName).join(", ")}, ${types} FROM ${quotedName(name)}${order}`);
    select.setReturnArrays(true);
    tables[name] = select.all();
  }
  return { schema, tables };
}

// A tenant as a year of use leaves it: both schemas, and rows in every shape SQLite stores,
// across more than one page.
function seededTenant() {
  const { db, sql } = tenantDb();
  db.query(SCHEMA_SQL);
  db.query(HOSTED_SCHEMA_SQL);
  db.query("INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)", ["general.timezone", '"Asia/Taipei"', STAMP]);
  db.query("INSERT INTO sources (name, url, type, config) VALUES (?, ?, 'rss', '{}')", ["A news", "https://a.test/feed"]);
  // A row whose foreign key names one with a later id, which the restore inserts after it.
  db.query("CREATE TABLE parents (id INTEGER PRIMARY KEY, name TEXT NOT NULL, parent INTEGER REFERENCES parents(id))");
  db.query("INSERT INTO parents (id, name) VALUES (2, 'New')");
  db.query("INSERT INTO parents (id, name, parent) VALUES (1, 'Old', 2)");
  const page = `<html><body>${"一則新聞 \"quoted\" \n".repeat(4000)}</body></html>`;
  db.query("INSERT INTO hosted_pages (path, body, content_type, updated_at) VALUES (?, ?, ?, ?)", [
    "/2026-11-22-morning.html",
    page,
    "text/html",
    STAMP,
  ]);
  for (let i = 0; i < 2 * PAGE_ROWS + 7; i++) {
    db.query(
      "INSERT INTO articles (url, guid, title, content, published_at, source_name, fetched_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
      [`https://a.test/${i}`, i % 3 ? `g${i}` : null, `Title ${i}`, `body\n${i}`, STAMP, "A news", STAMP],
    );
  }
  // A gap in the rowids, as deleted rows leave.
  db.query("DELETE FROM articles WHERE url = ?", ["https://a.test/5"]);
  // Tables the tenant's container could make, of any name and any value type.
  db.query(`CREATE TABLE ${quotedName('x"; DROP TABLE sources; --')} (i INTEGER, r REAL, t TEXT, b BLOB, n)`);
  db.query(`INSERT INTO ${quotedName('x"; DROP TABLE sources; --')} VALUES (?, ?, ?, ?, ?)`, [
    2 ** 40,
    0.5,
    "",
    new Uint8Array([0, 1, 2, 255]),
    null,
  ]);
  // A whole number in a column with no type, which some runtimes bind as a REAL.
  db.query(`INSERT INTO ${quotedName('x"; DROP TABLE sources; --')} VALUES (2, 1.5, 't', NULL, 42)`);
  // A REAL with no fraction, which JSON alone would bring back as an INTEGER, in a column with no type.
  db.query(`INSERT INTO ${quotedName('x"; DROP TABLE sources; --')} VALUES (1, 3.0, 'x', X'', CAST(7 AS REAL))`);
  db.query("CREATE TABLE kv (k TEXT PRIMARY KEY, v TEXT) WITHOUT ROWID");
  for (const k of ["b", "a", "c"]) db.query("INSERT INTO kv VALUES (?, ?)", [k, k.toUpperCase()]);
  db.query("CREATE VIEW article_titles AS SELECT title FROM articles");
  db.query("CREATE TABLE audit (url TEXT)");
  db.query("CREATE TRIGGER articles_audit AFTER INSERT ON articles BEGIN INSERT INTO audit VALUES (new.url); END");
  db.query("CREATE VIRTUAL TABLE notes USING fts5(body)");
  db.query("INSERT INTO notes (body) VALUES (?), (?)", ["first note", "second note"]);
  return { db, sql };
}

describe("a hosted database's dump", () => {
  it("restores into an empty object with every table's rows and every schema entry as they were", async () => {
    const source = seededTenant();
    const target = tenantDb();

    const answer = await target.db.restoreDump(await source.db.exportDump());

    expect(contents(target.sql)).toEqual(contents(source.sql));
    expect(answer.restored.articles).toBe(2 * PAGE_ROWS + 6);
    expect(answer.restored.audit).toBe(0);
    // The restored trigger is there, and fired on none of the restored rows.
    target.db.query("INSERT INTO articles (url, published_at, source_name, fetched_at) VALUES ('https://a.test/new', ?, 'A', ?)", [STAMP, STAMP]);
    expect(target.db.query("SELECT url FROM audit").rows).toEqual([{ url: "https://a.test/new" }]);
    expect(target.db.query("SELECT body FROM notes WHERE notes MATCH 'second'").rows).toEqual([{ body: "second note" }]);
  });

  it("is the same bytes for the same data, its restored copy's included", async () => {
    const source = seededTenant();
    const first = await source.db.exportDump();
    const target = tenantDb();
    await target.db.restoreDump(first);

    expect(await source.db.exportDump()).toEqual(first);
    expect(await target.db.exportDump()).toEqual(first);
  });

  it("is JSON lines: a header, each table and its rows, the schema, then an end line that counts them", async () => {
    const { db } = tenantDb();
    db.query("CREATE TABLE b (v)");
    db.query("CREATE TABLE a (id INTEGER PRIMARY KEY, v)");
    db.query("CREATE INDEX a_v ON a(v)");
    db.query("INSERT INTO a (v) VALUES (?), (?)", ["one", 1.5]);
    db.query("INSERT INTO a (v) VALUES (CAST(7 AS REAL))");
    db.query("INSERT INTO b (v) VALUES (X'00ff')");

    const lines = (await gunzip(await db.exportDump())).split("\n");

    expect(lines.map((line) => (line ? JSON.parse(line) : line))).toEqual([
      { format: DUMP_FORMAT, version: DUMP_VERSION },
      { table: "a", sql: "CREATE TABLE a (id INTEGER PRIMARY KEY, v)", columns: ["id", "v"] },
      [1, "one"],
      [2, 1.5],
      [3, { real: 7 }],
      { table: "b", sql: "CREATE TABLE b (v)", columns: ["v"] },
      [{ blob: "AP8=" }],
      { schema: "index", name: "a_v", table: "a", sql: "CREATE INDEX a_v ON a(v)" },
      { end: { tables: 2, rows: 4 } },
      "",
    ]);
  });

  it("holds every other event back while it is read, so it is one moment's data", async () => {
    const { db } = seededTenant();
    const registry = registryObject();

    await db.exportDump();
    await registry.exportDump();

    expect(db.ctx.blocked).toBe(1);
    expect(registry.ctx.blocked).toBe(1);
  });

  it("is refused past MAX_DUMP_BYTES compressed, without resetting the object, which keeps its rows", async () => {
    const { db, sql } = tenantDb();
    db.query("CREATE TABLE noise (b BLOB)");
    // Random bytes do not compress, so this many MiB of them is a dump past the limit.
    const rows = Math.ceil(MAX_DUMP_BYTES / 2 ** 20) + 2;
    for (let i = 0; i < rows; i++) sql.exec("INSERT INTO noise VALUES (?)", randomBytes(2 ** 20));

    await expect(db.exportDump()).rejects.toThrow(`the dump passed ${MAX_DUMP_BYTES} bytes compressed`);

    expect(db.ctx.blocked).toBe(1);
    expect(db.ctx.resets).toBe(0);
    expect(db.tables()).toEqual({ noise: rows });
  });

  it("throws a failed read only once the object is unblocked, so the object is not reset", async () => {
    const registry = registryObject();
    const exec = registry.ctx.storage.sql.exec;
    registry.ctx.storage.sql.exec = (statement, ...params) => {
      if (statement.startsWith("PRAGMA table_info")) throw new Error("SQLITE_IOERR: disk I/O error");
      return exec(statement, ...params);
    };

    await expect(registry.exportDump()).rejects.toThrow("SQLITE_IOERR");

    expect(registry.ctx.resets).toBe(0);
  });

  it("carries the registry's tables, which load into an empty object as they were", async () => {
    const registry = registryObject();
    registry.register("alpha", { now: new Date(STAMP) });
    registry.invite("alpha", "reader@example.com", { now: new Date(STAMP) });
    const target = tenantDb();

    await target.db.restoreDump(await registry.exportDump());

    expect(contents(target.sql)).toEqual(contents(registry.ctx.storage.sql));
    expect(target.db.query("SELECT id, status FROM tenants").rows).toEqual([{ id: "alpha", status: "active" }]);
  });

  it("is refused by an object that already holds a table, which keeps its own rows", async () => {
    const source = seededTenant();
    const target = tenantDb();
    target.db.query("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)");
    target.db.query("INSERT INTO settings VALUES ('mine', '1')");

    const answer = await target.db.restoreDump(await source.db.exportDump());

    expect(answer.refused).toMatch(/already holds 1 table/);
    expect(target.db.tables()).toEqual({ settings: 1 });
  });

  it("writes nothing when the dump is cut short or is not a dump", async () => {
    const text = await gunzip(await seededTenant().db.exportDump());
    const cut = text.split("\n").slice(0, 20).join("\n");
    const target = tenantDb();

    await expect(target.db.restoreDump(await gzip(cut))).rejects.toThrow(/cut short/);
    await expect(target.db.restoreDump(await gzip('{"format":"other","version":1}\n'))).rejects.toThrow(/not a cyris-sqlite-dump/);
    await expect(target.db.restoreDump(await gzip(text.replace('"rows":', '"rows":1')))).rejects.toThrow(/end line counts/);

    expect(target.db.tables()).toEqual({});
  });

  it("of the golden database is the golden file scripts/backup_to_sqlite.py is tested on", async () => {
    const { db } = tenantDb();
    for (const statement of GOLDEN_SQL) db.query(statement);

    const golden = readFileSync(new URL("./sqlite_dump_golden.jsonl", import.meta.url), "utf8");

    expect(await gunzip(await db.exportDump())).toBe(golden);
  });
});

// The golden database: a value of each kind SQLite stores, a quoted name, a foreign key named
// before its row, an index, a trigger and a view. tests/test_backup_to_sqlite.py loads its dump.
const GOLDEN_SQL = [
  "CREATE TABLE areas (id INTEGER PRIMARY KEY, name TEXT NOT NULL, merged_into INTEGER REFERENCES areas(id))",
  "INSERT INTO areas VALUES (2, 'New', NULL)",
  "INSERT INTO areas VALUES (1, 'Old', 2)",
  'CREATE TABLE "odd ""name""" (i INTEGER, r REAL, t TEXT, b BLOB, n)',
  `INSERT INTO "odd ""name""" VALUES (1099511627776, 0.25, 'line one
line "two" 一', X'00FF10', CAST(7 AS REAL))`,
  'INSERT INTO "odd ""name""" VALUES (NULL, 3.0, \'\', X\'\', 42)',
  "CREATE INDEX idx_areas_name ON areas(name)",
  "CREATE TABLE audit (id INTEGER)",
  "CREATE TRIGGER areas_audit AFTER INSERT ON areas BEGIN INSERT INTO audit VALUES (new.id); END",
  "CREATE VIEW area_names AS SELECT name FROM areas",
];
