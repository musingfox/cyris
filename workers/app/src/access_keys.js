// The Access team's signing keys and the issuer its JWTs name, from the one
// team-domain setting. No workerd-only imports: vitest runs this under Node.
import { createRemoteJWKSet, customFetch } from "jose";

// jose caches keys inside a key set, so one set per domain per isolate is what
// keeps the certs fetch from repeating on every request.
const memo = new Map();

export function accessKeys(teamDomain, { fetchImpl } = {}) {
  if (!teamDomain) return null;
  if (!memo.has(teamDomain)) {
    const origin = "https://" + teamDomain;
    const options = fetchImpl ? { [customFetch]: (url, init) => fetchImpl(url, init) } : {};
    memo.set(teamDomain, {
      keySet: createRemoteJWKSet(new URL("/cdn-cgi/access/certs", origin), options),
      issuer: origin,
    });
  }
  return memo.get(teamDomain);
}
