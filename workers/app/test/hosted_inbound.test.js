// A hosted tenant's newsletters: a random address per tenant in the registry, the hosted Worker's
// email() handler storing each message only in the addressed tenant's own object, and a run's
// gateway that reads and acks only its own tenant's queue.
// docs/decisions/0034-a-hosted-tenants-newsletters-arrive-at-a-random-address.md.
import { afterEach, beforeEach, describe, it, expect, vi } from "vitest";
import { env } from "cloudflare:workers";
import { generateKeyPair, exportJWK, SignJWT } from "jose";
import worker from "../src/hosted.js";
import { ADDRESS, ADDRESS_BYTES, randomAddress, recipientAddress } from "../src/inbound_address.js";
import { MAX_RAW_BYTES, authResults } from "../src/hosted_inbound.js";
import { DB_HOST, TenantDbGateway, isTenantTable } from "../src/tenant_db.js";
import { MAX_QUEUED } from "../src/tenant_inbound.js";

// What a hosted run writes for the messages it leaves queued because no source matches their
// sender (`TenantNewsletterSource._hold` in src/cyris/adapters/fetch/newsletter_worker_source.py).
const holdUnmatched = (TENANT_DB, tenant, sender, at) =>
  TENANT_DB.getByName(tenant).query("UPDATE newsletters SET held_at = ? WHERE held_at IS NULL AND sender = ?", [
    at,
    sender,
  ]);
import { tenantRegistry } from "../src/tenant_registry.js";
import CASES from "./newsletter_source_cases.json";
import { namespace, registryNamespace } from "./sql_storage.js";

const DOMAIN = "in.cyris.example";
const TEAM = "inbound-team.example";
const AUD = "AUD-INBOUND";
// The service token the operator signs in with: its Client ID, which Access signs in as `common_name`.
const OPERATOR = "operator-token.access";

// One issue as a publisher sends it, to `to`, with Cloudflare's Authentication-Results on top.
const issue = (
  to,
  {
    subject = "Issue 12",
    date = "Tue, 18 Nov 2026 06:00:00 +0000",
    auth = "dmarc=pass",
    body = "This week's letter.",
    from = "letter@news.example",
  } = {},
) =>
  [
    `Authentication-Results: mx.cloudflare.net; spf=pass smtp.mailfrom=news.example; dkim=pass header.d=news.example; ${auth}`,
    `From: The Letter <${from}>`,
    `To: ${to}`,
    `Subject: ${subject}`,
    `Date: ${date}`,
    "List-Id: <letter.news.example>",
    "Content-Type: text/plain; charset=utf-8",
    "",
    `${body} Read it at https://news.example/p/issue-12`,
    "",
  ].join("\r\n");

// ForwardableEmailMessage as Email Routing hands one to email(): the envelope, the raw message,
// and the three things a handler can do to the SMTP answer, which this records. A handler that
// calls none of them has accepted the message.
function message(to, raw = issue(to), { rawSize = raw.length } = {}) {
  const answered = [];
  return {
    from: "bounce@news.example",
    to,
    raw,
    rawSize,
    headers: new Headers(),
    answered,
    setReject: (reason) => answered.push(["reject", reason]),
    forward: async (rcpt) => answered.push(["forward", rcpt]),
    reply: async (reply) => answered.push(["reply", reply]),
  };
}

const rows = (TENANT_DB, tenant, sql = "SELECT id, sender, subject, text FROM newsletters ORDER BY id") =>
  TENANT_DB.objects.has(tenant) ? TENANT_DB.getByName(tenant).query(sql).rows : null;

describe("a newsletter address", () => {
  it("is 128 random bits in lowercase base32, and comes from the random source alone", () => {
    expect(ADDRESS_BYTES * 8).toBe(128);
    expect(randomAddress((bytes) => bytes.fill(0))).toBe("a".repeat(26));
    expect(randomAddress((bytes) => bytes.fill(255))).toBe(`${"7".repeat(25)}4`);
    const drawn = new Set(Array.from({ length: 2000 }, () => randomAddress()));
    expect(drawn.size).toBe(2000);
    for (const address of drawn) expect(address).toMatch(ADDRESS);
  });

  it("is read from the envelope recipient's local part, in any case, and nothing else is one", () => {
    const address = randomAddress();
    expect(recipientAddress(`${address}@${DOMAIN}`)).toBe(address);
    expect(recipientAddress(`${address.toUpperCase()}@${DOMAIN}`)).toBe(address);
    for (const to of [`${address}+news@${DOMAIN}`, `alpha@${DOMAIN}`, `${address}x@${DOMAIN}`, address, "", null]) {
      expect(recipientAddress(to), String(to)).toBeNull();
    }
  });

  it("is drawn by the registry with nothing of the tenant in it, and replaces the one before", () => {
    const registry = registryNamespace().getByName("cyris:registry");
    registry.register("alpha");
    registry.register("bravo");
    // The same random bytes give two tenants the same address: the id plays no part.
    const fixed = vi.spyOn(crypto, "getRandomValues").mockImplementation((bytes) => bytes.fill(7));
    const alpha = registry.rotateInboundAddress("alpha");
    registry.revokeInboundAddress("alpha");
    const bravo = registry.rotateInboundAddress("bravo");
    fixed.mockRestore();

    expect(alpha.address).toBe(bravo.address);
    expect(alpha.address).not.toContain("alpha");
    const next = registry.rotateInboundAddress("bravo");
    expect(next).toMatchObject({ revoked: 1 });
    expect(next.address).not.toBe(bravo.address);
    expect(registry.inboundTenant(bravo.address)).toBeNull();
    expect(registry.inboundTenant(next.address)).toBe("bravo");
    expect(registry.inboundAddress("bravo").address).toBe(next.address);
    expect(registry.rotateInboundAddress("charlie")).toEqual({ refused: "charlie is not a registered tenant" });
  });

  it("is read from the Cloudflare verdicts only when Cloudflare wrote the topmost header", () => {
    const header = (value) => ({ key: "authentication-results", value });

    expect(authResults([header("mx.cloudflare.net; spf=pass; dkim=fail; dmarc=FAIL"), header("x; dmarc=pass")])).toEqual({
      spf: "pass",
      dkim: "fail",
      dmarc: "fail",
    });
    expect(authResults([header("evil.example; dmarc=pass"), header("mx.cloudflare.net; dmarc=fail")])).toBeNull();
    expect(authResults([])).toBeNull();
  });
});

describe("the hosted Worker's email() handler", () => {
  let bound;
  let privateKey;
  let logs;

  beforeEach(async () => {
    const keys = await generateKeyPair("RS256");
    privateKey = keys.privateKey;
    const jwk = { ...(await exportJWK(keys.publicKey)), kid: "k1", alg: "RS256", use: "sig" };
    vi.stubGlobal("fetch", async () => Response.json({ keys: [jwk] }));
    logs = [];
    vi.spyOn(console, "log").mockImplementation((...args) => logs.push(JSON.stringify(args)));
    bound = { REGISTRY: registryNamespace(), TENANT_DB: namespace(), TENANT_RUN: namespace() };
    Object.assign(env, bound, { CYRIS_ACCESS_TEAM_DOMAIN: `${crypto.randomUUID()}.${TEAM}`, CYRIS_ACCESS_AUD: AUD, CYRIS_HOSTED_OPERATORS: OPERATOR });
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
    for (const key of Object.keys(env)) delete env[key];
  });

  const operator = async (method, path) => {
    const jwt = await new SignJWT({ common_name: OPERATOR })
      .setProtectedHeader({ alg: "RS256", kid: "k1" })
      .setIssuer(`https://${env.CYRIS_ACCESS_TEAM_DOMAIN}`)
      .setAudience(AUD)
      .setIssuedAt()
      .setExpirationTime(Math.floor(Date.now() / 1000) + 3600)
      .sign(privateKey);
    const request = new Request(`https://cyris-hosted.example.workers.dev${path}`, {
      method,
      headers: { "Cf-Access-Jwt-Assertion": jwt },
    });
    return worker.fetch(request);
  };

  // Registers each tenant and issues it an address through the operator's routes.
  async function addressed(...tenants) {
    const addresses = {};
    for (const tenant of tenants) {
      await operator("POST", `/hosted/tenants/${tenant}/register`);
      const rotated = await operator("POST", `/hosted/tenants/${tenant}/rotate-address`);
      expect(rotated.status).toBe(200);
      addresses[tenant] = (await rotated.json()).address;
    }
    return addresses;
  }

  it("stores one issue two tenants subscribed to in each tenant's own object", async () => {
    const { alpha, bravo } = await addressed("alpha", "bravo");

    const toAlpha = message(`${alpha}@${DOMAIN}`);
    const toBravo = message(`${bravo}@${DOMAIN}`);
    await worker.email(toAlpha);
    await worker.email(toBravo);

    expect([toAlpha.answered, toBravo.answered]).toEqual([[], []]);
    const [a] = rows(bound.TENANT_DB, "alpha");
    const [b] = rows(bound.TENANT_DB, "bravo");
    expect(a).toMatchObject({ sender: "letter@news.example", subject: "Issue 12" });
    expect(a.text).toContain("https://news.example/p/issue-12");
    // The same issue keys the same id in each, and neither write overwrote the other.
    expect(b).toEqual(a);
    expect(a.id).toMatch(/^nl:[0-9a-f]{64}$/);
  });

  it("lists each tenant's queued issues in its report by sender and subject, with no body", async () => {
    const { alpha, bravo } = await addressed("alpha", "bravo");
    await worker.email(message(`${alpha}@${DOMAIN}`));
    await operator("POST", "/hosted/tenants/bravo/seed");

    const reports = {};
    for (const tenant of ["alpha", "bravo"]) {
      reports[tenant] = await (await operator("GET", `/hosted/tenants/${tenant}/report`)).json();
    }

    expect(reports.alpha.newsletters).toEqual([
      expect.objectContaining({ sender: "letter@news.example", subject: "Issue 12", date: "2026-11-18T06:00:00.000Z" }),
    ]);
    expect(JSON.stringify(reports.alpha)).not.toContain("This week's letter");
    expect(JSON.stringify(reports)).not.toContain(alpha);
    expect(reports.bravo.newsletters).toEqual([]);
    expect(bravo).toMatch(ADDRESS);
  });

  it("keeps the first arrival of an issue: a re-delivered copy or a forged one changes nothing", async () => {
    const { alpha } = await addressed("alpha");
    const to = `${alpha}@${DOMAIN}`;

    await worker.email(message(to));
    await worker.email(message(to));
    // The same From, Subject and Date, which any sender can write, with another body.
    const forged = message(to, issue(to, { body: "Click here to verify your account." }));
    await worker.email(forged);

    const queued = rows(bound.TENANT_DB, "alpha");
    expect(queued).toHaveLength(1);
    expect(queued[0].text).toContain("This week's letter.");
    expect(queued[0].text).not.toContain("verify your account");
    expect(forged.answered).toEqual([]);
  });

  it("drops mail to every address no tenant holds without a word to the sender, and writes no tenant", async () => {
    const { alpha } = await addressed("alpha");
    const rotated = await (await operator("POST", "/hosted/tenants/alpha/rotate-address")).json();
    await addressed("bravo");
    await operator("POST", "/hosted/tenants/bravo/revoke-address");
    bound.TENANT_DB.gets.length = 0;

    const dropped = [
      `${randomAddress()}@${DOMAIN}`, // never issued
      `${alpha}@${DOMAIN}`, // alpha's address before its rotation
      `${rotated.address}+news@${DOMAIN}`, // a subaddress of alpha's current one
      `alpha@${DOMAIN}`, // a tenant's id
      `postmaster@${DOMAIN}`,
      "not-an-address",
    ];
    // What a live address's sender sees, which every dropped one must match.
    const live = message(`${rotated.address}@${DOMAIN}`);
    const liveAnswer = await worker.email(live);
    expect(live.answered).toEqual([]);
    bound.TENANT_DB.gets.length = 0;
    logs.length = 0;
    for (const to of dropped) {
      const sent = message(to);
      expect(await worker.email(sent), to).toEqual(liveAnswer);
      expect(sent.answered, to).toEqual([]);
    }
    expect(bound.TENANT_DB.gets).toEqual([]);
    // No log line names the address it dropped.
    for (const to of dropped) expect(logs.join("\n")).not.toContain(to.split("@")[0]);
  });

  it("drops a message too large, or past the queue's cap, as silently, and stores neither", async () => {
    const { alpha } = await addressed("alpha");
    const to = `${alpha}@${DOMAIN}`;

    const large = message(to, issue(to), { rawSize: MAX_RAW_BYTES + 1 });
    await worker.email(large);
    expect(large.answered).toEqual([]);
    expect(rows(bound.TENANT_DB, "alpha")).toBeNull();

    for (let i = 0; i < MAX_QUEUED; i += 1) await worker.email(message(to, issue(to, { subject: `Issue ${i}` })));
    const over = message(to, issue(to, { subject: "One more" }));
    await worker.email(over);

    expect(over.answered).toEqual([]);
    expect(rows(bound.TENANT_DB, "alpha")).toHaveLength(MAX_QUEUED);
    expect(rows(bound.TENANT_DB, "alpha").map((row) => row.subject)).not.toContain("One more");
  });

  it("lets no flood of held, unmatched mail crowd out a new issue", async () => {
    const { alpha } = await addressed("alpha");
    const to = `${alpha}@${DOMAIN}`;
    // A sender holding the leaked address fills the queue with mail no source will match.
    for (let i = 0; i < MAX_QUEUED; i += 1) {
      await worker.email(message(to, issue(to, { subject: `Spam ${i}`, from: "spam@else.example" })));
    }
    // Until a run has seen them, the queue is full of mail nobody has sorted: a new issue waits.
    await worker.email(message(to, issue(to, { subject: "Before the run" })));
    expect(rows(bound.TENANT_DB, "alpha").map((row) => row.subject)).not.toContain("Before the run");

    // The run matches none of them and leaves them held; from then on each new issue takes the
    // place of a held message, and more spam only takes the place of older spam.
    holdUnmatched(bound.TENANT_DB, "alpha", "spam@else.example", "2026-11-18T08:00:00+00:00");
    await worker.email(message(to, issue(to, { subject: "Issue 13" })));
    await worker.email(message(to, issue(to, { subject: "Spam again", from: "spam@else.example" })));
    holdUnmatched(bound.TENANT_DB, "alpha", "spam@else.example", "2026-11-18T20:00:00+00:00");
    for (let i = 14; i < 20; i += 1) await worker.email(message(to, issue(to, { subject: `Issue ${i}` })));

    const subjects = rows(bound.TENANT_DB, "alpha").map((row) => row.subject);
    expect(subjects).toHaveLength(MAX_QUEUED);
    expect(subjects.filter((subject) => subject.startsWith("Issue")).sort()).toEqual(
      ["Issue 13", "Issue 14", "Issue 15", "Issue 16", "Issue 17", "Issue 18", "Issue 19"],
    );
    // The spam held longest went first: the one held at 20:00 outlasts every one held at 08:00.
    expect(subjects).toContain("Spam again");
  });

  it("keeps taking a paused tenant's mail, so pausing loses none", async () => {
    const { alpha } = await addressed("alpha");
    await operator("POST", "/hosted/tenants/alpha/register?status=paused");

    const sent = message(`${alpha}@${DOMAIN}`);
    await worker.email(sent);

    expect(sent.answered).toEqual([]);
    expect(rows(bound.TENANT_DB, "alpha")).toHaveLength(1);
  });

  it("keeps the address out of the tenant's object, which its run container reads", async () => {
    const { alpha } = await addressed("alpha");

    await worker.email(message(`${alpha}@${DOMAIN}`));

    const db = bound.TENANT_DB.getByName("alpha");
    const tables = db
      .query("SELECT name FROM sqlite_master WHERE type = 'table'")
      .rows.map((row) => row.name)
      .filter(isTenantTable);
    expect(tables).toContain("newsletters");
    for (const name of tables) expect(JSON.stringify(db.query(`SELECT * FROM "${name}"`).rows)).not.toContain(alpha);
  });

  it("logs Cloudflare's verdicts for what it stored, and no content", async () => {
    const { alpha } = await addressed("alpha");

    await worker.email(message(`${alpha}@${DOMAIN}`, issue(`${alpha}@${DOMAIN}`, { auth: "dmarc=fail" })));

    const stored = logs.find((line) => line.includes("inbound stored"));
    expect(JSON.parse(stored)).toEqual([
      "inbound stored",
      { tenant: "alpha", auth: { spf: "pass", dkim: "pass", dmarc: "fail" } },
    ]);
    expect(logs.join("\n")).not.toContain("Issue 12");
  });

  it("lets tenant A's run neither read nor ack tenant B's mail", async () => {
    const { alpha, bravo } = await addressed("alpha", "bravo");
    await worker.email(message(`${alpha}@${DOMAIN}`));
    await worker.email(message(`${bravo}@${DOMAIN}`));
    await worker.email(message(`${bravo}@${DOMAIN}`, issue(`${bravo}@${DOMAIN}`, { subject: "Only bravo's" })));
    const bravoIds = rows(bound.TENANT_DB, "bravo").map((row) => row.id);
    const [alphaId] = rows(bound.TENANT_DB, "alpha").map((row) => row.id);
    // Alpha's run reaches its database only through a gateway registered for alpha.
    const gateway = new TenantDbGateway({ props: { tenant: "alpha" } }, env);
    const ask = (tenant, sql, params = []) =>
      gateway.fetch(new Request(`http://${DB_HOST}/t/${tenant}/query`, { method: "POST", body: JSON.stringify({ sql, params }) }));

    const pulled = await (await ask("alpha", 'SELECT id, sender AS "from", subject FROM newsletters')).json();
    expect(pulled.rows.map((row) => row.id)).toEqual([alphaId]);
    // Its ack deletes by id in its own object: bravo's ids, which alpha shares one of, leave
    // bravo's queue whole.
    const acked = await (await ask("alpha", `DELETE FROM newsletters WHERE id IN (${bravoIds.map(() => "?").join(", ")})`, bravoIds)).json();
    expect(acked.changes).toBe(1);
    expect(rows(bound.TENANT_DB, "bravo").map((row) => row.id)).toEqual(bravoIds);
    for (const sql of ["SELECT id FROM newsletters", "DELETE FROM newsletters"]) {
      expect((await ask("bravo", sql)).status).toBe(403);
    }
    expect(rows(bound.TENANT_DB, "bravo")).toHaveLength(2);
  });
});

describe("the operator's address routes", () => {
  let privateKey;
  let bound;

  beforeEach(async () => {
    const keys = await generateKeyPair("RS256");
    privateKey = keys.privateKey;
    const jwk = { ...(await exportJWK(keys.publicKey)), kid: "k1", alg: "RS256", use: "sig" };
    vi.stubGlobal("fetch", async () => Response.json({ keys: [jwk] }));
    bound = { REGISTRY: registryNamespace(), TENANT_DB: namespace(), TENANT_RUN: namespace() };
    Object.assign(env, bound, { CYRIS_ACCESS_TEAM_DOMAIN: `${crypto.randomUUID()}.${TEAM}`, CYRIS_ACCESS_AUD: AUD, CYRIS_HOSTED_OPERATORS: OPERATOR });
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    for (const key of Object.keys(env)) delete env[key];
  });

  const call = async (method, path, { signedIn = true } = {}) => {
    const headers = {};
    if (signedIn) {
      headers["Cf-Access-Jwt-Assertion"] = await new SignJWT({ common_name: OPERATOR })
        .setProtectedHeader({ alg: "RS256", kid: "k1" })
        .setIssuer(`https://${env.CYRIS_ACCESS_TEAM_DOMAIN}`)
        .setAudience(AUD)
        .setIssuedAt()
        .setExpirationTime(Math.floor(Date.now() / 1000) + 3600)
        .sign(privateKey);
    }
    return worker.fetch(new Request(`https://cyris-hosted.example.workers.dev${path}`, { method, headers }));
  };

  it("refuse every request without an Access sign-in, and every tenant the registry does not hold", async () => {
    for (const [method, action] of [
      ["GET", "address"],
      ["POST", "rotate-address"],
      ["POST", "revoke-address"],
    ]) {
      expect((await call(method, `/hosted/tenants/alpha/${action}`, { signedIn: false })).status).toBe(403);
      const unregistered = await call(method, `/hosted/tenants/alpha/${action}`);
      expect(unregistered.status).toBe(404);
      expect(await unregistered.json()).toEqual({ error: "alpha is not a registered tenant" });
    }
    expect(await tenantRegistry(env).inboundAddress("alpha")).toBeNull();
  });

  it("issue, read, rotate and revoke a tenant's address, and take each verb only", async () => {
    await call("POST", "/hosted/tenants/alpha/register");
    expect((await call("GET", "/hosted/tenants/alpha/address")).status).toBe(404);
    expect((await call("GET", "/hosted/tenants/alpha/rotate-address")).status).toBe(405);

    const first = await (await call("POST", "/hosted/tenants/alpha/rotate-address")).json();
    expect(first).toMatchObject({ tenant: "alpha", revoked: 0 });
    expect(first.address).toMatch(ADDRESS);
    expect(await (await call("GET", "/hosted/tenants/alpha/address")).json()).toEqual({
      tenant: "alpha",
      address: first.address,
      created_at: first.created_at,
    });

    const second = await (await call("POST", "/hosted/tenants/alpha/rotate-address")).json();
    expect(second.revoked).toBe(1);
    expect(await tenantRegistry(env).inboundTenant(first.address)).toBeNull();
    expect(await tenantRegistry(env).inboundTenant(second.address)).toBe("alpha");

    expect(await (await call("POST", "/hosted/tenants/alpha/revoke-address")).json()).toEqual({ tenant: "alpha", revoked: 1 });
    expect(await tenantRegistry(env).inboundTenant(second.address)).toBeNull();
    expect((await call("GET", "/hosted/tenants/alpha/address")).status).toBe(404);
    // No route reached a tenant's object to do any of it.
    expect(bound.TENANT_DB.gets).toEqual([]);
  });
});

describe("the operator's newsletter-source route", () => {
  let privateKey;
  let bound;

  beforeEach(async () => {
    const keys = await generateKeyPair("RS256");
    privateKey = keys.privateKey;
    const jwk = { ...(await exportJWK(keys.publicKey)), kid: "k1", alg: "RS256", use: "sig" };
    vi.stubGlobal("fetch", async () => Response.json({ keys: [jwk] }));
    vi.spyOn(console, "log").mockImplementation(() => {});
    bound = { REGISTRY: registryNamespace(), TENANT_DB: namespace(), TENANT_RUN: namespace() };
    Object.assign(env, bound, { CYRIS_ACCESS_TEAM_DOMAIN: `${crypto.randomUUID()}.${TEAM}`, CYRIS_ACCESS_AUD: AUD, CYRIS_HOSTED_OPERATORS: OPERATOR });
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
    for (const key of Object.keys(env)) delete env[key];
  });

  const call = async (method, path) => {
    const jwt = await new SignJWT({ common_name: OPERATOR })
      .setProtectedHeader({ alg: "RS256", kid: "k1" })
      .setIssuer(`https://${env.CYRIS_ACCESS_TEAM_DOMAIN}`)
      .setAudience(AUD)
      .setIssuedAt()
      .setExpirationTime(Math.floor(Date.now() / 1000) + 3600)
      .sign(privateKey);
    const request = new Request(`https://cyris-hosted.example.workers.dev${path}`, {
      method,
      headers: { "Cf-Access-Jwt-Assertion": jwt },
    });
    return worker.fetch(request);
  };
  const add = (tenant, query) => call("POST", `/hosted/tenants/${tenant}/newsletter-source?${new URLSearchParams(query)}`);
  const sourceRows = (tenant) =>
    bound.TENANT_DB.getByName(tenant)
      .query("SELECT name, url, type, config FROM sources WHERE type = 'newsletter' ORDER BY name")
      .rows.map((row) => ({ ...row, config: JSON.parse(row.config) }));

  it("refuses a tenant the registry does not hold, and one not seeded yet", async () => {
    expect((await add("alpha", CASES.written[0].query)).status).toBe(404);
    await call("POST", "/hosted/tenants/alpha/register");

    const unseeded = await add("alpha", CASES.written[0].query);

    expect(unseeded.status).toBe(409);
    expect(await unseeded.json()).toEqual({ error: "seed the tenant first" });
  });

  it("writes each case's row into the tenant's sources, and adding it again rewrites it", async () => {
    await call("POST", "/hosted/tenants/alpha/register");
    await call("POST", "/hosted/tenants/alpha/seed");

    for (const { query, row } of CASES.written) {
      const added = await add("alpha", query);
      expect(added.status).toBe(200);
      expect(await added.json()).toEqual({ tenant: "alpha", source: row });
    }
    await add("alpha", CASES.written[0].query);

    expect(sourceRows("alpha")).toEqual(CASES.written.map((c) => c.row).sort((a, b) => a.name.localeCompare(b.name)));
  });

  it("refuses each query that is not a source, and a name an RSS source holds, writing nothing", async () => {
    await call("POST", "/hosted/tenants/alpha/register");
    await call("POST", "/hosted/tenants/alpha/seed");

    for (const { query, error } of CASES.refused) {
      const refused = await add("alpha", query);
      expect(refused.status, JSON.stringify(query)).toBe(400);
      expect(await refused.json()).toEqual({ error });
    }
    const taken = await add("alpha", { name: "Hacker News", from: "hn@news.example" });
    expect(taken.status).toBe(409);
    expect(await taken.json()).toEqual({ error: "Hacker News is already a rss source" });
    expect(sourceRows("alpha")).toEqual([]);
  });

  it("gives two tenants the same newsletter source, and one issue reaches both their queues", async () => {
    const addresses = {};
    for (const tenant of ["alpha", "bravo"]) {
      await call("POST", `/hosted/tenants/${tenant}/register`);
      await call("POST", `/hosted/tenants/${tenant}/seed`);
      await add(tenant, CASES.written[0].query);
      addresses[tenant] = (await (await call("POST", `/hosted/tenants/${tenant}/rotate-address`)).json()).address;
    }

    // The publisher writes its From in mixed case, as the route's query named it.
    for (const tenant of ["alpha", "bravo"]) {
      const to = `${addresses[tenant]}@${DOMAIN}`;
      await worker.email(message(to, issue(to, { from: "Letter@News.example" })));
    }

    for (const tenant of ["alpha", "bravo"]) {
      expect(sourceRows(tenant)).toEqual([CASES.written[0].row]);
      // The sender is stored as written and the source in lower case: the run matches them in
      // any case (tests/test_tenant_newsletter_source.py).
      const [queued] = rows(bound.TENANT_DB, tenant);
      expect(queued.sender).toBe("Letter@News.example");
      expect(CASES.written[0].row.config.email_match).toBe("from:letter@news.example");
    }
  });
});
