const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[<>&"]/g, (c) => `&#${c.charCodeAt(0)};`);
let state = null;

const TABS = ["model", "digest", "notifications", "sources"];

// The category lives in the hash, not the path: the Worker guards /settings by
// exact match, and a hash never leaves the browser.
function route() {
  const hash = location.hash.slice(1);
  const tab = TABS.includes(hash) ? hash : "model";
  document.querySelectorAll(".tab").forEach((panel) => {
    panel.hidden = panel.dataset.tab !== tab;
  });
  document.querySelectorAll(".settings-nav a").forEach((link) => {
    if (link.dataset.tab === tab) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  });
  window.scrollTo(0, 0);
  const active = document.querySelector(".settings-nav a[aria-current]");
  active.parentElement.scrollLeft = active.offsetLeft - 16;
}

addEventListener("hashchange", route);
route();

// A category's Save is live only while its form differs from what was last
// loaded or saved, and the category list marks that form with a dot.
const clean = new Map();
const snapshot = (form) => [...form.querySelectorAll("input, select")]
  .map((input) => input.type === "radio" ? input.checked : input.value)
  .join("|");
const saveButton = (form) => form.querySelector('button[type="submit"]');
const navLink = (form) => document.querySelector(`.settings-nav a[data-tab="${form.dataset.tab}"]`);

function refresh(form) {
  const dirty = clean.has(form) && snapshot(form) !== clean.get(form);
  saveButton(form).disabled = !dirty;
  navLink(form).classList.toggle("dirty", dirty);
}

function markClean(form) {
  clean.set(form, snapshot(form));
  refresh(form);
}

document.querySelectorAll("form.tab").forEach((form) => {
  form.addEventListener("input", () => refresh(form));
  form.addEventListener("change", () => refresh(form));
});

function render() {
  $("providers").innerHTML = state.providers.map((p) => `
    <label class="choice${p.configured ? "" : " unavailable"}">
      <input type="radio" name="provider" value="${esc(p.name)}"
             ${p.name === state.provider ? "checked" : ""}
             ${p.configured ? "" : "disabled"}>
      <span class="name">${esc(p.name)}</span>
      ${p.configured
        ? `<span class="label">Key ready</span>`
        : `<span class="label key-missing">${esc(p.env_var)} missing</span>`}
    </label>`).join("");
  $("model-input").value = state.model;
  updateHint();
  $("providers").addEventListener("change", updateHint);
  const [m, e] = state.schedule || [];
  if (m) $("morning").value = parseInt(m, 10);
  if (e) $("evening").value = parseInt(e, 10);
  if (state.max_featured) $("max-featured").value = state.max_featured;
  if (state.notify_webhook) $("discord-webhook").value = state.notify_webhook;
  if (state.writable) {
    document.querySelectorAll("form.tab").forEach(markClean);
  } else {
    show("err", "This deployment has no settings store, so nothing can be saved here. Edit cyris.toml instead.");
  }
}

function chosen() {
  const el = document.querySelector('input[name=provider]:checked');
  return el && state.providers.find((p) => p.name === el.value);
}

function updateHint() {
  const p = chosen();
  $("model-input").placeholder = p ? `Empty uses ${p.default_model}` : "";
  $("model-hint").textContent = p
    ? `Saving checks the model against ${p.name} with a real call first — a typo is rejected here rather than at 08:00 tomorrow.`
    : "Pick a provider whose key is present.";
}

function show(kind, text, id = "result") {
  const el = $(id);
  el.className = kind === "err" ? "notice err" : "notice";
  el.textContent = text;
  el.hidden = false;
}

$("form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const p = chosen();
  if (!p) return show("err", "Pick a provider first.");
  $("save").disabled = true;
  show("ok", "Checking with the provider…");
  try {
    const res = await fetch("/api/settings", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({provider: p.name, model: $("model-input").value.trim()}),
    });
    const data = await res.json();
    if (data.ok) {
      state.provider = data.provider;
      state.model = data.model;
      markClean($("form"));
      show("ok", `${data.detail}\n${data.note}`);
    } else {
      show("err", data.error || `HTTP ${res.status}`);
    }
  } catch (err) {
    show("err", String(err));
  } finally {
    refresh($("form"));
  }
});

async function post(url, body) {
  const res = await fetch(url, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(body),
  });
  const data = await res.json();
  return data.ok ? data : Promise.reject(new Error(data.error || `HTTP ${res.status}`));
}

$("digest-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const times = [$("morning").value, $("evening").value]
    .map((h) => `${String(h).padStart(2, "0")}:00`);
  $("save-digest").disabled = true;
  try {
    const sched = await post("/api/settings/schedule", {times});
    state.schedule = sched.times;
    const digest = await post("/api/settings/digest",
      {max_featured: parseInt($("max-featured").value, 10)});
    state.max_featured = digest.max_featured;
    markClean($("digest-form"));
    show("ok", `Digest hours: ${sched.times.join(" and ")}. ${sched.note}\n` +
      `Featured sections: ${digest.max_featured}. ${digest.note}`, "digest-result");
  } catch (err) {
    show("err", err.message || String(err), "digest-result");
  } finally {
    refresh($("digest-form"));
  }
});

$("notify-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("save-notify").disabled = true;
  try {
    const res = await fetch("/api/settings/notify", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({discord_webhook_url: $("discord-webhook").value}),
    });
    const data = await res.json();
    if (data.ok) {
      state.notify_webhook = data.discord_webhook_url;
      $("discord-webhook").value = data.discord_webhook_url;
      markClean($("notify-form"));
      show("ok", `${data.detail} ${data.note}`, "notify-result");
    } else {
      show("err", data.error || `HTTP ${res.status}`, "notify-result");
    }
  } catch (err) {
    show("err", String(err), "notify-result");
  } finally {
    refresh($("notify-form"));
  }
});

let sources = [];
let filter = "all";

const EMPTY = {
  all: "No sources configured.",
  rss: "No RSS sources.",
  newsletter: "No newsletter sources.",
};

function renderSources() {
  const rows = sources.filter((s) => filter === "all" || s.type === filter);
  $("src-body").innerHTML = rows.length
    ? rows.map((s) => `
      <tr class="src-row" data-name="${esc(s.name)}" tabindex="0">
        <td class="name">${esc(s.name)}</td>
        <td><span class="label">${esc(s.type)}</span></td>
        <td><span class="pill${s.tier === "summarize" ? " score" : ""}">${esc(s.tier)}</span></td>
        <td class="target">${esc(s.url || s.email_match || "—")}</td>
        <td class="small">${esc((s.tags || []).join(", ") || "—")}</td>
      </tr>`).join("")
    : `<tr><td colspan="5" class="small">${EMPTY[sources.length ? filter : "all"]}</td></tr>`;
}

function loaded(d) {
  sources = d.sources;
  $("sources-origin").textContent = `Served from ${d.origin}.`;
  if (!d.writable) {
    $("save-src").disabled = true;
    show("err", "No writable source table here — this deployment reads sources.yaml.", "src-result");
  }
  renderSources();
}

document.querySelectorAll("[data-filter]").forEach((button) => {
  button.addEventListener("click", () => {
    filter = button.dataset.filter;
    document.querySelectorAll("[data-filter]").forEach((b) => {
      b.setAttribute("aria-pressed", String(b === button));
    });
    renderSources();
  });
});

$("src-body").addEventListener("click", (e) => {
  const row = e.target.closest("tr.src-row");
  if (!row) return;
  const s = sources.find((x) => x.name === row.dataset.name);
  $("src-name").value = s.name;
  $("src-type").value = s.type;
  $("src-tier").value = s.tier;
  $("src-url").value = s.url || "";
  $("src-email").value = s.email_match || "";
  $("src-homepage").value = s.homepage || "";
  $("src-tags").value = (s.tags || []).join(", ");
  $("src-form").scrollIntoView({behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth"});
});

$("src-body").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && e.target.matches("tr.src-row")) e.target.click();
});

$("retire-src").addEventListener("click", async () => {
  const name = $("src-name").value.trim();
  if (!name || !confirm(`Stop fetching ${name}?`)) return;
  await writeSource(`/api/sources/${encodeURIComponent(name)}`, "DELETE", null,
                    `${name} retired.`);
});

async function writeSource(url, method, body, okText) {
  $("save-src").disabled = true;
  try {
    const res = await fetch(url, {
      method,
      headers: {"Content-Type": "application/json"},
      body: body ? JSON.stringify(body) : undefined,
    });
    const data = await res.json();
    if (data.ok) {
      show("ok", `${okText} ${data.note}`, "src-result");
      await loadSources();
    } else {
      show("err", data.error || `HTTP ${res.status}`, "src-result");
    }
  } catch (err) {
    show("err", String(err), "src-result");
  } finally {
    $("save-src").disabled = false;
  }
}

$("src-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const name = $("src-name").value.trim();
  if (!name) return show("err", "A source needs a name.", "src-result");
  const tags = $("src-tags").value.split(",").map((t) => t.trim()).filter(Boolean);
  return writeSource("/api/sources", "POST", {
    name,
    type: $("src-type").value,
    tier: $("src-tier").value,
    url: $("src-url").value.trim() || null,
    email_match: $("src-email").value.trim() || null,
    homepage: $("src-homepage").value.trim() || null,
    tags,
  }, `${name} saved.`);
});

const loadSources = () =>
  fetch("/api/sources")
    .then((r) => r.json())
    .then(loaded)
    .catch((e) => show("err", `Could not load sources: ${e}`, "src-result"));

loadSources();

fetch("/api/settings")
  .then((r) => r.json())
  .then((d) => { state = d; render(); })
  .catch((e) => show("err", `Could not load settings: ${e}`));
