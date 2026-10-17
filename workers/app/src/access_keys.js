// The Access team's signing keys and the issuer its JWTs name, from the one
// team-domain setting. No workerd-only imports: vitest runs this under Node.
import { createRemoteJWKSet, customFetch } from "jose";

// jose caches keys inside a key set, so one set per domain per isolate is what
// keeps the certs fetch from repeating on every request.
const memo = new Map();

export function accessKeys(rawTeamDomain, { fetchImpl } = {}) {
  const teamDomain = (rawTeamDomain ?? "").trim();
  if (!teamDomain) return null;
  if (!memo.has(teamDomain)) {
    const origin = "https://" + teamDomain;
    // A value that names no host has no keys to fetch: null makes the router refuse with its 403.
    let certs;
    try {
      certs = new URL("/cdn-cgi/access/certs", origin);
    } catch {
      return null;
    }
    const options = fetchImpl ? { [customFetch]: (url, init) => fetchImpl(url, init) } : {};
    memo.set(teamDomain, {
      keySet: createRemoteJWKSet(certs, options),
      issuer: origin,
    });
  }
  return memo.get(teamDomain);
}
