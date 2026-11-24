// The /api/vote contract both editions answer (vote_cases.json): the app Worker's route, signed
// in, over the real promote Worker and an in-memory KV, so each answer is the one a page reads
// and each accepted vote is the queue entry the run's `promote-sync` applies.
// tests/test_local_votes.py holds a local `cyris triage-ui` to the same cases.
import { describe, it, expect } from "vitest";
import { generateKeyPair, exportJWK, SignJWT, createLocalJWKSet } from "jose";
import { handleRequest } from "../src/router.js";
import promote from "../../promote/src/index.js";
import contract from "./vote_cases.json" with { type: "json" };

const TEAM = "team.example";
const AUD = "AUD1";
const PROMOTE_URL = "https://promote.example";
const PROMOTE_TOKEN = "promote-tok";
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

function kv() {
  const entries = new Map();
  return {
    entries,
    async put(key, value) {
      entries.set(key, value);
    },
  };
}

// The promote Worker answers the app Worker's forward itself, as it does on Cloudflare.
function deps(queue) {
  return {
    accessKeys: ACCESS_KEYS,
    fetchImpl: (url, init) =>
      promote.fetch(new Request(url, init), { PROMOTE_TOKEN, PROMOTIONS: queue }),
  };
}

const ENV = {
  CYRIS_ACCESS_TEAM_DOMAIN: TEAM,
  CYRIS_ACCESS_AUD: AUD,
  DIGEST_ORIGIN: "https://p.pages.dev",
  CYRIS_PROMOTE_WORKER_URL: PROMOTE_URL,
  CYRIS_PROMOTE_TOKEN: PROMOTE_TOKEN,
};

// A probe carries no body; every other case sends its body as JSON, `null` included.
function request({ method, body }) {
  const headers = new Headers({ "Cf-Access-Jwt-Assertion": jwt });
  const init = { method, headers };
  if (method !== "GET" && method !== "HEAD") {
    headers.set("Content-Type", "application/json");
    init.body = JSON.stringify(body);
  }
  return new Request("https://cyris-app.x.workers.dev/api/vote", init);
}

// What `apply_promotions` makes of a queued vote: "down" rejects, anything else accepts.
const verdictOf = (vote) => (vote === "down" ? "rejected" : "accepted");

describe("the vote contract", () => {
  it.each(contract.cases.map((c) => [c.case, c]))("%s", async (_name, c) => {
    const queue = kv();
    const resp = await handleRequest(request(c), ENV, deps(queue));

    expect(resp.status).toBe(c.status);
    if (c.answer !== null) expect(await resp.json()).toEqual(c.answer);
    const queued = [...queue.entries.values()].map((v) => JSON.parse(v));
    if (c.verdict === null) {
      expect(queued).toEqual([]);
    } else {
      expect(queued).toHaveLength(1);
      expect(queued[0].url).toBe(c.body.url);
      expect(verdictOf(queued[0].vote)).toBe(c.verdict);
    }
  });

  it("names one stored article, the one the accepted cases vote on", () => {
    const voted = contract.cases.filter((c) => c.verdict !== null).map((c) => c.body.url);
    expect(voted).toContain(contract.stored);
    expect(voted.some((url) => url !== contract.stored)).toBe(true);
  });
});
