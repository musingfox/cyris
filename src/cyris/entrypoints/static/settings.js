const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[<>&"]/g, (c) => `&#${c.charCodeAt(0)};`);
let state = null;

const TABS = ["model", "digest", "pipeline", "notifications", "sources"];
const SETTINGS_NOTICES = ["model-result", "digest-result", "pipeline-result", "notify-result"];

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
  const gutter = parseFloat(getComputedStyle(document.documentElement).getPropertyValue("--s-4"));
  active.parentElement.scrollLeft = active.offsetLeft - gutter;
}

addEventListener("hashchange", route);
route();

// A category's Save is live only while its form differs from what was last
// loaded or saved, or holds a missing setting it can save as it stands; the
// category list marks a form that differs with a dot.
const clean = new Map();
// A form whose save is in flight keeps its Save disabled and its notice.
const saving = new Set();
// Keyed by field, so a save can tell which part of its form changed.
const snapshot = (form) => Object.fromEntries([...form.querySelectorAll("input, select, textarea")]
  .map((input) => input.type === "radio"
    ? [`${input.name}=${input.value}`, input.checked]
    : [input.id, input.value]));
const saveButton = (form) => form.querySelector('button[type="submit"]');
const navLink = (form) => document.querySelector(`.settings-nav a[data-tab="${form.dataset.tab}"]`);

// The Model form saves in two parts, each through its own route.
const LLM_PART = (field) => field.startsWith("provider=") || field === "model-input";
const VOTE_PART = (field) => !LLM_PART(field);
const partChanged = (form, part) => {
  const now = snapshot(form), stored = clean.get(form) || now;
  return Object.keys(now).some((field) => part(field) && now[field] !== stored[field]);
};

function refresh(form) {
  const dirty = clean.has(form)
    && JSON.stringify(snapshot(form)) !== JSON.stringify(clean.get(form));
  // With no provider chosen, there is nothing a model could be checked against.
  const blocked = form.dataset.tab === "model" && partChanged(form, LLM_PART) && !chosen();
  const pending = clean.has(form) && unsavedIn(form).length > 0;
  saveButton(form).disabled = !(dirty || pending) || blocked || saving.has(form);
  navLink(form).classList.toggle("dirty", dirty);
}

function markClean(form, values = snapshot(form)) {
  clean.set(form, values);
  refresh(form);
}

// An edit makes the last result stale, so it goes; a deployment that cannot
// save never tracks, and its explanation stays.
function edited(form) {
  if (clean.has(form) && !saving.has(form)) form.querySelector(".actions-line .notice").hidden = true;
  refresh(form);
}

document.querySelectorAll("form.tab").forEach((form) => {
  form.addEventListener("input", (e) => { unrefuseEdited(e.target); edited(form); });
  form.addEventListener("change", (e) => { unrefuseEdited(e.target); edited(form); });
});

// Each runtime setting's field, from the server's registry: the category it sits
// in, the label a notice names it by, the controls marked while it is not set, and
// the route that saves it.
let FIELDS = {};
// The settings saved through /api/settings/values, by the id of their control.
let PLAIN = {};
const keysSavedBy = (route) => Object.keys(FIELDS).filter((key) => FIELDS[key].route === route);
// A missing setting whose answer may be empty, such as Style or Email to, is
// already answered by its empty field: on a first boot nothing has changed, yet
// it can be saved as it stands. Not the webhook, whose off is its own action, nor
// a model, which is saved with its provider.
const SENT_AS_SHOWN = ["plain", "email"];
const unsavedIn = (form) => state.may_be_empty.filter((key) => state.missing.includes(key)
  && SENT_AS_SHOWN.includes(FIELDS[key].route) && form.contains($(FIELDS[key].controls[0])));

// A control's own placeholder, which a missing value covers with "Not set".
document.querySelectorAll("[placeholder]").forEach((el) => {
  el.dataset.placeholder = el.placeholder;
});

// Whether `el` is a control of a setting this deployment's home lacks.
const isMissing = (el) => Object.entries(FIELDS)
  .some(([key, field]) => field.controls.includes(el.id) && state.missing.includes(key));

function applyPlaceholder(el) {
  if (!("placeholder" in el)) return;
  el.placeholder = isMissing(el) ? "Not set" : el.dataset.placeholder || "";
}

// A missing key's field, its category's notice and its dot stay until a save
// stores it.
function markMissing() {
  const missing = new Set(state.missing);
  const byTab = {};
  for (const [key, field] of Object.entries(FIELDS)) {
    const unset = missing.has(key);
    field.controls.forEach((id) => {
      const el = $(id);
      if (unset) el.setAttribute("aria-invalid", "true");
      else if (!el.classList.contains("invalid")) el.removeAttribute("aria-invalid");
      applyPlaceholder(el);
    });
    byTab[field.tab] = byTab[field.tab] || [];
    if (unset && !byTab[field.tab].includes(field.label)) byTab[field.tab].push(field.label);
  }
  for (const [tab, labels] of Object.entries(byTab)) {
    const notice = $(`${tab}-missing`);
    notice.textContent = !labels.length ? ""
      : state.writable
        ? `Not set yet: ${labels.join(", ")}. The next run stops until they are saved.`
        : `Missing from cyris.toml: ${labels.join(", ")}. The next run stops until they are added there.`;
    notice.hidden = !labels.length;
    document.querySelector(`.settings-nav a[data-tab="${tab}"]`)
      .classList.toggle("missing", labels.length > 0);
  }
}

function markSet(keys) {
  state.missing = state.missing.filter((key) => !keys.includes(key));
  markMissing();
}

function render() {
  const values = state.values;
  FIELDS = state.fields;
  PLAIN = Object.fromEntries(keysSavedBy("plain").map((key) => [FIELDS[key].controls[0], key]));
  $("providers").innerHTML = state.providers.map((p) => `
    <label class="choice${p.configured ? "" : " unavailable"}">
      <input type="radio" name="provider" value="${esc(p.name)}"
             ${p.name === values["llm_provider.provider"] && p.configured ? "checked" : ""}
             ${p.configured ? "" : "disabled"}>
      <span class="name">${p.name === "none" ? "None — plain excerpts" : esc(p.name)}</span>
      ${p.name === "none" ? `<span class="label">No key needed</span>`
        : p.configured ? `<span class="label">Key ready</span>`
        : `<span class="label key-missing">${esc(p.env_var)} missing</span>`}
    </label>`).join("");
  $("model-input").value = values["llm_provider.model"] ?? "";
  updateHint();
  $("providers").addEventListener("change", updateHint);
  // A missing key does not disable an embedder: with vote similarity off,
  // choosing one needs no call.
  $("embedding-providers").innerHTML = state.embedding_providers.map((p) => `
    <label class="choice">
      <input type="radio" name="embedding-provider" value="${esc(p.name)}"
             ${p.name === values["vote_similarity.provider"] ? "checked" : ""}>
      <span class="name">${esc(p.name)}</span>
      ${p.configured
        ? `<span class="label">Key ready</span>`
        : `<span class="label key-missing">${esc(p.env_var)} missing</span>`}
    </label>`).join("");
  $("embedding-model-input").value = values["vote_similarity.model"] ?? "";
  updateEmbeddingHint();
  $("embedding-providers").addEventListener("change", updateEmbeddingHint);
  const enabled = values["vote_similarity.enabled"];
  if (enabled == null) $("vote-enabled").prepend(new Option("Not set", "", true, true));
  else $("vote-enabled").value = String(enabled);
  if (values["vote_similarity.max_seeds"] != null) {
    $("vote-seeds").value = values["vote_similarity.max_seeds"];
  }
  const [m, e] = values["general.digest_schedule"] || [];
  if (m) $("morning").value = parseInt(m, 10);
  if (e) $("evening").value = parseInt(e, 10);
  $("languages").replaceChildren(...state.languages.map((tag) => new Option("", tag)));
  // A select would otherwise show its first option for a missing value.
  for (const [id, key] of Object.entries(PLAIN)) {
    if (values[key] != null) $(id).value = values[key];
    else if ($(id).tagName === "SELECT") $(id).prepend(new Option("Not set", "", true, true));
  }
  if (values["notify.discord_webhook_url"]) {
    $("discord-webhook").value = values["notify.discord_webhook_url"];
  }
  $("email-to").value = values["notify.email_to"] ?? "";
  $("email-from").value = values["notify.email_from"] ?? "";
  showNotifyState();
  markMissing();
  if (state.writable) {
    document.querySelectorAll("form.tab").forEach((form) => markClean(form));
  } else {
    SETTINGS_NOTICES.forEach((id) => {
      show("err", "This deployment has no settings store, so nothing can be saved here. " +
        "Edit cyris.toml instead.", id);
    });
  }
}

function chosen() {
  const el = document.querySelector('input[name=provider]:checked');
  return el && state.providers.find((p) => p.name === el.value);
}

function updateHint() {
  const p = chosen();
  // None calls no model, so there is no model to name.
  const none = p && p.name === "none";
  $("model-input").disabled = !!none;
  if (none) $("model-input").value = "";
  $("model-input").dataset.placeholder = p && !none ? `Empty uses ${p.default_model}` : "";
  applyPlaceholder($("model-input"));
  $("model-hint").textContent = !p ? "Pick a provider whose key is present."
    : none ? "No model is called: digests list plain excerpts."
    : "Saving makes one real call to the provider first, so a typo is rejected here.";
}

function updateEmbeddingHint() {
  const el = document.querySelector("input[name=embedding-provider]:checked");
  const p = el && state.embedding_providers.find((x) => x.name === el.value);
  $("embedding-model-input").dataset.placeholder = p ? `Empty uses ${p.default_model}` : "";
  applyPlaceholder($("embedding-model-input"));
}

// A "busy" notice says what a save is doing while it is out; its result replaces it.
function show(kind, text, target) {
  const el = typeof target === "string" ? $(target) : target;
  el.className = kind === "err" ? "notice err" : "notice";
  if (kind === "busy") el.setAttribute("aria-busy", "true");
  else el.removeAttribute("aria-busy");
  el.textContent = text;
  el.hidden = false;
}

// The notice below a field, shared by the controls of one row.
function fieldNotice(control) {
  const anchor = control.closest(".field-row") || control;
  let notice = anchor.nextElementSibling;
  if (!notice || !notice.hasAttribute("data-field-error")) {
    notice = document.createElement("div");
    notice.setAttribute("data-field-error", "");
    anchor.after(notice);
  }
  return notice;
}

// A refusal pinned to one field is marked there too, until it is edited; a
// screen reader hears it as the field's description.
function refuse(controls, text) {
  const notice = fieldNotice(controls[0]);
  notice.id ||= `${controls[0].id}-error`;
  controls.forEach((el) => {
    el.classList.add("invalid");
    el.setAttribute("aria-invalid", "true");
    el.setAttribute("aria-describedby", notice.id);
  });
  show("err", text, notice);
}

function refuseSetting(err) {
  if (FIELDS[err.field]) refuse(FIELDS[err.field].controls.map($), err.message);
}

// A setting still missing keeps the aria-invalid its missing mark gives it.
function clearRefusal(el) {
  el.classList.remove("invalid");
  el.removeAttribute("aria-describedby");
  if (!isMissing(el)) el.removeAttribute("aria-invalid");
}

function unrefuse(scope) {
  scope.querySelectorAll(".invalid").forEach(clearRefusal);
  scope.querySelectorAll("[data-field-error]").forEach((notice) => { notice.hidden = true; });
}

function unrefuseEdited(control) {
  const marked = control.closest(".invalid");
  if (!marked) return;
  clearRefusal(marked);
  const anchor = marked.closest(".field-row") || marked;
  if (!anchor.querySelector(".invalid")) fieldNotice(marked).hidden = true;
}

// One Save, two routes: the LLM part is checked against its provider, the vote
// part against its embedder when it is on. Only a changed part is sent; a part
// that saved becomes clean, one that failed stays dirty.
$("model-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const form = $("model-form");
  const stored = {...clean.get(form)}, sent = snapshot(form);
  const keep = (part) => Object.keys(sent).filter(part).forEach((f) => { stored[f] = sent[f]; });
  const lines = [];
  let failed = false;
  saving.add(form);
  refresh(form);
  unrefuse(form);
  if (partChanged(form, LLM_PART)) {
    const p = chosen();
    show("busy", "Checking with the provider…", "model-result");
    try {
      if (!p) throw new Error("Pick a provider first.");
      const data = await post("/api/settings", {provider: p.name, model: sent["model-input"].trim()});
      state.values["llm_provider.provider"] = data.provider;
      state.values["llm_provider.model"] = data.model;
      markSet(keysSavedBy("llm"));
      keep(LLM_PART);
      lines.push(`${data.detail}\n${data.note}`);
    } catch (err) {
      failed = true;
      refuseSetting(err);
      lines.push(err.message);
    }
  }
  if (Object.keys(sent).some((f) => VOTE_PART(f) && sent[f] !== stored[f])) {
    const embedder = document.querySelector("input[name=embedding-provider]:checked");
    const seeds = sent["vote-seeds"];
    const body = {
      enabled: {true: true, false: false}[sent["vote-enabled"]] ?? null,
      provider: embedder ? embedder.value : null,
      model: sent["embedding-model-input"].trim(),
      max_seeds: seeds === "" ? null : Number(seeds),
    };
    show("busy", body.enabled ? "Checking the embedder…" : "Saving…", "model-result");
    try {
      const data = await post("/api/settings/vote-similarity", body);
      Object.assign(state.values, data.values);
      $("vote-enabled").querySelector('option[value=""]')?.remove();
      markSet(keysSavedBy("vote"));
      keep(VOTE_PART);
      lines.push(`${data.detail}\n${data.note}`);
    } catch (err) {
      failed = true;
      refuseSetting(err);
      lines.push(err.message);
    }
  }
  saving.delete(form);
  markClean(form, stored);
  show(failed ? "err" : "ok", lines.join("\n"), "model-result");
});

// The app Worker answers 401 once the session cookie has expired. Reloading
// /settings then serves the sign-in form, which returns here.
const SESSION_EXPIRED = new Error("Your session has expired. Reload the page to sign in again.");

// Rejects with what the reader needs: the server's own explanation when it
// gives one, otherwise what happened and what to do next.
async function post(url, body, method = "POST") {
  let res;
  try {
    res = await fetch(url, {
      method,
      headers: {"Content-Type": "application/json"},
      body: body ? JSON.stringify(body) : undefined,
    });
  } catch (err) {
    throw new Error(`Could not reach cyris (${err.message}). Check the connection and try again.`);
  }
  if (res.status === 401) throw SESSION_EXPIRED;
  const data = await res.json().catch(() => ({}));
  if (data.ok) return data;
  const err = new Error(data.error || `cyris answered ${res.status} without saying why. ` +
    "Try again; if it keeps failing, check the server log.");
  // The setting or source field the refusal is about, for the page to mark.
  err.field = data.field;
  throw err;
}

const plainValue = (input) => {
  if (input.type === "number") return input.value === "" ? null : Number(input.value);
  // The type size is stored as a number, which a select only holds as text.
  if (input.id === "type-scale") return input.value === "" ? null : Number(input.value);
  // The style prompt is the reader's own words, spaces included.
  return input.id === "style-prompt" ? input.value : input.value.trim();
};

const shownValue = (value) => {
  if (value === "") return "empty";
  const text = String(value);
  return text.length > 40 ? `${text.slice(0, 40)}…` : text;
};

// Every changed plain field of `form`, and every one it can save as it stands, in
// one request, all or nothing. A field
// that saved takes its sent value in `stored`; the result is one notice line.
async function savePlain(form, stored) {
  const ids = Object.keys(PLAIN).filter((id) => form.contains($(id))
    && ($(id).value !== stored[id] || unsavedIn(form).includes(PLAIN[id])));
  if (!ids.length) return null;
  const sent = Object.fromEntries(ids.map((id) => [id, $(id).value]));
  const values = Object.fromEntries(ids.map((id) => [PLAIN[id], plainValue($(id))]));
  try {
    const data = await post("/api/settings/values", {values});
    Object.assign(stored, sent);
    Object.assign(state.values, data.values);
    markSet(Object.keys(data.values));
    ids.forEach((id) => $(id).querySelector('option[value=""]')?.remove());
    // The Worker sizes the next page it serves; this one follows at once.
    const scale = data.values["digest.type_scale"];
    if (scale != null) document.documentElement.style.setProperty("--type-scale", String(scale));
    const parts = ids.map((id) => `${FIELDS[PLAIN[id]].label}: ${shownValue(data.values[PLAIN[id]])}`);
    return {ok: true, line: `${parts.join(", ")}. ${data.note}`};
  } catch (err) {
    refuseSetting(err);
    const labels = ids.map((id) => FIELDS[PLAIN[id]].label);
    return {ok: false, line: `${labels.join(", ")} not saved. ${err.message || err}`};
  }
}

const HOURS = [$("morning"), $("evening")];

// An empty hour is refused here: padded, it would reach the server as 00:00.
// Returns the reason shown beside the hours, or null when both are filled.
function emptyHours() {
  const empty = HOURS.filter((input) => input.value === "");
  if (!empty.length) return null;
  const names = empty.map((input) => input.labels[0].textContent).join(" and ");
  const reason = `${names} ${empty.length > 1 ? "need" : "needs"} a whole hour from 0 to 23.`;
  refuse(empty, reason);
  return reason;
}

// One Save, two endpoints: only a changed part is sent, the hours first, and
// both are tried. A part that saved becomes clean; one that failed stays dirty.
$("digest-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const form = $("digest-form");
  const [morning, evening] = HOURS.map((input) => input.value);
  const stored = {...clean.get(form)};
  const lines = [];
  let failed = false;
  saving.add(form);
  refresh(form);
  unrefuse(form);
  show("busy", "Saving…", "digest-result");
  const hoursChanged = morning !== stored.morning || evening !== stored.evening;
  const empty = hoursChanged && emptyHours();
  if (empty) {
    failed = true;
    lines.push(`Digest hours not saved: ${empty}`);
  } else if (hoursChanged) {
    const times = [morning, evening].map((h) => `${String(h).padStart(2, "0")}:00`);
    try {
      const data = await post("/api/settings/schedule", {times});
      lines.push(`Digest hours: ${data.times.join(" and ")}. ${data.note}`);
      Object.assign(stored, {morning, evening});
      markSet(keysSavedBy("schedule"));
    } catch (err) {
      failed = true;
      refuseSetting(err);
      lines.push(`Digest hours not saved: ${err.message || err}`);
    }
  }
  const plain = await savePlain(form, stored);
  if (plain) {
    failed = failed || !plain.ok;
    lines.push(plain.line);
  }
  saving.delete(form);
  markClean(form, stored);
  show(failed ? "err" : "ok", lines.join("\n"), "digest-result");
});

$("pipeline-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const form = $("pipeline-form");
  const stored = {...clean.get(form)};
  saving.add(form);
  refresh(form);
  unrefuse(form);
  show("busy", "Saving…", "pipeline-result");
  const plain = await savePlain(form, stored);
  saving.delete(form);
  markClean(form, stored);
  if (plain) show(plain.ok ? "ok" : "err", plain.line, "pipeline-result");
});

// The Notifications form saves in two parts too: the webhook and the mail addresses.
const DISCORD_PART = (field) => field === "discord-webhook";
const EMAIL_PART = (field) => !DISCORD_PART(field);

$("notify-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const form = $("notify-form"), field = $("discord-webhook");
  const stored = {...clean.get(form)}, sent = snapshot(form);
  const changed = (part) => Object.keys(sent).some((f) => part(f) && sent[f] !== stored[f]);
  const keep = (part) => Object.keys(sent).filter(part).forEach((f) => { stored[f] = sent[f]; });
  const lines = [];
  let failed = false;
  saving.add(form);
  refresh(form);
  unrefuse(form);
  if (changed(DISCORD_PART)) {
    const url = sent["discord-webhook"];
    show("busy", "Checking with Discord…", "notify-result");
    try {
      const data = await post("/api/settings/notify", {discord_webhook_url: url});
      state.values["notify.discord_webhook_url"] = data.discord_webhook_url;
      // The stored value comes back masked; an edit typed meanwhile is kept.
      if (field.value === url) field.value = data.discord_webhook_url;
      markSet(keysSavedBy("notify"));
      showNotifyState();
      stored["discord-webhook"] = data.discord_webhook_url;
      lines.push(`${data.detail} ${data.note}`);
    } catch (err) {
      failed = true;
      refuseSetting(err);
      lines.push(err.message);
    }
  }
  if (changed(EMAIL_PART) || unsavedIn(form).length) {
    show("busy", "Sending a test message…", "notify-result");
    try {
      const data = await post("/api/settings/email",
        {email_to: sent["email-to"], email_from: sent["email-from"]});
      state.values["notify.email_to"] = data["notify.email_to"];
      state.values["notify.email_from"] = data["notify.email_from"];
      markSet(keysSavedBy("email"));
      keep(EMAIL_PART);
      lines.push(data.detail ? `${data.detail} ${data.note}` : data.note);
    } catch (err) {
      failed = true;
      refuseSetting(err);
      lines.push(err.message);
    }
  }
  saving.delete(form);
  markClean(form, stored);
  show(failed ? "err" : "ok", lines.join("\n"), "notify-result");
});

// "" is off by choice; a missing webhook is neither on nor off yet.
function showNotifyState() {
  const webhook = state.values["notify.discord_webhook_url"];
  $("notify-state").hidden = webhook !== "";
  $("notify-off").hidden = !(webhook !== "" && state.writable);
}

// The spec's destructive confirm, as Retire does it: the stored URL cannot be
// read back, so turning it off is not undone by pressing Save again.
let notifyOffTimer;
const disarmNotifyOff = () => {
  clearTimeout(notifyOffTimer);
  $("notify-off").classList.remove("armed");
  $("notify-off").textContent = "Turn off";
};

$("notify-off").addEventListener("click", async () => {
  const button = $("notify-off"), form = $("notify-form");
  if (!button.classList.contains("armed")) {
    button.classList.add("armed");
    button.textContent = "Confirm turn off";
    notifyOffTimer = setTimeout(disarmNotifyOff, 3000);
    return;
  }
  disarmNotifyOff();
  button.disabled = true;
  show("busy", "Turning off…", "notify-result");
  try {
    const data = await post("/api/settings/notify", {off: true});
    state.values["notify.discord_webhook_url"] = data.discord_webhook_url;
    $("discord-webhook").value = data.discord_webhook_url;
    markSet(keysSavedBy("notify"));
    showNotifyState();
    // Only the webhook is saved; unsaved mail edits stay dirty.
    markClean(form, {...clean.get(form), "discord-webhook": data.discord_webhook_url});
    show("ok", data.note, "notify-result");
  } catch (err) {
    show("err", err.message, "notify-result");
  } finally {
    button.disabled = false;
  }
});

let sources = [];
let sourcesWritable = false;
let filter = "all";
// Which editor is open: null for none, "" for a new source, else its name.
let openName = null;

const EMPTY = {
  all: "No sources yet. The next run stops until one is added.",
  rss: "No RSS sources.",
  newsletter: "No newsletter sources.",
};

const NO_SOURCE_TABLE = "No writable source table here — edit sources.yaml instead.";

const sourcesNav = () => document.querySelector('.settings-nav a[data-tab="sources"]');

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
  sourcesNav().classList.remove("dirty");
  if (openName !== null) openEditor(openName);
}

function editorValues(ed) {
  return JSON.stringify([...ed.querySelectorAll("input, select")].map((i) => i.value)
    .concat(ed.querySelector('[data-type][aria-pressed="true"]').dataset.type));
}

function openEditor(name) {
  const adding = name === "";
  const s = adding
    ? {name: "", type: "rss", tier: "filter", tags: []}
    : sources.find((x) => x.name === name);
  const anchor = adding ? null
    : document.querySelector(`tr.src-row[data-name="${CSS.escape(name)}"]`);
  if (!adding && !(s && anchor)) {
    openName = null;
    return null;
  }
  const ed = $("editor-tpl").content.firstElementChild.cloneNode(true);
  const q = (selector) => ed.querySelector(selector);
  if (anchor) {
    anchor.after(ed);
    anchor.classList.add("open");
  } else {
    $("src-body").prepend(ed);
  }
  q(".editor-title").textContent = adding ? "New source" : `Editing ${s.name}`;
  q("#e-name").value = s.name;
  q("#e-name").readOnly = !adding;
  q("#e-tier").value = s.tier;
  q("#e-url").value = s.url || "";
  q("#e-email").value = s.email_match || "";
  q("#e-home").value = s.homepage || "";
  q("#e-tags").value = (s.tags || []).join(", ");
  const save = q('[data-act="save"]'), retire = q('[data-act="retire"]');
  const setType = (type) => {
    ed.querySelectorAll("[data-type]").forEach((b) => {
      b.setAttribute("aria-pressed", String(b.dataset.type === type));
    });
    ed.querySelectorAll("[data-for]").forEach((field) => {
      field.hidden = field.dataset.for !== type;
    });
  };
  setType(s.type);
  const initial = editorValues(ed);
  const refreshEditor = () => {
    const dirty = editorValues(ed) !== initial;
    save.disabled = ed.inert || !sourcesWritable || !dirty || !q("#e-name").value.trim();
    sourcesNav().classList.toggle("dirty", dirty && sourcesWritable);
  };
  ed.querySelectorAll("[data-type]").forEach((b) => {
    b.addEventListener("click", () => { setType(b.dataset.type); refreshEditor(); });
  });
  const editorEdited = () => {
    if (sourcesWritable) ed.querySelectorAll(".notice").forEach((n) => { n.hidden = true; });
    ed.querySelectorAll(".invalid").forEach(clearRefusal);
    refreshEditor();
  };
  ed.addEventListener("input", editorEdited);
  ed.addEventListener("change", editorEdited);
  refreshEditor();
  retire.hidden = adding;
  if (!sourcesWritable) {
    // Rows still open so their values stay readable; nothing in them writes.
    retire.disabled = true;
    show("err", NO_SOURCE_TABLE, save.parentElement.querySelector(".notice"));
  }
  q('[data-act="cancel"]').addEventListener("click", () => {
    openName = null;
    renderSources();
  });
  save.addEventListener("click", () => saveSource(ed, save));
  // The spec's destructive confirm: the first press arms the button in place,
  // and only a second press within three seconds retires.
  let armTimer;
  const disarm = () => {
    clearTimeout(armTimer);
    retire.classList.remove("armed");
    retire.textContent = "Retire";
  };
  retire.addEventListener("click", async () => {
    if (!retire.classList.contains("armed")) {
      retire.classList.add("armed");
      retire.textContent = "Confirm retire";
      armTimer = setTimeout(disarm, 3000);
      return;
    }
    disarm();
    const data = await writeSource(`/api/sources/${encodeURIComponent(s.name)}`, "DELETE", null,
                                   retire, "Retiring…");
    if (data) {
      openName = null;
      await loadSources();
      show("ok", `${s.name} retired. ${data.note}`, "sources-notice");
    }
  });
  if (adding) q("#e-name").focus();
  return ed;
}

function loaded(d) {
  sources = d.sources;
  sourcesWritable = d.writable;
  $("add-source").disabled = !d.writable;
  if (!d.writable) show("err", NO_SOURCE_TABLE, "sources-notice");
  // No source stops the next run, as a missing setting does.
  sourcesNav().classList.toggle("missing", !sources.length);
  renderSources();
}

function pressFilter(value) {
  filter = value;
  document.querySelectorAll("[data-filter]").forEach((b) => {
    b.setAttribute("aria-pressed", String(b.dataset.filter === value));
  });
}

document.querySelectorAll("[data-filter]").forEach((button) => {
  button.addEventListener("click", () => {
    pressFilter(button.dataset.filter);
    openName = null;
    renderSources();
  });
});

$("src-body").addEventListener("click", (e) => {
  const row = e.target.closest("tr.src-row");
  if (!row) return;
  openName = openName === row.dataset.name ? null : row.dataset.name;
  renderSources();
});

$("add-source").addEventListener("click", () => {
  openName = "";
  renderSources();
});

$("src-body").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && e.target.matches("tr.src-row")) e.target.click();
});

// Resolves to the response on success, or null once the failure is shown
// beside the pressed button.
async function writeSource(url, method, body, button, doing) {
  const notice = button.parentElement.querySelector(".notice");
  button.disabled = true;
  show("busy", doing, notice);
  try {
    return await post(url, body, method);
  } catch (err) {
    show("err", err.message, notice);
    const control = err.field && button.closest("tr.editor").querySelector(`[name="${err.field}"]`);
    if (control) refuse([control], err.message);
  }
  button.disabled = false;
  return null;
}

async function saveSource(ed, button) {
  const q = (selector) => ed.querySelector(selector);
  const name = q("#e-name").value.trim();
  const type = q('[data-type][aria-pressed="true"]').dataset.type;
  // Only the fields this type shows: a stale URL on a newsletter, or a sender
  // on a feed, would be stored with nothing on the page to reveal it.
  const shown = (selector) =>
    q(selector).closest("[data-for]").dataset.for === type ? q(selector).value.trim() || null : null;
  // Locked while the save is out: a success rebuilds the editor from what was
  // stored, which would drop anything typed meanwhile.
  ed.inert = true;
  const data = await writeSource("/api/sources", "POST", {
    name,
    type,
    tier: q("#e-tier").value,
    url: shown("#e-url"),
    email_match: shown("#e-email"),
    homepage: shown("#e-home"),
    tags: q("#e-tags").value.split(",").map((t) => t.trim()).filter(Boolean),
  }, button, "Saving…");
  if (!data) {
    ed.inert = false;
    return;
  }
  // A source saved out of the active filter would lose its row, and the result
  // beside its Save with it.
  if (filter !== "all" && filter !== type) pressFilter("all");
  openName = data.name;
  await loadSources();
  const reopened = document.querySelector("tr.editor");
  show("ok", `${data.name} saved. ${data.note}`,
       reopened ? reopened.querySelector(".notice") : "sources-notice");
}

const loadSources = () =>
  fetch("/api/sources")
    .then((r) => (r.status === 401 ? Promise.reject(SESSION_EXPIRED) : r.json()))
    .then(loaded)
    .catch((e) => {
      $("src-body").querySelector("[data-loading]")?.remove();
      $("add-source").disabled = true;
      show("err", e === SESSION_EXPIRED ? e.message
        : `Could not load sources: ${e}. Reload the page to try again.`, "sources-notice");
    });

loadSources();

fetch("/api/settings")
  .then((r) => (r.status === 401 ? Promise.reject(SESSION_EXPIRED) : r.json()))
  .then((d) => { state = d; render(); })
  .catch((e) => {
    // Every Save stays disabled: tracking only starts once the values are known.
    const text = e === SESSION_EXPIRED ? e.message
      : `Could not load settings: ${e}. Reload the page to try again.`;
    SETTINGS_NOTICES.forEach((id) => show("err", text, id));
  })
  .finally(() => document.querySelectorAll("form.tab [data-loading]").forEach((el) => el.remove()));
