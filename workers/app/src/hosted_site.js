// How the hosted Worker answers a tenant's page: which tenant's Durable Object it is read from,
// that tenant's own type size, and the style injected into the page. Kept out of hosted.js, which
// imports `cloudflare:workers` and the schema files only workerd can load, so a test can build
// the real factory. No workerd-only imports at module top: vitest runs this under Node, and
// HTMLRewriter is a global read when a page is rewritten.

import { serveTenantPage } from "./hosted_pages.js";
import { tenantDb, tenantRows } from "./tenant_db.js";
import { createTypeScale } from "./type_scale.js";

// As index.js's: the style goes at the end of <head>.
export const injectStyle = (response, html) =>
  new globalThis.HTMLRewriter()
    .on("head", {
      element(head) {
        head.append(html, { html: true });
      },
    })
    .transform(response);

// `page(request, tenant, rest)` for `handleHostedRequest`. Each tenant's type size is read from
// its own object and held in one memo per factory call and tenant, as index.js holds one for the
// deployment (ADR-0013); hosted.js calls this once at module scope, so that is one per isolate.
export function createSitePage(env) {
  const typeScales = new Map();
  const typeScaleOf = (tenant) => {
    if (!typeScales.has(tenant)) typeScales.set(tenant, createTypeScale({ env, read: tenantRows(env, tenant) }));
    return typeScales.get(tenant);
  };
  return (request, tenant, rest) =>
    serveTenantPage(request, {
      tenant,
      rest,
      db: tenantDb(env, tenant),
      deps: { typeScale: typeScaleOf(tenant), injectStyle },
    });
}
