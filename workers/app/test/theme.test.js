import { afterEach, describe, it, expect, vi } from "vitest";
import { THEME_STYLES, createTheme, themeStyle } from "../src/theme.js";

const TOKEN = "tok-quiet";
const ENV = {
  CLOUDFLARE_ACCOUNT_ID: "acct-1",
  CYRIS_STORE_DATABASE_ID: "db-1",
  CYRIS_STORE_BACKEND: "sqlite",
  CLOUDFLARE_API_TOKEN: TOKEN,
};
const rows = (value) => ({
  success: true,
  result: [{ results: value === undefined ? [] : [{ value }], success: true }],
});

// A D1 REST API answering `answer()` to every query, recording each call.
function d1(answer) {
  const fetchImpl = async (url, init) => {
    fetchImpl.calls.push({ url, init });
    return answer(url, init);
  };
  fetchImpl.calls = [];
  return fetchImpl;
}

const answering = (value) => d1(() => Response.json(rows(value)));

function captureConsole() {
  const logged = [];
  for (const method of ["log", "info", "warn", "error", "debug"]) {
    vi.spyOn(console, method).mockImplementation((...args) => {
      const text = (arg) =>
        arg instanceof Error ? `${arg.name}: ${arg.message}` : (JSON.stringify(arg) ?? String(arg));
      logged.push(...args.map(text));
    });
  }
  return logged;
}

afterEach(() => vi.restoreAllMocks());

describe("WorkerReadsTheme", () => {
  it("reads digest.theme from D1 over REST", async () => {
    const fetchImpl = answering(JSON.stringify("nord"));
    const theme = createTheme({ env: ENV, fetchImpl });

    expect(await theme.current()).toBe("nord");

    expect(fetchImpl.calls).toHaveLength(1);
    const [{ url, init }] = fetchImpl.calls;
    expect(url).toBe("https://api.cloudflare.com/client/v4/accounts/acct-1/d1/database/db-1/query");
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body)).toEqual({
      sql: "SELECT value FROM settings WHERE key = ?",
      params: ["digest.theme"],
    });
  });

  it("a json backend, an unreadable answer and a missing value all stay default", async () => {
    const jsonHome = { ...ENV, CYRIS_STORE_BACKEND: "json" };
    expect(await createTheme({ env: jsonHome, fetchImpl: answering('"nord"') }).current()).toBe(
      "default",
    );

    const failing = d1(() => new Response("nope", { status: 500 }));
    captureConsole();
    expect(await createTheme({ env: ENV, fetchImpl: failing }).current()).toBe("default");
    expect(await createTheme({ env: ENV, fetchImpl: answering("undefined") }).current()).toBe(
      "default",
    );
  });

  it("styles a page only for a named palette", () => {
    expect(themeStyle("default")).toBe("");
    expect(themeStyle("no-such-theme")).toBe("");
    // Every style is the same shape the python side renders, and the tokens
    // partial declares: :root vars plus the three literals outside them.
    for (const [name, style] of Object.entries(THEME_STYLES)) {
      expect(name).not.toBe("default");
      expect(style).toMatch(/^html:root\{--bg:#/);
      expect(style).toContain(".btn.primary:where(:not(:disabled)):hover");
      expect(style).toContain(".site-bar{background:rgba(");
      expect(style).toContain(".brand-mark{box-shadow:");
    }
  });
});
