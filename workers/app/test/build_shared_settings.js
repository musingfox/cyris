// Vitest's global setup: the shared settings page built into the hosted Worker before any suite
// imports it, as wrangler.hosted.toml's `[build]` builds it before a deploy (ADR-0043). The file
// is git-ignored, so a run never reads one a template, script or catalog has since changed.
import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";

const ROOT = fileURLToPath(new URL("../../..", import.meta.url));

export default function setup() {
  execFileSync("uv", ["run", "--frozen", "python", "scripts/build_shared_settings.py"], { cwd: ROOT, stdio: "inherit" });
}
