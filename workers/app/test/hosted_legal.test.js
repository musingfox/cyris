import { afterEach, beforeEach, describe, it, expect } from "vitest";
import { createHash } from "node:crypto";
import { env } from "cloudflare:workers";
import EN from "../../../src/cyris/locales/en.json" with { type: "json" };
import ZH_HANT from "../../../src/cyris/locales/zh-Hant.json" with { type: "json" };
import LEGAL from "../src/hosted_legal.json" with { type: "json" };
import POLICY from "../src/hosted_settings_policy.json" with { type: "json" };
import worker from "../src/hosted.js";
import { PRIVATE_HEADERS } from "../src/hosted_pages.js";
import { SESSION_COOKIE, SESSION_TTL_MS } from "../src/hosted_session.js";
import { namespace, registryNamespace } from "./sql_storage.js";

// The hosted Worker's entry, hosted.js, over a real registry: the pages must answer before any
// sign-in, so no session, Access JWT or mail binding is given.
const DOMAIN = "cyris-hosted.example.workers.dev";
const ORIGIN = `https://${DOMAIN}`;
const PAGES = { "/privacy": "privacy.", "/terms": "terms." };

let bound;

beforeEach(() => {
  bound = { REGISTRY: registryNamespace(), TENANT_DB: namespace() };
  Object.assign(env, bound, { CYRIS_STORE_HOSTED_DOMAIN: DOMAIN });
});

afterEach(() => {
  for (const key of Object.keys(env)) delete env[key];
});

const call = (path, { method = "GET", headers = {} } = {}) =>
  worker.fetch(new Request(`${ORIGIN}${path}`, { method, headers }), env, { waitUntil: () => {} });

const escape = (text) =>
  String(text).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

// The text between a message's placeholders, as the page escapes it: what a page that shows the
// message must contain, whatever the placeholders were given.
const fragments = (message) =>
  message
    .split(/\{\$\w+\}/)
    .map((part) => part.trim())
    .filter(Boolean)
    .map(escape);

// One language's block of a page: its <article lang>.
const block = (html, tag) => new RegExp(`<article class="doc" id="${tag}" lang="${tag}">(.*?)</article>`, "s").exec(html)?.[1];

describe("the privacy policy and terms", () => {
  it.each(Object.keys(PAGES))("answer GET %s with HTML to a request with no session", async (path) => {
    const response = await call(path);

    expect(response.status).toBe(200);
    expect(response.headers.get("Content-Type")).toBe("text/html; charset=utf-8");
    expect(response.headers.get("Set-Cookie")).toBeNull();
    expect(await response.text()).toMatch(/^<!doctype html><html lang="en">/);
  });

  it.each(Object.keys(PAGES))("answer %s to a stale session as to none, reading no session", async (path) => {
    const response = await call(path, { headers: { Cookie: `${SESSION_COOKIE}=${"x".repeat(43)}` } });

    expect(response.status).toBe(200);
    // A /t/ request with this cookie would clear it; the legal pages never look at it.
    expect(response.headers.get("Set-Cookie")).toBeNull();
  });

  it.each(Object.keys(PAGES))("carry %s the sign-in pages' private headers, uncached", async (path) => {
    const [page, signIn] = [await call(path), await call("/login")];

    for (const [name, value] of Object.entries(PRIVATE_HEADERS)) {
      expect(page.headers.get(name)).toBe(value);
      expect(page.headers.get(name)).toBe(signIn.headers.get(name));
    }
    expect(page.headers.get("Cache-Control")).toBe("no-store");
  });

  it.each(Object.keys(PAGES))("serve %s under a policy that runs no script and allows only its own style", async (path) => {
    const response = await call(path);
    const html = await response.text();
    const policy = response.headers.get("Content-Security-Policy");
    const style = /<style>(.*?)<\/style>/s.exec(html)[1];
    const hash = createHash("sha256").update(style).digest("base64");

    expect(policy.split("; ")).toEqual([
      "default-src 'none'",
      `style-src 'sha256-${hash}'`,
      "form-action 'none'",
      "base-uri 'none'",
      "frame-ancestors 'none'",
    ]);
    expect(html).not.toMatch(/<script|<link|<img|<form|\sstyle="/);
    expect(html).not.toMatch(/https?:\/\//);
  });

  it.each(Object.keys(PAGES))("answer HEAD %s with the page's headers and no body", async (path) => {
    const response = await call(path, { method: "HEAD" });

    expect(response.status).toBe(200);
    expect(response.headers.get("Content-Security-Policy")).toMatch(/^default-src 'none'/);
    expect(await response.text()).toBe("");
  });

  it.each(["POST", "PUT", "DELETE", "PATCH"])("refuse %s on both pages", async (method) => {
    for (const path of Object.keys(PAGES)) {
      const response = await call(path, { method });

      expect(response.status).toBe(405);
      expect(response.headers.get("Allow")).toBe("GET, HEAD");
      expect(response.headers.get("X-Robots-Tag")).toBe(PRIVATE_HEADERS["X-Robots-Tag"]);
    }
  });
});

describe("the two languages of a legal page", () => {
  it.each(Object.entries(PAGES))("show %s in Traditional Chinese first, then in English", async (path, prefix) => {
    const html = await (await call(path)).text();
    const [chinese, english] = [block(html, "zh-Hant"), block(html, "en")];
    // The description is the head's, in the page's English chrome (below); every other key is a block's.
    const keys = Object.keys(EN).filter((key) => key.startsWith(prefix) && key !== `${prefix}description`);

    expect(keys.length).toBeGreaterThan(5);
    expect(html.indexOf('lang="zh-Hant">')).toBeLessThan(html.indexOf('<article class="doc" id="en"'));
    for (const key of keys) {
      expect(ZH_HANT[key]?.source, key).toBe(EN[key]);
      for (const part of fragments(ZH_HANT[key].text)) expect(chinese, key).toContain(part);
      for (const part of fragments(EN[key])) expect(english, key).toContain(part);
    }
  });

  it.each(Object.keys(PAGES))("say on %s, in both languages, that the Chinese text prevails", async (path) => {
    const html = await (await call(path)).text();
    const notice = /<div class="notice">(.*?)<\/div>/s.exec(html)[1];

    expect(notice).toContain(`<p lang="zh-Hant">${escape(ZH_HANT["legal.prevails"].text)}</p>`);
    expect(notice).toContain(`<p lang="en">${escape(EN["legal.prevails"])}</p>`);
    expect(html.indexOf(notice)).toBeLessThan(html.indexOf("<article"));
  });

  it.each(Object.entries(PAGES))("title %s with the authoritative heading, its translation beside it", async (path, prefix) => {
    const html = await (await call(path)).text();
    const [original, translation] = [ZH_HANT[`${prefix}title`].text, EN[`${prefix}title`]];

    expect(html).toContain(`<h1 class="display" lang="zh-Hant">${original}</h1>`);
    expect(html).toContain(`<p class="heading" lang="en">${translation}</p>`);
    expect(html).toContain(`<title>${original} · ${translation}</title>`);
    expect(html).toContain(`<meta name="description" content="${escape(EN[`${prefix}description`])}">`);
  });
});

describe("what the privacy policy states", () => {
  it("names the session cookie and its lifetime as the Worker sets them", async () => {
    const html = await (await call("/privacy")).text();
    const days = SESSION_TTL_MS / (24 * 60 * 60 * 1000);

    for (const tag of ["zh-Hant", "en"]) {
      expect(block(html, tag)).toContain(SESSION_COOKIE);
      expect(block(html, tag)).toMatch(new RegExp(`\\b${days} (天|days)`));
    }
  });

  it("names the model the hosted runs are pinned to, and the backup's retention", async () => {
    const english = block(await (await call("/privacy")).text(), "en");

    expect(english).toContain(`the model ${POLICY.model}.`);
    expect(english).toContain(`set to expire ${LEGAL.backup_days} days after it is uploaded`);
    expect(english).not.toMatch(/backblaze|\bB2\b/i);
  });

  it("gives a deleted row's longest stay in the backups as the export's lifetime and R2's removal", async () => {
    const html = await (await call("/privacy")).text();
    // R2 removes an expired object typically within 24 hours, so "within 30 days" was a day short.
    const most = LEGAL.backup_days + Math.ceil(LEGAL.backup_removal_hours / 24);

    expect(most).toBeGreaterThan(LEGAL.backup_days);
    expect(block(html, "en")).toContain(`within ${LEGAL.backup_removal_hours} hours of that`);
    expect(block(html, "en")).toContain(`for up to about ${most} days`);
    expect(block(html, "zh-Hant")).toContain(`保留約 ${most} 天`);
    expect(html).not.toMatch(/leaves the backups within|從備份中消失/);
  });

  it("states how long a deletion takes, and how long Cloudflare keeps the logs", async () => {
    const html = await (await call("/privacy")).text();
    const [chinese, english] = [block(html, "zh-Hant"), block(html, "en")];

    expect(english).toContain(`we finish within ${LEGAL.deletion_days} days of your request`);
    expect(chinese).toContain(`收到請求後 ${LEGAL.deletion_days} 天內完成`);
    expect(english).toContain(`for up to ${LEGAL.log_days} days`);
    expect(chinese).toContain(`最長 ${LEGAL.log_days} 天`);
  });

  it("deletes on a request, on the end of access, and after a reminded spell without a sign-in", async () => {
    const html = await (await call("/privacy")).text();
    const [chinese, english] = [block(html, "zh-Hant"), block(html, "en")];
    const { deletion_days: days, inactive_months: months, inactive_reminder_days: reminder } = LEGAL;

    expect(english).toContain(`We delete it within ${days} days after you ask us to (see “Your rights”) or after we end your access.`);
    expect(english).toContain(`once you have gone ${months} months without signing in: ${reminder} days before that`);
    expect(english).toContain("signing in once starts the count again");
    // Until the registry records a last sign-in, the months cannot start from one (hosted-member-last-sign-in).
    expect(english).toContain("The service does not yet record the date you last signed in");
    expect(chinese).toContain(`我們終止您的使用權限時，我們會在其後 ${days} 天內刪除`);
    expect(chinese).toContain(`您連續 ${months} 個月沒有登入時，我們也會刪除：屆滿前 ${reminder} 天`);
    expect(chinese).toContain("本服務目前還不記錄您最後一次登入的日期");
    expect(english).toContain(`If the beta is to end, we tell you at least ${LEGAL.end_notice_days} days before`);
  });

  it("names what a deletion leaves behind", async () => {
    const english = block(await (await call("/privacy")).text(), "en");

    for (const left of ["the daily backups", "Cloudflare's logs", "what Anthropic received", "the run records, AI spend and schedule"]) {
      expect(english).toContain(escape(left));
    }
  });

  it("answers a request within the periods of the Act's Article 13", async () => {
    const html = await (await call("/privacy")).text();
    const [chinese, english] = [block(html, "zh-Hant"), block(html, "en")];
    const { see, change } = LEGAL.answer_days;

    expect(english).toContain(`within ${see} days, and a request to complete, correct or stop within ${change} days`);
    expect(english).toContain(`at most ${see} and ${change} more days`);
    expect(chinese).toContain(`會在 ${see} 天內答覆`);
    expect(chinese).toContain(`會在 ${change} 天內答覆`);
  });

  it("promises a mail to the reader once it learns of a security incident", async () => {
    const html = await (await call("/privacy")).text();

    // 個資法第 12 條 runs from 知悉 (learning of it), not from an investigation's end.
    expect(block(html, "en")).toContain("When we learn that your personal data has been stolen");
    expect(block(html, "zh-Hant")).toContain("我們知悉您的個人資料被竊取");
  });

  it.each(Object.keys(PAGES))("link every contact address on %s as mail, in both languages", async (path) => {
    const html = await (await call(path)).text();

    for (const tag of ["zh-Hant", "en"]) {
      for (const address of Object.values(LEGAL.contacts)) {
        expect(block(html, tag)).toContain(`<a href="mailto:${address}">${address}</a>`);
      }
    }
  });
});

describe("one reader to a digest", () => {
  it.each(Object.keys(PAGES))("is all %s assumes: no shared digest, no deletion only for a sole reader", async (path) => {
    const html = await (await call(path)).text();

    expect(html).not.toMatch(/more than one (invited )?reader|only reader|several readers|which reader/i);
    expect(html).not.toMatch(/不只一位|唯一的讀者|多位讀者|哪一位讀者/);
  });

  it("says so on the privacy policy, in both languages", async () => {
    const html = await (await call("/privacy")).text();

    expect(block(html, "en")).toContain("Each digest has one reader.");
    expect(block(html, "zh-Hant")).toContain("每份精選摘要只有一位讀者。");
  });
});

describe("changes and disputes", () => {
  it.each(Object.entries(PAGES))("mails the reader before a change to %s takes effect", async (path) => {
    const html = await (await call(path)).text();
    const days = LEGAL.notice_days;

    expect(block(html, "en")).toMatch(new RegExp(`write to the address you sign in with at least ${days} days before`));
    expect(block(html, "zh-Hant")).toContain(`生效至少 ${days} 天前`);
  });

  it("ends the beta only after a mailed notice, unless an urgent case stops it", async () => {
    const html = await (await call("/terms")).text();
    const [chinese, english] = [block(html, "zh-Hant"), block(html, "en")];
    const days = LEGAL.end_notice_days;

    expect(english).toContain(`We end the beta itself only after writing to the address you sign in with at least ${days} days before`);
    expect(english).toContain("Only an urgent case, such as a security incident or a requirement of the law");
    expect(english).toContain("we then write to you afterwards");
    expect(chinese).toContain(`本測試版本身的結束，我們會在至少 ${days} 天前寄信`);
    expect(chinese).toContain("並在事後寄信通知您");
    expect(html).not.toMatch(/without notice|不需事先通知/);
  });

  it("mails a reader whose access it ends, who may still ask for a copy", async () => {
    const html = await (await call("/terms")).text();

    expect(block(html, "en")).toContain("When we do, we write to tell you, and until your data is deleted you can still ask for a copy of it.");
    expect(block(html, "zh-Hant")).toContain("終止時我們會寄信通知您，資料刪除之前，您仍可要求取得複本。");
  });

  it("names no court of its own in the terms", async () => {
    const html = await (await call("/terms")).text();

    expect(html).toContain("the court is the one the laws of the Republic of China (Taiwan) name");
    expect(html).not.toMatch(/District Court|地方法院/);
  });

  it("mails ahead only a change that affects the reader, in both pages", async () => {
    const [privacy, terms] = [await (await call("/privacy")).text(), await (await call("/terms")).text()];
    const days = LEGAL.notice_days;

    expect(block(privacy, "en")).toContain(`If a change affects your rights or how your data is used, we also write to the address you sign in with at least ${days} days`);
    expect(block(privacy, "zh-Hant")).toContain("變更若影響您的權利或您資料的使用方式");
    expect(block(terms, "en")).toContain(`If a change affects your rights or obligations, we write to the address you sign in with at least ${days} days`);
    expect(block(terms, "zh-Hant")).toContain("變更若影響您的權利或義務");
  });

  it("lets the operator look at a digest when the law requires it", async () => {
    const html = await (await call("/privacy")).text();

    expect(block(html, "en")).toContain("to deal with abuse, or when the law requires it.");
    expect(block(html, "zh-Hant")).toContain("處理濫用行為，或依法律要求，才");
  });

  // A digest page contacts Google Fonts and publishers' image hosts (privacy.recipient_browser), so
  // the cookie section speaks only for the service and points there instead of denying any tracker.
  it("speaks for the service's own tools and points to the sites a digest page contacts", async () => {
    const html = await (await call("/privacy")).text();

    expect(block(html, "en")).toContain("uses no analytics, advertising or tracking tools. The other sites a digest page contacts");
    expect(block(html, "zh-Hant")).toContain("也不使用分析、廣告或追蹤工具。精選摘要頁面會連線到的其他網站");
    expect(html).not.toMatch(/no third-party trackers|沒有第三方追蹤工具/);
  });
});

describe("the way to the legal pages", () => {
  it.each(["/login", "/login/confirm", "/logout"])("is on the sign-in page %s", async (path) => {
    const html = await (await call(path)).text();

    expect(html).toContain('<a href="/privacy">');
    expect(html).toContain('<a href="/terms">');
  });

  // 消保法第 13 條 asks that the terms be made plain where they are agreed to: the confirm form is
  // the step that signs a reader in.
  it("says beside the confirm button that signing in agrees to the terms", async () => {
    const html = await (await call("/login/confirm")).text();

    expect(html).toMatch(/<button type="submit">[^<]*<\/button><p>By signing in, you agree to the Terms and have read the Privacy policy\.<\/p><\/form>/);
  });

  it.each(Object.keys(PAGES))("leads from %s to the other page and to the sign-in", async (path) => {
    const html = await (await call(path)).text();

    expect(html).toContain(`<a class="label" href="${path}" aria-current="page">`);
    for (const other of [...Object.keys(PAGES), "/login"].filter((href) => href !== path)) {
      expect(html).toContain(`<a class="label" href="${other}">`);
    }
  });
});
