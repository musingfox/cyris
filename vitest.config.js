import { fileURLToPath } from "node:url";
import { defineConfig } from "vitest/config";

export default defineConfig({
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
