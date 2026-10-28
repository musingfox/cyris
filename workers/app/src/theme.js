// The deployment's theme, read from D1 `settings` over REST the way the
// container reads it — the sibling of type_scale.js, and for the same reader
// reason (ADR-0013): a palette saved on /settings reaches the page being read,
// not the next issue. The default theme is no override at all, so a page whose
// deployment never picked one passes untouched. No workerd-only imports:
// vitest runs this under Node.
//
// These style texts are adapters/output/themes.py's own; tests/test_theme_setting.py
// keeps the two byte-equal, which is why this file looks written by a machine.

export const THEME_STYLES = {
  "light": "html:root{--bg:#fcfcfc;--bg-elev:#ffffff;--surface:#f3f4ef;--surface-2:#e9eae3;--border:#dcded4;--border-strong:#c6c9bb;--text:#191c14;--text-dim:#5c6050;--text-faint:#8f927f;--accent:#3f7a00;--accent-dim:#7ba636;--warn:#c7174c;--accent-tint:rgba(63, 122, 0,0.07);--warn-tint:rgba(199, 23, 76,0.12);--grid:rgba(63, 122, 0,0.035)}.btn.primary:where(:not(:disabled)):hover{background:#7ba636}.site-bar{background:rgba(252, 252, 252,.88)}.brand-mark{box-shadow:0 0 12px rgba(63, 122, 0,.45)}",
  "one_dark": "html:root{--bg:#282c34;--bg-elev:#2c313c;--surface:#2c313c;--surface-2:#31353f;--border:#3e4451;--border-strong:#4b5263;--text:#dcdfe4;--text-dim:#9da5b4;--text-faint:#5c6370;--accent:#98c379;--accent-dim:#6c9e4f;--warn:#e06c75;--accent-tint:rgba(152, 195, 121,0.07);--warn-tint:rgba(224, 108, 117,0.12);--grid:rgba(152, 195, 121,0.035)}.btn.primary:where(:not(:disabled)):hover{background:#6c9e4f}.site-bar{background:rgba(40, 44, 52,.88)}.brand-mark{box-shadow:0 0 12px rgba(152, 195, 121,.45)}",
  "catppuccin": "html:root{--bg:#1e1e2e;--bg-elev:#181825;--surface:#181825;--surface-2:#313244;--border:#45475a;--border-strong:#585b70;--text:#cdd6f4;--text-dim:#a6adc8;--text-faint:#6c7086;--accent:#a6e3a1;--accent-dim:#77c47f;--warn:#f38ba8;--accent-tint:rgba(166, 227, 161,0.07);--warn-tint:rgba(243, 139, 168,0.12);--grid:rgba(166, 227, 161,0.035)}.btn.primary:where(:not(:disabled)):hover{background:#77c47f}.site-bar{background:rgba(30, 30, 46,.88)}.brand-mark{box-shadow:0 0 12px rgba(166, 227, 161,.45)}",
  "nord": "html:root{--bg:#2e3440;--bg-elev:#333b4a;--surface:#3b4252;--surface-2:#434c5e;--border:#4c566a;--border-strong:#5e6a81;--text:#eceff4;--text-dim:#bcc2cd;--text-faint:#7b88a1;--accent:#a3be8c;--accent-dim:#76a05f;--warn:#bf616a;--accent-tint:rgba(163, 190, 140,0.07);--warn-tint:rgba(191, 97, 106,0.12);--grid:rgba(163, 190, 140,0.035)}.btn.primary:where(:not(:disabled)):hover{background:#76a05f}.site-bar{background:rgba(46, 52, 64,.88)}.brand-mark{box-shadow:0 0 12px rgba(163, 190, 140,.45)}",
  "tokyo_night": "html:root{--bg:#1a1b26;--bg-elev:#1f2335;--surface:#24283b;--surface-2:#2f334d;--border:#3b4261;--border-strong:#4f587e;--text:#c0caf5;--text-dim:#9aa5ce;--text-faint:#565f89;--accent:#9ece6a;--accent-dim:#73a846;--warn:#f7768e;--accent-tint:rgba(158, 206, 106,0.07);--warn-tint:rgba(247, 118, 142,0.12);--grid:rgba(158, 206, 106,0.035)}.btn.primary:where(:not(:disabled)):hover{background:#73a846}.site-bar{background:rgba(26, 27, 38,.88)}.brand-mark{box-shadow:0 0 12px rgba(158, 206, 106,.45)}",
  "github_dark": "html:root{--bg:#0d1117;--bg-elev:#161b22;--surface:#161b22;--surface-2:#21262d;--border:#30363d;--border-strong:#444c56;--text:#e6edf3;--text-dim:#9198a1;--text-faint:#6e7681;--accent:#3fb950;--accent-dim:#2ea043;--warn:#f85149;--accent-tint:rgba(63, 185, 80,0.07);--warn-tint:rgba(248, 81, 73,0.12);--grid:rgba(63, 185, 80,0.035)}.btn.primary:where(:not(:disabled)):hover{background:#2ea043}.site-bar{background:rgba(13, 17, 23,.88)}.brand-mark{box-shadow:0 0 12px rgba(63, 185, 80,.45)}"
};

const KEY = "digest.theme";
const FALLBACK = "default";
const ACCOUNTS_API = "https://api.cloudflare.com/client/v4/accounts";

export const themeStyle = (theme) => THEME_STYLES[theme] || "";

export function createTheme({ env, fetchImpl, now = Date.now, ttlMs = 60000, timeoutMs = 1000 }) {
  async function read() {
    const account = env.CLOUDFLARE_ACCOUNT_ID;
    const database = env.CYRIS_STORE_DATABASE_ID;
    const token = env.CLOUDFLARE_API_TOKEN;
    if (env.CYRIS_STORE_BACKEND === "json" || !account || !database || !token) return FALLBACK;
    try {
      const response = await fetchImpl(`${ACCOUNTS_API}/${account}/d1/database/${database}/query`, {
        method: "POST",
        headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
        body: JSON.stringify({ sql: "SELECT value FROM settings WHERE key = ?", params: [KEY] }),
        signal: AbortSignal.timeout(timeoutMs),
      });
      // The status only: an error body or message may echo the request.
      if (!response.ok) {
        console.warn("theme read failed:", response.status);
        return FALLBACK;
      }
      const data = await response.json();
      const row = data.success && data.result?.[0]?.results?.[0];
      if (!row) return FALLBACK;
      const value = JSON.parse(row.value);
      return THEME_STYLES[value] ? value : FALLBACK;
    } catch {
      console.warn("theme read failed");
      return FALLBACK;
    }
  }

  // The read in flight or settled, and when it goes stale. forget() drops it,
  // so a read already in flight cannot put the old value back.
  let held = null;
  return {
    current() {
      if (!held || now() >= held.expiresAt) {
        held = { value: read(), expiresAt: now() + ttlMs };
      }
      return held.value;
    },
    forget() {
      held = null;
    },
  };
}
