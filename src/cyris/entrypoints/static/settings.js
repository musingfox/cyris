const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[<>&"]/g, (c) => `&#${c.charCodeAt(0)};`);
let state = null;

const TABS = ["model", "digest", "theme", "pipeline", "notifications", "sources", "areas", "tracking"];
const SETTINGS_NOTICES = ["model-result", "digest-result", "theme-result", "pipeline-result", "notify-result"];

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
  // The Test buttons need their pair chosen; unsaved edits are what they test.
  if (form.dataset.tab === "model") {
    $("test-model").disabled = !chosen();
    $("test-embedding").disabled = !document.querySelector("input[name=embedding-provider]:checked");
  }
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
  // A stored zone is no longer only the detected one.
  if (!missing.has("general.timezone")) $("timezone-detected").hidden = true;
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

// The output languages, each stored as its BCP 47 tag and shown by the name
// languages.json gives it, in that file's order. A stored tag the list lacks is
// still a language the model is told to write in, so it stays an option of its
// own, shown as the tag, and a save that leaves the field alone keeps it.
function fillLanguages(select, languages, stored) {
  const options = languages.map(({tag, name}) => new Option(name, tag));
  if (stored != null && !languages.some(({tag}) => tag === stored)) {
    options.push(new Option(stored, stored));
  }
  select.replaceChildren(...options);
}

// The zone this browser reads its own clock in, or "" when it will not say.
function browserZone() {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || "";
  } catch {
    return "";
  }
}

// Every zone this browser knows, UTC, which some leave out, and the stored zone
// and this browser's own when the list lacks them, so a save that leaves the
// field alone keeps what was stored.
function fillTimezones(select, stored) {
  const known = typeof Intl.supportedValuesOf === "function" ? Intl.supportedValuesOf("timeZone") : [];
  const zones = new Set([...known, "UTC", stored, browserZone()].filter(Boolean));
  select.replaceChildren(...[...zones].sort().map((zone) => new Option(zone, zone)));
}

// A timezone with no stored value opens on this browser's own, marked as
// detected rather than stored; the form's Save then stores it.
function detectTimezone(form) {
  const zone = browserZone();
  if (state.values["general.timezone"] != null || !zone) return;
  $("timezone").value = zone;
  $("timezone-detected").hidden = false;
  refresh(form);
}

// A zone the reader picked is no longer the detected one.
$("timezone").addEventListener("change", () => { $("timezone-detected").hidden = true; });

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
        : p.name === "openai_compatible" ? `<span class="label">Key optional</span>`
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
      ${p.name === "openai_compatible" ? `<span class="label">Key optional</span>`
        : p.configured
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
  fillLanguages($("output-language"), state.languages, values["digest.output_language"]);
  fillTimezones($("timezone"), values["general.timezone"]);
  // A select would otherwise show its first option for a missing value.
  for (const [id, key] of Object.entries(PLAIN)) {
    if (values[key] != null) $(id).value = values[key];
    else if ($(id).tagName === "SELECT") $(id).prepend(new Option("Not set", "", true, true));
  }
  if (values["notify.discord_webhook_url"]) {
    $("discord-webhook").value = values["notify.discord_webhook_url"];
  }
  // The theme the page itself runs on: whichever the store already holds.
  applyTheme(values["digest.theme"] || "default");
  $("email-to").value = values["notify.email_to"] ?? "";
  $("email-from").value = values["notify.email_from"] ?? "";
  showNotifyState();
  markMissing();
  if (state.writable) {
    document.querySelectorAll("form.tab").forEach((form) => markClean(form));
    detectTimezone($("digest-form"));
  } else {
    SETTINGS_NOTICES.forEach((id) => {
      show("err", "This deployment has no settings store, so nothing can be saved here. " +
        "Edit cyris.toml instead.", id);
    });
  }
  updateDependencies();
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
  const compat = p && p.name === "openai_compatible";
  $("model-input").disabled = !!none || !!compat;
  if (compat) $("model-input").value = "";
  $("model-input").dataset.placeholder = p && !none && !compat && p.default_model
    ? `Empty uses ${p.default_model}` : "";
  applyPlaceholder($("model-input"));
  $("model-hint").textContent = !p ? "Pick a provider whose key is present."
    : none ? "No model is called: digests list plain excerpts."
    : compat
    ? `The endpoint and the model come from the environment: ${p.env_base_url ? "" : "OPENAI_COMPATIBLE_BASE_URL, "
        }OPENAI_COMPATIBLE_MODEL${p.env_model ? "" : " (unset)"}${p.env_base_url ? "" : "; the key OPENAI_COMPATIBLE_API_KEY (optional)"}.`
    : "Saving makes one real call to the provider first, so a typo is rejected here.";
}

function updateEmbeddingHint() {
  const el = document.querySelector("input[name=embedding-provider]:checked");
  const p = el && state.embedding_providers.find((x) => x.name === el.value);
  const compat = p && p.name === "openai_compatible";
  $("embedding-model-input").disabled = !!compat;
  if (compat) $("embedding-model-input").value = "";
  $("embedding-model-input").dataset.placeholder = p && !compat && p.default_model
    ? `Empty uses ${p.default_model}` : "";
  applyPlaceholder($("embedding-model-input"));
}

// A setting that does nothing until another holds a value is disabled until then,
// with a line saying what it waits for (ui-language.md §4 field). The vote order
// follows the Vote similarity select as it stands, unsaved or not; Clef's option
// follows its token, which only the server's environment can change.
function updateDependencies() {
  const votes = $("vote-enabled").value === "true";
  const rank = $("rank-by-preference");
  rank.disabled = !votes;
  $("rank-needs").hidden = votes;
  describe(rank, "rank-needs", !votes);
  // A required setting is never locked out of being stored: with no value, the
  // order a run takes without vote similarity is shown, and Save stores it.
  if (!votes && rank.value === "") {
    rank.value = "false";
    refresh($("digest-form"));
  }
  $("preference-source").querySelector('option[value="clef"]').disabled = !state.clef_ready;
  $("clef-needs").hidden = !!state.clef_ready;
  describe($("preference-source"), "clef-needs", !state.clef_ready);
}

// The theme the page itself runs on: the reader picks, the page changes at
// once, and Save stores the same id for the digest pages. One style element
// holds the override; the default theme is no override at all.
function applyTheme(id) {
  const style = document.getElementById("theme-preview") || (() => {
    const el = document.createElement("style");
    el.id = "theme-preview";
    document.head.appendChild(el);
    return el;
  })();
  style.textContent = ((state?.themes || []).find((t) => t.id === id) || {}).css || "";
}
applyTheme((state && state.values["digest.theme"]) || "default");
$("page-theme").addEventListener("change", () => applyTheme($("page-theme").value));

$("vote-enabled").addEventListener("change", () => { if (state) updateDependencies(); });

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
    describe(el, notice.id, true);
  });
  show("err", text, notice);
}

function refuseSetting(err) {
  if (FIELDS[err.field]) refuse(FIELDS[err.field].controls.map($), err.message);
}

// aria-describedby names every line that describes a control: its refusal, and the
// reason it is disabled, which come and go apart.
function describe(el, id, on) {
  const ids = new Set((el.getAttribute("aria-describedby") || "").split(" ").filter(Boolean));
  if (on) ids.add(id);
  else ids.delete(id);
  if (ids.size) el.setAttribute("aria-describedby", [...ids].join(" "));
  else el.removeAttribute("aria-describedby");
}

// A setting still missing keeps the aria-invalid its missing mark gives it.
function clearRefusal(el) {
  el.classList.remove("invalid");
  for (const id of (el.getAttribute("aria-describedby") || "").split(" ")) {
    if (document.getElementById(id)?.hasAttribute("data-field-error")) describe(el, id, false);
  }
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
      const data = await post("/api/settings", {
        provider: p.name,
        model: sent["model-input"].trim(),
      });
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

// The Test buttons: one real call per provider pair, storing nothing — a base
// URL and a model can be tried before committing to them. The same probes a
// Save makes, through the routes that leave the settings alone.
// A test's answer lands beside its own button — under the pair it tested —
// never in the save line, whose notices are about what was stored.
$("test-model").addEventListener("click", async () => {
  const p = chosen();
  if (!p) return;
  show("busy", `Testing ${p.name}…`, "test-model-result");
  try {
    const data = await post("/api/diagnostics/llm", {
      provider: p.name,
      model: $("model-input").value.trim(),
    });
    show("ok", `${data.detail} Egress: ${data.egress ?? "unknown"}. Nothing saved.`, "test-model-result");
  } catch (err) {
    show("err", `${err.message} Nothing saved.`, "test-model-result");
  }
});

$("test-embedding").addEventListener("click", async () => {
  const el = document.querySelector("input[name=embedding-provider]:checked");
  if (!el) return;
  show("busy", `Testing ${el.value}…`, "test-embedding-result");
  try {
    const data = await post("/api/diagnostics/embedding", {
      provider: el.value,
      model: $("embedding-model-input").value.trim(),
    });
    show("ok", `${data.detail} Nothing saved.`, "test-embedding-result");
  } catch (err) {
    show("err", `${err.message} Nothing saved.`, "test-embedding-result");
  }
});



// Access answers a reader it does not admit with a redirect the page cannot follow
// (opaqueredirect). Reloading /settings then serves the sign-in again.
const SESSION_EXPIRED = new Error("Your session has expired. Reload the page to sign in again.");
// The app Worker answers 403 when Cloudflare Access has not been configured here.
const ACCESS_NOT_SET_UP = new Error("Access is not set up for this deployment. Ask the operator to finish the Cloudflare Access setup, then reload the page.");
const REFUSALS = [SESSION_EXPIRED, ACCESS_NOT_SET_UP];

function refusalOf(res) {
  if (res.type === "opaqueredirect") return SESSION_EXPIRED;
  if (res.status === 403) return ACCESS_NOT_SET_UP;
  return null;
}

async function parseAnswer(res) {
  const refusal = refusalOf(res);
  if (refusal) throw refusal;
  return res.json();
}

// Rejects with what the reader needs: the server's own explanation when it
// gives one, otherwise what happened and what to do next.
async function post(url, body, method = "POST") {
  let res;
  try {
    res = await fetch(url, {
      method,
      headers: {"Content-Type": "application/json"},
      body: body ? JSON.stringify(body) : undefined,
      redirect: "manual",
    });
  } catch (err) {
    throw new Error(`Could not reach cyris (${err.message}). Check the connection and try again.`);
  }
  const refusal = refusalOf(res);
  if (refusal) throw refusal;
  const data = await res.json().catch(() => ({}));
  if (data.ok) return data;
  const err = new Error(data.error || `cyris answered ${res.status} without saying why. ` +
    "Try again; if it keeps failing, check the server log.");
  // The setting or source field the refusal is about, for the page to mark.
  err.field = data.field;
  err.status = res.status;
  err.body = data;
  throw err;
}

const plainValue = (input) => {
  if (input.type === "number") return input.value === "" ? null : Number(input.value);
  // The type size is stored as a number, which a select only holds as text.
  if (input.id === "type-scale") return input.value === "" ? null : Number(input.value);
  // A switch is stored as a boolean; its select holds "true" or "false".
  if (input.id === "rank-by-preference") return {true: true, false: false}[input.value] ?? null;
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

// The Theme form is the Pipeline form's one-field shape: one plain value, one
// notice line, nothing else to save.
$("theme-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const form = $("theme-form");
  const stored = {...clean.get(form)};
  saving.add(form);
  refresh(form);
  unrefuse(form);
  show("busy", "Saving…", "theme-result");
  const plain = await savePlain(form, stored);
  saving.delete(form);
  markClean(form, stored);
  if (plain) show(plain.ok ? "ok" : "err", plain.line, "theme-result");
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
  all: "No sources yet. The next run waits until one is added.",
  rss: "No RSS sources.",
  newsletter: "No newsletter sources.",
};

const NO_SOURCE_TABLE = "No writable source table here — edit sources.yaml instead.";

const rowOf = (name) => document.querySelector(`tr.src-row[data-name="${CSS.escape(name)}"]`);

const sourcesNav = () => document.querySelector('.settings-nav a[data-tab="sources"]');

// Only what `cyris doctor` would warn about: a feed failing now and then shows nothing.
const feedProblem = (s) => s.health?.problems?.length
  ? `<span class="small feed-problem">${esc(s.health.problems.join(" · "))}</span>`
  : "";

function renderSources() {
  const rows = sources.filter((s) => filter === "all" || s.type === filter);
  $("src-body").innerHTML = rows.length
    ? rows.map((s) => `
      <tr class="src-row" data-name="${esc(s.name)}" tabindex="0">
        <td class="name">${esc(s.name)}${feedProblem(s)}</td>
        <td><span class="label">${esc(s.type)}</span></td>
        <td><span class="pill">${esc(s.lane)}</span></td>
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
    ? {name: "", type: "rss", lane: "skim", tags: []}
    : sources.find((x) => x.name === name);
  const anchor = adding ? null : rowOf(name);
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
  q("#e-lane").value = s.lane;
  q("#e-url").value = s.url || "";
  q("#e-email").value = s.email_match || "";
  q("#e-home").value = s.homepage || "";
  q("#e-tags").value = (s.tags || []).join(", ");
  const area = q("#e-area"), suggest = q('[data-act="suggest"]'), note = q("[data-suggest-note]");
  const tag = q("[data-suggested]");
  // The select always holds the stored area, even before the area list answers, so a
  // Save that goes out then keeps it; the names fill in when the list does.
  const syncArea = (chosen = area.value) => {
    const active = activeAreas();
    area.replaceChildren(new Option("None", ""), ...active.map((x) => new Option(x.name, String(x.id))));
    if (chosen && !active.some((x) => String(x.id) === chosen)) {
      area.append(new Option(areasLoaded ? `Area ${chosen} (not active)` : `Area ${chosen} (list not loaded)`,
                             chosen));
    }
    area.value = chosen;
    area.disabled = !sourcesWritable || !areasLoaded;
    suggest.disabled = !sourcesWritable || !areasLoaded || !active.length;
    suggest.title = areasLoaded && !active.length ? "Confirm an area first" : "";
  };
  syncArea(s.area_id == null ? "" : String(s.area_id));
  syncSourceArea = syncArea;
  // A reader's own change ends the suggestion, which the field then no longer marks.
  area.addEventListener("change", () => { note.hidden = true; tag.hidden = true; });
  suggest.addEventListener("click", () => suggestArea(ed, suggest));
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
    (adding ? $("add-source") : rowOf(name)).focus();
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
    if (!data) {
      returnFocus(retire);
      return;
    }
    openName = null;
    await loadSources();
    show("ok", `${s.name} retired. ${data.note}`, "sources-notice");
    returnFocus($("add-source"));
  });
  q("#e-name").focus();
  return ed;
}

function loaded(d) {
  sources = d.sources;
  sourcesWritable = d.writable;
  sourcesLoaded = true;
  sourcesFailed = false;
  reconcilePicks();
  renderAreas();
  $("add-source").disabled = !d.writable;
  $("import-opml").hidden = !d.writable;
  if (!d.writable) show("err", NO_SOURCE_TABLE, "sources-notice");
  // No source publishes no issue, so the category is marked like a missing
  // setting; unlike one, the next run waits instead of stopping.
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
  const name = row.dataset.name;
  openName = openName === name ? null : name;
  renderSources();
  if (openName === null) rowOf(name).focus();
});

$("add-source").addEventListener("click", () => {
  openName = "";
  renderSources();
});

// Import OPML: one file of feeds, its folder structure flattened to a name and
// url each, deduplicated against the table and itself, then one POST per feed
// the same route `Add source` uses — the skim lane, editable per row after.
const opmlOutlines = (doc) =>
  [...doc.querySelectorAll("outline[xmlUrl]")]
    .map((o) => ({
      name: (o.getAttribute("title") || o.getAttribute("text") || o.getAttribute("xmlUrl") || "").trim(),
      url: o.getAttribute("xmlUrl").trim(),
    }))
    .filter((f) => f.name && f.url);

// Distinct from every name the table holds and every name this file already
// gave — a duplicate would silently overwrite, which an import must not do.
const distinctName = (name, taken) => {
  if (!taken.has(name)) return name;
  let n = 2, candidate;
  do candidate = `${name} (${n++})`;
  while (taken.has(candidate));
  return candidate;
};

$("import-opml").addEventListener("click", () => $("opml-file").click());

$("opml-file").addEventListener("change", async () => {
  const file = $("opml-file").files[0];
  $("opml-file").value = "";  // a re-pick of the same file must fire change again
  if (!file) return;
  let feeds;
  try {
    const doc = new DOMParser().parseFromString(await file.text(), "text/xml");
    if (doc.querySelector("parsererror")) throw new Error("the file is not valid XML");
    feeds = opmlOutlines(doc);
  } catch (err) {
    show("err", `Could not read the OPML: ${err.message}`, "sources-notice");
    return;
  }
  if (!feeds.length) {
    show("err", "No feeds in that file: an outline needs an xmlUrl to be one.", "sources-notice");
    return;
  }
  const taken = new Set(sources.map((s) => s.name));
  feeds = feeds.map((f) => {
    f.name = distinctName(f.name, taken);
    taken.add(f.name);
    return f;
  });
  const failures = [];
  for (let i = 0; i < feeds.length; i++) {
    show("busy", `Importing ${i + 1}/${feeds.length}: ${feeds[i].name}…`, "sources-notice");
    try {
      await post("/api/sources", feeds[i]);
    } catch (err) {
      if (err === SESSION_EXPIRED) { show("err", err.message, "sources-notice"); return; }
      failures.push(`${feeds[i].name}: ${err.message}`);
    }
  }
  openName = null;
  await loadSources();
  const note = failures.length
    ? `${feeds.length - failures.length} of ${feeds.length} imported. Not imported:\n${failures.slice(0, 5).join("\n")}${failures.length > 5 ? `\n…and ${failures.length - 5} more` : ""}`
    : `${feeds.length} imported. Each row sits in the skim lane; open one to change its lane, or retire it.`;
  show(failures.length ? "err" : "ok", note, "sources-notice");
});

$("src-body").addEventListener("keydown", (e) => {
  if ((e.key === "Enter" || e.key === " ") && e.target.matches("tr.src-row")) {
    // Space would otherwise scroll the page.
    e.preventDefault();
    e.target.click();
  }
});

// Disabling the focused button, or locking the editor around it, drops focus to
// the body; give it back, unless the reader has moved on meanwhile.
function returnFocus(control) {
  if (!document.activeElement || document.activeElement === document.body) control.focus();
}

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

// Selects the area the model suggests for this source, marked as a suggestion; a failure
// changes nothing and says so beside the button. Save is never held by it.
async function suggestArea(ed, button) {
  const q = (selector) => ed.querySelector(selector);
  const note = q("[data-suggest-note]");
  button.disabled = true;
  button.textContent = "Suggesting…";
  q("#e-area").disabled = true;
  try {
    const data = await post("/api/sources/suggest-area", {
      name: q("#e-name").value.trim(),
      type: q('[data-type][aria-pressed="true"]').dataset.type,
      url: q("#e-url").value.trim(),
      homepage: q("#e-home").value.trim(),
      tags: q("#e-tags").value.split(",").map((t) => t.trim()).filter(Boolean),
    });
    if (data.suggestion) {
      q("#e-area").value = String(data.suggestion.id);
      q("#e-area").dispatchEvent(new Event("change", {bubbles: true}));
      q("[data-suggested]").hidden = false;
      note.textContent = data.note;
    } else {
      note.textContent = `No suggestion. ${data.note}`;
    }
  } catch (err) {
    note.textContent = `No suggestion. ${err.message}`;
  }
  note.hidden = false;
  button.textContent = "Suggest";
  syncSourceArea();
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
    lane: q("#e-lane").value,
    url: shown("#e-url"),
    email_match: shown("#e-email"),
    homepage: shown("#e-home"),
    tags: q("#e-tags").value.split(",").map((t) => t.trim()).filter(Boolean),
    area_id: q("#e-area").value === "" ? null : Number(q("#e-area").value),
  }, button, "Saving…");
  if (!data) {
    ed.inert = false;
    returnFocus(button);
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
  fetch("/api/sources", {redirect: "manual"})
    .then(parseAnswer)
    .then(loaded)
    .catch((e) => {
      $("src-body").querySelector("[data-loading]")?.remove();
      $("add-source").disabled = true;
      sourcesLoaded = false;
      sourcesFailed = true;
      renderAreas();
      show("err", REFUSALS.includes(e) ? e.message
        : `Could not load sources: ${e}. Reload the page to try again.`, "sources-notice");
    });

loadSources();

// Areas: the active ones in order, the proposals waiting for a decision, and the
// closed ones, from one read. A deployment without an area table is read-only.
let areas = [];
let areasWritable = false;
// Whether GET /api/areas has answered, and the open source editor's area select, which
// it refills when it does.
let areasLoaded = false;
let syncSourceArea = null;
// The active ids as the server last listed them, and the page's order, which moves
// locally until Save order sends it.
let storedOrder = [];
let localOrder = [];
// Whether each read has answered, and whether it failed; the Sources part waits for both.
let sourcesLoaded = false;
let sourcesFailed = false;
let areasFailed = false;
// The Area each source's select was changed to, only where it differs from the stored area.
// A null-prototype map, so a source named like an Object.prototype member keeps its own key.
const picks = Object.create(null);
// The (articles, issues) a proposal must reach to be marked; null until it is set.
let birthThreshold = null;

const NO_AREA_TABLE = "No writable area table here — the areas live in the D1 store this deployment lacks.";

const areasNav = () => document.querySelector('.settings-nav a[data-tab="areas"]');

const meetsThreshold = (area) => birthThreshold !== null && area.status === "proposed"
  && area.proposal_articles >= birthThreshold.articles
  && area.proposal_issues >= birthThreshold.issues;

const areaNameOf = (id) => areas.find((area) => area.id === id)?.name ?? "";

// Which editor is open: null for none, else the area's id.
let openArea = null;
// The area each source is suggested, by the last propose answer: the first group naming it wins.
let suggestions = Object.create(null);
// The sources whose select the reader has changed since the last propose answer.
const touched = new Set();

// A suggestion counts while its area is active or proposed, whatever the source's stored area is.
const suggestionFor = (name) => {
  const id = suggestions[name];
  const target = areas.find((area) => area.id === id);
  if (!target || (target.status !== "active" && target.status !== "proposed")) return undefined;
  return id;
};

const areaRow = (id) => document.querySelector(`tr.src-row[data-id="${id}"]`);
// Where focus goes when an area's row has left its table: the Closed list it moved to, or the
// list itself when a 404 removed it.
const closedSummary = () => $("closed-list").closest("details").querySelector("summary");
const focusAfterLeaving = (id) => (areaRow(id) ?? closedSummary()).focus();

const activeAreas = () => areas.filter((area) => area.status === "active");
const orderDirty = () => localOrder.join() !== storedOrder.join();
const storedArea = (name) => sources.find((source) => source.name === name)?.area_id ?? null;
const pickedArea = (name) => (Object.hasOwn(picks, name) ? picks[name] : storedArea(name));
const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;
const listJoin = (words) => words.length < 2 ? words.join("")
  : `${words.slice(0, -1).join(", ")} and ${words.at(-1)}`;
// The unsaved selects that point at a proposal, said under its name.
const picked = (id) => {
  const n = Object.keys(picks).filter((name) => picks[name] === id).length;
  return n ? `<span class="small areas-note">${plural(n, "source")} picked</span>` : "";
};
// What a pick means after a reload: a merged area's pick follows it to its target, and a pick
// is dropped when its area is dismissed or gone, or when it no longer differs from the stored one.
const resolvePick = (id) => {
  if (id === null) return null;
  const area = areas.find((x) => x.id === id);
  if (area?.status === "merged") {
    return activeAreas().some((x) => x.id === area.merged_into) ? area.merged_into : undefined;
  }
  return area && (area.status === "active" || area.status === "proposed") ? id : undefined;
};
function reconcilePicks() {
  // Nothing is judged against a list that has not answered, so a failed read keeps every pick.
  if (!areasLoaded || !sourcesLoaded) return;
  for (const name of Object.keys(picks)) {
    const value = sources.some((s) => s.name === name) ? resolvePick(picks[name]) : undefined;
    if (value === undefined || value === storedArea(name)) delete picks[name];
    else picks[name] = value;
  }
}
const changedSources = () => sources.map((source) => source.name)
  .filter((name) => pickedArea(name) !== storedArea(name));
const thresholdDirty = () => thresholdValues() !== storedThreshold();

// The Areas nav link carries one dot for every unsaved change on the tab.
function updateAreasDot() {
  const dirty = orderDirty() || thresholdDirty() || changedSources().length > 0;
  areasNav().classList.toggle("dirty", areasWritable && dirty);
}

// Propose needs an area table to write to and a sources list that has answered with sources.
const proposable = () => areasWritable && sourcesLoaded && sources.length > 0;

function renderAreas() {
  const active = activeAreas();
  const proposed = areas.filter((area) => area.status === "proposed");
  const closed = areas.filter((area) => area.status === "dismissed" || area.status === "merged");
  const ordered = localOrder.map((id) => active.find((area) => area.id === id)).filter(Boolean);
  $("area-body").innerHTML = ordered.length
    ? ordered.map((area, i) => `
      <tr class="src-row" data-id="${area.id}" tabindex="0">
        <td class="name">${esc(area.name)}</td>
        <td class="areas-pos-cell"><select class="select areas-pos" aria-label="Position of ${esc(area.name)}" data-pos="${area.id}"${areasWritable ? "" : " disabled"}>${
          ordered.map((_, k) => `<option value="${k}"${k === i ? " selected" : ""}>${k + 1}</option>`).join("")}</select></td>
      </tr>`).join("")
    : areasFailed ? ""
    : !areasLoaded ? `<tr data-loading><td colspan="2" class="small">Loading areas…</td></tr>`
    : `<tr><td colspan="2" class="small">No areas yet. Use "Propose areas from my sources" under Sources, or wait for the next run to suggest some.</td></tr>`;
  $("proposed-body").innerHTML = proposed.length
    ? proposed.map((area) => `
      <tr class="src-row" data-id="${area.id}" tabindex="0">
        <td class="name">${esc(area.name)}${picked(area.id)}</td>
        <td class="small">${area.proposal_articles} articles / ${area.proposal_issues} issues${
          meetsThreshold(area) ? ' <span class="pill">Meets birth threshold</span>' : ""}</td>
      </tr>`).join("")
    : areasLoaded ? `<tr><td colspan="2" class="small">No proposals yet.</td></tr>` : "";
  $("closed-list").innerHTML = closed.length
    ? closed.map((area) => `<li>${esc(area.name)} — ${area.status === "merged"
      ? `merged into ${esc(areaNameOf(area.merged_into))}` : "dismissed"}</li>`).join("")
    : areasLoaded ? "<li>No closed areas.</li>" : "";
  $("threshold-hint").hidden = birthThreshold !== null;
  $("save-order").disabled = !areasWritable || !orderDirty();
  $("propose-areas").disabled = !proposable();
  renderAssign();
  updateAreasDot();
  if (openArea !== null) openAreaEditor(openArea);
}

// Every select lists None, the active areas in order, the proposals, and the stored area
// when it is neither, so an untouched row keeps its value.
function areaOptions(cur) {
  const active = activeAreas();
  const proposed = areas.filter((area) => area.status === "proposed");
  const option = (area) => `<option value="${area.id}"${area.id === cur ? " selected" : ""}>${esc(area.name)}</option>`;
  const known = [...active, ...proposed].some((area) => area.id === cur);
  return `<option value=""${cur === null ? " selected" : ""}>None</option>`
    + active.map(option).join("")
    + (proposed.length ? `<optgroup label="Proposed">${proposed.map(option).join("")}</optgroup>` : "")
    + (cur !== null && !known ? `<option value="${cur}" selected>Area ${cur} (not active)</option>` : "");
}

// Marks a suggestion the reader has not changed; a source with another area gets a note instead.
const suggestPill = (name) => storedArea(name) === null && suggestionFor(name) !== undefined
  && pickedArea(name) === suggestionFor(name) && !touched.has(name)
  ? `<span class="pill">Suggested</span>` : "";
const suggestNote = (name) => storedArea(name) !== null && suggestionFor(name) !== undefined
  && suggestionFor(name) !== storedArea(name)
  ? `<span class="small areas-note">Suggests ${esc(areaNameOf(suggestionFor(name)))}</span>` : "";

// The table waits for both reads, so no select can show a stored area before the list it names.
function renderAssign() {
  const ready = areasLoaded && sourcesLoaded;
  const failed = areasFailed || sourcesFailed;
  const changed = changedSources();
  $("assign-body").innerHTML = failed ? ""
    : !ready ? `<tr data-loading><td colspan="2" class="small">Loading sources…</td></tr>`
    : sources.length ? sources.map((source) => `
      <tr><td class="name">${esc(source.name)}${suggestNote(source.name)}</td>
        <td><div class="areas-cell"><select class="select" aria-label="Area for ${esc(source.name)}" data-source="${esc(source.name)}"${areasWritable ? "" : " disabled"}>${areaOptions(pickedArea(source.name))}</select>${suggestPill(source.name)}</div></td></tr>`).join("")
    : `<tr><td class="small" colspan="2">No sources yet. <a href="#sources">Add one under Sources</a>.</td></tr>`;
  $("save-assign").disabled = failed || !ready || !areasWritable || !changed.length;
}

// The destructive confirm: the first press arms the button, a second within three seconds acts.
function armThen(button, label, act) {
  let timer;
  const disarm = () => {
    clearTimeout(timer);
    button.classList.remove("armed");
    button.textContent = label;
  };
  button.addEventListener("click", () => {
    if (!button.classList.contains("armed")) {
      button.classList.add("armed");
      button.textContent = `Confirm ${label.toLowerCase()}`;
      timer = setTimeout(disarm, 3000);
      return;
    }
    disarm();
    act();
  });
}

// Resolves to the response, or null once the failure is shown in the editor, which stays open.
function openAreaEditor(id) {
  const area = areas.find((x) => x.id === id);
  const anchor = area && areaRow(id);
  if (!anchor) {
    openArea = null;
    return null;
  }
  const ed = $("area-editor-tpl").content.firstElementChild.cloneNode(true);
  const q = (selector) => ed.querySelector(selector);
  anchor.after(ed);
  anchor.classList.add("open");
  q(".editor-title").textContent = `Editing ${area.name}`;
  q("#a-name").value = area.name;
  const save = q('[data-act="save"]');
  const acts = ["activate", "dismiss", "merge"].map((act) => q(`[data-act="${act}"]`));
  const into = q("#a-into");
  const targets = activeAreas().filter((x) => x.id !== id);
  into.replaceChildren(...(targets.length
    ? targets.map((x) => new Option(x.name, String(x.id)))
    : [new Option("No active area to merge into", "")]));
  ed.querySelectorAll("[data-proposed]").forEach((el) => { el.hidden = area.status !== "proposed"; });
  save.classList.toggle("primary", area.status === "active");
  save.classList.toggle("secondary", area.status !== "active");
  const refreshEditor = () => {
    save.disabled = ed.inert || !areasWritable || !q("#a-name").value.trim()
      || q("#a-name").value === area.name;
    acts.forEach((button) => { button.disabled = !areasWritable; });
    into.disabled = !areasWritable || !targets.length;
    q('[data-act="merge"]').disabled = into.disabled;
  };
  const editorEdited = () => {
    ed.querySelectorAll(".notice, [data-field-error]").forEach((n) => { n.hidden = true; });
    ed.querySelectorAll(".invalid").forEach(clearRefusal);
    refreshEditor();
  };
  ed.addEventListener("input", editorEdited);
  ed.addEventListener("change", editorEdited);
  refreshEditor();
  if (!areasWritable) show("err", NO_AREA_TABLE, q(".notice"));
  const writeArea = async (url, method, body, doing) => {
    const notice = q(".notice");
    const pressed = document.activeElement;
    ed.inert = true;
    ed.querySelectorAll("button").forEach((button) => { button.disabled = true; });
    show("busy", doing, notice);
    try {
      return await post(url, body, method);
    } catch (err) {
      if (err.status === 404) {
        // The row changed or went elsewhere: close the editor and show the list as it is now.
        openArea = null;
        await loadAreas();
        show("err", "This area no longer exists.", "areas-notice");
        focusAfterLeaving(id);
        return null;
      }
      const control = err.field && q(`[name="${err.field}"]`);
      if (control) refuse([control], err.message);
      else show("err", err.message, notice);
      return null;
    } finally {
      ed.inert = false;
      refreshEditor();
      returnFocus(pressed);
    }
  };
  const finish = async (text) => {
    openArea = null;
    await loadAreas();
    show("ok", text, "areas-notice");
    focusAfterLeaving(id);
  };
  q('[data-act="cancel"]').addEventListener("click", () => {
    openArea = null;
    renderAreas();
    areaRow(id)?.focus();
  });
  save.addEventListener("click", async () => {
    const name = q("#a-name").value.trim();
    const data = await writeArea(`/api/areas/${id}`, "PUT", {name}, "Saving…");
    if (!data) return;
    // As Tracking's save does: the editor reopens on the row with the result beside it.
    openArea = id;
    await loadAreas();
    show("ok", `${data.name} saved. ${data.note}`, document.querySelector("tr.editor .notice"));
    areaRow(id)?.focus();
  });
  q('[data-act="activate"]').addEventListener("click", async () => {
    const data = await writeArea(`/api/areas/${id}/activate`, "POST", null, "Activating…");
    if (data) await finish(`${area.name} activated. ${data.note}`);
  });
  armThen(q('[data-act="dismiss"]'), "Dismiss", async () => {
    const data = await writeArea(`/api/areas/${id}/dismiss`, "POST", null, "Dismissing…");
    if (data) await finish(`${area.name} dismissed. ${data.note}`);
  });
  armThen(q('[data-act="merge"]'), "Merge", async () => {
    const target = Number(into.value);
    const data = await writeArea(`/api/areas/${id}/merge`, "POST", {into: target}, "Merging…");
    if (data) await finish(`${area.name} merged into ${areaNameOf(target)}. ${data.note}`);
  });
  return ed;
}

function toggleArea(id) {
  openArea = openArea === id ? null : id;
  renderAreas();
  if (openArea === null) areaRow(id)?.focus();
  else document.querySelector("tr.editor #a-name").focus();
}

$("propose-areas").addEventListener("click", async () => {
  const button = $("propose-areas");
  const label = button.textContent;
  button.disabled = true;
  button.textContent = "Proposing…";
  show("busy", "Proposing areas from your sources…", "areas-notice");
  try {
    const data = await post("/api/areas/propose", {});
    suggestions = Object.create(null);
    for (const p of data.proposals) {
      for (const name of p.sources) suggestions[name] ??= p.id;
    }
    touched.clear();
    await loadAreas();
    // A source with no area takes its suggestion, which the reader can turn down by changing it.
    for (const source of sources) {
      const suggested = suggestionFor(source.name);
      if (suggested !== undefined && storedArea(source.name) === null) {
        picks[source.name] = suggested;
      }
    }
    renderAreas();
    show("ok", data.created ? `${plural(data.created, "new proposal")} under Proposed.`
      : "No new proposals.", "areas-notice");
  } catch (err) {
    show("err", err.message, "areas-notice");
    button.disabled = !proposable();
  } finally {
    button.textContent = label;
  }
});

const areaBodyClick = (e) => {
  if (e.target.closest("button, a, input, select")) return;
  const row = e.target.closest("tr.src-row");
  if (row) toggleArea(Number(row.dataset.id));
};

const areaBodyKeydown = (e) => {
  if ((e.key === "Enter" || e.key === " ") && e.target.matches("tr.src-row")) {
    // Space would otherwise scroll the page.
    e.preventDefault();
    toggleArea(Number(e.target.dataset.id));
  }
};

["area-body", "proposed-body"].forEach((id) => {
  $(id).addEventListener("click", areaBodyClick);
  $(id).addEventListener("keydown", areaBodyKeydown);
});

// A Position change moves the row on the page only; Save order sends the order.
$("area-body").addEventListener("change", (e) => {
  if (!e.target.dataset.pos) return;
  const id = Number(e.target.dataset.pos);
  localOrder = localOrder.filter((x) => x !== id);
  localOrder.splice(Number(e.target.value), 0, id);
  renderAreas();
  document.querySelector(`[data-pos="${id}"]`).focus();
});

// Sends the whole page order. A refusal re-reads the areas, which keeps the unsaved order.
async function saveOrder() {
  const button = $("save-order");
  button.disabled = true;
  show("busy", "Saving order…", "order-result");
  try {
    const data = await post("/api/areas/order", {ids: localOrder}, "PUT");
    show("ok", `Order saved. ${data.note}`, "order-result");
    await loadAreas();
  } catch (err) {
    show("err", err.message, "order-result");
    if (err.status === 400) await loadAreas();
    else button.disabled = false;
  }
}
$("save-order").addEventListener("click", saveOrder);

// Sends the changed rows in one request, then re-reads both lists whatever the answer.
async function saveAssignments() {
  const assignments = Object.fromEntries(changedSources().map((name) => [name, pickedArea(name)]));
  const button = $("save-assign");
  button.disabled = true;
  show("busy", "Saving assignments…", "assign-result");
  try {
    const data = await post("/api/areas/assign", {assignments});
    const names = data.activated.map((area) => area.name);
    show("ok", `${plural(data.assigned, "source")} assigned${names.length
      ? `; ${listJoin(names)} ${names.length === 1 ? "is" : "are"} active` : ""}.`, "assign-result");
  } catch (err) {
    // A failed write may have landed in part, so the notice says what did and what is left.
    const landed = err.body?.assigned;
    const names = (err.body?.activated ?? []).map((area) => area.name);
    const verb = names.length === 1 ? "is" : "are";
    if (landed) {
      show("err", `${err.message} — ${landed.length} of ${Object.keys(assignments).length} sources assigned${
        names.length ? `; ${listJoin(names)} ${verb} active` : ""}. Save again for the rest.`, "assign-result");
    } else if (err.status === undefined && !REFUSALS.includes(err)) {
      show("err", "Could not confirm the save; showing what is stored.", "assign-result");
    } else {
      show("err", err.message, "assign-result");
    }
  }
  await Promise.all([loadAreas(), loadSources()]);
}
$("save-assign").addEventListener("click", saveAssignments);

$("assign-body").addEventListener("change", (e) => {
  const name = e.target.dataset.source;
  if (name === undefined) return;
  const value = e.target.value === "" ? null : Number(e.target.value);
  if (value === storedArea(name)) delete picks[name]; else picks[name] = value;
  touched.add(name);
  $("assign-result").hidden = true;
  renderAreas();
  document.querySelector(`select[data-source="${CSS.escape(name)}"]`).focus();
});

const loadAreas = () =>
  fetch("/api/areas", {redirect: "manual"})
    .then(parseAnswer)
    .then((d) => {
      if (!d.areas) throw new Error(d.error);
      areas = d.areas;
      areasWritable = d.writable;
      areasLoaded = true;
      areasFailed = false;
      reconcilePicks();
      const activeIds = activeAreas().map((area) => area.id);
      const clean = !orderDirty();
      storedOrder = activeIds;
      localOrder = clean ? activeIds
        : [...localOrder.filter((id) => activeIds.includes(id)),
           ...activeIds.filter((id) => !localOrder.includes(id))];
      birthThreshold = d.birth_threshold;
      if (!d.writable) show("err", NO_AREA_TABLE, "areas-notice");
      renderAreas();
      renderThreshold();
      syncSourceArea?.();
    })
    .catch((e) => {
      areas = [];
      areasWritable = false;
      areasLoaded = false;
      areasFailed = true;
      syncSourceArea?.();
      $("propose-areas").disabled = true;
      show("err", REFUSALS.includes(e) ? e.message
        : `Could not load areas: ${e.message || e}. Reload the page to try again.`, "areas-notice");
      renderAreas();
      renderThreshold();
    });

// The two inputs show what is stored, unless the reader has typed since the last save.
let thresholdTyped = false;
const thresholdValues = () => [$("th-articles").value, $("th-issues").value].join();
const storedThreshold = () => birthThreshold
  ? [birthThreshold.articles, birthThreshold.issues].join() : ",";

function thresholdEdited() {
  $("save-threshold").disabled = !thresholdDirty() || !areasWritable;
  updateAreasDot();
}

function renderThreshold() {
  if (!thresholdTyped) {
    $("th-articles").value = birthThreshold?.articles ?? "";
    $("th-issues").value = birthThreshold?.issues ?? "";
  }
  $("th-articles").disabled = !areasWritable;
  $("th-issues").disabled = !areasWritable;
  thresholdEdited();
}

// Only a number that reaches the server is stored; the server refuses the rest by field.
async function saveThreshold() {
  const button = $("save-threshold");
  unrefuse($("threshold-block"));
  button.disabled = true;
  show("busy", "Saving…", "threshold-result");
  try {
    const data = await post("/api/areas/birth-threshold", {
      articles: Number($("th-articles").value),
      issues: Number($("th-issues").value),
    }, "PUT");
    birthThreshold = {articles: data.articles, issues: data.issues};
    thresholdTyped = false;
    show("ok", "Birth threshold saved. It only marks proposals on this page.", "threshold-result");
    renderAreas();
    renderThreshold();
  } catch (err) {
    if (err.field) refuse([$(`th-${err.field}`)], err.message);
    else show("err", err.message, "threshold-result");
    thresholdEdited();
  }
}

["th-articles", "th-issues"].forEach((id) => {
  $(id).addEventListener("input", (e) => {
    thresholdTyped = true;
    unrefuseEdited(e.target);
    thresholdEdited();
  });
});
$("save-threshold").addEventListener("click", saveThreshold);

loadAreas();

// Tracked topics: the same table and in-place editor as sources. An empty list
// is a valid answer, so Tracking never takes the missing dot.
let topics = [];
let topicsWritable = false;
// The model the next run embeds with: a new topic starts from it, and a topic
// set for another one is marked, because the run skips it.
let topicModel = "";
// Which editor is open: null for none, "" for a new topic, else its name.
let openTopic = null;

const NO_TOPIC_TABLE = "No writable topic table here — edit [[tracked_topics]] in cyris.toml instead.";

const topicRow = (name) => $("topic-body").querySelector(`tr.src-row[data-name="${CSS.escape(name)}"]`);

const trackingNav = () => document.querySelector('.settings-nav a[data-tab="tracking"]');

const topicProblem = (t) => topicModel && t.model !== topicModel
  ? `<span class="small feed-problem">${esc(`Set for ${t.model}; runs embed with ${topicModel}, so it is skipped`)}</span>`
  : "";

function renderTopics() {
  $("topic-body").innerHTML = topics.length
    ? topics.map((t) => `
      <tr class="src-row" data-name="${esc(t.name)}" tabindex="0">
        <td class="name">${esc(t.name)}${topicProblem(t)}</td>
        <td class="small">${esc(t.description)}</td>
        <td class="target">${esc(t.threshold)}</td>
        <td class="target">${esc(t.model)}</td>
      </tr>`).join("")
    : `<tr><td colspan="4" class="small">No topics yet.</td></tr>`;
  trackingNav().classList.remove("dirty");
  if (openTopic !== null) openTopicEditor(openTopic);
}

function openTopicEditor(name) {
  const adding = name === "";
  const t = adding
    ? {name: "", description: "", threshold: "", model: topicModel}
    : topics.find((x) => x.name === name);
  const anchor = adding ? null : topicRow(name);
  if (!adding && !(t && anchor)) {
    openTopic = null;
    return null;
  }
  const ed = $("topic-editor-tpl").content.firstElementChild.cloneNode(true);
  const q = (selector) => ed.querySelector(selector);
  if (anchor) {
    anchor.after(ed);
    anchor.classList.add("open");
  } else {
    $("topic-body").prepend(ed);
  }
  q(".editor-title").textContent = adding ? "New topic" : `Editing ${t.name}`;
  q("#t-name").value = t.name;
  q("#t-name").readOnly = !adding;
  q("#t-description").value = t.description;
  q("#t-threshold").value = t.threshold;
  q("#t-model").value = t.model;
  const save = q('[data-act="save"]'), remove = q('[data-act="remove"]');
  const values = () => JSON.stringify([...ed.querySelectorAll("input, textarea")].map((i) => i.value));
  const initial = values();
  const refreshEditor = () => {
    const dirty = values() !== initial;
    save.disabled = ed.inert || !topicsWritable || !dirty || !q("#t-name").value.trim();
    trackingNav().classList.toggle("dirty", dirty && topicsWritable);
  };
  const editorEdited = () => {
    if (topicsWritable) ed.querySelectorAll(".notice").forEach((n) => { n.hidden = true; });
    ed.querySelectorAll(".invalid").forEach(clearRefusal);
    refreshEditor();
  };
  ed.addEventListener("input", editorEdited);
  ed.addEventListener("change", editorEdited);
  refreshEditor();
  remove.hidden = adding;
  if (!topicsWritable) {
    remove.disabled = true;
    show("err", NO_TOPIC_TABLE, save.parentElement.querySelector(".notice"));
  }
  q('[data-act="cancel"]').addEventListener("click", () => {
    openTopic = null;
    renderTopics();
    (adding ? $("add-topic") : topicRow(name)).focus();
  });
  save.addEventListener("click", () => saveTopic(ed, save));
  // The spec's destructive confirm, as Retire does it.
  let armTimer;
  const disarm = () => {
    clearTimeout(armTimer);
    remove.classList.remove("armed");
    remove.textContent = "Remove";
  };
  remove.addEventListener("click", async () => {
    if (!remove.classList.contains("armed")) {
      remove.classList.add("armed");
      remove.textContent = "Confirm remove";
      armTimer = setTimeout(disarm, 3000);
      return;
    }
    disarm();
    const data = await writeSource(`/api/topics/${encodeURIComponent(t.name)}`, "DELETE", null,
                                   remove, "Removing…");
    if (!data) {
      returnFocus(remove);
      return;
    }
    openTopic = null;
    await loadTopics();
    show("ok", `${t.name} removed. ${data.note}`, "topics-notice");
    returnFocus($("add-topic"));
  });
  q("#t-name").focus();
  return ed;
}

async function saveTopic(ed, button) {
  const q = (selector) => ed.querySelector(selector);
  const threshold = q("#t-threshold").value;
  // Locked while the save is out, as a source editor is.
  ed.inert = true;
  const data = await writeSource("/api/topics", "POST", {
    name: q("#t-name").value.trim(),
    description: q("#t-description").value.trim(),
    // No number is not 0: the server refuses it rather than store a threshold nobody set.
    threshold: threshold === "" ? null : Number(threshold),
    model: q("#t-model").value.trim(),
  }, button, "Saving…");
  if (!data) {
    ed.inert = false;
    returnFocus(button);
    return;
  }
  openTopic = data.name;
  await loadTopics();
  const reopened = $("topic-body").querySelector("tr.editor");
  show("ok", `${data.name} saved. ${data.note}`,
       reopened ? reopened.querySelector(".notice") : "topics-notice");
}

$("topic-body").addEventListener("click", (e) => {
  const row = e.target.closest("tr.src-row");
  if (!row) return;
  const name = row.dataset.name;
  openTopic = openTopic === name ? null : name;
  renderTopics();
  if (openTopic === null) topicRow(name).focus();
});

$("topic-body").addEventListener("keydown", (e) => {
  if ((e.key === "Enter" || e.key === " ") && e.target.matches("tr.src-row")) {
    // Space would otherwise scroll the page.
    e.preventDefault();
    e.target.click();
  }
});

$("add-topic").addEventListener("click", () => {
  openTopic = "";
  renderTopics();
});

const loadTopics = () =>
  fetch("/api/topics", {redirect: "manual"})
    .then(parseAnswer)
    .then((d) => {
      topics = d.topics;
      topicsWritable = d.writable;
      topicModel = d.embedding_model;
      $("add-topic").disabled = !d.writable;
      if (!d.writable) show("err", NO_TOPIC_TABLE, "topics-notice");
      renderTopics();
    })
    .catch((e) => {
      $("topic-body").querySelector("[data-loading]")?.remove();
      $("add-topic").disabled = true;
      show("err", REFUSALS.includes(e) ? e.message
        : `Could not load topics: ${e}. Reload the page to try again.`, "topics-notice");
    });

loadTopics();

fetch("/api/settings", {redirect: "manual"})
  .then(parseAnswer)
  .then((d) => { state = d; render(); })
  .catch((e) => {
    // Every Save stays disabled: tracking only starts once the values are known.
    const text = REFUSALS.includes(e) ? e.message
      : `Could not load settings: ${e}. Reload the page to try again.`;
    SETTINGS_NOTICES.forEach((id) => show("err", text, id));
  })
  .finally(() => document.querySelectorAll("form.tab [data-loading]").forEach((el) => el.remove()));
