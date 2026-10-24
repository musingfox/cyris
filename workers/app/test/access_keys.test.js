import { describe, it, expect } from "vitest";
import { generateKeyPair, exportJWK } from "jose";
import { accessKeys } from "../src/access_keys.js";

// Each test names its own team domain: accessKeys memoizes per domain for the
// life of the module, so a domain reused across tests would keep the first fetch.
const certsStub = async (kid) => {
  const { publicKey } = await generateKeyPair("RS256", { extractable: true });
  const jwk = { ...(await exportJWK(publicKey)), kid, alg: "RS256", use: "sig" };
  const calls = [];
  const fetchImpl = async (url, init) => {
    calls.push({ url: String(url), init });
    return new Response(JSON.stringify({ keys: [jwk] }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  };
  return { fetchImpl, calls };
};

describe("AccessKeySet", () => {
  it("names the issuer as the team domain with https", () => {
    const keys = accessKeys("issuer-case.cloudflareaccess.com");
    expect(keys.issuer).toBe("https://issuer-case.cloudflareaccess.com");
  });

  it("trims the team domain before it names the issuer", () => {
    const keys = accessKeys("  trim-case.cloudflareaccess.com ");
    expect(keys.issuer).toBe("https://trim-case.cloudflareaccess.com");
  });

  it("returns null for a team domain that names no host, rather than throwing", () => {
    expect(accessKeys("inner-case .cloudflareaccess.com")).toBeNull();
  });

  it("fetches the certs URL once and resolves the key id from it", async () => {
    const { fetchImpl, calls } = await certsStub("k1");
    const { keySet } = accessKeys("fetch-case.cloudflareaccess.com", { fetchImpl });
    const key = await keySet({ alg: "RS256", kid: "k1" });
    expect(key).toBeTruthy();
    expect(calls).toHaveLength(1);
    expect(calls[0].url).toBe("https://fetch-case.cloudflareaccess.com/cdn-cgi/access/certs");
  });

  it("returns null and makes no fetch for an unset or empty domain", () => {
    const calls = [];
    const fetchImpl = async (url) => {
      calls.push(url);
      return new Response("x");
    };
    expect(accessKeys(undefined, { fetchImpl })).toBeNull();
    expect(accessKeys("", { fetchImpl })).toBeNull();
    expect(calls).toHaveLength(0);
  });

  it("reuses the same object for a domain and a different one for another", () => {
    const first = accessKeys("memo-a.cloudflareaccess.com");
    const again = accessKeys("memo-a.cloudflareaccess.com");
    const other = accessKeys("memo-b.cloudflareaccess.com");
    expect(again).toBe(first);
    expect(other).not.toBe(first);
  });

  it("rejects the key lookup with an ordinary rejection when the fetch throws", async () => {
    const fetchImpl = async () => {
      throw new Error("network down");
    };
    const { keySet } = accessKeys("throw-case.cloudflareaccess.com", { fetchImpl });
    await expect(keySet({ alg: "RS256", kid: "k1" })).rejects.toThrow();
  });
});
