// A hosted tenant's site: the pages its runs stored in its own Durable Object, served under
// /t/<tenant>/ to a signed-in reader. The run renders them in its container and writes them into
// the tenant's `hosted_pages` table (src/cyris/adapters/output/hosted_pages.py); this module only
// reads them back. docs/decisions/0029-a-hosted-tenants-pages-live-in-its-durable-object.md.
// No workerd-only imports: vitest runs this under Node.
//
//   /t/<tenant>/                   the tenant's latest issue
//   /t/<tenant>/<date>-<period>    one issue, and its raw page at <date>-<period>-raw,
//                                  as a Pages project serves them; `.html` is taken too
//   /t/<tenant>/index.html         the archive, and any asset the site holds (favicon.svg)

import { sized } from "./router.js";

// The tenant's site on the hosted Worker. `SITE_PATH` in src/cyris/adapters/output/hosted_pages.py
// builds the run's links in the same shape; tests/test_hosted_config.py holds the two together.
export const SITE_ROUTE = /^\/t\/([^/]+)(\/.*)?$/;

// One file of the flat site, as the run stored it: no directories, no dot segments.
const PAGE_NAME = /^[A-Za-z0-9][A-Za-z0-9._-]*$/;

const PAGE_SQL = "SELECT body, content_type FROM hosted_pages WHERE path = ?";

// The latest issue is the one the archive leads with: the most recently saved issue in
// `digests` (D1DigestStore.issues) whose digest page the site holds. An issue whose pages failed
// to store is skipped, so the reader gets the newest page there is.
const LATEST_SQL =
  "SELECT p.path, p.body, p.content_type FROM digests d " +
  "JOIN hosted_pages p ON p.path = '/' || d.date || '-' || d.period || '.html' " +
  "ORDER BY d.saved_at DESC, d.rowid DESC LIMIT 1";

// The page /t/<tenant>/ serves, as its row: path, body and content_type; undefined for none.
export const latestPage = async (db) => (await db.query(LATEST_SQL, [])).rows[0];

// A page's SHA-256 as hex, over its UTF-8 bytes: what the runbook's check compares the page it
// fetched with (scripts/hosted_check.py).
export async function sha256Hex(text) {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return [...new Uint8Array(digest)].map((byte) => byte.toString(16).padStart(2, "0")).join("");
}

const json = (body, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

// The stored paths a request may name, in the order tried: Pages serves `<name>.html` at the
// clean URL `<name>`, and an asset or `.html` name at itself.
function storedPaths(name) {
  if (name === "") return null;
  if (!PAGE_NAME.test(name)) return [];
  return name.includes(".") ? [`/${name}`] : [`/${name}.html`];
}

// Answers a request under one tenant's site. `rest` is the path after /t/<tenant>, the caller
// has checked the sign-in and the tenant, and `db` is that tenant's object alone, so no path
// here can reach another tenant's pages. `deps`: typeScale (type_scale.js, this tenant's own)
// and injectStyle, as router.js takes them.
export async function serveTenantPage(request, { tenant, rest, db, deps }) {
  if (request.method !== "GET" && request.method !== "HEAD") {
    return json({ error: "a page takes GET or HEAD" }, 405);
  }
  // Without the slash, the page's relative links would resolve outside the tenant's site.
  if (rest === "") return new Response(null, { status: 301, headers: { Location: `/t/${tenant}/` } });

  const name = rest.slice(1);
  const paths = storedPaths(name);
  let row;
  try {
    if (paths === null) {
      row = await latestPage(db);
    } else {
      for (const path of paths) {
        row = (await db.query(PAGE_SQL, [path])).rows[0];
        if (row) break;
      }
    }
  } catch (error) {
    const message = String(error?.message ?? error);
    // A tenant not seeded yet has neither table; that is a site with no page, not a failure.
    if (/no such table/.test(message)) row = undefined;
    else {
      console.warn(`page read failed for tenant ${tenant}:`, message);
      return json({ error: `could not read ${tenant}'s pages; try again` }, 503);
    }
  }
  if (!row) {
    return json({ error: paths === null ? `${tenant} has no issue yet` : `${tenant} has no page ${name}` }, 404);
  }

  const headers = {
    "Content-Type": row.content_type,
    // Behind a sign-in, so no shared cache may keep it; no validators, so the type size
    // (ADR-0013) never meets a browser revalidating an older size.
    "Cache-Control": "private, no-cache",
  };
  const page = new Response(request.method === "HEAD" ? null : row.body, { status: 200, headers });
  return sized(page, await deps.typeScale.current(), deps);
}
