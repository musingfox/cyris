import { fileURLToPath } from "node:url";
import { defineConfig } from "vitest/config";

// wrangler bundles a `.sql` import as its text (a Text module rule it applies by default), so
// hosted.js imports schema.sql that way; this does the same under vitest, so a test can import
// the hosted Worker's entry itself.
const sqlAsText = {
  name: "sql-as-text",
  transform(code, id) {
    if (id.endsWith(".sql")) return { code: `export default ${JSON.stringify(code)};`, map: null };
  },
};

export default defineConfig({
  plugins: [sqlAsText],
  resolve: {
    alias: {
      "cloudflare:workers": fileURLToPath(new URL("workers/app/test/cloudflare_workers_stub.js", import.meta.url)),
    },
  },
  test: {
    environment: "node",
    // The Worker's config lives at the repo root (see wrangler.toml), so this
    // does too — but its tests do not. Naming them keeps the other three
    // Workers' suites, which have their own package.json, out of this run.
    include: ["workers/app/test/**/*.test.js"],
  },
});
