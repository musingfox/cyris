// The onboarding wizard: one step at a time, moved through with Back, Next and
// Skip, ending back on /settings. Three steps write settings, each through the
// route /settings saves that field with, so what lands in the store is what the
// settings form would have stored. The sources step shows the feed list's
// topics on one screen, then the chosen topics' feeds on the next, and writes
// each picked feed through POST /api/sources, the route the settings form adds
// a source with; a feed the list lacks is checked by its address first
// (POST /api/onboarding/feed-check) and then posted with the picks. The
// votes step deals cards from those feeds (POST /api/onboarding/cards) and
// writes the marked ones as votes (POST /api/onboarding/votes).
// The first-issue step turns vote similarity on through
// /api/settings/vote-similarity, the Model form's route, and starts a run through
// the app Worker's POST /run?period=&date=, the route any manual run takes, with
// the key GET /api/onboarding/first-issue picks, and reads the run's outcome back
// from that same route.
const $ = (id) => document.getElementById(id);

// The six steps, in order. Each one's hash opens it, so a reload keeps the place.
const STEPS = ["language", "schedule", "sources", "votes", "notify", "first-issue"];

// The settings written through /api/settings/values, by the id of their control.
// Each key and its route are src/cyris/settings_fields.json's own.
const PLAIN = {
  "o-output-language": "digest.output_language",
  "o-timezone": "general.timezone",
};
// The publish hours, written through /api/settings/schedule as one list.
const HOURS = ["o-morning", "o-evening"];
// The Discord webhook, written through /api/settings/notify.
const WEBHOOK = "o-discord-webhook";
// The mail pair, written through /api/settings/email, recipient first.
const MAIL = {"o-email-to": "notify.email_to", "o-email-from": "notify.email_from"};

const FIELDS = [...Object.keys(PLAIN), ...HOURS, WEBHOOK, ...Object.keys(MAIL)];

// What each control was loaded with: a step sends only what the reader changed,
// as /settings does, so Next over an untouched step stores nothing.
const loaded = new Map(FIELDS.map((id) => [id, $(id).value]));

// The result of a save, shown on the step the reader moves to.
let pending = null;
// Each setting's registry entry, by key, from GET /api/settings: a notice names
// a field by the same label /settings gives it.
let REGISTRY = {};
// The stored values GET /api/settings answered; null until it has. The
// first-issue step turns vote similarity on with the embedder stored here.
let VALUES = null;

// The sources step's second screen has a hash of its own (FEEDS_SCREEN), so a
// reload keeps the screen as well as the step.
const stepOf = () => {
  const hash = location.hash.slice(1);
  const step = hash === FEEDS_SCREEN ? "sources" : hash;
  return STEPS.includes(step) ? step : STEPS[0];
};

const hideNotice = () => { $("o-result").hidden = true; };

// A "busy" notice says what a save is doing while it is out; its result replaces it.
function show(kind, text) {
  const el = $("o-result");
  el.className = kind === "err" ? "notice err" : "notice";
  if (kind === "busy") el.setAttribute("aria-busy", "true");
  else el.removeAttribute("aria-busy");
  el.textContent = text;
  el.hidden = false;
}

// The opening (ui-language.md §6 onboarding) shows while the hash is empty: it
// is not a step, so a reload on a step keeps that step. Its stage runs three
// beats, overload, calm and claim, whose lengths are the §2 tokens; the motion
// inside each beat is a first cut, and its finer shape is still to be designed.
const REDUCED = matchMedia("(prefers-reduced-motion: reduce)");
const onOpening = () => location.hash.slice(1) === "";

// The sample headlines, the server's grade-A pool. A failed read leaves the
// stage without headlines; the opening still ends on its sentence and button.
const HEADLINES = fetch("/api/onboarding/headlines")
  .then((r) => (r.ok ? r.json() : {headlines: []}))
  .then((data) => data.headlines || [])
  .catch(() => []);

function createStage() {
  const canvas = $("o-canvas");
  const logo = $("o-logo");
  const cx = canvas.getContext("2d");
  const root = getComputedStyle(document.documentElement);
  const token = (name) => root.getPropertyValue(name).trim();
  const seconds = (name) => {
    const value = token(name);
    return value.endsWith("ms") ? parseFloat(value) / 1000 : parseFloat(value);
  };
  const C = {elev: token("--bg-elev"), border: token("--border"),
    text: token("--text"), dim: token("--text-dim")};
  const SANS = '"Geist", "PingFang TC", "Noto Sans TC", system-ui, sans-serif';
  const MONO = '"Geist Mono", ui-monospace, monospace';
  // Depth is u = ln z: a headline at depth z is drawn at 1/z of its size, so
  // equal steps in u read as equal steps in time, and the calm lane is a
  // lattice in u moving toward the reader at one slow speed.
  const ROWS = 3;
  const GAP = 0.3;
  const U_FAR = Math.log(9);
  const LANE_SPEED = 0.15;
  const JOINING = 5;
  const clamp = (x) => Math.max(0, Math.min(1, x));
  const ease = (x) => x * x * (3 - 2 * x);
  let seed = 3;
  const rnd = () => (seed = (seed * 16807) % 2147483647) / 2147483647;

  let W = 0, H = 0, raf = 0, last = 0, t = 0, thrown = 0;
  let pool = [], chaos = [], lane = [], rows = [], lanePos = 0, turned = false;
  let over = 3.6, calm = 2.9, onEnd = () => {}, ended = false, started = false;
  const beat = (name) => { $("o-opening").dataset.beat = name; };

  function size() {
    const r = canvas.getBoundingClientRect();
    if (!r.width || !r.height) return false;
    const dpr = Math.min(2, devicePixelRatio || 1);
    if (W !== r.width || H !== r.height) {
      W = r.width;
      H = r.height;
      canvas.width = Math.round(W * dpr);
      canvas.height = Math.round(H * dpr);
    }
    return true;
  }
  function geo() {
    // Canvas text follows the reader's type size, as every font-size does (§3).
    const scale = parseFloat(token("--type-scale")) || 1;
    const fs = (W >= 600 ? 14 : 12.5) * scale;
    const rowH = W >= 600 ? 34 : 30;
    const shelf = H - ROWS * rowH;
    const vy = Math.min(H * 0.36, shelf * 0.5);
    return {fs, row: 14 * scale, tag: 10 * scale, rowH, shelf, vx: W / 2, vy, yR: shelf - fs * 2};
  }
  function cut(text, f, maxW) {
    cx.font = `500 ${f}px ${SANS}`;
    if (cx.measureText(text).width <= maxW) return text;
    const chars = [...text];
    while (chars.length > 2 && cx.measureText(chars.join("") + "…").width > maxW) chars.pop();
    return chars.join("") + "…";
  }
  // A title already on screen is not drawn again until it leaves (§6), so the
  // pool's size bounds how many headlines the stage holds at once.
  function fresh() {
    const shown = new Set([...chaos, ...lane, ...rows].map((h) => h.key));
    const left = pool.filter((h) => !shown.has(h.title));
    if (!left.length) return null;
    const h = left[Math.floor(rnd() * left.length)];
    return {key: h.title, title: h.title, feed: h.feed};
  }

  // ---- overload: headlines thrown at the reader from the vanishing point ----
  function throwOne(z) {
    const h = fresh();
    if (!h) return;
    const m = Math.min(W, H);
    chaos.push({...h, z, ang: rnd() * Math.PI * 2, rho: m * (0.1 + 0.5 * Math.sqrt(rnd())),
      sz: 0.85 + rnd() * 0.4, bright: rnd() < 0.3, fade: 1});
  }
  function chaosAt(it, g) {
    const s = 1 / it.z;
    const f = g.fs * s * it.sz;
    return {x: g.vx + Math.cos(it.ang) * it.rho * s, y: g.vy + Math.sin(it.ang) * it.rho * s * 0.8,
      f, a: clamp((7.5 - it.z) / 2.5) * clamp((60 - f) / 26) * it.fade};
  }

  // ---- calm: one lane, evenly spaced, at one slow speed ----
  const taken = (q) => lane.some((it) => Math.abs(it.q - q) < 1e-6);
  function laneItem(h, q, g) {
    return {...h, q, title: cut(h.title, g.fs * 1.5, W - 32), fi: 0};
  }
  function fillLane(g, chance) {
    const far = Math.floor((U_FAR + lanePos) / GAP);
    for (let s = Math.ceil((0.25 + lanePos) / GAP); s <= far; s++) {
      if (taken(s * GAP) || rnd() > chance) continue;
      const h = fresh();
      if (h) lane.push(laneItem(h, s * GAP, g));
    }
  }
  function laneAt(it, g) {
    const u = it.q - lanePos;
    const s = Math.exp(-u);
    const p = {x: g.vx, y: g.vy + (g.yR - g.vy) * s, f: g.fs * s,
      a: clamp((U_FAR - u) / 0.8) * clamp(u / 0.25) * it.fi};
    if (!it.from) return p;
    const e = ease(it.j);
    return {x: it.from.x + (p.x - it.from.x) * e, y: it.from.y + (p.y - it.from.y) * e,
      f: it.from.f + (p.f - it.from.f) * e, a: it.from.a * (1 - e) + p.a * e};
  }
  // The turn: a few headlines in flight join the lane, the rest fade where they are.
  function turn(g) {
    turned = true;
    const s0 = Math.ceil((0.4 + lanePos) / GAP);
    const joining = chaos.filter((it) => it.z > 1.3 && it.z < 6).sort((a, b) => a.z - b.z)
      .slice(0, JOINING);
    joining.forEach((it, i) => {
      const item = laneItem(it, (s0 + i) * GAP, g);
      lane.push({...item, fi: 1, from: chaosAt(it, g), j: 0});
    });
    chaos = chaos.filter((it) => !joining.includes(it));
  }
  // What reaches the reader: a headline comes forward and rests in front as a row.
  function bringForward(g) {
    const near = chaos.filter((it) => it.fade > 0.2).map((it) => [it, chaosAt(it, g)])
      .filter(([, p]) => p.a > 0.1 && Math.abs(p.x - g.vx) < W * 0.35).sort((a, b) => b[1].f - a[1].f)[0];
    if (near) chaos.splice(chaos.indexOf(near[0]), 1);
    const h = near ? near[0] : fresh();
    if (!h) return false;
    const from = near ? near[1] : {x: g.vx, y: g.vy, f: 2, a: 0};
    rows.push({key: h.key, title: cut(h.title, g.row, W - 140), feed: h.feed, from, k: 0});
    return true;
  }

  function tick(dt) {
    t += dt;
    const g = geo();
    if (t < over) {
      beat("overload");
      const p = t / over;
      for (thrown += dt * (10 + 40 * p ** 1.6); thrown >= 1; thrown--) throwOne(7 + rnd() * 1.5);
    } else {
      if (!turned) turn(g);
      if (!ended) beat("calm");
      lanePos += LANE_SPEED * dt;
      fillLane(g, 0.85);
      const at = (t - over) / calm;
      while (rows.length < ROWS && at >= 0.45 + rows.length * 0.14 && bringForward(g));
      if (t >= over + calm) end();
    }
    const k = t < over ? 0.8 + 2.2 * clamp(t / over) ** 1.4 : 3 * (1 - 0.8 * ease(clamp((t - over) / calm)));
    for (const it of chaos) {
      it.z *= Math.exp(-k * dt);
      if (turned) it.fade = Math.max(0, it.fade - dt / 0.7);
    }
    chaos = chaos.filter((it) => {
      const p = chaosAt(it, g);
      return it.z > 0.12 && p.f < 60 && it.fade > 0 && Math.abs(p.x - g.vx) < W / 2 + p.f * 12;
    });
    for (const it of lane) {
      it.fi = Math.min(1, it.fi + dt / 0.6);
      if (it.from) it.j = Math.min(1, it.j + dt / 1.5);
    }
    lane = lane.filter((it) => it.q - lanePos > 0);
    for (const r of rows) r.k = Math.min(1, r.k + dt / 0.9);
  }

  function headline(it, p) {
    if (p.a <= 0.01 || p.f < 2.5) return;
    cx.globalAlpha = Math.min(1, p.a);
    cx.font = `${it.bright ? 600 : 500} ${p.f}px ${SANS}`;
    cx.fillStyle = it.bright || it.q !== undefined ? C.text : C.dim;
    cx.fillText(it.title, p.x, p.y);
    if (p.f >= 7) {
      cx.font = `500 ${p.f * 0.62}px ${MONO}`;
      cx.fillStyle = C.dim;
      cx.fillText(it.feed, p.x, p.y - p.f * 1.15);
    }
  }
  function drawRows(g) {
    rows.forEach((r, i) => {
      const y = g.shelf + i * g.rowH, h = g.rowH - 4, ty = y + h / 2 + 4, e = ease(r.k);
      cx.globalAlpha = e;
      cx.fillStyle = C.elev;
      cx.fillRect(0, y, W, h);
      cx.strokeStyle = C.border;
      cx.lineWidth = 1;
      cx.strokeRect(0.5, y + 0.5, W - 1, h - 1);
      cx.textAlign = "left";
      cx.font = `500 ${g.row}px ${SANS}`;
      cx.fillStyle = C.text;
      cx.fillText(r.title, 12, ty);
      cx.textAlign = "right";
      cx.font = `500 ${g.tag}px ${MONO}`;
      cx.fillStyle = C.dim;
      cx.fillText(cut(r.feed, g.tag, 100).toUpperCase(), W - 12, ty);
      cx.textAlign = "center";
      if (e < 1) {
        cx.globalAlpha = (1 - e) * Math.max(0.6, r.from.a);
        cx.font = `600 ${r.from.f + (g.row - r.from.f) * e}px ${SANS}`;
        cx.fillStyle = C.text;
        cx.fillText(r.title, r.from.x + (W / 2 - r.from.x) * e, r.from.y + (ty - r.from.y) * e);
      }
    });
    cx.globalAlpha = 1;
  }
  function draw() {
    if (!size()) return;
    const g = geo();
    const dpr = canvas.width / W;
    cx.setTransform(dpr, 0, 0, dpr, 0, 0);
    cx.globalAlpha = 1;
    cx.clearRect(0, 0, W, H);
    const calmed = ended ? 1 : clamp((t - over) / calm);
    const laneA = ease(clamp(calmed * 2));
    if (laneA > 0.01) {
      const spread = Math.min(W * 0.42, 300);
      cx.globalAlpha = laneA;
      cx.strokeStyle = C.border;
      cx.lineWidth = 1;
      cx.beginPath();
      cx.moveTo(g.vx, g.vy);
      cx.lineTo(g.vx - spread, g.shelf);
      cx.moveTo(g.vx, g.vy);
      cx.lineTo(g.vx + spread, g.shelf);
      cx.stroke();
    }
    cx.textAlign = "center";
    cx.textBaseline = "alphabetic";
    const drawn = [...chaos.map((it) => [it, chaosAt(it, g)]), ...lane.map((it) => [it, laneAt(it, g)])];
    drawn.sort((a, b) => a[1].f - b[1].f).forEach(([it, p]) => headline(it, p));
    cx.globalAlpha = 1;
    drawRows(g);
    logo.style.top = `${g.vy}px`;
    logo.style.opacity = String(ease(clamp(calmed / 0.4)));
  }

  function frame(ts) {
    const dt = last ? Math.min(0.05, (ts - last) / 1000) : 0;
    last = ts;
    if (size()) tick(dt);
    draw();
    raf = requestAnimationFrame(frame);
  }
  function run() {
    if (!raf && !REDUCED.matches) {
      last = 0;
      raf = requestAnimationFrame(frame);
    }
  }
  function end() {
    if (ended) return;
    ended = true;
    beat("claim");
    onEnd();
  }
  // The end of the third beat, drawn at once: the logo, the lane, the rows in
  // front. Under reduced motion it is drawn once and stays still.
  function settle() {
    size();
    const g = geo();
    t = over + calm;
    turned = true;
    chaos = [];
    lane = lane.map((it) => ({...it, fi: 1, from: null}));
    rows.forEach((r) => { r.k = 1; });
    fillLane(g, 0.85);
    while (rows.length < ROWS && bringForward(g));
    rows.forEach((r) => { r.k = 1; });
    end();
    draw();
  }

  return {
    play(then) {
      onEnd = then;
      over = seconds("--t-opening-overload");
      calm = seconds("--t-opening-calm");
      t = 0; thrown = 0; chaos = []; lane = []; rows = []; lanePos = 0;
      turned = false; ended = false; started = true;
      beat("overload");
      if (REDUCED.matches) settle();
      run();
    },
    finish() {
      if (!started || ended) return;
      settle();
      run();
    },
    stop() {
      cancelAnimationFrame(raf);
      raf = 0;
      started = false;
    },
    redraw() { if (!raf) draw(); },
    // The pool arrives whenever its request answers: a beat still running draws
    // from it at once, and an end already drawn is drawn again with it.
    supply(headlines) {
      pool = headlines;
      if (started && ended) settle();
    },
    // The reader changed the preference mid-page: turned on, the running loop
    // stops and the end is drawn still, whichever beat it was in; turned off,
    // the lane moves again.
    motionChanged() {
      if (!started) return;
      if (REDUCED.matches) {
        cancelAnimationFrame(raf);
        raf = 0;
        settle();
      } else run();
    },
  };
}

const STAGE = createStage();
new ResizeObserver(() => STAGE.redraw()).observe($("o-canvas"));
REDUCED.addEventListener("change", () => STAGE.motionChanged());

// The sentence, then Show me how; Skip intro leaves, and a keyboard reader on
// it is handed to Show me how rather than to a button no longer there.
function claim() {
  const skip = $("o-skip-intro");
  const had = document.activeElement === skip;
  $("o-claim").removeAttribute("data-held");
  skip.hidden = true;
  $("o-show").hidden = false;
  if (had) $("o-show").focus();
}

// The opening never waits on its headlines: they are optional samples, and a
// request stalled behind a waking container must not keep a reader out of the
// wizard. The beats start at once and draw from the pool once it arrives.
function openOpening() {
  $("o-claim").setAttribute("data-held", "");
  $("o-show").hidden = true;
  $("o-skip-intro").hidden = REDUCED.matches;
  STAGE.play(claim);
}

HEADLINES.then((pool) => STAGE.supply(pool));

$("o-skip-intro").addEventListener("click", () => {
  STAGE.finish();
  claim();
});

// Show me how saves nothing; it opens the first step and hands focus to its
// heading, so the reader is not left on a button that has gone.
let focusStep = false;
$("o-show").addEventListener("click", () => {
  focusStep = true;
  location.hash = STEPS[0];
});

const STEP_CHROME = [".page-head", "#o-steps", ".step-nav"];

function route() {
  const opening = onOpening();
  $("o-opening").hidden = !opening;
  STEP_CHROME.forEach((selector) => { document.querySelector(selector).hidden = opening; });
  if (opening) {
    document.querySelectorAll(".step").forEach((panel) => { panel.hidden = true; });
    openOpening();
    return;
  }
  STAGE.stop();
  const step = stepOf();
  const index = STEPS.indexOf(step);
  document.querySelectorAll(".step").forEach((panel) => {
    panel.hidden = panel.dataset.step !== step;
  });
  document.querySelectorAll("#o-steps li").forEach((item, position) => {
    if (item.dataset.step === step) item.setAttribute("aria-current", "step");
    else item.removeAttribute("aria-current");
    item.classList.toggle("done", position < index);
  });
  $("o-progress").textContent = `Step ${index + 1} of ${STEPS.length}`;
  const last = index === STEPS.length - 1;
  $("o-back").hidden = index === 0;
  $("o-skip").hidden = false;
  // Nothing is left to save on the last step: its own button starts the run,
  // and Finish, the link out of the wizard, takes Next's place.
  $("o-next").hidden = last;
  $("o-next").disabled = false;
  $("o-start").hidden = true;
  $("o-finish").hidden = !last;
  hideNotice();
  if (pending) show("ok", pending);
  pending = null;
  window.scrollTo(0, 0);
  drawScreen();
  if (focusStep) {
    focusStep = false;
    const heading = document.querySelector(`.step[data-step="${step}"] h2`);
    heading.tabIndex = -1;
    heading.focus();
  }
  if (step === "votes") loadCards();
  if (last) loadIssue();
}

addEventListener("hashchange", route);

// A refusal pinned to a field is marked there until it is edited; the reason
// itself goes in the notice beside the buttons.
const mark = (ids) => ids.forEach((id) => {
  $(id).classList.add("invalid");
  $(id).setAttribute("aria-invalid", "true");
});

const unmark = (ids) => ids.forEach((id) => {
  $(id).classList.remove("invalid");
  $(id).removeAttribute("aria-invalid");
});

// The app Worker answers 401 once the session cookie has expired. Reloading
// /onboarding then serves the sign-in form, which returns here.
const SESSION_EXPIRED = new Error(
  "Your session has expired. Reload the page to sign in again.");

// Rejects with what the reader needs: the server's own explanation when it
// gives one, otherwise what happened and what to do next.
async function post(url, body) {
  let res;
  try {
    res = await fetch(url, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(body),
    });
  } catch (err) {
    throw new Error(`Could not reach cyris (${err.message}). Check the connection and try again.`);
  }
  if (res.status === 401) throw SESSION_EXPIRED;
  const data = await res.json().catch(() => ({}));
  if (data.ok) return data;
  const err = new Error(data.error || `cyris answered ${res.status} without saying why. ` +
    "Try again; if it keeps failing, check the server log.");
  // The setting the refusal is about, for the page to mark its field.
  err.field = data.field;
  err.data = data;
  throw err;
}

const changed = (ids) => ids.filter((id) => $(id).value !== loaded.get(id));
const keep = (ids) => ids.forEach((id) => loaded.set(id, $(id).value));
const fieldOf = (ids, keys, field) => ids.filter((id) => keys[id] === field);
// The registry's label, or the key itself when the settings read never answered.
const labelOf = (key) => REGISTRY[key]?.label ?? key;

// Each save answers the line to show when it stored something, "" when the step
// was left as it was loaded, or throws with what to tell the reader.
async function saveLanguage() {
  const ids = changed(Object.keys(PLAIN));
  if (!ids.length) return "";
  // Trimmed, as the settings form's own plain fields are.
  const values = Object.fromEntries(ids.map((id) => [PLAIN[id], $(id).value.trim()]));
  try {
    const data = await post("/api/settings/values", {values});
    keep(ids);
    // Stored now, so no longer only detected.
    if (ids.includes("o-timezone")) $("o-timezone-detected").hidden = true;
    const parts = ids.map((id) => `${labelOf(PLAIN[id])}: ${data.values[PLAIN[id]]}`);
    return `${parts.join(", ")}. ${data.note}`;
  } catch (err) {
    mark(err.field ? fieldOf(ids, PLAIN, err.field) : ids);
    throw err;
  }
}

async function saveSchedule() {
  if (!changed(HOURS).length) return "";
  // An empty hour is refused here: padded, it would reach the server as 00:00.
  const empty = HOURS.filter((id) => $(id).value === "");
  if (empty.length) {
    mark(empty);
    const names = empty.map((id) => $(id).labels[0].textContent).join(" and ");
    throw new Error(
      `${names} ${empty.length > 1 ? "need" : "needs"} a whole hour from 0 to 23.`);
  }
  const times = HOURS.map((id) => `${String($(id).value).padStart(2, "0")}:00`);
  try {
    const data = await post("/api/settings/schedule", {times});
    keep(HOURS);
    return `Publish hours: ${data.times.join(" and ")}. ${data.note}`;
  } catch (err) {
    mark(HOURS);
    throw err;
  }
}

// Two routes, as the settings form's Notifications has: the webhook and the
// mail pair, each tried on its own. An empty field is nothing to save rather
// than an off switch — turning notifications off is its own action, on /settings.
async function saveNotify() {
  const lines = [], problems = [];
  if (changed([WEBHOOK]).length && $(WEBHOOK).value.trim()) {
    show("busy", "Checking with Discord…");
    try {
      // Sent as typed, as the settings form sends it; the server trims it.
      const data = await post("/api/settings/notify", {discord_webhook_url: $(WEBHOOK).value});
      keep([WEBHOOK]);
      lines.push(`${data.detail} ${data.note}`);
    } catch (err) {
      mark([WEBHOOK]);
      problems.push(err.message);
    }
  }
  const mail = Object.keys(MAIL);
  const [to, from] = mail.map((id) => $(id).value);
  if (changed(mail).length && !to.trim() && from.trim()) {
    // The mirror of the server's own refusal of a recipient without a sender:
    // with nowhere to send it, the pair is not a setting yet.
    mark([mail[0]]);
    problems.push("Enter the address the digest is sent to, too.");
  } else if (changed(mail).length && to.trim()) {
    show("busy", "Sending a test message…");
    try {
      const data = await post("/api/settings/email", {email_to: to, email_from: from});
      keep(mail);
      lines.push(data.detail ? `${data.detail} ${data.note}` : data.note);
    } catch (err) {
      mark(err.field ? fieldOf(mail, MAIL, err.field) : mail);
      problems.push(err.message);
    }
  }
  if (problems.length) throw new Error(problems.join("\n"));
  return lines.join("\n");
}

// The topic tiles, one per topic GET /api/onboarding/feeds sent, in its order,
// and the feed list's rows, one per entry, in its order; both empty when the
// server offered none.
let TOPICS = [];
let FEEDS = [];
// The feeds the reader added by address, in the order added, rows like the
// list's: each one's entry is the checked feed, with no tags and no topics.
let OWN = [];
const OWN_LEGEND = "Your feeds";

// The topics chosen, kept for this tab so a reload opens the step on the same
// feeds. A feed's own pick is not kept: a reload starts it unchecked, as before.
const TOPICS_KEY = "cyris-onboarding-topics";

// Past this many picked feeds the Votes step waits more than a minute before it
// deals cards: its poll fetches four feeds at a time, each given 20 seconds
// (docs/architecture.md §5, Onboarding poll wait), so 12 is three batches.
const SLOW_PICKS = 12;

const chosenTopics = () => TOPICS.filter((row) => row.chosen).map((row) => row.topic.id);
const pickedFeeds = () => [...FEEDS, ...OWN].filter((row) => row.box.checked);

function storedTopics() {
  try {
    const ids = JSON.parse(sessionStorage.getItem(TOPICS_KEY) || "[]");
    return new Set(Array.isArray(ids) ? ids : []);
  } catch {
    return new Set();
  }
}

// A checklist row, as the feed list's rows all are: a checkbox, the name, and
// the label role on the right.
function checklistRow(name) {
  const label = document.createElement("label");
  label.className = "choice";
  const box = document.createElement("input");
  box.type = "checkbox";
  const title = document.createElement("span");
  title.className = "name";
  title.textContent = name;
  const side = document.createElement("span");
  side.className = "label";
  label.append(box, title, side);
  return {label, box, side};
}

function checklist(legend, rows) {
  const set = document.createElement("fieldset");
  set.className = "sources-check";
  const head = document.createElement("legend");
  head.className = "label";
  head.textContent = legend;
  set.append(head, ...rows.map((row) => row.label));
  return set;
}

// A topic's toggle tile: one press chooses it, the next unchooses it.
function topicTile(topic, count) {
  const tile = document.createElement("button");
  tile.type = "button";
  tile.className = "tile";
  const name = document.createElement("span");
  name.className = "name";
  name.textContent = topic.label;
  const side = document.createElement("span");
  side.className = "label";
  side.textContent = `${count} ${count === 1 ? "feed" : "feeds"}`;
  tile.append(name, side);
  return tile;
}

function setChosen(row, chosen) {
  row.chosen = chosen;
  row.tile.setAttribute("aria-pressed", String(chosen));
}

// Where a picked feed goes once none of its topics is chosen: it stays checked
// and shown, so Next never sends a feed the reader can no longer see.
const KEPT = "Picked, outside the chosen topics";

// The chosen topics' feeds, one checklist per topic in the topic list's order.
// A feed goes under the first of its own topics that is chosen, so one in two
// chosen topics shows once; a picked feed in none of them goes under KEPT,
// first, rather than vanish with its pick.
function drawFeeds() {
  const chosen = chosenTopics();
  const groups = new Map(chosen.map((id) => [id, []]));
  const kept = [];
  for (const row of FEEDS) {
    const home = row.entry.topics.find((id) => groups.has(id));
    if (home) groups.get(home).push(row);
    else if (row.box.checked) kept.push(row);
  }
  const sets = kept.length ? [checklist(KEPT, kept)] : [];
  for (const row of TOPICS) {
    const rows = groups.get(row.topic.id);
    if (rows && rows.length) sets.push(checklist(row.topic.label, rows));
  }
  $("o-feeds").replaceChildren(...sets);
  $("o-feeds").hidden = !sets.length;
  $("o-feeds-hint").hidden = chosen.length > 0;
  drawPicked();
  drawScreen();
}

// How many feeds Next sends, and past SLOW_PICKS, how long the next step waits
// for them. It warns; it does not stop the reader.
function drawPicked() {
  const count = pickedFeeds().length;
  const lines = [`${count} ${count === 1 ? "feed" : "feeds"} picked.`];
  if (count > SLOW_PICKS) {
    lines.push("The Votes step fetches every picked feed before it deals cards: " +
      `up to ${SLOW_PICKS} take about a minute, and more take longer.`);
  }
  $("o-picked").textContent = lines.join(" ");
}

// The sources step's two screens, by the hash: its topics, then their feeds.
const FEEDS_SCREEN = "sources/feeds";
const onFeeds = () => location.hash.slice(1) === FEEDS_SCREEN;

// Shows the screen the hash names, once the list is drawn. Next on the topics
// screen never waits for a topic: the feeds screen also holds the reader's own
// feed, which needs none.
function drawScreen() {
  if (stepOf() !== "sources" || !TOPICS.length) return;
  const screen = onFeeds() ? "feeds" : "topics";
  document.querySelectorAll('.step[data-step="sources"] [data-screen]').forEach((el) => {
    el.hidden = el.dataset.screen !== screen;
  });
}

// A row's state, shown on its right before its tags. An added feed's checkbox
// is disabled, and that is what keeps a retry from sending it again.
function setState(row, state) {
  row.state = state;
  row.side.textContent = [state, ...row.entry.tags].filter(Boolean).join(" · ");
  const added = state === "Added";
  row.box.disabled = added;
  row.label.classList.toggle("unavailable", added);
  if (state === "Not added") row.box.setAttribute("aria-invalid", "true");
  else row.box.removeAttribute("aria-invalid");
}

// The loading line goes only once the step is drawn, so a list that fails to
// draw leaves it in place for the reason.
function renderFeeds(data) {
  if (!data.feeds.length) {
    $("o-feeds-loading").remove();
    document.querySelectorAll('.step[data-step="sources"] [data-screen]').forEach((el) => {
      el.hidden = true;
    });
    $("o-feeds-had").hidden = false;
    return;
  }
  FEEDS = data.feeds.map((entry) => {
    // The list's own grouping is not a source's field: the body posted is the rest.
    const {topics, ...source} = entry;
    // The entry's own name and tags, as the list holds them.
    const row = {entry, source, ...checklistRow(entry.name)};
    row.box.addEventListener("change", () => {
      if (row.state === "Not added") setState(row, "");
      // An unchecked feed none of whose topics is chosen leaves the list.
      if (!row.box.checked && !topics.some((id) => chosenTopics().includes(id))) drawFeeds();
      else drawPicked();
    });
    setState(row, "");
    return row;
  });
  const stored = storedTopics();
  TOPICS = data.topics.map((topic) => {
    const count = FEEDS.filter((row) => row.entry.topics.includes(topic.id)).length;
    const row = {topic, tile: topicTile(topic, count)};
    setChosen(row, stored.has(topic.id));
    row.tile.addEventListener("click", () => {
      setChosen(row, !row.chosen);
      // Kept only for a reload: storage the browser refuses must not stop the redraw.
      try {
        sessionStorage.setItem(TOPICS_KEY, JSON.stringify(chosenTopics()));
      } catch {}
      drawFeeds();
    });
    return row;
  });
  $("o-topics").append(...TOPICS.map((row) => row.tile));
  drawFeeds();
  $("o-feeds-loading").remove();
}

// The reader's own feeds, under the list's: none until one is added.
function drawOwn() {
  $("o-own").replaceChildren(...(OWN.length ? [checklist(OWN_LEGEND, OWN)] : []));
  drawPicked();
  drawScreen();
}

// The add field's own notice, beside its button, as the step's is beside Next.
function showOwn(kind, text) {
  const el = $("o-own-result");
  el.className = kind === "err" ? "notice err" : "notice";
  if (kind === "busy") el.setAttribute("aria-busy", "true");
  else el.removeAttribute("aria-busy");
  el.textContent = text;
  el.hidden = false;
}

// A checked feed, as a row of the list: checked, since the reader asked for it,
// and posted as a source with its name and address, no tags and the default lane.
function ownRow({name, url}) {
  const entry = {name, url, tags: [], topics: []};
  const row = {entry, source: {name, url, tags: []}, ...checklistRow(name)};
  row.box.checked = true;
  row.box.addEventListener("change", () => {
    if (row.state === "Not added") setState(row, "");
    drawPicked();
  });
  setState(row, "");
  return row;
}

// Checks the address, then lists the feed under Your feeds. A feed already
// listed is not added twice: a row of the list is checked instead, under its
// topic or with the picks kept outside them.
async function addOwnFeed() {
  const input = $("o-own-url");
  const url = input.value.trim();
  unmark(["o-own-url"]);
  if (!url) {
    mark(["o-own-url"]);
    showOwn("err", "Paste a feed's address, or a site's home page, first.");
    return;
  }
  const rows = [...FEEDS, ...OWN];
  $("o-own-add").disabled = true;
  showOwn("busy", `Checking ${url}…`);
  try {
    const data = await post("/api/onboarding/feed-check", {
      url,
      taken: rows.map((row) => ({name: row.entry.name, url: row.entry.url ?? ""})),
    });
    OWN.push(ownRow(data));
    drawOwn();
    input.value = "";
    showOwn("ok", `${data.name} is under ${OWN_LEGEND}, checked: Next adds it with the others.`);
  } catch (err) {
    const listed = rows.find((row) => row.entry.name === err.data?.name);
    if (listed) {
      listed.box.checked = true;
      drawFeeds();
      drawOwn();
      input.value = "";
      showOwn("ok", `${listed.entry.name} is already listed, and is checked there.`);
      return;
    }
    mark(["o-own-url"]);
    showOwn("err", err.message);
  } finally {
    $("o-own-add").disabled = false;
  }
}

// Next waits for a check still out, so the feed it adds is among the picks it saves.
let checking = Promise.resolve();
function startCheck() {
  checking = addOwnFeed();
}

$("o-own-add").addEventListener("click", startCheck);
$("o-own-url").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !$("o-own-add").disabled) startCheck();
});
$("o-own-url").addEventListener("input", () => unmark(["o-own-url"]));

// One POST /api/sources per picked feed, each body the entry as listed less its
// topics, so the source stored is the list's own, tags and lane included; an own
// feed's body is its name and address. A feed
// already added is not sent again: Next after a partial failure retries only the
// ones that failed. Every picked feed is on the page, so none is sent unseen.
async function saveSources() {
  await checking;
  const picked = [...FEEDS, ...OWN].filter((row) => row.box.checked && !row.box.disabled);
  if (!picked.length) return "";
  const added = [], failed = [];
  let note = "";
  for (const [index, row] of picked.entries()) {
    show("busy", `Adding ${row.entry.name} (${index + 1} of ${picked.length})…`);
    try {
      ({note} = await post("/api/sources", row.source));
      setState(row, "Added");
      added.push(row.entry.name);
    } catch (err) {
      setState(row, "Not added");
      failed.push(`${row.entry.name}: ${err.message}`);
      // A lapsed sign-in fails every feed after it the same way.
      if (err === SESSION_EXPIRED) break;
    }
  }
  if (failed.length) {
    const which = failed.length === 1 ? "This one was" : "These were";
    throw new Error([
      `Added ${added.length} of ${picked.length} feeds. ${which} not added:`,
      ...failed,
      "Press Next to try again, or Skip to go on without them.",
    ].join("\n"));
  }
  // The cards were dealt from the feeds there were; these are new.
  if (added.length) cardsDealt = false;
  const feeds = added.length === 1 ? "feed" : "feeds";
  return `Added ${added.length} ${feeds}: ${added.join(", ")}. ${note}`;
}

// The vote cards, one per card POST /api/onboarding/cards dealt, in its order.
// A card's `vote` is "up", "down" or null, the untouched card; `recorded` is
// set once the server has written its vote, and the card no longer changes.
let CARDS = [];
// Whether the cards on the page are dealt from the feeds the table holds now;
// a sources save that adds a feed clears it, and the next visit deals again.
let cardsDealt = false;
let dealing = false;

const VOTE_WORDS = {up: "Interested", down: "Not interested"};

function setVote(card, vote) {
  card.vote = vote;
  for (const [value, button] of Object.entries(card.buttons)) {
    button.setAttribute("aria-pressed", String(value === vote));
  }
}

// A recorded card shows what was written in place of its buttons, which would
// otherwise offer to change a vote that is already stamped as the reader's.
function setRecorded(card) {
  card.recorded = true;
  card.seg.hidden = true;
  card.state.textContent = `Recorded · ${VOTE_WORDS[card.vote]}`;
  card.state.hidden = false;
}

// A card whose article left its feed before Next: nothing to record it on, so
// it is not sent again.
function setGone(card) {
  card.gone = true;
  card.seg.hidden = true;
  card.state.textContent = "No longer in the feed · not recorded";
  card.state.hidden = false;
}

function cardRow(card) {
  const row = document.createElement("div");
  row.className = "list-row vote-card";
  const source = document.createElement("span");
  source.className = "label";
  source.textContent = card.source;
  // The feed's own words, in the feed's own language.
  const title = document.createElement("a");
  title.className = "title";
  title.lang = "";
  title.textContent = card.title || card.url;
  // A feed decides this URL, so only a web address becomes a link.
  if (/^https?:\/\//i.test(card.url)) {
    title.href = card.url;
    title.target = "_blank";
    title.rel = "noopener";
  }
  row.append(source, title);
  if (card.excerpt) {
    const text = document.createElement("p");
    text.className = "small";
    text.lang = "";
    text.textContent = card.excerpt;
    row.append(text);
  }
  const seg = document.createElement("div");
  seg.className = "seg";
  seg.setAttribute("role", "group");
  seg.setAttribute("aria-label", `Vote on ${card.title || card.url}`);
  const state = document.createElement("span");
  state.className = "label";
  state.hidden = true;
  const entry = {card, row, seg, state, buttons: {}, vote: null, recorded: false};
  for (const [vote, word] of Object.entries(VOTE_WORDS)) {
    const button = document.createElement("button");
    button.type = "button";
    button.dataset.vote = vote;
    button.textContent = word;
    // A second press on the pressed answer takes it back: the card is untouched again.
    button.addEventListener("click", () => setVote(entry, entry.vote === vote ? null : vote));
    entry.buttons[vote] = button;
    seg.append(button);
  }
  setVote(entry, null);
  row.append(seg, state);
  return entry;
}

// Each source the read found nothing for, named, and why the poll before it
// failed when it did: both say why a feed the reader picked shows no card.
function renderCards(data) {
  CARDS = data.cards.map(cardRow);
  $("o-cards").replaceChildren(...CARDS.map((card) => card.row));
  $("o-cards").hidden = !CARDS.length;
  const problems = [];
  if (data.empty.length) {
    const which = data.empty.length === 1 ? "this feed" : `these ${data.empty.length} feeds`;
    problems.push(`No articles came back from ${which}: ${data.empty.join(", ")}. ` +
      "They stay in your sources and the first issue reads them again; " +
      "check them under Sources in Settings.");
  }
  if (data.poll_error) {
    problems.push(`The feeds could not be fetched just now (${data.poll_error}), ` +
      "so these cards are only what had been fetched before.");
  }
  $("o-cards-empty").textContent = problems.join("\n");
  $("o-cards-empty").hidden = !problems.length;
  let none = "";
  if (!data.sources) {
    none = "There are no feeds to draw from yet. Press Back to pick some, or Next to go on.";
  } else if (!CARDS.length) {
    none = "There is no article to vote on yet. Press Next to go on: the first issue works without votes.";
  }
  $("o-cards-none").textContent = none;
  $("o-cards-none").hidden = !none;
}

// Deals once per set of feeds: a reload or a visit after new feeds deals
// again, a Back and Next over the same feeds keeps the marks made.
async function loadCards() {
  if (cardsDealt || dealing) return;
  dealing = true;
  const loading = $("o-cards-loading");
  loading.textContent = "Fetching the latest articles from your feeds. This can take a minute or two…";
  loading.setAttribute("aria-busy", "true");
  loading.hidden = false;
  try {
    renderCards(await post("/api/onboarding/cards", {}));
    cardsDealt = true;
    loading.hidden = true;
  } catch (err) {
    loading.removeAttribute("aria-busy");
    loading.textContent = `${err.message} Press Skip to go on without votes, or reload the page to try again.`;
  } finally {
    dealing = false;
  }
}

// The marked cards not yet written. A card left alone is not sent, and nothing
// marked is a Next that writes nothing, as a Skip is.
async function saveVotes() {
  const marked = CARDS.filter((card) => card.vote && !card.recorded && !card.gone);
  const lines = [];
  if (marked.length) {
    show("busy", `Recording ${marked.length} ${marked.length === 1 ? "vote" : "votes"}…`);
    const data = await post("/api/onboarding/votes", {
      votes: marked.map((card) => ({url: card.card.url, vote: card.vote})),
    });
    const gone = new Set(data.missing);
    marked.forEach((card) => (gone.has(card.card.url) ? setGone : setRecorded)(card));
    const kept = data.accepted.length + data.rejected.length;
    lines.push(`Recorded ${kept} ${kept === 1 ? "vote" : "votes"}: ` +
      `${data.accepted.length} interested, ${data.rejected.length} not interested.`);
    if (gone.size) {
      lines.push(`${gone.size} ${gone.size === 1 ? "article has" : "articles have"} left ` +
        "its feed since the cards were dealt, so that vote was not recorded.");
    }
  }
  return lines.join("\n");
}

// The first-issue step's state: "loading" while the route is read, "ready" with
// a run to start, "closed" with none to start, then "running", "done" or "failed".
let issueState = "loading";
// The route's last answer, and the run this page started while it is out.
let ISSUE = null;
let running = null;
// Redraws the step's notice, which a step change clears. A state with no notice
// of its own leaves the one shown, such as the result of the step before.
let issueNotice = () => {};

// One read every few seconds while a run is out: the run takes minutes.
const POLL_MS = 5000;
// The run container's own cap (RUN_SLEEP_AFTER in workers/app/src/index.js):
// a run with no row by then is not coming.
const RUN_WAIT_MS = 15 * 60 * 1000;
// The run this tab started, kept so a reload goes on waiting for it rather than
// offering to start a second one.
const RUNNING_KEY = "cyris-onboarding-first-issue";

// What each way a run can end, other than with its issue published, tells the reader.
const RUN_ENDS = {
  publish_failed: "The issue was built, but publishing it to the archive failed.",
  no_articles: "None of your feeds had an article in the window the run reads.",
  no_pending: "Every article in the window was already decided, so the run had nothing new.",
  error: "The run stopped with an error.",
};

function drawIssueNav() {
  if (stepOf() !== STEPS[STEPS.length - 1]) return;
  const open = ["loading", "ready"].includes(issueState);
  $("o-back").hidden = issueState === "running";
  $("o-skip").hidden = !open;
  $("o-start").hidden = !["ready", "failed"].includes(issueState);
  $("o-start").textContent = issueState === "failed" ? "Try again" : "Start the first issue";
  $("o-finish").hidden = open;
}

function setIssue(state, notice = () => {}) {
  issueState = state;
  issueNotice = notice;
  drawIssueNav();
  if (stepOf() === STEPS[STEPS.length - 1]) issueNotice();
}

function plan(text) {
  $("o-issue-plan").textContent = text;
  $("o-issue-plan").hidden = !text;
}

// A link the run's own answer names, so only a web address becomes one.
function link(text, href) {
  const a = document.createElement("a");
  a.textContent = text;
  if (/^https?:\/\//i.test(href)) a.href = href;
  return a;
}

async function getJson(url) {
  let res;
  try {
    res = await fetch(url);
  } catch (err) {
    throw new Error(`Could not reach cyris (${err.message}). Check the connection and try again.`);
  }
  if (res.status === 401) throw SESSION_EXPIRED;
  const data = await res.json().catch(() => ({}));
  if (data.ok) return data;
  throw new Error(data.error || `cyris answered ${res.status} without saying why. ` +
    "Reload the page to try again; if it keeps failing, check the server log.");
}

// Says what Start would do with the route's answer, and whether there is a run
// to start. A retry may replace the issue its own failed run stored.
function planIssue(data, retry) {
  if (!data.sources) {
    plan("There are no sources yet, so there is nothing to build an issue from. " +
      "Press Back to pick some, or Finish and add them later under Sources in Settings.");
    return false;
  }
  if (data.published && !retry) {
    plan(`The ${data.date} ${data.period} issue is already published, and one built now ` +
      "would replace it. Read it in the archive.");
    return false;
  }
  if (data.date !== data.today) {
    // Today's two hours are both still to come, and each would replace an
    // issue dated today, so the first one is dated yesterday.
    plan(`Start builds the ${data.date} ${data.period} issue now, dated yesterday: ` +
      `neither of today's publish hours, ${data.hours.join(" and ")} ${data.timezone}, has ` +
      "begun, and an issue dated today would be replaced by them. It takes a few minutes.");
    return true;
  }
  plan(`Start builds the ${data.date} ${data.period} issue now. It takes a few minutes, ` +
    "and no later issue today replaces it.");
  return true;
}

// Vote similarity, without which no vote changes an issue, is turned on once
// there is a source to build from, whether or not a card was voted on: the votes
// cast on the issues to come count only with it on. It is not the reader's
// choice (docs/milestones/cold-start-onboarding.md), so no Skip keeps it off,
// and a refusal leaves Start in reach. A refused save is tried again by the
// next visit or Start.
let voteSimilarity = null;

async function turnOnVoteSimilarity() {
  await SETTINGS_LOADED;
  if (!VALUES) throw new Error("The settings did not load. Reload the page.");
  if (VALUES["vote_similarity.enabled"] === true) return;
  // The embedder this deployment already names: which model it embeds with is
  // the deployer's choice, under Model in Settings, and the wizard leaves it.
  const data = await post("/api/settings/vote-similarity", {
    enabled: true,
    provider: VALUES["vote_similarity.provider"],
    model: VALUES["vote_similarity.model"] ?? "",
    max_seeds: VALUES["vote_similarity.max_seeds"],
  });
  Object.assign(VALUES, data.values);
}

function voteSimilarityOn() {
  const line = $("o-vote-similarity");
  voteSimilarity ??= turnOnVoteSimilarity().then(
    () => { line.hidden = true; },
    (err) => {
      voteSimilarity = null;
      line.textContent = `Vote similarity is still off, so votes have no effect yet: ${err.message} ` +
        "Turn it on under Model in Settings.";
      line.hidden = false;
    });
  return voteSimilarity;
}

// Read on every visit to the step, so the plan is the one for the time it is
// started at; a run already out is waited on instead.
async function loadIssue() {
  if (["running", "done", "failed"].includes(issueState)) return setIssue(issueState, issueNotice);
  setIssue("loading");
  $("o-issue-loading").hidden = false;
  let data;
  try {
    data = await getJson("/api/onboarding/first-issue");
  } catch (err) {
    plan("");
    return setIssue("closed", () => show("err", err.message));
  } finally {
    $("o-issue-loading").hidden = true;
  }
  ISSUE = data;
  if (data.sources) voteSimilarityOn();
  const saved = JSON.parse(sessionStorage.getItem(RUNNING_KEY) || "null");
  if (saved && Date.now() - saved.started < RUN_WAIT_MS) {
    plan(`The ${saved.date} ${saved.period} issue was started from this page.`);
    return waitForRun(saved);
  }
  setIssue(planIssue(data, false) ? "ready" : "closed");
}

// The app Worker's own manual run: it answers once the run container is starting.
// The date goes with it, so a run that ends after midnight keeps its key.
async function startRun(period, date) {
  let res;
  try {
    const query = `period=${encodeURIComponent(period)}&date=${encodeURIComponent(date)}`;
    res = await fetch(`/run?${query}`, {method: "POST"});
  } catch (err) {
    throw new Error(`Could not reach cyris (${err.message}). Check the connection and try again.`);
  }
  if (res.status === 401) throw SESSION_EXPIRED;
  const data = await res.json().catch(() => ({}));
  if (res.ok && data.started) return data;
  throw new Error(`The run did not start: ${data.error || `cyris answered ${res.status}`}.`);
}

function failIssue(text) {
  sessionStorage.removeItem(RUNNING_KEY);
  running = null;
  setIssue("failed", () => show("err", [
    text,
    "Press Try again, or Finish to leave: the next publish hour brings an issue either way.",
  ].join("\n")));
}

// The plan is read again first: the period is the one for the time the run
// starts at, and the run is told from earlier ones by its row's id, which only grows.
async function startIssue() {
  const retry = issueState === "failed";
  $("o-start").disabled = true;
  show("busy", "Starting the run…");
  try {
    ISSUE = await getJson("/api/onboarding/first-issue");
    if (!planIssue(ISSUE, retry)) {
      hideNotice();
      setIssue("closed");
      return;
    }
    await voteSimilarityOn();
    await startRun(ISSUE.period, ISSUE.date);
  } catch (err) {
    failIssue(err.message);
    return;
  } finally {
    $("o-start").disabled = false;
  }
  const run = {
    date: ISSUE.date, period: ISSUE.period, after: ISSUE.run?.id ?? 0, started: Date.now(),
  };
  sessionStorage.setItem(RUNNING_KEY, JSON.stringify(run));
  waitForRun(run);
}

function elapsed(since) {
  const seconds = Math.floor((Date.now() - since) / 1000);
  return `${Math.floor(seconds / 60)}m ${String(seconds % 60).padStart(2, "0")}s`;
}

async function waitForRun(run) {
  running = run;
  const busy = () => show("busy", `Building the ${run.date} ${run.period} issue: ` +
    `${elapsed(run.started)} so far. It usually takes a few minutes. You can leave with ` +
    "Finish: the issue still reaches the archive.");
  setIssue("running", busy);
  while (running === run) {
    await new Promise((resolve) => setTimeout(resolve, POLL_MS));
    if (running !== run) return;
    let data;
    try {
      data = await getJson("/api/onboarding/first-issue");
    } catch (err) {
      // A missed read is read again; the run goes on regardless.
      if (err !== SESSION_EXPIRED) continue;
      return failIssue(err.message);
    }
    const row = data.run;
    if (row && row.id > run.after && row.period === run.period) return endIssue(row, run);
    if (Date.now() - run.started > RUN_WAIT_MS) {
      return failIssue(`No run has ended in ${RUN_WAIT_MS / 60000} minutes, so this one ` +
        "did not start or stopped without a record. Its log is in the app Worker's logs.");
    }
    if (stepOf() === STEPS[STEPS.length - 1]) busy();
  }
}

function endIssue(row, run) {
  sessionStorage.removeItem(RUNNING_KEY);
  running = null;
  if (row.status !== "ok") {
    const why = [RUN_ENDS[row.status] || `The run ended as ${row.status}.`];
    if (row.error) why.push(row.error);
    if (row.failed_sources.length) why.push(`Fetching failed for: ${row.failed_sources.join(", ")}.`);
    return failIssue(why.join(" "));
  }
  setIssue("done", () => {
    const issue = `Your first issue, the ${run.date} ${run.period} issue,`;
    if (!row.digest_url) {
      show("ok", `${issue} is built. This deployment publishes no site, ` +
        "so it reached you only through its notifications.");
      return;
    }
    show("ok", `${issue} is published. `);
    $("o-result").append(
      link("Read it", row.digest_url), ", or find it with every later issue in the ",
      link("archive", new URL("/", row.digest_url).href), ".");
  });
}

const SAVES = {
  language: saveLanguage,
  schedule: saveSchedule,
  sources: saveSources,
  votes: saveVotes,
  notify: saveNotify,
};

// Only ever called from a step that has one after it: the last step shows
// its own Start and Finish in Next's place, and its Skip leaves.
function advance() {
  const index = STEPS.indexOf(stepOf());
  location.hash = STEPS[index + 1];
}

const moving = (on) => {
  ["o-back", "o-skip", "o-next"].forEach((id) => { $(id).disabled = on; });
};

$("o-next").addEventListener("click", async () => {
  // The topics screen saves nothing: its Next opens the chosen topics' feeds.
  if (stepOf() === "sources" && TOPICS.length && !onFeeds()) {
    location.hash = FEEDS_SCREEN;
    return;
  }
  const save = SAVES[stepOf()];
  if (!save) return advance();
  moving(true);
  unmark(FIELDS);
  show("busy", "Saving…");
  try {
    pending = await save();
    moving(false);
    hideNotice();
    advance();
  } catch (err) {
    moving(false);
    drawScreen();
    show("err", err.message);
  }
});

$("o-skip").addEventListener("click", () => {
  unmark(FIELDS);
  // Skipping the last step leaves through Finish itself, starting nothing.
  if (stepOf() === STEPS[STEPS.length - 1]) $("o-finish").click();
  else advance();
});

$("o-start").addEventListener("click", startIssue);

$("o-back").addEventListener("click", () => {
  unmark(FIELDS);
  // The feeds screen's Back returns to the topics, every check kept.
  if (onFeeds()) {
    location.hash = "sources";
    return;
  }
  const index = STEPS.indexOf(stepOf());
  if (index > 0) location.hash = STEPS[index - 1];
});

for (const id of FIELDS) {
  $(id).addEventListener("input", () => unmark([id]));
}

// The output languages, each stored as its BCP 47 tag and shown by the name
// languages.json gives it, in that file's order. A stored tag the list lacks is
// still a language the model is told to write in, so it stays an option of its
// own, shown as the tag, and Next over the untouched field stores nothing.
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
// and this browser's own when the list lacks them.
function fillTimezones(select, stored) {
  const known = typeof Intl.supportedValuesOf === "function" ? Intl.supportedValuesOf("timeZone") : [];
  const zones = new Set([...known, "UTC", stored, browserZone()].filter(Boolean));
  select.replaceChildren(...[...zones].sort().map((zone) => new Option(zone, zone)));
}

// A setting with no stored value opens on "Not set", which a select would
// otherwise show as its first option.
function openOn(select, stored) {
  if (stored != null) select.value = stored;
  else select.prepend(new Option("Not set", "", true, true));
}

// A zone the reader picked is no longer the detected one.
$("o-timezone").addEventListener("change", () => { $("o-timezone-detected").hidden = true; });

function render(state) {
  const values = state.values;
  VALUES = values;
  REGISTRY = state.fields;
  fillLanguages($("o-output-language"), state.languages, values["digest.output_language"]);
  openOn($("o-output-language"), values["digest.output_language"]);
  fillTimezones($("o-timezone"), values["general.timezone"]);
  openOn($("o-timezone"), values["general.timezone"]);
  const [morning, evening] = values["general.digest_schedule"] || [];
  if (morning) $("o-morning").value = parseInt(morning, 10);
  if (evening) $("o-evening").value = parseInt(evening, 10);
  // The stored webhook comes back masked, and a masked URL is not one Discord
  // would confirm, so that field opens empty; the mail pair is shown in full.
  $("o-email-to").value = values["notify.email_to"] ?? "";
  $("o-email-from").value = values["notify.email_from"] ?? "";
  keep(FIELDS);
  // With no stored zone the field opens on this browser's own, marked as
  // detected rather than stored: it differs from what was loaded, so Next
  // saves it and Skip leaves the setting unset.
  const zone = browserZone();
  if (values["general.timezone"] == null && zone) {
    $("o-timezone").value = zone;
    $("o-timezone-detected").hidden = false;
  }
}

route();

const SETTINGS_LOADED = fetch("/api/settings")
  .then((r) => (r.status === 401 ? Promise.reject(SESSION_EXPIRED) : r.json()))
  .then(render)
  .catch((e) => {
    show("err", e === SESSION_EXPIRED ? e.message
      : `Could not load settings: ${e}. Reload the page to try again.`);
  })
  .finally(() => document.querySelectorAll(".step [data-loading]").forEach((el) => el.remove()));

fetch("/api/onboarding/feeds")
  .then(async (r) => {
    if (r.status === 401) throw SESSION_EXPIRED;
    if (!r.ok) {
      const data = await r.json().catch(() => ({}));
      throw new Error(data.error || `cyris answered ${r.status}`);
    }
    return r.json();
  })
  .then(renderFeeds)
  .catch((e) => {
    $("o-feeds-loading").textContent = e === SESSION_EXPIRED ? e.message
      : `Could not load the feed list: ${e}. Reload the page to try again.`;
  });
