// Each hosted tenant's monthly AI allowance and its spend against it, kept in the registry's
// `spend_rollup` and `tenants.monthly_ai_budget_usd` (tenant_registry.js). The beta charges
// readers nothing and the operator pays every model call, so each tenant's month has a ceiling:
// a run that starts with its tenant's allowance spent makes its issue with no AI call at all.
// Why the registry holds it, and why a run is checked only as it starts:
// docs/decisions/0038-a-hosted-tenants-ai-spend-is-metered-in-the-registry.md.
//
// No workerd-only imports: tenant_registry.js and its tests run this under Node.

// What a tenant's month allows unless the operator sets another: Haiku 5.5 making two issues a
// day of 150 articles, every one of them kept, for a month, measured at about 0.0168 USD a run
// on 2026-11-13 (68,393 input and 20,052 output tokens) with room for the embedding beside it.
export const DEFAULT_MONTHLY_AI_BUDGET_USD = 2.25;

// The most the budget route takes. A bound on a typo, not a policy: the operator's real ceiling
// is what they choose under it.
export const MAX_MONTHLY_AI_BUDGET_USD = 100;

// The columns `spend_rollup` gained with this card, and the tenant's allowance, beside the
// `cost_usd` and `runs` the registry created the table with. `cost_usd` stays the month's total,
// `llm_usd` plus `embedding_usd`; `unpriced_tokens` are tokens of a model with no rate card in the
// container, which it charged at the dearest rate card it has. Added in place, as ADR-0032's
// columns are.
export const SPEND_COLUMNS = [
  ["spend_rollup", "llm_usd", "REAL NOT NULL DEFAULT 0"],
  ["spend_rollup", "embedding_usd", "REAL NOT NULL DEFAULT 0"],
  ["spend_rollup", "input_tokens", "INTEGER NOT NULL DEFAULT 0"],
  ["spend_rollup", "output_tokens", "INTEGER NOT NULL DEFAULT 0"],
  ["spend_rollup", "unpriced_tokens", "INTEGER NOT NULL DEFAULT 0"],
  ["tenants", "monthly_ai_budget_usd", `REAL NOT NULL DEFAULT ${DEFAULT_MONTHLY_AI_BUDGET_USD}`],
];

export const SPEND_ROLLUP_SCHEMA = `
CREATE TABLE IF NOT EXISTS spend_rollup (
  tenant_id TEXT NOT NULL,
  month TEXT NOT NULL,
  cost_usd REAL NOT NULL DEFAULT 0,
  runs INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL,
  ${SPEND_COLUMNS.filter(([table]) => table === "spend_rollup").map(([, column, type]) => `${column} ${type},`).join("\n  ")}
  PRIMARY KEY (tenant_id, month)
);`;

// A month of the ledger, `YYYY-MM` in UTC. UTC and not the tenant's zone: the allowance is the
// operator's loss ceiling, counted in the months the model providers bill in, and one month for
// every tenant lets one query list them all.
export const MONTH = /^\d{4}-(0[1-9]|1[0-2])$/;
export const spendMonth = (now) => now.toISOString().slice(0, 7);

// The fields one run's report carries (`report_spend` in src/cyris/adapters/hosted_spend.py).
const DOLLARS = ["llm_usd", "embedding_usd"];
const TOKENS = ["input_tokens", "output_tokens", "unpriced_tokens"];

// `report` as the numbers the ledger adds, or `{refused}`. Nothing negative, since a report that
// credited the month would reopen a spent allowance, and nothing missing or not a number.
export function checkSpend(report) {
  if (!report || typeof report !== "object") return { refused: "the body must be a JSON object" };
  const spend = {};
  for (const key of [...DOLLARS, ...TOKENS]) {
    const value = report[key] ?? 0;
    if (typeof value !== "number" || !Number.isFinite(value) || value < 0) {
      return { refused: `${key} must be a number at least 0, not ${JSON.stringify(report[key])}` };
    }
    if (TOKENS.includes(key) && !Number.isInteger(value)) return { refused: `${key} must be a whole number` };
    spend[key] = value;
  }
  return { spend };
}

// The allowance the operator's budget route takes, as a number, or `{refused}`.
export function checkBudget(raw) {
  const usd = raw === null || raw === undefined || raw === "" ? NaN : Number(raw);
  if (!Number.isFinite(usd) || usd < 0 || usd > MAX_MONTHLY_AI_BUDGET_USD) {
    return { refused: `usd is a number from 0 to ${MAX_MONTHLY_AI_BUDGET_USD}, not ${raw}` };
  }
  return { usd };
}

const ADD_SPEND = `
INSERT INTO spend_rollup (tenant_id, month, cost_usd, llm_usd, embedding_usd, input_tokens, output_tokens, unpriced_tokens, runs, updated_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
ON CONFLICT (tenant_id, month) DO UPDATE SET
  cost_usd = cost_usd + excluded.cost_usd,
  llm_usd = llm_usd + excluded.llm_usd,
  embedding_usd = embedding_usd + excluded.embedding_usd,
  input_tokens = input_tokens + excluded.input_tokens,
  output_tokens = output_tokens + excluded.output_tokens,
  unpriced_tokens = unpriced_tokens + excluded.unpriced_tokens,
  runs = runs + 1,
  updated_at = excluded.updated_at`;

// Adds one run's checked spend to `tenant`'s month, the month it is reported in: a run that
// starts before midnight UTC on the last day and ends after it is charged to the new month.
export function addSpend(sql, tenant, spend, month, at) {
  const cost = spend.llm_usd + spend.embedding_usd;
  sql.exec(
    ADD_SPEND,
    tenant,
    month,
    cost,
    spend.llm_usd,
    spend.embedding_usd,
    spend.input_tokens,
    spend.output_tokens,
    spend.unpriced_tokens,
    at,
  );
}

const MONTH_ROW = `
SELECT cost_usd, llm_usd, embedding_usd, input_tokens, output_tokens, unpriced_tokens, runs
FROM spend_rollup WHERE tenant_id = ? AND month = ?`;

const NOTHING_SPENT = { cost_usd: 0, llm_usd: 0, embedding_usd: 0, input_tokens: 0, output_tokens: 0, unpriced_tokens: 0, runs: 0 };

// `tenant`'s `month`: its allowance, what it has spent, and `spent`, whether a run starting now
// may call no model. A month with no row has spent nothing, which is how every tenant's allowance
// comes back on the first of the month. An allowance of 0 is always spent.
export function monthSpend(sql, tenantRow, month) {
  const [row] = sql.exec(MONTH_ROW, tenantRow.id, month).toArray();
  const spent = row ? { ...row } : { ...NOTHING_SPENT };
  const budget = tenantRow.monthly_ai_budget_usd ?? DEFAULT_MONTHLY_AI_BUDGET_USD;
  return { tenant: tenantRow.id, month, budget_usd: budget, ...spent, spent: spent.cost_usd >= budget };
}

const ALL_TENANTS = "SELECT id, status, monthly_ai_budget_usd FROM tenants ORDER BY id";

// Every registered tenant's `month`, in id order, a tenant that ran nothing with zeros.
export function spendReport(sql, month) {
  return sql
    .exec(ALL_TENANTS)
    .toArray()
    .map((row) => ({ ...monthSpend(sql, row, month), status: row.status }));
}
