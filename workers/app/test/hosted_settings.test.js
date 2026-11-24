import { describe, it, expect } from "vitest";
import EN from "../../../src/cyris/locales/en.json" with { type: "json" };
import ZH_HANT from "../../../src/cyris/locales/zh-Hant.json" with { type: "json" };
import FEED_CATALOG from "../../../src/cyris/onboarding_feeds.json" with { type: "json" };
import STARTING_VALUES from "../../../src/cyris/settings_starting_values.json" with { type: "json" };
import SEED from "../src/hosted_seed.json" with { type: "json" };
import POLICY from "../src/hosted_settings_policy.json" with { type: "json" };
import { seedTenant } from "../src/hosted_seed.js";
import { CATALOGS, REFUSAL_REASONS } from "../src/hosted_settings.js";
import { WRITABLE, catalogTag, checkSettings, rssSource, urlRefusal } from "../src/hosted_settings_rules.js";
import CASES from "./hosted_settings_cases.json" with { type: "json" };
import { HOSTED_SCHEMA_SQL, SCHEMA_SQL, tenantDb } from "./sql_storage.js";

// The schedule the seed writes a tenant the registry has none for: one hour, in UTC.
const SEEDED_SCHEDULE = { times: ["08:00"], timeZone: "UTC" };

const DOMAIN = "cyris-hosted.example.workers.dev";

describe("a tenant's settings save", () => {
  it.each(CASES.settings.accepted)("takes $key = $value", ({ key, value }) => {
    expect(checkSettings({ [key]: value })).toEqual({ rows: [[key, value]], schedule: null });
  });

  it.each(CASES.settings.refused)("refuses $key = $value as $reason", ({ key, value, reason }) => {
    expect(checkSettings({ [key]: value }).refused).toMatchObject({ reason, key });
  });

  it("writes exactly the policy's writable keys", () => {
    expect([...WRITABLE].sort()).toEqual([...POLICY.writable_settings].sort());
  });

  it("takes JSON's own types only: a number in a string is refused", () => {
    expect(checkSettings({ "digest.max_articles_per_digest": "5" }).refused.reason).toBe("whole_number");
    expect(checkSettings({ "digest.rank_by_preference": "true" }).refused.reason).toBe("on_off");
  });

  it("refuses the whole save for one bad key, so nothing of it is written", () => {
    const answer = checkSettings({ "digest.style_prompt": "Short", "llm_provider.provider": "gemini" });

    expect(answer).toEqual({ refused: { reason: "operator_key", key: "llm_provider.provider" } });
  });

  it("takes the schedule apart from the rows, and refuses hours that are not one or two whole ones", () => {
    expect(checkSettings({ "general.digest_schedule": ["07:00", "19:00"], "general.timezone": "Asia/Taipei" })).toEqual({
      rows: [],
      schedule: { times: ["07:00", "19:00"], timeZone: "Asia/Taipei" },
    });
    expect(checkSettings({ "general.digest_schedule": ["07:00"] })).toEqual({ rows: [], schedule: { times: ["07:00"] } });
    for (const times of [[], ["07:00", "12:00", "19:00"], ["07:00", "07:00"], ["07:30", "19:00"], "07:00,19:00"]) {
      expect(checkSettings({ "general.digest_schedule": times }).refused.reason).toBe("schedule");
    }
  });

  it("refuses a body that is not an object of settings, and an empty one", () => {
    for (const body of [null, [], "x", 5]) expect(checkSettings(body).refused.reason).toBe("body");
    expect(checkSettings({}).refused.reason).toBe("empty");
  });
});

describe("a tenant's source save", () => {
  const own = [DOMAIN];

  it.each(CASES.sources.written)("writes $body.name as its row", ({ body, row }) => {
    expect(rssSource(body, own)).toEqual({ row });
  });

  it.each(CASES.sources.refused)("refuses $body.name as $reason", ({ body, reason }) => {
    expect(rssSource(body, own).refused.reason).toBe(reason);
  });

  it("takes a URL of the policy's length and no longer", () => {
    const base = "https://example.org/";
    const at = base + "a".repeat(POLICY.source_url.max_length - base.length);

    expect(urlRefusal(at)).toBeNull();
    expect(urlRefusal(`${at}a`)).toEqual({ reason: "url_length", max: String(POLICY.source_url.max_length) });
  });

  it("refuses this Worker's own host, whatever its name", () => {
    expect(urlRefusal("https://digest.example.com/hosted/spend", ["digest.example.com"])).toEqual({ reason: "url_service" });
    expect(urlRefusal("https://digest.example.com/feed")).toBeNull();
  });
});

describe("the catalog a tenant's settings are worded in", () => {
  it.each(CASES.languages)("is $catalog for $tag", ({ tag, catalog }) => {
    expect(catalogTag(tag, Object.keys(CATALOGS))).toBe(catalog);
  });

  it("is every catalog's message for each refusal", () => {
    for (const reason of REFUSAL_REASONS) {
      expect(EN).toHaveProperty(`hosted_settings.refused_${reason}`);
      expect(ZH_HANT).toHaveProperty(`hosted_settings.refused_${reason}`);
    }
  });
});

// TenantDb over real SQLite: a seeded tenant's object.
async function seeded(tenant = "alpha") {
  const { db } = tenantDb();
  await seedTenant({
    db,
    tenant,
    schemaSql: SCHEMA_SQL,
    hostedSchemaSql: HOSTED_SCHEMA_SQL,
    startingValues: STARTING_VALUES,
    seed: SEED,
    catalog: FEED_CATALOG,
    schedule: SEEDED_SCHEDULE,
  });
  return db;
}

const rss = (name) => ({ name, url: `https://${name.toLowerCase().replaceAll(" ", "-")}.example/feed`, type: "rss", config: { lane: "skim", tags: [] } });

describe("TenantDb's settings and sources", () => {
  it("refuses a key the policy does not let a tenant write, whoever calls it", async () => {
    const db = await seeded();
    const before = db.query("SELECT key, value FROM settings ORDER BY key").rows;

    const answer = db.saveSettings([
      ["digest.style_prompt", "Short"],
      ["llm_provider.provider", "gemini"],
    ]);

    expect(answer).toMatchObject({ refused: "operator_key", key: "llm_provider.provider" });
    expect(db.query("SELECT key, value FROM settings ORDER BY key").rows).toEqual(before);
  });

  it("writes each value as JSON, as the container's store reads one", async () => {
    const db = await seeded();

    db.saveSettings([
      ["digest.max_articles_per_digest", 120],
      ["digest.rank_by_preference", false],
    ]);

    expect(db.settings()).toMatchObject({ "digest.max_articles_per_digest": 120, "digest.rank_by_preference": false });
    expect(db.query("SELECT value FROM settings WHERE key = ?", ["digest.max_articles_per_digest"]).rows[0].value).toBe("120");
  });

  it("shows no key but the writable ones and the schedule's copy", async () => {
    const db = await seeded();

    expect(Object.keys(db.settings()).sort()).toEqual([...POLICY.writable_settings, "general.digest_schedule", "general.timezone"].sort());
  });

  it("lets two saves at once take the last place under the cap, and only one", async () => {
    const db = await seeded();
    const held = db.query("SELECT count(*) AS n FROM sources").rows[0].n;
    for (let i = held; i < POLICY.max_sources - 1; i++) db.saveSource(rss(`Feed ${i}`), POLICY.max_sources);

    const answers = await Promise.all([rss("Last A"), rss("Last B")].map(async (row) => db.saveSource(row, POLICY.max_sources)));

    expect(answers.filter((answer) => answer.source)).toHaveLength(1);
    expect(answers.filter((answer) => answer.refused === "source_cap")).toHaveLength(1);
    expect(db.query("SELECT count(*) AS n FROM sources").rows[0].n).toBe(POLICY.max_sources);
  });

  it("counts newsletter sources against the cap, and still rewrites a source it holds when full", async () => {
    const db = await seeded();
    db.query("INSERT INTO sources (name, url, type, config) VALUES (?, NULL, 'newsletter', ?)", ["Letter", '{"email_match":"from:a@b.example"}']);
    const held = db.query("SELECT count(*) AS n FROM sources").rows[0].n;
    for (let i = held; i < POLICY.max_sources; i++) db.saveSource(rss(`Feed ${i}`), POLICY.max_sources);

    expect(db.saveSource(rss("One more"), POLICY.max_sources).refused).toBe("source_cap");
    expect(db.saveSource({ ...rss("Feed 20"), config: { lane: "read", tags: [] } }, POLICY.max_sources).source).toBeTruthy();
    expect(db.saveSource(rss("Letter"), POLICY.max_sources).refused).toBe("source_taken");
  });

  it("keeps what else a rewritten source's config held, its tags too", async () => {
    const db = await seeded();
    db.query("INSERT INTO sources (name, url, type, config) VALUES (?, ?, 'rss', ?)", ["Kept", "https://kept.example/feed", '{"lane":"skim","tags":["x"],"language":"en"}']);

    db.saveSource(rssSource({ name: "Kept", url: "https://kept.example/new", lane: "read" }).row, POLICY.max_sources);

    expect(db.query("SELECT url, config FROM sources WHERE name = 'Kept'").rows[0]).toEqual({
      url: "https://kept.example/new",
      config: JSON.stringify({ lane: "read", tags: ["x"], language: "en" }),
    });
  });

  it("answers a tenant never seeded with no settings and no sources, and writes nothing", () => {
    const { db } = tenantDb();

    expect(db.settings()).toBeNull();
    expect(db.sourceList()).toBeNull();
    expect(db.saveSettings([["digest.style_prompt", "x"]]).refused).toBe("not_seeded");
    expect(db.saveSource(rss("A"), 30).refused).toBe("not_seeded");
  });
});
