// A page's source decision through /api/vote (source_review_cases.json), held on the two Workers:
// the app Worker's route, signed in, over a stand-in for D1's REST API on real SQLite, and the
// hosted Worker's vote route over a tenant's real object. Neither reaches a container or the
// promote Worker. tests/test_source_review_cases.py holds a local `cyris triage-ui` to the same
// cases.
import { describe, it, expect } from "vitest";
import { DatabaseSync } from "node:sqlite";
import { generateKeyPair, exportJWK, SignJWT, createLocalJWKSet } from "jose";
import { createD1Sources } from "../src/d1_sources.js";
import { handleVote } from "../src/hosted_vote.js";
import { handleRequest } from "../src/router.js";
import { SCHEMA_SQL, tenantDb } from "./sql_storage.js";
import contract from "./source_review_cases.json" with { type: "json" };

const TEAM = "team.example";
const AUD = "AUD1";
const { privateKey, publicKey } = await generateKeyPair("RS256");
const JWK = { ...(await exportJWK(publicKey)), kid: "k1", alg: "RS256", use: "sig" };
const ACCESS_KEYS = { keySet: createLocalJWKSet({ keys: [JWK] }), issuer: `https://${TEAM}` };
const jwt = await new SignJWT({})
  .setProtectedHeader({ alg: "RS256", kid: "k1" })
  .setIssuer(`https://${TEAM}`)
  .setAudience(AUD)
  .setIssuedAt()
  .setExpirationTime(Math.floor(Date.now() / 1000) + 3600)
  .sign(privateKey);

const ENV = {
  CYRIS_ACCESS_TEAM_DOMAIN: TEAM,
  CYRIS_ACCESS_AUD: AUD,
  DIGEST_ORIGIN: "https://p.pages.dev",
  CLOUDFLARE_ACCOUNT_ID: "account",
  CYRIS_STORE_DATABASE_ID: "database",
  CLOUDFLARE_API_TOKEN: "token",
};
const D1_QUERY = "https://api.cloudflare.com/client/v4/accounts/account/d1/database/database/query";

const put = (run, { name, url, type, config }) =>
  run("INSERT INTO sources (name, url, type, config) VALUES (?, ?, ?, ?)", [name, url, type, JSON.stringify(config)]);

// D1's REST query endpoint over one SQLite database, answered in its shape; every other URL, the
// promote Worker's included, is refused, so a case that reached one fails.
function d1Rest() {
  const db = new DatabaseSync(":memory:");
  db.exec(SCHEMA_SQL);
  const calls = [];
  const fetchImpl = async (url, init) => {
    calls.push(String(url));
    if (String(url) !== D1_QUERY) return new Response("not this", { status: 599 });
    const { sql, params } = JSON.parse(init.body);
    const statement = db.prepare(sql);
    if (/^\s*select/i.test(sql)) return Response.json({ success: true, result: [{ success: true, results: statement.all(...params), meta: {} }] });
    const { changes } = statement.run(...params);
    return Response.json({ success: true, result: [{ success: true, results: [], meta: { changes } }] });
  };
  for (const row of contract.rows) put((sql, params) => db.prepare(sql).run(...params), row);
  return { fetchImpl, calls };
}

function appWorker() {
  const rest = d1Rest();
  const deps = {
    accessKeys: ACCESS_KEYS,
    fetchImpl: () => Promise.reject(new Error("a decision reached the promote Worker")),
    container: () => Promise.reject(new Error("a decision woke the container")),
    sources: createD1Sources({ env: ENV, fetchImpl: rest.fetchImpl }),
  };
  const send = (method, body) => {
    const headers = new Headers({ "Cf-Access-Jwt-Assertion": jwt });
    const init = { method, headers };
    if (body !== undefined) {
      headers.set("Content-Type", "application/json");
      init.body = JSON.stringify(body);
    }
    return handleRequest(new Request("https://cyris-app.x.workers.dev/api/vote", init), ENV, deps);
  };
  return { send, calls: rest.calls };
}

function hostedWorker() {
  const { db, sql } = tenantDb();
  sql.db.exec(SCHEMA_SQL);
  for (const row of contract.rows) put((statement, params) => db.query(statement, params), row);
  const deps = {
    voteSession: async (token) => (token === "page-token" ? { tenant_id: "alpha" } : null),
    sourceReviews: async () => db.sourceReviews(),
    reviewSource: async (_tenant, name, review) => db.setSourceReview(name, review),
    recordVote: () => Promise.reject(new Error("a decision was queued as a vote")),
  };
  const send = (method, body) => {
    const headers = new Headers({ Origin: "null", "X-Cyris-Vote": "page-token" });
    const init = { method, headers };
    if (body !== undefined) {
      headers.set("Content-Type", "application/json");
      init.body = JSON.stringify(body);
    }
    return handleVote(new Request("https://hosted.example/api/vote", init), deps);
  };
  return { send };
}

describe.each([
  ["the app Worker", appWorker],
  ["the hosted Worker", hostedWorker],
])("a source decision through %s's vote door", (_door, make) => {
  it("lists each source's review in its probe", async () => {
    const door = make();
    const probe = await door.send("GET");

    expect(probe.status).toBe(200);
    expect((await probe.json()).reviews).toEqual(contract.probe.reviews);
  });

  it.each(contract.cases.map((c) => [c.case, c]))("%s", async (_name, c) => {
    const door = make();
    const response = await door.send("POST", c.body);

    expect(response.status).toBe(c.status);
    expect(await response.json()).toEqual(c.answer);
    expect((await (await door.send("GET")).json()).reviews).toEqual(c.reviews);
  });
});

describe("the app Worker's door and a cross-site form", () => {
  it("refuses a decision that is not sent as JSON, and writes nothing", async () => {
    const rest = d1Rest();
    const resp = await handleRequest(
      new Request("https://cyris-app.x.workers.dev/api/vote", {
        method: "POST",
        headers: { "Cf-Access-Jwt-Assertion": jwt, "Content-Type": "text/plain" },
        body: JSON.stringify({ source: "new@letter.example", decision: "stop" }),
      }),
      ENV,
      { accessKeys: ACCESS_KEYS, sources: createD1Sources({ env: ENV, fetchImpl: rest.fetchImpl }) },
    );

    expect([resp.status, await resp.json()]).toEqual([415, { error: "a decision is JSON" }]);
    expect(rest.calls).toEqual([]);
  });
});

describe("the app Worker's door with no D1 to read", () => {
  it("probes with no reviews, and refuses a decision it cannot write", async () => {
    const deps = { accessKeys: ACCESS_KEYS, fetchImpl: () => Promise.reject(new Error("no")), sources: null };
    const send = (init) =>
      handleRequest(
        new Request("https://cyris-app.x.workers.dev/api/vote", { ...init, headers: { "Cf-Access-Jwt-Assertion": jwt, "Content-Type": "application/json" } }),
        { ...ENV, CLOUDFLARE_API_TOKEN: undefined },
        deps,
      );

    expect(await (await send({ method: "GET" })).json()).toEqual({ authorized: true });
    const refused = await send({ method: "POST", body: JSON.stringify({ source: "new@letter.example", decision: "keep" }) });
    expect([refused.status, await refused.json()]).toEqual([503, { error: "no source store" }]);
  });

  it("keeps the marks when D1 does not answer the probe's read", async () => {
    const sources = createD1Sources({ env: ENV, fetchImpl: async () => new Response("down", { status: 500 }) });
    const resp = await handleRequest(
      new Request("https://cyris-app.x.workers.dev/api/vote", { headers: { "Cf-Access-Jwt-Assertion": jwt } }),
      ENV,
      { accessKeys: ACCESS_KEYS, sources },
    );

    expect(await resp.json()).toEqual({ authorized: true });
  });
});
