import { DatabaseSync } from "node:sqlite";

// A D1-shaped binding over node:sqlite, so Worker tests run real SQL.
class Statement {
  constructor(db, sql, params = []) {
    this.db = db;
    this.sql = sql;
    this.params = params;
  }

  bind(...params) {
    return new Statement(this.db, this.sql, params);
  }

  async all() {
    return { results: this.db.prepare(this.sql).all(...this.params) };
  }

  async first() {
    return this.db.prepare(this.sql).get(...this.params) ?? null;
  }

  async run() {
    const { changes } = this.db.prepare(this.sql).run(...this.params);
    return { meta: { changes } };
  }
}

export function d1Sqlite() {
  const raw = new DatabaseSync(":memory:");
  return {
    raw,
    prepare: (sql) => new Statement(raw, sql),
    async exec(sql) {
      raw.exec(sql);
    },
  };
}
